"""Pure GenericData extraction for the ECB YC and BPS products.

The XML envelope and its safety limits are shared with the EXR parser. Product
callbacks keep the distinct series dimensions and observation period semantics
explicit, while raw attributes remain attached to their original source layer.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from typing import TypeAlias, TypedDict, cast

from pydantic import ValidationError

from opendata.data.models.ecb_series import (
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
)
from opendata.data.providers.ecb.models import _reference_rates as _shared

_YC_DIMENSIONS = (
    "FREQ",
    "REF_AREA",
    "CURRENCY",
    "PROVIDER_FM",
    "INSTRUMENT_FM",
    "PROVIDER_FM_ID",
    "DATA_TYPE_FM",
)
_BPS_DIMENSIONS = (
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
_DIMENSION_TOKEN = re.compile(r"[A-Z0-9_][A-Z0-9_-]{0,31}\Z")
_MONTH_PERIOD = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])\Z")
_QUARTER_PERIOD = re.compile(r"(?P<year>[0-9]{4})-Q[1-4]\Z")

RawGroupContext: TypeAlias = _shared.RawSdmxGroupContext


class RawEcbSeriesRecord(TypedDict):
    """One source row with the complete product key and unmodified context."""

    series_key: str
    period: str
    dimensions: dict[str, str]
    value: str | None
    dataset_attributes: dict[str, str]
    series_attributes: dict[str, str]
    observation_attributes: dict[str, str]
    group_context: list[RawGroupContext]


_RawRecordSequence: TypeAlias = Sequence[RawEcbSeriesRecord]


def _validate_dimensions(
    element: _shared.ET.Element,
    *,
    dimensions: tuple[str, ...],
    prefix: str,
    frequencies: frozenset[str],
) -> tuple[dict[str, str], str]:
    values = _shared._parse_values(element, "SeriesKey")
    if set(values) != set(dimensions):
        _shared._raise(f"{prefix} SeriesKey must contain exactly its published dimensions")
    ordered: dict[str, str] = {}
    for dimension in dimensions:
        value = values[dimension]
        if not _DIMENSION_TOKEN.fullmatch(value):
            _shared._raise(f"{prefix} dimension values must be single uppercase source tokens")
        ordered[dimension] = value
    frequency = ordered["FREQ"]
    if frequency not in frequencies:
        _shared._raise(f"{prefix} frequency is outside the supported product slice")
    series_key = f"{prefix}." + ".".join(ordered[dimension] for dimension in dimensions)
    return ordered, series_key


def _validate_yc_period(period: str, frequency: str) -> str:
    if frequency != "B":
        _shared._raise("YC frequency must be business-day B")
    try:
        parsed = date.fromisoformat(period)
    except ValueError:
        _shared._raise("YC ObsDimension must be a canonical daily ISO date")
    if parsed.isoformat() != period:
        _shared._raise("YC ObsDimension must be a canonical daily ISO date")
    return period


def _validate_bps_period(period: str, frequency: str) -> str:
    if frequency == "M":
        if not _MONTH_PERIOD.fullmatch(period) or int(period[:4]) == 0:
            _shared._raise("BPS monthly ObsDimension must be YYYY-MM")
    elif frequency == "Q":
        match = _QUARTER_PERIOD.fullmatch(period)
        if match is None or int(match.group("year")) == 0:
            _shared._raise("BPS quarterly ObsDimension must be YYYY-Qn")
    else:
        _shared._raise("BPS frequency must be monthly M or quarterly Q")
    return period


def _copy_group_context(groups: list[RawGroupContext]) -> list[RawGroupContext]:
    return [
        {
            "group_type": group["group_type"],
            "key": dict(group["key"]),
            "attributes": dict(group["attributes"]),
        }
        for group in groups
    ]


def _parse_product_series(
    element: _shared.ET.Element,
    *,
    prefix: str,
    dimensions: tuple[str, ...],
    frequencies: frozenset[str],
    period_validator: Callable[[str, str], str],
    dataset_attributes: dict[str, str],
    group_context: list[RawGroupContext],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _shared._JsonArrayBudget,
) -> list[RawEcbSeriesRecord]:
    key_element, series_attributes, observations = _shared._parse_series_parts(
        element,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
    )
    dimension_values, series_key = _validate_dimensions(
        key_element,
        dimensions=dimensions,
        prefix=prefix,
        frequencies=frequencies,
    )
    if not observations:
        _shared._raise("Series with no observations is outside the supported data slice")

    result: list[RawEcbSeriesRecord] = []
    for observation in observations:
        period, raw_value, observation_attributes = _shared._parse_observation_components(
            observation
        )
        period = period_validator(period, dimension_values["FREQ"])
        row_for_size: RawEcbSeriesRecord = {
            "series_key": series_key,
            "period": period,
            "dimensions": dimension_values,
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
                "dimensions": dict(dimension_values),
                "dataset_attributes": dict(dataset_attributes),
                "series_attributes": dict(series_attributes),
                "observation_attributes": dict(observation_attributes),
                "group_context": _copy_group_context(group_context),
            }
        )
    return result


def _parse_yc_series(
    element: _shared.ET.Element,
    *,
    dataset_attributes: dict[str, str],
    group_context: list[RawGroupContext],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _shared._JsonArrayBudget,
) -> list[RawEcbSeriesRecord]:
    """Parse one YC Series with its seven official dimensions and daily period."""
    return _parse_product_series(
        element,
        prefix="YC",
        dimensions=_YC_DIMENSIONS,
        frequencies=frozenset({"B"}),
        period_validator=_validate_yc_period,
        dataset_attributes=dataset_attributes,
        group_context=group_context,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
        output_budget=output_budget,
    )


def _parse_bps_series(
    element: _shared.ET.Element,
    *,
    dataset_attributes: dict[str, str],
    group_context: list[RawGroupContext],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _shared._JsonArrayBudget,
) -> list[RawEcbSeriesRecord]:
    """Parse one BPS Series with its 17 official dimensions and period."""
    return _parse_product_series(
        element,
        prefix="BPS",
        dimensions=_BPS_DIMENSIONS,
        frequencies=frozenset({"M", "Q"}),
        period_validator=_validate_bps_period,
        dataset_attributes=dataset_attributes,
        group_context=group_context,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
        output_budget=output_budget,
    )


def _parse_yc_dataset(
    element: _shared.ET.Element,
    *,
    structures: Mapping[str, str],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _shared._JsonArrayBudget,
) -> list[RawEcbSeriesRecord]:
    """Parse one YC dataset using the shared GenericData layer."""
    return _shared._parse_dataset(
        element,
        structures=structures,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
        output_budget=output_budget,
        parse_series=_parse_yc_series,
    )


def _parse_bps_dataset(
    element: _shared.ET.Element,
    *,
    structures: Mapping[str, str],
    maximum_observations: int,
    observation_count: list[int],
    output_budget: _shared._JsonArrayBudget,
) -> list[RawEcbSeriesRecord]:
    """Parse one BPS dataset using the shared GenericData layer."""
    return _shared._parse_dataset(
        element,
        structures=structures,
        maximum_observations=maximum_observations,
        observation_count=observation_count,
        output_budget=output_budget,
        parse_series=_parse_bps_series,
    )


def _identity(record: RawEcbSeriesRecord) -> tuple[str, str]:
    return record["series_key"], record["period"]


def _validate_fixed_limits(
    *,
    max_body_bytes: int,
    max_xml_nodes: int,
    max_observations: int,
    max_output_bytes: int,
) -> None:
    fixed_limits = (
        ("max_body_bytes", max_body_bytes, _shared.DEFAULT_MAX_BODY_BYTES),
        ("max_xml_nodes", max_xml_nodes, _shared.DEFAULT_MAX_XML_NODES),
        ("max_observations", max_observations, _shared.DEFAULT_MAX_OBSERVATIONS),
    )
    for name, value, maximum in fixed_limits:
        if _shared._validate_limit(name, value) > maximum:
            _shared._raise(f"{name} cannot exceed the fixed maximum of {maximum}")
    _shared._validate_limit("max_output_bytes", max_output_bytes)


def parse_yield_curve_generic_data(
    body: bytes,
    *,
    max_body_bytes: int = _shared.DEFAULT_MAX_BODY_BYTES,
    max_xml_nodes: int = _shared.DEFAULT_MAX_XML_NODES,
    max_observations: int = _shared.DEFAULT_MAX_OBSERVATIONS,
    max_output_bytes: int = _shared.DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[RawEcbSeriesRecord, ...]:
    """Extract source-ordered YC rows from a supported GenericData message."""
    _validate_fixed_limits(
        max_body_bytes=max_body_bytes,
        max_xml_nodes=max_xml_nodes,
        max_observations=max_observations,
        max_output_bytes=max_output_bytes,
    )
    return _shared._parse_generic_message(
        body,
        dataset_parser=_parse_yc_dataset,
        observation_identity=_identity,
        max_body_bytes=max_body_bytes,
        max_xml_nodes=max_xml_nodes,
        max_observations=max_observations,
        max_output_bytes=max_output_bytes,
    )


def parse_balance_of_payments_generic_data(
    body: bytes,
    *,
    max_body_bytes: int = _shared.DEFAULT_MAX_BODY_BYTES,
    max_xml_nodes: int = _shared.DEFAULT_MAX_XML_NODES,
    max_observations: int = _shared.DEFAULT_MAX_OBSERVATIONS,
    max_output_bytes: int = _shared.DEFAULT_MAX_OUTPUT_BYTES,
) -> tuple[RawEcbSeriesRecord, ...]:
    """Extract source-ordered BPS rows while preserving monthly/quarterly periods."""
    _validate_fixed_limits(
        max_body_bytes=max_body_bytes,
        max_xml_nodes=max_xml_nodes,
        max_observations=max_observations,
        max_output_bytes=max_output_bytes,
    )
    return _shared._parse_generic_message(
        body,
        dataset_parser=_parse_bps_dataset,
        observation_identity=_identity,
        max_body_bytes=max_body_bytes,
        max_xml_nodes=max_xml_nodes,
        max_observations=max_observations,
        max_output_bytes=max_output_bytes,
    )


def _validate_raw_record(
    record: object,
    *,
    product: str,
    dimensions: tuple[str, ...],
) -> tuple[dict[str, object], str | None]:
    raw_fields = {
        "series_key",
        "period",
        "dimensions",
        "value",
        "dataset_attributes",
        "series_attributes",
        "observation_attributes",
        "group_context",
    }
    if not isinstance(record, Mapping) or set(record) != raw_fields:
        _shared._raise(f"{product} record does not match the complete raw field set")
    series_key = record["series_key"]
    period = record["period"]
    dimension_values = record["dimensions"]
    if not isinstance(series_key, str) or not isinstance(period, str):
        _shared._raise(f"{product} series_key and period must remain strings")
    if not isinstance(dimension_values, Mapping) or set(dimension_values) != set(dimensions):
        _shared._raise(f"{product} dimensions do not match the complete product key")
    if any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in dimension_values.items()
    ):
        _shared._raise(f"{product} dimensions must contain only source strings")
    for field_name in ("dataset_attributes", "series_attributes", "observation_attributes"):
        values = record[field_name]
        if not isinstance(values, Mapping) or any(
            not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
        ):
            _shared._raise(f"{product} {field_name} must be a string-to-string map")
    groups = record["group_context"]
    if not isinstance(groups, (list, tuple)):
        _shared._raise(f"{product} group_context must be a sequence")
    raw_value = record["value"]
    if raw_value is not None and not isinstance(raw_value, str):
        _shared._raise(f"{product} value must be original numeric text or None")
    for group in groups:
        if not isinstance(group, Mapping) or set(group) != {"group_type", "key", "attributes"}:
            _shared._raise(f"{product} group_context entry has an unsupported shape")
        for name in ("group_type",):
            if not isinstance(group[name], str):
                _shared._raise(f"{product} group_context fields must remain source strings")
        for name in ("key", "attributes"):
            values = group[name]
            if not isinstance(values, Mapping) or any(
                not isinstance(key, str) or not isinstance(value, str)
                for key, value in values.items()
            ):
                _shared._raise(f"{product} group_context maps must contain only strings")
    ordered = {dimension: dimension_values[dimension] for dimension in dimensions}
    expected_key = f"{product}." + ".".join(ordered.values())
    if series_key != expected_key:
        _shared._raise(f"{product} series_key does not match its source dimensions")
    return dict(record), raw_value


def _normalize_records(
    records: Sequence[RawEcbSeriesRecord],
    *,
    product: str,
    dimensions: tuple[str, ...],
    model: type[EcbYieldCurveObservation] | type[EcbBalanceOfPaymentsObservation],
) -> tuple[EcbYieldCurveObservation | EcbBalanceOfPaymentsObservation, ...]:
    if isinstance(records, (str, bytes, bytearray, Mapping)) or not isinstance(records, Sequence):
        _shared._raise(f"{product} records must be a sequence of raw records")
    normalized: list[EcbYieldCurveObservation | EcbBalanceOfPaymentsObservation] = []
    for index, record in enumerate(records):
        row, raw_value = _validate_raw_record(record, product=product, dimensions=dimensions)
        dimension_values = row["dimensions"]
        if not isinstance(dimension_values, Mapping):
            _shared._raise(f"{product} dimensions do not match the complete product key")
        model_data: dict[str, object] = {
            "series_key": row["series_key"],
            "value": None if raw_value is None else _shared._numeric_value(raw_value),
            "source_value": raw_value,
            "dataset_attributes": row["dataset_attributes"],
            "series_attributes": row["series_attributes"],
            "observation_attributes": row["observation_attributes"],
            "group_context": tuple(cast("Sequence[RawGroupContext]", row["group_context"])),
        }
        for dimension in dimensions:
            field_name = "frequency" if dimension == "FREQ" else dimension.lower()
            model_data[field_name] = dimension_values[dimension]
        if product == "YC":
            model_data["date"] = row["period"]
        else:
            model_data["period"] = row["period"]
        try:
            normalized.append(model.model_validate(model_data))
        except ValidationError as exc:
            raise _shared.ReferenceRatesParseError(
                f"{product} record {index} violates its typed contract"
            ) from exc
    return tuple(normalized)


def normalize_yield_curve_records(
    records: _RawRecordSequence,
) -> tuple[EcbYieldCurveObservation, ...]:
    """Normalize YC rows atomically into the central daily observation contract."""
    return _normalize_records(
        records,
        product="YC",
        dimensions=_YC_DIMENSIONS,
        model=EcbYieldCurveObservation,
    )  # type: ignore[return-value]


def normalize_balance_of_payments_records(
    records: _RawRecordSequence,
) -> tuple[EcbBalanceOfPaymentsObservation, ...]:
    """Normalize BPS rows atomically while keeping their monthly/quarterly period."""
    return _normalize_records(
        records,
        product="BPS",
        dimensions=_BPS_DIMENSIONS,
        model=EcbBalanceOfPaymentsObservation,
    )  # type: ignore[return-value]
