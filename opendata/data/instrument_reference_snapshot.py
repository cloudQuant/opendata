"""Strict, bounded JSON codec for reviewed instrument reference snapshots.

This module only encodes and decodes caller-provided bytes. It does not select
or read a file, load an environment variable, contact a provider, or access a
database. The digest makes accidental changes detectable when its expected
value is trusted. It is not a signature and does
not establish review, source rights, completeness, or point-in-time availability.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)

MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_ROWS_PER_KIND = 100_000
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 3_000_000
_HASH_LENGTH = 64
_LIFECYCLE_STATUS_VALUES = frozenset(status.value for status in LifecycleStatus)

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "registry_version",
        "evidence_ref",
        "lifecycles",
        "aliases",
        "content_sha256",
    }
)
_LIFECYCLE_FIELDS = frozenset(
    {
        "dataset_id",
        "source_id",
        "instrument_id",
        "lifecycle_version",
        "status",
        "status_as_of",
        "status_evidence_ref",
        "known_start_date",
        "start_boundary_unknown",
        "start_evidence_ref",
        "delisted_on",
        "delist_evidence_ref",
        "schema_version",
    }
)
_ALIAS_FIELDS = frozenset(
    {
        "dataset_id",
        "source_id",
        "alias",
        "instrument_id",
        "alias_version",
        "evidence_ref",
        "valid_from",
        "valid_through",
        "schema_version",
    }
)


class InstrumentReferenceSnapshotError(ValueError):
    """Raised when a reviewed instrument snapshot is malformed or unbounded."""


def encode_instrument_reference_snapshot(registry: InstrumentReferenceRegistry) -> bytes:
    """Encode one validated registry as deterministic, hash-pinned UTF-8 JSON.

    Lifecycle and alias rows are sorted by their stable identities so equivalent
    registries produce the same bytes regardless of input ordering. The SHA-256
    covers the canonical JSON document with ``content_sha256`` omitted.

    Args:
        registry: Explicitly reviewed registry already validated by the domain model.

    Returns:
        A canonical UTF-8 JSON document as immutable bytes.

    Raises:
        InstrumentReferenceSnapshotError: If the input or encoded size is invalid.
    """
    if not isinstance(registry, InstrumentReferenceRegistry):
        raise InstrumentReferenceSnapshotError("registry must be an InstrumentReferenceRegistry")
    _check_registry_node_budget(registry)
    try:
        payload = _payload_for_registry(registry)
        digest = _payload_sha256(payload)
        document = {**payload, "content_sha256": digest}
        encoded = _canonical_json_bytes(document)
    except InstrumentReferenceSnapshotError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise InstrumentReferenceSnapshotError(
            "instrument reference snapshot is not encodable"
        ) from None
    if len(encoded) > MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES:
        raise InstrumentReferenceSnapshotError("instrument reference snapshot exceeds 64 MiB")
    return encoded


def decode_instrument_reference_snapshot(raw: bytes) -> InstrumentReferenceRegistry:
    """Decode strict, bounded JSON and validate it with the domain registry.

    Nullable lifecycle and alias fields must be present explicitly. Unknown
    lifecycle status and unknown start boundaries are preserved exactly; this
    codec never derives a start date from market-data rows.

    Args:
        raw: Immutable UTF-8 JSON bytes supplied by the caller.

    Returns:
        A fully validated, immutable instrument reference registry.

    Raises:
        InstrumentReferenceSnapshotError: If bytes, JSON, hash, schema, or domain
            constraints are invalid.
    """
    if type(raw) is not bytes:
        raise InstrumentReferenceSnapshotError("snapshot input must be bytes")
    if not raw:
        raise InstrumentReferenceSnapshotError("snapshot input must not be empty")
    if len(raw) > MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES:
        raise InstrumentReferenceSnapshotError("instrument reference snapshot exceeds 64 MiB")
    try:
        text = raw.decode("utf-8", errors="strict")
        _preflight_json(text)
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except InstrumentReferenceSnapshotError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise InstrumentReferenceSnapshotError("snapshot is not strict UTF-8 JSON") from None

    root = _object(document, _ROOT_FIELDS, "root")
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise InstrumentReferenceSnapshotError("snapshot schema_version must be integer 1")
    supplied_hash = root["content_sha256"]
    if (
        not isinstance(supplied_hash, str)
        or len(supplied_hash) != _HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in supplied_hash)
    ):
        raise InstrumentReferenceSnapshotError("snapshot content_sha256 must be lowercase SHA-256")

    lifecycles_raw = root["lifecycles"]
    aliases_raw = root["aliases"]
    if not isinstance(lifecycles_raw, list) or len(lifecycles_raw) > _MAX_ROWS_PER_KIND:
        raise InstrumentReferenceSnapshotError(
            "snapshot lifecycles must be an array of at most 100000 rows"
        )
    if not isinstance(aliases_raw, list) or len(aliases_raw) > _MAX_ROWS_PER_KIND:
        raise InstrumentReferenceSnapshotError(
            "snapshot aliases must be an array of at most 100000 rows"
        )

    try:
        lifecycles = tuple(_parse_lifecycle(row) for row in lifecycles_raw)
        aliases = tuple(_parse_alias(row) for row in aliases_raw)
        registry = InstrumentReferenceRegistry(
            registry_version=_string(root["registry_version"], "registry_version"),
            evidence_ref=_string(root["evidence_ref"], "evidence_ref"),
            lifecycles=lifecycles,
            aliases=aliases,
            schema_version=root["schema_version"],
        )
    except InstrumentReferenceSnapshotError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise InstrumentReferenceSnapshotError(
            "snapshot fails instrument reference validation"
        ) from None

    try:
        expected_hash = _payload_sha256(_payload_for_registry(registry))
    except InstrumentReferenceSnapshotError:
        raise
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError):
        raise InstrumentReferenceSnapshotError(
            "snapshot contains text that cannot be canonically encoded"
        ) from None
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise InstrumentReferenceSnapshotError("snapshot content_sha256 does not match its payload")
    return registry


def _payload_for_registry(registry: InstrumentReferenceRegistry) -> dict[str, object]:
    """Build the normalized hash input, including stable row order."""
    lifecycles = sorted(
        registry.lifecycles,
        key=lambda row: (row.dataset_id, row.source_id, row.instrument_id),
    )
    aliases = sorted(
        registry.aliases,
        key=lambda row: (
            row.dataset_id,
            row.source_id,
            row.alias,
            row.valid_from.isoformat() if row.valid_from is not None else "",
            row.valid_through.isoformat() if row.valid_through is not None else "",
            row.instrument_id,
            row.alias_version,
        ),
    )
    return {
        "schema_version": registry.schema_version,
        "registry_version": registry.registry_version,
        "evidence_ref": registry.evidence_ref,
        "lifecycles": [_lifecycle_payload(row) for row in lifecycles],
        "aliases": [_alias_payload(row) for row in aliases],
    }


def _check_registry_node_budget(registry: InstrumentReferenceRegistry) -> None:
    """Keep encoder output within the same pre-parse node budget as the decoder."""
    node_count = 13 + 27 * len(registry.lifecycles) + 19 * len(registry.aliases)
    if node_count > _MAX_JSON_NODES:
        raise InstrumentReferenceSnapshotError("snapshot JSON exceeds node limit")


def _lifecycle_payload(row: InstrumentLifecycle) -> dict[str, object]:
    """Return every lifecycle field without deriving missing evidence."""
    return {
        "dataset_id": row.dataset_id,
        "source_id": row.source_id,
        "instrument_id": row.instrument_id,
        "lifecycle_version": row.lifecycle_version,
        "status": LifecycleStatus(row.status).value,
        "status_as_of": row.status_as_of.isoformat(),
        "status_evidence_ref": row.status_evidence_ref,
        "known_start_date": _date_payload(row.known_start_date),
        "start_boundary_unknown": row.start_boundary_unknown,
        "start_evidence_ref": row.start_evidence_ref,
        "delisted_on": _date_payload(row.delisted_on),
        "delist_evidence_ref": row.delist_evidence_ref,
        "schema_version": row.schema_version,
    }


def _alias_payload(row: InstrumentAlias) -> dict[str, object]:
    """Return an alias's exact scope, interval, version, and evidence."""
    return {
        "dataset_id": row.dataset_id,
        "source_id": row.source_id,
        "alias": row.alias,
        "instrument_id": row.instrument_id,
        "alias_version": row.alias_version,
        "evidence_ref": row.evidence_ref,
        "valid_from": _date_payload(row.valid_from),
        "valid_through": _date_payload(row.valid_through),
        "schema_version": row.schema_version,
    }


