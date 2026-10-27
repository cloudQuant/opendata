"""Decode caller-supplied Fuyao financial JSON into source-native observations.

The adapter accepts the exact response body bytes supplied by its caller. It
does not perform I/O or retain those bytes. Each emitted item carries the
body digest and JSON Pointer needed to resolve its source value if the caller
archives the body. Caller-supplied unit declarations are required because the
decoder must not infer an item's unit from its name or another field.

Only invalid source disclosure dates are row-quarantined. Structural,
identity, period, unit-declaration, and numeric errors fail the whole decode;
silently returning a partial financial statement would make completeness
ambiguous. This adapter does not archive raw responses or reconstruct an
unarchived historical point-in-time value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, TypeGuard
from zoneinfo import ZoneInfo

from opendata.data.financial_observation import (
    FinancialJsonScalar,
    FinancialStatementObservation,
    Periodicity,
    StatementType,
)
from opendata.data.mapping import require_pivot

#: Maximum UTF-8 response size accepted by the in-memory decoder.
MAX_BODY_BYTES = 8 * 1024 * 1024
#: Maximum wide financial rows accepted from one measured response.
MAX_FINANCIAL_ROWS = 64
MAX_ROW_FIELDS = 256
MAX_REQUEST_ID_CHARS = 256
MAX_JSON_NUMBER_TOKEN_CHARS = 128

FinancialDecodeErrorCode = Literal[
    "body_type_invalid",
    "body_empty",
    "body_too_large",
    "body_encoding_invalid",
    "json_invalid",
    "json_duplicate_key",
    "json_nonfinite_number",
    "json_number_too_long",
    "envelope_invalid",
    "upstream_error",
    "rows_too_many",
    "rows_empty",
    "row_invalid",
    "row_identity_invalid",
    "row_currency_invalid",
    "row_period_invalid",
    "row_field_missing",
    "row_value_invalid",
    "schema_drift",
    "native_units_invalid",
    "unit_mapping_declaration_invalid",
    "observed_at_invalid",
    "context_invalid",
    "observation_invalid",
    "duplicate_observation_key",
]
"""Stable, non-sensitive decoder error categories."""

QuarantineReason = Literal[
    "disclosure_date_missing",
    "disclosure_date_invalid",
    "disclosure_date_after_observed_at",
]

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_INSTRUMENT_PATTERN = re.compile(r"^(?P<ticker>[0-9]{6})\.(?P<exchange>SH|SZ|BJ)$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STATEMENT_TYPES = frozenset({"income", "balance", "cashflow"})
_PERIODICITIES = frozenset({"annual", "quarterly"})
_PERIOD_ENDS: dict[str, tuple[int, int]] = {
    "Q1": (3, 31),
    "Q2": (6, 30),
    "Q3": (9, 30),
    "Q4": (12, 31),
    "FY": (12, 31),
}
_ENVELOPE_REQUIRED = frozenset({"code", "message", "request_id", "data"})
_DATA_REQUIRED = frozenset({"timestamp", "item"})
_ROW_OPTIONAL_METADATA_FIELDS = frozenset({"report_date_ms"})
_ROW_REQUIRED = frozenset(
    {
        "thscode",
        "ticker",
        "period",
        "fiscal_year",
        "fiscal_period",
        "period_end_ms",
        "currency",
    }
)


class FinancialObservationDecodeError(ValueError):
    """Safe, stable error for malformed caller-supplied Fuyao JSON."""

    def __init__(self, code: FinancialDecodeErrorCode, pointer: str | None = None) -> None:
        """Create an error without embedding source values or response text.

        Args:
            code: Stable error category.
            pointer: Optional pointer to a known field path in the supplied body.
        """
        self.code = code
        self.pointer = pointer
        message = code if pointer is None else f"{code} at {pointer}"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class FinancialObservationSourceReference:
    """Resolve one decoded value to its source row and exact JSON field.

    Attributes:
        observation: Validated source-native financial observation.
        body_sha256: SHA-256 of the caller-supplied response bytes.
        row_pointer: RFC 6901 pointer to the original ``data.item`` row.
        value_pointer: RFC 6901 pointer to the source field carrying the value.
    """

    observation: FinancialStatementObservation
    body_sha256: str
    row_pointer: str
    value_pointer: str


@dataclass(frozen=True, slots=True)
class QuarantinedFinancialObservationRow:
    """Reference a row whose source disclosure date cannot support PIT use.

    Attributes:
        body_sha256: SHA-256 of the caller-supplied response bytes.
        row_pointer: RFC 6901 pointer to the original ``data.item`` row.
        disclosure_pointer: Pointer to the date field, or ``None`` if absent.
        reason: Stable quarantine reason, without copying source content.
    """

    body_sha256: str
    row_pointer: str
    disclosure_pointer: str | None
    reason: QuarantineReason


@dataclass(frozen=True, slots=True)
class FinancialObservationDecodeResult:
    """Immutable in-memory result of decoding one response body.

    Attributes:
        body_sha256: SHA-256 of the exact caller-supplied bytes.
        source_request_id: Request identifier echoed by the source response.
        native_unit_mapping_version_declared: Unit mapping version declared by
            the caller; the decoder does not validate its provenance or meaning.
        native_unit_mapping_sha256_declared: Unit mapping digest declared by
            the caller; the decoder validates only its lowercase SHA-256 shape.
        observations: Source values with row-level and field-level pointers.
        quarantined_rows: Rows withheld because their source disclosure date is unusable.
    """

    body_sha256: str
    source_request_id: str
    native_unit_mapping_version_declared: str
    native_unit_mapping_sha256_declared: str
    observations: tuple[FinancialObservationSourceReference, ...]
    quarantined_rows: tuple[QuarantinedFinancialObservationRow, ...]


class _JsonInputError(ValueError):
    """Internal parser failure that never carries a source token."""

    def __init__(self, code: FinancialDecodeErrorCode) -> None:
        self.code = code
        super().__init__(code)


def decode_financial_observation_body(
    raw_body: bytes,
    *,
    expected_instrument_id: str,
    statement_type: StatementType,
    periodicity: Periodicity,
    observed_at: datetime,
    native_units: Mapping[str, str],
    native_unit_mapping_version_declared: str,
    native_unit_mapping_sha256_declared: str,
) -> FinancialObservationDecodeResult:
    """Decode a real-shape Fuyao financial response without performing I/O.

    The response must have the Fuyao envelope shape ``code/message/request_id/data``;
    financial rows are wide mappings under ``data.item``. ``parse_float=Decimal``
    preserves JSON fractional tokens exactly. Each configured source line item
    becomes one immutable :class:`FinancialStatementObservation` with the source
    row's scalar metadata and a pointer to its exact value token.

    Args:
        raw_body: Exact UTF-8 JSON response bytes already obtained by the caller.
        expected_instrument_id: Exchange-qualified issuer requested from Fuyao.
        statement_type: Requested Fuyao statement family.
        periodicity: Requested ``annual`` or ``quarterly`` source query mode.
        observed_at: Actual, timezone-aware retrieval instant supplied by the caller.
        native_units: Caller-supplied unit declaration for every item in the selected
            mapping group. The decoder checks exact item coverage and token syntax,
            but cannot verify semantic correctness or review provenance. No unit is
            inferred from ``currency`` or ``item`` names.
        native_unit_mapping_version_declared: Caller-provided version label for
            the full unit mapping. This string is retained as a declaration only;
            the decoder does not verify its provenance or semantic correctness.
        native_unit_mapping_sha256_declared: Caller-provided lowercase SHA-256
            digest for the full unit mapping. The decoder checks its shape and
            retains the declaration but does not recompute or validate the digest.

    Returns:
        Immutable item observations and row references quarantined for unusable
        source disclosure dates.

    Raises:
        FinancialObservationDecodeError: If the bytes, envelope, row shape, exact
            values, source identity, period, units, or observation time are invalid.

    Notes:
        This function neither retains the body bytes nor persists the returned
        references. Durable raw capture and source request lifecycle belong to its
        caller. Values describe only what this call observed; no historical PIT
        series is reconstructed.
    """
    body_sha256 = _validate_body(raw_body)
    observed_utc = _normalize_observed_at(observed_at)
    if type(statement_type) is not str or statement_type not in _STATEMENT_TYPES:
        raise FinancialObservationDecodeError("context_invalid")
    if type(periodicity) is not str or periodicity not in _PERIODICITIES:
        raise FinancialObservationDecodeError("context_invalid")
    if (
        type(expected_instrument_id) is not str
        or _INSTRUMENT_PATTERN.fullmatch(expected_instrument_id) is None
    ):
        raise FinancialObservationDecodeError("context_invalid")

    financial_mapping = require_pivot("ths", "financial_statement")
    items = financial_mapping.item_columns(statement_type)
    disclosure_column = financial_mapping.row_column("announce_date")
    if disclosure_column not in _ROW_OPTIONAL_METADATA_FIELDS:
        raise FinancialObservationDecodeError("schema_drift")
    checked_units = _validate_native_units(native_units, items)
    _validate_unit_mapping_declaration(
        native_unit_mapping_version_declared,
        native_unit_mapping_sha256_declared,
    )
    payload = _parse_response(raw_body)
    source_request_id, data_timestamp_ms, rows = _response_rows(payload)

    references: list[FinancialObservationSourceReference] = []
    quarantined: list[QuarantinedFinancialObservationRow] = []
    seen_keys: set[tuple[str, str, str, str, date, str]] = set()
    latest_period_end_ms: int | None = None
    item_columns = frozenset(items)

    for row_index, raw_row in enumerate(rows):
        row_pointer = _pointer("data", "item", str(row_index))
        row, report_period = _validate_row(
            raw_row,
            row_pointer,
            items,
            periodicity,
            expected_instrument_id,
        )
        period_end_ms = row["period_end_ms"]
        if type(period_end_ms) is not int:
            raise FinancialObservationDecodeError("row_period_invalid", row_pointer)
        latest_period_end_ms = (
            period_end_ms
            if latest_period_end_ms is None
            else max(latest_period_end_ms, period_end_ms)
        )
        disclosure_pointer = _pointer("data", "item", str(row_index), disclosure_column)
        disclosure_date, quarantine_reason = _disclosure_date(
            row, report_period, observed_utc, disclosure_column
        )
        if quarantine_reason is not None:
            quarantined.append(
                QuarantinedFinancialObservationRow(
                    body_sha256=body_sha256,
                    row_pointer=row_pointer,
                    disclosure_pointer=(disclosure_pointer if disclosure_column in row else None),
                    reason=quarantine_reason,
                )
            )
            continue
        if disclosure_date is None:
            raise FinancialObservationDecodeError("row_invalid", disclosure_pointer)

        instrument_id = row["thscode"]
        if type(instrument_id) is not str:
            raise FinancialObservationDecodeError("row_invalid", row_pointer)

        for item_code in items:
            value_pointer = _pointer("data", "item", str(row_index), item_code)
            raw_value = row[item_code]
            if not _is_financial_value(raw_value):
                raise FinancialObservationDecodeError("row_value_invalid", value_pointer)
            natural_key = (
                "ths_fuyao",
                instrument_id,
                statement_type,
                periodicity,
                report_period,
                item_code,
            )
            if natural_key in seen_keys:
                raise FinancialObservationDecodeError("duplicate_observation_key", value_pointer)
            seen_keys.add(natural_key)
            metadata = tuple(
                (key, value)
                for key, value in row.items()
                if key not in item_columns and _is_json_scalar(value)
            )
            try:
                observation = FinancialStatementObservation(
                    instrument_id=instrument_id,
                    statement_type=statement_type,
                    periodicity=periodicity,
                    report_period=report_period,
                    source_disclosure_date=disclosure_date,
                    observed_at=observed_utc,
                    source_id="ths_fuyao",
                    item_code=item_code,
                    value=raw_value,
                    native_unit=checked_units[item_code],
                    source_metadata=metadata,
                )
            except (TypeError, ValueError, OverflowError):
                raise FinancialObservationDecodeError(
                    "observation_invalid", value_pointer
                ) from None
            references.append(
                FinancialObservationSourceReference(
                    observation=observation,
                    body_sha256=body_sha256,
                    row_pointer=row_pointer,
                    value_pointer=value_pointer,
                )
            )

    if rows and data_timestamp_ms != latest_period_end_ms:
        raise FinancialObservationDecodeError("envelope_invalid", "/data/timestamp")

    references.sort(
        key=lambda reference: (
            reference.observation.report_period,
            reference.observation.item_code,
            reference.row_pointer,
        )
    )
    return FinancialObservationDecodeResult(
        body_sha256=body_sha256,
        source_request_id=source_request_id,
        native_unit_mapping_version_declared=native_unit_mapping_version_declared,
        native_unit_mapping_sha256_declared=native_unit_mapping_sha256_declared,
        observations=tuple(references),
        quarantined_rows=tuple(quarantined),
    )


def _validate_body(raw_body: bytes) -> str:
    """Require bounded exact bytes and compute their content reference."""
    if type(raw_body) is not bytes:
        raise FinancialObservationDecodeError("body_type_invalid")
    if not raw_body:
        raise FinancialObservationDecodeError("body_empty")
    if len(raw_body) > MAX_BODY_BYTES:
        raise FinancialObservationDecodeError("body_too_large")
    return hashlib.sha256(raw_body).hexdigest()


def _normalize_observed_at(observed_at: datetime) -> datetime:
    """Require the actual caller-supplied aware retrieval instant and normalize UTC."""
    if type(observed_at) is not datetime:
        raise FinancialObservationDecodeError("observed_at_invalid")
    try:
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise FinancialObservationDecodeError("observed_at_invalid")
        return observed_at.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError):
        raise FinancialObservationDecodeError("observed_at_invalid") from None


def _parse_response(raw_body: bytes) -> object:
    """Parse strict UTF-8 JSON with duplicate-key and non-finite guards."""
    try:
        text = raw_body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise FinancialObservationDecodeError("body_encoding_invalid") from None
    try:
        return json.loads(
            text,
            parse_float=_parse_float,
            parse_int=_parse_int,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except _JsonInputError as exc:
        raise FinancialObservationDecodeError(exc.code) from None
    except (json.JSONDecodeError, RecursionError, ValueError):
        raise FinancialObservationDecodeError("json_invalid") from None


def _parse_float(token: str) -> Decimal:
    """Parse a bounded JSON fractional number as an exact decimal."""
    if len(token) > MAX_JSON_NUMBER_TOKEN_CHARS:
        raise _JsonInputError("json_number_too_long")
    try:
        parsed = Decimal(token)
    except InvalidOperation:
        raise _JsonInputError("json_invalid") from None
    if not parsed.is_finite():
        raise _JsonInputError("json_nonfinite_number")
    return parsed


def _parse_int(token: str) -> int:
    """Parse a bounded JSON integer without accepting enormous tokens."""
    if len(token) > MAX_JSON_NUMBER_TOKEN_CHARS:
        raise _JsonInputError("json_number_too_long")
    try:
        return int(token)
    except ValueError:
        raise _JsonInputError("json_invalid") from None


def _reject_constant(_token: str) -> None:
    """Reject JavaScript-style NaN and Infinity constants accepted by json.loads."""
    raise _JsonInputError("json_nonfinite_number")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object only when every JSON key occurs exactly once."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonInputError("json_duplicate_key")
        result[key] = value
    return result


def _validate_native_units(
    native_units: Mapping[str, str], items: tuple[str, ...]
) -> dict[str, str]:
    """Require explicit, complete unit declarations for the selected item group."""
    if not isinstance(native_units, Mapping):
        raise FinancialObservationDecodeError("native_units_invalid")
    try:
        supplied = dict(native_units.items())
    except Exception:
        raise FinancialObservationDecodeError("native_units_invalid") from None
    if set(supplied) != set(items):
        raise FinancialObservationDecodeError("native_units_invalid")
    for item_code, native_unit in supplied.items():
        if (
            type(item_code) is not str
            or type(native_unit) is not str
            or not native_unit
            or native_unit != native_unit.strip()
            or len(native_unit) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in native_unit)
            or not _is_utf8_encodable(native_unit)
        ):
            raise FinancialObservationDecodeError("native_units_invalid")
    return supplied


def _validate_unit_mapping_declaration(version: str, digest: str) -> None:
    """Validate only the caller declaration's bounded syntax, not its meaning."""
    if (
        type(version) is not str
        or not version
        or version != version.strip()
        or len(version) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in version)
        or not _is_utf8_encodable(version)
        or type(digest) is not str
        or _SHA256_PATTERN.fullmatch(digest) is None
    ):
        raise FinancialObservationDecodeError("unit_mapping_declaration_invalid")


