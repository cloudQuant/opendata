"""Read-only integrity checks for SQL-listed financial response captures.

The audit selects bounded metadata pages from ``od_financial_captures`` and
verifies each referenced manifest and response body with
``RawResponseArtifactReader``. It never walks the artifact directory, reads
unreferenced files, decodes source data, writes files, changes SQL, creates an
engine, or opens a network connection. Callers provide an already configured
SQLAlchemy connection, a private-root reader, an explicit source allowlist, a
trusted UTC upper time bound, and a page size.

Passing proves only that the selected SQL references agree with the locally
stored content at audit time. It does not prove source authenticity, clock
accuracy, collection or retention authorization, completeness of the SQL
capture list, historical coverage, backup durability, or absence of orphaned
files. A manifest digest is an integrity address, not a signature. Error
results contain only a validated capture id and a stable code; they never
contain response bytes, manifest text, paths, or exception messages.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, TypedDict, TypeGuard

from sqlalchemy import and_, select
from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError

from opendata.pipeline.financial_observation_store import CAPTURES
from opendata.pipeline.raw_evidence import RawResponseSource
from opendata.pipeline.raw_evidence_reader import (
    RawEvidenceContextMismatchError,
    RawEvidenceIntegrityError,
    RawEvidenceReadError,
    RawEvidenceReadInputError,
    RawEvidenceReadResult,
    RawResponseArtifactReader,
    UnsafeRawEvidenceError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.sql.elements import ColumnElement

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_SCOPES = 32
_MAX_PAGE_SIZE = 500
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024
_MAX_PAGE_EVIDENCE_BYTES = 17 * 1024 * 1024


class _CaptureRow(TypedDict):
    """Database values selected for one bounded integrity check."""

    capture_id: str
    provider_id: str
    dataset_id: str
    operation_id: str
    source_id: str
    observed_at_utc: datetime
    captured_at_utc: datetime
    http_status: int
    request_summary_sha256: str
    raw_object_sha256: str
    raw_object_size_bytes: int
    raw_object_key: str
    raw_manifest_sha256: str
    raw_manifest_size_bytes: int
    raw_manifest_key: str
    retention_class: str
    source_history_limit: str


AuditErrorCode = Literal[
    "capture_scope_mismatch",
    "invalid_sql_capture",
    "capture_manifest_mismatch",
    "evidence_context_mismatch",
    "evidence_integrity_failure",
    "evidence_unavailable_or_unsafe",
    "invalid_evidence_reference",
]


class FinancialCaptureIntegrityInputError(ValueError):
    """Raised when explicit audit inputs are invalid or unbounded."""


class FinancialCaptureIntegrityQueryError(RuntimeError):
    """Raised when the read-only capture query fails without exposing driver details."""


@dataclass(frozen=True, slots=True)
class FinancialCaptureIntegrityScope:
    """Authorize one source id and its exact manifest provider identity."""

    source_id: str
    raw_source: RawResponseSource


@dataclass(frozen=True, slots=True)
class FinancialCaptureIntegrityResult:
    """One capture's sanitized integrity result."""

    capture_id: str | None
    error_code: AuditErrorCode | None

    @property
    def valid(self) -> bool:
        """Return whether the selected capture's SQL and raw evidence agree."""
        return self.error_code is None


@dataclass(frozen=True, slots=True)
class FinancialCaptureIntegrityReport:
    """A bounded page of checks and its keyset pagination state.

    ``pagination_blocked`` means another SQL page exists, but its predecessor
    key is malformed and cannot be returned as a safe, bounded cursor. The
    caller must stop and investigate instead of treating the scan as complete.
    ``byte_budget_exhausted`` means the next capture was deferred because
    reserving its conservative reader limit would exceed this page's evidence
    byte budget. Its safe predecessor cursor lets the caller continue later.
    """

    results: tuple[FinancialCaptureIntegrityResult, ...]
    next_cursor: str | None
    has_more: bool
    pagination_blocked: bool
    evidence_bytes_reserved_upper_bound: int
    byte_budget_exhausted: bool

    @property
    def checked_count(self) -> int:
        """Return the number of captures examined in this page."""
        return len(self.results)

    @property
    def invalid_count(self) -> int:
        """Return the number of captures that failed an integrity check."""
        return sum(not result.valid for result in self.results)


