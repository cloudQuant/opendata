"""GenericData examples are synthetic XSD-shaped fixtures, not ECB responses."""

from __future__ import annotations

import json
import math
from datetime import date
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from opendata.data.models.ecb_series import (
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
)
from opendata.data.providers.ecb.models import _reference_rates
from opendata.data.providers.ecb.models._reference_rates import ReferenceRatesParseError
from opendata.data.providers.ecb.models._series_sdmx import (
    normalize_balance_of_payments_records,
    normalize_yield_curve_records,
    parse_balance_of_payments_generic_data,
    parse_yield_curve_generic_data,
)

MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

YC_DIMENSIONS = (
    "FREQ",
    "REF_AREA",
    "CURRENCY",
    "PROVIDER_FM",
    "INSTRUMENT_FM",
    "PROVIDER_FM_ID",
    "DATA_TYPE_FM",
)
YC_VALUES = {
    "FREQ": "B",
    "REF_AREA": "U2",
    "CURRENCY": "EUR",
    "PROVIDER_FM": "4F",
    "INSTRUMENT_FM": "G_N_A",
    "PROVIDER_FM_ID": "SV_C_YM",
    "DATA_TYPE_FM": "SR_3M",
}
BPS_DIMENSIONS = (
    "FREQ",
    "ADJUSTMENT",
    "REF_AREA",
    "COUNTERPART_AREA",
    "REF_SECTOR",
    "COUNTERPART_SECTOR",
    "FLOW_STOCK_ENTRY",
    "ACCOUNTING_ENTRY",
    "INT_ACC_ITEM",
    "FUNCTIONAL_CAT",
    "INSTR_ASSET",
    "MATURITY",
    "UNIT_MEASURE",
    "CURRENCY_DENOM",
    "VALUATION",
    "COMP_METHOD",
    "TYPE_ENTITY",
)
BPS_VALUES = {
    "FREQ": "M",
    "ADJUSTMENT": "N",
    "REF_AREA": "I10",
    "COUNTERPART_AREA": "W1",
    "REF_SECTOR": "S121",
    "COUNTERPART_SECTOR": "S1",
    "FLOW_STOCK_ENTRY": "T",
    "ACCOUNTING_ENTRY": "A",
    "INT_ACC_ITEM": "FA",
    "FUNCTIONAL_CAT": "R",
    "INSTR_ASSET": "F",
    "MATURITY": "_Z",
    "UNIT_MEASURE": "EUR",
    "CURRENCY_DENOM": "X1",
    "VALUATION": "_X",
    "COMP_METHOD": "N",
    "TYPE_ENTITY": "ALL",
}


def _values(values: dict[str, str], order: tuple[str, ...] | None = None) -> str:
    ids = order or tuple(values)
    return "".join(
        f'<generic:Value id="{dimension}" value="{values[dimension]}"/>' for dimension in ids
    )


def _obs(
    period: str,
    value: str | None = "1.25",
    *,
    attributes: str = "",
    dimension_id: str | None = "TIME_PERIOD",
) -> str:
    dimension_id_attr = "" if dimension_id is None else f' id="{dimension_id}"'
    dimension = f'<generic:ObsDimension value="{period}"{dimension_id_attr}/>'
    value_element = "" if value is None else f'<generic:ObsValue value="{value}"/>'
    return f"<generic:Obs>{dimension}{value_element}{attributes}</generic:Obs>"


def _series(
    values: dict[str, str],
    observations: list[str],
    *,
    order: tuple[str, ...] | None = None,
    attributes: str = "",
    key_content: str | None = None,
) -> str:
    key = _values(values, order) if key_content is None else key_content
    return (
        "<generic:Series>"
        f"<generic:SeriesKey>{key}</generic:SeriesKey>"
        f"{attributes}{''.join(observations)}</generic:Series>"
    )


def _document(datasets: list[str]) -> bytes:
    schema_location = f"{MESSAGE_NS} SDMXMessage.xsd {GENERIC_NS} SDMXDataGeneric.xsd"
    dataset_xml = "".join(
        f'<message:DataSet structureRef="PRODUCT">{dataset}</message:DataSet>'
        for dataset in datasets
    )
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData xmlns:message="{MESSAGE_NS}" xmlns:common="{COMMON_NS}"
 xmlns:generic="{GENERIC_NS}" xmlns:xsi="{XSI_NS}"
 xsi:schemaLocation="{schema_location}">
 <message:Header>
  <message:Structure structureID="PRODUCT" dimensionAtObservation="TIME_PERIOD">
   <common:Structure><Ref agencyID="ECB" id="SYNTHETIC" version="1.0"
    class="DataStructure" package="datastructure"/></common:Structure>
  </message:Structure>
 </message:Header>
 {dataset_xml}
