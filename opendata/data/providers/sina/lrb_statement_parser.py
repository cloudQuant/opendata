"""Preserve Sina LRB response structure without assigning accounting semantics.

The parser accepts at most 4 MiB of wire bytes, 256 entries in each report
collection, 512 rows per report, and 65,536 rows total. However, ``json.loads``
materializes the whole response before collection limits run, then the parser
builds an immutable ordered view while the decoded tree is still alive. Peak
memory therefore includes both representations plus Python object overhead;
the amplification has not been measured, and these limits are not a hard
resident-memory cap. The input cap also does not bound concurrent parses, so
production callers need separate concurrency controls or a streaming parser
before handling concurrent large responses.

Values are JSON-decoded values, not byte-for-byte wire tokens. Decimal numbers
avoid binary-float rounding, but lexical spellings such as ``-0`` may normalize.
Retain the separate private raw-response artifact when exact source bytes matter.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import NoReturn, TypeAlias

__all__ = [
    "SinaLrbRawArray",
    "SinaLrbRawObject",
    "SinaLrbRawReport",
    "SinaLrbRawResponse",
    "SinaLrbRawValue",
    "SinaLrbWireError",
    "parse_sina_lrb_response",
]

MAX_SINA_LRB_RAW_RESPONSE_BYTES = 4 * 1024 * 1024
"""Maximum response body accepted by the bounded LRB parser."""
MAX_SINA_LRB_JSON_DEPTH = 32
"""Maximum JSON object/array nesting accepted before decoding."""
MAX_SINA_LRB_REPORTS = 256
"""Maximum entries accepted in either source report collection."""
MAX_SINA_LRB_ROWS_PER_REPORT = 512
"""Maximum raw rows accepted in one report-list entry."""
MAX_SINA_LRB_TOTAL_ROWS = 65_536
"""Maximum raw rows accepted across the complete response."""

_COUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z", re.ASCII)

_JsonScalar: TypeAlias = str | int | Decimal | bool | None
"""JSON scalar with decimal numbers retained without binary-float rounding."""


@dataclass(frozen=True, slots=True)
class SinaLrbRawObject:
    """JSON object represented as ordered source key/value pairs."""

    fields: tuple[tuple[str, SinaLrbRawValue], ...]


@dataclass(frozen=True, slots=True)
class SinaLrbRawArray:
    """JSON array represented as an ordered immutable sequence."""

    items: tuple[SinaLrbRawValue, ...]


SinaLrbRawValue: TypeAlias = _JsonScalar | SinaLrbRawObject | SinaLrbRawArray
"""Recursively frozen JSON values; object field and array order are retained."""


@dataclass(frozen=True, slots=True)
class SinaLrbRawReport:
    """One report-list entry, retaining its map key and report-local row order.

    ``report_list_key`` identifies only the key in the upstream ``report_list``
    object. The parser does not assert that it is the same concept as, or maps
    to, any item in the separate ``report_date`` array.
    """

    report_list_key: str
    source_ordinal: int
    source_fields: SinaLrbRawObject
    rows: tuple[SinaLrbRawObject, ...]


@dataclass(frozen=True, slots=True)
class SinaLrbRawResponse:
    """Unnormalized response with independent, source-ordered collections."""

    source_document: SinaLrbRawObject
    report_dates: tuple[SinaLrbRawObject, ...]
    reports: tuple[SinaLrbRawReport, ...]


class SinaLrbWireError(ValueError):
    """Fixed safe error for malformed, unsupported, or oversized LRB wire data."""

    def __init__(self, code: str) -> None:
        """Create an error without retaining or exposing response content."""
        allowed_codes = {
            "invalid_json",
            "invalid_envelope",
            "invalid_identity",
            "limit_exceeded",
            "parser_failed",
        }
        self.code = code if code in allowed_codes else "invalid_envelope"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class _ObjectPairs:
    """Internal ordered JSON object produced by ``object_pairs_hook``."""

    pairs: tuple[tuple[str, object], ...]


class _DuplicateJsonKeyError(ValueError):
    """Internal signal for a JSON object with ambiguous duplicate keys."""


class _DepthLimitExceededError(ValueError):
    """Internal signal for excessive structural nesting."""


def parse_sina_lrb_response(body: bytes) -> SinaLrbRawResponse:
    """Parse a bounded Sina LRB response while retaining source structure.

    The ``report_date`` array and ``report_list`` object are retained as
    independent ordered collections. No date association, financial meaning,
    unit, currency, publication time, instrument identity, or completeness is
    inferred. Every decoded source field remains available in
    ``source_document``. JSON values are not the original wire lexemes; keep the
    separate raw artifact for exact bytes. The 4 MiB input cap limits wire size,
    while full JSON materialization can use more memory before row limits apply.

    Args:
        body: Exact UTF-8 JSON response bytes.

    Returns:
        The complete frozen source document, ordered report-date objects, and
        report-list entries with their original row order.

    Raises:
        SinaLrbWireError: If JSON, envelope shape, collection identity, or
            configured size/count bounds are invalid, or an unexpected parser
            failure occurs.
    """
    error_code: str | None = None
    process_signal: BaseException | None = None
    response: SinaLrbRawResponse | None = None
    try:
        response = _parse_sina_lrb_response(body)
    except _ParseAbortError as error:
        error_code = error.code
        _clear_exception_details(error)
    except Exception as error:
        # Keep unexpected parser failures value-free at the public boundary.
        error_code = "parser_failed"
        _clear_exception_details(error)
    except BaseException as error:
        # Preserve process signals without retaining the parser's raw-body frame.
        _clear_exception_details(error)

        process_signal = error

    if process_signal is not None:
        del body
        raise process_signal.with_traceback(None) from None

    if error_code is not None or response is None:
        del body
        raise SinaLrbWireError(error_code or "invalid_json") from None
    return response


def _parse_sina_lrb_response(body: bytes) -> SinaLrbRawResponse:
    """Decode and validate one response; failures use a private code-only signal."""
    if type(body) is not bytes:
        _raise_wire("invalid_json")
    if len(body) > MAX_SINA_LRB_RAW_RESPONSE_BYTES:
        _raise_wire("limit_exceeded")
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _raise_wire("invalid_json")
    try:
        _check_json_depth(text)
        parsed: object = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_float=Decimal,
            parse_constant=_reject_json_constant,
        )
    except _DepthLimitExceededError:
        _raise_wire("limit_exceeded")
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        _raise_wire("invalid_json")

    document = _freeze_json(parsed)
    del parsed
    root = _as_object(document)
    if root is None:
        _raise_wire("invalid_envelope")
    result = _as_object(_field(root, "result"))
    if result is None:
        _raise_wire("invalid_envelope")
    status = _as_object(_field(result, "status"))
    data = _as_object(_field(result, "data"))
    if status is None or data is None:
        _raise_wire("invalid_envelope")
    status_code = _field(status, "code")
    if type(status_code) is not int or status_code != 0:
        _raise_wire("invalid_envelope")

    raw_count = _field(data, "report_count")
    if type(raw_count) is not str or _COUNT_PATTERN.fullmatch(raw_count) is None:
        _raise_wire("invalid_envelope")
    if len(raw_count) > len(str(MAX_SINA_LRB_REPORTS)):
        _raise_wire("limit_exceeded")
    expected_count = int(raw_count)
    if expected_count > MAX_SINA_LRB_REPORTS:
        _raise_wire("limit_exceeded")

    raw_dates = _as_array(_field(data, "report_date"))
    raw_report_list = _as_object(_field(data, "report_list"))
    if raw_dates is None or raw_report_list is None:
        _raise_wire("invalid_envelope")
    if (
        len(raw_dates.items) > MAX_SINA_LRB_REPORTS
        or len(raw_report_list.fields) > MAX_SINA_LRB_REPORTS
    ):
        _raise_wire("limit_exceeded")
    if len(raw_dates.items) != expected_count or len(raw_report_list.fields) != expected_count:
        _raise_wire("invalid_identity")

    report_dates: list[SinaLrbRawObject] = []
    for value in raw_dates.items:
        period = _as_object(value)
        if period is None:
            _raise_wire("invalid_envelope")
        report_dates.append(period)

    reports: list[SinaLrbRawReport] = []
    total_rows = 0
    for ordinal, (report_key, report_value) in enumerate(raw_report_list.fields):
        report = _as_object(report_value)
        if report is None:
            _raise_wire("invalid_envelope")
        raw_rows = _as_array(_field(report, "data"))
        if raw_rows is None:
            _raise_wire("invalid_envelope")
        if len(raw_rows.items) > MAX_SINA_LRB_ROWS_PER_REPORT:
            _raise_wire("limit_exceeded")
        total_rows += len(raw_rows.items)
        if total_rows > MAX_SINA_LRB_TOTAL_ROWS:
            _raise_wire("limit_exceeded")
        rows: list[SinaLrbRawObject] = []
        for row in raw_rows.items:
            row_object = _as_object(row)
            if row_object is None:
                _raise_wire("invalid_envelope")
            rows.append(row_object)
        reports.append(
            SinaLrbRawReport(
                report_list_key=report_key,
                source_ordinal=ordinal,
                source_fields=report,
                rows=tuple(rows),
            )
        )

    return SinaLrbRawResponse(
        source_document=root,
        report_dates=tuple(report_dates),
        reports=tuple(reports),
    )


class _ParseAbortError(Exception):
    """Internal code-only failure that is sanitized before reaching callers."""

    def __init__(self, code: str) -> None:
        """Create a private failure containing only a stable public code."""
        self.code = code
        super().__init__(code)


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> _ObjectPairs:
    """Retain JSON object order and reject ambiguous repeated member names."""
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise _DuplicateJsonKeyError
        seen.add(key)
    return _ObjectPairs(tuple(pairs))


def _reject_json_constant(value: str) -> NoReturn:
    """Reject JavaScript-style non-finite values outside the JSON standard."""
    del value
    raise ValueError("non-standard JSON constant")


def _freeze_json(value: object) -> SinaLrbRawValue:
    """Convert decoded JSON into recursively immutable, order-preserving values."""
    if isinstance(value, _ObjectPairs):
        return SinaLrbRawObject(
            fields=tuple((key, _freeze_json(item)) for key, item in value.pairs)
        )
    if isinstance(value, list):
        return SinaLrbRawArray(items=tuple(_freeze_json(item) for item in value))
    if value is None:
        return value
    if type(value) is str:
        return value
    if type(value) is int:
        return value
    if type(value) is bool:
        return value
    if isinstance(value, Decimal):
        return value
    _raise_wire("invalid_json")


def _check_json_depth(text: str) -> None:
    """Reject excess JSON nesting without counting delimiters inside strings."""
    depth = 0
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
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_SINA_LRB_JSON_DEPTH:
                raise _DepthLimitExceededError
        elif character in "]}":
            depth -= 1


def _as_object(value: SinaLrbRawValue) -> SinaLrbRawObject | None:
    """Return an object value without coercing another JSON type."""
    return value if isinstance(value, SinaLrbRawObject) else None


def _as_array(value: SinaLrbRawValue) -> SinaLrbRawArray | None:
    """Return an array value without coercing another JSON type."""
    return value if isinstance(value, SinaLrbRawArray) else None


def _field(value: SinaLrbRawObject, name: str) -> SinaLrbRawValue:
    """Find one required field in an ordered object or raise a safe wire error."""
    for key, item in value.fields:
        if key == name:
            return item
    _raise_wire("invalid_envelope")


def _raise_wire(code: str) -> NoReturn:
    """Raise an internal code-only signal for the public wrapper to sanitize."""
    raise _ParseAbortError(code)


def _clear_exception_details(error: BaseException) -> None:
    """Drop traceback and chained exceptions that could retain response data."""
    error.__traceback__ = None
    error.__cause__ = None
    error.__context__ = None
