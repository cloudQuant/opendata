"""Strict source-native parser for Sina's recorded balance-sheet response."""

from __future__ import annotations

import json
import math
import re
from dataclasses import replace
from datetime import date, datetime
from typing import NoReturn
from zoneinfo import ZoneInfo

from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    MAX_SINA_FZB_JSON_DEPTH,
    MAX_SINA_FZB_REPORTS,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    MAX_SINA_FZB_TOTAL_ROWS,
    SINA_FZB_GROUP_DISPLAY,
    SINA_FZB_GROUP_DISPLAY_TYPE,
    SINA_FZB_GROUP_ITEM_PRECISION,
    SINA_FZB_GROUP_ITEM_SOURCE,
    SINA_FZB_GROUP_ITEM_TONGBI,
    SINA_FZB_GROUP_MARKERS,
    SINA_FZB_QUERY_TYPE,
    SINA_FZB_SOURCE_CODE,
    JsonScalar,
    SinaFzbEntry,
    SinaFzbGroupMarker,
    SinaFzbGroupNo,
    SinaFzbGroupTitle,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
    SinaFzbValueState,
    SinaFzbWireError,
    SinaFzbWireErrorCode,
)

__all__ = ["parse_sina_fzb_response"]

_DATE_PATTERN = re.compile(r"[0-9]{8}\Z")
_COUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REPORT_PROMOTED_FIELDS = frozenset({"publish_date", "update_time", "rCurrency", "data"})
_ROW_PROMOTED_FIELDS = frozenset({"item_field", "item_title", "item_value"})
_PERIOD_PROMOTED_FIELDS = frozenset({"date_value"})
_DATA_REQUIRED_FIELDS = frozenset({"report_count", "report_date", "report_list"})
_DATA_ECHO_FIELDS = frozenset({"paperCode", "source", "type"})


class _DuplicateJsonKeyError(ValueError):
    """Internal signal for ambiguous duplicate keys in a JSON object."""


class _DepthLimitExceededError(ValueError):
    """Internal signal for excessive JSON structural nesting."""


