"""Parser tests use synthetic payloads shaped by the published SDMX 2.1 XSD.

These fixtures are not captured from the ECB and make no claim about a live
response's completeness or licensing.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date, timedelta

import pytest

from opendata.data.providers.ecb.models import _reference_rates
from opendata.data.providers.ecb.models._reference_rates import (
    ReferenceRatesParseError,
    normalize_reference_rates,
    parse_generic_data,
)

MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
FOOTER_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message/footer"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def _obs(
    value: str | None = "1.2345",
    *,
    date: str = "2026-10-08",
    dimension_id: str | None = "TIME_PERIOD",
    value_id: str | None = "OBS_VALUE",
    attributes: str = "",
) -> str:
    dimension_id_attr = "" if dimension_id is None else f' id="{dimension_id}"'
    dimension = f'<generic:ObsDimension value="{date}"{dimension_id_attr}/>'
    if value is None:
        value_element = ""
    else:
        value_id_attr = "" if value_id is None else f' id="{value_id}"'
        value_element = f'<generic:ObsValue value="{value}"{value_id_attr}/>'
    return f"<generic:Obs>{dimension}{value_element}{attributes}</generic:Obs>"


def _series(
    observations: list[str] | None = None,
    *,
    key: str | None = None,
    attributes: str = "",
) -> str:
    if key is None:
        key = (
            '<generic:Value id="FREQ" value="D"/>'
            '<generic:Value id="CURRENCY" value="ZXQ"/>'
            '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
            '<generic:Value id="EXR_TYPE" value="SP00"/>'
            '<generic:Value id="EXR_SUFFIX" value="A"/>'
        )
    if observations is None:
        observations = [_obs()]
    return (
        "<generic:Series>"
        f"<generic:SeriesKey>{key}</generic:SeriesKey>"
        f"{attributes}{''.join(observations)}"
        "</generic:Series>"
    )


def _document(
    dataset: str = "",
    *,
    structure_ref: str = "ECB_REF",
    dimension_at_observation: str = "TIME_PERIOD",
    message_namespace: str = MESSAGE_NS,
    generic_namespace: str = GENERIC_NS,
) -> bytes:
    schema_location = f"{MESSAGE_NS} SDMXMessage.xsd {GENERIC_NS} SDMXDataGeneric.xsd"
    xml = f'''<?xml version="1.0" encoding="UTF-8"?>
<message:GenericData
  xmlns:message="{message_namespace}"
  xmlns:common="{COMMON_NS}"
  xmlns:generic="{generic_namespace}"
  xmlns:xsi="{XSI_NS}"
  xsi:schemaLocation="{schema_location}">
  <message:Header>
    <message:ID>synthetic-fixture</message:ID>
    <message:Test>false</message:Test>
    <message:Prepared>2026-10-08T00:00:00Z</message:Prepared>
    <message:Sender id="SYNTHETIC"/>
    <message:Structure structureID="ECB_REF" dimensionAtObservation="{dimension_at_observation}">
      <common:Structure>
        <Ref agencyID="ECB" id="EXR" version="1.0"
             class="DataStructure" package="datastructure"/>
      </common:Structure>
    </message:Structure>
  </message:Header>
  <message:DataSet structureRef="{structure_ref}">{dataset}</message:DataSet>
</message:GenericData>'''
    return xml.encode("utf-8")


def test_extracts_raw_source_order_and_preserves_each_attribute_layer_and_group() -> None:
    dataset_attributes = (
        '<generic:Attributes><generic:Value id="UNIT" value="currency per EUR"/>'
        '<generic:Value id="UNKNOWN_DATASET" value="raw dataset value"/></generic:Attributes>'
    )
    series_attributes = (
        '<generic:Attributes><generic:Value id="COLLECTION" value="A"/></generic:Attributes>'
    )
    group = (
        '<generic:Group type="EXR_GROUP"><generic:GroupKey>'
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        "</generic:GroupKey><generic:Attributes>"
        '<generic:Value id="COMMENT" value="raw group text"/>'
        "</generic:Attributes></generic:Group>"
    )
    observation_attributes = (
        '<generic:Attributes><generic:Value id="OBS_STATUS" value="A"/></generic:Attributes>'
    )
    dataset = f"{dataset_attributes}{group}" + _series(
        [
            _obs("1.234500", date="2026-10-08", attributes=observation_attributes),
            _obs("2.5e0", date="2026-10-09"),
        ],
        attributes=series_attributes,
    )

    raw = parse_generic_data(_document(dataset))

    assert [row["date"] for row in raw] == ["2026-10-08", "2026-10-09"]
    assert [row["value"] for row in raw] == ["1.234500", "2.5e0"]
    assert raw[0]["series_key"] == "D.ZXQ.EUR.SP00.A"
    assert raw[0]["dataset_attributes"] == {
        "UNIT": "currency per EUR",
        "UNKNOWN_DATASET": "raw dataset value",
    }
    assert raw[0]["series_attributes"] == {"COLLECTION": "A"}
    assert raw[0]["observation_attributes"] == {"OBS_STATUS": "A"}
    assert raw[0]["group_context"] == [
        {
            "group_type": "EXR_GROUP",
            "key": {"FREQ": "D", "CURRENCY": "ZXQ"},
            "attributes": {"COMMENT": "raw group text"},
        }
    ]
    assert raw[1]["observation_attributes"] == {}
    assert raw[1]["group_context"] == raw[0]["group_context"]

    raw_before_normalization = deepcopy(raw)
    typed = normalize_reference_rates(raw)
    assert [row.value for row in typed] == [1.2345, 2.5]
    assert [row.source_value for row in typed] == ["1.234500", "2.5e0"]
    assert typed[0].date.isoformat() == "2026-10-08"
    typed[0].dataset_attributes["UNIT"] = "mutated typed value"
    typed[0].group_context[0].attributes["COMMENT"] = "mutated typed value"
    assert raw == raw_before_normalization


def test_missing_obs_value_is_nullable_and_valid_empty_dataset_returns_empty_tuple() -> None:
    raw = parse_generic_data(_document(_series([_obs(None)])))
    typed = normalize_reference_rates(raw)
    assert typed[0].value is None
    assert typed[0].source_value is None

    assert parse_generic_data(_document("")) == ()


def test_header_structure_selection_and_time_dimension_are_required() -> None:
    with pytest.raises(ReferenceRatesParseError, match="does not select"):
        parse_generic_data(_document(_series(), structure_ref="OTHER_REF"))
    with pytest.raises(ReferenceRatesParseError, match="TIME_PERIOD"):
        parse_generic_data(_document(_series(), dimension_at_observation="AllDimensions"))


def test_header_structure_reference_urn_text_is_supported_but_not_interpreted() -> None:
    body = _document(_series()).replace(
        b'<Ref agencyID="ECB" id="EXR" version="1.0"\n'
        b'             class="DataStructure" package="datastructure"/>',
        b"<URN>urn:sdmx:org.sdmx.infomodel.datastructure.DataStructure=ECB:EXR(1.0)</URN>",
    )
    assert len(parse_generic_data(body)) == 1

    with pytest.raises(ReferenceRatesParseError, match="URN must not contain child"):
        parse_generic_data(body.replace(b"</URN>", b"<nested/></URN>"))
    with pytest.raises(ReferenceRatesParseError, match="unsupported XML attribute"):
        parse_generic_data(body.replace(b"<URN>", b'<URN extra="x">'))


def test_message_and_data_namespaces_are_exact_and_error_payloads_do_not_succeed() -> None:
    with pytest.raises(ReferenceRatesParseError, match="root must be"):
        parse_generic_data(_document(_series(), message_namespace="urn:wrong-message"))
    with pytest.raises(ReferenceRatesParseError, match="unsupported element"):
        parse_generic_data(_document(_series(), generic_namespace="urn:wrong-data"))
    with pytest.raises(ReferenceRatesParseError, match="root must be"):
        parse_generic_data(b"<GenericData/>")
    with pytest.raises(ReferenceRatesParseError, match="root must be"):
        parse_generic_data(b"<html><body>error</body></html>")
    with pytest.raises(ReferenceRatesParseError, match="Footer"):
        parse_generic_data(
            _document(_series()).replace(
                b"</message:GenericData>",
                b'<footer:Footer xmlns:footer="'
                + FOOTER_NS.encode()
                + b'"/>\n</message:GenericData>',
            )
        )


def test_dtd_entity_flat_and_unsupported_xml_shapes_fail_closed() -> None:
    with pytest.raises(ReferenceRatesParseError, match="DOCTYPE and entity"):
        parse_generic_data(b'<!DOCTYPE x [<!ENTITY a "1">]><x/>')
    with pytest.raises(ReferenceRatesParseError, match="ungrouped/flat"):
        parse_generic_data(_document("<generic:Obs><generic:ObsKey/></generic:Obs>"))
    with pytest.raises(ReferenceRatesParseError, match="unsupported XML attribute"):
        parse_generic_data(
            _document(_series()).replace(
                b'<message:DataSet structureRef="ECB_REF"',
                b'<message:DataSet action="Append" structureRef="ECB_REF"',
            )
        )


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
    ("unsafe_declaration", "secret_marker"),
    [
        ("<!DOCTYPE x><x/>", None),
        (
            '<!DOCTYPE x [<!ENTITY private "private-entity-sentinel">]><x>&private;</x>',
            "private-entity-sentinel",
        ),
        (
            '<!DOCTYPE x [<!ENTITY remote SYSTEM "https://example.invalid/private">]>'
            "<x>&remote;</x>",
            "example.invalid",
        ),
    ],
)
def test_defused_parser_rejects_unsafe_xml_even_when_prefilter_is_bypassed(
    monkeypatch: pytest.MonkeyPatch,
    xml_encoding: str,
    codec: str,
    bom: bytes,
    unsafe_declaration: str,
    secret_marker: str | None,
) -> None:
    xml = f'<?xml version="1.0" encoding="{xml_encoding}"?>{unsafe_declaration}'
    body = bom + xml.encode(codec)
    monkeypatch.setattr(_reference_rates, "_reject_dtd_or_entity", lambda _: None)

    with pytest.raises(ReferenceRatesParseError, match="forbidden DTD or entity") as exc_info:
        parse_generic_data(body)

    assert exc_info.value.__suppress_context__ is True
    if secret_marker is not None:
        assert secret_marker not in str(exc_info.value)


def test_annotation_text_fields_are_allowed_and_unknown_annotation_shapes_fail() -> None:
    annotations = (
        '<common:Annotations><common:Annotation id="note">'
        "<common:AnnotationTitle>synthetic title</common:AnnotationTitle>"
        "<common:AnnotationType>synthetic type</common:AnnotationType>"
        "<common:AnnotationURL>urn:synthetic:annotation</common:AnnotationURL>"
        '<common:AnnotationText xml:lang="en">synthetic text</common:AnnotationText>'
        "</common:Annotation></common:Annotations>"
    )
    raw = parse_generic_data(_document(annotations + _series()))
    assert len(raw) == 1
    assert raw[0]["dataset_attributes"] == {}

    malformed = [
        annotations.replace("</common:Annotations>", "<common:Unexpected/></common:Annotations>"),
        annotations.replace(
            '<common:AnnotationText xml:lang="en">',
            '<common:AnnotationText xml:lang="en" extra="x">',
        ),
        annotations.replace(
            "synthetic text</common:AnnotationText>",
            "<common:Nested/>synthetic text</common:AnnotationText>",
        ),
    ]
    for invalid_annotations in malformed:
        with pytest.raises(ReferenceRatesParseError):
            parse_generic_data(_document(invalid_annotations + _series()))


@pytest.mark.parametrize(
    "bad_key",
    [
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>',
        '<generic:Value id="FREQ" value="D"/><generic:Value id="FREQ" value="D"/>'
        '<generic:Value id="CURRENCY" value="ZXQ"/><generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/><generic:Value id="EXR_SUFFIX" value="A"/>',
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="EXTRA" value="x"/>',
    ],
)
def test_series_key_requires_unique_exact_five_exr_dimensions(bad_key: str) -> None:
    with pytest.raises(ReferenceRatesParseError, match="SeriesKey"):
        parse_generic_data(_document(_series(key=bad_key)))


def test_series_key_values_may_arrive_in_any_order_but_key_is_canonical() -> None:
    permuted_key = (
        '<generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="EXR_SUFFIX" value="A"/>'
        '<generic:Value id="FREQ" value="D"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
    )
    raw = parse_generic_data(_document(_series(key=permuted_key)))
    assert raw[0]["series_key"] == "D.ZXQ.EUR.SP00.A"
    assert raw[0]["quote_currency"] == "ZXQ"


@pytest.mark.parametrize(
    "bad_key",
    [
        '<generic:Value id="FREQ" value="M"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="EXR_SUFFIX" value="A"/>',
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="USD"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="EXR_SUFFIX" value="A"/>',
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP01"/>'
        '<generic:Value id="EXR_SUFFIX" value="A"/>',
        '<generic:Value id="FREQ" value="D"/><generic:Value id="CURRENCY" value="ZXQ"/>'
        '<generic:Value id="CURRENCY_DENOM" value="EUR"/>'
        '<generic:Value id="EXR_TYPE" value="SP00"/>'
        '<generic:Value id="EXR_SUFFIX" value="B"/>',
    ],
)
def test_other_frequency_base_and_product_are_rejected(bad_key: str) -> None:
    with pytest.raises(ReferenceRatesParseError):
        parse_generic_data(_document(_series(key=bad_key)))


@pytest.mark.parametrize(
    "observation",
    [
        _obs(date="2026-1-08"),
        _obs(date="2026-10"),
        _obs(dimension_id="OBS_PERIOD"),
        _obs(value_id="OBS_STATUS"),
    ],
)
def test_observation_dimension_and_value_ids_are_strict(observation: str) -> None:
    with pytest.raises(ReferenceRatesParseError):
        parse_generic_data(_document(_series([observation])))


def test_duplicate_attribute_ids_and_unsupported_data_children_are_rejected() -> None:
    duplicate_attributes = (
        '<generic:Attributes><generic:Value id="UNIT" value="x"/>'
        '<generic:Value id="UNIT" value="x"/></generic:Attributes>'
    )
    with pytest.raises(ReferenceRatesParseError, match="duplicate Value id"):
        parse_generic_data(_document(duplicate_attributes + _series()))
    with pytest.raises(ReferenceRatesParseError, match="unsupported element"):
        parse_generic_data(
            _document(
                _series().replace("</generic:Series>", "<generic:Unexpected/></generic:Series>")
            )
        )


def test_identical_duplicate_rows_are_preserved_but_conflicts_fail() -> None:
    first = _series([_obs("1.0")])
    identical = parse_generic_data(_document(first + first))
    assert len(identical) == 2
    assert identical[0] == identical[1]

    conflicting = _series([_obs("2.0")])
    with pytest.raises(ReferenceRatesParseError, match="conflicting duplicate"):
        parse_generic_data(_document(first + conflicting))


def test_bad_last_observation_returns_no_partial_parse_or_normalized_result() -> None:
    body = _document(_series([_obs("1.0"), _obs("2.0", date="2026-10")]))
    with pytest.raises(ReferenceRatesParseError, match="daily ISO date"):
        parse_generic_data(body)

    with pytest.raises(ReferenceRatesParseError, match="numeric syntax"):
        parse_generic_data(
            _document(_series([_obs("1.0"), _obs("not-a-number", date="2026-10-09")]))
        )


@pytest.mark.parametrize("invalid_value", ["", "NaN", "INF", "Infinity", "1e999"])
def test_present_obs_value_must_be_a_finite_decimal_without_rewriting_raw_value(
    invalid_value: str,
) -> None:
    with pytest.raises(ReferenceRatesParseError, match="finite|numeric syntax|finite float"):
        parse_generic_data(_document(_series([_obs(invalid_value)])))


def test_normalizer_requires_the_complete_raw_record_including_value_presence() -> None:
    row = dict(parse_generic_data(_document(_series()))[0])
    del row["value"]
    with pytest.raises(ReferenceRatesParseError, match="raw reference-rate field set"):
        normalize_reference_rates([row])  # type: ignore[list-item]


@pytest.mark.parametrize(
    ("parameter", "bad_limit"),
    [
        (parameter, bad_limit)
        for parameter in (
            "max_body_bytes",
            "max_xml_nodes",
            "max_observations",
            "max_output_bytes",
        )
        for bad_limit in (True, False, 0, -1, 1.0, "10")
    ],
)
def test_resource_limits_require_positive_non_boolean_integers(
    parameter: str, bad_limit: object
) -> None:
    body = _document(_series())
    with pytest.raises(ReferenceRatesParseError, match="positive integer"):
        parse_generic_data(body, **{parameter: bad_limit})  # type: ignore[arg-type]


def test_output_budget_cannot_be_expanded_above_the_fixed_cap() -> None:
    with pytest.raises(ReferenceRatesParseError, match="fixed 8388608-byte cap"):
        parse_generic_data(_document(_series()), max_output_bytes=8 * 1024 * 1024 + 1)


def test_body_node_and_observation_limits_are_enforced_without_truncation() -> None:
    one_row = _document(_series())
    two_rows = _document(_series([_obs("1"), _obs("2", date="2026-10-09")]))
    with pytest.raises(ReferenceRatesParseError, match="body exceeds"):
        parse_generic_data(one_row, max_body_bytes=len(one_row) - 1)
    with pytest.raises(ReferenceRatesParseError, match="node count exceeds"):
        parse_generic_data(one_row, max_xml_nodes=2)
    with pytest.raises(ReferenceRatesParseError, match="observation count exceeds"):
        parse_generic_data(two_rows, max_observations=1)


def test_json_output_budget_counts_exact_array_bytes_across_datasets() -> None:
    first_dataset = _series([_obs("1.0", date="2026-10-08")])
    second_dataset = _series([_obs("2.0", date="2026-10-09")])
    body = _document(first_dataset).replace(
        b"</message:DataSet>",
        b'</message:DataSet><message:DataSet structureRef="ECB_REF">'
        + second_dataset.encode()
        + b"</message:DataSet>",
    )
    expected = parse_generic_data(body)
    encoded = json.dumps(
        list(expected), ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")

    assert parse_generic_data(body, max_output_bytes=len(encoded)) == expected
    with pytest.raises(ReferenceRatesParseError, match="raw JSON output exceeds"):
        parse_generic_data(body, max_output_bytes=len(encoded) - 1)

    empty = _document("")
    assert parse_generic_data(empty, max_output_bytes=2) == ()
    with pytest.raises(ReferenceRatesParseError, match="raw JSON output exceeds"):
        parse_generic_data(empty, max_output_bytes=1)


def test_large_repeated_group_context_is_rejected_before_output_expansion() -> None:
    large_value = "x" * 15_000
    large_group = (
        '<generic:Group type="EXR_GROUP"><generic:Attributes>'
        f'<generic:Value id="COMMENT" value="{large_value}"/>'
        "</generic:Attributes></generic:Group>"
    )
    observations = [
        _obs("1.0", date=(date(2000, 1, 1) + timedelta(days=index)).isoformat())
        for index in range(600)
    ]
    body = _document(large_group + _series(observations))
    assert len(body) < 4 * 1024 * 1024

    with pytest.raises(ReferenceRatesParseError, match="raw JSON output exceeds"):
        parse_generic_data(body)
