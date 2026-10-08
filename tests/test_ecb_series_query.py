"""Offline validation tests for the ECB YC and BPS query-only contracts."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.providers.ecb.models._series_query import (
    EcbBalanceOfPaymentsQuery,
    EcbYieldCurveQuery,
)

_YC_KEY = "YC.B.U2.EUR.4F.G_N_A.SV_C_YM.SR_3M"
_BPS_MONTH_KEY = "BPS.M.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_BPS_QUARTER_KEY = "BPS.Q.N.I10.W1.S121.S1.T.A.FA.R.F._Z.EUR.X1._X.N.ALL"
_MODELS = (
    (EcbYieldCurveQuery, _YC_KEY),
    (EcbBalanceOfPaymentsQuery, _BPS_MONTH_KEY),
)


def _key_with_dimension(key: str, dimension_index: int, token: str) -> str:
    parts = key.split(".")
    parts[dimension_index + 1] = token
    return ".".join(parts)


def _bps_query(key: str = _BPS_MONTH_KEY, **kwargs: Any) -> EcbBalanceOfPaymentsQuery:
    return EcbBalanceOfPaymentsQuery.model_validate({"series_key": key, **kwargs})


@pytest.mark.parametrize(
    ("model", "key"),
    [
        (EcbYieldCurveQuery, _YC_KEY),
        (EcbBalanceOfPaymentsQuery, _BPS_MONTH_KEY),
        (EcbBalanceOfPaymentsQuery, _BPS_QUARTER_KEY),
    ],
)
def test_exact_product_key_and_dimension_tokens_are_preserved(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
) -> None:
    query = model.model_validate({"series_key": key})
    assert query.series_key == key
    assert query.source == "auto"
    assert query.market is None
    assert query.symbol is None
    assert query.max_records == 10_000


@pytest.mark.parametrize(("model", "key"), _MODELS)
def test_each_nonfrequency_dimension_accepts_one_or_32_character_tokens(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
) -> None:
    dimension_count = 7 if model is EcbYieldCurveQuery else 17
    for index in range(1, dimension_count):
        for token in ("A", "Z" * 32):
            query = model.model_validate({"series_key": _key_with_dimension(key, index, token)})
            assert query.series_key.split(".")[index + 1] == token


@pytest.mark.parametrize(("model", "key"), _MODELS)
def test_wrong_prefix_dimension_count_or_frequency_is_rejected(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
) -> None:
    parts = key.split(".")
    parts[0] = "BPS" if model is EcbYieldCurveQuery else "YC"
    wrong_prefix = ".".join(parts)
    wrong_frequency_parts = key.split(".")
    wrong_frequency_parts[1] = "A" if model is EcbYieldCurveQuery else "Y"
    bad_keys = (
        "",
        wrong_prefix,
        f"{key}.EXTRA",
        ".".join(key.split(".")[:-1]),
        ".".join(wrong_frequency_parts),
    )
    for bad_key in bad_keys:
        with pytest.raises(ValidationError):
            model.model_validate({"series_key": bad_key})


@pytest.mark.parametrize(
    ("model", "key", "dimension_count"),
    [(EcbYieldCurveQuery, _YC_KEY, 7), (EcbBalanceOfPaymentsQuery, _BPS_MONTH_KEY, 17)],
)
@pytest.mark.parametrize("unsafe_token", ["", "A*", "A+B", "A/B", "%2F", "lower", "A" * 33])
def test_unsafe_or_oversized_dimension_tokens_are_rejected(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    dimension_count: int,
    unsafe_token: str,
) -> None:
    for index in range(1, dimension_count):
        candidate = _key_with_dimension(key, index, unsafe_token)
        with pytest.raises(ValidationError):
            model.model_validate({"series_key": candidate})


@pytest.mark.parametrize(("model", "key"), _MODELS)
@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "auto", "market": None, "symbol": None},
        {"source": "ecb", "market": "eu", "symbol": None},
    ],
)
def test_common_routing_fields_allow_only_declared_values(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    overrides: dict[str, object],
) -> None:
    query = model.model_validate({"series_key": key, **overrides})
    assert query.source == overrides["source"]
    assert query.market == overrides["market"]
    assert query.symbol is None


@pytest.mark.parametrize(("model", "key"), _MODELS)
@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "fred"},
        {"source": 1},
        {"market": "us"},
        {"market": 1},
        {"symbol": "x"},
        {"symbol": False},
    ],
)
def test_common_routing_fields_reject_unbound_values(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"series_key": key, **overrides})


@pytest.mark.parametrize(("model", "key"), _MODELS)
@pytest.mark.parametrize("maximum", [1, 10_000])
def test_record_budget_is_strict_and_inclusive(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    maximum: int,
) -> None:
    query = model.model_validate({"series_key": key, "max_records": maximum})
    assert query.max_records == maximum


@pytest.mark.parametrize(("model", "key"), _MODELS)
@pytest.mark.parametrize("maximum", [True, False, 0, 10_001, 1.0, "1", -1])
def test_record_budget_rejects_bool_float_string_and_out_of_range(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    maximum: object,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"series_key": key, "max_records": maximum})


@pytest.mark.parametrize("start", [date(2024, 1, 2), "2024-01-02"])
def test_yield_curve_accepts_date_object_and_canonical_date_text(start: date | str) -> None:
    query = EcbYieldCurveQuery.model_validate(
        {"series_key": _YC_KEY, "start_date": start, "end_date": "2024-12-31"}
    )
    assert query.start_date == date(2024, 1, 2)
    assert query.end_date == date(2024, 12, 31)


@pytest.mark.parametrize(
    "bad_date",
    [
        datetime(2024, 1, 2),
        datetime(2024, 1, 2, tzinfo=timezone.utc),
        "2024-1-02",
        "20240102",
        "2024-01-02T00:00:00",
        "2024-01-02Z",
        20240102,
        True,
    ],
)
def test_yield_curve_rejects_datetime_timestamp_and_noncanonical_date(
    bad_date: object,
) -> None:
    with pytest.raises(ValidationError):
        EcbYieldCurveQuery.model_validate({"series_key": _YC_KEY, "start_date": bad_date})


def test_yield_curve_date_window_is_ordered_and_roundtrips_json() -> None:
    query = EcbYieldCurveQuery.model_validate(
        {"series_key": _YC_KEY, "start_date": "2024-01-01", "end_date": date(2024, 12, 31)}
    )
    restored = EcbYieldCurveQuery.model_validate_json(
        json.dumps(query.model_dump(mode="json"), separators=(",", ":"))
    )
    assert restored == query
    with pytest.raises(ValidationError):
        EcbYieldCurveQuery.model_validate(
            {"series_key": _YC_KEY, "start_date": "2025-01-01", "end_date": "2024-12-31"}
        )


@pytest.mark.parametrize("extra", ["as_of", "filters", "history", "updatedAfter", "unknown"])
@pytest.mark.parametrize(("model", "key"), _MODELS)
def test_unstated_query_controls_and_unknown_fields_are_rejected(
    model: type[EcbYieldCurveQuery] | type[EcbBalanceOfPaymentsQuery],
    key: str,
    extra: str,
) -> None:
    with pytest.raises(ValidationError):
        model.model_validate({"series_key": key, extra: None})


@pytest.mark.parametrize(
    "period_window",
    [
        {"start_period": "2024-01", "end_period": "2024-12"},
        {"start_period": "2024-12", "end_period": "2025-01"},
    ],
)
def test_bps_month_period_windows_preserve_months_and_year_boundaries(
    period_window: dict[str, str],
) -> None:
    query = _bps_query(**period_window)
    assert query.start_period == period_window["start_period"]
    assert query.end_period == period_window["end_period"]
    restored = EcbBalanceOfPaymentsQuery.model_validate_json(
        json.dumps(query.model_dump(mode="json"), separators=(",", ":"))
    )
    assert restored == query
    assert restored.start_period == period_window["start_period"]


@pytest.mark.parametrize(
    "period_window",
    [
        {"start_period": "2024-Q1", "end_period": "2024-Q4"},
        {"start_period": "2024-Q4", "end_period": "2025-Q1"},
    ],
)
def test_bps_quarter_period_windows_preserve_quarters_and_year_boundaries(
    period_window: dict[str, str],
) -> None:
    query = _bps_query(_BPS_QUARTER_KEY, **period_window)
    assert query.start_period == period_window["start_period"]
    assert query.end_period == period_window["end_period"]


@pytest.mark.parametrize(
    ("key", "period"),
    [
        (_BPS_MONTH_KEY, "0001-01"),
        (_BPS_MONTH_KEY, "9999-12"),
        (_BPS_QUARTER_KEY, "0001-Q1"),
        (_BPS_QUARTER_KEY, "9999-Q4"),
    ],
)
def test_bps_period_accepts_inclusive_year_boundaries(key: str, period: str) -> None:
    assert _bps_query(key, start_period=period).start_period == period


@pytest.mark.parametrize(
    ("key", "period"),
    [
        (_BPS_MONTH_KEY, "2024-Q1"),
        (_BPS_QUARTER_KEY, "2024-01"),
        (_BPS_MONTH_KEY, "2024-00"),
        (_BPS_MONTH_KEY, "2024-13"),
        (_BPS_MONTH_KEY, "2024-1"),
        (_BPS_QUARTER_KEY, "2024-Q0"),
        (_BPS_QUARTER_KEY, "2024-Q5"),
        (_BPS_QUARTER_KEY, "2024-Q"),
        (_BPS_MONTH_KEY, "0000-01"),
        (_BPS_QUARTER_KEY, "0000-Q1"),
    ],
)
def test_bps_period_format_must_match_frequency_and_calendar(
    key: str,
    period: str,
) -> None:
    with pytest.raises(ValidationError):
        _bps_query(key, start_period=period)


@pytest.mark.parametrize(
    ("start_period", "end_period"),
    [("2025-01", "2024-12"), ("2025-Q1", "2024-Q4")],
)
def test_bps_period_window_must_be_ordered(start_period: str, end_period: str) -> None:
    key = _BPS_QUARTER_KEY if "Q" in start_period else _BPS_MONTH_KEY
    with pytest.raises(ValidationError):
        _bps_query(key, start_period=start_period, end_period=end_period)


@pytest.mark.parametrize("date_field", ["start_date", "end_date"])
@pytest.mark.parametrize("date_value", ["2024-01-01", date(2024, 1, 1)])
def test_bps_explicitly_rejects_calendar_dates(
    date_field: str,
    date_value: str | date,
) -> None:
    with pytest.raises(ValidationError):
        _bps_query(**{date_field: date_value})


def test_bps_allows_only_null_date_overrides() -> None:
    query = _bps_query(start_date=None, end_date=None)
    assert query.start_date is None
    assert query.end_date is None