def parse_sina_fzb_response(body: bytes, *, query: SinaFzbQuery) -> tuple[SinaFzbReport, ...]:
    """Parse one complete Sina balance-sheet response without normalizing values.

    Args:
        body: Exact UTF-8 response body bytes.
        query: Explicit canonical symbol that identifies this request.

    Returns:
        Source-ordered reports with raw item strings, metadata, structural
        group markers, and source row ordinals preserved.

    Raises:
        SinaFzbWireError: If the query, JSON, envelope, identity, values,
            dates, duplicate rows, or configured response bounds are invalid.
    """
    _validate_query(query)
    if type(body) is not bytes:
        _raise_wire("invalid_json")
    if len(body) > MAX_RAW_RESPONSE_BYTES:
        _raise_wire("limit_exceeded")

    try:
        text = body.decode("utf-8", errors="strict")
        _check_json_depth(text)
        document: object = json.loads(
            text,
            object_pairs_hook=_object_without_duplicate_keys,
            parse_float=_finite_json_float,
            parse_constant=_reject_json_constant,
        )
    except _DepthLimitExceededError:
        _raise_wire("limit_exceeded")
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, ValueError):
        _raise_wire("invalid_json")

    root = _as_json_object(document)
    if root is None or set(root) != {"result"}:
        _raise_wire("invalid_envelope")
    result = _as_json_object(root["result"])
    if result is None or set(result) != {"status", "data"}:
        _raise_wire("invalid_envelope")
    status = _as_json_object(result["status"])
    if status is None or set(status) != {"code"}:
        _raise_wire("invalid_envelope")
    if type(status["code"]) is not int or status["code"] != 0:
        _raise_wire("invalid_envelope")

    data = _as_json_object(result["data"])
    if data is None or not _DATA_REQUIRED_FIELDS.issubset(data):
        _raise_wire("invalid_envelope")
    if set(data) - _DATA_REQUIRED_FIELDS - _DATA_ECHO_FIELDS:
        _raise_wire("invalid_envelope")

    paper_code = _paper_code(query.symbol)
    _validate_identity_echoes(data, paper_code=paper_code)
    period_entries = _as_json_list(data["report_date"])
    report_list = _as_json_object(data["report_list"])
    if report_list is None:
        _raise_wire("invalid_envelope")
    report_count = _report_count(data["report_count"])
    if report_count > MAX_SINA_FZB_REPORTS:
        _raise_wire("limit_exceeded")
    if report_count != len(period_entries) or report_count != len(report_list):
        _raise_wire("invalid_identity")

    ordered_periods: list[tuple[str, date, tuple[tuple[str, JsonScalar], ...]]] = []
    seen_dates: set[date] = set()
    for period_value in period_entries:
        period = _as_json_object(period_value)
        if period is None or "date_value" not in period:
            _raise_wire("invalid_envelope")
        date_value = _required_string(period, "date_value", code="invalid_date")
        report_period = _parse_yyyymmdd(date_value)
        if report_period in seen_dates:
            _raise_wire("invalid_identity")
        seen_dates.add(report_period)
        period_metadata = _metadata(period, excluded=_PERIOD_PROMOTED_FIELDS)
        ordered_periods.append((date_value, report_period, period_metadata))

    report_keys: set[str] = set()
    for key in report_list:
        if _DATE_PATTERN.fullmatch(key) is None:
            _raise_wire("invalid_date")
        _parse_yyyymmdd(key)
        report_keys.add(key)
    ordered_keys = {value for value, _, _ in ordered_periods}
    if report_keys != ordered_keys:
        _raise_wire("invalid_identity")

    total_rows = 0
    reports: list[SinaFzbReport] = []
    for date_value, report_period, period_metadata in ordered_periods:
        report_value = report_list[date_value]
        report = _as_json_object(report_value)
        if report is None:
            _raise_wire("invalid_envelope")
        published_text = _required_string(report, "publish_date", code="invalid_date")
        published_on = _parse_yyyymmdd(published_text)
        update_epoch = _required_epoch_seconds(report)
        try:
            source_updated_at = datetime.fromtimestamp(update_epoch, tz=_SHANGHAI)
        except (OSError, OverflowError, ValueError):
            _raise_wire("invalid_value")
        currency = _required_nonempty_string(report, "rCurrency")
        raw_rows = _as_json_list(report.get("data"))
        if len(raw_rows) > MAX_SINA_FZB_ROWS_PER_REPORT:
            _raise_wire("limit_exceeded")
        total_rows += len(raw_rows)
        if total_rows > MAX_SINA_FZB_TOTAL_ROWS:
            _raise_wire("limit_exceeded")

        entries = _parse_entries(raw_rows)
        report_metadata = _metadata(report, excluded=_REPORT_PROMOTED_FIELDS)
        reports.append(
            SinaFzbReport(
                symbol=query.symbol,
                source_paper_code=paper_code,
                statement_code="fzb",
                report_period=report_period,
                period_metadata=period_metadata,
                published_on=published_on,
                publication_precision="date",
                source_update_epoch_seconds=update_epoch,
                source_updated_at=source_updated_at,
                currency=currency,
                report_metadata=report_metadata,
                entries=entries,
            )
        )
    return tuple(reports)


def _validate_query(query: SinaFzbQuery) -> None:
    """Reject forged or noncanonical query values even if construction was bypassed."""
    if type(query) is not SinaFzbQuery or type(query.symbol) is not str:
        _raise_wire("invalid_query")
    if re.fullmatch(r"[0-9]{6}\.(?:SH|SZ)", query.symbol) is None:
        _raise_wire("invalid_query")


def _paper_code(symbol: str) -> str:
    """Translate a canonical exchange-qualified symbol to Sina's wire code."""
    code, separator, exchange = symbol.partition(".")
    if separator != "." or len(code) != 6 or exchange not in {"SH", "SZ"}:
        _raise_wire("invalid_query")
    return f"{exchange.lower()}{code}"


def _validate_identity_echoes(data: dict[str, object], *, paper_code: str) -> None:
    """Validate only the exact optional request-identity echo fields."""
    expected_values = {
        "paperCode": paper_code,
        "source": SINA_FZB_SOURCE_CODE,
        "type": SINA_FZB_QUERY_TYPE,
    }
    for key, expected in expected_values.items():
        if key in data:
            value = data[key]
            if type(value) is not str or value != expected:
                _raise_wire("invalid_identity")


