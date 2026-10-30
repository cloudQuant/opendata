"""Offline, SQLite-only source attempt reservations and shared token buckets.

The gate records one database reservation per transport attempt. It does not
send HTTP, prove source rights, impose run/day attempt ceilings, or limit
cross-process in-flight concurrency. Its operator-declared evidence fields are
references and attestations, not automatic legal confirmation. The module has
no grant-issuance API and an empty database authorizes nothing.
Attempt rows retain validated policy metadata, not a full bucket-state replay.
File identity checks detect misdirected/replaced connections but cannot prevent
privileged operating-system races or make SQLite audit rows tamper-proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Literal
from urllib.parse import unquote, urlsplit

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import stat_result

    from sqlalchemy.engine import Connection, Engine


SOURCE_GATE_CLOCK_TABLE = "od_source_gate_clock"
SOURCE_GATE_POLICY_TABLE = "od_source_gate_policies"
SOURCE_GATE_BINDING_TABLE = "od_source_gate_source_bindings"
SOURCE_GATE_STATE_TABLE = "od_source_gate_state"
SOURCE_GRANT_TABLE = "od_source_grants"
SOURCE_GRANT_HOST_TABLE = "od_source_grant_hosts"
SOURCE_GRANT_HOST_SET_TABLE = "od_source_grant_host_sets"
SOURCE_GRANT_EVENT_TABLE = "od_source_grant_events"
SOURCE_ATTEMPT_TABLE = "od_source_attempt_reservations"
SOURCE_ATTEMPT_SCOPE_V2_TABLE = "od_source_attempt_scope_v2"
SOURCE_GATE_VERSION_TABLE = "alembic_version_source_gate"
SOURCE_GATE_SCOPE_V2_REVISION = "sg0002_source_attempt_scope_v2"

MICROTOKENS_PER_TOKEN = 1_000_000
MICROSECONDS_PER_SECOND = 1_000_000
MAX_RATE_MICROTOKENS_PER_SECOND = 1_000_000_000_000
MAX_CAPACITY_MICROTOKENS = 1_000_000_000_000
MAX_ATTEMPT_NO = 1_000_000
MAX_REVISION = 2_147_483_647
MAX_TIMESTAMP_US = 253_402_300_799_999_999
MAX_RETRY_AFTER_US = 1_000_000_000_000_000_000
_MAX_TEXT_LENGTH = 128
_MAX_SCOPE_V2_BYTES = 16 * 1024
_IDENTIFIER_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST_LABEL_PATTERN = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_ATTEMPT_DENIAL_CODES = frozenset(
    {
        "budget_scope_mismatch",
        "grant_conditions_invalid",
        "grant_conditions_unresolved",
        "grant_evidence_incomplete",
        "grant_expired",
        "grant_evidence_reference_mismatch",
        "grant_host_mismatch",
        "grant_hosts_invalid",
        "grant_hosts_unsealed",
        "grant_invalid",
        "grant_not_allowed",
        "grant_not_found",
        "grant_not_yet_valid",
        "grant_revision_mismatch",
        "grant_scope_mismatch",
        "source_policy_revision_invalid",
        "source_policy_unconfigured",
        "source_quota_state_invalid",
        "source_quota_state_unavailable",
        "source_quota_unconfigured",
        "source_rate_limited",
    }
)
_BASE_ATTEMPT_COLUMNS = {
    "attempt_id": ("VARCHAR(128)", 1, 1),
    "request_id": ("VARCHAR(128)", 1, 0),
    "attempt_no": ("BIGINT", 1, 0),
    "grant_id": ("VARCHAR(128)", 1, 0),
    "expected_revision": ("BIGINT", 1, 0),
    "source": ("VARCHAR(128)", 1, 0),
    "canonical_model": ("VARCHAR(128)", 1, 0),
    "operation": ("VARCHAR(16)", 1, 0),
    "principal_scope": ("VARCHAR(128)", 1, 0),
    "host": ("VARCHAR(253)", 1, 0),
    "run_key": ("VARCHAR(128)", 0, 0),
    "outcome": ("VARCHAR(8)", 1, 0),
    "denial_code": ("VARCHAR(64)", 0, 0),
    "retry_after_us": ("BIGINT", 0, 0),
    "decided_at_us": ("BIGINT", 1, 0),
    "reserved_at_us": ("BIGINT", 0, 0),
    "quota_key": ("VARCHAR(128)", 0, 0),
    "policy_revision": ("BIGINT", 0, 0),
    "rate_microtokens_per_second": ("BIGINT", 0, 0),
    "capacity_microtokens": ("BIGINT", 0, 0),
}
_BASE_ATTEMPT_CHECKS = (
    "length(trim(attempt_id)) BETWEEN 1 AND 128",
    "length(trim(request_id)) BETWEEN 1 AND 128",
    "attempt_no BETWEEN 1 AND 1000000",
    "length(trim(grant_id)) BETWEEN 1 AND 128",
    "expected_revision BETWEEN 1 AND 2147483647",
    "length(trim(source)) BETWEEN 1 AND 128",
    "length(trim(canonical_model)) BETWEEN 1 AND 128",
    "operation IN ('query', 'store', 'export', 'subscribe')",
    "length(trim(principal_scope)) BETWEEN 1 AND 128",
    "length(trim(host)) BETWEEN 1 AND 253",
    "run_key IS NULL OR length(trim(run_key)) BETWEEN 1 AND 128",
    "(outcome = 'RESERVED' AND denial_code IS NULL AND reserved_at_us IS NOT NULL) OR "
    "(outcome = 'DENIED' AND denial_code IS NOT NULL "
    "AND length(trim(denial_code)) BETWEEN 1 AND 64 "
    "AND reserved_at_us IS NULL)",
    "retry_after_us IS NULL OR retry_after_us BETWEEN 1 AND 1000000000000000000",
    "decided_at_us >= 0",
    "reserved_at_us IS NULL OR reserved_at_us = decided_at_us",
    "(quota_key IS NULL AND policy_revision IS NULL "
    "AND rate_microtokens_per_second IS NULL AND capacity_microtokens IS NULL) OR "
    "(quota_key IS NOT NULL AND policy_revision IS NOT NULL "
    "AND rate_microtokens_per_second IS NOT NULL AND capacity_microtokens IS NOT NULL AND "
    "length(trim(quota_key)) BETWEEN 1 AND 128 AND "
    "policy_revision BETWEEN 1 AND 2147483647 AND "
    "rate_microtokens_per_second BETWEEN 1 AND 1000000000000 AND "
    "capacity_microtokens BETWEEN 1000000 AND 1000000000000)",
)
_SCOPE_V2_CHECKS = (
    "scope_version = 2",
    "length(CAST(scope_json AS BLOB)) BETWEEN 1 AND 16384",
    "length(scope_sha256) = 64 AND scope_sha256 NOT GLOB '*[^0-9a-f]*'",
)
_IMMUTABLE_TRIGGER_SQL = {
    (SOURCE_ATTEMPT_TABLE, "UPDATE"): (
        "CREATE TRIGGER trg_od_source_attempt_immutable_update BEFORE UPDATE ON "
        "od_source_attempt_reservations BEGIN SELECT RAISE(ABORT, "
        "'source attempt tombstones are immutable'); END"
    ),
    (SOURCE_ATTEMPT_TABLE, "DELETE"): (
        "CREATE TRIGGER trg_od_source_attempt_immutable_delete BEFORE DELETE ON "
        "od_source_attempt_reservations BEGIN SELECT RAISE(ABORT, "
        "'source attempt tombstones are permanent'); END"
    ),
    (SOURCE_ATTEMPT_SCOPE_V2_TABLE, "UPDATE"): (
        "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_update BEFORE UPDATE ON "
        "od_source_attempt_scope_v2 BEGIN SELECT RAISE(ABORT, "
        "'source attempt scope v2 rows are immutable'); END"
    ),
    (SOURCE_ATTEMPT_SCOPE_V2_TABLE, "DELETE"): (
        "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_delete BEFORE DELETE ON "
        "od_source_attempt_scope_v2 BEGIN SELECT RAISE(ABORT, "
        "'source attempt scope v2 rows are permanent'); END"
    ),
}


class SourceAttemptGateError(RuntimeError):
    """Base error for a gate decision that cannot safely return a reservation."""


class SourceAttemptGateUnavailableError(SourceAttemptGateError):
    """The storage transaction or required clock could not be trusted."""


class SourceAttemptGateConfigurationError(SourceAttemptGateError):
    """The gate was constructed for a dialect not verified by this release."""


class SourceAttemptInputError(ValueError):
    """A caller supplied an invalid or unbounded attempt identity."""


SourceAttemptGateUnavailable = SourceAttemptGateUnavailableError


class ReservationStatus(str, Enum):
    """Safe, non-reusable result of one attempt reservation call."""

    RESERVED_NEW = "RESERVED_NEW"
    DENIED = "DENIED"
    DUPLICATE = "DUPLICATE"
    ID_CONFLICT = "ID_CONFLICT"


class GrantRevocationStatus(str, Enum):
    """Result of a compare-and-revoke request for a persisted grant."""

    REVOKED = "REVOKED"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_REVOKED = "ALREADY_REVOKED"
    STALE_REVISION = "STALE_REVISION"
    NOT_REVOCABLE = "NOT_REVOCABLE"


class SourceGrantInspectionStatus(str, Enum):
    """Outcome of an advisory, read-only inspection of one source grant."""

    MATCHED = "MATCHED"
    REJECTED = "REJECTED"
    INDETERMINATE = "INDETERMINATE"


class SourceGrantInspectionReason(str, Enum):
    """Fixed, non-sensitive result codes for manual source-grant inspection."""

    GRANT_MATCHED = "grant_matched"
    GRANT_NOT_FOUND = "grant_not_found"
    GRANT_NOT_ALLOWED = "grant_not_allowed"
    GRANT_INVALID = "grant_invalid"
    GRANT_REVISION_MISMATCH = "grant_revision_mismatch"
    GRANT_SCOPE_MISMATCH = "grant_scope_mismatch"
    GRANT_NOT_YET_VALID = "grant_not_yet_valid"
    GRANT_EXPIRED = "grant_expired"
    GRANT_EVIDENCE_INCOMPLETE = "grant_evidence_incomplete"
    GRANT_CONDITIONS_INVALID = "grant_conditions_invalid"
    GRANT_CONDITIONS_UNRESOLVED = "grant_conditions_unresolved"
    GRANT_HOSTS_UNSEALED = "grant_hosts_unsealed"
    GRANT_HOSTS_INVALID = "grant_hosts_invalid"
    GRANT_HOST_MISMATCH = "grant_host_mismatch"
    DATABASE_UNAVAILABLE = "database_unavailable"
    SCHEMA_UNAVAILABLE = "schema_unavailable"
    CLOCK_INVALID = "clock_invalid"
    GRANT_RECORD_UNVERIFIABLE = "grant_record_unverifiable"
    GRANT_SEAL_UNVERIFIABLE = "grant_seal_unverifiable"


_GRANT_INSPECTION_DENIAL_REASONS = {
    reason.value: reason
    for reason in (
        SourceGrantInspectionReason.GRANT_NOT_FOUND,
        SourceGrantInspectionReason.GRANT_NOT_ALLOWED,
        SourceGrantInspectionReason.GRANT_REVISION_MISMATCH,
        SourceGrantInspectionReason.GRANT_SCOPE_MISMATCH,
        SourceGrantInspectionReason.GRANT_NOT_YET_VALID,
        SourceGrantInspectionReason.GRANT_EXPIRED,
        SourceGrantInspectionReason.GRANT_CONDITIONS_UNRESOLVED,
        SourceGrantInspectionReason.GRANT_HOST_MISMATCH,
    )
}
_GRANT_INSPECTION_UNVERIFIABLE_REASONS = {
    "grant_invalid": SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE,
    "grant_evidence_incomplete": SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE,
    "grant_conditions_invalid": SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE,
    "grant_hosts_unsealed": SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE,
    "grant_hosts_invalid": SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE,
}


@dataclass(frozen=True, slots=True)
class SourceGrantInspectionRequest:
    """Exact caller-supplied scope for an advisory inspection of a persisted grant."""

    grant_id: str
    revision: int
    source: str
    canonical_model: str
    operation: str
    principal_scope: str
    host: str

    def __post_init__(self) -> None:
        """Validate bounded identifiers before any database access."""
        for field_name in (
            "grant_id",
            "source",
            "canonical_model",
            "principal_scope",
        ):
            _validate_identifier(getattr(self, field_name), field_name)
        if type(self.revision) is not int or not 1 <= self.revision <= MAX_REVISION:
            raise SourceAttemptInputError("revision is outside the supported range")
        if not isinstance(self.operation, str) or self.operation not in {
            "query",
            "store",
            "export",
            "subscribe",
        }:
            raise SourceAttemptInputError("operation is not supported")
        object.__setattr__(self, "host", _normalize_host(self.host))


@dataclass(frozen=True, slots=True)
class SourceGrantInspectionResult:
    """Status and fixed reason for one advisory source-grant inspection."""

    status: SourceGrantInspectionStatus
    reason: SourceGrantInspectionReason


class AttemptLookupReasonCode(str, Enum):
    """Fixed, non-sensitive reasons why a strict v2 lookup is indeterminate."""

    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    SCHEMA_UNAVAILABLE = "SCHEMA_UNAVAILABLE"
    LEGACY_SCOPE_UNVERIFIABLE = "LEGACY_SCOPE_UNVERIFIABLE"
    ID_CONFLICT = "ID_CONFLICT"
    SCOPE_MISMATCH = "SCOPE_MISMATCH"
    SCOPE_CORRUPT = "SCOPE_CORRUPT"
    RECORD_CORRUPT = "RECORD_CORRUPT"


@dataclass(frozen=True, slots=True)
class TransportAttempt:
    """Bound identity for a single physical HTTP transport attempt.

    The caller must create a new ``attempt_id`` and increment ``attempt_no``
    for every retry. ``host`` is an exact DNS hostname, without scheme/port.
    ``run_key`` participates in idempotency binding only; this gate does not
    count per-run, daily, or task-level attempts. The optional expected
    evidence reference binds a grant's persisted declaration in the same
    transaction as a new v1 attempt reservation. The v1 reservation table does
    not persist that reference: a repeated identity is classified from the
    stored columns, so a ``DUPLICATE`` or ``ID_CONFLICT`` is never a send
    permit. V2 rejects this optional field until its scope schema can bind it.
    """

    attempt_id: str
    request_id: str
    attempt_no: int
    grant_id: str
    expected_revision: int
    source: str
    canonical_model: str
    operation: str
    principal_scope: str
    host: str
    run_key: str | None = None
    expected_rights_evidence_reference: str | None = None

    def __post_init__(self) -> None:
        """Validate bounded identifiers and freeze a canonical host value."""
        for field_name in (
            "attempt_id",
            "request_id",
            "grant_id",
            "source",
            "canonical_model",
            "principal_scope",
        ):
            _validate_identifier(getattr(self, field_name), field_name)
        if self.run_key is not None:
            _validate_identifier(self.run_key, "run_key")
        evidence_reference = self.expected_rights_evidence_reference
        if evidence_reference is not None and (
            type(evidence_reference) is not str
            or not evidence_reference
            or evidence_reference != evidence_reference.strip()
            or len(evidence_reference) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in evidence_reference)
        ):
            raise SourceAttemptInputError("expected evidence reference is invalid")
        if type(self.attempt_no) is not int or not 1 <= self.attempt_no <= MAX_ATTEMPT_NO:
            raise SourceAttemptInputError("attempt_no is outside the supported range")
        if (
            type(self.expected_revision) is not int
            or not 1 <= self.expected_revision <= MAX_REVISION
        ):
            raise SourceAttemptInputError("expected_revision is outside the supported range")
        if not isinstance(self.operation, str) or self.operation not in {
            "query",
            "store",
            "export",
            "subscribe",
        }:
            raise SourceAttemptInputError("operation is not supported")
        object.__setattr__(self, "host", _normalize_host(self.host))


@dataclass(frozen=True, slots=True)
class SourceAttemptScopeV2:
    """Complete, non-secret caller-declared scope for one v2 gate reservation.

    Policy and budget pins identify the requested scope; they do not prove source
    rights, HTTP delivery, or provider receipt. ``budget_quota_key`` names the
    gate's shared bucket dimension. A v2 scope always requires a durable run key.
    """

    attempt: TransportAttempt
    capture_intent_id: str
    request_summary_sha256: str
    source_policy_id: str
    source_policy_revision: str
    source_policy_sha256: str
    budget_binding_sha256: str
    budget_quota_key: str
    budget_policy_revision: int
    budget_policy_sha256: str

    def __post_init__(self) -> None:
        """Reject partial, malformed, or unbounded scope before any database access."""
        if type(self.attempt) is not TransportAttempt:
            raise SourceAttemptInputError("attempt must be a TransportAttempt")
        if self.attempt.run_key is None:
            raise SourceAttemptInputError("v2 attempt scope requires a run_key")
        for field_name in (
            "capture_intent_id",
            "source_policy_id",
            "source_policy_revision",
            "budget_quota_key",
        ):
            _validate_identifier(getattr(self, field_name), field_name)
        for field_name in (
            "request_summary_sha256",
            "source_policy_sha256",
            "budget_binding_sha256",
            "budget_policy_sha256",
        ):
            if not _valid_sha256(getattr(self, field_name)):
                raise SourceAttemptInputError(f"{field_name} must be a lowercase SHA-256 digest")
        if (
            type(self.budget_policy_revision) is not int
            or not 1 <= self.budget_policy_revision <= MAX_REVISION
        ):
            raise SourceAttemptInputError("budget_policy_revision is outside the supported range")
        _scope_v2_bytes(self)


@dataclass(frozen=True, slots=True)
class AttemptLookupReserved:
    """Exact matching committed reservation; not proof an HTTP request was sent."""

    status: Literal["RESERVED"]
    attempt_id: str
    scope_sha256: str
    source_policy_revision: str
    budget_policy_revision: int
    reserved_at_us: int


@dataclass(frozen=True, slots=True)
class AttemptLookupDenied:
    """Exact matching committed denial tombstone."""

    status: Literal["DENIED"]
    attempt_id: str
    scope_sha256: str
    source_policy_revision: str
    denial_code: str


@dataclass(frozen=True, slots=True)
class AttemptLookupAbsent:
    """No matching row in a verified, successful local SQLite read snapshot."""

    status: Literal["ABSENT"]
    scope_sha256: str


@dataclass(frozen=True, slots=True)
class AttemptLookupIndeterminate:
    """Lookup could not prove an exact decision or authoritative absence."""

    status: Literal["INDETERMINATE"]
    reason_code: AttemptLookupReasonCode


GateAttemptLookupResult = (
    AttemptLookupReserved | AttemptLookupDenied | AttemptLookupAbsent | AttemptLookupIndeterminate
)


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    """Reservation result; only RESERVED_NEW after confirmed commit may send."""

    status: ReservationStatus
    denial_code: str | None = None
    retry_after_us: int | None = None
    reserved_at_us: int | None = None


@dataclass(frozen=True, slots=True)
class GrantRevocationResult:
    """Compare-and-revoke outcome without exposing grant evidence contents."""

    status: GrantRevocationStatus
    revision: int | None = None


@dataclass(frozen=True, slots=True)
class _BucketDecision:
    """Refilled bucket values and whether one transport token is available."""

    tokens_micro: int
    remainder: int
    last_refill_us: int
    policy_revision: int
    allowed: bool
    retry_after_us: int | None


@dataclass(frozen=True, slots=True)
class _PolicySnapshot:
    """Policy metadata copied to attempts; this is not a full bucket replay record."""

    quota_key: str
    revision: int
    rate_microtokens_per_second: int
    capacity_microtokens: int


class SourceAttemptGate:
    """Persistently reserve attempts through an explicitly supplied SQLite engine.

    This first version proves local file-backed SQLite serialization only.
    PostgreSQL, MySQL, multi-host locking, HTTP integration, and concurrency
    leases remain unverified and are deliberately rejected or out of scope.
    Filesystem checks are not an operating-system-level race or tamper defense.
    """

    def __init__(self, engine: Engine) -> None:
        """Bind the gate to a SQLite SQLAlchemy engine; never choose a DB implicitly."""
        if engine.dialect.name != "sqlite":
            raise SourceAttemptGateConfigurationError(
                "source attempt gate supports SQLite only; other dialects are unverified"
            )
        database = engine.url.database
        uri_query = engine.url.query
        mode_values = tuple(value for key, value in uri_query.items() if key.lower() == "mode")
        flattened_modes = tuple(
            mode
            for value in mode_values
            for mode in (value if isinstance(value, tuple) else (value,))
            if mode is not None
        )
        if (
            not isinstance(database, str)
            or not database.strip()
            or database.strip().lower() == ":memory:"
            or database.strip().lower().startswith("file::memory:")
            or "mode=memory" in database.lower()
            or any(str(mode).lower() == "memory" for mode in flattened_modes)
        ):
            raise SourceAttemptGateConfigurationError(
                "source attempt gate requires a file-backed SQLite database"
            )
        try:
            expected_path = _configured_database_path(database, uri_query)
            expected_path, expected_stat = _stat_database_file(expected_path)
        except (OSError, ValueError):
            raise SourceAttemptGateConfigurationError(
                "source attempt gate requires an existing regular SQLite file"
            ) from None
        self._expected_main_path = expected_path
        self._expected_main_identity = (expected_stat.st_dev, expected_stat.st_ino)
        self._engine = engine

    def reserve_attempt(self, attempt: TransportAttempt) -> AttemptReservation:
        """Reserve one request attempt or persist a non-consuming denial tombstone.

        A duplicate never returns the original reservation. On storage or
        commit uncertainty this method raises ``SourceAttemptGateUnavailable``;
        callers must send only after receiving ``RESERVED_NEW``.
        """
        if not isinstance(attempt, TransportAttempt):
            raise SourceAttemptInputError("attempt must be a TransportAttempt")
        return self._reserve_attempt(attempt, None)

    def reserve_attempt_v2(self, scope: SourceAttemptScopeV2) -> AttemptReservation:
        """Atomically reserve a complete v2 scope and its immutable scope sidecar.

        This API is available only after the dedicated source-gate migration is
        installed. It never retries a request and a repeated identity never
        returns a reusable permit.
        """
        if type(scope) is not SourceAttemptScopeV2:
            raise SourceAttemptInputError("scope must be a SourceAttemptScopeV2")
        _scope_v2_bytes(scope)
        return self._reserve_attempt(scope.attempt, scope)

    def _reserve_attempt(
        self,
        attempt: TransportAttempt,
        scope: SourceAttemptScopeV2 | None,
    ) -> AttemptReservation:
        """Share the v1 writer while keeping v2 metadata in the same transaction."""
        connection = self._open_write_connection()
        try:
            if scope is not None:
                self._require_v2_schema(connection)
            effective_now = self._advance_clock(connection)
            duplicate = (
                self._find_existing_v2_attempt(connection, scope)
                if scope is not None
                else self._find_existing_attempt(connection, attempt)
            )
            if duplicate is not None:
                self._commit(connection)
                return duplicate

            if scope is not None:
                decision, bucket_update, policy_snapshot = self._evaluate_attempt_v2(
                    connection, scope, effective_now
                )
            else:
                decision, bucket_update, policy_snapshot = self._evaluate_attempt(
                    connection, attempt, effective_now
                )
            if bucket_update is not None:
                self._persist_bucket(connection, bucket_update[0], bucket_update[1])
            self._insert_attempt(connection, attempt, decision, effective_now, policy_snapshot)
            if scope is not None:
                self._insert_attempt_scope_v2(connection, scope, effective_now)
            self._commit(connection)
            return decision
        except SourceAttemptGateUnavailable:
            raise
        except SQLAlchemyError:
            raise SourceAttemptGateUnavailable("source attempt gate is unavailable") from None
        except Exception:
            raise
        finally:
            connection.close()

    def lookup_attempt_v2(self, scope: SourceAttemptScopeV2) -> GateAttemptLookupResult:
        """Read one exact attempt decision using a SQLite read-only connection.

        This reports only a local gate tombstone. It does not establish that a
        transport was invoked, that a provider received a request, or that
        crash recovery is implemented. Any unverified state is indeterminate.
        """
        if type(scope) is not SourceAttemptScopeV2:
            raise SourceAttemptInputError("scope must be a SourceAttemptScopeV2")
        scope_json, scope_sha256 = _scope_v2_bytes_and_digest(scope)
        try:
            connection = self._open_readonly_connection()
        except Exception:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.DATABASE_UNAVAILABLE
            )
        try:
            reason_code = self._v2_schema_reason(connection)
            if reason_code is not None:
                return AttemptLookupIndeterminate("INDETERMINATE", reason_code)
            return self._lookup_v2_snapshot(scope, scope_json, scope_sha256, connection)
        except Exception:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.DATABASE_UNAVAILABLE
            )
        finally:
            with suppress(sqlite3.Error):
                connection.rollback()
            connection.close()

    def inspect_grant(self, request: SourceGrantInspectionRequest) -> SourceGrantInspectionResult:
        """Inspect one persisted grant in a read-only snapshot for human review.

        ``MATCHED`` means only that the current snapshot's grant declaration and
        sealed host scope match the supplied identity. This advisory result is
        never a transport permit; every transport must perform a fresh
        ``reserve_attempt`` call. The method does not mutate gate rows, reserve
        quota, schedule work, access the network, or load application settings.
        SQLite may manage its own WAL lock sidecars while opening a read-only
        snapshot. File identity checks still cannot prevent same-user
        swap-and-restore races.
        """
        if type(request) is not SourceGrantInspectionRequest:
            raise SourceAttemptInputError("request must be a SourceGrantInspectionRequest")
        try:
            connection = self._open_readonly_connection()
        except Exception:
            return _grant_inspection_indeterminate(SourceGrantInspectionReason.DATABASE_UNAVAILABLE)
        try:
            if not self._source_gate_snapshot_schema_valid(connection):
                return _grant_inspection_indeterminate(
                    SourceGrantInspectionReason.SCHEMA_UNAVAILABLE
                )
            effective_now = self._readonly_effective_now_us(connection)
            if effective_now is None:
                return _grant_inspection_indeterminate(SourceGrantInspectionReason.CLOCK_INVALID)

            grant_rows = self._query_rows(
                connection,
                "SELECT * FROM od_source_grants WHERE grant_id = :grant_id",
                {"grant_id": request.grant_id},
            )
            if len(grant_rows) > 1:
                return _grant_inspection_indeterminate(
                    SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE
                )
            if not grant_rows:
                return _grant_inspection_rejected(SourceGrantInspectionReason.GRANT_NOT_FOUND)

            attempt = TransportAttempt(
                attempt_id="grant-inspection",
                request_id="grant-inspection",
                attempt_no=1,
                grant_id=request.grant_id,
                expected_revision=request.revision,
                source=request.source,
                canonical_model=request.canonical_model,
                operation=request.operation,
                principal_scope=request.principal_scope,
                host=request.host,
            )
            denial = self._grant_denial(connection, grant_rows[0], attempt, effective_now)
            if denial is not None:
                reason = _GRANT_INSPECTION_DENIAL_REASONS.get(denial)
                if reason is not None:
                    return _grant_inspection_rejected(reason)
                reason = _GRANT_INSPECTION_UNVERIFIABLE_REASONS.get(denial)
                if reason is not None:
                    return _grant_inspection_indeterminate(reason)
                return _grant_inspection_indeterminate(
                    SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE
                )
            if not self._grant_seal_event_matches(connection, request):
                return _grant_inspection_indeterminate(
                    SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE
                )
            return SourceGrantInspectionResult(
                SourceGrantInspectionStatus.MATCHED,
                SourceGrantInspectionReason.GRANT_MATCHED,
            )
        except (sqlite3.Error, SQLAlchemyError):
            return _grant_inspection_indeterminate(SourceGrantInspectionReason.DATABASE_UNAVAILABLE)
        except Exception:
            return _grant_inspection_indeterminate(
                SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE
            )
        finally:
            with suppress(sqlite3.Error):
                connection.rollback()
            connection.close()

    def _source_gate_snapshot_schema_valid(self, connection: sqlite3.Connection) -> bool:
        """Require the pinned full v1/v2 schema on the current read snapshot."""
        try:
            from alembic_source_gate.schema_contract import (
                V1_TABLES,
                V2_REVISION,
                V2_TABLE,
                VERSION_TABLE,
                _reference_schema,
            )

            expected = _reference_schema(include_v2=True)
        except Exception:
            return False
        gate_tables = V1_TABLES | {V2_TABLE}
        if not self._readonly_gate_objects_match(connection, expected):
            return False
        encoding = self._query_rows(connection, "PRAGMA encoding", {})
        if len(encoding) != 1 or encoding[0].get("encoding") != "UTF-8":
            return False
        if not self._readonly_migration_marker_matches(connection, VERSION_TABLE, V2_REVISION):
            return False
        violations = self._query_rows(connection, "PRAGMA foreign_key_check", {})
        if any(violation.get("table") in gate_tables for violation in violations):
            return False
        return self._readonly_schema_metadata_complete(
            connection,
            gate_tables | {VERSION_TABLE},
        )

    def _readonly_gate_objects_match(
        self,
        connection: sqlite3.Connection,
        expected: Mapping[tuple[str, str, str], str | None],
    ) -> bool:
        """Compare all source-gate tables, indexes, and triggers with the pinned DDL."""
        actual: dict[tuple[str, str, str], str | None] = {}
        rows = self._query_rows(
            connection,
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name",
            {},
        )
        for row in rows:
            object_type = row.get("type")
            name = row.get("name")
            table_name = row.get("tbl_name")
            sql = row.get("sql")
            if not isinstance(object_type, str) or not isinstance(name, str):
                continue
            if not isinstance(table_name, str):
                table_name = ""
            if not (
                _sqlite_ascii_prefix(name, "od_source_")
                or _sqlite_ascii_prefix(name, "trg_od_source_")
                or _sqlite_ascii_prefix(table_name, "od_source_")
            ):
                continue
            normalized_sql = None
            if isinstance(sql, str):
                normalized_sql = re.sub(r"\s+", " ", sql).strip().rstrip(";")
            actual[(object_type, name, table_name)] = normalized_sql
        return actual == expected

    def _readonly_migration_marker_matches(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        expected_revision: str,
    ) -> bool:
        """Validate the isolated source-gate revision marker, including its key index."""
        marker_tables = self._query_rows(
            connection,
            "SELECT type, sql FROM sqlite_master WHERE name = :table_name",
            {"table_name": table_name},
        )
        marker_sql = marker_tables[0].get("sql") if len(marker_tables) == 1 else None
        expected_sql = (
            "CREATE TABLE alembic_version_source_gate ( version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
        )
        normalized_sql = None
        if isinstance(marker_sql, str):
            normalized_sql = re.sub(r"\s+", " ", marker_sql).strip().rstrip(";")
            # SQLite preserves Alembic's newline between the nested and outer
            # closing parentheses. Normalize only that optional final gap.
            normalized_sql = re.sub(r"\) \)$", "))", normalized_sql)
        if (
            len(marker_tables) != 1
            or marker_tables[0].get("type") != "table"
            or normalized_sql != expected_sql
        ):
            return False
        literal = _quote_sqlite_literal(table_name)
        columns = self._query_rows(
            connection,
            f"PRAGMA table_xinfo({literal})",
            {},
        )
        if (
            len(columns) != 1
            or columns[0].get("cid") != 0
            or columns[0].get("name") != "version_num"
            or str(columns[0].get("type", "")).upper() != "VARCHAR(32)"
            or columns[0].get("notnull") != 1
            or columns[0].get("dflt_value") is not None
            or columns[0].get("pk") != 1
            or columns[0].get("hidden") != 0
        ):
            return False
        indexes = self._query_rows(connection, f"PRAGMA index_list({literal})", {})
        if (
            len(indexes) != 1
            or indexes[0].get("unique") != 1
            or indexes[0].get("origin") != "pk"
            or indexes[0].get("partial") != 0
            or not isinstance(indexes[0].get("name"), str)
        ):
            return False
        index_name = _quote_sqlite_literal(str(indexes[0]["name"]))
        index_info = self._query_rows(connection, f"PRAGMA index_info({index_name})", {})
        index_xinfo = self._query_rows(connection, f"PRAGMA index_xinfo({index_name})", {})
        key_rows = tuple(row for row in index_xinfo if row.get("key") == 1)
        auxiliary_rows = tuple(row for row in index_xinfo if row.get("key") == 0)
        if (
            len(index_info) != 1
            or index_info[0].get("name") != "version_num"
            or len(key_rows) != 1
            or key_rows[0].get("cid") != 0
            or key_rows[0].get("name") != "version_num"
            or key_rows[0].get("desc") != 0
            or key_rows[0].get("coll") != "BINARY"
            or len(auxiliary_rows) != 1
            or auxiliary_rows[0].get("cid") != -1
            or auxiliary_rows[0].get("name") is not None
            or auxiliary_rows[0].get("desc") != 0
            or auxiliary_rows[0].get("coll") != "BINARY"
        ):
            return False
        triggers = self._query_rows(
            connection,
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table_name",
            {"table_name": table_name},
        )
        if triggers:
            return False
        if table_name != SOURCE_GATE_VERSION_TABLE:
            return False
        versions = self._query_rows(
            connection,
            "SELECT version_num FROM alembic_version_source_gate",
            {},
        )
        return len(versions) == 1 and versions[0].get("version_num") == expected_revision

    def _readonly_schema_metadata_complete(
        self,
        connection: sqlite3.Connection,
        table_names: frozenset[str],
    ) -> bool:
        """Check generated columns and every pinned index key on the same snapshot."""
        try:
            for table_name in sorted(table_names):
                literal = _quote_sqlite_literal(table_name)
                table_columns = self._query_rows(
                    connection,
                    f"PRAGMA table_xinfo({literal})",
                    {},
                )
                if not table_columns or any(column.get("hidden") != 0 for column in table_columns):
                    return False
                indexes = self._query_rows(
                    connection,
                    f"PRAGMA index_list({literal})",
                    {},
                )
                for index in indexes:
                    index_name = index.get("name")
                    if (
                        index.get("unique") != 1
                        or index.get("partial") != 0
                        or index.get("origin") not in {"pk", "u"}
                        or not isinstance(index_name, str)
                    ):
                        return False
                    index_literal = _quote_sqlite_literal(index_name)
                    info_rows = self._query_rows(
                        connection,
                        f"PRAGMA index_info({index_literal})",
                        {},
                    )
                    xinfo_rows = self._query_rows(
                        connection,
                        f"PRAGMA index_xinfo({index_literal})",
                        {},
                    )
                    key_rows = tuple(row for row in xinfo_rows if row.get("key") == 1)
                    key_names = tuple(row.get("name") for row in key_rows)
                    auxiliary_rows = tuple(row for row in xinfo_rows if row.get("key") == 0)
                    info_names = tuple(row.get("name") for row in info_rows)
                    if not info_names or info_names != key_names or len(auxiliary_rows) > 1:
                        return False
                    for row in key_rows:
                        column_id = row.get("cid")
                        if (
                            not isinstance(row.get("name"), str)
                            or type(column_id) is not int
                            or column_id < 0
                            or row.get("desc") != 0
                            or row.get("coll") != "BINARY"
                        ):
                            return False
                    for row in auxiliary_rows:
                        if (
                            row.get("cid") != -1
                            or row.get("name") is not None
                            or row.get("desc") != 0
                            or row.get("coll") != "BINARY"
                        ):
                            return False
        except (sqlite3.Error, SQLAlchemyError):
            return False
        except Exception:
            return False
        return True

    def _readonly_effective_now_us(self, connection: sqlite3.Connection) -> int | None:
        """Read the high-water mark and SQLite clock without updating gate state."""
        clock_rows = self._query_rows(
            connection,
            "SELECT clock_id, highwater_us FROM od_source_gate_clock ORDER BY clock_id",
            {},
        )
        highwater = (
            _bounded_int_or_none(clock_rows[0].get("highwater_us"), 0, MAX_TIMESTAMP_US)
            if len(clock_rows) == 1
            else None
        )
        if len(clock_rows) != 1 or clock_rows[0].get("clock_id") != 1 or highwater is None:
            return None
        now_rows = self._query_rows(
            connection,
            "SELECT CAST(ROUND((julianday('now') - 2440587.5) * "
            "86400000000.0) AS INTEGER) AS now_us",
            {},
        )
        if len(now_rows) != 1:
            return None
        database_now = _bounded_int_or_none(now_rows[0].get("now_us"), 1, MAX_TIMESTAMP_US)
        if database_now is None:
            return None
        return max(database_now, highwater)

    def _grant_seal_event_matches(
        self,
        connection: sqlite3.Connection,
        request: SourceGrantInspectionRequest,
    ) -> bool:
        """Bind an allowed grant's host seal to its single immutable audit event."""
        host_sets = self._query_rows(
            connection,
            "SELECT revision, host_count, sealed_at_us, sealed_by, reason_code "
            "FROM od_source_grant_host_sets WHERE grant_id = :grant_id",
            {"grant_id": request.grant_id},
        )
        if len(host_sets) != 1:
            return False
        host_set = host_sets[0]
        revision = _bounded_int_or_none(host_set.get("revision"), 1, MAX_REVISION)
        sealed_at = _bounded_int_or_none(host_set.get("sealed_at_us"), 0, MAX_TIMESTAMP_US)
        sealed_by = host_set.get("sealed_by")
        reason_code = host_set.get("reason_code")
        if (
            revision != request.revision
            or _bounded_int_or_none(host_set.get("host_count"), 1, 10_000) is None
            or sealed_at is None
            or not _valid_identifier(sealed_by)
            or not _valid_identifier(reason_code)
        ):
            return False

        events = self._query_rows(
            connection,
            "SELECT event_id, event_type, old_revision, new_revision, actor, reason_code, "
            "occurred_at_us FROM od_source_grant_events WHERE grant_id = :grant_id "
            "ORDER BY event_id",
            {"grant_id": request.grant_id},
        )
        if len(events) != 1:
            return False
        event = events[0]
        return (
            _bounded_int_or_none(event.get("event_id"), 1, 9_223_372_036_854_775_807) is not None
            and event.get("event_type") == "HOSTS_SEALED"
            and _bounded_int_or_none(event.get("old_revision"), 1, MAX_REVISION) == revision
            and _bounded_int_or_none(event.get("new_revision"), 1, MAX_REVISION) == revision
            and event.get("actor") == sealed_by
            and event.get("reason_code") == reason_code
            and _bounded_int_or_none(event.get("occurred_at_us"), 0, MAX_TIMESTAMP_US) == sealed_at
        )

    def revoke_grant(
        self,
        grant_id: str,
        *,
        expected_revision: int,
        actor: str,
        reason_code: str,
    ) -> GrantRevocationResult:
        """Revoke one current ALLOWED grant and append its audit event atomically.

        There is intentionally no API to create or allow a grant. A committed
        reservation remains eligible to reach HTTP after revoke; DB state
        cannot atomically cancel an already-authorized remote transport.
        """
        _validate_identifier(grant_id, "grant_id")
        _validate_identifier(actor, "actor")
        _validate_identifier(reason_code, "reason_code")
        if len(reason_code) > 64:
            raise SourceAttemptInputError("reason_code is too long")
        if type(expected_revision) is not int or not 1 <= expected_revision <= MAX_REVISION:
            raise SourceAttemptInputError("expected_revision is outside the supported range")
        connection = self._open_write_connection()
        try:
            effective_now = self._advance_clock(connection)
            row = self._fetch_one(
                connection,
                "SELECT decision, revision, created_at_us, updated_at_us, revoked_at_us, "
                "revocation_actor, revocation_reason_code FROM od_source_grants "
                "WHERE grant_id = :grant_id",
                {"grant_id": grant_id},
            )
            if row is not None and not _valid_grant_record_times(row, effective_now):
                raise SourceAttemptGateUnavailable("source grant timestamps are invalid")
            if row is None:
                result = GrantRevocationResult(GrantRevocationStatus.NOT_FOUND)
            elif row["decision"] == "DENIED" and row["revoked_at_us"] is not None:
                result = GrantRevocationResult(
                    GrantRevocationStatus.ALREADY_REVOKED,
                    _bounded_int(row["revision"], 1, MAX_REVISION),
                )
            elif row["decision"] != "ALLOWED":
                result = GrantRevocationResult(
                    GrantRevocationStatus.NOT_REVOCABLE,
                    _bounded_int(row["revision"], 1, MAX_REVISION),
                )
            elif row["revision"] != expected_revision:
                result = GrantRevocationResult(
                    GrantRevocationStatus.STALE_REVISION,
                    _bounded_int(row["revision"], 1, MAX_REVISION),
                )
            elif expected_revision >= MAX_REVISION:
                result = GrantRevocationResult(
                    GrantRevocationStatus.NOT_REVOCABLE,
                    expected_revision,
                )
            else:
                new_revision = expected_revision + 1
                connection.execute(
                    text(
                        "UPDATE od_source_grants SET decision = 'DENIED', "
                        "revision = :new_revision, revoked_at_us = :now_us, "
                        "revocation_actor = :actor, revocation_reason_code = :reason_code, "
                        "updated_at_us = :now_us WHERE grant_id = :grant_id"
                    ),
                    {
                        "grant_id": grant_id,
                        "new_revision": new_revision,
                        "actor": actor,
                        "reason_code": reason_code,
                        "now_us": effective_now,
                    },
                )
                result = GrantRevocationResult(GrantRevocationStatus.REVOKED, new_revision)
            self._commit(connection)
            return result
        except SourceAttemptGateUnavailable:
            raise
        except SQLAlchemyError:
            raise SourceAttemptGateUnavailable("source attempt gate is unavailable") from None
        except Exception:
            raise
        finally:
            connection.close()

    def _database_now_us(self, connection: Connection) -> int:
        """Read SQLite's UTC VFS clock at microsecond scale, without local time."""
        value = connection.exec_driver_sql(
            "SELECT CAST(ROUND((julianday('now') - 2440587.5) * 86400000000.0) AS INTEGER)"
        ).scalar_one()
        if type(value) is not int or not 0 < value <= MAX_TIMESTAMP_US:
            raise SourceAttemptGateUnavailable("SQLite database clock is invalid")
        return value

    def _open_write_connection(self) -> Connection:
        """Acquire the SQLite writer before any mutable clock or quota reads."""
        try:
            connection = self._engine.connect()
        except Exception:
            raise SourceAttemptGateUnavailable("source attempt gate is unavailable") from None
        try:
            self._verify_main_database(connection)
            connection.exec_driver_sql("PRAGMA foreign_keys = ON")
            connection.exec_driver_sql("PRAGMA busy_timeout = 5000")
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            self._verify_main_database(connection)
        except SourceAttemptGateUnavailable:
            connection.close()
            raise
        except SQLAlchemyError:
            connection.close()
            raise SourceAttemptGateUnavailable("source attempt gate is unavailable") from None
        except Exception:
            connection.close()
            raise SourceAttemptGateUnavailable("source attempt gate is unavailable") from None
        return connection

    def _open_readonly_connection(self) -> sqlite3.Connection:
        """Open the exact gate file with SQLite URI read-only enforcement."""
        database_uri = f"{self._expected_main_path.as_uri()}?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            uri=True,
            timeout=5.0,
            isolation_level=None,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA query_only = ON")
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or query_only[0] != 1:
                raise sqlite3.OperationalError("read-only mode is unavailable")
            self._verify_readonly_main_database(connection)
            connection.execute("BEGIN")
            return connection
        except Exception:
            connection.close()
            raise

    def _verify_readonly_main_database(self, connection: sqlite3.Connection) -> None:
        """Confirm the read-only handle resolves to the file identity pinned at init."""
        rows = connection.execute("PRAGMA database_list").fetchall()
        main_rows = [row for row in rows if row[1] == "main"]
        if len(main_rows) != 1 or not isinstance(main_rows[0][2], str) or not main_rows[0][2]:
            raise sqlite3.OperationalError("source attempt gate requires persistent SQLite")
        try:
            actual_path, actual_stat = _stat_database_file(Path(main_rows[0][2]))
        except (OSError, ValueError):
            raise sqlite3.OperationalError("source attempt database identity is invalid") from None
        actual_identity = (actual_stat.st_dev, actual_stat.st_ino)
        if (
            actual_path != self._expected_main_path
            or actual_identity != self._expected_main_identity
        ):
            raise sqlite3.OperationalError("source attempt database identity changed")

    def _verify_main_database(self, connection: Connection) -> None:
        """Reject memory/custom-creator/replaced databases before a write transaction."""
        rows = connection.exec_driver_sql("PRAGMA database_list").all()
        main_rows = [row for row in rows if row[1] == "main"]
        if len(main_rows) != 1 or not isinstance(main_rows[0][2], str) or not main_rows[0][2]:
            raise SourceAttemptGateUnavailable("source attempt gate requires persistent SQLite")
        try:
            actual_path, actual_stat = _stat_database_file(Path(main_rows[0][2]))
        except (OSError, ValueError):
            raise SourceAttemptGateUnavailable(
                "source attempt database identity is invalid"
            ) from None
        actual_identity = (actual_stat.st_dev, actual_stat.st_ino)
        if (
            actual_path != self._expected_main_path
            or actual_identity != self._expected_main_identity
        ):
            raise SourceAttemptGateUnavailable("source attempt database identity changed")

    def _advance_clock(self, connection: Connection) -> int:
        """Use a persisted high-water mark for expiry and bucket calculations."""
        row = self._fetch_one(
            connection,
            "SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1",
            {},
        )
        if row is None:
            raise SourceAttemptGateUnavailable("source attempt gate clock is missing")
        highwater = _bounded_int(row["highwater_us"], 0, MAX_TIMESTAMP_US)
        db_now = self._database_now_us(connection)
        effective_now = max(db_now, highwater)
        result = connection.execute(
            text("UPDATE od_source_gate_clock SET highwater_us = :now_us WHERE clock_id = 1"),
            {"now_us": effective_now},
        )
        if result.rowcount != 1:
            raise SourceAttemptGateUnavailable("source attempt gate clock update failed")
        return effective_now

    def _find_existing_attempt(
        self, connection: Connection, attempt: TransportAttempt
    ) -> AttemptReservation | None:
        """Resolve v1 identities from stored columns; no duplicate is a send permit.

        V1 does not store ``expected_rights_evidence_reference``. Therefore an
        identical persisted identity with a changed evidence reference is
        classified as ``DUPLICATE``; ``ID_CONFLICT`` applies only to a
        difference in fields that the v1 row does store. Neither result allows
        transport to send.
        """
        by_id = self._fetch_one(
            connection,
            "SELECT * FROM od_source_attempt_reservations WHERE attempt_id = :attempt_id",
            {"attempt_id": attempt.attempt_id},
        )
        by_request = self._fetch_one(
            connection,
            "SELECT * FROM od_source_attempt_reservations "
            "WHERE request_id = :request_id AND attempt_no = :attempt_no",
            {"request_id": attempt.request_id, "attempt_no": attempt.attempt_no},
        )
        if by_id is None and by_request is None:
            return None
        if (
            by_id is not None
            and by_request is not None
            and by_id["attempt_id"] != by_request["attempt_id"]
        ):
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        existing = by_id if by_id is not None else by_request
        if existing is not None and self._same_attempt_scope(existing, attempt):
            return AttemptReservation(ReservationStatus.DUPLICATE)
        return AttemptReservation(ReservationStatus.ID_CONFLICT)

    def _find_existing_v2_attempt(
        self, connection: Connection, scope: SourceAttemptScopeV2
    ) -> AttemptReservation | None:
        """Treat only an exact v2 tombstone as a duplicate; v1 rows are conflicts."""
        attempt = scope.attempt
        by_id = self._fetch_all(
            connection,
            "SELECT * FROM od_source_attempt_reservations WHERE attempt_id = :attempt_id",
            {"attempt_id": attempt.attempt_id},
        )
        by_request = self._fetch_all(
            connection,
            "SELECT * FROM od_source_attempt_reservations "
            "WHERE request_id = :request_id AND attempt_no = :attempt_no",
            {"request_id": attempt.request_id, "attempt_no": attempt.attempt_no},
        )
        if len(by_id) > 1 or len(by_request) > 1:
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        if not by_id and not by_request:
            sidecar = self._fetch_one(
                connection,
                "SELECT attempt_id FROM od_source_attempt_scope_v2 WHERE attempt_id = :attempt_id",
                {"attempt_id": attempt.attempt_id},
            )
            return (
                AttemptReservation(ReservationStatus.ID_CONFLICT) if sidecar is not None else None
            )
        if by_id and by_request and by_id[0]["attempt_id"] != by_request[0]["attempt_id"]:
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        existing = by_id[0] if by_id else by_request[0]
        if not self._same_attempt_scope(existing, attempt):
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        sidecar = self._fetch_one(
            connection,
            "SELECT scope_version, scope_json, scope_sha256, recorded_at_us "
            "FROM od_source_attempt_scope_v2 WHERE attempt_id = :attempt_id",
            {"attempt_id": attempt.attempt_id},
        )
        if sidecar is None:
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        expected_json, expected_digest = _scope_v2_bytes_and_digest(scope)
        stored_json = sidecar["scope_json"]
        if not isinstance(stored_json, str):
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        try:
            stored_bytes = stored_json.encode("utf-8", errors="strict")
        except UnicodeError:
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        if (
            sidecar["scope_version"] != 2
            or not _valid_sha256(sidecar["scope_sha256"])
            or hashlib.sha256(stored_bytes).hexdigest() != sidecar["scope_sha256"]
            or stored_bytes != expected_json
            or sidecar["scope_sha256"] != expected_digest
            or sidecar["recorded_at_us"] != existing["decided_at_us"]
        ):
            return AttemptReservation(ReservationStatus.ID_CONFLICT)
        return AttemptReservation(ReservationStatus.DUPLICATE)

    def _require_v2_schema(self, connection: Connection) -> None:
        """Fail closed before mutating the gate unless the v2 lineage is current."""
        reason = self._v2_schema_reason(connection)
        if reason is not None:
            raise SourceAttemptGateUnavailable("source attempt gate v2 schema is unavailable")

    def _v2_schema_reason(
        self, connection: Connection | sqlite3.Connection
    ) -> AttemptLookupReasonCode | None:
        """Validate the independent v2 marker and immutable sidecar DDL contract."""
        try:
            version_table = self._query_rows(
                connection,
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = :name",
                {"name": SOURCE_GATE_VERSION_TABLE},
            )
            if len(version_table) != 1:
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            versions = self._query_rows(
                connection,
                "SELECT version_num FROM alembic_version_source_gate",
                {},
            )
            if (
                len(versions) != 1
                or versions[0].get("version_num") != SOURCE_GATE_SCOPE_V2_REVISION
            ):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE

            sidecar_tables = self._query_rows(
                connection,
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name",
                {"name": SOURCE_ATTEMPT_SCOPE_V2_TABLE},
            )
            if len(sidecar_tables) != 1:
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            sidecar_sql = sidecar_tables[0].get("sql")
            if not isinstance(sidecar_sql, str):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            if not self._has_v2_scope_checks(sidecar_sql):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE

            columns = self._query_rows(
                connection,
                "PRAGMA table_info('od_source_attempt_scope_v2')",
                {},
            )
            expected_columns = {
                "attempt_id": ("TEXT", 1, 1),
                "scope_version": ("INTEGER", 1, 0),
                "scope_json": ("TEXT", 1, 0),
                "scope_sha256": ("TEXT", 1, 0),
                "recorded_at_us": ("INTEGER", 1, 0),
            }
            if len(columns) != len(expected_columns):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            for column in columns:
                column_name = column.get("name")
                expected = (
                    expected_columns.get(column_name) if isinstance(column_name, str) else None
                )
                actual = (
                    str(column.get("type", "")).upper(),
                    column.get("notnull"),
                    column.get("pk"),
                )
                if expected is None or actual != expected:
                    return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE

            foreign_keys = self._query_rows(
                connection,
                "PRAGMA foreign_key_list('od_source_attempt_scope_v2')",
                {},
            )
            if len(foreign_keys) != 1:
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            foreign_key = foreign_keys[0]
            if (
                foreign_key.get("table") != SOURCE_ATTEMPT_TABLE
                or foreign_key.get("from") != "attempt_id"
                or foreign_key.get("to") != "attempt_id"
                or str(foreign_key.get("on_delete", "")).upper() != "RESTRICT"
            ):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            if not self._has_scope_v2_primary_key_index(connection):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE

            triggers = self._query_rows(
                connection,
                "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = :table_name",
                {"table_name": SOURCE_ATTEMPT_SCOPE_V2_TABLE},
            )
            required_triggers = {
                "trg_od_source_attempt_scope_v2_immutable_update": "UPDATE",
                "trg_od_source_attempt_scope_v2_immutable_delete": "DELETE",
            }
            found_triggers = {row.get("name"): row for row in triggers}
            if set(found_triggers) != set(required_triggers):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            for name, event in required_triggers.items():
                trigger = found_triggers[name]
                trigger_sql = trigger.get("sql")
                if (
                    trigger.get("tbl_name") != SOURCE_ATTEMPT_SCOPE_V2_TABLE
                    or not isinstance(trigger_sql, str)
                    or not self._has_immutable_trigger(
                        trigger_sql, event, SOURCE_ATTEMPT_SCOPE_V2_TABLE
                    )
                ):
                    return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE

            base_tables = self._query_rows(
                connection,
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name",
                {"name": SOURCE_ATTEMPT_TABLE},
            )
            if len(base_tables) != 1:
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            base_create_sql = base_tables[0].get("sql")
            if not isinstance(base_create_sql, str):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            if not self._has_base_attempt_schema(connection, base_create_sql):
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
            encoding = self._query_rows(connection, "PRAGMA encoding", {})
            if len(encoding) != 1 or str(encoding[0].get("encoding", "")).upper() != "UTF-8":
                return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
        except (sqlite3.Error, SQLAlchemyError):
            return AttemptLookupReasonCode.DATABASE_UNAVAILABLE
        except Exception:
            return AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
        return None

    def _has_v2_scope_checks(self, create_sql: str) -> bool:
        """Require exactly the version, UTF-8 byte bound, and digest CHECK clauses."""
        checks = _extract_sqlite_check_expressions(create_sql)
        expected = {_normalize_check_expression(item) for item in _SCOPE_V2_CHECKS}
        return (
            checks is not None and len(checks) == len(_SCOPE_V2_CHECKS) and set(checks) == expected
        )

    def _has_immutable_trigger(self, trigger_sql: str, event: str, table_name: str) -> bool:
        """Require byte-for-byte semantic DDL after insignificant whitespace folding."""
        expected = _IMMUTABLE_TRIGGER_SQL.get((table_name, event))
        return expected is not None and " ".join(trigger_sql.split()) == " ".join(expected.split())

    def _has_base_attempt_schema(
        self,
        connection: Connection | sqlite3.Connection,
        create_sql: str,
    ) -> bool:
        """Verify the complete immutable base-table contract before any v2 result."""
        columns = self._query_rows(
            connection,
            "PRAGMA table_info('od_source_attempt_reservations')",
            {},
        )
        if len(columns) != len(_BASE_ATTEMPT_COLUMNS):
            return False
        for column in columns:
            column_name = column.get("name")
            expected = (
                _BASE_ATTEMPT_COLUMNS.get(column_name) if isinstance(column_name, str) else None
            )
            actual = (
                str(column.get("type", "")).upper(),
                column.get("notnull"),
                column.get("pk"),
            )
            if expected is None or actual != expected or column.get("dflt_value") is not None:
                return False

        check_expressions = _extract_sqlite_check_expressions(create_sql)
        expected_checks = {_normalize_check_expression(item) for item in _BASE_ATTEMPT_CHECKS}
        if (
            check_expressions is None
            or len(check_expressions) != len(_BASE_ATTEMPT_CHECKS)
            or set(check_expressions) != expected_checks
        ):
            return False

        foreign_keys = self._query_rows(
            connection,
            "PRAGMA foreign_key_list('od_source_attempt_reservations')",
            {},
        )
        if len(foreign_keys) != 1:
            return False
        foreign_key = foreign_keys[0]
        if (
            foreign_key.get("table") != SOURCE_GATE_POLICY_TABLE
            or foreign_key.get("from") != "quota_key"
            or foreign_key.get("to") != "quota_key"
            or str(foreign_key.get("on_delete", "")).upper() != "RESTRICT"
        ):
            return False

        triggers = self._query_rows(
            connection,
            "SELECT name, tbl_name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name = :table_name",
            {"table_name": SOURCE_ATTEMPT_TABLE},
        )
        required_triggers = {
            "trg_od_source_attempt_immutable_update": "UPDATE",
            "trg_od_source_attempt_immutable_delete": "DELETE",
        }
        found_triggers = {row.get("name"): row for row in triggers}
        if set(found_triggers) != set(required_triggers):
            return False
        for name, event in required_triggers.items():
            trigger = found_triggers[name]
            trigger_sql = trigger.get("sql")
            if (
                trigger.get("tbl_name") != SOURCE_ATTEMPT_TABLE
                or not isinstance(trigger_sql, str)
                or not self._has_immutable_trigger(trigger_sql, event, SOURCE_ATTEMPT_TABLE)
            ):
                return False
        return self._has_attempt_request_unique_key(connection)

    def _has_scope_v2_primary_key_index(self, connection: Connection | sqlite3.Connection) -> bool:
        """Require the sidecar's only index to be its complete attempt-id primary key."""
        indexes = self._query_rows(
            connection,
            "PRAGMA index_list('od_source_attempt_scope_v2')",
            {},
        )
        if len(indexes) != 1:
            return False
        index = indexes[0]
        index_name = index.get("name")
        if (
            index.get("unique") != 1
            or index.get("partial") != 0
            or index.get("origin") != "pk"
            or not isinstance(index_name, str)
        ):
            return False
        quoted_name = _quote_sqlite_literal(index_name)
        columns = self._query_rows(connection, f"PRAGMA index_info({quoted_name})", {})
        return tuple(column.get("name") for column in columns) == ("attempt_id",)

    def _has_attempt_request_unique_key(self, connection: Connection | sqlite3.Connection) -> bool:
        """Require exactly the PK and full non-partial request/attempt unique indexes."""
        indexes = self._query_rows(
            connection,
            "PRAGMA index_list('od_source_attempt_reservations')",
            {},
        )
        if len(indexes) != 2:
            return False
        found: set[tuple[str, tuple[object, ...]]] = set()
        for index in indexes:
            index_name = index.get("name")
            if (
                index.get("unique") != 1
                or index.get("partial") != 0
                or not isinstance(index_name, str)
            ):
                continue
            quoted_name = _quote_sqlite_literal(index_name)
            columns = self._query_rows(connection, f"PRAGMA index_info({quoted_name})", {})
            found.add((str(index.get("origin")), tuple(column.get("name") for column in columns)))
        return found == {
            ("pk", ("attempt_id",)),
            ("u", ("request_id", "attempt_no")),
        }

    def _same_attempt_scope(self, row: Mapping[str, object], attempt: TransportAttempt) -> bool:
        """Compare all immutable fields available in a v1 reservation row.

        The v1 schema omits the expected evidence reference, so it is checked
        against the grant for a fresh reservation but cannot affect duplicate
        versus conflict classification for an already stored identity.
        """
        expected = (
            attempt.attempt_id,
            attempt.request_id,
            attempt.attempt_no,
            attempt.grant_id,
            attempt.expected_revision,
            attempt.source,
            attempt.canonical_model,
            attempt.operation,
            attempt.principal_scope,
            attempt.host,
            attempt.run_key,
        )
        actual = tuple(
            row[key]
            for key in (
                "attempt_id",
                "request_id",
                "attempt_no",
                "grant_id",
                "expected_revision",
                "source",
                "canonical_model",
                "operation",
                "principal_scope",
                "host",
                "run_key",
            )
        )
        return actual == expected

    def _evaluate_attempt_v2(
        self,
        connection: Connection,
        scope: SourceAttemptScopeV2,
        effective_now: int,
    ) -> tuple[
        AttemptReservation,
        tuple[str, _BucketDecision] | None,
        _PolicySnapshot | None,
    ]:
        """Check grant first, then bind any consuming decision to the pinned bucket."""
        attempt = scope.attempt
        grant = self._fetch_one(
            connection,
            "SELECT * FROM od_source_grants WHERE grant_id = :grant_id",
            {"grant_id": attempt.grant_id},
        )
        grant_denial = self._grant_denial(connection, grant, attempt, effective_now)
        if grant_denial is not None:
            return AttemptReservation(ReservationStatus.DENIED, grant_denial), None, None
        if self._scope_budget_mismatch(connection, scope, effective_now):
            return (
                AttemptReservation(ReservationStatus.DENIED, "budget_scope_mismatch"),
                None,
                None,
            )
        return self._evaluate_attempt(connection, attempt, effective_now)

    def _scope_budget_mismatch(
        self,
        connection: Connection,
        scope: SourceAttemptScopeV2,
        effective_now: int,
    ) -> bool:
        """Deny a v2 request if a valid current gate bucket differs from its pins.

        Missing or invalid gate configuration is left to the regular evaluator,
        which persists its established fixed denial code without granting a
        request. This comparison does not establish source rights.
        """
        binding = self._fetch_one(
            connection,
            "SELECT * FROM od_source_gate_source_bindings WHERE source = :source",
            {"source": scope.attempt.source},
        )
        quota_key = self._binding_value(binding, effective_now)
        if quota_key is None:
            return False
        if quota_key != scope.budget_quota_key or binding is None:
            return True
        if binding["binding_sha256"] != scope.budget_binding_sha256:
            return True
        policy = self._fetch_one(
            connection,
            "SELECT * FROM od_source_gate_policies WHERE quota_key = :quota_key",
            {"quota_key": quota_key},
        )
        values = self._policy_values(policy, effective_now)
        if values is None:
            return False
        policy_revision, _rate, _capacity = values
        return (
            policy_revision != scope.budget_policy_revision
            or policy is None
            or policy["policy_sha256"] != scope.budget_policy_sha256
        )

    def _insert_attempt_scope_v2(
        self,
        connection: Connection,
        scope: SourceAttemptScopeV2,
        recorded_at_us: int,
    ) -> None:
        """Insert immutable v2 metadata inside the reservation transaction."""
        scope_json, scope_sha256 = _scope_v2_bytes_and_digest(scope)
        connection.execute(
            text(
                "INSERT INTO od_source_attempt_scope_v2 "
                "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
                "VALUES (:attempt_id, 2, :scope_json, :scope_sha256, :recorded_at_us)"
            ),
            {
                "attempt_id": scope.attempt.attempt_id,
                "scope_json": scope_json.decode("utf-8"),
                "scope_sha256": scope_sha256,
                "recorded_at_us": recorded_at_us,
            },
        )

    def _lookup_v2_snapshot(
        self,
        scope: SourceAttemptScopeV2,
        expected_json: bytes,
        expected_sha256: str,
        connection: sqlite3.Connection,
    ) -> GateAttemptLookupResult:
        """Resolve both immutable base keys and compare the whole v2 sidecar."""
        attempt = scope.attempt
        by_id = self._query_rows(
            connection,
            "SELECT * FROM od_source_attempt_reservations WHERE attempt_id = :attempt_id",
            {"attempt_id": attempt.attempt_id},
        )
        by_request = self._query_rows(
            connection,
            "SELECT * FROM od_source_attempt_reservations "
            "WHERE request_id = :request_id AND attempt_no = :attempt_no",
            {"request_id": attempt.request_id, "attempt_no": attempt.attempt_no},
        )
        if len(by_id) > 1 or len(by_request) > 1:
            return AttemptLookupIndeterminate("INDETERMINATE", AttemptLookupReasonCode.ID_CONFLICT)
        if by_id and by_request and by_id[0]["attempt_id"] != by_request[0]["attempt_id"]:
            return AttemptLookupIndeterminate("INDETERMINATE", AttemptLookupReasonCode.ID_CONFLICT)
        if not by_id and not by_request:
            foreign_key_violations = self._query_rows(
                connection,
                "PRAGMA foreign_key_check('od_source_attempt_scope_v2')",
                {},
            )
            if foreign_key_violations:
                return AttemptLookupIndeterminate(
                    "INDETERMINATE", AttemptLookupReasonCode.SCOPE_CORRUPT
                )
            orphan = self._query_rows(
                connection,
                "SELECT attempt_id FROM od_source_attempt_scope_v2 WHERE attempt_id = :attempt_id",
                {"attempt_id": attempt.attempt_id},
            )
            if orphan:
                return AttemptLookupIndeterminate(
                    "INDETERMINATE", AttemptLookupReasonCode.SCOPE_CORRUPT
                )
            return AttemptLookupAbsent("ABSENT", expected_sha256)

        row = by_id[0] if by_id else by_request[0]
        if not self._same_attempt_scope(row, attempt):
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.SCOPE_MISMATCH
            )
        sidecars = self._query_rows(
            connection,
            "SELECT scope_version, scope_json, scope_sha256, recorded_at_us "
            "FROM od_source_attempt_scope_v2 WHERE attempt_id = :attempt_id",
            {"attempt_id": attempt.attempt_id},
        )
        if not sidecars:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.LEGACY_SCOPE_UNVERIFIABLE
            )
        if len(sidecars) != 1:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.SCOPE_CORRUPT
            )
        sidecar = sidecars[0]
        try:
            stored_json = sidecar.get("scope_json")
            if not isinstance(stored_json, str):
                raise ValueError("scope JSON is not text")
            stored_bytes = stored_json.encode("utf-8", errors="strict")
        except (UnicodeError, ValueError):
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.SCOPE_CORRUPT
            )
        stored_sha256 = sidecar.get("scope_sha256")
        if (
            sidecar.get("scope_version") != 2
            or len(stored_bytes) > _MAX_SCOPE_V2_BYTES
            or not _valid_sha256(stored_sha256)
            or hashlib.sha256(stored_bytes).hexdigest() != stored_sha256
        ):
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.SCOPE_CORRUPT
            )
        if stored_bytes != expected_json or stored_sha256 != expected_sha256:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.SCOPE_MISMATCH
            )
        recorded_at = _bounded_int_or_none(sidecar.get("recorded_at_us"), 0, MAX_TIMESTAMP_US)
        decided_at = _bounded_int_or_none(row.get("decided_at_us"), 0, MAX_TIMESTAMP_US)
        if recorded_at is None or decided_at is None or recorded_at != decided_at:
            return AttemptLookupIndeterminate(
                "INDETERMINATE", AttemptLookupReasonCode.RECORD_CORRUPT
            )
        snapshot_values = (
            row.get("quota_key"),
            row.get("policy_revision"),
            row.get("rate_microtokens_per_second"),
            row.get("capacity_microtokens"),
        )
        if any(value is not None for value in snapshot_values):
            quota_key, policy_revision_value, rate_value, capacity_value = snapshot_values
            if (
                not _valid_identifier(quota_key)
                or quota_key != scope.budget_quota_key
                or _bounded_int_or_none(policy_revision_value, 1, MAX_REVISION)
                != scope.budget_policy_revision
                or _bounded_int_or_none(rate_value, 1, MAX_RATE_MICROTOKENS_PER_SECOND) is None
                or _bounded_int_or_none(
                    capacity_value, MICROTOKENS_PER_TOKEN, MAX_CAPACITY_MICROTOKENS
                )
                is None
            ):
                return AttemptLookupIndeterminate(
                    "INDETERMINATE", AttemptLookupReasonCode.RECORD_CORRUPT
                )

        if row.get("outcome") == "RESERVED":
            reserved_at = _bounded_int_or_none(row.get("reserved_at_us"), 0, MAX_TIMESTAMP_US)
            policy_revision = _bounded_int_or_none(row.get("policy_revision"), 1, MAX_REVISION)
            rate = _bounded_int_or_none(
                row.get("rate_microtokens_per_second"), 1, MAX_RATE_MICROTOKENS_PER_SECOND
            )
            capacity = _bounded_int_or_none(
                row.get("capacity_microtokens"), MICROTOKENS_PER_TOKEN, MAX_CAPACITY_MICROTOKENS
            )
            if (
                row.get("denial_code") is not None
                or reserved_at is None
                or reserved_at != decided_at
                or row.get("quota_key") != scope.budget_quota_key
                or policy_revision != scope.budget_policy_revision
                or rate is None
                or capacity is None
                or row.get("retry_after_us") is not None
            ):
                return AttemptLookupIndeterminate(
                    "INDETERMINATE", AttemptLookupReasonCode.RECORD_CORRUPT
                )
            return AttemptLookupReserved(
                "RESERVED",
                attempt.attempt_id,
                expected_sha256,
                scope.source_policy_revision,
                scope.budget_policy_revision,
                reserved_at,
            )
        if row.get("outcome") == "DENIED":
            denial_code = row.get("denial_code")
            retry_after = row.get("retry_after_us")
            if (
                not isinstance(denial_code, str)
                or denial_code not in _ATTEMPT_DENIAL_CODES
                or row.get("reserved_at_us") is not None
                or (
                    retry_after is not None
                    and _bounded_int_or_none(retry_after, 1, MAX_RETRY_AFTER_US) is None
                )
            ):
                return AttemptLookupIndeterminate(
                    "INDETERMINATE", AttemptLookupReasonCode.RECORD_CORRUPT
                )
            return AttemptLookupDenied(
                "DENIED",
                attempt.attempt_id,
                expected_sha256,
                scope.source_policy_revision,
                denial_code,
            )
        return AttemptLookupIndeterminate("INDETERMINATE", AttemptLookupReasonCode.RECORD_CORRUPT)

    def _evaluate_attempt(
        self, connection: Connection, attempt: TransportAttempt, effective_now: int
    ) -> tuple[
        AttemptReservation,
        tuple[str, _BucketDecision] | None,
        _PolicySnapshot | None,
    ]:
        """Check grant, source mapping, policy, and shared bucket under one write lock."""
        grant = self._fetch_one(
            connection,
            "SELECT * FROM od_source_grants WHERE grant_id = :grant_id",
            {"grant_id": attempt.grant_id},
        )
        denial = self._grant_denial(connection, grant, attempt, effective_now)
        if denial is not None:
            return AttemptReservation(ReservationStatus.DENIED, denial), None, None

        binding = self._fetch_one(
            connection,
            "SELECT * FROM od_source_gate_source_bindings WHERE source = :source",
            {"source": attempt.source},
        )
        quota_key_value = self._binding_value(binding, effective_now)
        if quota_key_value is None:
            return (
                AttemptReservation(ReservationStatus.DENIED, "source_quota_unconfigured"),
                None,
                None,
            )
        quota_key = quota_key_value
        policy = self._fetch_one(
            connection,
            "SELECT * FROM od_source_gate_policies WHERE quota_key = :quota_key",
            {"quota_key": quota_key},
        )
        policy_values = self._policy_values(policy, effective_now)
        if policy_values is None:
            return (
                AttemptReservation(ReservationStatus.DENIED, "source_policy_unconfigured"),
                None,
                None,
            )
        policy_revision, rate, capacity = policy_values
        policy_snapshot = _PolicySnapshot(quota_key, policy_revision, rate, capacity)

        state = self._fetch_one(
            connection,
            "SELECT * FROM od_source_gate_state WHERE quota_key = :quota_key",
            {"quota_key": quota_key},
        )
        state_values = self._state_values(state, effective_now)
        if state_values is None:
            return (
                AttemptReservation(ReservationStatus.DENIED, "source_quota_state_unavailable"),
                None,
                policy_snapshot,
            )
        state_revision, tokens, remainder, last_refill = state_values
        if state_revision > policy_revision:
            return (
                AttemptReservation(ReservationStatus.DENIED, "source_policy_revision_invalid"),
                None,
                policy_snapshot,
            )
        if state_revision < policy_revision:
            tokens = min(tokens, capacity)
            remainder = 0
            last_refill = effective_now
        elif tokens > capacity:
            return (
                AttemptReservation(ReservationStatus.DENIED, "source_quota_state_invalid"),
                None,
                policy_snapshot,
            )

        bucket = _refill_bucket(
            tokens_micro=tokens,
            remainder=remainder,
            last_refill_us=last_refill,
            effective_now_us=effective_now,
            policy_revision=policy_revision,
            rate_microtokens_per_second=rate,
            capacity_microtokens=capacity,
        )
        if not bucket.allowed:
            denial_result = AttemptReservation(
                ReservationStatus.DENIED,
                "source_rate_limited",
                bucket.retry_after_us,
            )
            return denial_result, (quota_key, bucket), policy_snapshot
        debited = _BucketDecision(
            tokens_micro=bucket.tokens_micro - MICROTOKENS_PER_TOKEN,
            remainder=bucket.remainder,
            last_refill_us=bucket.last_refill_us,
            policy_revision=bucket.policy_revision,
            allowed=True,
            retry_after_us=None,
        )
        return (
            AttemptReservation(ReservationStatus.RESERVED_NEW, reserved_at_us=effective_now),
            (quota_key, debited),
            policy_snapshot,
        )

    def _grant_denial(
        self,
        connection: Connection | sqlite3.Connection,
        grant: Mapping[str, object] | None,
        attempt: TransportAttempt,
        effective_now: int,
    ) -> str | None:
        """Return a safe fixed reason unless an exact current grant permits this scope."""
        if grant is None:
            return "grant_not_found"
        if grant["decision"] != "ALLOWED":
            return "grant_not_allowed"
        if not _bounded_int_or_none(grant["revision"], 1, MAX_REVISION):
            return "grant_invalid"
        created_at = _bounded_int_or_none(grant["created_at_us"], 0, MAX_TIMESTAMP_US)
        updated_at = _bounded_int_or_none(grant["updated_at_us"], 0, MAX_TIMESTAMP_US)
        revoked_at_value = grant["revoked_at_us"]
        if (
            created_at is None
            or updated_at is None
            or created_at > updated_at
            or updated_at > effective_now
            or revoked_at_value is not None
            or grant["revocation_actor"] is not None
            or grant["revocation_reason_code"] is not None
        ):
            return "grant_invalid"
        if grant["revision"] != attempt.expected_revision:
            return "grant_revision_mismatch"
        expected_scope = (
            attempt.source,
            attempt.canonical_model,
            attempt.operation,
            attempt.principal_scope,
        )
        actual_scope = tuple(
            grant[key] for key in ("source", "canonical_model", "operation", "principal_scope")
        )
        if actual_scope != expected_scope:
            return "grant_scope_mismatch"
        not_before = _bounded_int_or_none(grant["not_before_us"], 0, MAX_TIMESTAMP_US)
        expires = _bounded_int_or_none(grant["expires_at_us"], 1, MAX_TIMESTAMP_US)
        if not_before is None or expires is None or not_before >= expires:
            return "grant_invalid"
        if effective_now < not_before:
            return "grant_not_yet_valid"
        if effective_now >= expires:
            return "grant_expired"
        if not _valid_allowed_declaration(grant, effective_now):
            return "grant_evidence_incomplete"
        if (
            attempt.expected_rights_evidence_reference is not None
            and grant.get("rights_evidence_reference") != attempt.expected_rights_evidence_reference
        ):
            return "grant_evidence_reference_mismatch"
        condition_text = grant["conditions_json"]
        if not isinstance(condition_text, str):
            return "grant_conditions_invalid"
        try:
            conditions = json.loads(condition_text)
        except (TypeError, json.JSONDecodeError):
            return "grant_conditions_invalid"
        if not isinstance(conditions, list) or any(
            not isinstance(condition, str) or not condition.strip() for condition in conditions
        ):
            return "grant_conditions_invalid"
        if conditions:
            return "grant_conditions_unresolved"
        host_set = self._fetch_one(
            connection,
            "SELECT revision, host_count, sealed_at_us, sealed_by, reason_code "
            "FROM od_source_grant_host_sets WHERE grant_id = :grant_id",
            {"grant_id": attempt.grant_id},
        )
        if host_set is None:
            return "grant_hosts_unsealed"
        seal_revision = _bounded_int_or_none(host_set["revision"], 1, MAX_REVISION)
        host_count = _bounded_int_or_none(host_set["host_count"], 1, 10_000)
        sealed_at = _bounded_int_or_none(host_set["sealed_at_us"], 0, MAX_TIMESTAMP_US)
        if (
            seal_revision != grant["revision"]
            or host_count is None
            or sealed_at is None
            or sealed_at < updated_at
            or sealed_at > effective_now
            or not _valid_identifier(host_set["sealed_by"])
            or not _valid_identifier(host_set["reason_code"])
        ):
            return "grant_hosts_invalid"
        hosts = self._fetch_all(
            connection,
            "SELECT host FROM od_source_grant_hosts WHERE grant_id = :grant_id",
            {"grant_id": attempt.grant_id},
        )
        if len(hosts) != host_count:
            return "grant_hosts_invalid"
        normalized_hosts: set[str] = set()
        try:
            for host_row in hosts:
                host_value = host_row["host"]
                normalized = _normalize_host(host_value)
                if normalized != host_value:
                    return "grant_hosts_invalid"
                normalized_hosts.add(normalized)
        except SourceAttemptInputError:
            return "grant_hosts_invalid"
        if not normalized_hosts or attempt.host not in normalized_hosts:
            return "grant_host_mismatch"
        return None

    def _binding_value(self, row: Mapping[str, object] | None, effective_now: int) -> str | None:
        """Validate an immutable, operator-reviewed source-to-quota mapping."""
        if row is None:
            return None
        quota_key = row["quota_key"]
        configured_at = _bounded_int_or_none(row["configured_at_us"], 0, MAX_TIMESTAMP_US)
        if (
            not isinstance(quota_key, str)
            or not _valid_identifier(quota_key)
            or not isinstance(row["binding_reference"], str)
            or not row["binding_reference"].strip()
            or not _valid_sha256(row["binding_sha256"])
            or not _valid_identifier(row["configured_by"])
            or configured_at is None
            or configured_at > effective_now
        ):
            return None
        return quota_key

    def _policy_values(
        self, row: Mapping[str, object] | None, effective_now: int
    ) -> tuple[int, int, int] | None:
        """Validate policy parameters before arithmetic; malformed policy denies."""
        if row is None:
            return None
        revision = _bounded_int_or_none(row["policy_revision"], 1, MAX_REVISION)
        rate = _bounded_int_or_none(
            row["rate_microtokens_per_second"], 1, MAX_RATE_MICROTOKENS_PER_SECOND
        )
        capacity = _bounded_int_or_none(
            row["capacity_microtokens"], MICROTOKENS_PER_TOKEN, MAX_CAPACITY_MICROTOKENS
        )
        if revision is None or rate is None or capacity is None:
            return None
        if not _valid_sha256(row["policy_sha256"]) or not _valid_identifier(row["configured_by"]):
            return None
        if not isinstance(row["policy_reference"], str) or not row["policy_reference"].strip():
            return None
        configured_at = _bounded_int_or_none(row["configured_at_us"], 0, MAX_TIMESTAMP_US)
        if configured_at is None or configured_at > effective_now:
            return None
        return revision, rate, capacity

    def _state_values(
        self, row: Mapping[str, object] | None, effective_now: int
    ) -> tuple[int, int, int, int] | None:
        """Validate existing bucket state without treating missing rows as full."""
        if row is None:
            return None
        revision = _bounded_int_or_none(row["policy_revision"], 1, MAX_REVISION)
        tokens = _bounded_int_or_none(row["tokens_micro"], 0, MAX_CAPACITY_MICROTOKENS)
        remainder = _bounded_int_or_none(row["remainder"], 0, MICROSECONDS_PER_SECOND - 1)
        last_refill = _bounded_int_or_none(row["last_refill_us"], 0, MAX_TIMESTAMP_US)
        if None in (revision, tokens, remainder, last_refill):
            return None
        if last_refill is None or last_refill > effective_now:
            return None
        if revision is None or tokens is None or remainder is None:
            return None
        return revision, tokens, remainder, last_refill

    def _persist_bucket(
        self, connection: Connection, quota_key: str, bucket: _BucketDecision
    ) -> None:
        """Persist refill/debit and policy revision in the caller's transaction."""
        result = connection.execute(
            text(
                "UPDATE od_source_gate_state SET tokens_micro = :tokens, "
                "remainder = :remainder, last_refill_us = :last_refill, "
                "policy_revision = :revision WHERE quota_key = :quota_key"
            ),
            {
                "tokens": bucket.tokens_micro,
                "remainder": bucket.remainder,
                "last_refill": bucket.last_refill_us,
                "revision": bucket.policy_revision,
                "quota_key": quota_key,
            },
        )
        if result.rowcount != 1:
            raise SourceAttemptGateUnavailable("source quota state update failed")

    def _insert_attempt(
        self,
        connection: Connection,
        attempt: TransportAttempt,
        decision: AttemptReservation,
        effective_now: int,
        policy_snapshot: _PolicySnapshot | None,
    ) -> None:
        """Persist either a committed one-use reservation or a dead denial ID."""
        outcome = "RESERVED" if decision.status is ReservationStatus.RESERVED_NEW else "DENIED"
        connection.execute(
            text(
                "INSERT INTO od_source_attempt_reservations "
                "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
                "canonical_model, operation, principal_scope, host, run_key, outcome, "
                "denial_code, retry_after_us, decided_at_us, reserved_at_us, quota_key, "
                "policy_revision, rate_microtokens_per_second, capacity_microtokens) VALUES "
                "(:attempt_id, :request_id, :attempt_no, :grant_id, :expected_revision, :source, "
                ":canonical_model, :operation, :principal_scope, :host, :run_key, :outcome, "
                ":denial_code, :retry_after_us, :decided_at_us, :reserved_at_us, :quota_key, "
                ":policy_revision, :rate_microtokens_per_second, :capacity_microtokens)"
            ),
            {
                "attempt_id": attempt.attempt_id,
                "request_id": attempt.request_id,
                "attempt_no": attempt.attempt_no,
                "grant_id": attempt.grant_id,
                "expected_revision": attempt.expected_revision,
                "source": attempt.source,
                "canonical_model": attempt.canonical_model,
                "operation": attempt.operation,
                "principal_scope": attempt.principal_scope,
                "host": attempt.host,
                "run_key": attempt.run_key,
                "outcome": outcome,
                "denial_code": decision.denial_code,
                "retry_after_us": decision.retry_after_us,
                "decided_at_us": effective_now,
                "reserved_at_us": decision.reserved_at_us,
                "quota_key": None if policy_snapshot is None else policy_snapshot.quota_key,
                "policy_revision": None if policy_snapshot is None else policy_snapshot.revision,
                "rate_microtokens_per_second": (
                    None if policy_snapshot is None else policy_snapshot.rate_microtokens_per_second
                ),
                "capacity_microtokens": (
                    None if policy_snapshot is None else policy_snapshot.capacity_microtokens
                ),
            },
        )

    def _fetch_one(
        self,
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> dict[str, object] | None:
        """Execute a parameterized query and return its first mapping row."""
        if isinstance(connection, sqlite3.Connection):
            rows = self._query_rows(connection, statement, parameters)
            return None if not rows else rows[0]
        result = connection.execute(text(statement), parameters).mappings().first()
        return None if result is None else dict(result)

    def _fetch_all(
        self,
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        """Execute a parameterized query and return mapping rows."""
        if isinstance(connection, sqlite3.Connection):
            return self._query_rows(connection, statement, parameters)
        result = connection.execute(text(statement), parameters).mappings().all()
        return [dict(row) for row in result]

    def _query_rows(
        self,
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        """Run a parameterized metadata/data query on either supported connection."""
        if isinstance(connection, sqlite3.Connection):
            return [dict(row) for row in connection.execute(statement, parameters).fetchall()]
        result = connection.execute(text(statement), parameters).mappings().all()
        return [dict(row) for row in result]

    def _commit(self, connection: Connection) -> None:
        """Commit or fail closed when the database acknowledgement is uncertain."""
        try:
            connection.commit()
        except Exception:
            raise SourceAttemptGateUnavailable("source attempt gate commit is uncertain") from None


def _scope_v2_document(scope: SourceAttemptScopeV2) -> dict[str, object]:
    """Return the closed v2 JSON object; values are identifiers, pins, and integers only."""
    attempt = scope.attempt
    if attempt.expected_rights_evidence_reference is not None:
        raise SourceAttemptInputError(
            "v2 source attempt scope does not support expected rights evidence references"
        )
    return {
        "attempt_id": attempt.attempt_id,
        "attempt_no": attempt.attempt_no,
        "budget_binding_sha256": scope.budget_binding_sha256,
        "budget_policy_revision": scope.budget_policy_revision,
        "budget_policy_sha256": scope.budget_policy_sha256,
        "budget_quota_key": scope.budget_quota_key,
        "canonical_model": attempt.canonical_model,
        "capture_intent_id": scope.capture_intent_id,
        "expected_grant_revision": attempt.expected_revision,
        "grant_id": attempt.grant_id,
        "host": attempt.host,
        "operation": attempt.operation,
        "principal_scope": attempt.principal_scope,
        "request_id": attempt.request_id,
        "request_summary_sha256": scope.request_summary_sha256,
        "run_key": attempt.run_key,
        "scope_version": 2,
        "source": attempt.source,
        "source_policy_id": scope.source_policy_id,
        "source_policy_revision": scope.source_policy_revision,
        "source_policy_sha256": scope.source_policy_sha256,
    }


def _scope_v2_bytes(scope: SourceAttemptScopeV2) -> bytes:
    """Encode the complete v2 scope in bounded canonical UTF-8 JSON."""
    encoded = json.dumps(
        _scope_v2_document(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8", errors="strict")
    if not 1 <= len(encoded) <= _MAX_SCOPE_V2_BYTES:
        raise SourceAttemptInputError("canonical v2 scope exceeds the supported size")
    return encoded


def _scope_v2_bytes_and_digest(scope: SourceAttemptScopeV2) -> tuple[bytes, str]:
    """Return canonical UTF-8 scope bytes and their lowercase SHA-256 digest."""
    encoded = _scope_v2_bytes(scope)
    return encoded, hashlib.sha256(encoded).hexdigest()


def _extract_sqlite_check_expressions(create_sql: str) -> tuple[str, ...] | None:
    """Extract balanced CHECK bodies so schema verification sees their full clauses."""
    expressions: list[str] = []
    for match in re.finditer(r"\bCHECK\s*\(", create_sql, re.IGNORECASE):
        opening = match.end() - 1
        parsed = _sqlite_parenthesized_content(create_sql, opening)
        if parsed is None:
            return None
        expression, _closing = parsed
        expressions.append(_normalize_check_expression(expression))
    return tuple(expressions)


def _normalize_check_expression(expression: str) -> str:
    """Normalize whitespace and redundant outer parentheses in one CHECK body."""
    normalized = " ".join(expression.split()).lower()
    while normalized.startswith("(") and normalized.endswith(")"):
        parsed = _sqlite_parenthesized_content(normalized, 0)
        if parsed is None or parsed[1] != len(normalized) - 1:
            break
        normalized = " ".join(parsed[0].split()).lower()
    return normalized


def _sqlite_parenthesized_content(value: str, opening: int) -> tuple[str, int] | None:
    """Return the content and matching close of a SQLite SQL parenthesis group."""
    if opening >= len(value) or value[opening] != "(":
        return None
    depth = 0
    quote: str | None = None
    index = opening
    while index < len(value):
        char = value[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
        elif char == "[":
            quote = "]"
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return value[opening + 1 : index], index
            if depth < 0:
                return None
        index += 1
    return None


def _quote_sqlite_literal(value: str) -> str:
    """Quote one SQLite string literal used only by PRAGMA metadata inspection."""
    return "'" + value.replace("'", "''") + "'"


def _refill_bucket(
    *,
    tokens_micro: int,
    remainder: int,
    last_refill_us: int,
    effective_now_us: int,
    policy_revision: int,
    rate_microtokens_per_second: int,
    capacity_microtokens: int,
) -> _BucketDecision:
    """Refill a token bucket using bounded integer arithmetic and microsecond time."""
    if (
        type(tokens_micro) is not int
        or not 0 <= tokens_micro <= MAX_CAPACITY_MICROTOKENS
        or type(remainder) is not int
        or not 0 <= remainder < MICROSECONDS_PER_SECOND
        or type(last_refill_us) is not int
        or not 0 <= last_refill_us <= effective_now_us <= MAX_TIMESTAMP_US
        or type(rate_microtokens_per_second) is not int
        or not 1 <= rate_microtokens_per_second <= MAX_RATE_MICROTOKENS_PER_SECOND
        or type(capacity_microtokens) is not int
        or not MICROTOKENS_PER_TOKEN <= capacity_microtokens <= MAX_CAPACITY_MICROTOKENS
        or type(policy_revision) is not int
        or not 1 <= policy_revision <= MAX_REVISION
    ):
        raise SourceAttemptGateUnavailable("source quota parameters are invalid")
    elapsed_us = effective_now_us - last_refill_us
    deficit_to_full = capacity_microtokens - tokens_micro
    if deficit_to_full <= 0:
        tokens_micro = capacity_microtokens
        remainder = 0
    elif elapsed_us > 0:
        numerator_to_full = deficit_to_full * MICROSECONDS_PER_SECOND - remainder
        elapsed_to_full = max(
            0, (numerator_to_full + rate_microtokens_per_second - 1) // rate_microtokens_per_second
        )
        if elapsed_us >= elapsed_to_full:
            tokens_micro = capacity_microtokens
            remainder = 0
        else:
            earned_numerator = elapsed_us * rate_microtokens_per_second + remainder
            earned_microtokens, remainder = divmod(earned_numerator, MICROSECONDS_PER_SECOND)
            tokens_micro += earned_microtokens
    wait_numerator = (MICROTOKENS_PER_TOKEN - tokens_micro) * MICROSECONDS_PER_SECOND - remainder
    if wait_numerator <= 0:
        allowed = True
        retry_after = None
    else:
        retry_after = (
            wait_numerator + rate_microtokens_per_second - 1
        ) // rate_microtokens_per_second
        if retry_after > MAX_RETRY_AFTER_US:
            raise SourceAttemptGateUnavailable("source quota retry interval is invalid")
        allowed = False
    return _BucketDecision(
        tokens_micro=tokens_micro,
        remainder=remainder,
        last_refill_us=effective_now_us,
        policy_revision=policy_revision,
        allowed=allowed,
        retry_after_us=retry_after,
    )


def _validate_identifier(value: object, name: str) -> str:
    """Validate a bounded opaque identifier without accepting whitespace/secrets."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TEXT_LENGTH
        or not _IDENTIFIER_PATTERN.fullmatch(value)
    ):
        raise SourceAttemptInputError(f"{name} must be a bounded opaque identifier")
    return value


def _valid_identifier(value: object) -> bool:
    """Return whether persisted identifiers follow the caller token grammar."""
    return isinstance(value, str) and _IDENTIFIER_PATTERN.fullmatch(value) is not None


def _sqlite_ascii_prefix(value: str, prefix: str) -> bool:
    """Match SQLite schema-name prefixes using its ASCII case rules."""
    candidate = value[: len(prefix)]
    return len(value) >= len(prefix) and candidate.isascii() and candidate.lower() == prefix


def _normalize_host(value: object) -> str:
    """Normalize an exact DNS hostname and reject URLs, ports, paths, and wildcards."""
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 253:
        raise SourceAttemptInputError("host must be an exact DNS hostname")
    if any(token in value for token in ("://", "/", "?", "#", "@", "*", ":")):
        raise SourceAttemptInputError("host must be an exact DNS hostname without a port")
    try:
        host = value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise SourceAttemptInputError("host must be a valid DNS hostname") from exc
    if (
        not host
        or len(host) > 253
        or any(not _HOST_LABEL_PATTERN.fullmatch(label) for label in host.split("."))
    ):
        raise SourceAttemptInputError("host must be a valid exact DNS hostname")
    return host


def _configured_database_path(database: str, uri_query: Mapping[str, object]) -> Path:
    """Resolve the SQLite URL's main file without opening or creating it."""
    if database.startswith("file:"):
        uri_flags = tuple(value for key, value in uri_query.items() if key.lower() == "uri")
        flattened_flags = tuple(
            flag
            for value in uri_flags
            for flag in (value if isinstance(value, tuple) else (value,))
            if flag is not None
        )
        if not any(str(flag).lower() == "true" for flag in flattened_flags):
            raise ValueError("SQLite file URI requires uri=true")
        parsed = urlsplit(database)
        if (
            parsed.scheme != "file"
            or parsed.query
            or parsed.fragment
            or parsed.netloc not in {"", "localhost"}
        ):
            raise ValueError("SQLite file URI is not a local file")
        path_value = unquote(parsed.path)
    else:
        path_value = database
    if not path_value or path_value.strip().lower() in {"", ":memory:"}:
        raise ValueError("SQLite database path is empty or in-memory")
    return Path(path_value)


def _stat_database_file(path: Path) -> tuple[Path, stat_result]:
    """Return a canonical regular-file path and its filesystem identity."""
    resolved_path = path.resolve(strict=True)
    file_stat = resolved_path.stat()
    if not S_ISREG(file_stat.st_mode):
        raise ValueError("SQLite main database is not a regular file")
    return resolved_path, file_stat


def _bounded_int_or_none(value: object, minimum: int, maximum: int) -> int | None:
    """Validate exact integers, excluding bool and unbounded values."""
    if type(value) is not int or not minimum <= value <= maximum:
        return None
    return value


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    """Validate a persisted integer and fail closed on corrupted state."""
    result = _bounded_int_or_none(value, minimum, maximum)
    if result is None:
        raise SourceAttemptGateUnavailable("source gate persisted state is invalid")
    return result


def _valid_sha256(value: object) -> bool:
    """Require a lowercase, fixed-width digest for operator evidence references."""
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _valid_grant_record_times(grant: Mapping[str, object], effective_now: int) -> bool:
    """Validate grant lifecycle timestamps against the transaction high-water clock."""
    created_at = _bounded_int_or_none(grant["created_at_us"], 0, MAX_TIMESTAMP_US)
    updated_at = _bounded_int_or_none(grant["updated_at_us"], 0, MAX_TIMESTAMP_US)
    if created_at is None or updated_at is None or not created_at <= updated_at <= effective_now:
        return False
    revoked_value = grant["revoked_at_us"]
    actor = grant["revocation_actor"]
    reason = grant["revocation_reason_code"]
    if revoked_value is None:
        return actor is None and reason is None
    revoked_at = _bounded_int_or_none(revoked_value, created_at, updated_at)
    return (
        grant["decision"] == "DENIED"
        and revoked_at is not None
        and _valid_identifier(actor)
        and _valid_identifier(reason)
    )


def _valid_allowed_declaration(grant: Mapping[str, object], effective_now: int) -> bool:
    """Check required declaration fields without interpreting them as legal proof."""
    attested_at = _bounded_int_or_none(grant["attested_at_us"], 0, MAX_TIMESTAMP_US)
    return (
        isinstance(grant["rights_evidence_reference"], str)
        and bool(grant["rights_evidence_reference"].strip())
        and _valid_sha256(grant["rights_evidence_sha256"])
        and isinstance(grant["operator_attestation_note"], str)
        and bool(grant["operator_attestation_note"].strip())
        and _valid_identifier(grant["attested_by"])
        and attested_at is not None
        and attested_at <= effective_now
    )


def _grant_inspection_rejected(
    reason: SourceGrantInspectionReason,
) -> SourceGrantInspectionResult:
    """Build a fixed rejection without returning persisted grant contents."""
    return SourceGrantInspectionResult(SourceGrantInspectionStatus.REJECTED, reason)


def _grant_inspection_indeterminate(
    reason: SourceGrantInspectionReason,
) -> SourceGrantInspectionResult:
    """Build a fixed indeterminate result without exposing storage diagnostics."""
    return SourceGrantInspectionResult(SourceGrantInspectionStatus.INDETERMINATE, reason)


__all__ = [
    "AttemptLookupAbsent",
    "AttemptLookupDenied",
    "AttemptLookupIndeterminate",
    "AttemptLookupReasonCode",
    "AttemptLookupReserved",
    "AttemptReservation",
    "GateAttemptLookupResult",
    "GrantRevocationResult",
    "GrantRevocationStatus",
    "ReservationStatus",
    "SourceAttemptGate",
    "SourceAttemptGateConfigurationError",
    "SourceAttemptGateError",
    "SourceAttemptGateUnavailable",
    "SourceAttemptGateUnavailableError",
    "SourceAttemptInputError",
    "SourceAttemptScopeV2",
    "SourceGrantInspectionReason",
    "SourceGrantInspectionRequest",
    "SourceGrantInspectionResult",
    "SourceGrantInspectionStatus",
    "TransportAttempt",
]
