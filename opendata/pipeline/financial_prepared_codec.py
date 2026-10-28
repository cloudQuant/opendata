"""Versioned, bounded JSON encoding for prepared financial archive captures.

The codec transports only the existing immutable SQL-value DTO. It does not
read credentials, add credential fields, create provenance, access a database,
or authenticate a source. Existing string values are preserved verbatim and
are not scanned or redacted by this codec; callers remain responsible for the
content accepted by the store contract. Encoding and decoding both delegate
semantic validation to the archive store's existing prepared-capture materializer.

Version 1 has a 16 MiB limit for the runner path. A larger DTO that the store
can otherwise represent is rejected before publication. This codec does not
split DTOs; the runner must keep such a capture rejected until a separate,
reviewed batching path exists. Do not raise this limit without evaluating real
capture sizes and the runner's memory budget.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import TypeAlias

from opendata.pipeline.financial_observation_store import (
    FinancialObservationStoreError,
    PreparedCapture,
    _materialize_prepared_capture,
)

_CODEC_NAME = "opendata.financial_prepared_capture"
_CODEC_VERSION = 1
_MAX_CODEC_BYTES = 16 * 1024 * 1024
# The current envelope contains at most 111,365 object/array entries, counting
# every nested row pair and scalar-tag object: envelope 8; capture row 25 + 50
# pair-array entries + 50 tag fields; 1,024 observations with 1 outer + 2 pair
# + 15 row + 30 column-pair + 30 tag entries each; 1,024 links with 1 outer
# + 2 group-pair + 1 row-list + 5 row + 10 column-pair + 10 tag entries each;
# and 64 quarantine rows with 1 outer + 5 row + 10 column-pair + 10 tag entries
# each. The 140,000 cap leaves roughly 26% room for modest contract growth.
_MAX_JSON_TOTAL_ENTRIES = 140_000
_MAX_JSON_CONTAINER_ENTRIES = 2_048
_MAX_JSON_NESTING = 8
_ENVELOPE_FIELDS = frozenset(
    {
        "capture_id",
        "capture_values",
        "codec",
        "links_by_observation",
        "observation_values",
        "quarantine_values",
        "sql_statement_ceiling",
        "version",
    }
)

_JsonValue: TypeAlias = dict[str, object] | list[object] | str | int | None
_SqlScalar: TypeAlias = str | int | date | datetime | None
_FrozenRow: TypeAlias = tuple[tuple[str, _SqlScalar], ...]


class FinancialPreparedCaptureCodecError(ValueError):
    """Raised when a prepared capture is invalid or outside the JSON contract."""


def encode_prepared_capture(prepared: PreparedCapture) -> bytes:
    """Encode a store-validated DTO as canonical, bounded UTF-8 JSON bytes.

    Strings such as decimal text, native units, source metadata JSON, and
    content-addressed keys are carried without parsing or normalization.

    Args:
        prepared: Exact immutable DTO returned by the archive preparation API.

    Returns:
        Canonical JSON bytes using codec version 1.

    Raises:
        FinancialPreparedCaptureCodecError: The DTO is invalid or its canonical
            representation would exceed the 16 MiB runner-path limit.
    """
    checked = _store_validated_capture(prepared)
    return _canonical_json_bytes(_document_from_capture(checked))


def decode_prepared_capture(payload: object) -> PreparedCapture:
    """Decode canonical version-1 JSON and revalidate the exact store DTO.

    Args:
        payload: Untrusted candidate value, which must be exact ``bytes``.

    Returns:
        A deeply immutable prepared capture accepted by the current store
        validator.

    Raises:
        FinancialPreparedCaptureCodecError: The bytes are malformed, non-
            canonical, oversized, unsupported, or fail store validation.
    """
    if type(payload) is not bytes or not 1 <= len(payload) <= _MAX_CODEC_BYTES:
        raise FinancialPreparedCaptureCodecError("prepared capture payload size is invalid")
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FinancialPreparedCaptureCodecError("prepared capture is not valid UTF-8") from None
    _preflight_json_structure(text)

    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_float=_reject_json_float,
            parse_int=_parse_bounded_json_int,
            parse_constant=_reject_json_constant,
        )
    except FinancialPreparedCaptureCodecError:
        raise
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        raise FinancialPreparedCaptureCodecError("prepared capture JSON is invalid") from None

    try:
        prepared = _decode_document(document)
        checked = _store_validated_capture(prepared)
        if _canonical_json_bytes(_document_from_capture(checked)) != payload:
            raise FinancialPreparedCaptureCodecError(
                "prepared capture JSON is not in canonical form"
            )
    except FinancialPreparedCaptureCodecError:
        raise
    except (
        AttributeError,
        IndexError,
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise FinancialPreparedCaptureCodecError(
            "prepared capture does not match the version-1 store contract"
        ) from None
    return checked


def _preflight_json_structure(text: str) -> None:
    """Bound JSON nesting and collection entries in one string-aware scan.

    The standard-library parser builds a complete object graph. This scan runs
    first so a small byte payload containing a huge number of nested values
    cannot make parsing allocate an unbounded tree. Quotes and backslash
    escapes are tracked so JSON-looking characters in strings are ignored.
    Full grammar and escape validation remains the responsibility of
    ``json.loads``.
    """
    # Each mutable frame contains [opening bracket, comma count, has content].
    stack: list[list[int | bool]] = []
    total_entries = 0
    in_string = False
    escaped = False

    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            if stack:
                stack[-1][2] = True
            in_string = True
        elif character in "[{":
            if stack:
                stack[-1][2] = True
            if len(stack) >= _MAX_JSON_NESTING:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture JSON nesting exceeds its limit"
                )
            stack.append([ord(character), 0, False])
        elif character in "]}":
            if not stack:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture JSON structure is invalid"
                )
            frame = stack.pop()
            expected_opening = ord("[") if character == "]" else ord("{")
            if frame[0] != expected_opening:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture JSON structure is invalid"
                )
            commas = int(frame[1])
            has_content = bool(frame[2])
            entries = commas + int(has_content)
            if entries > _MAX_JSON_CONTAINER_ENTRIES:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture JSON container exceeds its entry limit"
                )
            total_entries += entries
            if total_entries > _MAX_JSON_TOTAL_ENTRIES:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture JSON exceeds its total entry limit"
                )
        elif character == ",":
            if stack:
                frame = stack[-1]
                frame[1] = int(frame[1]) + 1
                frame[2] = True
                # A comma implies at least one more item/member than preceding
                # separators; reject before scanning/parsing the rest.
                if int(frame[1]) + 1 > _MAX_JSON_CONTAINER_ENTRIES:
                    raise FinancialPreparedCaptureCodecError(
                        "prepared capture JSON container exceeds its entry limit"
                    )
        elif character not in " \t\r\n" and stack:
            stack[-1][2] = True

    if in_string or stack:
        raise FinancialPreparedCaptureCodecError("prepared capture JSON structure is invalid")


def _document_from_capture(prepared: PreparedCapture) -> dict[str, _JsonValue]:
    """Build the fixed version-1 envelope from an already validated DTO."""
    return {
        "capture_id": prepared._capture_id,
        "capture_values": _encode_row(prepared._capture_values),
        "codec": _CODEC_NAME,
        "links_by_observation": [
            [observation_id, [_encode_row(row) for row in rows]]
            for observation_id, rows in prepared._links_by_observation
        ],
        "observation_values": [
            [observation_id, _encode_row(row)]
            for observation_id, row in prepared._observation_values
        ],
        "quarantine_values": [_encode_row(row) for row in prepared._quarantine_values],
        "sql_statement_ceiling": prepared._sql_statement_ceiling,
        "version": _CODEC_VERSION,
    }


def _decode_document(document: object) -> PreparedCapture:
    """Validate envelope shape and rebuild immutable tuples without coercion."""
    if type(document) is not dict or set(document) != _ENVELOPE_FIELDS:
        raise FinancialPreparedCaptureCodecError(
            "prepared capture envelope has unknown or missing fields"
        )
    if type(document["codec"]) is not str or document["codec"] != _CODEC_NAME:
        raise FinancialPreparedCaptureCodecError("prepared capture codec identifier is unknown")
    if type(document["version"]) is not int or document["version"] != _CODEC_VERSION:
        raise FinancialPreparedCaptureCodecError("prepared capture codec version is unsupported")

    capture_id = document["capture_id"]
    statement_ceiling = document["sql_statement_ceiling"]
    if type(capture_id) is not str or type(statement_ceiling) is not int:
        raise FinancialPreparedCaptureCodecError(
            "prepared capture envelope field types are invalid"
        )

    capture_values = _decode_row(document["capture_values"])
    observation_values = _decode_observation_rows(document["observation_values"])
    links_by_observation = _decode_link_groups(document["links_by_observation"])
    quarantine_values = _decode_rows(document["quarantine_values"])
    return PreparedCapture(
        _capture_id=capture_id,
        _capture_values=capture_values,
        _observation_values=observation_values,
        _links_by_observation=links_by_observation,
        _quarantine_values=quarantine_values,
        _sql_statement_ceiling=statement_ceiling,
    )


def _decode_observation_rows(value: object) -> tuple[tuple[str, _FrozenRow], ...]:
    """Decode the exact two-element observation identity and row pairs."""
    if type(value) is not list:
        raise FinancialPreparedCaptureCodecError("prepared observation rows are invalid")
    decoded: list[tuple[str, _FrozenRow]] = []
    for entry in value:
        if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str:
            raise FinancialPreparedCaptureCodecError("prepared observation entry is invalid")
        decoded.append((entry[0], _decode_row(entry[1])))
    return tuple(decoded)


def _decode_link_groups(value: object) -> tuple[tuple[str, tuple[_FrozenRow, ...]], ...]:
    """Decode exact link groups and their immutable row tuples."""
    if type(value) is not list:
        raise FinancialPreparedCaptureCodecError("prepared link groups are invalid")
    decoded: list[tuple[str, tuple[_FrozenRow, ...]]] = []
    for entry in value:
        if (
            type(entry) is not list
            or len(entry) != 2
            or type(entry[0]) is not str
            or type(entry[1]) is not list
        ):
            raise FinancialPreparedCaptureCodecError("prepared link group is invalid")
        decoded.append((entry[0], tuple(_decode_row(row) for row in entry[1])))
    return tuple(decoded)


def _decode_rows(value: object) -> tuple[_FrozenRow, ...]:
    """Decode a list of immutable SQL rows."""
    if type(value) is not list:
        raise FinancialPreparedCaptureCodecError("prepared SQL rows are invalid")
    return tuple(_decode_row(row) for row in value)


def _decode_row(value: object) -> _FrozenRow:
    """Decode a SQL row while preserving its original tuple order."""
    if type(value) is not list:
        raise FinancialPreparedCaptureCodecError("prepared SQL row is invalid")
    decoded: list[tuple[str, _SqlScalar]] = []
    names: set[str] = set()
    for entry in value:
        if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str:
            raise FinancialPreparedCaptureCodecError("prepared SQL row entry is invalid")
        name = entry[0]
        if name in names:
            raise FinancialPreparedCaptureCodecError("prepared SQL row has a duplicate column")
        names.add(name)
        decoded.append((name, _decode_scalar(entry[1])))
    return tuple(decoded)


def _encode_row(row: _FrozenRow) -> list[object]:
    """Encode a frozen SQL row while retaining its exact column tuple order."""
    return [[name, _encode_scalar(value)] for name, value in row]


def _encode_scalar(value: object) -> dict[str, object]:
    """Tag one exact supported Python scalar without changing its content."""
    if value is None:
        return {"kind": "null"}
    if type(value) is str:
        return {"kind": "string", "value": value}
    if type(value) is int:
        return {"kind": "integer", "value": value}
    if type(value) is date:
        return {"kind": "date", "value": value.isoformat()}
    if type(value) is datetime:
        if value.tzinfo is not None:
            raise FinancialPreparedCaptureCodecError(
                "prepared datetime must be a naive UTC database value"
            )
        return {
            "kind": "datetime_utc",
            "value": value.isoformat(timespec="microseconds") + "Z",
        }
    raise FinancialPreparedCaptureCodecError("prepared scalar has an unsupported exact type")


def _decode_scalar(value: object) -> _SqlScalar:
    """Decode one closed scalar tag while rejecting implicit type coercions."""
    if type(value) is not dict or type(value.get("kind")) is not str:
        raise FinancialPreparedCaptureCodecError("prepared scalar tag is invalid")
    kind = value["kind"]
    if kind == "null":
        if set(value) != {"kind"}:
            raise FinancialPreparedCaptureCodecError("prepared null scalar has unknown fields")
        return None
    if set(value) != {"kind", "value"}:
        raise FinancialPreparedCaptureCodecError("prepared scalar has unknown or missing fields")
    payload = value["value"]
    if kind == "string" and type(payload) is str:
        return payload
    if kind == "integer" and type(payload) is int:
        return payload
    if kind == "date" and type(payload) is str:
        try:
            parsed_date = date.fromisoformat(payload)
        except ValueError:
            raise FinancialPreparedCaptureCodecError("prepared date scalar is invalid") from None
        if parsed_date.isoformat() != payload:
            raise FinancialPreparedCaptureCodecError("prepared date scalar is not canonical")
        return parsed_date
    if kind == "datetime_utc" and type(payload) is str:
        if not payload.endswith("Z"):
            raise FinancialPreparedCaptureCodecError("prepared UTC datetime marker is missing")
        try:
            parsed_datetime = datetime.fromisoformat(payload[:-1])
        except ValueError:
            raise FinancialPreparedCaptureCodecError("prepared UTC datetime is invalid") from None
        if (
            parsed_datetime.tzinfo is not None
            or parsed_datetime.isoformat(timespec="microseconds") + "Z" != payload
        ):
            raise FinancialPreparedCaptureCodecError(
                "prepared UTC datetime is not exact canonical UTC"
            )
        return parsed_datetime
    raise FinancialPreparedCaptureCodecError("prepared scalar tag or value type is invalid")


def _store_validated_capture(value: object) -> PreparedCapture:
    """Require the exact DTO and enforce the store's complete existing checks."""
    if type(value) is not PreparedCapture:
        raise FinancialPreparedCaptureCodecError("prepared capture has an invalid exact type")
    try:
        _materialize_prepared_capture(value)
    except (
        AttributeError,
        FinancialObservationStoreError,
        IndexError,
        KeyError,
        OverflowError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        raise FinancialPreparedCaptureCodecError(
            "prepared capture failed the existing store validation"
        ) from None
    return value


def _canonical_json_bytes(document: dict[str, _JsonValue]) -> bytes:
    """Encode canonical JSON incrementally, stopping at the fixed byte limit."""
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    output = bytearray()
    try:
        for chunk in encoder.iterencode(document):
            encoded_chunk = chunk.encode("utf-8", errors="strict")
            if len(output) + len(encoded_chunk) > _MAX_CODEC_BYTES:
                raise FinancialPreparedCaptureCodecError(
                    "prepared capture exceeds the 16 MiB runner-path limit"
                )
            output.extend(encoded_chunk)
    except FinancialPreparedCaptureCodecError:
        raise
    except (TypeError, UnicodeEncodeError, ValueError, RecursionError):
        raise FinancialPreparedCaptureCodecError(
            "prepared capture cannot be encoded as canonical JSON"
        ) from None
    return bytes(output)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject repeated JSON object keys before a mapping can discard them."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FinancialPreparedCaptureCodecError("prepared capture has a duplicate JSON key")
        result[key] = value
    return result


def _reject_json_float(token: str) -> object:
    """Reject floating JSON tokens because store scalars are exact SQL values."""
    del token
    raise FinancialPreparedCaptureCodecError("prepared capture JSON floats are unsupported")


def _parse_bounded_json_int(token: str) -> int:
    """Parse only a bounded integer token; store validation checks field ranges."""
    if len(token) > 20:
        raise FinancialPreparedCaptureCodecError("prepared capture integer token is oversized")
    try:
        return int(token)
    except ValueError:
        raise FinancialPreparedCaptureCodecError(
            "prepared capture integer token is invalid"
        ) from None


def _reject_json_constant(token: str) -> object:
    """Reject JSON extensions such as NaN and Infinity."""
    del token
    raise FinancialPreparedCaptureCodecError("prepared capture contains a non-finite number")


__all__ = [
    "FinancialPreparedCaptureCodecError",
    "decode_prepared_capture",
    "encode_prepared_capture",
]