def _parse_entries(raw_rows: list[object]) -> tuple[SinaFzbEntry, ...]:
    """Parse source rows, preserving ordered exact-duplicate ordinals."""
    entries: list[SinaFzbEntry] = []
    item_positions: dict[str, int] = {}
    item_fingerprints: dict[str, bytes] = {}
    marker_positions: dict[SinaFzbGroupNo, int] = {}
    marker_fingerprints: dict[SinaFzbGroupNo, bytes] = {}

    for ordinal, raw_value in enumerate(raw_rows):
        row = _as_json_object(raw_value)
        if row is None:
            _raise_wire("invalid_envelope")
        item_field = _required_string(row, "item_field", code="invalid_value")
        item_title = _required_nonempty_string(row, "item_title")
        if "item_value" not in row:
            _raise_wire("invalid_value")
        item_value = row["item_value"]
        if item_value is not None and type(item_value) is not str:
            _raise_wire("invalid_value")
        fingerprint = _canonical_row_fingerprint(row)
        if item_field == "":
            _group_marker(
                row,
                ordinal=ordinal,
                item_title=item_title,
                item_value=item_value,
                fingerprint=fingerprint,
                entries=entries,
                positions=marker_positions,
                fingerprints=marker_fingerprints,
            )
            continue
        if not item_field.strip():
            _raise_wire("invalid_value")

        source_metadata = _metadata(row, excluded=_ROW_PROMOTED_FIELDS)
        previous_position = item_positions.get(item_field)
        if previous_position is not None:
            if item_fingerprints[item_field] != fingerprint:
                _raise_wire("duplicate_conflict")
            previous = entries[previous_position]
            if not isinstance(previous, SinaFzbItem):
                _raise_wire("duplicate_conflict")
            entries[previous_position] = replace(
                previous,
                source_ordinals=(*previous.source_ordinals, ordinal),
            )
            continue

        value_state = _value_state(item_value)
        item_positions[item_field] = len(entries)
        item_fingerprints[item_field] = fingerprint
        entries.append(
            SinaFzbItem(
                source_item_id=item_field,
                source_title=item_title,
                source_ordinals=(ordinal,),
                raw_value=item_value,
                value_state=value_state,
                source_metadata=source_metadata,
            )
        )
    return tuple(entries)


def _group_marker(
    row: dict[str, object],
    *,
    ordinal: int,
    item_title: str,
    item_value: str | None,
    fingerprint: bytes,
    entries: list[SinaFzbEntry],
    positions: dict[SinaFzbGroupNo, int],
    fingerprints: dict[SinaFzbGroupNo, bytes],
) -> None:
    """Parse only exact recorded source group-heading rows."""
    if item_value != "":
        _raise_wire("invalid_value")
    group_number_value = row.get("item_group_no")
    if type(group_number_value) is not int:
        _raise_wire("invalid_value")
    group_number: SinaFzbGroupNo | None = None
    group_title: SinaFzbGroupTitle | None = None
    for expected_number, expected_title in SINA_FZB_GROUP_MARKERS:
        if group_number_value == expected_number and item_title == expected_title:
            group_number = expected_number
            group_title = expected_title
            break
    if group_number is None or group_title is None:
        _raise_wire("invalid_value")
    if (
        row.get("item_display_type") != SINA_FZB_GROUP_DISPLAY_TYPE
        or type(row.get("item_display_type")) is not int
        or row.get("item_display") != SINA_FZB_GROUP_DISPLAY
        or row.get("item_source") != SINA_FZB_GROUP_ITEM_SOURCE
        or row.get("item_precision") != SINA_FZB_GROUP_ITEM_PRECISION
        or row.get("item_tongbi") != SINA_FZB_GROUP_ITEM_TONGBI
    ):
        _raise_wire("invalid_value")
    source_metadata = _metadata(row, excluded=_ROW_PROMOTED_FIELDS)
    previous_position = positions.get(group_number)
    if previous_position is not None:
        if fingerprints[group_number] != fingerprint:
            _raise_wire("duplicate_conflict")
        previous = entries[previous_position]
        if not isinstance(previous, SinaFzbGroupMarker):
            _raise_wire("duplicate_conflict")
        entries[previous_position] = replace(
            previous,
            source_ordinals=(*previous.source_ordinals, ordinal),
        )
        return

    positions[group_number] = len(entries)
    fingerprints[group_number] = fingerprint
    marker = SinaFzbGroupMarker(
        source_ordinals=(ordinal,),
        source_group_no=group_number,
        source_title=group_title,
        raw_item_field="",
        raw_item_value="",
        source_metadata=source_metadata,
    )
    entries.append(marker)


def _value_state(value: str | None) -> SinaFzbValueState:
    """Retain the three distinct source states without numeric coercion."""
    if value is None:
        return "null"
    if value == "":
        return "empty"
    return "present"


