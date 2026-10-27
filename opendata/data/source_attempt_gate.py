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

import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING
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

MICROTOKENS_PER_TOKEN = 1_000_000
MICROSECONDS_PER_SECOND = 1_000_000
MAX_RATE_MICROTOKENS_PER_SECOND = 1_000_000_000_000
MAX_CAPACITY_MICROTOKENS = 1_000_000_000_000
MAX_ATTEMPT_NO = 1_000_000
MAX_REVISION = 2_147_483_647
MAX_TIMESTAMP_US = 253_402_300_799_999_999
MAX_RETRY_AFTER_US = 1_000_000_000_000_000_000
_MAX_TEXT_LENGTH = 128
_IDENTIFIER_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_HOST_LABEL_PATTERN = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")


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


@dataclass(frozen=True, slots=True)
class TransportAttempt:
    """Bound identity for a single physical HTTP transport attempt.

    The caller must create a new ``attempt_id`` and increment ``attempt_no``
    for every retry. ``host`` is an exact DNS hostname, without scheme/port.
    ``run_key`` participates in idempotency binding only; this gate does not
    count per-run, daily, or task-level attempts.
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
        connection = self._open_write_connection()
        try:
            effective_now = self._advance_clock(connection)
            duplicate = self._find_existing_attempt(connection, attempt)
            if duplicate is not None:
                self._commit(connection)
                return duplicate

            decision, bucket_update, policy_snapshot = self._evaluate_attempt(
                connection, attempt, effective_now
            )
            if bucket_update is not None:
                self._persist_bucket(connection, bucket_update[0], bucket_update[1])
            self._insert_attempt(connection, attempt, decision, effective_now, policy_snapshot)
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
        """Resolve both unique keys without exposing an old reservation as a permit."""
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

    def _same_attempt_scope(self, row: Mapping[str, object], attempt: TransportAttempt) -> bool:
        """Compare every immutable caller-supplied scope field in an old row."""
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
        connection: Connection,
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
        self, connection: Connection, statement: str, parameters: dict[str, object]
    ) -> dict[str, object] | None:
        """Execute a parameterized query and return its first mapping row."""
        result = connection.execute(text(statement), parameters).mappings().first()
        return None if result is None else dict(result)

    def _fetch_all(
        self, connection: Connection, statement: str, parameters: dict[str, object]
    ) -> list[dict[str, object]]:
        """Execute a parameterized query and return mapping rows."""
        result = connection.execute(text(statement), parameters).mappings().all()
        return [dict(row) for row in result]

    def _commit(self, connection: Connection) -> None:
        """Commit or fail closed when the database acknowledgement is uncertain."""
        try:
            connection.commit()
        except Exception:
            raise SourceAttemptGateUnavailable("source attempt gate commit is uncertain") from None


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


__all__ = [
    "AttemptReservation",
    "GrantRevocationResult",
    "GrantRevocationStatus",
    "ReservationStatus",
    "SourceAttemptGate",
    "SourceAttemptGateConfigurationError",
    "SourceAttemptGateError",
    "SourceAttemptGateUnavailable",
    "SourceAttemptGateUnavailableError",
    "SourceAttemptInputError",
    "TransportAttempt",
]
