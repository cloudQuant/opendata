"""Offline validation for immutable provider extractor-result captures."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import FrozenInstanceError, replace
from datetime import date, datetime

import pytest

from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery
from opendata.pipeline.provider_model_raw import (
    MAX_JSON_DEPTH,
    MAX_QUERY_CONTEXT_BYTES,
    MAX_RAW_PAYLOAD_BYTES,
    MAX_RAW_ROWS,
    NativeRawCapture,
    make_provider_model_capture,
    validate_provider_model_capture,
)


def _fred_query(**updates: object) -> FredSeriesQuery:
    return FredSeriesQuery.model_validate(
        {
            "source": "fred",
            "market": "us",
            "series_id": "CPIAUCSL",
            "start_date": date(2024, 1, 1),
            "end_date": date(2024, 2, 1),
            **updates,
        }
    )


def _bls_query(**updates: object) -> BlsSeriesQuery:
    return BlsSeriesQuery.model_validate(
        {
            "source": "bls",
            "market": "us",
            "series_ids": ["LNS14000000"],
            "start_year": 2025,
            "end_year": 2025,
            "max_requests": 2,
            **updates,
        }
    )


def _fmp_query(**updates: object) -> EquityHistoricalQuery:
    return EquityHistoricalQuery.model_validate(
        {"source": "fmp", "market": "us", "symbol": "BRK.B", **updates}
    )


def _capture(
    *,
    source: str = "fred",
    model: str = "FredSeries",
    raw: list[dict[str, object]] | None = None,
    params: object | None = None,
) -> NativeRawCapture:
    selected_params = _fred_query() if params is None else params
    return make_provider_model_capture(
        source=source,
        model=model,
        raw=[] if raw is None else raw,
        params=selected_params,  # type: ignore[arg-type]
    )


def test_three_provider_captures_preserve_raw_extractor_shape() -> None:
    fred_raw: list[dict[str, object]] = [
        {
            "date": "2024-01-01",
            "value": ".",
            "realtime_start": "2024-01-01",
            "realtime_end": "2024-02-01",
        },
        {
            "date": "2024-01-01",
            "value": "3.1",
            "realtime_start": "2024-02-02",
            "realtime_end": "9999-12-31",
        },
    ]
    fred_capture = _capture(raw=fred_raw)
    fred_rows = (
        SeriesObservation(
            series_id="CPIAUCSL",
            date=date(2024, 1, 1),
            value=None,
            realtime_start=date(2024, 1, 1),
            realtime_end=date(2024, 2, 1),
            transform_units="lin",
            output_type=1,
            requested_frequency=None,
            requested_aggregation_method="avg",
        ),
        SeriesObservation(
            series_id="CPIAUCSL",
            date=date(2024, 1, 1),
            value=3.1,
            realtime_start=date(2024, 2, 2),
            realtime_end=date(9999, 12, 31),
            transform_units="lin",
            output_type=1,
            requested_frequency=None,
            requested_aggregation_method="avg",
        ),
    )
    assert fred_capture.raw_row_count == 2
    assert json.loads(fred_capture.raw_json)[0]["value"] == "."
    assert fred_rows[0].value is None
    assert fred_capture.query_json == json.dumps(
        _fred_query().model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )

    bls_raw = [
        {
            "series_id": "LNS14000000",
            "year": "2025",
            "period": "M13",
            "period_name": "Annual average",
            "value": "4.1",
            "footnotes": [{"code": "P", "text": "Preliminary."}],
            "latest": None,
            "api_version": "v2",
        }
    ]
    bls_capture = _capture(
        source="bls",
        model="BlsSeries",
        params=_bls_query(),
        raw=bls_raw,
    )
    bls_row = BlsObservation(
        series_id="LNS14000000",
        year=2025,
        period="M13",
        period_name="Annual average",
        value=4.1,
        footnotes=(BlsFootnote(code="P", text="Preliminary."),),
        latest=None,
        preliminary=True,
        api_version="v2",
    )
    assert json.loads(bls_capture.raw_json)[0]["year"] == "2025"
    assert type(bls_row.year) is int
    assert json.loads(bls_capture.raw_json)[0]["period"] == bls_row.period == "M13"

    big_volume = 2**64 + 1
    fmp_raw = [
        {
            "symbol": "BRK.B",
            "date": "2026-01-02",
            "open": 100.0,
            "high": 103.0,
            "low": 99.5,
            "close": 102.0,
            "volume": big_volume,
            "change": -0.0,
            "changePercent": 0.25,
            "vwap": 101.0,
        }
    ]
    fmp_capture = _capture(
        source="fmp",
        model="EquityHistorical",
        params=_fmp_query(),
        raw=fmp_raw,
    )
    fmp_row = EquityHistorical(
        symbol="BRK.B",
        date=date(2026, 1, 2),
        open=100.0,
        high=103.0,
        low=99.5,
        close=102.0,
        volume=big_volume,
        change=-0.0,
        change_percent=0.25,
        vwap=101.0,
        currency=None,
        currency_semantics="source_unverified",
        volume_unit=None,
        volume_unit_semantics="source_unverified",
        query_window_scope="provider_default_unknown",
        window_boundary_semantics="source_unverified",
        provider_default_window_semantics="source_unverified",
        close_adjustment_semantics="split_adjusted_per_source_faq",
        adj_close_provided=False,
    )
    raw_fmp_row = json.loads(fmp_capture.raw_json)[0]
    assert raw_fmp_row["volume"] == big_volume
    assert type(raw_fmp_row["volume"]) is int
    assert math.copysign(1.0, raw_fmp_row["change"]) == -1.0
    assert fmp_row.volume == big_volume
    assert fmp_row.change == 0.0 and math.copysign(1.0, fmp_row.change) == -1.0


def test_query_context_is_complete_canonical_query_without_transport_budget() -> None:
    query = _bls_query(max_requests=7)
    capture = _capture(source="bls", model="BlsSeries", params=query)
    payload = json.loads(capture.query_json)
    assert payload == query.model_dump(mode="json")
    assert payload["source"] == "bls" and payload["market"] == "us"
    assert payload["max_requests"] == 7
    assert "request_budget" not in payload
    assert "deadline" not in payload
    assert "headers" not in payload and "api_key" not in payload
    assert capture.raw_scope == "extract_data_output"
    assert capture.schema_version == 1


def test_capture_copies_raw_values_and_is_frozen() -> None:
    raw = [{"ordered": ["first", {"nested": 2**80}], "negative_zero": -0.0}]
    capture = _capture(raw=raw)
    before = capture.raw_json
    raw[0]["ordered"].append("later")  # type: ignore[union-attr]
    assert capture.raw_json == before
    assert json.loads(capture.raw_json)[0]["ordered"] == ["first", {"nested": 2**80}]
    with pytest.raises(FrozenInstanceError):
        capture.raw_json = "[]"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("source", "model", "params"),
    [
        ("auto", "FredSeries", _fred_query()),
        ("fred", "FredSearch", _fred_query()),
        ("bls", "BlsSeries", _fred_query()),
        ("fmp", "EquityHistorical", _fred_query()),
        ("fred", "FredSeries", _fred_query(source="auto")),
        ("fred", "FredSeries", _fred_query(market="cn")),
        ("fred", "FredSeries", _fred_query(market=None)),
    ],
)
def test_capture_requires_exact_source_model_query_and_market_binding(
    source: str,
    model: str,
    params: object,
) -> None:
    with pytest.raises(ValueError):
        _capture(source=source, model=model, params=params)


def test_capture_rejects_unvalidated_query_extras_without_echoing_them() -> None:
    query = _fred_query()
    query.__dict__["api_key"] = "super-secret-query-key"
    with pytest.raises(ValueError) as exc_info:
        _capture(params=query)
    assert "super-secret-query-key" not in str(exc_info.value)


def test_capture_validation_rejects_invalid_unicode_without_echoing_content() -> None:
    capture = _capture()
    forged = replace(capture, raw_json='[{"value":"' + chr(0xD800) + '"}]')
    with pytest.raises(ValueError) as exc_info:
        validate_provider_model_capture(source="fred", model="FredSeries", capture=forged)
    assert "invalid Unicode" in str(exc_info.value)


@pytest.mark.parametrize(
    "raw",
    [
        (),
        {"row": 1},
        [[]],
        [{1: "non-string-key"}],
        [{"bytes": b"not-json"}],
        [{"float": float("nan")}],
        [{"float": float("inf")}],
        [{"float": float("-inf")}],
        [{"tuple": (1, 2)}],
        [{"date": date(2026, 10, 8)}],
        [{"datetime": datetime(2026, 10, 8, 12, 30)}],
    ],
)
def test_capture_rejects_non_json_raw_shapes(raw: object) -> None:
    with pytest.raises(ValueError):
        _capture(raw=raw)  # type: ignore[arg-type]


def test_capture_rejects_cycles_depth_and_row_limit() -> None:
    cyclic_raw: list[dict[str, object]] = []
    nested: dict[str, object] = {"loop": cyclic_raw}
    cyclic_raw.append(nested)
    with pytest.raises(ValueError, match="cycle"):
        _capture(raw=cyclic_raw)

    nested_value: object = "leaf"
    for _ in range(MAX_JSON_DEPTH + 2):
        nested_value = [nested_value]
    with pytest.raises(ValueError, match="depth"):
        _capture(raw=[{"nested": nested_value}])

    with pytest.raises(ValueError, match="row limit"):
        _capture(raw=[{}] * (MAX_RAW_ROWS + 1))


def test_capture_rejects_raw_and_query_byte_limits() -> None:
    with pytest.raises(ValueError, match="size limit"):
        _capture(raw=[{"payload": "x" * MAX_RAW_PAYLOAD_BYTES}])

    long_query = _fred_query(series_id="X" * MAX_QUERY_CONTEXT_BYTES)
    with pytest.raises(ValueError, match="reviewed contract"):
        _capture(params=long_query)


@pytest.mark.parametrize(
    "mutation",
    [
        {"raw_sha256": "0" * 64},
        {"raw_row_count": 1},
        {"source": "bls"},
        {"schema_version": True},
        {"raw_scope": "wire_bytes"},
    ],
)
def test_capture_writer_validation_rechecks_all_outer_fields(
    mutation: dict[str, object],
) -> None:
    capture = _capture()
    forged = replace(capture, **mutation)
    with pytest.raises(ValueError):
        validate_provider_model_capture(source="fred", model="FredSeries", capture=forged)


def test_capture_writer_validation_rechecks_canonical_json_hashes_and_query_contract() -> None:
    capture = _capture(raw=[{"v": 1}])
    noncanonical = replace(
        capture,
        raw_json='[ {"v":1} ]',
        raw_sha256=hashlib.sha256(b'[ {"v":1} ]').hexdigest(),
    )
    with pytest.raises(ValueError, match="canonical"):
        validate_provider_model_capture(source="fred", model="FredSeries", capture=noncanonical)

    query_with_extra = json.loads(capture.query_json)
    query_with_extra["api_key"] = "secret"
    extra_json = json.dumps(
        query_with_extra,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )
    forged_query = replace(
        capture,
        query_json=extra_json,
        query_sha256=hashlib.sha256(extra_json.encode()).hexdigest(),
    )
    with pytest.raises(ValueError):
        validate_provider_model_capture(source="fred", model="FredSeries", capture=forged_query)

    duplicate_raw = replace(
        capture,
        raw_json='[{"v":1,"v":2}]',
        raw_sha256=hashlib.sha256(b'[{"v":1,"v":2}]').hexdigest(),
    )
    with pytest.raises(ValueError):
        validate_provider_model_capture(source="fred", model="FredSeries", capture=duplicate_raw)
