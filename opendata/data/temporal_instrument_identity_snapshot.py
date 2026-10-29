"""Strict, bounded JSON codec for temporal instrument identity registries.

The codec only transforms caller-supplied immutable registry values and bytes.
It does not read files, load credentials, contact providers, or access storage.
``source_sha256`` is preserved as a caller declaration. Decode checks the
canonical content against the digest embedded in the same document; changing
both together remains possible. A separately stored and trusted expected digest
can detect later changes, but neither digest proves source authenticity, rights,
completeness, or persistence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import date
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

from opendata.data.temporal_instrument_identity import (
    Exchange,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
)

MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES = 64 * 1024 * 1024
_MAX_ASSIGNMENTS = 100_000
_MAX_JSON_DEPTH = 8
_MAX_JSON_NODES = 2_200_000
_MAX_JSON_ARRAY_ITEMS = _MAX_ASSIGNMENTS
_MAX_OBJECT_MEMBERS = 10
_HASH_LENGTH = 64
_SNAPSHOT_TYPE = "temporal_identity_registry"

_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "reference_version",
        "evidence_ref",
        "assignments",
        "content_sha256",
        "snapshot_type",
    }
)
_ASSIGNMENT_FIELDS = frozenset(
    {
        "assignment_id",
        "instrument_id",
        "exchange",
        "symbol",
        "valid_from",
        "valid_to",
        "source_id",
        "source_version",
        "evidence_ref",
        "source_sha256",
    }
)


class TemporalIdentitySnapshotError(ValueError):
    """Raised when a temporal identity snapshot is malformed or unbounded."""


def encode_temporal_identity_snapshot(registry: TemporalIdentityRegistry) -> bytes:
    """Encode a validated registry as deterministic, self-digested UTF-8 JSON.

    Assignments are sorted by their domain identity order. The SHA-256 covers
    canonical JSON with ``content_sha256`` omitted. Serialization is streamed
    row by row so peak temporary output memory is bounded by the byte cap.

    Args:
        registry: Explicitly supplied and domain-validated identity registry.

    Returns:
        Canonical UTF-8 JSON bytes suitable for caller-managed persistence.

    Raises:
        TemporalIdentitySnapshotError: If the registry or encoded output is invalid.
    """
    if type(registry) is not TemporalIdentityRegistry:
        raise TemporalIdentitySnapshotError("registry must be a TemporalIdentityRegistry")
    _check_registry_budget(registry)
    try:
        digest = _payload_sha256(registry)
        encoded = _document_bytes(registry, digest)
    except TemporalIdentitySnapshotError:
        raise
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError):
        raise TemporalIdentitySnapshotError(
            "temporal identity snapshot is not canonically encodable"
        ) from None
    return encoded


def decode_temporal_identity_snapshot(raw: bytes) -> TemporalIdentityRegistry:
    """Decode bounded canonical JSON and validate it with the domain registry.

    The decoder rejects duplicate keys, unknown fields, noncanonical byte
    encodings, malformed dates, overlapping assignments, invalid hashes, and
    Python-style boolean/integer coercions. It preserves caller-declared source
    hashes without treating them as independently verified evidence.

    Args:
        raw: Immutable UTF-8 JSON bytes supplied by the caller.

    Returns:
        A fully validated immutable temporal identity registry.

    Raises:
        TemporalIdentitySnapshotError: If bytes, JSON, digest, or domain values
            are malformed or exceed the fixed resource bounds.
    """
    if type(raw) is not bytes:
        raise TemporalIdentitySnapshotError("snapshot input must be bytes")
    if not raw:
        raise TemporalIdentitySnapshotError("snapshot input must not be empty")
    if len(raw) > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
        raise TemporalIdentitySnapshotError("temporal identity snapshot exceeds 64 MiB")

    try:
        text = raw.decode("utf-8", errors="strict")
        _preflight_json(text)
        document = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
            parse_float=_reject_float,
            parse_int=_parse_integer,
        )
    except TemporalIdentitySnapshotError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise TemporalIdentitySnapshotError("snapshot is not strict UTF-8 JSON") from None

    root = _object(document, _ROOT_FIELDS, "root")
    if root["snapshot_type"] != _SNAPSHOT_TYPE or type(root["snapshot_type"]) is not str:
        raise TemporalIdentitySnapshotError(
            "snapshot snapshot_type must be temporal_identity_registry"
        )
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise TemporalIdentitySnapshotError("snapshot schema_version must be integer 1")
    supplied_hash = root["content_sha256"]
    if (
        type(supplied_hash) is not str
        or len(supplied_hash) != _HASH_LENGTH
        or any(character not in "0123456789abcdef" for character in supplied_hash)
    ):
        raise TemporalIdentitySnapshotError("snapshot content_sha256 must be lowercase SHA-256")

    assignments_raw = root["assignments"]
    if not isinstance(assignments_raw, list) or len(assignments_raw) > _MAX_ASSIGNMENTS:
        raise TemporalIdentitySnapshotError(
            "snapshot assignments must be an array of at most 100000 rows"
        )

    try:
        assignments = tuple(_parse_assignment(row) for row in assignments_raw)
        registry = TemporalIdentityRegistry(
            reference_version=_string(root["reference_version"], "reference_version"),
            evidence_ref=_string(root["evidence_ref"], "evidence_ref"),
            assignments=assignments,
        )
    except TemporalIdentitySnapshotError:
        raise
    except (TypeError, ValueError, RecursionError):
        raise TemporalIdentitySnapshotError("snapshot fails temporal identity validation") from None

    try:
        expected_hash = _payload_sha256(registry)
    except TemporalIdentitySnapshotError:
        raise
    except (UnicodeEncodeError, TypeError, ValueError, RecursionError):
        raise TemporalIdentitySnapshotError(
            "snapshot contains text that cannot be canonically encoded"
        ) from None
    if not hmac.compare_digest(supplied_hash, expected_hash):
        raise TemporalIdentitySnapshotError("snapshot content_sha256 does not match its payload")
    if not _matches_canonical_document(raw, registry, supplied_hash):
        raise TemporalIdentitySnapshotError("snapshot bytes are not canonical JSON")
    return registry


def _check_registry_budget(registry: TemporalIdentityRegistry) -> None:
    """Reject row/node counts before building any serialized row mappings."""
    count = len(registry.assignments)
    if count > _MAX_ASSIGNMENTS:
        raise TemporalIdentitySnapshotError("snapshot exceeds the 100000-assignment limit")
    # Root object + six keys/values; each row has an object and ten key/value pairs.
    if 13 + 21 * count > _MAX_JSON_NODES:
        raise TemporalIdentitySnapshotError("snapshot JSON exceeds node limit")


def _assignment_payload(row: TemporalIdentityAssignment) -> dict[str, object]:
    """Return all versioned assignment metadata without deriving evidence."""
    return {
        "assignment_id": row.assignment_id,
        "instrument_id": row.instrument_id,
        "exchange": Exchange(row.exchange).value,
        "symbol": row.symbol,
        "valid_from": row.valid_from.isoformat(),
        "valid_to": row.valid_to.isoformat() if row.valid_to is not None else None,
        "source_id": row.source_id,
        "source_version": row.source_version,
        "evidence_ref": row.evidence_ref,
        "source_sha256": row.source_sha256,
    }


def _parse_assignment(raw: object) -> TemporalIdentityAssignment:
    """Parse one exact-schema assignment with no value coercion."""
    row = _object(raw, _ASSIGNMENT_FIELDS, "assignment")
    return TemporalIdentityAssignment(
        assignment_id=_string(row["assignment_id"], "assignment_id"),
        instrument_id=_string(row["instrument_id"], "instrument_id"),
        exchange=_string(row["exchange"], "exchange"),
        symbol=_string(row["symbol"], "symbol"),
        valid_from=_required_date(row["valid_from"], "valid_from"),
        valid_to=_date_value(row["valid_to"], "valid_to", nullable=True),
        source_id=_string(row["source_id"], "source_id"),
        source_version=_string(row["source_version"], "source_version"),
        evidence_ref=_string(row["evidence_ref"], "evidence_ref"),
        source_sha256=_nullable_string(row["source_sha256"], "source_sha256"),
    )


def _object(value: object, fields: frozenset[str], name: str) -> Mapping[str, object]:
    """Require a JSON object with exactly the frozen field set."""
    if not isinstance(value, dict) or set(value) != fields:
        raise TemporalIdentitySnapshotError(f"snapshot {name} fields do not match schema")
    return value


def _string(value: object, name: str) -> str:
    """Require a JSON string and reject numeric or boolean coercion."""
    if type(value) is not str:
        raise TemporalIdentitySnapshotError(f"snapshot {name} must be a string")
    return value


def _nullable_string(value: object, name: str) -> str | None:
    """Require an explicit JSON null or a string."""
    if value is None:
        return None
    return _string(value, name)


def _date_value(value: object, name: str, *, nullable: bool) -> date | None:
    """Parse only canonical YYYY-MM-DD strings and explicit nullable nulls."""
    if value is None and nullable:
        return None
    if (
        type(value) is not str
        or len(value) != 10
        or value[4] != "-"
        or value[7] != "-"
        or any(
            not value[index].isascii() or not value[index].isdigit()
            for index in (0, 1, 2, 3, 5, 6, 8, 9)
        )
    ):
        raise TemporalIdentitySnapshotError(f"snapshot {name} must be an ISO date string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise TemporalIdentitySnapshotError(f"snapshot {name} is not a valid ISO date") from None
    if parsed.isoformat() != value:
        raise TemporalIdentitySnapshotError(f"snapshot {name} is not canonical YYYY-MM-DD")
    return parsed


def _required_date(value: object, name: str) -> date:
    """Parse one required canonical date string."""
    parsed = _date_value(value, name, nullable=False)
    if parsed is None:
        raise TemporalIdentitySnapshotError(f"snapshot {name} must not be null")
    return parsed


def _row_sort_key(row: TemporalIdentityAssignment) -> tuple[object, ...]:
    """Match the registry's stable ordering for reproducible cross-process bytes."""
    return (
        Exchange(row.exchange).value,
        row.symbol,
        row.valid_from,
        row.valid_to or date.max,
        row.instrument_id,
        row.assignment_id,
    )


