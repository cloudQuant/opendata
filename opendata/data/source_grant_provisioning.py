"""Issue narrow, time-bounded source grants from an explicit local declaration.

This module records a local operator's self-declaration. It does not verify a
supplier contract, independently establish legal rights, inspect evidence
files, access the network, load application settings, or read ``.env``. An
evidence reference and SHA-256 are operator-supplied metadata; the digest is
format-checked, not recomputed. The SQLite gate is trusted only as a local
single-machine control and does not provide strong operator identity or
tamper-proof audit storage against a privileged local user.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import unquote

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from opendata.data.source_attempt_gate import (
    MAX_TIMESTAMP_US,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    _normalize_host,
    _valid_sha256,
    _validate_identifier,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_ALLOWED_OPERATIONS = frozenset({"query", "store", "export", "subscribe"})
_SELF_DECLARATION_PREFIX = (
    "Local operator self-declaration; supplier rights were not independently verified. "
)
_SECRET_LIKE_PATTERN = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+|"
    r"\bbearer\s+\S+)"
)
_URI_USERINFO_PATTERN = re.compile(r"(?i)://[^/@\s]+@")
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]")
_UNCERTAIN_WRITE_MESSAGE = (
    "write outcome is uncertain; inspect the exact grant ID with gate.inspect_grant before retrying"
)
_PRECOMMIT_CLEANUP_MESSAGE = (
    "source grant was not committed; transaction cleanup failed; "
    "resolve gate cleanup before retrying"
)
_COMMITTED_CLEANUP_MESSAGE = (
    "source grant commit was acknowledged; connection cleanup failed; do not retry this grant"
)


class SourceGrantProvisioningError(RuntimeError):
    """Base error for a source-grant provisioning operation."""


class SourceGrantProvisioningUnavailableError(SourceGrantProvisioningError):
    """The existing gate, schema, clock, or pre-commit transaction was unavailable."""


class SourceGrantProvisioningConflictError(SourceGrantProvisioningError):
    """The requested grant ID already has a permanent gate record."""


class SourceGrantProvisioningCommitUncertainError(SourceGrantProvisioningError):
    """The commit acknowledgement was lost; inspect before retrying."""


class SourceGrantProvisioningCleanupError(SourceGrantProvisioningError):
    """A pre-commit transaction cleanup could not be confirmed."""


class SourceGrantProvisioningCommittedCleanupError(SourceGrantProvisioningError):
    """Commit succeeded, but connection cleanup failed; do not repeat the grant."""


class SourceGrantProvisioningStatus(str, Enum):
    """Fixed status for a committed local grant declaration."""

    PROVISIONED = "PROVISIONED"


@dataclass(frozen=True, slots=True, repr=False)
class SourceGrantProvisionRequest:
    """Bound request for one host and one exact source operation.

    ``operator_confirms_authorized_use`` is an explicit local self-declaration,
    not a supplier-rights verification or an external approval workflow.
    Conditions must be empty because the current gate does not resolve any
    non-empty condition semantics.
    """

    grant_id: str
    revision: int
    source: str
    canonical_model: str
    operation: str
    principal_scope: str
    host: str
    not_before: datetime
    expires_at: datetime
    rights_evidence_reference: str
    rights_evidence_sha256: str
    operator_confirms_authorized_use: bool
    operator_attestation_note: str
    attested_by: str
    conditions: tuple[str, ...] = ()
    _not_before_us: int = field(init=False, repr=False, compare=False)
    _expires_at_us: int = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject ambiguous types, unbounded values, and broad host scopes."""
        for field_name in (
            "grant_id",
            "source",
            "canonical_model",
            "principal_scope",
            "attested_by",
        ):
            value = getattr(self, field_name)
            if type(value) is not str:
                raise SourceAttemptInputError(f"{field_name} must be a string")
            _validate_identifier(value, field_name)
            _reject_credential_like_text(value, field_name)

        if type(self.revision) is not int or self.revision != 1:
            raise SourceAttemptInputError("a new source grant must start at revision 1")
        if type(self.operation) is not str or self.operation not in _ALLOWED_OPERATIONS:
            raise SourceAttemptInputError("operation is not supported")
        _reject_credential_like_text(self.operation, "operation")
        if type(self.host) is not str:
            raise SourceAttemptInputError("host must be a string")
        normalized_host = _normalize_host(self.host)
        _reject_credential_like_text(normalized_host, "host")
        object.__setattr__(self, "host", normalized_host)

        normalized_not_before, not_before_us = _normalize_aware_datetime(
            self.not_before, "not_before"
        )
        normalized_expires_at, expires_at_us = _normalize_aware_datetime(
            self.expires_at, "expires_at"
        )
        if expires_at_us <= not_before_us:
            raise SourceAttemptInputError("expires_at must be later than not_before")
        object.__setattr__(self, "not_before", normalized_not_before)
        object.__setattr__(self, "expires_at", normalized_expires_at)
        object.__setattr__(self, "_not_before_us", not_before_us)
        object.__setattr__(self, "_expires_at_us", expires_at_us)
        if type(self.operator_confirms_authorized_use) is not bool:
            raise SourceAttemptInputError("operator confirmation must be an explicit boolean")
        if not self.operator_confirms_authorized_use:
            raise SourceAttemptInputError("local operator must confirm the declared source use")
        if type(self.conditions) is not tuple or self.conditions:
            raise SourceAttemptInputError("conditions must be an empty tuple")

        _validate_evidence_text(self.rights_evidence_reference, "evidence reference", 512)
        if type(self.rights_evidence_sha256) is not str or not _valid_sha256(
            self.rights_evidence_sha256
        ):
            raise SourceAttemptInputError("evidence SHA-256 must be 64 lowercase hexadecimal chars")
        _validate_evidence_text(self.operator_attestation_note, "attestation note", 900)
        if len(_SELF_DECLARATION_PREFIX) + len(self.operator_attestation_note.strip()) > 1024:
            raise SourceAttemptInputError("attestation note is too long")

    def __repr__(self) -> str:
        """Hide operator text and evidence references from logs and tracebacks."""
        return "SourceGrantProvisionRequest(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class SourceGrantProvisionResult:
    """Minimal result for an atomically committed grant and exact host seal."""

    status: SourceGrantProvisioningStatus
    grant_id: str
    revision: int
    host: str
    not_before_us: int
    expires_at_us: int
    attested_by: str

    def __repr__(self) -> str:
        """Avoid echoing scope identifiers in default logs."""
        return (
            "SourceGrantProvisionResult(status="
            f"{self.status.value}, grant_id=<redacted>, revision={self.revision}, "
            "host=<redacted>, not_before_us=<redacted>, expires_at_us=<redacted>, "
            "attested_by=<redacted>)"
        )


