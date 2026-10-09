"""Pure SDMX-ML 2.1 GenericData parsing for ECB reference-rate observations.

This module supports the time-series GenericData slice described by the ECB
CurrencyReferenceRates contract. It does not perform transport or interpret
attribute attachment rules.
"""

from __future__ import annotations

import json
import math
import re

# ET supplies tree node types/building and ParseError only; all XML parsing uses defusedxml.
import xml.etree.ElementTree as ET  # nosec B405  # ET gives TreeBuilder/ParseError only; parsing goes through defusedxml DefusedXMLParser
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import NoReturn, Protocol, TypedDict, TypeVar

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import DefusedXMLParser
from pydantic import ValidationError

from opendata.data.models.currency import CurrencyReferenceRate

MESSAGE_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message"
COMMON_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"
GENERIC_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/data/generic"
FOOTER_NS = "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message/footer"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"

DEFAULT_MAX_BODY_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_XML_NODES = 100_000
DEFAULT_MAX_OBSERVATIONS = 10_000
DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024

_DIMENSIONS = ("FREQ", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX")
_RAW_RECORD_FIELDS = {
    "series_key",
    "date",
    "frequency",
    "quote_currency",
    "base_currency",
    "rate_type",
    "rate_suffix",
    "value",
    "dataset_attributes",
    "series_attributes",
    "observation_attributes",
    "group_context",
}
_NUMBER_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


class ReferenceRatesParseError(ValueError):
    """Raised when an XML payload is outside the supported reference-rate slice."""


class _NodeLimitError(Exception):
    pass


class _LimitedTreeBuilder(ET.TreeBuilder):
    def __init__(self, maximum: int) -> None:
        super().__init__()
        self.maximum = maximum
        self.count = 0

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.count += 1
        if self.count > self.maximum:
            raise _NodeLimitError
        return super().start(tag, attrs)


class RawSdmxGroupContext(TypedDict):
    """One raw SDMX Group key and attribute set without inferred attachment semantics."""

    group_type: str
    key: dict[str, str]
    attributes: dict[str, str]


class RawReferenceRateRecord(TypedDict):
    """One extracted source row; ``value`` remains the exact XML string."""

    series_key: str
    date: str
    frequency: str
    quote_currency: str
    base_currency: str
    rate_type: str
    rate_suffix: str
    value: str | None
    dataset_attributes: dict[str, str]
    series_attributes: dict[str, str]
    observation_attributes: dict[str, str]
    group_context: list[RawSdmxGroupContext]


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}" if namespace else local_name


def _raise(message: str) -> NoReturn:
    raise ReferenceRatesParseError(message)


def _validate_limit(name: str, value: object) -> int:
    if type(value) is not int or value < 1:
        _raise(f"{name} must be a positive integer (bool is not accepted)")
    return value


def _reject_dtd_or_entity(body: bytes) -> None:
    # Removing NUL bytes also detects the ASCII declarations in UTF-16/UTF-32
    # payloads before ElementTree sees any part of the document.
    normalized = body.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in normalized or b"<!ENTITY" in normalized:
        _raise("DOCTYPE and entity declarations are not supported")


def _split_tag(tag: str) -> tuple[str, str]:
    if tag.startswith("{"):
        namespace, _, local_name = tag[1:].partition("}")
        return namespace, local_name
    return "", tag


def _check_attributes(
    element: ET.Element,
    *,
    allowed: set[str],
    required: set[str] | None = None,
    context: str,
) -> None:
    unexpected = set(element.attrib) - allowed
    missing = (required or set()) - set(element.attrib)
    if unexpected:
        _raise(f"{context} has unsupported XML attribute(s): {sorted(unexpected)}")
    if missing:
        _raise(f"{context} is missing required XML attribute(s): {sorted(missing)}")


def _check_empty_content(element: ET.Element, context: str) -> None:
    if element.text and element.text.strip():
        _raise(f"{context} contains unsupported text content")
    if len(element):
        _raise(f"{context} must not contain child elements")
    if any(child.tail and child.tail.strip() for child in element):
        _raise(f"{context} contains unsupported text content")


