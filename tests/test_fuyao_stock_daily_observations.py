"""Offline checks for mapping pilot observations to THS stock-daily ODS rows."""

from __future__ import annotations

import copy
import math
from datetime import date
from typing import TYPE_CHECKING

import pytest

from opendata.data.providers.ths._source import SOURCE
from opendata.data.providers.ths.endpoints import PRICES_ENDPOINT, shanghai_midnight_millis
from opendata.pipeline import fuyao_stock_daily_observations as adapter
from opendata.pipeline.fuyao_stock_daily_observations import (
    ODS_STOCK_DAILY_COLUMNS,
    FuyaoStockDailyObservationError,
    prepare_fuyao_stock_daily_observations,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd

_FIELD_UNITS = {
    "open_price": "provider-native value; not independently calibrated",
    "high_price": "provider-native value; not independently calibrated",
    "low_price": "provider-native value; not independently calibrated",
    "close_price": "provider-native value; not independently calibrated",
    "volume": "provider contract label: shares; not independently calibrated",
    "turnover": "provider contract label: CNY; not independently calibrated",
}


def _source_fields(day: str, **updates: object) -> dict[str, object]:
    """Build one synthetic source-native observation row."""
    values: dict[str, object] = {
        "date_ms": shanghai_midnight_millis(date.fromisoformat(day)),
        "open_price": 100.0,
        "high_price": 103.0,
        "low_price": 99.0,
        "close_price": 102.0,
        "volume": 10_000,
        "turnover": 1_020_000.0,
    }
    values.update(updates)
    return values


def _observation(day: str, **source_updates: object) -> dict[str, object]:
    """Build one pilot-shaped daily observation for a fixed synthetic request."""
    return {
        "symbol": "600519.SH",
        "trade_date": day,
        "adjustment_basis": "unadjusted",
        "source_fields": _source_fields(day, **source_updates),
    }


def _document(rows: list[dict[str, object]] | None = None) -> dict[str, object]:
    """Build the exact observations document schema emitted by the pilot."""
    selected_rows = (
        rows
        if rows is not None
        else [
            _observation("2026-10-20"),
            _observation("2026-10-19"),
        ]
    )
    return {
        "schema_version": 1,
        "source": SOURCE,
        "vendor": "Fuyao (THS) REST API",
        "endpoint_path": PRICES_ENDPOINT,
        "request": {
            "symbol": "600519.SH",
            "start_date": "2026-10-19",
            "end_date": "2026-10-23",
            "adjustment_basis": "unadjusted",
            "provider_adjust_parameter": "none",
        },
        "time_field": "date_ms",
        "time_semantics": "Unix milliseconds at Asia/Shanghai trading-date midnight",
        "field_units": dict(_FIELD_UNITS),
        "rows": selected_rows,
    }


def _assert_rejected(document: dict[str, object], code: str) -> None:
    """Assert a malformed synthetic document fails with its stable code."""
    with pytest.raises(FuyaoStockDailyObservationError) as captured:
        prepare_fuyao_stock_daily_observations(document)
    assert captured.value.code == code
    assert str(captured.value) == code


def test_maps_exact_pilot_rows_to_schema_and_preserves_native_values() -> None:
    """Map schema fields, keep native numeric scalars, and leave unknowns null."""
    document = _document(
        [
            _observation("2026-10-20", open_price=101, volume=9_007_199_254_740_993),
            _observation("2026-10-19", close_price=101.25),
        ]
    )

    frame = prepare_fuyao_stock_daily_observations(document)

    assert tuple(frame.columns) == ODS_STOCK_DAILY_COLUMNS
    assert frame["thscode"].tolist() == ["600519.SH", "600519.SH"]
    assert frame["trade_date"].tolist() == [date(2026, 10, 19), date(2026, 10, 20)]
    assert frame["date_ms"].tolist() == [
        shanghai_midnight_millis(date(2026, 10, 19)),
        shanghai_midnight_millis(date(2026, 10, 20)),
    ]
    assert frame["open_price"].tolist() == [100.0, 101]
    assert frame.loc[1, "volume"] == 9_007_199_254_740_993
    assert type(frame.loc[1, "volume"]) is int
    assert frame["turnover"].tolist() == [1_020_000.0, 1_020_000.0]
    assert frame["currency"].isna().all()
    assert frame["interval"].isna().all()
    assert frame["adjusted"].tolist() == ["none", "none"]
    assert not {"_source", "_fetched_at", "_batch_id", "captured_at"}.intersection(frame.columns)
    assert frame.attrs["field_units"] == _FIELD_UNITS
    assert frame.attrs["source_authenticity_status"] == "unverified"
    assert frame.attrs["capture_time_status"] == "unknown"


@pytest.mark.parametrize(
    ("mutate", "error_code"),
    [
        (lambda doc: doc.update(extra="unreviewed"), "document_schema_invalid"),
        (lambda doc: doc.update({None: "invalid-key"}), "document_schema_invalid"),
        (lambda doc: doc.update(schema_version=True), "document_schema_invalid"),
        (lambda doc: doc.update(schema_version=2), "document_schema_invalid"),
        (lambda doc: doc.update(source="invented"), "document_source_label_invalid"),
        (lambda doc: doc.update(vendor="unknown"), "document_vendor_label_invalid"),
        (lambda doc: doc.update(endpoint_path="/other"), "document_endpoint_invalid"),
        (lambda doc: doc.update(time_field="timestamp"), "document_time_field_invalid"),
        (
            lambda doc: doc.update(time_semantics="UTC calendar date"),
            "document_time_semantics_invalid",
        ),
        (lambda doc: doc["field_units"].update(volume="lots"), "document_field_units_invalid"),
        (lambda doc: doc.update(field_units=[]), "document_field_units_invalid"),
        (lambda doc: doc.update(request=[]), "request_schema_invalid"),
        (lambda doc: doc["request"].update(extra="x"), "request_schema_invalid"),
        (lambda doc: doc["request"].update(symbol="bad"), "instrument_symbol_invalid"),
        (lambda doc: doc["request"].update(start_date="2026-10-24"), "request_date_invalid"),
        (lambda doc: doc["request"].update(end_date="2026-10-18"), "request_date_invalid"),
        (lambda doc: doc["request"].update(adjustment_basis="qfq"), "adjustment_basis_unsupported"),
        (
            lambda doc: doc["request"].update(provider_adjust_parameter="forward"),
            "adjustment_basis_unsupported",
        ),
        (lambda doc: doc.update(rows=[]), "observations_empty_or_invalid"),
        (lambda doc: doc.update(rows=()), "observations_empty_or_invalid"),
        (lambda doc: doc.update(rows=[None]), "observation_schema_invalid"),
        (lambda doc: doc["rows"][0].update(symbol="000001.SZ"), "observation_symbol_mismatch"),
        (
            lambda doc: doc["rows"][0].update(trade_date="2026-10-24"),
            "observation_date_outside_request",
        ),
        (
            lambda doc: doc["rows"][0].update(trade_date="2026-02-31"),
            "observation_date_invalid",
        ),
        (
            lambda doc: doc["rows"][0].update(adjustment_basis="forward"),
            "adjustment_basis_unsupported",
        ),
        (
            lambda doc: doc["rows"][0].update(extra="unreviewed"),
            "observation_schema_invalid",
        ),
        (
            lambda doc: doc["rows"][0].update(source_fields=[]),
            "source_fields_schema_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(extra=1),
            "source_fields_schema_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].pop("turnover"),
            "source_fields_schema_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(date_ms=True),
            "observation_date_ms_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(date_ms=10**1000),
            "observation_date_ms_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(
                date_ms=shanghai_midnight_millis(date(2026, 10, 19))
            ),
            "observation_date_ms_mismatch",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(volume=True),
            "observation_numeric_value_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(volume=math.nan),
            "observation_numeric_value_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(volume="10000"),
            "observation_numeric_value_invalid",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(turnover=-1),
            "observation_value_negative",
        ),
        (
            lambda doc: doc["rows"][0]["source_fields"].update(high_price=100),
            "observation_ohlc_inconsistent",
        ),
    ],
)
def test_rejects_schema_identity_date_adjustment_and_value_drift(
    mutate: Callable[[dict[str, object]], object], error_code: str
) -> None:
    """Fail closed when any declared source contract or row invariant drifts."""
    document = _document()
    mutate(document)
    _assert_rejected(document, error_code)