def _response_rows(
    payload: object,
) -> tuple[str, int | None, tuple[dict[str, object], ...]]:
    """Require the measured Fuyao response envelope and bounded wide row list."""
    if type(payload) is not dict or not _ENVELOPE_REQUIRED.issubset(payload):
        raise FinancialObservationDecodeError("envelope_invalid")
    code = payload["code"]
    if type(code) is not int:
        raise FinancialObservationDecodeError("envelope_invalid")
    if code != 0:
        raise FinancialObservationDecodeError("upstream_error")
    message = payload["message"]
    request_id = payload["request_id"]
    if type(message) is not str or type(request_id) is not str:
        raise FinancialObservationDecodeError("envelope_invalid")
    if (
        not request_id
        or request_id != request_id.strip()
        or len(request_id) > MAX_REQUEST_ID_CHARS
        or any(ord(char) < 32 or ord(char) == 127 for char in request_id)
        or not _is_utf8_encodable(request_id)
    ):
        raise FinancialObservationDecodeError("envelope_invalid", "/request_id")
    data = payload["data"]
    if type(data) is not dict or not _DATA_REQUIRED.issubset(data):
        raise FinancialObservationDecodeError("envelope_invalid")
    timestamp = data["timestamp"]
    if timestamp is not None and type(timestamp) is not int:
        raise FinancialObservationDecodeError("envelope_invalid", "/data/timestamp")
    raw_rows = data["item"]
    if type(raw_rows) is not list:
        raise FinancialObservationDecodeError("envelope_invalid", "/data/item")
    if len(raw_rows) > MAX_FINANCIAL_ROWS:
        raise FinancialObservationDecodeError("rows_too_many", "/data/item")
    if not raw_rows:
        raise FinancialObservationDecodeError("rows_empty", "/data/item")
    rows: list[dict[str, object]] = []
    for row_index, row in enumerate(raw_rows):
        if type(row) is not dict:
            raise FinancialObservationDecodeError(
                "row_invalid", _pointer("data", "item", str(row_index))
            )
        rows.append(row)
    return request_id, timestamp, tuple(rows)