def _parse_lifecycle(raw: object) -> InstrumentLifecycle:
    """Parse one exact-schema lifecycle without permissive type coercion."""
    row = _object(raw, _LIFECYCLE_FIELDS, "lifecycle")
    status = _string(row["status"], "status")
    if status not in _LIFECYCLE_STATUS_VALUES:
        raise InstrumentReferenceSnapshotError("lifecycle status is unsupported")
    lifecycle_status = LifecycleStatus(status)
    return InstrumentLifecycle(
        dataset_id=_string(row["dataset_id"], "dataset_id"),
        source_id=_string(row["source_id"], "source_id"),
        instrument_id=_string(row["instrument_id"], "instrument_id"),
        lifecycle_version=_string(row["lifecycle_version"], "lifecycle_version"),
        status=lifecycle_status,
        status_as_of=_required_date(row["status_as_of"], "status_as_of"),
        status_evidence_ref=_string(row["status_evidence_ref"], "status_evidence_ref"),
        known_start_date=_date_value(row["known_start_date"], "known_start_date", nullable=True),
        start_boundary_unknown=_bool(row["start_boundary_unknown"], "start_boundary_unknown"),
        start_evidence_ref=_nullable_string(row["start_evidence_ref"], "start_evidence_ref"),
        delisted_on=_date_value(row["delisted_on"], "delisted_on", nullable=True),
        delist_evidence_ref=_nullable_string(row["delist_evidence_ref"], "delist_evidence_ref"),
        schema_version=_version(row["schema_version"], "lifecycle schema_version"),
    )