def _check_text_content(element: ET.Element, context: str) -> None:
    if len(element):
        _raise(f"{context} must not contain child elements")


def _check_container_text(element: ET.Element, context: str) -> None:
    if element.text and element.text.strip():
        _raise(f"{context} contains unsupported text content")
    if any(child.tail and child.tail.strip() for child in element):
        _raise(f"{context} contains unsupported text content")


def _validate_annotations(element: ET.Element, context: str) -> None:
    if element.tag != _tag(COMMON_NS, "Annotations"):
        _raise(f"{context} contains an annotation in an unsupported namespace")
    _check_attributes(element, allowed=set(), context=context)
    _check_container_text(element, context)
    allowed_annotation_fields = {
        _tag(COMMON_NS, "AnnotationTitle"),
        _tag(COMMON_NS, "AnnotationType"),
        _tag(COMMON_NS, "AnnotationURL"),
        _tag(COMMON_NS, "AnnotationText"),
    }
    for annotation in element:
        if annotation.tag != _tag(COMMON_NS, "Annotation"):
            _raise(f"{context} contains an unsupported annotation element")
        _check_attributes(annotation, allowed={"id"}, context="Annotation")
        _check_container_text(annotation, "Annotation")
        for field in annotation:
            if field.tag not in allowed_annotation_fields:
                _raise("Annotation contains an unsupported element")
            allowed_attrs = (
                {_tag(XML_NS, "lang")} if field.tag == _tag(COMMON_NS, "AnnotationText") else set()
            )
            _check_attributes(field, allowed=allowed_attrs, context="Annotation field")
            _check_text_content(field, "Annotation field")


def _validate_header_structure(element: ET.Element) -> tuple[str, str]:
    _check_attributes(
        element,
        allowed={"structureID", "dimensionAtObservation"},
        required={"structureID", "dimensionAtObservation"},
        context="Header Structure",
    )
    structure_id = element.attrib["structureID"]
    if not structure_id or any(char.isspace() or ord(char) < 32 for char in structure_id):
        _raise("Header Structure structureID must be a nonempty token")
    dimension = element.attrib["dimensionAtObservation"]
    if dimension != "TIME_PERIOD":
        _raise("Header Structure dimensionAtObservation must be TIME_PERIOD")
    _check_container_text(element, "Header Structure")
    if len(element) != 1:
        _raise("Header Structure must contain exactly one published structure reference")
    reference = element[0]
    if reference.tag not in {
        _tag(COMMON_NS, "Structure"),
        _tag(COMMON_NS, "StructureUsage"),
        _tag(COMMON_NS, "ProvisionAgrement"),
    }:
        _raise("Header Structure contains an unsupported structure reference")
    _check_attributes(
        reference,
        allowed={"serviceURL", "structureURL"},
        context="Header structure reference",
    )
    _check_container_text(reference, "Header structure reference")
    ref_children = list(reference)
    if not ref_children or len(ref_children) > 2:
        _raise("Header structure reference has an unsupported shape")
    if any(child.tag not in {"Ref", "URN"} for child in ref_children):
        _raise("Header structure reference has an unsupported child")
    if len(ref_children) == 2 and [child.tag for child in ref_children] != ["Ref", "URN"]:
        _raise("Header structure reference has an unsupported child order")
    for child in ref_children:
        if child.tag == "Ref":
            _check_empty_content(child, "Header structure reference Ref")
        else:
            _check_attributes(child, allowed=set(), context="Header structure reference URN")
            _check_text_content(child, "Header structure reference URN")
    return structure_id, dimension


