"""Strict physical-key and row codecs for reviewed provider models.

This module deliberately covers only the reviewed FRED, BLS, and FMP
historical contracts. It does not create tables, access providers, or grant
operations.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import date
from typing import TYPE_CHECKING

from pydantic import ValidationError

from opendata.data.domains import contract_model, require_domain_semantics
from opendata.data.models import (
    BlsFootnote,
    BlsObservation,
    EquityHistorical,
    SeriesObservation,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_FRED_CONTEXT_FIELDS = (
    "transform_units",
    "output_type",
    "requested_frequency",
    "requested_aggregation_method",
)
_AUDIT_FIELDS = frozenset(
    {
        "_source",
        "_fetched_at",
        "_batch_id",
        "source",
        "_merged_at",
        "_diff_flag",
        "_as_of",
    }
)
_REVIEWED_DOMAINS: dict[str, dict[str, object]] = {
    "fred_series": {
        "contract": "SeriesObservation",
        "model": SeriesObservation,
        "temporal_kind": "series",
        "time_field": "date",
        "natural_key": (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "filter_dims": (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "transform_units",
            "output_type",
            "requested_frequency",
            "requested_aggregation_method",
        ),
        "storage_mode": "upsert",
        "physical_key": (
            "series_id",
            "date",
            "realtime_start",
            "realtime_end",
            "_request_context",
        ),
    },
    "bls_series": {
        "contract": "BlsObservation",
        "model": BlsObservation,
        "temporal_kind": "series",
        "time_field": "year",
        "natural_key": ("series_id", "year", "period"),
        "filter_dims": ("series_id", "year", "period", "api_version"),
        "storage_mode": "upsert",
        "physical_key": ("series_id", "year", "period"),
    },
    "equity_historical": {
        "contract": "EquityHistorical",
        "model": EquityHistorical,
        "temporal_kind": "series",
        "time_field": "date",
        "natural_key": ("symbol", "date"),
        "filter_dims": (
            "symbol",
            "date",
            "query_window_scope",
            "close_adjustment_semantics",
        ),
        "storage_mode": "upsert",
        "physical_key": ("symbol", "date"),
    },
}


def _reviewed_domain(domain: str) -> tuple[type[ContractModel], tuple[str, ...]]:
    """Return an exact reviewed contract after checking the live DomainSpec."""
    if not isinstance(domain, str):
        raise ValueError("storage domain must be a string")
    expected = _REVIEWED_DOMAINS.get(domain)
    if expected is None:
        raise ValueError("storage codec does not support this domain")

    try:
        spec = require_domain_semantics(domain)
        model = contract_model(domain)
    except (LookupError, ValueError, TypeError):
        raise ValueError("storage domain is not fully declared") from None

    if (
        not spec.semantics_declared
        or spec.contract != expected["contract"]
        or spec.temporal_kind != expected["temporal_kind"]
        or spec.time_field != expected["time_field"]
        or spec.natural_key != expected["natural_key"]
        or spec.filter_dims != expected["filter_dims"]
        or spec.storage_mode != expected["storage_mode"]
        or "store" not in spec.permissions
        or model is not expected["model"]
    ):
        raise ValueError("storage domain declaration does not match the reviewed codec")

    physical_key = expected["physical_key"]
    if not isinstance(physical_key, tuple) or not all(
        isinstance(field_name, str) for field_name in physical_key
    ):
        raise ValueError("reviewed physical key configuration is invalid")
    return model, physical_key


def physical_model_key(domain: str) -> tuple[str, ...]:
    """Return the reviewed physical key column names for a series domain."""
    _, physical_key = _reviewed_domain(domain)
    return physical_key


def _strict_row_values(
    domain: str, row: ContractModel, expected_model: type[ContractModel]
) -> dict[str, object]:
    """Dump and strictly revalidate an exact central contract instance."""
    if type(row) is not expected_model:
        raise ValueError("row does not use the exact reviewed central contract")

    model_field_names = set(expected_model.model_fields)
    if set(vars(row)) != model_field_names or getattr(row, "__pydantic_extra__", None):
        raise ValueError("row contains missing or unknown contract fields")

    try:
        dumped = row.model_dump(mode="python", by_alias=False)
        if type(dumped) is not dict or set(dumped) != model_field_names:
            raise ValueError
        _validate_storage_python_types(domain, dumped)
        validated = expected_model.model_validate(dumped, strict=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise ValueError(f"invalid {domain} contract row") from None

    values = validated.model_dump(mode="python", by_alias=False)
    if type(values) is not dict or set(values) != model_field_names:
        raise ValueError(f"invalid {domain} contract row")

    return values


def _validate_storage_python_types(domain: str, values: Mapping[str, object]) -> None:
    """Reject ambiguous Python values before Pydantic can normalize them."""
    if domain == "fred_series":
        if type(values.get("output_type")) is not int:
            raise ValueError("FRED output_type must be an integer literal")
        observation_value = values.get("value")
        if observation_value is not None and (
            type(observation_value) is not float or not math.isfinite(observation_value)
        ):
            raise ValueError("FRED observation value must be a finite float or null")
        if any(
            type(values.get(field_name)) is not date
            for field_name in ("date", "realtime_start", "realtime_end")
        ):
            raise ValueError("FRED date fields must be plain dates")
    elif domain == "bls_series":
        if type(values.get("year")) is not int:
            raise ValueError("BLS year must be an integer")
        observation_value = values.get("value")
        if type(observation_value) is not float or not math.isfinite(observation_value):
            raise ValueError("BLS value must be a finite float")
    elif domain == "equity_historical":
        if type(values.get("symbol")) is not str:
            raise ValueError("equity symbol must be a string")
        if type(values.get("date")) is not date:
            raise ValueError("equity historical date must be a plain date")
        for field_name in ("open", "high", "low", "close"):
            value = values.get(field_name)
            if type(value) is not float or not math.isfinite(value):
                raise ValueError("equity price fields must be finite floats")
        for field_name in ("change", "change_percent", "vwap"):
            value = values.get(field_name)
            if value is not None and (type(value) is not float or not math.isfinite(value)):
                raise ValueError("optional equity price fields must be finite floats or null")
        volume = values.get("volume")
        if volume is not None and type(volume) not in (int, float):
            raise ValueError("equity volume must be an exact integer, float, or null")
        if type(volume) is float and not math.isfinite(volume):
            raise ValueError("equity volume float must be finite")
        if values.get("currency") is not None or values.get("volume_unit") is not None:
            raise ValueError("equity currency and volume unit remain unverified")
        if values.get("adj_close_provided") is not False:
            raise ValueError("equity adjusted close flag must remain false")


def _canonical_json(value: object) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ValueError("storage JSON value is not serializable finite JSON") from None
    if not isinstance(encoded, str):
        raise ValueError("storage JSON value is not serializable")
    return encoded


def _fred_request_context(values: Mapping[str, object]) -> str:
    output_type = values["output_type"]
    if type(output_type) is not int:
        raise ValueError("FRED output_type must be an integer literal")
    return _canonical_json([values[field_name] for field_name in _FRED_CONTEXT_FIELDS])


def _encode_equity_volume(value: object) -> str | None:
    """Encode an exact equity volume as driver-bindable SQL JSON text."""
    if value is None:
        return None
    if type(value) is int:
        return _canonical_json({"kind": "int", "value": str(value)})
    if type(value) is float and math.isfinite(value):
        return _canonical_json({"kind": "float", "value": value.hex()})
    raise ValueError("equity volume must be an exact finite integer, float, or null")


def encode_model_storage_row(domain: str, row: ContractModel) -> dict[str, object]:
    """Encode one reviewed FRED/BLS/equity historical row for storage.

    The returned mapping is a codec result only; callers still need an
    explicitly reviewed schema, writer, and operation authorization.
    """
    expected_model, _ = _reviewed_domain(domain)
    values = _strict_row_values(domain, row, expected_model)

    if domain == "fred_series":
        encoded = dict(values)
        encoded["_request_context"] = _fred_request_context(values)
        return encoded

    if domain == "equity_historical":
        encoded = dict(values)
        encoded["volume"] = _encode_equity_volume(values["volume"])
        return encoded

    footnotes = values["footnotes"]
    if not isinstance(footnotes, tuple):
        raise ValueError("BLS footnotes must be a validated tuple")
    footnote_values: list[dict[str, object]] = []
    for footnote in footnotes:
        if type(footnote) is not dict:
            raise ValueError("BLS footnotes must use the central footnote contract")
        footnote_dump = footnote
        if type(footnote_dump) is not dict or set(footnote_dump) != set(BlsFootnote.model_fields):
            raise ValueError("BLS footnote contains unknown fields")
        try:
            strict_footnote = BlsFootnote.model_validate(footnote_dump, strict=True)
        except (ValidationError, TypeError, ValueError):
            raise ValueError("BLS footnote is invalid") from None
        footnote_values.append(strict_footnote.model_dump(mode="python", by_alias=False))

    encoded = dict(values)
    encoded["footnotes"] = _canonical_json(footnote_values)
    return encoded


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_non_finite_json_constant(value: str) -> object:
    del value
    raise ValueError("non-finite JSON number")


def _parse_json_array_text(value: str) -> list[object]:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("stored JSON array is invalid") from None
    if type(parsed) is not list:
        raise ValueError("stored JSON value must be an array")
    return parsed


def _parse_json_object_text(value: str) -> dict[str, object]:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_non_finite_json_constant,
        )
    except (TypeError, ValueError, RecursionError):
        raise ValueError("stored equity volume JSON is invalid") from None
    if type(parsed) is not dict or any(type(key) is not str for key in parsed):
        raise ValueError("stored equity volume must be a JSON object")
    return parsed


def _decode_equity_volume(value: object) -> int | float | None:
    """Decode the tagged, exact-number representation used for SQL JSON."""
    if value is None:
        return None
    if type(value) is str:
        payload = _parse_json_object_text(value)
    elif type(value) is dict:
        payload = value
    else:
        raise ValueError("stored equity volume must be JSON text, an object, or null")

    if set(payload) != {"kind", "value"}:
        raise ValueError("stored equity volume object has unknown or missing fields")
    kind = payload["kind"]
    encoded_value = payload["value"]
    if type(kind) is not str or type(encoded_value) is not str:
        raise ValueError("stored equity volume tag and value must be strings")

    if kind == "int":
        try:
            decoded_int = int(encoded_value)
        except ValueError:
            raise ValueError("stored equity integer volume is invalid") from None
        if str(decoded_int) != encoded_value:
            raise ValueError("stored equity integer volume is not canonical decimal text")
        return decoded_int

    if kind == "float":
        try:
            decoded_float = float.fromhex(encoded_value)
        except (OverflowError, ValueError):
            raise ValueError("stored equity float volume is invalid hexadecimal text") from None
        if not math.isfinite(decoded_float) or decoded_float.hex() != encoded_value:
            raise ValueError("stored equity float volume is not canonical finite hexadecimal text")
        return decoded_float

    raise ValueError("stored equity volume has an unknown numeric kind")


def _decode_equity_adjusted_close_flag(value: object) -> bool:
    """Accept only the source contract's literal false and its SQL TINY zero."""
    if value is False:
        return False
    if type(value) is int and value == 0:
        return False
    raise ValueError("equity adj_close_provided must be exact false or TINY zero")