def audit_financial_capture_integrity(
    connection: Connection,
    *,
    reader: RawResponseArtifactReader,
    scopes: tuple[FinancialCaptureIntegrityScope, ...],
    captured_at_not_after: datetime,
    page_size: int = 100,
    after_capture_id: str | None = None,
) -> FinancialCaptureIntegrityReport:
    """Check one bounded, explicitly scoped page of SQL-referenced raw evidence.

    The supplied connection is used only for a parameterized ``SELECT`` from
    ``od_financial_captures``. The caller owns its connection, read-only access
    policy, and transaction. The raw reader verifies each content-addressed
    manifest and response body without modifying the artifact store.

    Args:
        connection: Existing synchronous SQLAlchemy connection.
        reader: Existing reader bound to the permitted private artifact root.
        scopes: Non-empty allowlist pairing source ids with exact raw identities.
        captured_at_not_after: Caller-trusted, timezone-aware UTC upper bound.
        page_size: Maximum number of captures to examine, from 1 through 500.
        after_capture_id: Optional lowercase capture id for keyset pagination.

    Returns:
        Sanitized per-capture results and a cursor when another page exists.

    Raises:
        FinancialCaptureIntegrityInputError: Inputs fail validation or the page
            size exceeds its finite bound.
        TypeError: The connection or reader has an unsupported concrete type.
    """
    if not isinstance(connection, Connection):
        raise TypeError("connection must be an existing SQLAlchemy Connection")
    if type(reader) is not RawResponseArtifactReader:
        raise TypeError("reader must be the reviewed RawResponseArtifactReader type")
    checked_scopes = _validate_scopes(scopes)
    checked_bound = _validate_utc_bound(captured_at_not_after)
    reader_body_limit = _validated_reader_body_limit(reader)
    if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
        raise FinancialCaptureIntegrityInputError("page_size is outside the bounded range")
    if after_capture_id is not None and (
        type(after_capture_id) is not str or _CAPTURE_ID_PATTERN.fullmatch(after_capture_id) is None
    ):
        raise FinancialCaptureIntegrityInputError("after_capture_id is invalid")

    allowed_source_ids = tuple(sorted({scope.source_id for scope in checked_scopes}))
    predicates: list[ColumnElement[bool]] = [CAPTURES.c.source_id.in_(allowed_source_ids)]
    if after_capture_id is not None:
        predicates.append(CAPTURES.c.capture_id > after_capture_id)
    statement = (
        select(
            CAPTURES.c.capture_id,
            CAPTURES.c.provider_id,
            CAPTURES.c.dataset_id,
            CAPTURES.c.operation_id,
            CAPTURES.c.source_id,
            CAPTURES.c.observed_at_utc,
            CAPTURES.c.captured_at_utc,
            CAPTURES.c.http_status,
            CAPTURES.c.request_summary_sha256,
            CAPTURES.c.raw_object_sha256,
            CAPTURES.c.raw_object_size_bytes,
            CAPTURES.c.raw_object_key,
            CAPTURES.c.raw_manifest_sha256,
            CAPTURES.c.raw_manifest_size_bytes,
            CAPTURES.c.raw_manifest_key,
            CAPTURES.c.retention_class,
            CAPTURES.c.source_history_limit,
        )
        .where(and_(*predicates))
        .order_by(CAPTURES.c.capture_id)
        .limit(page_size + 1)
    )
    try:
        selected_rows = connection.execute(statement).mappings().all()
    except SQLAlchemyError:
        raise FinancialCaptureIntegrityQueryError(
            "read-only financial capture query failed"
        ) from None
    has_more = len(selected_rows) > page_size
    page_rows = [dict(row) for row in selected_rows[:page_size]]
    page_results: list[FinancialCaptureIntegrityResult] = []
    bytes_reserved = 0
    processed_rows = 0
    byte_budget_exhausted = False
    per_capture_upper_bound = reader_body_limit + _MAX_MANIFEST_BYTES
    for row in page_rows:
        if _is_valid_sql_capture(row, checked_bound):
            if bytes_reserved + per_capture_upper_bound > _MAX_PAGE_EVIDENCE_BYTES:
                byte_budget_exhausted = True
                break
            bytes_reserved += per_capture_upper_bound
        page_results.append(
            _audit_one_capture(
                row,
                reader=reader,
                scopes=checked_scopes,
                captured_at_not_after=checked_bound,
            )
        )
        processed_rows += 1
    results = tuple(page_results)
    has_more = has_more or byte_budget_exhausted
    cursor_row = page_rows[processed_rows - 1] if has_more and processed_rows else None
    raw_cursor = cursor_row.get("capture_id") if cursor_row is not None else None
    next_cursor = (
        raw_cursor
        if type(raw_cursor) is str and _CAPTURE_ID_PATTERN.fullmatch(raw_cursor) is not None
        else None
    )
    pagination_blocked = bool(has_more and next_cursor is None)
    return FinancialCaptureIntegrityReport(
        results=results,
        next_cursor=next_cursor,
        has_more=has_more,
        pagination_blocked=pagination_blocked,
        evidence_bytes_reserved_upper_bound=bytes_reserved,
        byte_budget_exhausted=byte_budget_exhausted,
    )