</message:GenericData>'''.encode()


def _group(value: str = "opaque") -> str:
    return (
        '<generic:Group type="SYNTHETIC_GROUP"><generic:GroupKey>'
        '<generic:Value id="FREQ" value="B"/></generic:GroupKey>'
        "<generic:Attributes>"
        f'<generic:Value id="UNKNOWN_GROUP_ATTRIBUTE" value="{value}"/>'
        "</generic:Attributes></generic:Group>"
    )


def _key(prefix: str, values: dict[str, str], dimensions: tuple[str, ...]) -> str:
    return prefix + "." + ".".join(values[dimension] for dimension in dimensions)


def test_yc_reorders_xml_dimensions_and_normalizes_daily_source_faithfully() -> None:
    order = tuple(reversed(YC_DIMENSIONS))
    dataset_attributes = (
        '<generic:Attributes><generic:Value id="UNKNOWN_DATASET" value="raw-Ω"/>'
        "</generic:Attributes>"
    )
    series_attributes = (
        '<generic:Attributes><generic:Value id="UNKNOWN_SERIES" value="source spelling"/>'
        "</generic:Attributes>"
    )
    observation_attributes = (
        '<generic:Attributes><generic:Value id="OBS_STATUS" value="raw status"/>'
        "</generic:Attributes>"
    )
    dataset = (
        dataset_attributes
        + _group()
        + _series(
            YC_VALUES,
            [
                _obs("2026-10-07", "-0.000", attributes=observation_attributes),
                _obs("2026-10-08", "1e-10000"),
            ],
            order=order,
            attributes=series_attributes,
        )
    )

    raw = parse_yield_curve_generic_data(_document([dataset]))
    assert len(raw) == 2
    assert tuple(raw[0]["dimensions"]) == YC_DIMENSIONS
    assert raw[0]["series_key"] == _key("YC", YC_VALUES, YC_DIMENSIONS)
    assert raw[0]["period"] == "2026-10-07"
    assert raw[0]["value"] == "-0.000"
    assert raw[0]["dataset_attributes"] == {"UNKNOWN_DATASET": "raw-Ω"}
    assert raw[0]["series_attributes"] == {"UNKNOWN_SERIES": "source spelling"}
    assert raw[0]["observation_attributes"] == {"OBS_STATUS": "raw status"}
    assert raw[0]["group_context"] == [
        {
            "group_type": "SYNTHETIC_GROUP",
            "key": {"FREQ": "B"},
            "attributes": {"UNKNOWN_GROUP_ATTRIBUTE": "opaque"},
        }
    ]

    typed = normalize_yield_curve_records(raw)
    assert all(isinstance(row, EcbYieldCurveObservation) for row in typed)
    assert typed[0].date == date(2026, 10, 7)
    assert typed[0].source_value == "-0.000"
    assert typed[0].value == 0.0 and math.copysign(1.0, typed[0].value) == -1.0
    assert typed[1].source_value == "1e-10000"
    assert typed[1].value == 0.0 and math.copysign(1.0, typed[1].value) == 1.0
    assert typed[0].group_context[0].attributes["UNKNOWN_GROUP_ATTRIBUTE"] == "opaque"


def test_raw_rows_get_independent_nested_context_and_missing_values_stay_null() -> None:
    raw = parse_yield_curve_generic_data(
        _document([_group() + _series(YC_VALUES, [_obs("2026-10-07", None), _obs("2026-10-08")])])
    )
    assert raw[0]["value"] is None
    assert raw[0]["group_context"] is not raw[1]["group_context"]
    assert raw[0]["group_context"][0] is not raw[1]["group_context"][0]
    assert raw[0]["dimensions"] is not raw[1]["dimensions"]
    typed = normalize_yield_curve_records(raw)
    assert typed[0].value is None and typed[0].source_value is None
    raw[0]["group_context"][0]["attributes"]["UNKNOWN_GROUP_ATTRIBUTE"] = "mutated"
    raw[0]["dimensions"]["REF_AREA"] = "ZZ"
    assert raw[1]["group_context"][0]["attributes"]["UNKNOWN_GROUP_ATTRIBUTE"] == "opaque"
    assert raw[1]["dimensions"]["REF_AREA"] == "U2"


def test_bps_month_and_quarter_periods_remain_strings_with_all_dimensions() -> None:
    quarterly = {**BPS_VALUES, "FREQ": "Q"}
    monthly_series = _series(BPS_VALUES, [_obs("2014-09", "-12.50")])
    quarterly_series = _series(quarterly, [_obs("2014-Q3", "4.2e1")])
    raw = parse_balance_of_payments_generic_data(_document([monthly_series + quarterly_series]))
    assert [row["period"] for row in raw] == ["2014-09", "2014-Q3"]
    assert raw[0]["series_key"] == _key("BPS", BPS_VALUES, BPS_DIMENSIONS)
    assert raw[1]["series_key"] == _key("BPS", quarterly, BPS_DIMENSIONS)
    assert tuple(raw[0]["dimensions"]) == BPS_DIMENSIONS

    typed = normalize_balance_of_payments_records(raw)
    assert all(isinstance(row, EcbBalanceOfPaymentsObservation) for row in typed)
    assert [(row.frequency, row.period) for row in typed] == [
        ("M", "2014-09"),
        ("Q", "2014-Q3"),
    ]
    assert [row.value for row in typed] == [-12.5, 42.0]
    assert [row.source_value for row in typed] == ["-12.50", "4.2e1"]


@pytest.mark.parametrize(
    "parser",
    [parse_yield_curve_generic_data, parse_balance_of_payments_generic_data],
)
def test_valid_empty_dataset_returns_empty_tuple(
    parser: Callable[..., tuple[object, ...]],
) -> None:
    assert parser(_document([""])) == ()


@pytest.mark.parametrize(
    ("product", "values", "observations", "error"),
    [
        ("YC", {**YC_VALUES, "FREQ": "D"}, ["2026-10-07"], "frequency"),
        ("YC", YC_VALUES, ["2026-1-07"], "canonical daily"),
        ("BPS", {**BPS_VALUES, "FREQ": "A"}, ["2014-09"], "frequency"),
        ("BPS", BPS_VALUES, ["2014-Q3"], "monthly"),
        ("BPS", {**BPS_VALUES, "FREQ": "Q"}, ["2014-03"], "quarterly"),
        ("BPS", BPS_VALUES, ["0000-09"], "monthly"),
        ("BPS", {**BPS_VALUES, "FREQ": "Q"}, ["0000-Q1"], "quarterly"),
    ],
)
def test_bad_frequency_or_period_is_rejected(
    product: str,
    values: dict[str, str],
    observations: list[str],
    error: str,
) -> None:
    series = _series(values, [_obs(period) for period in observations])
    parser = (
        parse_yield_curve_generic_data
        if product == "YC"
        else parse_balance_of_payments_generic_data
    )
    with pytest.raises(ReferenceRatesParseError, match=error):
        parser(_document([series]))


@pytest.mark.parametrize("product", ["YC", "BPS"])
def test_serieskey_requires_exact_unique_dimensions_in_any_xml_order(product: str) -> None:
    if product == "YC":
        dimensions, values, parser = (
            YC_DIMENSIONS,
            YC_VALUES,
            parse_yield_curve_generic_data,
        )
    else:
        dimensions, values, parser = (
            BPS_DIMENSIONS,
            BPS_VALUES,
            parse_balance_of_payments_generic_data,
        )
    valid_period = "2026-10-07" if product == "YC" else "2014-09"
    permuted = tuple(reversed(dimensions))
    good = parser(_document([_series(values, [_obs(valid_period)], order=permuted)]))
    assert good[0]["series_key"] == _key(product, values, dimensions)

    missing = dict(values)
    missing.pop(dimensions[-1])
    extra = {**values, "EXTRA_DIMENSION": "X"}
    duplicate = (
        _values(values) + f'<generic:Value id="{dimensions[0]}" value="{values[dimensions[0]]}"/>'
    )
    missing_xml = _values(
        missing, tuple(dimension for dimension in dimensions if dimension in missing)
    )
    cases = (missing_xml, _values(extra), duplicate)
    for invalid_key in cases:
        with pytest.raises(ReferenceRatesParseError, match="SeriesKey"):
            parser(
                _document(
                    [
                        _series(
                            values,
                            [_obs(valid_period)],
                            key_content=invalid_key,
                        )
                    ]
                )
            )


@pytest.mark.parametrize(
    ("product", "values"),
    [
        ("YC", {**YC_VALUES, "REF_AREA": "U2/INVALID"}),
        ("BPS", {**BPS_VALUES, "COUNTERPART_AREA": "lowercase"}),
    ],
)
def test_dimension_values_must_be_single_safe_source_tokens(
    product: str, values: dict[str, str]
) -> None:
    parser = (
        parse_yield_curve_generic_data
        if product == "YC"
        else parse_balance_of_payments_generic_data
    )
    period = "2026-10-07" if product == "YC" else "2014-09"
    with pytest.raises(ReferenceRatesParseError, match="source tokens"):
        parser(_document([_series(values, [_obs(period)])]))


def test_normalizers_reject_incomplete_or_inconsistent_raw_rows() -> None:
    parsed = parse_yield_curve_generic_data(_document([_series(YC_VALUES, [_obs("2026-10-07")])]))
    extra_field = {**parsed[0], "unexpected": "field"}
    with pytest.raises(ReferenceRatesParseError, match="complete raw field set"):
        normalize_yield_curve_records([extra_field])  # type: ignore[list-item]
    inconsistent_key = {
        **parsed[0],
        "dimensions": {**parsed[0]["dimensions"], "REF_AREA": "ZZ"},
    }
    with pytest.raises(ReferenceRatesParseError, match="series_key does not match"):
        normalize_yield_curve_records([inconsistent_key])  # type: ignore[list-item]


def test_identical_duplicates_preserve_order_conflicts_and_bad_tail_fail_atomically() -> None:
    yc_first = _series(YC_VALUES, [_obs("2026-10-07", "1.0")])
    identical = parse_yield_curve_generic_data(_document([yc_first + yc_first]))
    assert len(identical) == 2 and identical[0] == identical[1]
    with pytest.raises(ReferenceRatesParseError, match="conflicting duplicate"):
        parse_yield_curve_generic_data(
            _document([yc_first + _series(YC_VALUES, [_obs("2026-10-07", "2.0")])])
        )
    with pytest.raises(ReferenceRatesParseError, match="canonical daily"):
        parse_yield_curve_generic_data(
            _document(
                [
                    _series(
                        YC_VALUES,
                        [_obs("2026-10-07", "1.0"), _obs("2026-10", "2.0")],
                    )
                ]
            )
        )
    with pytest.raises(ReferenceRatesParseError, match="numeric syntax"):
        parse_balance_of_payments_generic_data(
            _document([_series(BPS_VALUES, [_obs("2014-09", "1"), _obs("2014-10", "bad")])])
        )


@pytest.mark.parametrize("bad_value", ["", "NaN", "INF", "Infinity", "1e999"])
def test_present_obsvalue_must_be_finite_numeric_text(bad_value: str) -> None:
    with pytest.raises(ReferenceRatesParseError, match="finite|numeric syntax"):
        parse_yield_curve_generic_data(
            _document([_series(YC_VALUES, [_obs("2026-10-07", bad_value)])])
        )


def test_both_products_share_header_namespace_and_unsupported_shape_guards() -> None:
    dataset = _series(YC_VALUES, [_obs("2026-10-07")])
    body = _document([dataset])
    with pytest.raises(ReferenceRatesParseError, match="root must be"):
        parse_yield_curve_generic_data(body.replace(MESSAGE_NS.encode(), b"urn:wrong-message"))
    with pytest.raises(ReferenceRatesParseError, match="unsupported element"):
        parse_yield_curve_generic_data(body.replace(GENERIC_NS.encode(), b"urn:wrong-generic"))
    with pytest.raises(ReferenceRatesParseError, match="flat Obs"):
        parse_yield_curve_generic_data(_document(["<generic:Obs><generic:ObsKey/></generic:Obs>"]))
    with pytest.raises(ReferenceRatesParseError, match="DataProvider"):
        parse_balance_of_payments_generic_data(
            _document(["<generic:DataProvider/><generic:Series/>"])
        )
    with pytest.raises(ReferenceRatesParseError, match="DataSet-level context"):
        parse_yield_curve_generic_data(
            _document(
                ['<generic:Attributes><generic:Value id="UNIT" value="x"/></generic:Attributes>']
            )
        )


@pytest.mark.parametrize(
    "parser", [parse_yield_curve_generic_data, parse_balance_of_payments_generic_data]
)
def test_all_resource_limits_are_strict_and_never_return_partial_rows(parser: object) -> None:
    body = _document([_series(YC_VALUES, [_obs("2026-10-07")])])
    for name in ("max_body_bytes", "max_xml_nodes", "max_observations", "max_output_bytes"):
        for bad in (True, False, 0, -1, 1.0, "1"):
            with pytest.raises(ReferenceRatesParseError, match="positive integer"):
                parser(body, **{name: bad})  # type: ignore[operator]
    with pytest.raises(ReferenceRatesParseError, match="fixed 8388608-byte cap"):
        parser(body, max_output_bytes=8 * 1024 * 1024 + 1)  # type: ignore[operator]
    for name, maximum in (
        ("max_body_bytes", 4 * 1024 * 1024),
        ("max_xml_nodes", 100_000),
        ("max_observations", 10_000),
    ):
        with pytest.raises(ReferenceRatesParseError, match="cannot exceed the fixed maximum"):
            parser(body, **{name: maximum + 1})  # type: ignore[operator]
    with pytest.raises(ReferenceRatesParseError, match="body exceeds"):
        parser(body, max_body_bytes=len(body) - 1)  # type: ignore[operator]
    with pytest.raises(ReferenceRatesParseError, match="node count exceeds"):
        parser(body, max_xml_nodes=2)  # type: ignore[operator]
    with pytest.raises(ReferenceRatesParseError, match="observation count exceeds"):
        parser(
            _document([_series(YC_VALUES, [_obs("2026-10-07"), _obs("2026-10-08")])]),
            max_observations=1,
        )  # type: ignore[operator]


def test_exact_json_output_budget_counts_multiple_datasets_and_amplified_groups() -> None:
    body = _document(
        [
            _series(YC_VALUES, [_obs("2026-10-07")]),
            _series(YC_VALUES, [_obs("2026-10-08")]),
        ]
    )
    expected = parse_yield_curve_generic_data(body)
    encoded = json.dumps(
        list(expected), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    assert parse_yield_curve_generic_data(body, max_output_bytes=len(encoded)) == expected
    with pytest.raises(ReferenceRatesParseError, match="raw JSON output exceeds"):
        parse_yield_curve_generic_data(body, max_output_bytes=len(encoded) - 1)

    amplified_group = (
        '<generic:Group type="SYNTHETIC_GROUP"><generic:Attributes>'
        f'<generic:Value id="COMMENT" value="{"x" * 15000}"/>'
        "</generic:Attributes></generic:Group>"
    )
    many_observations = [_obs(f"{year:04d}-01-01", "1") for year in range(2000, 2600)]
    amplified = _document([amplified_group + _series(YC_VALUES, many_observations)])
    assert len(amplified) < 4 * 1024 * 1024
    with pytest.raises(ReferenceRatesParseError, match="raw JSON output exceeds"):
        parse_yield_curve_generic_data(amplified)


@pytest.mark.parametrize(
    ("xml_encoding", "codec", "bom"),
    [
        ("UTF-8", "utf-8", b""),
        ("UTF-16", "utf-16", b""),
        ("UTF-16LE", "utf-16-le", b"\xff\xfe"),
        ("UTF-16BE", "utf-16-be", b"\xfe\xff"),
    ],
)
@pytest.mark.parametrize(
    "declaration",
    [
        "<!DOCTYPE x><x/>",
        '<!DOCTYPE x [<!ENTITY x "entity-secret">]><x>&x;</x>',
        '<!DOCTYPE x [<!ENTITY x SYSTEM "https://example.invalid/never-fetch">]><x>&x;</x>',
    ],
)
def test_defused_parser_rejects_attacks_when_text_prefilter_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
    xml_encoding: str,
    codec: str,
    bom: bytes,
    declaration: str,
) -> None:
    body = bom + (f'<?xml version="1.0" encoding="{xml_encoding}"?>{declaration}'.encode(codec))
    monkeypatch.setattr(_reference_rates, "_reject_dtd_or_entity", lambda _: None)
    with pytest.raises(ReferenceRatesParseError, match="forbidden DTD or entity") as exc_info:
        parse_yield_curve_generic_data(body)
    assert exc_info.value.__suppress_context__ is True
    assert "example.invalid" not in str(exc_info.value)
    assert "entity-secret" not in str(exc_info.value)