def _parse_header(header: ET.Element) -> dict[str, str]:
    _check_attributes(header, allowed=set(), context="Header")
    _check_container_text(header, "Header")
    allowed_message_fields = {
        "ID",
        "Test",
        "Prepared",
        "Sender",
        "Receiver",
        "Structure",
        "DataProvider",
        "DataSetAction",
        "DataSetID",
        "Extracted",
        "ReportingBegin",
        "ReportingEnd",
        "EmbargoDate",
        "Source",
    }
    structures: dict[str, str] = {}
    for child in header:
        namespace, local_name = _split_tag(child.tag)
        if namespace == MESSAGE_NS and local_name == "Structure":
            structure_id, dimension = _validate_header_structure(child)
            if structure_id in structures:
                _raise(f"Header contains duplicate structureID {structure_id!r}")
            structures[structure_id] = dimension
            continue
        if namespace == MESSAGE_NS and local_name in allowed_message_fields - {"Structure"}:
            # These published header fields are non-data context for this parser.
            continue
        if child.tag == _tag(COMMON_NS, "Name"):
            continue
        _raise(f"Header contains unsupported element {child.tag!r}")
    if not structures:
        _raise("Header is missing a published Structure declaration")
    return structures


def _parse_values(element: ET.Element, context: str) -> dict[str, str]:
    _check_attributes(element, allowed=set(), context=context)
    _check_container_text(element, context)
    values: dict[str, str] = {}
    for value in element:
        if value.tag != _tag(GENERIC_NS, "Value"):
            _raise(f"{context} contains an unsupported element")
        _check_attributes(
            value,
            allowed={"id", "value"},
            required={"id", "value"},
            context=f"{context} Value",
        )
        _check_empty_content(value, f"{context} Value")
        component_id = value.attrib["id"]
        component_value = value.attrib["value"]
        if not component_id or any(char.isspace() or ord(char) < 32 for char in component_id):
            _raise(f"{context} Value id must be a nonempty token")
        if component_id in values:
            _raise(f"{context} contains duplicate Value id {component_id!r}")
        values[component_id] = component_value
    if not values:
        _raise(f"{context} must contain at least one Value")
    return values


def _parse_group(element: ET.Element) -> RawSdmxGroupContext:
    _check_attributes(
        element,
        allowed={"type"},
        required={"type"},
        context="Group",
    )
    group_type = element.attrib["type"]
    if not group_type or any(char.isspace() or ord(char) < 32 for char in group_type):
        _raise("Group type must be a nonempty token")
    _check_container_text(element, "Group")
    group_key: dict[str, str] = {}
    attributes: dict[str, str] | None = None
    annotations_seen = False
    key_seen = False
    for child in element:
        if child.tag == _tag(COMMON_NS, "Annotations"):
            if annotations_seen:
                _raise("Group contains duplicate Annotations elements")
            _validate_annotations(child, "Group Annotations")
            annotations_seen = True
        elif child.tag == _tag(GENERIC_NS, "GroupKey"):
            if key_seen:
                _raise("Group contains duplicate GroupKey elements")
            group_key = _parse_values(child, "GroupKey")
            key_seen = True
        elif child.tag == _tag(GENERIC_NS, "Attributes"):
            if attributes is not None:
                _raise("Group contains duplicate Attributes elements")
            attributes = _parse_values(child, "Group Attributes")
        else:
            _raise(f"Group contains unsupported element {child.tag!r}")
    if attributes is None:
        _raise("Group is missing its required Attributes element")
    return {"group_type": group_type, "key": group_key, "attributes": attributes}


