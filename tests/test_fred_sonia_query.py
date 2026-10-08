"""Synthetic tests for the strict FRED SONIA query contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery

if TYPE_CHECKING:
    from collections.abc import Mapping


_SERIES_BY_PARAMETER = {
    "rate": "IUDSOIA",
    "index": "IUDZOS2",
    "10th_percentile": "IUDZLS6",
    "25th_percentile": "IUDZLS7",
    "75th_percentile": "IUDZLS8",
    "90th_percentile": "IUDZLS9",
    "total_nominal_value": "IUDZLT2",
}
_TRANSFORM_UNITS = ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")
_FREQUENCIES = (
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


def _values(**updates: object) -> dict[str, object]:
    values: dict[str, object] = {"series_id": "IUDSOIA"}
    values.update(updates)
    return values


def _assert_rejected(values: Mapping[str, object]) -> None:
    with pytest.raises(ValidationError):
        FredSoniaQuery.model_validate(values)


@pytest.mark.parametrize(("parameter", "series_id"), _SERIES_BY_PARAMETER.items())
def test_parameter_binds_its_default_series_id_and_accepts_explicit_match(
    parameter: str, series_id: str
) -> None:
    query = FredSoniaQuery.model_validate({"parameter": parameter})
    explicit = FredSoniaQuery.model_validate({"parameter": parameter, "series_id": series_id})

    assert (query.parameter, query.series_id) == (parameter, series_id)
    assert (explicit.parameter, explicit.series_id) == (parameter, series_id)


def test_no_selector_defaults_to_rate_and_iudsoia() -> None:
    query = FredSoniaQuery.model_validate({})

    assert query.parameter == "rate"
    assert query.series_id == "IUDSOIA"
    assert query.source == "auto"
    assert query.market is None
    assert query.symbol is None
    assert query.transform_units == "lin"
    assert query.output_type == 1
    assert query.frequency is None
    assert query.aggregation_method == "avg"
    assert query.sort_order == "asc"
    assert (query.page_size, query.offset, query.max_records, query.max_pages) == (
        100_000,
        0,
        200_000,
        2,
    )
    assert FredSeriesQuery.model_fields["page_size"].default == query.page_size
    assert FredSeriesQuery.model_fields["max_records"].default == query.max_records
    assert FredSeriesQuery.model_fields["max_pages"].default == query.max_pages


@pytest.mark.parametrize(
    ("parameter", "series_id"),
    [
        ("rate", "IUDZOS2"),
        ("index", "IUDSOIA"),
        ("10th_percentile", "IUDZLS7"),
        ("25th_percentile", "IUDZLS8"),
        ("75th_percentile", "IUDZLS9"),
        ("90th_percentile", "IUDZLT2"),
        ("total_nominal_value", "IUDZLS6"),
    ],
)
def test_explicit_selector_must_match_series_id(parameter: str, series_id: str) -> None:
    _assert_rejected(_values(parameter=parameter, series_id=series_id))


@pytest.mark.parametrize(
    ("parameter", "series_id"),
    [
        ("Rate", "IUDSOIA"),
        (" rate", "IUDSOIA"),
        ("rate ", "IUDSOIA"),
        ("unsupported", "IUDSOIA"),
        ("rate", " IUDSOIA"),
        ("rate", "IUDSOIA "),
        ("rate", "iudsoia"),
        ("rate", "GDP"),
    ],
)
def test_parameter_and_series_id_are_never_trimmed_or_case_normalized(
    parameter: str, series_id: str
) -> None:
    _assert_rejected(_values(parameter=parameter, series_id=series_id))


@pytest.mark.parametrize("transform_units", _TRANSFORM_UNITS)
def test_every_existing_transform_unit_is_supported(transform_units: str) -> None:
    query = FredSoniaQuery.model_validate(_values(transform_units=transform_units))

    assert query.transform_units == transform_units


@pytest.mark.parametrize("frequency", [None, *_FREQUENCIES])
def test_every_existing_frequency_and_none_are_supported(frequency: str | None) -> None:
    query = FredSoniaQuery.model_validate(_values(frequency=frequency))

    assert query.frequency == frequency


@pytest.mark.parametrize("field_name", ["transform_units", "frequency"])
def test_unknown_transform_and_frequency_values_are_rejected(field_name: str) -> None:
    _assert_rejected(_values(**{field_name: "unsupported"}))


@pytest.mark.parametrize("aggregation_method", ["avg", "sum", "eop"])
def test_every_existing_aggregation_method_is_supported(aggregation_method: str) -> None:
    query = FredSoniaQuery.model_validate(_values(aggregation_method=aggregation_method))

    assert query.aggregation_method == aggregation_method


@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_both_sort_orders_are_preserved(sort_order: str) -> None:
    assert FredSoniaQuery.model_validate(_values(sort_order=sort_order)).sort_order == sort_order


@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_all_output_types_accept_exact_integer_values(output_type: int) -> None:
    assert (
        FredSoniaQuery.model_validate(_values(output_type=output_type)).output_type == output_type
    )


@pytest.mark.parametrize("output_type", [True, False, 1.0, "1", "4", 0, 5])
def test_output_type_rejects_boolean_float_string_and_out_of_range_values(
    output_type: object,
) -> None:
    _assert_rejected(_values(output_type=output_type))


class _DateSubclass(date):
    pass


@pytest.mark.parametrize(
    "field_name",
    ["start_date", "end_date", "as_of", "realtime_start", "realtime_end"],
)
@pytest.mark.parametrize(
    "bad_date",
    [
        datetime(2026, 1, 1),
        _DateSubclass(2026, 1, 1),
        1767225600,
        20260101.0,
        "2026-1-1",
        "20260101",
        "2026-02-30",
        "0000-01-01",
        "10000-01-01",
    ],
)
def test_query_dates_reject_datetime_subclasses_epochs_and_noncanonical_strings(
    field_name: str, bad_date: object
) -> None:
    _assert_rejected(_values(**{field_name: bad_date}))


def test_date_year_boundaries_and_valid_date_objects_are_accepted() -> None:
    query = FredSoniaQuery.model_validate(
        _values(
            start_date="0001-01-01",
            end_date=date(9999, 12, 31),
            realtime_start="0001-01-01",
            realtime_end="9999-12-31",
        )
    )

    assert query.start_date == date(1, 1, 1)
    assert query.end_date == date(9999, 12, 31)
    assert query.realtime_start == date(1, 1, 1)
    assert query.realtime_end == date(9999, 12, 31)


@pytest.mark.parametrize(
    "field_name",
    ["start_date", "end_date", "as_of", "realtime_start", "realtime_end"],
)
def test_query_dates_accept_none(field_name: str) -> None:
    assert getattr(FredSoniaQuery.model_validate(_values(**{field_name: None})), field_name) is None


@pytest.mark.parametrize(
    "updates",
    [
        {"start_date": "2026-02-01", "end_date": "2026-01-01"},
        {"realtime_start": "2026-02-01", "realtime_end": "2026-01-01"},
    ],
)
def test_observation_and_realtime_window_order_is_enforced(updates: dict[str, object]) -> None:
    _assert_rejected(_values(**updates))


@pytest.mark.parametrize(
    "updates",
    [
        {"as_of": "2026-01-01", "realtime_start": "2026-01-01"},
        {"as_of": "2026-01-01", "realtime_end": "2026-01-01"},
        {"as_of": "2026-01-01", "realtime_start": "2026-01-01", "realtime_end": "2026-01-02"},
        {"vintage_dates": ["2026-01-01"], "as_of": "2026-01-01"},
        {"vintage_dates": ["2026-01-01"], "realtime_start": "2026-01-01"},
        {"vintage_dates": ["2026-01-01"], "realtime_end": "2026-01-01"},
    ],
)
def test_realtime_selector_combinations_are_mutually_exclusive(
    updates: dict[str, object],
) -> None:
    _assert_rejected(_values(**updates))


def test_valid_as_of_realtime_vintage_and_observation_windows() -> None:
    assert FredSoniaQuery.model_validate(_values(as_of="2026-01-01")).as_of == date(2026, 1, 1)
    assert FredSoniaQuery.model_validate(
        _values(realtime_start="2026-01-01", realtime_end="2026-01-02")
    ).realtime_end == date(2026, 1, 2)
    assert FredSoniaQuery.model_validate(
        _values(start_date="2026-01-01", end_date="2026-01-02", as_of="2026-01-03")
    ).as_of == date(2026, 1, 3)
    assert FredSoniaQuery.model_validate(_values(vintage_dates=["2026-01-01"])).vintage_dates == [
        date(2026, 1, 1)
    ]


def test_vintage_dates_preserve_input_order_and_duplicates() -> None:
    values = ["2026-01-03", "2026-01-01", "2026-01-03"]
    query = FredSoniaQuery.model_validate(_values(vintage_dates=values))

    assert query.vintage_dates == [date(2026, 1, 3), date(2026, 1, 1), date(2026, 1, 3)]


def test_vintage_dates_allow_2000_entries_and_reject_2001() -> None:
    allowed = ["2026-01-01"] * 2000
    query = FredSoniaQuery.model_validate(_values(vintage_dates=allowed))

    assert len(query.vintage_dates or []) == 2000
    _assert_rejected(_values(vintage_dates=allowed + ["2026-01-01"]))


@pytest.mark.parametrize("vintage_dates", [[], "2026-01-01", (), None])
def test_vintage_dates_reject_empty_or_nonlist_inputs(vintage_dates: object) -> None:
    if vintage_dates is None:
        assert FredSoniaQuery.model_validate(_values(vintage_dates=None)).vintage_dates is None
        return
    _assert_rejected(_values(vintage_dates=vintage_dates))


@pytest.mark.parametrize(
    "bad_date",
    [
        datetime(2026, 1, 1),
        _DateSubclass(2026, 1, 1),
        1767225600,
        "2026-1-1",
        "2026-02-30",
    ],
)
def test_vintage_date_items_use_the_same_strict_date_contract(bad_date: object) -> None:
    _assert_rejected(_values(vintage_dates=["2026-01-01", bad_date]))


@pytest.mark.parametrize(
    ("field_name", "valid_values"),
    [
        ("page_size", [1, 100_000]),
        ("offset", [0, 10**12]),
        ("max_records", [1, 1_000_000]),
        ("max_pages", [1, 100]),
    ],
)
def test_pagination_accepts_existing_bounds(field_name: str, valid_values: list[int]) -> None:
    for value in valid_values:
        assert (
            getattr(FredSoniaQuery.model_validate(_values(**{field_name: value})), field_name)
            == value
        )


@pytest.mark.parametrize(
    ("field_name", "invalid_values"),
    [
        ("page_size", [0, 100_001]),
        ("offset", [-1]),
        ("max_records", [0, 1_000_001]),
        ("max_pages", [0, 101]),
    ],
)
def test_pagination_rejects_out_of_range_values(field_name: str, invalid_values: list[int]) -> None:
    for value in invalid_values:
        _assert_rejected(_values(**{field_name: value}))


@pytest.mark.parametrize("field_name", ["page_size", "offset", "max_records", "max_pages"])
@pytest.mark.parametrize("bad_value", [True, 1.0, "1"])
def test_pagination_rejects_bool_float_and_string_values(
    field_name: str, bad_value: object
) -> None:
    _assert_rejected(_values(**{field_name: bad_value}))


@pytest.mark.parametrize("source", ["auto", "fred"])
def test_allowed_sources_are_preserved(source: str) -> None:
    assert FredSoniaQuery.model_validate(_values(source=source)).source == source


@pytest.mark.parametrize("source", ["", "FRED", "other", None, True])
def test_invalid_source_values_are_rejected(source: object) -> None:
    _assert_rejected(_values(source=source))


def test_market_only_allows_gb_or_none_and_symbol_only_allows_none() -> None:
    assert FredSoniaQuery.model_validate(_values(market="gb")).market == "gb"
    assert FredSoniaQuery.model_validate(_values(market=None, symbol=None)).symbol is None
    _assert_rejected(_values(market="us"))
    _assert_rejected(_values(market="GB"))
    _assert_rejected(_values(symbol="IUDSOIA"))


def test_unknown_fields_are_forbidden() -> None:
    _assert_rejected(_values(unexpected="extra"))


def test_model_dump_and_json_round_trip_preserve_selector_and_query_options() -> None:
    query = FredSoniaQuery.model_validate(
        {
            "parameter": "total_nominal_value",
            "source": "fred",
            "market": "gb",
            "transform_units": "pch",
            "output_type": 3,
            "start_date": "0001-01-01",
            "end_date": "9999-12-31",
            "vintage_dates": ["2026-01-03", "2026-01-01", "2026-01-03"],
            "frequency": "wef",
            "aggregation_method": "sum",
            "sort_order": "desc",
            "page_size": 750,
            "offset": 17,
            "max_records": 800,
            "max_pages": 4,
        }
    )

    assert query.series_id == "IUDZLT2"
    assert FredSoniaQuery.model_validate(query.model_dump()) == query
    assert FredSoniaQuery.model_validate_json(query.model_dump_json()) == query
    assert query.vintage_dates == [date(2026, 1, 3), date(2026, 1, 1), date(2026, 1, 3)]


def test_constructed_selector_mismatch_is_rejected_when_revalidated() -> None:
    malformed = FredSoniaQuery.model_construct(parameter="index", series_id="IUDSOIA")

    with pytest.raises(ValidationError):
        FredSoniaQuery.model_validate(malformed)


def test_existing_fred_series_query_contract_remains_available() -> None:
    query = FredSeriesQuery.model_validate(
        {
            "series_id": " GDP ",
            "page_size": 100_000,
            "offset": 0,
            "max_records": 200_000,
            "max_pages": 2,
        }
    )

    assert query.series_id == "GDP"
    assert (query.page_size, query.offset, query.max_records, query.max_pages) == (
        100_000,
        0,
        200_000,
        2,
    )