def test_rejects_duplicate_business_keys_even_when_values_differ() -> None:
    """A changed payload cannot create a second row for the same instrument day."""
    document = _document(
        [
            _observation("2026-10-19", close_price=101),
            _observation("2026-10-19", close_price=102),
        ]
    )

    _assert_rejected(document, "observation_business_key_duplicate")


@pytest.mark.parametrize(
    ("field", "value", "error_code"),
    [
        ("open_price", 0, "observation_price_not_positive"),
        ("low_price", 104, "observation_ohlc_inconsistent"),
        ("close_price", float("inf"), "observation_numeric_value_invalid"),
        ("volume", -1, "observation_value_negative"),
    ],
)
def test_rejects_nonpositive_or_inconsistent_bar_values(
    field: str, value: object, error_code: str
) -> None:
    """Reject invalid OHLC constraints and negative volume without coercion."""
    document = _document([_observation("2026-10-19", **{field: value})])

    _assert_rejected(document, error_code)


def test_does_not_mutate_the_caller_owned_document() -> None:
    """Preparation leaves the input observation artifact unchanged."""
    document = _document()
    before = copy.deepcopy(document)

    prepare_fuyao_stock_daily_observations(document)

    assert document == before


def test_wraps_existing_ods_mapping_failures_without_exposing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Translate a downstream schema conversion failure into a stable code."""

    def fail_mapping(frame: pd.DataFrame) -> None:
        raise ValueError("synthetic private row detail")

    monkeypatch.setattr(adapter, "prepare_daily_k_frame", fail_mapping)
    with pytest.raises(FuyaoStockDailyObservationError) as captured:
        prepare_fuyao_stock_daily_observations(_document())

    assert captured.value.code == "ods_mapping_failed"
    assert "synthetic private row detail" not in str(captured.value)