def _iter_json_value(value: object) -> Iterator[bytes]:
    """Yield compact, sorted-key JSON chunks for one bounded scalar or row."""
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    for chunk in encoder.iterencode(value):
        yield chunk.encode("utf-8")


def _iter_assignment_chunks(assignments: tuple[TemporalIdentityAssignment, ...]) -> Iterator[bytes]:
    """Stream the canonically sorted assignment array a row at a time."""
    ordered = sorted(assignments, key=_row_sort_key)
    yield b"["
    for index, row in enumerate(ordered):
        if index:
            yield b","
        yield from _iter_json_value(_assignment_payload(row))
    yield b"]"


def _iter_payload_chunks(registry: TemporalIdentityRegistry) -> Iterator[bytes]:
    """Yield canonical digest input with the self-referential field omitted."""
    yield b'{"assignments":'
    yield from _iter_assignment_chunks(registry.assignments)
    yield b',"evidence_ref":'
    yield from _iter_json_value(registry.evidence_ref)
    yield b',"reference_version":'
    yield from _iter_json_value(registry.reference_version)
    yield b',"schema_version":1,"snapshot_type":'
    yield from _iter_json_value(_SNAPSHOT_TYPE)
    yield b"}"


def _payload_sha256(registry: TemporalIdentityRegistry) -> str:
    """Hash canonical payload bytes while enforcing the shared byte cap."""
    digest = hashlib.sha256()
    consumed = 0
    for chunk in _iter_payload_chunks(registry):
        consumed += len(chunk)
        if consumed > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
            raise TemporalIdentitySnapshotError("temporal identity snapshot exceeds 64 MiB")
        digest.update(chunk)
    return digest.hexdigest()


