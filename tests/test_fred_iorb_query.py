"""Strict query compatibility and boundary tests for FRED IORB."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from opendata.data.providers.fred.models._iorb_query import FredIorbQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery

TRANSFORMS = ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")
FREQUENCIES = (
    None,
    "d",
    "w",
    "bw",
    "m",
    "q",
    "sa",
    "a",
    "wef",
    "weth",
    "wew",
    "wetu",
    "wem",
    "wesu",
    "wesa",
    "bwew",
    "bwem",
)


def query(**updates: object) -> FredIorbQuery:
    return FredIorbQuery.model_validate(updates)


@pytest.mark.parametrize("transform_units", TRANSFORMS)
def test_every_inherited_transform_unit_is_available(transform_units: str) -> None:
    assert query(transform_units=transform_units).transform_units == transform_units


@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_all_inherited_frequency_options_are_available(frequency: str | None) -> None:
    assert query(frequency=frequency).frequency == frequency


@pytest.mark.parametrize("aggregation", ["avg", "sum", "eop"])
def test_aggregation_methods_are_preserved(aggregation: str) -> None:
    assert query(aggregation_method=aggregation).aggregation_method == aggregation


@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_sort_order_is_preserved(sort_order: str) -> None:
    assert query(sort_order=sort_order).sort_order == sort_order


@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_output_type_keeps_exact_supported_integer(output_type: int) -> None:
    assert query(output_type=output_type).output_type == output_type


@pytest.mark.parametrize("output_type", [True, False, 1.0, "1", 0, 5])
def test_output_type_rejects_boolean_float_string_and_out_of_range(output_type: object) -> None:
    with pytest.raises(ValidationError):
        query(output_type=output_type)


def test_fixed_series_source_market_and_symbol_contract() -> None:
    default = query()
    assert default.series_id == "IORB"
    assert default.source == "auto"
    assert default.market is None
    assert default.symbol is None
    assert query(source="fred", market="us").source == "fred"


@pytest.mark.parametrize(
    "updates",
    [
        {"series_id": "SOFR"},
        {"series_id": "iorb"},
        {"series_id": "IORB "},
        {"source": "unknown"},
        {"market": "gb"},
        {"symbol": "IORB"},
        {"unexpected": "typo"},
    ],
)
def test_wrong_source_identity_or_unknown_field_fails_closed(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        query(**updates)


@pytest.mark.parametrize(
    "field", ["start_date", "end_date", "as_of", "realtime_start", "realtime_end"]
)
@pytest.mark.parametrize(
    "bad_date",
    [
        datetime(2025, 1, 2),
        datetime(2025, 1, 2, tzinfo=timezone.utc),
        1735776000,
        20250102,
        "2025-1-2",
        "2025-02-30",
    ],
)
def test_query_date_fields_reject_timestamp_datetime_and_bad_text(
    field: str, bad_date: object
) -> None:
    with pytest.raises(ValidationError):
        query(**{field: bad_date})


def test_query_date_fields_accept_exact_date_and_canonical_strings() -> None:
    item = query(start_date=date(1, 1, 1), end_date="9999-12-31")
    assert item.start_date == date(1, 1, 1)
    assert item.end_date == date(9999, 12, 31)


def test_date_subclasses_are_rejected_in_query_fields_and_vintages() -> None:
    class DateChild(date):
        pass

    for payload in (
        {"start_date": DateChild(2025, 1, 2)},
        {"vintage_dates": [DateChild(2025, 1, 2)]},
    ):
        with pytest.raises(ValidationError):
            query(**payload)


def test_vintage_dates_preserve_order_and_duplicates() -> None:
    item = query(vintage_dates=["2025-01-02", date(2024, 1, 2), "2025-01-02"])
    assert item.vintage_dates == [date(2025, 1, 2), date(2024, 1, 2), date(2025, 1, 2)]


def test_vintage_date_limit_is_two_thousand() -> None:
    dates = [date(2025, 1, 1)] * 2000
    assert len(query(vintage_dates=dates).vintage_dates or []) == 2000
    with pytest.raises(ValidationError):
        query(vintage_dates=[date(2025, 1, 1)] * 2001)


@pytest.mark.parametrize("bad", [[], (), "2025-01-01", 4, ["2025-1-1"], ["2025-02-30"]])
def test_vintage_dates_reject_empty_wrong_container_and_bad_items(bad: object) -> None:
    with pytest.raises(ValidationError):
        query(vintage_dates=bad)


def test_inherited_date_window_order_and_selector_exclusivity() -> None:
    for payload in (
        {"start_date": "2025-01-03", "end_date": "2025-01-02"},
        {"realtime_start": "2025-01-03", "realtime_end": "2025-01-02"},
        {"as_of": "2025-01-02", "realtime_start": "2025-01-02"},
        {"vintage_dates": ["2025-01-02"], "as_of": "2025-01-02"},
        {"vintage_dates": ["2025-01-02"], "realtime_end": "2025-01-02"},
    ):
        with pytest.raises(ValidationError):
            query(**payload)


def test_pagination_retains_inherited_limits_and_strict_integer_types() -> None:
    item = query(page_size=1, offset=0, max_records=1, max_pages=1)
    assert (item.page_size, item.offset, item.max_records, item.max_pages) == (1, 0, 1, 1)
    for payload in (
        {"page_size": True},
        {"page_size": 1.0},
        {"page_size": "1"},
        {"offset": -1},
        {"max_records": 0},
        {"max_pages": 101},
    ):
        with pytest.raises(ValidationError):
            query(**payload)


def test_json_round_trip_and_exact_parent_query_compatibility() -> None:
    item = query(
        start_date="2025-01-01",
        end_date="2025-01-31",
        transform_units="pch",
        frequency="m",
        output_type=3,
        vintage_dates=["2025-01-02", "2025-01-01"],
    )
    assert FredIorbQuery.model_validate_json(item.model_dump_json()) == item
    generic = FredSeriesQuery.model_validate({"series_id": "IORB"})
    assert generic.series_id == "IORB"
    assert type(generic) is FredSeriesQuery


def test_constructed_or_copied_invalid_query_rejects_when_revalidated() -> None:
    constructed = FredIorbQuery.model_construct(series_id="SOFR")
    copied = query().model_copy(update={"series_id": "SOFR"})
    for malformed in (constructed, copied):
        with pytest.raises(ValidationError):
            FredIorbQuery.model_validate(malformed)