class SourceGrantProvisioner:
    """Create grants using the existing file-identity, schema, and clock checks.

    A successful call inserts the grant, its single exact DNS host, and its
    sealed host-set row in one ``BEGIN IMMEDIATE`` transaction. The existing
    SQLite trigger writes the ``HOSTS_SEALED`` audit event in that transaction.
    Any pre-commit failure rolls back all rows and the high-water-clock update.
    A lost commit acknowledgement requires inspection of the exact request
    scope with ``gate.inspect_grant`` before retrying. A cleanup failure after
    an acknowledged commit must not be retried; a pre-commit cleanup failure
    requires resolving the gate connection state before any retry.
    """

    def __init__(self, gate: SourceAttemptGate) -> None:
        """Bind to an explicitly constructed source gate; never choose storage."""
        if type(gate) is not SourceAttemptGate:
            raise SourceAttemptInputError("gate must be an explicit SourceAttemptGate")
        self._gate = gate

    def provision(self, request: SourceGrantProvisionRequest) -> SourceGrantProvisionResult:
        """Persist one time-bounded self-declared grant without external access."""
        if type(request) is not SourceGrantProvisionRequest:
            raise SourceAttemptInputError("request must be a SourceGrantProvisionRequest")

        try:
            connection = self._gate._open_write_connection()
        except SourceAttemptGateUnavailable:
            raise SourceGrantProvisioningUnavailableError(
                "source gate transaction is unavailable"
            ) from None
        commit_started = False
        commit_acknowledged = False
        try:
            self._require_valid_gate_schema(connection)
            effective_now = self._gate._advance_clock(connection)
            not_before_us = request._not_before_us
            expires_at_us = request._expires_at_us
            if expires_at_us <= effective_now:
                raise SourceAttemptInputError("expires_at must be later than the source gate clock")

            existing = connection.execute(
                text("SELECT 1 FROM od_source_grants WHERE grant_id = :grant_id"),
                {"grant_id": request.grant_id},
            ).first()
            if existing is not None:
                raise SourceGrantProvisioningConflictError("grant ID already exists")

            attestation_note = _SELF_DECLARATION_PREFIX + request.operator_attestation_note.strip()
            connection.execute(
                text(
                    "INSERT INTO od_source_grants "
                    "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                    "revision, not_before_us, expires_at_us, conditions_json, "
                    "rights_evidence_reference, rights_evidence_sha256, "
                    "operator_attestation_note, attested_by, attested_at_us, revoked_at_us, "
                    "revocation_actor, revocation_reason_code, created_at_us, updated_at_us) "
                    "VALUES (:grant_id, :source, :canonical_model, :operation, :principal_scope, "
                    "'ALLOWED', :revision, :not_before_us, :expires_at_us, '[]', "
                    ":evidence_reference, :evidence_sha256, :attestation_note, :attested_by, "
                    ":attested_at_us, NULL, NULL, NULL, :created_at_us, :updated_at_us)"
                ),
                {
                    "grant_id": request.grant_id,
                    "source": request.source,
                    "canonical_model": request.canonical_model,
                    "operation": request.operation,
                    "principal_scope": request.principal_scope,
                    "revision": request.revision,
                    "not_before_us": not_before_us,
                    "expires_at_us": expires_at_us,
                    "evidence_reference": request.rights_evidence_reference,
                    "evidence_sha256": request.rights_evidence_sha256,
                    "attestation_note": attestation_note,
                    "attested_by": request.attested_by,
                    "attested_at_us": effective_now,
                    "created_at_us": effective_now,
                    "updated_at_us": effective_now,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"
                ),
                {"grant_id": request.grant_id, "host": request.host},
            )
            connection.execute(
                text(
                    "INSERT INTO od_source_grant_host_sets "
                    "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                    "VALUES (:grant_id, :revision, 1, :sealed_at_us, :sealed_by, "
                    "'operator-self-declaration')"
                ),
                {
                    "grant_id": request.grant_id,
                    "revision": request.revision,
                    "sealed_at_us": effective_now,
                    "sealed_by": request.attested_by,
                },
            )
            self._verify_inserted_rows(connection, request, effective_now, not_before_us)
            commit_started = True
            try:
                self._gate._commit(connection)
            except Exception:
                raise SourceGrantProvisioningCommitUncertainError(
                    _UNCERTAIN_WRITE_MESSAGE
                ) from None
            commit_acknowledged = True
            return SourceGrantProvisionResult(
                status=SourceGrantProvisioningStatus.PROVISIONED,
                grant_id=request.grant_id,
                revision=request.revision,
                host=request.host,
                not_before_us=not_before_us,
                expires_at_us=expires_at_us,
                attested_by=request.attested_by,
            )
        except (SourceAttemptInputError, SourceGrantProvisioningError):
            raise
        except (SQLAlchemyError, SourceAttemptGateUnavailable):
            raise SourceGrantProvisioningUnavailableError(
                "source grant could not be safely committed"
            ) from None
        finally:
            try:
                connection.close()
            except Exception:
                if commit_started and not commit_acknowledged:
                    raise SourceGrantProvisioningCommitUncertainError(
                        _UNCERTAIN_WRITE_MESSAGE
                    ) from None
                if commit_acknowledged:
                    raise SourceGrantProvisioningCommittedCleanupError(
                        _COMMITTED_CLEANUP_MESSAGE
                    ) from None
                raise SourceGrantProvisioningCleanupError(_PRECOMMIT_CLEANUP_MESSAGE) from None

    def _require_valid_gate_schema(self, connection: Connection) -> None:
        """Recheck the exact file's full pinned SQLite gate schema under its writer lock."""
        try:
            raw_connection = connection.connection.driver_connection
        except Exception:
            raise SourceGrantProvisioningUnavailableError(
                "source gate schema is unavailable"
            ) from None
        if type(raw_connection) is not sqlite3.Connection:
            raise SourceGrantProvisioningUnavailableError("source gate schema is unavailable")
        try:
            original_row_factory = raw_connection.row_factory
            try:
                raw_connection.row_factory = sqlite3.Row
                schema_valid = self._gate._source_gate_snapshot_schema_valid(raw_connection)
            finally:
                raw_connection.row_factory = original_row_factory
            if schema_valid is not True:
                raise SourceGrantProvisioningUnavailableError("source gate schema is unavailable")
            self._gate._require_v2_schema(connection)
        except SourceGrantProvisioningUnavailableError:
            raise
        except Exception:
            raise SourceGrantProvisioningUnavailableError(
                "source gate schema is unavailable"
            ) from None

    @staticmethod
    def _verify_inserted_rows(
        connection: Connection,
        request: SourceGrantProvisionRequest,
        effective_now: int,
        not_before_us: int,
    ) -> None:
        """Confirm the expected one-host seal and trigger event before commit."""
        rows = (
            connection.execute(
                text(
                    "SELECT g.grant_id, g.source, g.canonical_model, g.operation, "
                    "g.principal_scope, g.decision, g.revision, g.not_before_us, "
                    "g.expires_at_us, g.conditions_json, g.rights_evidence_reference, "
                    "g.rights_evidence_sha256, g.operator_attestation_note, g.attested_by, "
                    "g.attested_at_us, g.revoked_at_us, g.revocation_actor, "
                    "g.revocation_reason_code, g.created_at_us, g.updated_at_us, "
                    "h.host, s.revision AS sealed_revision, s.host_count, "
                    "s.sealed_at_us, s.sealed_by, "
                    "s.reason_code, e.event_type, e.old_revision, e.new_revision, e.actor, "
                    "e.reason_code AS event_reason, e.occurred_at_us "
                    "FROM od_source_grants AS g "
                    "JOIN od_source_grant_hosts AS h ON h.grant_id = g.grant_id "
                    "JOIN od_source_grant_host_sets AS s ON s.grant_id = g.grant_id "
                    "JOIN od_source_grant_events AS e ON e.grant_id = g.grant_id "
                    "WHERE g.grant_id = :grant_id ORDER BY e.event_id"
                ),
                {"grant_id": request.grant_id},
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            raise SourceGrantProvisioningUnavailableError("source grant seal verification failed")
        row = rows[0]
        expected_attestation_note = (
            _SELF_DECLARATION_PREFIX + request.operator_attestation_note.strip()
        )
        if (
            row["grant_id"] != request.grant_id
            or row["source"] != request.source
            or row["canonical_model"] != request.canonical_model
            or row["operation"] != request.operation
            or row["principal_scope"] != request.principal_scope
            or row["decision"] != "ALLOWED"
            or row["revision"] != request.revision
            or row["not_before_us"] != not_before_us
            or row["expires_at_us"] != request._expires_at_us
            or row["conditions_json"] != "[]"
            or row["rights_evidence_reference"] != request.rights_evidence_reference
            or row["rights_evidence_sha256"] != request.rights_evidence_sha256
            or row["operator_attestation_note"] != expected_attestation_note
            or row["attested_by"] != request.attested_by
            or row["attested_at_us"] != effective_now
            or row["revoked_at_us"] is not None
            or row["revocation_actor"] is not None
            or row["revocation_reason_code"] is not None
            or row["created_at_us"] != effective_now
            or row["updated_at_us"] != effective_now
            or row["host"] != request.host
            or row["sealed_revision"] != request.revision
            or row["host_count"] != 1
            or row["sealed_at_us"] != effective_now
            or row["sealed_by"] != request.attested_by
            or row["reason_code"] != "operator-self-declaration"
            or row["event_type"] != "HOSTS_SEALED"
            or row["old_revision"] != request.revision
            or row["new_revision"] != request.revision
            or row["actor"] != request.attested_by
            or row["event_reason"] != "operator-self-declaration"
            or row["occurred_at_us"] != effective_now
        ):
            raise SourceGrantProvisioningUnavailableError("source grant seal verification failed")


def _validate_evidence_text(value: object, name: str, maximum: int) -> None:
    """Validate non-secret operator metadata without opening paths or URLs."""
    if type(value) is not str:
        raise SourceAttemptInputError(f"{name} must be text")
    decoded = unquote(value)
    if (
        not value
        or value != value.strip()
        or len(value) > maximum
        or _CONTROL_PATTERN.search(value) is not None
    ):
        raise SourceAttemptInputError(f"{name} is invalid or contains credential-like material")
    _reject_credential_like_text(decoded, name)


def _reject_credential_like_text(value: str, name: str) -> None:
    """Reject credential-shaped text without echoing the supplied value."""
    if (
        _SECRET_LIKE_PATTERN.search(value) is not None
        or _URI_USERINFO_PATTERN.search(value) is not None
        or ".env" in value.lower()
    ):
        raise SourceAttemptInputError(f"{name} must not contain credential-like material")


def _normalize_aware_datetime(value: object, name: str) -> tuple[datetime, int]:
    """Snapshot one timezone offset and return immutable UTC value and microseconds."""
    if type(value) is not datetime:
        raise SourceAttemptInputError(f"{name} must be a timezone-aware datetime")
    try:
        if value.tzinfo is None:
            raise SourceAttemptInputError(f"{name} must be a timezone-aware datetime")
        offset = value.utcoffset()
        if offset is None:
            raise SourceAttemptInputError(f"{name} must be a timezone-aware datetime")
        utc_naive = value.replace(tzinfo=None) - offset
    except SourceAttemptInputError:
        raise
    except Exception:
        raise SourceAttemptInputError(f"{name} is outside the supported time range") from None
    utc_value = utc_naive.replace(tzinfo=timezone.utc)
    delta = utc_value - _EPOCH_UTC
    epoch_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if not 0 <= epoch_us <= MAX_TIMESTAMP_US:
        raise SourceAttemptInputError(f"{name} is outside the supported time range")
    return utc_value, epoch_us


__all__ = [
    "SourceGrantProvisioningCleanupError",
    "SourceGrantProvisioningConflictError",
    "SourceGrantProvisioningCommitUncertainError",
    "SourceGrantProvisioningCommittedCleanupError",
    "SourceGrantProvisionRequest",
    "SourceGrantProvisionResult",
    "SourceGrantProvisioner",
    "SourceGrantProvisioningError",
    "SourceGrantProvisioningStatus",
    "SourceGrantProvisioningUnavailableError",
]