def _parse_alias(raw: object) -> InstrumentAlias:
    """Parse one exact-schema alias without permissive type coercion."""
    row = _object(raw, _ALIAS_FIELDS, "alias")
    return InstrumentAlias(
        dataset_id=_string(row["dataset_id"], "dataset_id"),
        source_id=_string(row["source_id"], "source_id"),
        alias=_string(row["alias"], "alias"),
        instrument_id=_string(row["instrument_id"], "instrument_id"),
        alias_version=_string(row["alias_version"], "alias_version"),
        evidence_ref=_string(row["evidence_ref"], "evidence_ref"),
        valid_from=_date_value(row["valid_from"], "valid_from", nullable=True),
        valid_through=_date_value(row["valid_through"], "valid_through", nullable=True),
        schema_version=_version(row["schema_version"], "alias schema_version"),
    )


def _object(value: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    """Require a JSON object with the exact frozen field set."""
    if not isinstance(value, dict) or set(value) != fields:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} fields do not match schema")
    return value


def _string(value: object, name: str) -> str:
    """Require a JSON string, rejecting bool and numeric coercions."""
    if not isinstance(value, str) or len(value) > 512:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} must be a bounded string")
    return value


def _nullable_string(value: object, name: str) -> str | None:
    """Require either an explicit JSON null or a string."""
    if value is None:
        return None
    return _string(value, name)


def _bool(value: object, name: str) -> bool:
    """Require a JSON boolean without accepting integers."""
    if type(value) is not bool:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} must be a boolean")
    return value


def _version(value: object, name: str) -> int:
    """Require this codec's integer schema version without accepting bool."""
    if type(value) is not int or value != 1:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} must be integer 1")
    return value


def _date_value(value: object, name: str, *, nullable: bool) -> date | None:
    """Parse only canonical YYYY-MM-DD strings or an explicit nullable null."""
    if value is None and nullable:
        return None
    if (
        not isinstance(value, str)
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or any(
            not value[index].isascii() or not value[index].isdigit()
            for index in (0, 1, 2, 3, 5, 6, 8, 9)
        )
    ):
        raise InstrumentReferenceSnapshotError(f"snapshot {name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} is not a valid ISO date") from None
    if parsed.isoformat() != value:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} is not canonical YYYY-MM-DD")
    return parsed


def _required_date(value: object, name: str) -> date:
    """Parse one required canonical date string."""
    parsed = _date_value(value, name, nullable=False)
    if parsed is None:
        raise InstrumentReferenceSnapshotError(f"snapshot {name} must not be null")
    return parsed


def _date_payload(value: date | None) -> str | None:
    """Serialize a validated date without coercing timestamps."""
    return value.isoformat() if value is not None else None


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """Serialize deterministic JSON while stopping at the byte limit."""
    encoded = bytearray()
    for chunk in _canonical_json_chunks(payload):
        if len(encoded) + len(chunk) > MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES:
            raise InstrumentReferenceSnapshotError("instrument reference snapshot exceeds 64 MiB")
        encoded.extend(chunk)
    return bytes(encoded)


def _payload_sha256(payload: Mapping[str, object]) -> str:
    """Hash the canonical payload, excluding its self-referential digest field."""
    digest = hashlib.sha256()
    consumed = 0
    for chunk in _canonical_json_chunks(payload):
        consumed += len(chunk)
        if consumed > MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES:
            raise InstrumentReferenceSnapshotError("instrument reference snapshot exceeds 64 MiB")
        digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_chunks(payload: Mapping[str, object]) -> Iterator[bytes]:
    """Yield canonical UTF-8 JSON chunks without constructing an unbounded copy."""
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    for chunk in encoder.iterencode(payload):
        yield chunk.encode("utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build JSON objects while rejecting duplicate property names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InstrumentReferenceSnapshotError("snapshot JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    """Reject non-standard JSON constants such as NaN and Infinity."""
    raise InstrumentReferenceSnapshotError("snapshot JSON contains a non-standard number")


def _preflight_json(text: str) -> None:
    """Bound JSON nesting and node count before the standard parser allocates a tree."""
    nodes = 0
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character.isspace() or character in ",:}":
            if character == "}":
                depth -= 1
            index += 1
            continue
        if character in "[{":
            nodes += 1
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise InstrumentReferenceSnapshotError("snapshot JSON exceeds depth limit")
            index += 1
        elif character == "]":
            depth -= 1
            index += 1
        elif character == '"':
            nodes += 1
            index += 1
            escaped = False
            while index < length:
                current = text[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
        else:
            nodes += 1
            index += 1
            while index < length and not text[index].isspace() and text[index] not in ',]}:{["':
                index += 1
        if nodes > _MAX_JSON_NODES:
            raise InstrumentReferenceSnapshotError("snapshot JSON exceeds node limit")


__all__ = [
    "MAX_INSTRUMENT_REFERENCE_SNAPSHOT_BYTES",
    "InstrumentReferenceSnapshotError",
    "decode_instrument_reference_snapshot",
    "encode_instrument_reference_snapshot",
]
