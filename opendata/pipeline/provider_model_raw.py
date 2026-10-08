"""Immutable, bounded captures of reviewed provider extractor output.

Raw payloads here are parsed extractor results, not wire bytes, response
headers, or transport request details.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from pydantic import ValidationError

from opendata.data.providers.bls.models.series import BlsSeriesQuery
from opendata.data.providers.fmp.models.equity_historical import EquityHistoricalQuery
from opendata.data.providers.fred.models.series import FredSeriesQuery

if TYPE_CHECKING:
    from opendata.data.protocol import QueryParams

JSONValue: TypeAlias = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]

MAX_RAW_ROWS = 10_000
MAX_RAW_PAYLOAD_BYTES = 8 * 1024 * 1024
MAX_QUERY_CONTEXT_BYTES = 64 * 1024
MAX_JSON_DEPTH = 64

_QUERY_MODELS: dict[tuple[str, str], type[QueryParams]] = {
    ("fred", "FredSeries"): FredSeriesQuery,
    ("bls", "BlsSeries"): BlsSeriesQuery,
    ("fmp", "EquityHistorical"): EquityHistoricalQuery,
}
_CAPTURE_FIELDS = frozenset(
    {
        "source",
        "model",
        "raw_json",
        "query_json",
        "raw_sha256",
        "query_sha256",
        "raw_row_count",
        "raw_scope",
        "schema_version",
    }
)


@dataclass(frozen=True, slots=True)
class NativeRawCapture:
    """One immutable extractor-output snapshot and its validated query context."""

    source: str
    model: str
    raw_json: str
    query_json: str
    raw_sha256: str
    query_sha256: str
    raw_row_count: int
    raw_scope: Literal["extract_data_output"]
    schema_version: Literal[1]


def _binding(source: object, model: object) -> type[QueryParams]:
    if type(source) is not str or type(model) is not str:
        raise ValueError("provider-model capture identity is not reviewed")
    query_model = _QUERY_MODELS.get((source, model))
    if query_model is None:
        raise ValueError("provider-model capture identity is not reviewed")
    return query_model


def _copy_json_value(value: object, *, depth: int, active: set[int]) -> JSONValue:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("provider-model raw payload exceeds the JSON depth limit")
    if value is None or type(value) in (str, int, bool):
        return value  # type: ignore[return-value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("provider-model raw payload contains a non-finite number")
        return value
    if type(value) is list:
        identity = id(value)
        if identity in active:
            raise ValueError("provider-model raw payload contains a cycle")
        active.add(identity)
        try:
            return [_copy_json_value(item, depth=depth + 1, active=active) for item in value]
        finally:
            active.remove(identity)
    if type(value) is dict:
        identity = id(value)
        if identity in active:
            raise ValueError("provider-model raw payload contains a cycle")
        active.add(identity)
        try:
            copied: dict[str, JSONValue] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("provider-model raw payload has a non-string object key")
                copied[key] = _copy_json_value(item, depth=depth + 1, active=active)
            return copied
        finally:
            active.remove(identity)
    raise ValueError("provider-model raw payload contains a non-JSON value")


def _canonical_json(value: object, *, maximum_bytes: int, error: str) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        payload = encoded.encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError, UnicodeError):
        raise ValueError(error) from None
    if len(payload) > maximum_bytes:
        raise ValueError(error)
    return encoded


def _reject_duplicate_object_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError("duplicate JSON object key")
        output[key] = value
    return output


def _reject_non_finite_constant(_value: str) -> object:
    raise ValueError("non-finite JSON number")


def _load_json(text: str, *, error: str) -> object:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_reject_non_finite_constant,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError(error) from None


def _validate_query_identity(query: QueryParams, *, source: str) -> None:
    if type(query.source) is not str or query.source != source:
        raise ValueError("provider-model query source is not explicitly bound")
    if type(query.market) is not str or query.market != "us":
        raise ValueError("provider-model query market is not bound to the reviewed domain")


def _validated_query_payload(
    source: str,
    model: str,
    params: object,
) -> tuple[str, type[QueryParams]]:
    query_model = _binding(source, model)
    if type(params) is not query_model:
        raise ValueError("provider-model query does not match its reviewed identity")
    try:
        if set(params.__dict__) != set(query_model.model_fields):
            raise ValueError
        if getattr(params, "__pydantic_extra__", None) not in (None, {}):
            raise ValueError
        python_payload = params.model_dump(mode="python")
        if type(python_payload) is not dict or set(python_payload) != set(query_model.model_fields):
            raise ValueError
        verified = query_model.model_validate(python_payload, strict=True)
        if type(verified) is not query_model:
            raise ValueError
        _validate_query_identity(verified, source=source)
        query_payload = verified.model_dump(mode="json")
        if type(query_payload) is not dict or set(query_payload) != set(query_model.model_fields):
            raise ValueError
        query_json = _canonical_json(
            query_payload,
            maximum_bytes=MAX_QUERY_CONTEXT_BYTES,
            error="provider-model query context is invalid or exceeds its size limit",
        )
    except (ValidationError, TypeError, ValueError, OverflowError, AttributeError):
        raise ValueError("provider-model query does not satisfy its reviewed contract") from None
    return query_json, query_model


def _canonical_raw_json(raw: object) -> tuple[str, int]:
    if type(raw) is not list:
        raise ValueError("provider-model raw result must be an exact list of objects")
    if len(raw) > MAX_RAW_ROWS:
        raise ValueError("provider-model raw result exceeds its row limit")
    active: set[int] = set()
    copied_rows: list[JSONValue] = []
    for row in raw:
        if type(row) is not dict:
            raise ValueError("provider-model raw result must contain exact objects")
        copied_rows.append(_copy_json_value(row, depth=1, active=active))
    raw_json = _canonical_json(
        copied_rows,
        maximum_bytes=MAX_RAW_PAYLOAD_BYTES,
        error="provider-model raw payload is invalid or exceeds its size limit",
    )
    return raw_json, len(copied_rows)


def make_provider_model_capture(
    *,
    source: str,
    model: str,
    raw: list[dict[str, object]],
    params: QueryParams,
) -> NativeRawCapture:
    """Build a bounded immutable capture from a validated query and raw output."""
    query_model = _binding(source, model)
    if type(params) is not query_model:
        raise ValueError("provider-model query does not match its reviewed identity")
    raw_json, row_count = _canonical_raw_json(raw)
    query_json, _ = _validated_query_payload(source, model, params)
    return NativeRawCapture(
        source=source,
        model=model,
        raw_json=raw_json,
        query_json=query_json,
        raw_sha256=hashlib.sha256(raw_json.encode("utf-8")).hexdigest(),
        query_sha256=hashlib.sha256(query_json.encode("utf-8")).hexdigest(),
        raw_row_count=row_count,
        raw_scope="extract_data_output",
        schema_version=1,
    )


def _validate_raw_json(raw_json: str) -> int:
    parsed = _load_json(raw_json, error="provider-model raw payload is invalid")
    if type(parsed) is not list or len(parsed) > MAX_RAW_ROWS:
        raise ValueError("provider-model raw payload has an invalid top-level shape")
    if any(type(row) is not dict for row in parsed):
        raise ValueError("provider-model raw payload must contain only objects")
    copied = _copy_json_value(parsed, depth=0, active=set())
    canonical = _canonical_json(
        copied,
        maximum_bytes=MAX_RAW_PAYLOAD_BYTES,
        error="provider-model raw payload is invalid or exceeds its size limit",
    )
    if canonical != raw_json:
        raise ValueError("provider-model raw payload is not canonical JSON")
    return len(parsed)


def _validate_query_json(query_json: str, *, source: str, model: str) -> None:
    query_model = _binding(source, model)
    parsed = _load_json(query_json, error="provider-model query context is invalid")
    if type(parsed) is not dict or set(parsed) != set(query_model.model_fields):
        raise ValueError("provider-model query context does not match its reviewed fields")
    try:
        query = query_model.model_validate_json(query_json, strict=True)
        if type(query) is not query_model:
            raise ValueError
        _validate_query_identity(query, source=source)
        payload = query.model_dump(mode="json")
        canonical = _canonical_json(
            payload,
            maximum_bytes=MAX_QUERY_CONTEXT_BYTES,
            error="provider-model query context is invalid or exceeds its size limit",
        )
    except (ValidationError, TypeError, ValueError, OverflowError, AttributeError):
        raise ValueError(
            "provider-model query context does not satisfy its reviewed contract"
        ) from None
    if canonical != query_json:
        raise ValueError("provider-model query context is not canonical JSON")


def validate_provider_model_capture(
    *,
    source: str,
    model: str,
    capture: NativeRawCapture,
) -> NativeRawCapture:
    """Revalidate every capture field before it can reach a storage writer."""
    _binding(source, model)
    if type(capture) is not NativeRawCapture:
        raise ValueError("provider-model capture has an unknown type")
    if set(capture.__dataclass_fields__) != _CAPTURE_FIELDS:
        raise ValueError("provider-model capture fields differ from the reviewed contract")
    if type(capture.source) is not str or capture.source != source:
        raise ValueError("provider-model capture source does not match the writer")
    if type(capture.model) is not str or capture.model != model:
        raise ValueError("provider-model capture model does not match the writer")
    if type(capture.raw_scope) is not str or capture.raw_scope != "extract_data_output":
        raise ValueError("provider-model capture raw scope is invalid")
    if type(capture.schema_version) is not int or capture.schema_version != 1:
        raise ValueError("provider-model capture schema version is invalid")
    if type(capture.raw_row_count) is not int or not 0 <= capture.raw_row_count <= MAX_RAW_ROWS:
        raise ValueError("provider-model capture row count is invalid")
    if any(
        type(value) is not str
        for value in (
            capture.raw_json,
            capture.query_json,
            capture.raw_sha256,
            capture.query_sha256,
        )
    ):
        raise ValueError("provider-model capture text fields are invalid")
    try:
        raw_bytes = capture.raw_json.encode("utf-8")
        query_bytes = capture.query_json.encode("utf-8")
    except UnicodeError:
        raise ValueError("provider-model capture contains invalid Unicode text") from None
    if len(raw_bytes) > MAX_RAW_PAYLOAD_BYTES or len(query_bytes) > MAX_QUERY_CONTEXT_BYTES:
        raise ValueError("provider-model capture exceeds its payload size limit")
    if hashlib.sha256(raw_bytes).hexdigest() != capture.raw_sha256:
        raise ValueError("provider-model capture raw digest does not match its payload")
    if hashlib.sha256(query_bytes).hexdigest() != capture.query_sha256:
        raise ValueError("provider-model capture query digest does not match its context")
    if _validate_raw_json(capture.raw_json) != capture.raw_row_count:
        raise ValueError("provider-model capture row count does not match its payload")
    _validate_query_json(capture.query_json, source=source, model=model)
    return capture


__all__ = [
    "MAX_JSON_DEPTH",
    "MAX_QUERY_CONTEXT_BYTES",
    "MAX_RAW_PAYLOAD_BYTES",
    "MAX_RAW_ROWS",
    "NativeRawCapture",
    "make_provider_model_capture",
    "validate_provider_model_capture",
]