def _validate_scopes(
    scopes: tuple[FinancialCaptureIntegrityScope, ...],
) -> tuple[FinancialCaptureIntegrityScope, ...]:
    """Deeply revalidate a finite, unique caller-owned source allowlist."""
    if type(scopes) is not tuple or not 1 <= len(scopes) <= _MAX_SCOPES:
        raise FinancialCaptureIntegrityInputError("scopes must be a non-empty bounded tuple")
    checked: list[FinancialCaptureIntegrityScope] = []
    identities: set[tuple[str, str, str, str]] = set()
    for scope in scopes:
        if type(scope) is not FinancialCaptureIntegrityScope:
            raise FinancialCaptureIntegrityInputError("scope has an unsupported type")
        if (
            type(scope.source_id) is not str
            or _SOURCE_ID_PATTERN.fullmatch(scope.source_id) is None
            or type(scope.raw_source) is not RawResponseSource
        ):
            raise FinancialCaptureIntegrityInputError("scope identity is invalid")
        try:
            raw_source = RawResponseSource.model_validate(
                scope.raw_source.model_dump(mode="python"), strict=True
            )
        except (AttributeError, TypeError, ValueError):
            raise FinancialCaptureIntegrityInputError("scope identity is invalid") from None
        identity = (
            scope.source_id,
            raw_source.provider_id,
            raw_source.dataset_id,
            raw_source.operation_id,
        )
        if identity in identities:
            raise FinancialCaptureIntegrityInputError("scopes contain a duplicate identity")
        identities.add(identity)
        checked.append(FinancialCaptureIntegrityScope(scope.source_id, raw_source))
    return tuple(checked)


def _validate_utc_bound(value: datetime) -> datetime:
    """Require an explicit aware timestamp at UTC offset zero."""
    if type(value) is not datetime or value.tzinfo is None:
        raise FinancialCaptureIntegrityInputError(
            "captured_at_not_after must be a timezone-aware UTC datetime"
        )
    try:
        offset = value.utcoffset()
    except (OverflowError, ValueError):
        raise FinancialCaptureIntegrityInputError(
            "captured_at_not_after must be a timezone-aware UTC datetime"
        ) from None
    if offset is None or offset.total_seconds() != 0:
        raise FinancialCaptureIntegrityInputError("captured_at_not_after must use UTC offset zero")
    return value


def _validated_reader_body_limit(reader: RawResponseArtifactReader) -> int:
    """Require a reader limit no larger than the store's per-capture contract.

    The audit accepts only the exact reviewed reader class. Its private bound is
    inspected so a future reader change fails closed instead of invalidating the
    aggregate byte-work calculation.
    """
    maximum = getattr(reader, "_max_response_bytes", None)
    if type(maximum) is not int or not 1 <= maximum <= _MAX_BODY_BYTES:
        raise FinancialCaptureIntegrityInputError("reader response limit must be at most eight MiB")
    return maximum


def _audit_one_capture(
    row: Mapping[str, object],
    *,
    reader: RawResponseArtifactReader,
    scopes: tuple[FinancialCaptureIntegrityScope, ...],
    captured_at_not_after: datetime,
) -> FinancialCaptureIntegrityResult:
    """Verify one SQL row and its addressed evidence without exposing content."""
    values = row
    raw_capture_id = values.get("capture_id")
    capture_id = (
        raw_capture_id
        if type(raw_capture_id) is str and _CAPTURE_ID_PATTERN.fullmatch(raw_capture_id)
        else None
    )
    if capture_id is None:
        return FinancialCaptureIntegrityResult(None, "invalid_sql_capture")
    scope = _scope_for_row(values, scopes)
    if scope is None:
        return FinancialCaptureIntegrityResult(capture_id, "capture_scope_mismatch")
    if not _is_valid_sql_capture(values, captured_at_not_after):
        return FinancialCaptureIntegrityResult(capture_id, "invalid_sql_capture")

    try:
        evidence = reader.read(
            values["raw_manifest_sha256"],
            expected_source=scope.raw_source,
            expected_request_summary_sha256=values["request_summary_sha256"],
            captured_at_not_after=captured_at_not_after,
            expected_http_status=values["http_status"],
        )
    except RawEvidenceContextMismatchError:
        return FinancialCaptureIntegrityResult(capture_id, "evidence_context_mismatch")
    except RawEvidenceIntegrityError:
        return FinancialCaptureIntegrityResult(capture_id, "evidence_integrity_failure")
    except UnsafeRawEvidenceError:
        return FinancialCaptureIntegrityResult(capture_id, "evidence_unavailable_or_unsafe")
    except RawEvidenceReadInputError:
        return FinancialCaptureIntegrityResult(capture_id, "invalid_evidence_reference")
    except RawEvidenceReadError:
        return FinancialCaptureIntegrityResult(capture_id, "evidence_integrity_failure")

    if not _manifest_matches_sql_row(values, evidence):
        return FinancialCaptureIntegrityResult(capture_id, "capture_manifest_mismatch")
    return FinancialCaptureIntegrityResult(capture_id, None)


