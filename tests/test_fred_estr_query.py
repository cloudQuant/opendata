"""Synthetic request and fan-out tests for FRED euro short-term rate queries."""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TypeAlias

import pytest
from pydantic import ValidationError

from opendata.data.providers.fred.models._estr_query import (
    _ESTR_MEASURE_MAP,
    FredEuroShortTermRateQuery,
    FredEuroShortTermRateSeriesQuery,
    expand_estr_queries,
)
from opendata.data.providers.fred.models._sofr_query import FredSofrQuery
from opendata.data.providers.fred.models._sonia_query import FredSoniaQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery

_MEASURE_PAIRS = (
    ("rate", "ECBESTRVOLWGTTRMDMNRT"),
    ("percentile_25", "ECBESTRRT25THPCTVOL"),
    ("percentile_75", "ECBESTRRT75THPCTVOL"),
    ("volume", "ECBESTRTOTVOL"),
    ("transactions", "ECBESTRNUMTRANS"),
    ("number_of_banks", "ECBESTRNUMACTBANKS"),
    ("large_bank_share_of_volume", "ECBESTRSHRVOL5LRGACTBNK"),
)
_TRANSFORM_UNITS = ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log")
_FREQUENCIES = (
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
_AGGREGATION_METHODS = ("avg", "sum", "eop")
EstrQueryClass: TypeAlias = (
    type[FredEuroShortTermRateQuery] | type[FredEuroShortTermRateSeriesQuery]
)
_QUERY_TYPES: tuple[EstrQueryClass, ...] = (
    FredEuroShortTermRateQuery,
    FredEuroShortTermRateSeriesQuery,
)


def _values(query_type: EstrQueryClass, **updates: object) -> dict[str, object]:
    if query_type is FredEuroShortTermRateQuery:
        values: dict[str, object] = {"measures": ["rate"]}
    else:
        values = {"measure": "rate"}
    values.update(updates)
    return values


def _assert_rejected(query_type: EstrQueryClass, values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        query_type.model_validate(values)


def test_top_level_defaults_are_all_seven_in_the_central_map_order() -> None:
    query = FredEuroShortTermRateQuery.model_validate({})

    assert query.measures == tuple(measure for measure, _ in _MEASURE_PAIRS)
    assert query.measures == tuple(_ESTR_MEASURE_MAP)
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
    assert (query.max_total_records, query.max_total_pages) == (200_000, 14)
    assert FredSeriesQuery.model_fields["page_size"].default == query.page_size
    assert FredSeriesQuery.model_fields["max_records"].default == query.max_records
    assert FredSeriesQuery.model_fields["max_pages"].default == query.max_pages


@pytest.mark.parametrize("measure", [measure for measure, _ in _MEASURE_PAIRS])
def test_measure_selection_accepts_exact_ordered_subsets(measure: str) -> None:
    subset = ["transactions", measure]
    if measure == "transactions":
        subset = ["transactions", "rate"]
    query = FredEuroShortTermRateQuery.model_validate({"measures": subset})

    assert query.measures == tuple(subset)
    assert tuple(item.measure for item in expand_estr_queries(query)) == tuple(subset)


@pytest.mark.parametrize(
    "measures",
    [
        [],
        (),
        None,
        "rate",
        {"rate"},
        {"measures": ["rate"]},
        ["rate", "rate"],
        ["rate", "unknown"],
        ["rate", 1],
        [True],
    ],
)
def test_measure_selection_rejects_empty_duplicate_unknown_or_unordered_values(
    measures: object,
) -> None:
    _assert_rejected(FredEuroShortTermRateQuery, {"measures": measures})


def test_json_measure_lists_keep_order_and_round_trip_as_a_tuple() -> None:
    query = FredEuroShortTermRateQuery.model_validate_json(
        json.dumps({"measures": ["volume", "rate", "transactions"]})
    )
    restored = FredEuroShortTermRateQuery.model_validate_json(query.model_dump_json())

    assert query.measures == ("volume", "rate", "transactions")
    assert restored == query
    assert isinstance(restored.measures, tuple)


def test_top_level_rejects_series_id_and_unknown_fields() -> None:
    _assert_rejected(FredEuroShortTermRateQuery, {"series_id": "ECBESTRVOLWGTTRMDMNRT"})
    _assert_rejected(FredEuroShortTermRateQuery, {"unknown": "value"})


def test_both_query_classes_limit_source_market_and_symbol() -> None:
    for query_type in _QUERY_TYPES:
        query = query_type.model_validate(_values(query_type))
        assert (query.source, query.market, query.symbol) == ("auto", None, None)

        fred_source = query_type.model_validate(_values(query_type, source="fred", market="eu"))
        assert (fred_source.source, fred_source.market, fred_source.symbol) == (
            "fred",
            "eu",
            None,
        )
        for field_name, invalid in (
            ("source", "other"),
            ("source", "FRED"),
            ("market", "us"),
            ("market", "EU"),
            ("symbol", "ESTR"),
            ("symbol", ""),
        ):
            _assert_rejected(query_type, _values(query_type, **{field_name: invalid}))


@pytest.mark.parametrize(("measure", "series_id"), _MEASURE_PAIRS)
def test_single_measure_query_binds_omitted_id_and_accepts_explicit_match(
    measure: str, series_id: str
) -> None:
    bound = FredEuroShortTermRateSeriesQuery.model_validate({"measure": measure})
    explicit = FredEuroShortTermRateSeriesQuery.model_validate(
        {"measure": measure, "series_id": series_id}
    )

    assert (bound.measure, bound.series_id) == (measure, series_id)
    assert (explicit.measure, explicit.series_id) == (measure, series_id)
    assert bound.source == "auto"
    assert bound.market is None
    assert bound.symbol is None
    assert (bound.max_records, bound.max_pages) == (200_000, 2)


@pytest.mark.parametrize(
    ("measure", "series_id"),
    [
        ("rate", "ECBESTRRT25THPCTVOL"),
        ("percentile_25", "ECBESTRVOLWGTTRMDMNRT"),
        ("percentile_75", "ECBESTRVOLWGTTRMDMNRT"),
        ("volume", "ECBESTRTOTVOLX"),
        ("transactions", "ECBESTRNUMACTBANKS"),
        ("number_of_banks", "ECBESTRSHRVOL5LRGACTBNK"),
        ("large_bank_share_of_volume", "ECBESTRNUMTRANS"),
        ("rate", "SOFR"),
    ],
)
def test_single_measure_query_rejects_mismatched_or_unknown_series(
    measure: str, series_id: str
) -> None:
    _assert_rejected(
        FredEuroShortTermRateSeriesQuery,
        {"measure": measure, "series_id": series_id},
    )


def test_single_measure_requires_supported_measure_and_id() -> None:
    _assert_rejected(FredEuroShortTermRateSeriesQuery, {})
    _assert_rejected(FredEuroShortTermRateSeriesQuery, {"measure": "unknown"})
    _assert_rejected(FredEuroShortTermRateSeriesQuery, {"measure": "rate", "series_id": "unknown"})


@pytest.mark.parametrize(("measure", "series_id"), _MEASURE_PAIRS)
@pytest.mark.parametrize("transform_units", _TRANSFORM_UNITS)
@pytest.mark.parametrize("frequency", _FREQUENCIES)
@pytest.mark.parametrize("aggregation_method", _AGGREGATION_METHODS)
@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_all_measure_transform_frequency_aggregation_and_output_combinations(
    measure: str,
    series_id: str,
    transform_units: str,
    frequency: str | None,
    aggregation_method: str,
    output_type: int,
) -> None:
    query = FredEuroShortTermRateQuery.model_validate(
        {
            "measures": [measure],
            "transform_units": transform_units,
            "frequency": frequency,
            "aggregation_method": aggregation_method,
            "output_type": output_type,
        }
    )
    expanded = expand_estr_queries(query)

    assert len(expanded) == 1
    assert expanded[0].measure == measure
    assert expanded[0].series_id == series_id
    assert expanded[0].transform_units == transform_units
    assert expanded[0].frequency == frequency
    assert expanded[0].aggregation_method == aggregation_method
    assert expanded[0].output_type == output_type


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("transform_units", _TRANSFORM_UNITS)
def test_all_fred_transforms_are_supported(
    query_type: EstrQueryClass, transform_units: str
) -> None:
    query = query_type.model_validate(_values(query_type, transform_units=transform_units))

    assert query.transform_units == transform_units


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("aggregation_method", _AGGREGATION_METHODS)
def test_all_fred_aggregation_methods_are_supported(
    query_type: EstrQueryClass, aggregation_method: str
) -> None:
    query = query_type.model_validate(_values(query_type, aggregation_method=aggregation_method))

    assert query.aggregation_method == aggregation_method


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("sort_order", ["asc", "desc"])
def test_both_fred_sort_orders_are_supported(query_type: EstrQueryClass, sort_order: str) -> None:
    query = query_type.model_validate(_values(query_type, sort_order=sort_order))

    assert query.sort_order == sort_order


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("output_type", [1, 2, 3, 4])
def test_output_type_accepts_exact_int_1_through_4(
    query_type: EstrQueryClass, output_type: int
) -> None:
    assert (
        query_type.model_validate(_values(query_type, output_type=output_type)).output_type
        == output_type
    )


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("output_type", [True, False, 1.0, "1", "4", 0, 5])
def test_output_type_rejects_bool_float_string_and_out_of_range_values(
    query_type: EstrQueryClass, output_type: object
) -> None:
    _assert_rejected(query_type, _values(query_type, output_type=output_type))


class _DateSubclass(date):
    pass


_DATE_FIELDS = ("start_date", "end_date", "as_of", "realtime_start", "realtime_end")
_BAD_DATES = (
    datetime(2026, 1, 1),
    _DateSubclass(2026, 1, 1),
    1767225600,
    20260101.0,
    "2026-1-1",
    "20260101",
    "2026-02-30",
    "0000-01-01",
    "10000-01-01",
    True,
)


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("field_name", _DATE_FIELDS)
@pytest.mark.parametrize("bad_date", _BAD_DATES)
def test_query_dates_reject_datetime_subclasses_epochs_and_noncanonical_strings(
    query_type: EstrQueryClass, field_name: str, bad_date: object
) -> None:
    _assert_rejected(query_type, _values(query_type, **{field_name: bad_date}))


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_query_dates_accept_exact_dates_and_canonical_strings(
    query_type: EstrQueryClass,
) -> None:
    query = query_type.model_validate(
        _values(
            query_type,
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


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("field_name", _DATE_FIELDS)
def test_query_dates_accept_none(query_type: EstrQueryClass, field_name: str) -> None:
    assert (
        getattr(query_type.model_validate(_values(query_type, **{field_name: None})), field_name)
        is None
    )


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize(
    "updates",
    [
        {"start_date": "2026-02-01", "end_date": "2026-01-01"},
        {"realtime_start": "2026-02-01", "realtime_end": "2026-01-01"},
        {"as_of": "2026-01-01", "realtime_start": "2026-01-01"},
        {"as_of": "2026-01-01", "realtime_end": "2026-01-01"},
        {
            "as_of": "2026-01-01",
            "realtime_start": "2026-01-01",
            "realtime_end": "2026-01-02",
        },
        {"vintage_dates": ["2026-01-01"], "as_of": "2026-01-01"},
        {"vintage_dates": ["2026-01-01"], "realtime_start": "2026-01-01"},
        {"vintage_dates": ["2026-01-01"], "realtime_end": "2026-01-01"},
    ],
)
def test_observation_and_realtime_selectors_are_ordered_and_exclusive(
    query_type: EstrQueryClass, updates: dict[str, object]
) -> None:
    _assert_rejected(query_type, _values(query_type, **updates))


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_vintage_dates_preserve_order_duplicates_and_observation_window(
    query_type: EstrQueryClass,
) -> None:
    dates = ["2026-01-03", "2026-01-01", "2026-01-03"]
    query = query_type.model_validate(
        _values(
            query_type,
            start_date="2026-01-01",
            end_date="2026-01-04",
            vintage_dates=dates,
        )
    )

    assert query.vintage_dates == [date(2026, 1, 3), date(2026, 1, 1), date(2026, 1, 3)]


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_vintage_dates_allow_2000_and_reject_2001(
    query_type: EstrQueryClass,
) -> None:
    values = ["2026-01-01"] * 2000
    query = query_type.model_validate(_values(query_type, vintage_dates=values))

    assert len(query.vintage_dates or []) == 2000
    _assert_rejected(query_type, _values(query_type, vintage_dates=values + ["2026-01-01"]))


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("vintage_dates", [[], (), "2026-01-01"])
def test_vintage_dates_reject_empty_and_unordered_values(
    query_type: EstrQueryClass, vintage_dates: object
) -> None:
    _assert_rejected(query_type, _values(query_type, vintage_dates=vintage_dates))


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_vintage_date_items_use_the_same_strict_date_contract(
    query_type: EstrQueryClass,
) -> None:
    with pytest.raises(ValidationError):
        query_type.model_validate(
            _values(query_type, vintage_dates=["2026-01-01", datetime(2026, 1, 2)])
        )


@pytest.mark.parametrize(
    ("field_name", "valid_values"),
    [
        ("page_size", [1, 100_000]),
        ("offset", [0, 1]),
        ("max_records", [1, 1_000_000]),
        ("max_pages", [1, 100]),
    ],
)
@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_per_measure_pagination_and_record_caps_accept_boundaries(
    query_type: EstrQueryClass, field_name: str, valid_values: list[int]
) -> None:
    for value in valid_values:
        query = query_type.model_validate(_values(query_type, **{field_name: value}))
        assert getattr(query, field_name) == value


@pytest.mark.parametrize(
    ("field_name", "invalid_values"),
    [
        ("page_size", [0, 100_001]),
        ("offset", [-1]),
        ("max_records", [0, 1_000_001]),
        ("max_pages", [0, 101]),
    ],
)
@pytest.mark.parametrize("query_type", _QUERY_TYPES)
def test_per_measure_pagination_and_record_caps_reject_out_of_range_values(
    query_type: EstrQueryClass, field_name: str, invalid_values: list[int]
) -> None:
    for value in invalid_values:
        _assert_rejected(query_type, _values(query_type, **{field_name: value}))


@pytest.mark.parametrize("query_type", _QUERY_TYPES)
@pytest.mark.parametrize("field_name", ["page_size", "offset", "max_records", "max_pages"])
@pytest.mark.parametrize("bad_value", [True, False, 1.0, "1"])
def test_per_measure_pagination_and_record_caps_require_exact_integers(
    query_type: EstrQueryClass, field_name: str, bad_value: object
) -> None:
    _assert_rejected(query_type, _values(query_type, **{field_name: bad_value}))


@pytest.mark.parametrize(
    ("field_name", "valid_values"),
    [
        ("max_total_records", [1, 1_000_000]),
        ("max_total_pages", [1, 64]),
    ],
)
def test_model_total_caps_accept_boundaries(field_name: str, valid_values: list[int]) -> None:
    for value in valid_values:
        query = FredEuroShortTermRateQuery.model_validate({field_name: value})

        assert getattr(query, field_name) == value


@pytest.mark.parametrize(
    ("field_name", "invalid_values"),
    [
        ("max_total_records", [0, 1_000_001]),
        ("max_total_pages", [0, 65]),
    ],
)
def test_model_total_caps_reject_out_of_range_values(
    field_name: str, invalid_values: list[int]
) -> None:
    for value in invalid_values:
        _assert_rejected(FredEuroShortTermRateQuery, {field_name: value})


@pytest.mark.parametrize("field_name", ["max_total_records", "max_total_pages"])
@pytest.mark.parametrize("bad_value", [True, False, 1.0, "1"])
def test_model_total_caps_require_exact_integers(field_name: str, bad_value: object) -> None:
    _assert_rejected(FredEuroShortTermRateQuery, {field_name: bad_value})


def test_total_caps_are_not_part_of_the_single_measure_query() -> None:
    _assert_rejected(
        FredEuroShortTermRateSeriesQuery,
        {"measure": "rate", "max_total_records": 1},
    )


def test_schema_enumerates_only_supported_measures_and_series_ids() -> None:
    top_schema = FredEuroShortTermRateQuery.model_json_schema()
    series_schema = FredEuroShortTermRateSeriesQuery.model_json_schema()

    assert top_schema["properties"]["measures"]["items"]["enum"] == [
        measure for measure, _ in _MEASURE_PAIRS
    ]
    assert series_schema["properties"]["measure"]["enum"] == [
        measure for measure, _ in _MEASURE_PAIRS
    ]
    assert series_schema["properties"]["series_id"]["enum"] == [
        series_id for _, series_id in _MEASURE_PAIRS
    ]


def test_top_and_single_measure_queries_round_trip_through_json() -> None:
    top = FredEuroShortTermRateQuery.model_validate(
        {
            "measures": ["percentile_75", "volume"],
            "source": "fred",
            "market": "eu",
            "transform_units": "pch",
            "frequency": "q",
            "aggregation_method": "sum",
            "output_type": 4,
            "start_date": "2026-01-01",
            "vintage_dates": ["2026-01-03", "2026-01-01"],
            "page_size": 17,
            "offset": 3,
            "max_records": 101,
            "max_pages": 4,
            "max_total_records": 900,
            "max_total_pages": 9,
        }
    )
    restored_top = FredEuroShortTermRateQuery.model_validate_json(top.model_dump_json())
    single = expand_estr_queries(top)[0]
    restored_single = FredEuroShortTermRateSeriesQuery.model_validate_json(single.model_dump_json())

    assert restored_top == top
    assert restored_single == single


def test_expansion_preserves_shared_context_and_removes_only_top_level_caps() -> None:
    query = FredEuroShortTermRateQuery.model_validate(
        {
            "source": "fred",
            "market": "eu",
            "measures": ["large_bank_share_of_volume", "transactions"],
            "transform_units": "ch1",
            "output_type": 2,
            "start_date": "2026-01-01",
            "end_date": "2026-02-01",
            "realtime_start": "2026-02-02",
            "realtime_end": "2026-02-04",
            "frequency": "bw",
            "aggregation_method": "eop",
            "sort_order": "desc",
            "page_size": 99,
            "offset": 7,
            "max_records": 1_001,
            "max_pages": 6,
            "max_total_records": 2_002,
            "max_total_pages": 10,
        }
    )
    expanded = expand_estr_queries(query)
    shared = query.model_dump(
        mode="python",
        exclude={"measures", "max_total_records", "max_total_pages"},
    )

    assert tuple(item.measure for item in expanded) == query.measures
    assert tuple(item.series_id for item in expanded) == (
        "ECBESTRSHRVOL5LRGACTBNK",
        "ECBESTRNUMTRANS",
    )
    for item in expanded:
        assert item.model_dump(mode="python", exclude={"measure", "series_id"}) == shared
        assert item.symbol is None


def test_expansion_revalidates_model_copy_and_construct_mutants() -> None:
    valid = FredEuroShortTermRateQuery.model_validate({"measures": ["rate", "volume"]})
    copied = valid.model_copy(update={"measures": ("rate", "rate")})
    constructed_values = valid.model_dump(mode="python")
    constructed_values["measures"] = ("unsupported",)
    constructed = FredEuroShortTermRateQuery.model_construct(**constructed_values)

    with pytest.raises(ValidationError):
        expand_estr_queries(copied)
    with pytest.raises(ValidationError):
        expand_estr_queries(constructed)


def test_expansion_requires_the_exact_top_level_query_class() -> None:
    class DerivedFredEuroShortTermRateQuery(FredEuroShortTermRateQuery):
        pass

    derived = DerivedFredEuroShortTermRateQuery.model_validate({})

    with pytest.raises(TypeError, match="exact FredEuroShortTermRateQuery"):
        expand_estr_queries(derived)
    with pytest.raises(TypeError, match="exact FredEuroShortTermRateQuery"):
        expand_estr_queries({"measures": ["rate"]})


def test_single_measure_query_revalidates_mutated_model_copy() -> None:
    valid = FredEuroShortTermRateSeriesQuery.model_validate({"measure": "rate"})
    copied = valid.model_copy(update={"series_id": "SOFR"})

    with pytest.raises(ValidationError):
        FredEuroShortTermRateSeriesQuery.model_validate(copied)


def test_existing_generic_sofr_sonia_and_series_queries_remain_usable() -> None:
    generic = FredSeriesQuery.model_validate({"series_id": "GDP"})
    sofr = FredSofrQuery.model_validate({"series_id": "SOFR"})
    sonia = FredSoniaQuery.model_validate({"parameter": "rate"})

    assert generic.series_id == "GDP"
    assert (sofr.series_id, sofr.market) == ("SOFR", None)
    assert (sonia.parameter, sonia.series_id, sonia.market) == ("rate", "IUDSOIA", None)