def _decode_bls_footnotes(value: object) -> tuple[BlsFootnote, ...]:
    if isinstance(value, str):
        parsed = _parse_json_array_text(value)
    elif type(value) is list:
        parsed = value
    else:
        raise ValueError("BLS footnotes must be JSON text or a native JSON array")

    strict_footnotes: list[BlsFootnote] = []
    for item in parsed:
        if type(item) is not dict or any(type(key) is not str for key in item):
            raise ValueError("BLS footnote entries must be JSON objects")
        if not set(item).issubset(BlsFootnote.model_fields):
            raise ValueError("BLS footnote contains unknown fields")
        if any(
            item.get(field_name) is not None and type(item.get(field_name)) is not str
            for field_name in ("code", "text")
        ):
            raise ValueError("BLS footnote fields must be strings or null")
        try:
            strict_footnotes.append(BlsFootnote.model_validate(item, strict=True))
        except (ValidationError, TypeError, ValueError):
            raise ValueError("BLS footnote is invalid") from None
    return tuple(strict_footnotes)


def _decode_bls_tiny_boolean_fields(record: dict[str, object]) -> None:
    """Normalize PyMySQL TINY integer results for the two BLS bool columns."""
    for field_name in ("latest", "preliminary"):
        if field_name not in record:
            continue
        value = record[field_name]
        if field_name == "latest" and value is None:
            continue
        if type(value) is bool:
            continue
        if type(value) is int and value in (0, 1):
            record[field_name] = bool(value)
            continue
        raise ValueError(f"BLS {field_name} must be bool or an exact TINY 0/1 value")