def _scope_for_row(
    values: Mapping[str, object], scopes: tuple[FinancialCaptureIntegrityScope, ...]
) -> FinancialCaptureIntegrityScope | None:
    """Find the allowlisted scope whose source id and manifest identity match."""
    for scope in scopes:
        if (
            values.get("source_id") == scope.source_id
            and values.get("provider_id") == scope.raw_source.provider_id
            and values.get("dataset_id") == scope.raw_source.dataset_id
            and values.get("operation_id") == scope.raw_source.operation_id
        ):
            return scope
    return None


def _is_valid_sql_capture(values: Mapping[str, object], bound: datetime) -> TypeGuard[_CaptureRow]:
    """Check bounded scalar types, canonical keys, digests, and timestamp order."""
    text_fields = (
        "capture_id",
        "provider_id",
        "dataset_id",
        "operation_id",
        "source_id",
        "request_summary_sha256",
        "raw_object_sha256",
        "raw_object_key",
        "raw_manifest_sha256",
        "raw_manifest_key",
        "retention_class",
        "source_history_limit",
    )
    for name in text_fields:
        if type(values.get(name)) is not str:
            return False
    capture_id = values.get("capture_id")
    if type(capture_id) is not str or _CAPTURE_ID_PATTERN.fullmatch(capture_id) is None:
        return False
    request_hash = values.get("request_summary_sha256")
    object_hash = values.get("raw_object_sha256")
    manifest_hash = values.get("raw_manifest_sha256")
    if any(
        type(digest) is not str or _SHA256_PATTERN.fullmatch(digest) is None
        for digest in (request_hash, object_hash, manifest_hash)
    ):
        return False
    if type(object_hash) is not str or type(manifest_hash) is not str:
        return False
    if values.get("raw_object_key") != f"{object_hash[:2]}/{object_hash}.blob":
        return False
    if values.get("raw_manifest_key") != f"{manifest_hash[:2]}/{manifest_hash}.json":
        return False
    object_size = values.get("raw_object_size_bytes")
    manifest_size = values.get("raw_manifest_size_bytes")
    if (
        type(object_size) is not int
        or not 1 <= object_size <= _MAX_BODY_BYTES
        or type(manifest_size) is not int
        or not 1 <= manifest_size <= _MAX_MANIFEST_BYTES
    ):
        return False
    status = values.get("http_status")
    if type(status) is not int or not 100 <= status <= 599:
        return False
    if values.get("retention_class") not in {
        "unknown",
        "ephemeral",
        "restricted",
        "durable_candidate",
    }:
        return False
    if values.get("source_history_limit") not in {
        "unknown",
        "provider_retention_window",
        "snapshot_only",
        "partial_history",
        "none_documented",
    }:
        return False
    observed_at = values.get("observed_at_utc")
    captured_at = values.get("captured_at_utc")
    return not (
        type(observed_at) is not datetime
        or observed_at.tzinfo is not None
        or type(captured_at) is not datetime
        or captured_at.tzinfo is not None
        or observed_at > captured_at
        or captured_at.replace(tzinfo=timezone.utc) > bound
    )


def _manifest_matches_sql_row(values: _CaptureRow, evidence: RawEvidenceReadResult) -> bool:
    """Compare all manifest-backed SQL fields after the reader verifies body bytes."""
    manifest = evidence.manifest
    canonical_manifest = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8", errors="strict")
    captured_at = manifest.captured_at_utc.astimezone(timezone.utc).replace(tzinfo=None)
    return (
        evidence.manifest_sha256 == values["raw_manifest_sha256"]
        and len(canonical_manifest) == values["raw_manifest_size_bytes"]
        and manifest.capture_id == values["capture_id"]
        and manifest.source.provider_id == values["provider_id"]
        and manifest.source.dataset_id == values["dataset_id"]
        and manifest.source.operation_id == values["operation_id"]
        and manifest.http_status == values["http_status"]
        and manifest.request_summary_sha256 == values["request_summary_sha256"]
        and manifest.response_sha256 == values["raw_object_sha256"]
        and manifest.response_size_bytes == values["raw_object_size_bytes"]
        and manifest.retention_class == values["retention_class"]
        and manifest.source_history_limit == values["source_history_limit"]
        and captured_at == values["captured_at_utc"]
    )


__all__ = [
    "FinancialCaptureIntegrityInputError",
    "FinancialCaptureIntegrityQueryError",
    "FinancialCaptureIntegrityReport",
    "FinancialCaptureIntegrityResult",
    "FinancialCaptureIntegrityScope",
    "audit_financial_capture_integrity",
]