def _is_utf8_encodable(value: str) -> bool:
    """Reject strings containing unpaired surrogates before they enter output."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    return True


def _validate_row(
    raw_row: dict[str, object],
    row_pointer: str,
    items: tuple[str, ...],
    periodicity: Periodicity,
    expected_instrument_id: str,
) -> tuple[dict[str, object], date]:
    """Validate the measured THS wide row and derive only its fiscal period."""
    if len(raw_row) > MAX_ROW_FIELDS:
        raise FinancialObservationDecodeError("row_invalid", row_pointer)
    if not _ROW_REQUIRED.issubset(raw_row):
        missing = next(iter(sorted(_ROW_REQUIRED.difference(raw_row))))
        raise FinancialObservationDecodeError(
            "row_field_missing", _pointer("data", "item", row_pointer.rsplit("/", 1)[-1], missing)
        )
    for item_code in items:
        if item_code not in raw_row:
            row_index = row_pointer.rsplit("/", 1)[-1]
            raise FinancialObservationDecodeError(
                "row_field_missing", _pointer("data", "item", row_index, item_code)
            )
    for key, value in raw_row.items():
        if type(key) is not str or not _is_json_scalar(value):
            raise FinancialObservationDecodeError("row_invalid", row_pointer)
    allowed_fields = _ROW_REQUIRED.union(items, _ROW_OPTIONAL_METADATA_FIELDS)
    if set(raw_row).difference(allowed_fields):
        raise FinancialObservationDecodeError("schema_drift", row_pointer)

    thscode = raw_row["thscode"]
    ticker = raw_row["ticker"]
    currency = raw_row["currency"]
    if type(thscode) is not str or type(ticker) is not str:
        raise FinancialObservationDecodeError("row_identity_invalid", row_pointer)
    if type(currency) is not str or currency != "CNY":
        raise FinancialObservationDecodeError(
            "row_currency_invalid", _pointer(*row_pointer.strip("/").split("/"), "currency")
        )
    match = _INSTRUMENT_PATTERN.fullmatch(thscode)
    if match is None or ticker != match.group("ticker") or thscode != expected_instrument_id:
        raise FinancialObservationDecodeError("row_identity_invalid", row_pointer)
    if raw_row["period"] != periodicity:
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)

    year = raw_row["fiscal_year"]
    fiscal_period = raw_row["fiscal_period"]
    if type(year) is not int or not 1 <= year <= 9999 or type(fiscal_period) is not str:
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)
    if fiscal_period not in _PERIOD_ENDS:
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)
    if periodicity == "annual" and fiscal_period != "FY":
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)
    if periodicity == "quarterly" and fiscal_period == "FY":
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)

    month, day = _PERIOD_ENDS[fiscal_period]
    try:
        report_period = date(year, month, day)
        stamped_period = _shanghai_date_from_millis(raw_row["period_end_ms"])
    except (OverflowError, OSError, TypeError, ValueError):
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer) from None
    if stamped_period != report_period:
        raise FinancialObservationDecodeError("row_period_invalid", row_pointer)

    return raw_row, report_period


def _disclosure_date(
    row: Mapping[str, object],
    report_period: date,
    observed_at: datetime,
    disclosure_column: str,
) -> tuple[date | None, QuarantineReason | None]:
    """Parse the source disclosure timestamp or return a safe quarantine reason."""
    if disclosure_column not in row:
        return None, "disclosure_date_missing"
    try:
        disclosed = _shanghai_date_from_millis(row[disclosure_column])
    except (OverflowError, OSError, TypeError, ValueError):
        return None, "disclosure_date_invalid"
    if disclosed < report_period:
        return None, "disclosure_date_invalid"
    if disclosed > observed_at.astimezone(_SHANGHAI).date():
        return None, "disclosure_date_after_observed_at"
    return disclosed, None


def _shanghai_date_from_millis(value: object) -> date:
    """Interpret an exact epoch-millisecond value at Shanghai-local midnight."""
    if type(value) is not int:
        raise TypeError("timestamp must be an integer")
    instant = _UTC_EPOCH + timedelta(milliseconds=value)
    local = instant.astimezone(_SHANGHAI)
    if local.timetz().replace(tzinfo=None) != time.min:
        raise ValueError("timestamp must represent Shanghai midnight")
    return local.date()


def _is_json_scalar(value: object) -> TypeGuard[FinancialJsonScalar]:
    """Recognize exact JSON scalar values returned by the strict parser."""
    return value is None or type(value) in {bool, int, str} or isinstance(value, Decimal)


def _is_financial_value(value: object) -> TypeGuard[Decimal | int | str | None]:
    """Recognize exact source value forms while excluding booleans and floats."""
    return value is None or isinstance(value, Decimal) or type(value) in {int, str}


def _pointer(*segments: str) -> str:
    """Build an RFC 6901 JSON Pointer from path segments."""
    escaped = (segment.replace("~", "~0").replace("/", "~1") for segment in segments)
    return "/" + "/".join(escaped)


__all__ = [
    "MAX_BODY_BYTES",
    "MAX_FINANCIAL_ROWS",
    "FinancialDecodeErrorCode",
    "FinancialObservationDecodeError",
    "FinancialObservationDecodeResult",
    "FinancialObservationSourceReference",
    "QuarantinedFinancialObservationRow",
    "decode_financial_observation_body",
]