def _copy_record(record: Mapping[str, object]) -> dict[str, object]:
    copied: dict[str, object] = {}
    try:
        items = record.items()
        for key, value in items:
            if type(key) is not str:
                raise ValueError("storage record keys must be strings")
            if key in copied:
                raise ValueError("storage record contains duplicate keys")
            copied[key] = value
    except (TypeError, ValueError, RecursionError):
        raise ValueError("storage record mapping is invalid") from None
    return copied


def decode_model_storage_row(domain: str, record: Mapping[str, object]) -> ContractModel:
    """Decode one native storage record into its exact central contract."""
    expected_model, _ = _reviewed_domain(domain)
    if not isinstance(record, Mapping):
        raise ValueError("storage record must be a mapping")
    copied = _copy_record(record)
    public_fields = set(expected_model.model_fields)
    allowed_fields = public_fields | set(_AUDIT_FIELDS)
    if domain == "fred_series":
        allowed_fields.add("_request_context")
    if not set(copied).issubset(allowed_fields):
        raise ValueError("storage record contains unknown fields")

    context_text: str | None = None
    if domain == "fred_series":
        raw_context = copied.pop("_request_context", None)
        if not isinstance(raw_context, str):
            raise ValueError("FRED storage row requires canonical request context")
        context_text = raw_context

    for field_name in _AUDIT_FIELDS:
        copied.pop(field_name, None)

    if domain == "bls_series":
        if "footnotes" not in copied:
            raise ValueError("BLS storage row requires footnotes")
        copied["footnotes"] = _decode_bls_footnotes(copied["footnotes"])
        _decode_bls_tiny_boolean_fields(copied)
    elif domain == "equity_historical":
        if not public_fields.issubset(copied):
            raise ValueError("equity historical storage row requires every contract field")
        copied["volume"] = _decode_equity_volume(copied["volume"])
        copied["adj_close_provided"] = _decode_equity_adjusted_close_flag(
            copied["adj_close_provided"]
        )

    _validate_storage_python_types(domain, copied)

    try:
        validated = expected_model.model_validate(copied, strict=True)
    except (ValidationError, TypeError, ValueError, RecursionError):
        raise ValueError(f"invalid {domain} storage row") from None

    values = _strict_row_values(domain, validated, expected_model)
    if domain == "fred_series":
        expected_context = _fred_request_context(values)
        if context_text != expected_context:
            raise ValueError("FRED request context does not match contract fields")
    return validated
