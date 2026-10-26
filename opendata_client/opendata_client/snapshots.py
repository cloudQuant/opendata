"""Strict synchronous client for reproducible, versioned OpenData snapshots.

This module only depends on the public HTTP contract. A snapshot pins one
dataset publication, schema, native units, half-open date window and identity
semantics before records are paged. Missing or drifting metadata is an error.
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from types import MappingProxyType
from typing import TypeAlias
from urllib.parse import quote

import httpx

JsonValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | list["JsonValue"]
    | tuple["JsonValue", ...]
    | Mapping[str, "JsonValue"]
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_DEADLINE = 120.0
DEFAULT_MAX_PAGES = 1_000
DEFAULT_MAX_ROWS = 200_000
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
MAX_PAGE_SIZE = 1_000
MAX_HARD_PAGES = 10_000
MAX_HARD_ROWS = 1_000_000
MAX_HARD_BYTES = 256 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RETRIES = 2
MAX_RETRY_AFTER = 1.0
_CHUNK_SIZE = 64 * 1024
_MAX_JSON_DEPTH = 64
_MAX_CURSOR_LENGTH = 8_192

_STORAGE_ORDER_V1: tuple[JsonValue, ...] = (
    {
        "field": "business_date",
        "direction": "asc",
        "nulls": "last",
        "comparison": "native",
    },
    {"field": "business_key", "direction": "asc", "comparison": "binary_lexicographic"},
)
_TEMPORAL_FIELDS_V1: Mapping[str, JsonValue] = {
    "business_date": "business-date bucket; null ordering last (route rejects null time values)",
    "observed_at": "source observation time",
    "source_published_at": "source publication time",
    "available_at": "platform availability time",
    "event_at": "source event time",
}


class SnapshotClientError(RuntimeError):
    """Base error for transport, protocol, scope, and completeness failures."""


class SnapshotProtocolError(SnapshotClientError):
    """The server response did not satisfy the versioned snapshot contract."""


class SnapshotScopeError(SnapshotProtocolError):
    """A response changed dataset, publication, window, or snapshot identity."""


class SnapshotLimitError(SnapshotClientError):
    """A configured page, row, or byte limit prevents a complete read."""


class SnapshotDeadlineError(SnapshotClientError):
    """The total operation deadline expired."""


class SnapshotHTTPError(SnapshotClientError):
    """An HTTP error occurred; response bodies are intentionally not exposed."""

    def __init__(self, status_code: int) -> None:
        """Build a stable error that exposes only the HTTP status."""
        self.status_code = status_code
        super().__init__(f"snapshot API returned HTTP {status_code}")


class SnapshotAuthenticationError(SnapshotHTTPError):
    """The API key was rejected (HTTP 401)."""


class SnapshotPermissionError(SnapshotHTTPError):
    """The API key lacks scope for the requested snapshot (HTTP 403)."""


class SnapshotNotFoundError(SnapshotHTTPError):
    """The scoped dataset or snapshot was not found (HTTP 404)."""


class SnapshotExpiredError(SnapshotHTTPError):
    """The snapshot cursor or snapshot itself expired (HTTP 410)."""


class SnapshotInvalidRequestError(SnapshotHTTPError):
    """The snapshot request was rejected as invalid (HTTP 400/422)."""


class SnapshotTooLargeError(SnapshotHTTPError):
    """The API rejected a response that exceeds its limit (HTTP 413)."""


class SnapshotRateLimitError(SnapshotHTTPError):
    """The API rate limit remained active after bounded GET retries (HTTP 429)."""


class SnapshotUnavailableError(SnapshotHTTPError):
    """The API or requested schema is not available (HTTP 503)."""


@dataclass(frozen=True)
class SnapshotWindow:
    """Inclusive start and exclusive end of one snapshot's date scope."""

    start: date
    end: date


@dataclass(frozen=True)
class Snapshot:
    """Immutable publication and schema identity returned by OpenData."""

    snapshot_id: str
    dataset_id: str
    publication_id: str
    sequence: int
    schema_version: str
    semantic_version: str
    source: Mapping[str, JsonValue]
    schema: tuple[Mapping[str, JsonValue], ...]
    quality: Mapping[str, JsonValue]
    field_units: Mapping[str, JsonValue]
    field_currencies: Mapping[str, JsonValue]
    field_adjustments: Mapping[str, JsonValue]
    currency: str | None
    adjustment_basis: str
    asset_class: str
    frequency: str
    record_kind: str
    business_key_fields: tuple[str, ...]
    ordering_fields: tuple[str, ...]
    filter_fields: tuple[str, ...]
    time_field: str | None
    temporal_semantics: str
    calendar_id: str | None
    timezone: str | None
    pit_policy: str
    source_available_at_field: str | None
    identity_key_encoding: str
    storage_order: tuple[Mapping[str, JsonValue], ...]
    contract_sha256: str
    window: SnapshotWindow
    page_size: int
    as_of: str
    created_at: str
    expires_at: str
    record_count: int
    selection_sha256: str
    content_sha256: str
    identity_semantics: Mapping[str, JsonValue]


@dataclass(frozen=True)
class SnapshotPage:
    """One bounded record page tied to a single immutable snapshot."""

    snapshot: Snapshot
    rows: tuple[dict[str, JsonValue], ...]
    count: int
    next_cursor: str | None
    has_more: bool
    response_bytes: int


def _positive_number(value: object, name: str, maximum: float) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite positive number")
    if number <= 0 or number > maximum:
        raise ValueError(f"{name} must be greater than 0 and no greater than {maximum}")
    return number


def _bounded_int(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _validate_dataset_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or value in {".", ".."}
        or any(char in value for char in "/\\?#")
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError("dataset_id must be a non-empty path-safe identifier")
    return value


def _validate_opaque_id(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    return value


def _parse_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}") from None
    if parsed.isoformat() != value:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    return parsed


def _parse_requested_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must use YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name} must use YYYY-MM-DD") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must use YYYY-MM-DD")
    return parsed