def _iter_document_chunks(
    registry: TemporalIdentityRegistry, content_sha256: str
) -> Iterator[bytes]:
    """Yield the canonical full document with sorted root keys."""
    yield b'{"assignments":'
    yield from _iter_assignment_chunks(registry.assignments)
    yield b',"content_sha256":'
    yield from _iter_json_value(content_sha256)
    yield b',"evidence_ref":'
    yield from _iter_json_value(registry.evidence_ref)
    yield b',"reference_version":'
    yield from _iter_json_value(registry.reference_version)
    yield b',"schema_version":1,"snapshot_type":'
    yield from _iter_json_value(_SNAPSHOT_TYPE)
    yield b"}"


def _document_bytes(registry: TemporalIdentityRegistry, content_sha256: str) -> bytes:
    """Serialize bounded canonical bytes without a second full JSON tree."""
    encoded = bytearray()
    for chunk in _iter_document_chunks(registry, content_sha256):
        if len(encoded) + len(chunk) > MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES:
            raise TemporalIdentitySnapshotError("temporal identity snapshot exceeds 64 MiB")
        encoded.extend(chunk)
    return bytes(encoded)


def _matches_canonical_document(
    raw: bytes, registry: TemporalIdentityRegistry, content_sha256: str
) -> bool:
    """Compare canonical chunks without constructing a second input-sized copy."""
    offset = 0
    for chunk in _iter_document_chunks(registry, content_sha256):
        end = offset + len(chunk)
        if end > len(raw) or raw[offset:end] != chunk:
            return False
        offset = end
    return offset == len(raw)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a shallow JSON object while rejecting repeated member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise TemporalIdentitySnapshotError("snapshot JSON contains a duplicate object key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    """Reject nonstandard JSON constants such as NaN and Infinity."""
    raise TemporalIdentitySnapshotError("snapshot JSON contains a nonstandard number")


def _reject_float(value: str) -> NoReturn:
    """Reject floating-point numbers because the schema has no float fields."""
    raise TemporalIdentitySnapshotError("snapshot JSON contains an unsupported number")


def _parse_integer(value: str) -> int:
    """Parse only the sole integer allowed by the versioned wire schema."""
    if value != "1":
        raise TemporalIdentitySnapshotError("snapshot schema_version must be integer 1")
    return 1


def _preflight_json(text: str) -> None:
    """Bound nesting, rows, object members, and tokens before ``json.loads``.

    This lexical pass uses a stack no deeper than the configured depth and
    rejects oversized arrays and objects before the standard decoder builds a
    Python object tree. Full grammar and schema validation follows separately.
    """
    nodes = 0
    object_count = 0
    array_items = 0
    stack: list[tuple[str, int]] = []
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character in " \t\r\n":
            index += 1
            continue
        if character in ",:":
            if character == ":" and stack and stack[-1][0] == "{":
                kind, members = stack[-1]
                members += 1
                stack[-1] = (kind, members)
                if members > _MAX_OBJECT_MEMBERS:
                    raise TemporalIdentitySnapshotError("snapshot JSON object exceeds member limit")
            index += 1
            continue
        if character in "]}":
            expected = "[" if character == "]" else "{"
            if not stack or stack[-1][0] != expected:
                raise TemporalIdentitySnapshotError("snapshot JSON has mismatched containers")
            stack.pop()
            index += 1
            continue

        if stack and stack[-1][0] == "[":
            array_items += 1
            if array_items > _MAX_JSON_ARRAY_ITEMS:
                raise TemporalIdentitySnapshotError("snapshot JSON exceeds array item limit")

        if character in "[{":
            nodes += 1
            if character == "{":
                object_count += 1
                if object_count > _MAX_ASSIGNMENTS + 1:
                    raise TemporalIdentitySnapshotError(
                        "snapshot exceeds the 100000-assignment limit"
                    )
            stack.append((character, 0))
            if len(stack) > _MAX_JSON_DEPTH:
                raise TemporalIdentitySnapshotError("snapshot JSON exceeds depth limit")
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
                raise TemporalIdentitySnapshotError("snapshot JSON contains an unterminated string")
        else:
            nodes += 1
            index += 1
            while index < length and not text[index].isspace() and text[index] not in ',]}:{["':
                index += 1

        if nodes > _MAX_JSON_NODES:
            raise TemporalIdentitySnapshotError("snapshot JSON exceeds node limit")
    if stack:
        raise TemporalIdentitySnapshotError("snapshot JSON has unterminated containers")


__all__ = [
    "MAX_TEMPORAL_IDENTITY_SNAPSHOT_BYTES",
    "TemporalIdentitySnapshotError",
    "decode_temporal_identity_snapshot",
    "encode_temporal_identity_snapshot",
]