def _parse_series_key(element: ET.Element) -> tuple[dict[str, str], str]:
    values = _parse_values(element, "SeriesKey")
    if set(values) != set(_DIMENSIONS):
        _raise("SeriesKey must contain exactly the five EXR dimensions")
    for value in values.values():
        if (
            not value
            or "." in value
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            _raise("SeriesKey dimension values must be nonempty safe tokens")
    if values["FREQ"] != "D":
        _raise("Only daily D reference rates are supported")
    if values["CURRENCY_DENOM"] != "EUR":
        _raise("Only EUR-denominated reference rates are supported")
    if values["EXR_TYPE"] != "SP00" or values["EXR_SUFFIX"] != "A":
        _raise("Only SP00/A reference-rate products are supported")
    return values, ".".join(values[dimension] for dimension in _DIMENSIONS)


def _parse_observation_components(
    element: ET.Element,
) -> tuple[str, str | None, dict[str, str]]:
    """Read GenericData observation period, raw value, and source attributes."""
    _check_attributes(element, allowed=set(), context="Obs")
    _check_container_text(element, "Obs")
    dimension: ET.Element | None = None
    value: str | None = None
    value_seen = False
    attributes: dict[str, str] = {}
    attributes_seen = False
    annotations_seen = False
    for child in element:
        if child.tag == _tag(COMMON_NS, "Annotations"):
            if annotations_seen:
                _raise("Obs contains duplicate Annotations elements")
            _validate_annotations(child, "Obs Annotations")
            annotations_seen = True
        elif child.tag == _tag(GENERIC_NS, "ObsDimension"):
            if dimension is not None:
                _raise("Obs contains duplicate ObsDimension elements")
            dimension = child
        elif child.tag == _tag(GENERIC_NS, "ObsValue"):
            if value_seen:
                _raise("Obs contains duplicate ObsValue elements")
            _check_attributes(
                child,
                allowed={"id", "value"},
                required={"value"},
                context="ObsValue",
            )
            _check_empty_content(child, "ObsValue")
            if child.attrib.get("id", "OBS_VALUE") != "OBS_VALUE":
                _raise("ObsValue id must be OBS_VALUE when present")
            value = child.attrib["value"]
            _numeric_value(value)
            value_seen = True
        elif child.tag == _tag(GENERIC_NS, "Attributes"):
            if attributes_seen:
                _raise("Obs contains duplicate Attributes elements")
            attributes = _parse_values(child, "Observation Attributes")
            attributes_seen = True
        else:
            _raise(f"Obs contains unsupported element {child.tag!r}")
    if dimension is None:
        _raise("Obs is missing ObsDimension")
    _check_attributes(
        dimension,
        allowed={"id", "value"},
        required={"value"},
        context="ObsDimension",
    )
    _check_empty_content(dimension, "ObsDimension")
    if dimension.attrib.get("id", "TIME_PERIOD") != "TIME_PERIOD":
        _raise("ObsDimension id must be TIME_PERIOD when present")
    return dimension.attrib["value"], value, attributes


def _parse_observation(element: ET.Element) -> tuple[str, str | None, dict[str, str]]:
    """Read an EXR observation and require its canonical daily ISO date."""
    raw_date, value, attributes = _parse_observation_components(element)
    try:
        parsed_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise ReferenceRatesParseError("ObsDimension must be a daily ISO date") from exc
    if parsed_date.isoformat() != raw_date:
        _raise("ObsDimension must be a canonical daily ISO date")
    return raw_date, value, attributes


class _JsonArrayBudget:
    """Count the exact compact UTF-8 JSON bytes of all raw rows."""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 2  # JSON array opening and closing brackets.
        self.rows = 0
        if self.used > maximum:
            _raise(f"raw JSON output exceeds {maximum} bytes")

    def add(self, row: Mapping[str, object]) -> None:
        encoded = json.dumps(
            row,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        required = self.used + len(encoded) + (1 if self.rows else 0)
        if required > self.maximum:
            _raise(f"raw JSON output exceeds {self.maximum} bytes")
        self.used = required
        self.rows += 1


_RecordT = TypeVar("_RecordT")


class _SeriesParser(Protocol[_RecordT]):
    """Parse a GenericData Series into product-specific raw observation rows."""

    def __call__(
        self,
        element: ET.Element,
        *,
        dataset_attributes: dict[str, str],
        group_context: list[RawSdmxGroupContext],
        maximum_observations: int,
        observation_count: list[int],
        output_budget: _JsonArrayBudget,
    ) -> list[_RecordT]: ...


class _DatasetParser(Protocol[_RecordT]):
    """Parse one GenericData dataset using the shared message state and limits."""

    def __call__(
        self,
        element: ET.Element,
        *,
        structures: Mapping[str, str],
        maximum_observations: int,
        observation_count: list[int],
        output_budget: _JsonArrayBudget,
    ) -> list[_RecordT]: ...


def _parse_series_parts(
    element: ET.Element,
    *,
    maximum_observations: int,
    observation_count: list[int],
) -> tuple[ET.Element, dict[str, str], list[ET.Element]]:
    """Extract one series key, its attributes, and its observation elements."""
    _check_attributes(element, allowed=set(), context="Series")
    _check_container_text(element, "Series")
    series_key_element: ET.Element | None = None
    series_attributes: dict[str, str] = {}
    series_attributes_seen = False
    annotations_seen = False
    observations: list[ET.Element] = []
    for child in element:
        if child.tag == _tag(COMMON_NS, "Annotations"):
            if annotations_seen:
                _raise("Series contains duplicate Annotations elements")
            _validate_annotations(child, "Series Annotations")
            annotations_seen = True
        elif child.tag == _tag(GENERIC_NS, "SeriesKey"):
            if series_key_element is not None:
                _raise("Series contains duplicate SeriesKey elements")
            series_key_element = child
        elif child.tag == _tag(GENERIC_NS, "Attributes"):
            if series_attributes_seen:
                _raise("Series contains duplicate Attributes elements")
            series_attributes = _parse_values(child, "Series Attributes")
            series_attributes_seen = True
        elif child.tag == _tag(GENERIC_NS, "Obs"):
            observation_count[0] += 1
            if observation_count[0] > maximum_observations:
                _raise(f"XML observation count exceeds {maximum_observations}")
            observations.append(child)
        else:
            _raise(f"Series contains unsupported element {child.tag!r}")
    if series_key_element is None:
        _raise("Series is missing SeriesKey")
    return series_key_element, series_attributes, observations


def _parse_series(
    element: ET.Element,
    *,
    dataset_attributes: dict[str, str],
    group_context: list[RawSdmxGroupContext],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _JsonArrayBudget,
) -> list[RawReferenceRateRecord]:
    series_key_element, series_attributes, observations = _parse_series_parts(
        element,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
    )
    dimensions, series_key = _parse_series_key(series_key_element)
    if not observations:
        _raise("Series with no observations is outside the supported data slice")
    result: list[RawReferenceRateRecord] = []
    for observation in observations:
        raw_date, raw_value, observation_attributes = _parse_observation(observation)
        row_for_size: RawReferenceRateRecord = {
            "series_key": series_key,
            "date": raw_date,
            "frequency": dimensions["FREQ"],
            "quote_currency": dimensions["CURRENCY"],
            "base_currency": dimensions["CURRENCY_DENOM"],
            "rate_type": dimensions["EXR_TYPE"],
            "rate_suffix": dimensions["EXR_SUFFIX"],
            "value": raw_value,
            "dataset_attributes": dataset_attributes,
            "series_attributes": series_attributes,
            "observation_attributes": observation_attributes,
            "group_context": group_context,
        }
        output_budget.add(row_for_size)
        result.append(
            {
                **row_for_size,
                "dataset_attributes": dict(dataset_attributes),
                "series_attributes": dict(series_attributes),
                "observation_attributes": dict(observation_attributes),
                "group_context": [
                    {
                        "group_type": group["group_type"],
                        "key": dict(group["key"]),
                        "attributes": dict(group["attributes"]),
                    }
                    for group in group_context
                ],
            }
        )
    return result


def _parse_dataset(
    element: ET.Element,
    *,
    structures: Mapping[str, str],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _JsonArrayBudget,
    parse_series: _SeriesParser[_RecordT],
) -> list[_RecordT]:
    _check_attributes(
        element,
        allowed={"structureRef"},
        required={"structureRef"},
        context="DataSet",
    )
    structure_ref = element.attrib["structureRef"]
    if structure_ref not in structures:
        _raise(f"DataSet structureRef {structure_ref!r} does not select a Header Structure")
    if structures[structure_ref] != "TIME_PERIOD":
        _raise("DataSet Header Structure must use TIME_PERIOD at observation level")
    _check_container_text(element, "DataSet")
    dataset_attributes: dict[str, str] = {}
    attributes_seen = False
    groups: list[RawSdmxGroupContext] = []
    series: list[ET.Element] = []
    annotations_seen = False
    for child in element:
        if child.tag == _tag(COMMON_NS, "Annotations"):
            if annotations_seen:
                _raise("DataSet contains duplicate Annotations elements")
            _validate_annotations(child, "DataSet Annotations")
            annotations_seen = True
        elif child.tag == _tag(GENERIC_NS, "Attributes"):
            if attributes_seen:
                _raise("DataSet contains duplicate Attributes elements")
            dataset_attributes = _parse_values(child, "DataSet Attributes")
            attributes_seen = True
        elif child.tag == _tag(GENERIC_NS, "Group"):
            groups.append(_parse_group(child))
        elif child.tag == _tag(GENERIC_NS, "Series"):
            series.append(child)
        elif child.tag == _tag(GENERIC_NS, "DataProvider"):
            _raise("DataSet DataProvider is unsupported by the row contract")
        elif child.tag == _tag(GENERIC_NS, "Obs"):
            _raise("ungrouped/flat Obs rows are outside the supported time-series slice")
        else:
            _raise(f"DataSet contains unsupported element {child.tag!r}")
    result: list[_RecordT] = []
    for series_element in series:
        result.extend(
            parse_series(
                series_element,
                dataset_attributes=dataset_attributes,
                group_context=groups,
                maximum_observations=maximum_observations,
                observation_count=observation_count,
                output_budget=output_budget,
            )
        )
    if not result and (dataset_attributes or groups):
        _raise("DataSet-level context has no observation rows to preserve it")
    return result


def _parse_exr_dataset(
    element: ET.Element,
    *,
    structures: Mapping[str, str],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _JsonArrayBudget,
) -> list[RawReferenceRateRecord]:
    """Bind the shared dataset parser to the existing EXR series parser."""
    return _parse_dataset(
        element,
        structures=structures,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
        output_budget=output_budget,
        parse_series=_parse_series,
    )


def _parse_generic_message(
    body: bytes,
    *,
    dataset_parser: _DatasetParser[_RecordT],
    observation_identity: Callable[[_RecordT], tuple[str, str]],
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_xml_nodes: int = DEFAULT_MAX_XML_NODES,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[_RecordT, ...]:
    """Parse the shared SDMX GenericData envelope and delegate each dataset."""
    body_limit = _validate_limit("max_body_bytes", max_body_bytes)
    node_limit = _validate_limit("max_xml_nodes", max_xml_nodes)
    observation_limit = _validate_limit("max_observations", max_observations)
    output_limit = _validate_limit("max_output_bytes", max_output_bytes)
    if output_limit > DEFAULT_MAX_OUTPUT_BYTES:
        _raise(f"max_output_bytes cannot exceed the fixed {DEFAULT_MAX_OUTPUT_BYTES}-byte cap")
    if not isinstance(body, bytes):
        _raise("body must be bytes")
    if len(body) > body_limit:
        _raise(f"XML body exceeds {body_limit} bytes")
    _reject_dtd_or_entity(body)
    builder = _LimitedTreeBuilder(node_limit)
    parser = DefusedXMLParser(
        target=builder,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    try:
        parser.feed(body)
        root = parser.close()
    except _NodeLimitError as exc:
        raise ReferenceRatesParseError(f"XML node count exceeds {node_limit}") from exc
    except DefusedXmlException:
        raise ReferenceRatesParseError(
            "XML parser rejected a forbidden DTD or entity construct"
        ) from None
    except ET.ParseError as exc:
        raise ReferenceRatesParseError("body is not well-formed supported XML") from exc
    if root.tag != _tag(MESSAGE_NS, "GenericData"):
        _raise("root must be SDMX 2.1 message:GenericData")
    _check_attributes(
        root,
        allowed={_tag(XSI_NS, "schemaLocation")},
        context="GenericData",
    )
    _check_container_text(root, "GenericData")
    children = list(root)
    if any(child.tag == _tag(FOOTER_NS, "Footer") for child in children):
        _raise("message Footer is unsupported and cannot be treated as a data response")
    if any(
        child.tag not in {_tag(MESSAGE_NS, "Header"), _tag(MESSAGE_NS, "DataSet")}
        for child in children
    ):
        _raise("GenericData contains an unsupported message element")
    headers = [child for child in children if child.tag == _tag(MESSAGE_NS, "Header")]
    if len(headers) != 1 or not children or children[0] is not headers[0]:
        _raise("GenericData must begin with exactly one Header")
    structures = _parse_header(headers[0])
    result: list[_RecordT] = []
    observation_count = [0]
    output_budget = _JsonArrayBudget(output_limit)
    for dataset in children[1:]:
        result.extend(
            dataset_parser(
                dataset,
                structures=structures,
                maximum_observations=observation_limit,
                observation_count=observation_count,
                output_budget=output_budget,
            )
        )
    seen: dict[tuple[str, str], _RecordT] = {}
    for record in result:
        identity = observation_identity(record)
        prior = seen.get(identity)
        if prior is not None and prior != record:
            _raise(f"conflicting duplicate observation for {identity[0]} at {identity[1]}")
        seen[identity] = record
    return tuple(result)


def _reference_rate_identity(record: RawReferenceRateRecord) -> tuple[str, str]:
    return record["series_key"], record["date"]


def parse_generic_data(
    body: bytes,
    *,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    max_xml_nodes: int = DEFAULT_MAX_XML_NODES,
    max_observations: int = DEFAULT_MAX_OBSERVATIONS,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[RawReferenceRateRecord, ...]:
    """Extract supported daily EUR-reference rows without converting values.

    The returned tuple preserves XML source order. Exact duplicate series/date
    observations are preserved in that order; conflicting duplicates fail.
    Dataset, series, observation, and group attributes remain raw strings in
    their original layers. The compact UTF-8 JSON-array representation is
    bounded by ``max_output_bytes``, which cannot exceed the fixed 8 MiB cap.
    """
    return _parse_generic_message(
        body,
        dataset_parser=_parse_exr_dataset,
        observation_identity=_reference_rate_identity,
        max_body_bytes=max_body_bytes,
        max_xml_nodes=max_xml_nodes,
        max_observations=max_observations,
        max_output_bytes=max_output_bytes,
    )


def _numeric_value(raw_value: str) -> float:
    if not _NUMBER_PATTERN.fullmatch(raw_value):
        _raise("ObsValue must use finite decimal numeric syntax")
    try:
        decimal_value = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ReferenceRatesParseError("ObsValue must use finite decimal numeric syntax") from exc
    if not decimal_value.is_finite():
        _raise("ObsValue must be finite")
    try:
        value = float(decimal_value)
    except (OverflowError, ValueError) as exc:
        raise ReferenceRatesParseError("ObsValue must convert to a finite float") from exc
    if not math.isfinite(value):
        _raise("ObsValue must convert to a finite float")
    return value


def normalize_reference_rates(
    records: Sequence[RawReferenceRateRecord],
) -> tuple[CurrencyReferenceRate, ...]:
    """Convert raw extractor records into typed contracts atomically."""
    if isinstance(records, (str, bytes, bytearray, Mapping)) or not isinstance(records, Sequence):
        _raise("records must be a sequence of raw reference-rate records")
    normalized: list[CurrencyReferenceRate] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            _raise(f"record {index} must be a mapping")
        if set(record) != _RAW_RECORD_FIELDS:
            _raise(f"record {index} does not match the raw reference-rate field set")
        raw_value = record.get("value")
        if raw_value is None:
            numeric_value = None
        elif isinstance(raw_value, str):
            numeric_value = _numeric_value(raw_value)
        else:
            _raise(f"record {index} value must be the original string or None")
        model_data = dict(record)
        model_data["source_value"] = raw_value
        model_data["value"] = numeric_value
        try:
            normalized.append(CurrencyReferenceRate.model_validate(model_data))
        except ValidationError as exc:
            raise ReferenceRatesParseError(
                f"record {index} violates CurrencyReferenceRate: {exc}"
            ) from exc
    return tuple(normalized)