def _parse_timestamp(value: object, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON numeric constant: {value}")


def _json_depth_is_bounded(value: JsonValue, max_depth: int = _MAX_JSON_DEPTH) -> bool:
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > max_depth:
            return False
        if isinstance(current, Mapping):
            pending.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend((child, depth + 1) for child in current)
    return True


def _json_numbers_are_finite(value: JsonValue) -> bool:
    pending: list[JsonValue] = [value]
    while pending:
        current = pending.pop()
        if type(current) is float and not math.isfinite(current):
            return False
        if isinstance(current, Mapping):
            pending.extend(current.values())
        elif isinstance(current, (list, tuple)):
            pending.extend(current)
    return True


def _canonical_json(value: Mapping[str, JsonValue], name: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a JSON object")
    try:
        encoded = json.dumps(
            _thaw_mapping(value),
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        decoded = json.loads(encoded, parse_constant=_reject_json_constant)
    except (TypeError, ValueError, RecursionError):
        raise ValueError(f"{name} must contain finite JSON values") from None
    if not isinstance(decoded, dict) or not _json_depth_is_bounded(decoded):
        raise ValueError(f"{name} exceeds the supported JSON depth")
    return encoded


def _freeze(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze(child) for child in value)
    return value


def _freeze_mapping(value: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    return MappingProxyType({key: _freeze(child) for key, child in value.items()})


def _thaw(value: JsonValue) -> JsonValue:
    if isinstance(value, Mapping):
        return _thaw_mapping(value)
    if isinstance(value, tuple):
        return [_thaw(child) for child in value]
    return value


def _thaw_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    return {key: _thaw(child) for key, child in value.items()}


def _semantics_json(value: Mapping[str, JsonValue], name: str) -> str:
    encoded = _canonical_json(value, name)
    try:
        decoded = json.loads(encoded)
    except (ValueError, TypeError):
        raise ValueError(f"{name} is invalid") from None
    required = {
        "version",
        "identity_key_encoding",
        "revision",
        "temporal_fields",
        "business_key_fields",
        "ordering_fields",
        "temporal_semantics",
        "time_field",
        "storage_order",
    }
    if not isinstance(decoded, dict) or set(decoded) != required:
        raise ValueError(f"{name} must include the complete supported v1 identity contract")
    business_key = decoded["business_key_fields"]
    ordering = decoded["ordering_fields"]
    storage_order = decoded["storage_order"]
    temporal_fields = decoded["temporal_fields"]
    time_field = decoded["time_field"]
    if (
        type(decoded["version"]) is not int
        or decoded["version"] != 1
        or not isinstance(business_key, list)
        or not business_key
        or any(not isinstance(field, str) or not field for field in business_key)
        or len(set(business_key)) != len(business_key)
        or not isinstance(ordering, list)
        or not ordering
        or any(not isinstance(field, str) or not field for field in ordering)
        or len(set(ordering)) != len(ordering)
        or decoded["identity_key_encoding"] != "canonical-json-array-v1"
        or decoded["revision"]
        != "latest non-deleted revision per business_key at the pinned publication sequence"
        or not isinstance(decoded["temporal_semantics"], str)
        or not decoded["temporal_semantics"]
        or (time_field is not None and (not isinstance(time_field, str) or not time_field))
        or not isinstance(storage_order, list)
        or _canonical_json({"value": storage_order}, "identity storage order")
        != _canonical_json({"value": _STORAGE_ORDER_V1}, "storage order contract")
        or not isinstance(temporal_fields, dict)
        or _canonical_json({"value": temporal_fields}, "identity temporal fields")
        != _canonical_json({"value": _TEMPORAL_FIELDS_V1}, "temporal field contract")
    ):
        raise ValueError(f"{name} does not match the supported v1 identity contract")
    return encoded


def _sha256(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    return value


def _string_value(value: object, name: str, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    return value


def _string_field_map(value: object, name: str, *, allow_empty: bool) -> dict[str, JsonValue]:
    if not isinstance(value, Mapping) or (not value and not allow_empty):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    result: dict[str, JsonValue] = {}
    for key, field_value in value.items():
        if (
            not isinstance(key, str)
            or not key
            or not isinstance(field_value, str)
            or not field_value
        ):
            raise SnapshotProtocolError(f"snapshot response has invalid {name}")
        result[key] = field_value
    return result


def _parse_string_list(value: object, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    if any(not isinstance(item, str) or not item for item in value):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    items = tuple(value)
    if len(set(items)) != len(items):
        raise SnapshotProtocolError(f"snapshot response has duplicate {name} entries")
    return items


def _parse_storage_order(value: object, name: str) -> tuple[Mapping[str, JsonValue], ...]:
    if not isinstance(value, list) or any(not isinstance(term, Mapping) for term in value):
        raise SnapshotProtocolError(f"snapshot response has invalid {name}")
    try:
        encoded = _canonical_json({"value": value}, name)
        expected = _canonical_json({"value": _STORAGE_ORDER_V1}, "storage order contract")
    except ValueError:
        raise SnapshotProtocolError(f"snapshot response has invalid {name}") from None
    if encoded != expected:
        raise SnapshotProtocolError(f"snapshot response has unsupported {name}")
    return tuple(_freeze_mapping(dict(term)) for term in value)


def _parse_snapshot(payload: object) -> Snapshot:
    if not isinstance(payload, Mapping):
        raise SnapshotProtocolError("snapshot API returned an invalid response shape")
    required = {
        "snapshot_id",
        "dataset_id",
        "publication_id",
        "sequence",
        "schema_version",
        "semantic_version",
        "source",
        "schema",
        "quality",
        "field_units",
        "field_currencies",
        "field_adjustments",
        "currency",
        "adjustment_basis",
        "asset_class",
        "frequency",
        "record_kind",
        "business_key_fields",
        "ordering_fields",
        "storage_order",
        "identity_key_encoding",
        "filter_fields",
        "time_field",
        "temporal_semantics",
        "timezone",
        "pit_policy",
        "source_available_at_field",
        "calendar_id",
        "contract_sha256",
        "window",
        "page_size",
        "as_of",
        "created_at",
        "expires_at",
        "record_count",
        "selection_sha256",
        "content_sha256",
        "identity_semantics",
    }
    if not required.issubset(payload):
        raise SnapshotProtocolError("snapshot API omitted required snapshot metadata")

    snapshot_id = _validate_opaque_id(payload["snapshot_id"], "snapshot_id")
    try:
        dataset_id = _validate_dataset_id(payload["dataset_id"])
    except ValueError:
        raise SnapshotProtocolError("snapshot response has invalid dataset_id") from None
    publication_id = _validate_opaque_id(payload["publication_id"], "publication_id")
    sequence = payload["sequence"]
    if type(sequence) is not int or sequence < 1:
        raise SnapshotProtocolError("snapshot response has invalid sequence")
    schema_version = payload["schema_version"]
    if not isinstance(schema_version, str) or not schema_version:
        raise SnapshotProtocolError("snapshot response has invalid schema_version")
    semantic_version = payload["semantic_version"]
    if not isinstance(semantic_version, str) or not semantic_version:
        raise SnapshotProtocolError("snapshot response has invalid semantic_version")

    source = payload["source"]
    if (
        not isinstance(source, Mapping)
        or not isinstance(source.get("source_id"), str)
        or not source["source_id"]
        or not isinstance(source.get("source"), str)
        or not source["source"]
    ):
        raise SnapshotProtocolError("snapshot response has invalid source identity")
    try:
        field_units = _string_field_map(payload["field_units"], "field_units", allow_empty=True)
        field_currencies = _string_field_map(
            payload["field_currencies"], "field_currencies", allow_empty=True
        )
        field_adjustments = _string_field_map(
            payload["field_adjustments"], "field_adjustments", allow_empty=True
        )
        source_json = _canonical_json(source, "snapshot source")
    except ValueError:
        raise SnapshotProtocolError("snapshot response has invalid metadata values") from None
    source_copy = json.loads(source_json)

    raw_schema = payload["schema"]
    if not isinstance(raw_schema, list):
        raise SnapshotProtocolError("snapshot response has invalid schema")
    schema_fields: list[Mapping[str, JsonValue]] = []
    schema_names: set[str] = set()
    for field in raw_schema:
        if (
            not isinstance(field, Mapping)
            or set(field) != {"name", "data_type", "nullable"}
            or not isinstance(field["name"], str)
            or not field["name"]
            or not isinstance(field["data_type"], str)
            or field["data_type"]
            not in {"string", "integer", "number", "boolean", "date", "datetime", "object", "array"}
            or type(field["nullable"]) is not bool
            or field["name"] in schema_names
        ):
            raise SnapshotProtocolError("snapshot response has invalid schema field")
        schema_names.add(field["name"])
        schema_fields.append(_freeze_mapping(dict(field)))

    raw_quality = payload["quality"]
    if not isinstance(raw_quality, Mapping) or set(raw_quality) != {
        "status",
        "report_id",
        "issue_count",
    }:
        raise SnapshotProtocolError("snapshot response has invalid quality metadata")
    quality_status = raw_quality.get("status")
    report_id = raw_quality.get("report_id")
    issue_count = raw_quality.get("issue_count")
    if (
        not isinstance(quality_status, str)
        or quality_status not in {"passed", "warning", "rejected", "unknown"}
        or (report_id is not None and (not isinstance(report_id, str) or not report_id))
        or not isinstance(issue_count, int)
        or isinstance(issue_count, bool)
        or issue_count < 0
    ):
        raise SnapshotProtocolError("snapshot response has invalid quality metadata")
    quality_copy = json.loads(_canonical_json(raw_quality, "snapshot quality"))

    currency = _string_value(payload["currency"], "currency", nullable=True)
    adjustment_basis = payload["adjustment_basis"]
    if not isinstance(adjustment_basis, str) or not adjustment_basis:
        raise SnapshotProtocolError("snapshot response has invalid adjustment_basis")
    asset_class = _string_value(payload["asset_class"], "asset_class") or ""
    frequency = _string_value(payload["frequency"], "frequency") or ""
    record_kind = _string_value(payload["record_kind"], "record_kind") or ""
    temporal_semantics = _string_value(payload["temporal_semantics"], "temporal_semantics") or ""
    pit_policy = _string_value(payload["pit_policy"], "pit_policy") or ""
    time_field = _string_value(payload["time_field"], "time_field", nullable=True)
    timezone_name = _string_value(payload["timezone"], "timezone") or ""
    source_available_at_field = _string_value(
        payload["source_available_at_field"], "source_available_at_field", nullable=True
    )
    calendar_id = _string_value(payload["calendar_id"], "calendar_id", nullable=True)
    business_key_fields = _parse_string_list(payload["business_key_fields"], "business_key_fields")
    ordering_fields = _parse_string_list(payload["ordering_fields"], "ordering_fields")
    storage_order = _parse_storage_order(payload["storage_order"], "storage_order")
    identity_key_encoding = (
        _string_value(payload["identity_key_encoding"], "identity_key_encoding") or ""
    )
    if identity_key_encoding != "canonical-json-array-v1":
        raise SnapshotProtocolError("snapshot response has unsupported identity_key_encoding")
    filter_fields = _parse_string_list(payload["filter_fields"], "filter_fields", allow_empty=True)
    if (
        not set(business_key_fields).issubset(schema_names)
        or not set(ordering_fields).issubset(schema_names)
        or not set(filter_fields).issubset(schema_names)
        or not set(field_units).issubset(schema_names)
        or not set(field_currencies).issubset(schema_names)
        or not set(field_adjustments).issubset(schema_names)
    ):
        raise SnapshotProtocolError("snapshot metadata references unknown schema fields")
    if time_field is not None and time_field not in schema_names:
        raise SnapshotProtocolError("snapshot time_field is absent from its schema")

    raw_window = payload["window"]
    if not isinstance(raw_window, Mapping) or raw_window.get("end_exclusive") is not True:
        raise SnapshotProtocolError("snapshot window must declare a half-open end")
    start = _parse_date(raw_window.get("start"), "window.start")
    end = _parse_date(raw_window.get("end"), "window.end")
    if start >= end:
        raise SnapshotProtocolError("snapshot window must have start before end")

    page_size = payload["page_size"]
    if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
        raise SnapshotProtocolError("snapshot response has invalid page_size")
    as_of = _parse_timestamp(payload["as_of"], "as_of")
    created_at = _parse_timestamp(payload["created_at"], "created_at")
    expires_at = _parse_timestamp(payload["expires_at"], "expires_at")
    record_count = payload["record_count"]
    if type(record_count) is not int or record_count < 0:
        raise SnapshotProtocolError("snapshot response has invalid record_count")

    raw_semantics = payload["identity_semantics"]
    if not isinstance(raw_semantics, Mapping):
        raise SnapshotProtocolError("snapshot response omitted identity semantics")
    try:
        semantics_encoded = _semantics_json(raw_semantics, "identity semantics")
    except ValueError:
        raise SnapshotProtocolError("snapshot response has invalid identity semantics") from None
    semantics_copy = json.loads(semantics_encoded)
    if (
        semantics_copy["business_key_fields"] != list(business_key_fields)
        or semantics_copy["ordering_fields"] != list(ordering_fields)
        or semantics_copy["time_field"] != time_field
        or semantics_copy["temporal_semantics"] != temporal_semantics
        or semantics_copy["identity_key_encoding"] != identity_key_encoding
        or _canonical_json({"value": semantics_copy["storage_order"]}, "identity storage order")
        != _canonical_json({"value": _thaw(storage_order)}, "snapshot storage order")
    ):
        raise SnapshotProtocolError(
            "snapshot identity semantics disagree with its dataset contract"
        )

    return Snapshot(
        snapshot_id=snapshot_id,
        dataset_id=dataset_id,
        publication_id=publication_id,
        sequence=sequence,
        schema_version=schema_version,
        semantic_version=semantic_version,
        source=_freeze_mapping(source_copy),
        schema=tuple(schema_fields),
        quality=_freeze_mapping(quality_copy),
        field_units=_freeze_mapping(field_units),
        field_currencies=_freeze_mapping(field_currencies),
        field_adjustments=_freeze_mapping(field_adjustments),
        currency=currency,
        adjustment_basis=adjustment_basis,
        asset_class=asset_class,
        frequency=frequency,
        record_kind=record_kind,
        business_key_fields=business_key_fields,
        ordering_fields=ordering_fields,
        filter_fields=filter_fields,
        time_field=time_field,
        temporal_semantics=temporal_semantics,
        calendar_id=calendar_id,
        timezone=timezone_name,
        pit_policy=pit_policy,
        source_available_at_field=source_available_at_field,
        identity_key_encoding=identity_key_encoding,
        storage_order=storage_order,
        contract_sha256=_sha256(payload["contract_sha256"], "contract_sha256"),
        window=SnapshotWindow(start=start, end=end),
        page_size=page_size,
        as_of=as_of or "",
        created_at=created_at or "",
        expires_at=expires_at or "",
        record_count=record_count,
        selection_sha256=_sha256(payload["selection_sha256"], "selection_sha256"),
        content_sha256=_sha256(payload["content_sha256"], "content_sha256"),
        identity_semantics=_freeze_mapping(semantics_copy),
    )


def _http_error(status_code: int) -> SnapshotHTTPError:
    error_class: type[SnapshotHTTPError]
    if status_code == 401:
        error_class = SnapshotAuthenticationError
    elif status_code == 403:
        error_class = SnapshotPermissionError
    elif status_code == 404:
        error_class = SnapshotNotFoundError
    elif status_code == 410:
        error_class = SnapshotExpiredError
    elif status_code in (400, 422):
        error_class = SnapshotInvalidRequestError
    elif status_code == 413:
        error_class = SnapshotTooLargeError
    elif status_code == 429:
        error_class = SnapshotRateLimitError
    elif status_code == 503:
        error_class = SnapshotUnavailableError
    else:
        error_class = SnapshotHTTPError
    return error_class(status_code)


def _retry_delay(value: str | None, attempt: int, maximum: float) -> float | None:
    if value is None:
        delay = 0.1 * (2**attempt)
        return delay if delay < maximum else maximum
    stripped = value.strip()
    try:
        delay = float(stripped)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(stripped)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            # HTTP dates have whole-second precision. A date at or before the
            # current instant means retry immediately, including normal clock
            # skew and rounding; negative numeric delay values remain invalid.
            delay = max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(delay) or delay < 0 or delay > maximum:
        return None
    return delay


def _timestamp_key(value: str | None, business_key: str) -> tuple[bool, str, str]:
    return (value is None, value or "", business_key)


def _validate_row(
    row: JsonValue,
    snapshot: Snapshot,
) -> tuple[dict[str, JsonValue], str, tuple[bool, str, str]]:
    if not isinstance(row, dict):
        raise SnapshotProtocolError("records page contains a non-object row")
    required = {
        "dataset_id",
        "business_key",
        "revision",
        "publication_id",
        "publication_sequence",
        "business_date",
        "event_at",
        "observed_at",
        "source_published_at",
        "available_at",
        "payload",
        "record_hash",
    }
    if not required.issubset(row):
        raise SnapshotProtocolError("record omitted required identity or temporal fields")
    key = row["business_key"]
    revision = row["revision"]
    if not isinstance(key, str) or not key or len(key) > 2_048:
        raise SnapshotProtocolError("record has invalid business_key")
    if row["dataset_id"] != snapshot.dataset_id:
        raise SnapshotScopeError("record changed dataset or pinned publication")
    publication_id = row["publication_id"]
    publication_sequence = row["publication_sequence"]
    if (
        not isinstance(publication_id, str)
        or type(publication_sequence) is not int
        or publication_sequence < 1
    ):
        raise SnapshotProtocolError("record has invalid origin publication identity")
    if publication_sequence > snapshot.sequence:
        raise SnapshotScopeError("record origin publication is newer than the pinned snapshot")
    if publication_sequence == snapshot.sequence:
        if publication_id != snapshot.publication_id:
            raise SnapshotScopeError("record changed dataset or pinned publication")
    elif re.fullmatch(r"[0-9a-f]{32}", publication_id) is None:
        raise SnapshotProtocolError("record has invalid origin publication identity")
    if type(revision) is not int or revision < 1:
        raise SnapshotProtocolError("record has invalid revision")

    business_date_value = row["business_date"]
    parsed_date = _parse_date(business_date_value, "record.business_date")
    if not snapshot.window.start <= parsed_date < snapshot.window.end:
        raise SnapshotScopeError("record business_date is outside the pinned window")
    business_date = parsed_date.isoformat()
    for field in ("event_at", "observed_at", "source_published_at", "available_at"):
        _parse_timestamp(row[field], f"record.{field}", nullable=True)

    payload = row["payload"]
    if (
        not isinstance(payload, dict)
        or not _json_depth_is_bounded(row)
        or not _json_numbers_are_finite(row)
    ):
        raise SnapshotProtocolError("record payload is not a bounded JSON object")
    try:
        key_values = json.loads(
            key,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (ValueError, TypeError, RecursionError):
        raise SnapshotProtocolError("record business_key is not canonical JSON") from None
    if (
        not isinstance(key_values, list)
        or len(key_values) != len(snapshot.business_key_fields)
        or not _json_numbers_are_finite(key_values)
        or any(
            not (item is None or isinstance(item, (str, int, float, bool))) for item in key_values
        )
        or json.dumps(key_values, ensure_ascii=False, allow_nan=False, separators=(",", ":")) != key
    ):
        raise SnapshotProtocolError("record business_key does not match its declared encoding")
    for field_name, key_value in zip(snapshot.business_key_fields, key_values, strict=True):
        payload_value = payload.get(field_name)
        if type(payload_value) is not type(key_value) or payload_value != key_value:
            raise SnapshotProtocolError("record business_key disagrees with its payload fields")
    if snapshot.time_field is not None and payload.get(snapshot.time_field) != business_date:
        raise SnapshotProtocolError("record time field disagrees with business_date")
    record_hash = row["record_hash"]
    if not isinstance(record_hash, str) or re.fullmatch(r"[0-9a-f]{64}", record_hash) is None:
        raise SnapshotProtocolError("record has invalid record_hash")
    return row, key, _timestamp_key(business_date, key)


class SnapshotClient:
    """Synchronous SDK for immutable OpenData v2 snapshots.

    A client pins a dataset publication and schema before reading. All read
    methods require callers to confirm the supported identity semantics so a
    provider's business key or temporal rules cannot silently change.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT,
        deadline: float = DEFAULT_DEADLINE,
        max_pages: int = DEFAULT_MAX_PAGES,
        max_rows: int = DEFAULT_MAX_ROWS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Create a bounded v2 client.

        Args:
            base_url: OpenData server root, for example ``https://data.example``.
            api_key: Scoped OpenData API key sent as ``X-API-Key``.
            timeout: Maximum duration of one network operation in seconds.
            deadline: Total duration allowed for each public operation in seconds.
            max_pages: Maximum pages an iterator may request.
            max_rows: Maximum rows an iterator may yield.
            max_bytes: Maximum decoded JSON bytes per public read operation.
            transport: Optional httpx transport, usually a mock in tests.
            http_client: Optional caller-owned httpx client.

        Raises:
            ValueError: If a URL, API key, or limit is invalid.
        """
        if not isinstance(base_url, str) or not base_url:
            raise ValueError("base_url must be a non-empty URL")
        parsed = httpx.URL(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.host
            or parsed.userinfo
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be an HTTP(S) URL without credentials or query parts")
        if not isinstance(api_key, str) or not api_key or any(ord(char) < 32 for char in api_key):
            raise ValueError("api_key must be a non-empty credential")
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = _positive_number(timeout, "timeout", 600.0)
        self.deadline = _positive_number(deadline, "deadline", 3_600.0)
        self.max_pages = _bounded_int(max_pages, "max_pages", MAX_HARD_PAGES)
        self.max_rows = _bounded_int(max_rows, "max_rows", MAX_HARD_ROWS)
        self.max_bytes = _bounded_int(max_bytes, "max_bytes", MAX_HARD_BYTES)
        if http_client is not None and transport is not None:
            raise ValueError("pass either transport or http_client, not both")
        self._owns_client = http_client is None
        self._client = http_client or httpx.Client(transport=transport)

    def close(self) -> None:
        """Close the underlying httpx client when this object owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SnapshotClient:
        """Return this client for a context-managed use block."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying client at the end of a context-managed use block."""
        self.close()

    def _new_deadline(self) -> float:
        return time.monotonic() + self.deadline

    @staticmethod
    def _check_deadline(end_time: float) -> float:
        remaining = end_time - time.monotonic()
        if remaining <= 0:
            raise SnapshotDeadlineError("snapshot operation exceeded its deadline")
        return remaining

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, JsonValue] | None = None,
        end_time: float,
        byte_limit: int,
    ) -> tuple[dict[str, JsonValue], int]:
        url = f"{self.base_url}/api/v2{path}"
        headers = {"X-API-Key": self._api_key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            content = (
                json.dumps(
                    dict(body),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                if body is not None
                else None
            )
        except (TypeError, ValueError):
            raise ValueError("request body must contain finite JSON values") from None

        attempts = 0
        while True:
            remaining = self._check_deadline(end_time)
            timeout = httpx.Timeout(min(self.timeout, remaining))
            retry_delay: float | None = None
            try:
                with self._client.stream(
                    method,
                    url,
                    params=params,
                    content=content,
                    headers=headers,
                    timeout=timeout,
                ) as response:
                    self._check_deadline(end_time)
                    if (
                        response.status_code in {429, 503}
                        and method == "GET"
                        and attempts < MAX_RETRIES
                    ):
                        retry_delay = _retry_delay(
                            response.headers.get("retry-after"), attempts, MAX_RETRY_AFTER
                        )
                    if retry_delay is not None:
                        pass
                    elif not response.is_success:
                        raise _http_error(response.status_code)
                    else:
                        content_type = response.headers.get("content-type", "")
                        media_type = content_type.split(";", 1)[0].strip().casefold()
                        if media_type != "application/json" and not media_type.endswith("+json"):
                            raise SnapshotProtocolError("snapshot API returned a non-JSON response")
                        content_encoding = response.headers.get("content-encoding", "identity")
                        declared_length = response.headers.get("content-length")
                        if (
                            content_encoding.casefold() in {"", "identity"}
                            and declared_length is not None
                            and declared_length.isascii()
                            and declared_length.isdecimal()
                            and int(declared_length) > min(MAX_RESPONSE_BYTES, byte_limit)
                        ):
                            raise SnapshotLimitError("snapshot response exceeds the byte limit")
                        body_bytes = bytearray()
                        ceiling = min(MAX_RESPONSE_BYTES, byte_limit)
                        for chunk in response.iter_bytes(chunk_size=_CHUNK_SIZE):
                            self._check_deadline(end_time)
                            if len(body_bytes) + len(chunk) > ceiling:
                                raise SnapshotLimitError("snapshot response exceeds the byte limit")
                            body_bytes.extend(chunk)
                        self._check_deadline(end_time)
                        try:
                            payload = json.loads(
                                body_bytes,
                                object_pairs_hook=_object_without_duplicate_keys,
                                parse_constant=_reject_json_constant,
                            )
                        except (ValueError, UnicodeDecodeError, RecursionError):
                            raise SnapshotProtocolError(
                                "snapshot API returned invalid JSON"
                            ) from None
                        if (
                            not isinstance(payload, dict)
                            or not _json_depth_is_bounded(payload)
                            or not _json_numbers_are_finite(payload)
                        ):
                            raise SnapshotProtocolError(
                                "snapshot API returned an invalid JSON object"
                            )
                        return payload, len(body_bytes)
            except (SnapshotClientError, ValueError):
                raise
            except httpx.HTTPError:
                raise SnapshotClientError("snapshot API request failed") from None

            if retry_delay is None:
                raise _http_error(429 if response.status_code == 429 else 503)
            remaining = self._check_deadline(end_time)
            if retry_delay > remaining:
                raise SnapshotDeadlineError("snapshot retry would exceed its deadline")
            if retry_delay:
                time.sleep(retry_delay)
            attempts += 1

    @staticmethod
    def _validate_expected_semantics(expected: Mapping[str, JsonValue]) -> str:
        try:
            return _semantics_json(expected, "expected_identity_semantics")
        except ValueError as exc:
            raise ValueError(str(exc)) from None

    def create_snapshot(
        self,
        dataset_id: str,
        *,
        start_date: str,
        end_date: str,
        expected_identity_semantics: Mapping[str, JsonValue],
        page_size: int = 100,
        publication_id: str | None = None,
    ) -> Snapshot:
        """Create an immutable snapshot for one publication and half-open window.

        The POST is never retried automatically because snapshot creation has
        no idempotency-key contract. An unknown network outcome therefore
        remains ambiguous and must be resolved by the caller.

        Args:
            dataset_id: Exact registered dataset identity.
            start_date: Inclusive ISO date at the start of the requested window.
            end_date: Exclusive ISO date at the end of the requested window.
            expected_identity_semantics: Exact v1 record identity contract expected.
            page_size: Page size pinned into the snapshot.
            publication_id: Optional exact immutable publication to select.

        Returns:
            A frozen snapshot descriptor suitable for ``list_records``.

        Raises:
            ValueError: If request arguments or expected semantics are invalid.
            SnapshotClientError: If creation fails or metadata is incomplete.
        """
        dataset_id = _validate_dataset_id(dataset_id)
        start = _parse_requested_date(start_date, "start_date")
        end = _parse_requested_date(end_date, "end_date")
        if start >= end:
            raise ValueError("start_date must be earlier than the exclusive end_date")
        expected = self._validate_expected_semantics(expected_identity_semantics)
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError(f"page_size must be an integer between 1 and {MAX_PAGE_SIZE}")
        if publication_id is not None:
            publication_id = _validate_opaque_id(publication_id, "publication_id")

        request_body: dict[str, JsonValue] = {
            "dataset_id": dataset_id,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "page_size": page_size,
        }
        if publication_id is not None:
            request_body["publication_id"] = publication_id
        payload, _ = self._request_json(
            "POST",
            "/snapshots",
            body=request_body,
            end_time=self._new_deadline(),
            byte_limit=self.max_bytes,
        )
        snapshot = _parse_snapshot(payload)
        if (
            snapshot.dataset_id != dataset_id
            or snapshot.window.start != start
            or snapshot.window.end != end
            or snapshot.page_size != page_size
            or (publication_id is not None and snapshot.publication_id != publication_id)
            or _canonical_json(snapshot.identity_semantics, "identity semantics") != expected
        ):
            raise SnapshotScopeError("created snapshot does not match the requested scope")
        return snapshot

    def get_snapshot(
        self,
        snapshot_id: str,
        *,
        dataset_id: str,
        expected_identity_semantics: Mapping[str, JsonValue],
    ) -> Snapshot:
        """Fetch and validate immutable snapshot metadata by its exact ID."""
        _validate_opaque_id(snapshot_id, "snapshot_id")
        dataset_id = _validate_dataset_id(dataset_id)
        expected = self._validate_expected_semantics(expected_identity_semantics)
        payload, _ = self._request_json(
            "GET",
            f"/snapshots/{quote(snapshot_id, safe='')}",
            params={"dataset_id": dataset_id},
            end_time=self._new_deadline(),
            byte_limit=self.max_bytes,
        )
        snapshot = _parse_snapshot(payload)
        if snapshot.snapshot_id != snapshot_id or snapshot.dataset_id != dataset_id:
            raise SnapshotScopeError("snapshot metadata returned a different snapshot scope")
        if _canonical_json(snapshot.identity_semantics, "identity semantics") != expected:
            raise SnapshotScopeError("snapshot identity semantics do not match the caller")
        return snapshot

    def _resolve_snapshot(
        self,
        snapshot: Snapshot | str,
        expected_identity_semantics: Mapping[str, JsonValue],
        dataset_id: str | None,
    ) -> Snapshot:
        self._validate_expected_semantics(expected_identity_semantics)
        if isinstance(snapshot, str):
            if dataset_id is None:
                raise ValueError("dataset_id is required when resolving a snapshot ID")
            return self.get_snapshot(
                snapshot,
                dataset_id=dataset_id,
                expected_identity_semantics=expected_identity_semantics,
            )
        if not isinstance(snapshot, Snapshot):
            raise ValueError("snapshot must be a Snapshot or snapshot ID")
        expected = _canonical_json(expected_identity_semantics, "expected identity semantics")
        actual = _canonical_json(snapshot.identity_semantics, "identity semantics")
        if expected != actual:
            raise ValueError("snapshot identity semantics do not match the caller")
        return snapshot

    def list_records(
        self,
        snapshot: Snapshot | str,
        *,
        expected_identity_semantics: Mapping[str, JsonValue],
        dataset_id: str | None = None,
        cursor: str | None = None,
    ) -> SnapshotPage:
        """Read one page from a previously pinned snapshot.

        Args:
            snapshot: Validated snapshot descriptor or ID to resolve first.
            expected_identity_semantics: Exact v1 identity rules expected by the caller.
            dataset_id: Required only when ``snapshot`` is an ID. The API uses it
                to reauthorize the snapshot against the dataset scope.
            cursor: Opaque cursor returned by the preceding page, if any.

        Returns:
            One page with publication/schema/window identity revalidated.

        Raises:
            ValueError: If the input scope or cursor is invalid.
            SnapshotClientError: If the server changes the pinned scope or page contract.
        """
        resolved = self._resolve_snapshot(snapshot, expected_identity_semantics, dataset_id)
        if cursor is not None and (
            not isinstance(cursor, str)
            or not cursor
            or len(cursor) > _MAX_CURSOR_LENGTH
            or any(ord(char) < 32 for char in cursor)
        ):
            raise ValueError("cursor must be a non-empty bounded opaque string")
        params = {"snapshot_id": resolved.snapshot_id}
        if cursor is not None:
            params["cursor"] = cursor
        payload, response_bytes = self._request_json(
            "GET",
            f"/datasets/{quote(resolved.dataset_id, safe='')}/records",
            params=params,
            end_time=self._new_deadline(),
            byte_limit=self.max_bytes,
        )
        return self._parse_page(payload, resolved, response_bytes)

    @staticmethod
    def _parse_page(
        payload: Mapping[str, JsonValue], snapshot: Snapshot, response_bytes: int
    ) -> SnapshotPage:
        required = {
            "snapshot_id",
            "dataset_id",
            "publication_id",
            "schema_version",
            "sequence",
            "semantic_version",
            "source",
            "schema",
            "quality",
            "field_units",
            "field_currencies",
            "field_adjustments",
            "currency",
            "adjustment_basis",
            "asset_class",
            "frequency",
            "record_kind",
            "business_key_fields",
            "ordering_fields",
            "storage_order",
            "identity_key_encoding",
            "filter_fields",
            "time_field",
            "temporal_semantics",
            "timezone",
            "pit_policy",
            "source_available_at_field",
            "calendar_id",
            "contract_sha256",
            "window",
            "page_size",
            "as_of",
            "created_at",
            "expires_at",
            "record_count",
            "selection_sha256",
            "content_sha256",
            "identity_semantics",
            "rows",
            "count",
            "next_cursor",
            "has_more",
            "meta",
        }
        if not required.issubset(payload):
            raise SnapshotProtocolError("records page omitted required snapshot metadata")
        page_snapshot = _parse_snapshot(payload)
        if page_snapshot != snapshot:
            raise SnapshotScopeError("records page changed dataset or pinned publication")

        meta = payload["meta"]
        if not isinstance(meta, Mapping) or not isinstance(meta.get("snapshot"), Mapping):
            raise SnapshotProtocolError("records page omitted pinned snapshot metadata")
        if set(meta) != {
            "snapshot",
            "ordering_fields",
            "storage_order",
            "identity_key_encoding",
        }:
            raise SnapshotProtocolError("records page has invalid metadata envelope")
        meta_snapshot = _parse_snapshot(meta["snapshot"])
        if meta_snapshot != snapshot or page_snapshot != meta_snapshot:
            raise SnapshotScopeError("records page snapshot metadata changed")
        meta_ordering = _parse_string_list(meta["ordering_fields"], "meta.ordering_fields")
        meta_storage_order = _parse_storage_order(meta["storage_order"], "meta.storage_order")
        if (
            meta_ordering != snapshot.ordering_fields
            or meta_storage_order != snapshot.storage_order
            or meta["identity_key_encoding"] != snapshot.identity_key_encoding
        ):
            raise SnapshotProtocolError("records page metadata envelope changed its contract")

        rows = payload["rows"]
        count = payload["count"]
        has_more = payload["has_more"]
        cursor = payload["next_cursor"]
        if (
            not isinstance(rows, list)
            or type(count) is not int
            or count != len(rows)
            or type(has_more) is not bool
            or (cursor is not None and (not isinstance(cursor, str) or not cursor))
            or has_more != (cursor is not None)
            or count > snapshot.page_size
        ):
            raise SnapshotProtocolError("records page has invalid count or cursor metadata")

        validated: list[dict[str, JsonValue]] = []
        page_keys: set[str] = set()
        previous_order: tuple[bool, str, str] | None = None
        for raw_row in rows:
            row, key, ordering = _validate_row(raw_row, snapshot)
            if key in page_keys:
                raise SnapshotProtocolError("records page repeats a business_key")
            if previous_order is not None and ordering < previous_order:
                raise SnapshotProtocolError("records page violates its declared stable ordering")
            page_keys.add(key)
            previous_order = ordering
            validated.append(row)
        return SnapshotPage(
            snapshot=snapshot,
            rows=tuple(validated),
            count=count,
            next_cursor=cursor,
            has_more=has_more,
            response_bytes=response_bytes,
        )

    def iter_records(
        self,
        snapshot: Snapshot | str,
        *,
        expected_identity_semantics: Mapping[str, JsonValue],
        dataset_id: str | None = None,
    ) -> Iterator[dict[str, JsonValue]]:
        """Yield every record in a fixed snapshot or raise if completion is unknown.

        Page, row, decoded-byte, and deadline limits apply to the whole
        iterator. The iterator also rejects repeated cursors, duplicate
        business keys, and ordering drift across pages. It never silently
        truncates at a configured limit.

        Args:
            snapshot: Validated snapshot descriptor or snapshot ID.
            expected_identity_semantics: Exact v1 identity rules expected by the caller.
            dataset_id: Required only when ``snapshot`` is an ID so the API can
                reauthorize the scope before reading metadata.

        Yields:
            Record mappings in the stable order declared by the snapshot.

        Raises:
            SnapshotClientError: If a page, cursor, scope, or completeness invariant fails.
        """
        resolved = self._resolve_snapshot(snapshot, expected_identity_semantics, dataset_id)
        end_time = self._new_deadline()
        seen_cursors: set[str] = set()
        seen_keys: set[str] = set()
        last_order: tuple[bool, str, str] | None = None
        total_rows = 0
        total_bytes = 0
        page_count = 0
        cursor: str | None = None

        while True:
            self._check_deadline(end_time)
            if page_count >= self.max_pages:
                raise SnapshotLimitError("snapshot iterator reached max_pages before completion")
            remaining_bytes = self.max_bytes - total_bytes
            if remaining_bytes <= 0:
                raise SnapshotLimitError("snapshot iterator reached max_bytes before completion")
            page = self._list_records_until(
                resolved,
                expected_identity_semantics,
                cursor=cursor,
                end_time=end_time,
                byte_limit=remaining_bytes,
            )
            page_count += 1
            total_bytes += page.response_bytes
            if total_rows + page.count > self.max_rows:
                raise SnapshotLimitError("snapshot iterator reached max_rows before completion")
            if page.has_more and page_count >= self.max_pages:
                raise SnapshotLimitError("snapshot iterator reached max_pages before completion")
            if page.has_more and total_rows + page.count >= self.max_rows:
                raise SnapshotLimitError("snapshot iterator reached max_rows before completion")
            if page.has_more and total_bytes >= self.max_bytes:
                raise SnapshotLimitError("snapshot iterator reached max_bytes before completion")

            for row in page.rows:
                self._check_deadline(end_time)
                _, key, ordering = _validate_row(row, resolved)
                if key in seen_keys:
                    raise SnapshotProtocolError("snapshot iterator repeats a business_key")
                if last_order is not None and ordering < last_order:
                    raise SnapshotProtocolError(
                        "snapshot iterator violates its declared stable ordering"
                    )
                seen_keys.add(key)
                last_order = ordering
                total_rows += 1
                yield row

            if not page.has_more:
                if total_rows != resolved.record_count:
                    raise SnapshotProtocolError(
                        "snapshot iterator ended before record_count was satisfied"
                    )
                return
            next_cursor = page.next_cursor
            if next_cursor is None or next_cursor in seen_cursors or next_cursor == cursor:
                raise SnapshotProtocolError("snapshot cursor did not make forward progress")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    def _list_records_until(
        self,
        snapshot: Snapshot,
        expected_identity_semantics: Mapping[str, JsonValue],
        *,
        cursor: str | None,
        end_time: float,
        byte_limit: int,
    ) -> SnapshotPage:
        self._validate_expected_semantics(expected_identity_semantics)
        params = {"snapshot_id": snapshot.snapshot_id}
        if cursor is not None:
            params["cursor"] = cursor
        payload, response_bytes = self._request_json(
            "GET",
            f"/datasets/{quote(snapshot.dataset_id, safe='')}/records",
            params=params,
            end_time=end_time,
            byte_limit=byte_limit,
        )
        return self._parse_page(payload, snapshot, response_bytes)