def _metadata(
    row: dict[str, object], *, excluded: frozenset[str]
) -> tuple[tuple[str, JsonScalar], ...]:
    """Return sorted scalar metadata without coercing source types."""
    values: list[tuple[str, JsonScalar]] = []
    for key, value in row.items():
        _validate_utf8_text(key, code="invalid_value")
        if key in excluded:
            continue
        values.append((key, _json_scalar(value)))
    return tuple(sorted(values, key=lambda pair: pair[0]))


def _json_scalar(value: object) -> JsonScalar:
    """Validate and retain one JSON scalar in its original Python JSON type."""
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            _raise_wire("invalid_value")
        return value
    if type(value) is str:
        _validate_utf8_text(value, code="invalid_value")
        return value
    _raise_wire("invalid_envelope")


def _canonical_row_fingerprint(row: dict[str, object]) -> bytes:
    """Serialize a full raw row canonically for type-sensitive duplicate checks."""
    try:
        canonical = json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return canonical.encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        _raise_wire("invalid_value")


def _strict_decimal_date(value: object, *, code: SinaFzbWireErrorCode) -> str:
    """Return a byte-for-byte UTF-8 date string matching YYYYMMDD."""
    if type(value) is not str or _DATE_PATTERN.fullmatch(value) is None:
        _raise_wire(code)
    _validate_utf8_text(value, code=code)
    return value


def _parse_yyyymmdd(value: str) -> date:
    """Parse a canonical YYYYMMDD date and reject impossible calendar values."""
    canonical = _strict_decimal_date(value, code="invalid_date")
    try:
        return date(int(canonical[0:4]), int(canonical[4:6]), int(canonical[6:8]))
    except ValueError:
        _raise_wire("invalid_date")


def _report_count(value: object) -> int:
    """Parse the exact canonical decimal count from the Sina envelope."""
    if type(value) is not str or _COUNT_PATTERN.fullmatch(value) is None:
        _raise_wire("invalid_envelope")
    maximum = str(MAX_SINA_FZB_REPORTS)
    if len(value) > len(maximum) or (len(value) == len(maximum) and value > maximum):
        _raise_wire("limit_exceeded")
    try:
        return int(value)
    except ValueError:
        _raise_wire("invalid_envelope")


def _required_epoch_seconds(row: dict[str, object]) -> int:
    """Read a positive exact integer update epoch, rejecting bool and coercions."""
    value = row.get("update_time")
    if type(value) is not int or value <= 0:
        _raise_wire("invalid_value")
    return value


def _required_string(row: dict[str, object], key: str, *, code: SinaFzbWireErrorCode) -> str:
    """Read one required exact string field and validate its UTF-8 encoding."""
    if key not in row:
        _raise_wire("invalid_envelope")
    value = row[key]
    if type(value) is not str:
        _raise_wire(code)
    _validate_utf8_text(value, code=code)
    return value


def _required_nonempty_string(row: dict[str, object], key: str) -> str:
    """Read a required exact string field that is not empty."""
    value = _required_string(row, key, code="invalid_value")
    if value == "":
        _raise_wire("invalid_value")
    return value


def _validate_utf8_text(value: str, *, code: SinaFzbWireErrorCode) -> None:
    """Reject escaped lone surrogates that cannot be represented as UTF-8."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _raise_wire(code)


def _as_json_object(value: object) -> dict[str, object] | None:
    """Narrow an arbitrary JSON value to an object with exact string keys."""
    if type(value) is not dict:
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            return None
        _validate_utf8_text(key, code="invalid_json")
        result[key] = item
    return result


def _as_json_list(value: object) -> list[object]:
    """Require an exact JSON array and return it as object-typed values."""
    if type(value) is not list:
        _raise_wire("invalid_envelope")
    return value


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Construct a JSON object while refusing ambiguous repeated keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKeyError
        result[key] = value
    return result


def _finite_json_float(token: str) -> float:
    """Parse one JSON float while rejecting overflow to infinity."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError
    return value


def _reject_json_constant(token: str) -> NoReturn:
    """Reject Python JSON's nonstandard NaN and Infinity extensions."""
    del token
    raise ValueError


def _check_json_depth(text: str) -> None:
    """Bound JSON structural nesting without counting brackets inside strings."""
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
            if depth > MAX_SINA_FZB_JSON_DEPTH:
                raise _DepthLimitExceededError
        elif character in "]}":
            depth -= 1


def _raise_wire(code: SinaFzbWireErrorCode) -> NoReturn:
    """Raise a contract-owned sanitized parser error."""
    raise SinaFzbWireError(code)
