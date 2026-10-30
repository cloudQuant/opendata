"""Provision a fixed THS token bucket for a local CSI500 sample configuration.

This module configures an already migrated, dedicated source-gate SQLite file.
It never creates or changes a source grant, sends an HTTP request, or enables a
worker. ``actor`` and ``evidence_reference`` are local operator declarations;
they do not prove supplier rights or operator identity. The current source-gate
schema has no append-only quota-policy event table, so the receipt records only
the initial local configuration fact. The binding is global to ``ths`` within
this database and does not restrict a request to CSI500 or a market.

This module only seeds the source-level binding and refillable token bucket.
The V4 gate's persistent one-shot lock is a separate operation and is not
created by this receipt. Until that lock is configured and the runtime path is
reviewed to use it, another grant can extend access and the bucket refills over
time. The receipt is not an execution permit, data-rights proof, CSI500 request
constraint, or legacy-worker isolation proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote, unquote

from sqlalchemy import URL, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from alembic_source_gate.path_contract import (
    DatabaseFileIdentity,
    assert_database_file_identity,
    inspect_existing_database_path,
)
from opendata.data.source_attempt_gate import (
    MAX_TIMESTAMP_US,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceGrantInspectionRequest,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

_SOURCE = "ths"
_CANONICAL_MODEL = "index_daily"
_OPERATION = "query"
_HOST = "fuyao.aicubes.cn"
_QUOTA_KEY = "csi500-fuyao-sample-204"
_POLICY_REVISION = 1
_RATE_MICROTOKENS_PER_SECOND = 1
_CAPACITY_MICROTOKENS = 1_000_000
_MAX_DECLARED_GRANT_LIFETIME_SECONDS = 999_999
_MICROSECONDS_PER_SECOND = 1_000_000
_IDENTIFIER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"\A[0-9a-f]{64}\Z")
_SECRET_LIKE_RE = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+|"
    r"\bbearer\s+\S+)"
)
_URI_USERINFO_RE = re.compile(r"(?i)://[^/@\s]+@")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=timezone.utc)
_FRESH_TABLE_QUERIES = {
    "od_source_gate_policies": text("SELECT COUNT(*) FROM od_source_gate_policies"),
    "od_source_gate_source_bindings": text("SELECT COUNT(*) FROM od_source_gate_source_bindings"),
    "od_source_gate_state": text("SELECT COUNT(*) FROM od_source_gate_state"),
    "od_source_attempt_reservations": text("SELECT COUNT(*) FROM od_source_attempt_reservations"),
    "od_source_attempt_scope_v2": text("SELECT COUNT(*) FROM od_source_attempt_scope_v2"),
    "od_source_attempt_scope_v3": text("SELECT COUNT(*) FROM od_source_attempt_scope_v3"),
    "od_source_one_shot_configs": text("SELECT COUNT(*) FROM od_source_one_shot_configs"),
    "od_source_one_shot_permits": text("SELECT COUNT(*) FROM od_source_one_shot_permits"),
}


class Csi500SampleQuotaError(RuntimeError):
    """Base error for a fail-closed sample token-bucket configuration operation."""


class Csi500SampleQuotaInputError(ValueError):
    """A caller supplied an invalid or unsupported provisioning value."""


class Csi500SampleQuotaUnavailableError(Csi500SampleQuotaError):
    """The target identity, schema, clock, or pre-commit transaction is unavailable."""


class Csi500SampleQuotaConflictError(Csi500SampleQuotaError):
    """The gate is not fresh or already contains quota configuration or attempts."""


class Csi500SampleQuotaGrantError(Csi500SampleQuotaError):
    """The exact active operator-declared grant is absent or does not match."""


class Csi500SampleQuotaCommitUncertainError(Csi500SampleQuotaError):
    """Commit or post-commit identity confirmation was not acknowledged; never retry."""


class Csi500SampleQuotaCleanupError(Csi500SampleQuotaError):
    """A pre-commit rollback or connection cleanup could not be confirmed."""


class Csi500SampleQuotaCommittedCleanupError(Csi500SampleQuotaError):
    """The commit was acknowledged but connection cleanup failed; do not retry."""


class Csi500SampleQuotaStatus(str, Enum):
    """Fixed status for an acknowledged local quota configuration."""

    PROVISIONED = "PROVISIONED"


@dataclass(frozen=True, slots=True, repr=False)
class Csi500SampleQuotaReceipt:
    """Receipt for local bucket rows, not an execution or one-attempt guarantee."""

    status: Csi500SampleQuotaStatus
    grant_id: str
    source: Literal["ths"]
    quota_key: str
    policy_revision: int
    binding_sha256: str
    policy_sha256: str
    rate_microtokens_per_second: int
    capacity_microtokens: int
    target_identity_sha256: str

    def __repr__(self) -> str:
        """Keep identifiers and file identity out of default logs."""
        return (
            "Csi500SampleQuotaReceipt(status=PROVISIONED, grant_id=<redacted>, "
            "source=ths, quota_key=<redacted>, policy_revision=1, "
            "binding_sha256=<redacted>, policy_sha256=<redacted>, "
            "rate_microtokens_per_second=1, capacity_microtokens=1000000, "
            "target_identity_sha256=<redacted>)"
        )


def provision_csi500_sample_quota(
    *,
    database_path: str | Path,
    expected_identity: DatabaseFileIdentity,
    grant_id: str,
    principal_scope: str,
    actor: str,
    evidence_reference: str,
    expires_at: datetime,
) -> Csi500SampleQuotaReceipt:
    """Atomically configure a fixed THS token bucket in a fresh V4 sample gate.

    The database must be an existing regular SQLite file at the supplied
    device/inode identity, have the exact complete source-gate V4 schema and
    marker, and contain exactly one active ``ths/index_daily/query`` grant
    matching the declared principal, actor, evidence reference, and expiry.
    The V2 and V3 attempt sidecars, reservations, policies, bindings, bucket
    state, one-shot configuration, and one-shot permit must all be empty. The
    call inserts the fixed policy, ``ths`` binding, and full initial bucket in
    one SQLite ``BEGIN IMMEDIATE`` transaction.

    The bucket starts with one token. At one microtoken per second, a full
    token refills in 1,000,000 seconds (about 11 days 13 hours 46 minutes).
    This function requires the declared grant's remaining lifetime to be
    shorter than that interval. It does not create the separate V4 one-shot
    scope lock, bind this bucket to a capture intent, or independently enable
    real collection. Callers must configure the V4 lock through the gate's
    explicit operator setup API; the runtime must then enforce that exact V3
    scope. The host set must be exactly ``fuyao.aicubes.cn``; it still does not
    bind a request to CSI500 or a particular market.

    Args:
        database_path: Absolute path to the already existing SQLite gate file.
        expected_identity: Device and inode captured for that exact file.
        grant_id: ID of the one previously provisioned source grant.
        principal_scope: Exact principal stored on that grant.
        actor: Local actor label; it must equal the grant's declared attestor.
        evidence_reference: Provenance reference; it must match the grant. The
            function does not read the referenced material or verify rights.
        expires_at: Exact expiry on the existing grant, with an explicit zone.

    Returns:
        A receipt containing the initial committed local configuration facts
        and V4-compatible binding/policy pins. Those pins are not the V4
        one-shot lock receipt or an execution permit.

    Raises:
        Csi500SampleQuotaInputError: If arguments are malformed or unbounded.
        Csi500SampleQuotaConflictError: If the gate is not fresh.
        Csi500SampleQuotaGrantError: If the exact active grant does not match.
        Csi500SampleQuotaCommitUncertainError: If commit status is uncertain;
            never retry this gate blindly.
        Csi500SampleQuotaUnavailableError: If schema, clock, or storage is
            unavailable before commit.
    """
    path, identity, grant_expiry_us = _validate_inputs(
        database_path=database_path,
        expected_identity=expected_identity,
        grant_id=grant_id,
        principal_scope=principal_scope,
        actor=actor,
        evidence_reference=evidence_reference,
        expires_at=expires_at,
    )
    target_identity_sha256 = _identity_sha256(identity)
    engine = _open_explicit_engine(path)
    gate: SourceAttemptGate | None = None
    connection: Connection | None = None
    commit_started = False
    commit_acknowledged = False
    commit_uncertain = False
    try:
        assert_database_file_identity(path, identity)
        try:
            gate = SourceAttemptGate(engine)
            connection = gate._open_write_connection()
        except Exception:
            raise Csi500SampleQuotaUnavailableError(
                "source gate transaction is unavailable"
            ) from None

        try:
            assert_database_file_identity(path, identity)
            _require_valid_schema(gate, connection)
            effective_now = gate._advance_clock(connection)
            if grant_expiry_us <= effective_now:
                raise Csi500SampleQuotaGrantError("source grant is expired or not yet usable")
            if grant_expiry_us - effective_now >= (
                _MAX_DECLARED_GRANT_LIFETIME_SECONDS * _MICROSECONDS_PER_SECOND
            ):
                raise Csi500SampleQuotaInputError(
                    "source grant expiry exceeds the configured refill-window bound"
                )
            _require_fresh_gate(connection)
            _require_exact_grant(
                gate,
                connection,
                effective_now=effective_now,
                grant_id=grant_id,
                principal_scope=principal_scope,
                actor=actor,
                evidence_reference=evidence_reference,
                grant_expiry_us=grant_expiry_us,
            )

            policy_reference = evidence_reference
            binding_reference = evidence_reference
            policy_payload = {
                "actor": actor,
                "capacity_microtokens": _CAPACITY_MICROTOKENS,
                "configured_at_us": effective_now,
                "evidence_reference": evidence_reference,
                "expires_at_us": grant_expiry_us,
                "grant_id": grant_id,
                "host": _HOST,
                "policy_revision": _POLICY_REVISION,
                "principal_scope": principal_scope,
                "quota_key": _QUOTA_KEY,
                "rate_microtokens_per_second": _RATE_MICROTOKENS_PER_SECOND,
                "schema": "csi500-ths-sample-bucket-v1",
                "source": _SOURCE,
                "target_identity_sha256": target_identity_sha256,
            }
            policy_sha256 = _canonical_sha256(policy_payload)
            binding_sha256 = _canonical_sha256(
                {
                    "actor": actor,
                    "binding_reference": binding_reference,
                    "configured_at_us": effective_now,
                    "evidence_reference": evidence_reference,
                    "expires_at_us": grant_expiry_us,
                    "grant_id": grant_id,
                    "host": _HOST,
                    "model": _CANONICAL_MODEL,
                    "operation": _OPERATION,
                    "principal_scope": principal_scope,
                    "quota_key": _QUOTA_KEY,
                    "schema": "csi500-ths-sample-bucket-binding-v1",
                    "source": _SOURCE,
                    "target_identity_sha256": target_identity_sha256,
                }
            )
            _insert_quota_rows(
                connection,
                actor=actor,
                configured_at_us=effective_now,
                policy_reference=policy_reference,
                policy_sha256=policy_sha256,
                binding_reference=binding_reference,
                binding_sha256=binding_sha256,
            )
            _verify_quota_rows(
                connection,
                actor=actor,
                configured_at_us=effective_now,
                policy_reference=policy_reference,
                policy_sha256=policy_sha256,
                binding_reference=binding_reference,
                binding_sha256=binding_sha256,
            )
            assert_database_file_identity(path, identity)
            commit_started = True
            try:
                gate._commit(connection)
            except BaseException:
                raise Csi500SampleQuotaCommitUncertainError(
                    "quota commit acknowledgement is uncertain; never retry this gate"
                ) from None
            commit_acknowledged = True
            try:
                assert_database_file_identity(path, identity)
            except Exception:
                raise Csi500SampleQuotaCommitUncertainError(
                    "quota commit completed but target identity changed; never retry this gate"
                ) from None
            return Csi500SampleQuotaReceipt(
                status=Csi500SampleQuotaStatus.PROVISIONED,
                grant_id=grant_id,
                source=_SOURCE,
                quota_key=_QUOTA_KEY,
                policy_revision=_POLICY_REVISION,
                binding_sha256=binding_sha256,
                policy_sha256=policy_sha256,
                rate_microtokens_per_second=_RATE_MICROTOKENS_PER_SECOND,
                capacity_microtokens=_CAPACITY_MICROTOKENS,
                target_identity_sha256=target_identity_sha256,
            )
        except BaseException as exc:
            if isinstance(exc, Csi500SampleQuotaCommitUncertainError):
                commit_uncertain = True
            if not commit_started:
                try:
                    connection.rollback()
                except Exception:
                    raise Csi500SampleQuotaCleanupError(
                        "pre-commit rollback could not be confirmed; resolve gate state"
                    ) from None
            if isinstance(exc, (Csi500SampleQuotaError, Csi500SampleQuotaInputError)):
                raise
            if commit_started:
                commit_uncertain = True
                raise Csi500SampleQuotaCommitUncertainError(
                    "quota commit acknowledgement is uncertain; never retry this gate"
                ) from None
            if isinstance(exc, (SQLAlchemyError, SourceAttemptGateUnavailable)):
                raise Csi500SampleQuotaUnavailableError(
                    "quota configuration could not be safely committed"
                ) from None
            raise Csi500SampleQuotaUnavailableError(
                "quota configuration could not be safely committed"
            ) from None
    finally:
        connection_cleanup_failed = False
        if connection is not None:
            try:
                connection.close()
            except Exception:
                connection_cleanup_failed = True
        engine_cleanup_failed = False
        try:
            engine.dispose()
        except Exception:
            engine_cleanup_failed = True

        if connection_cleanup_failed or engine_cleanup_failed:
            if connection_cleanup_failed and engine_cleanup_failed:
                cleanup_description = "connection and engine cleanup failed"
            elif connection_cleanup_failed:
                cleanup_description = "connection cleanup failed"
            else:
                cleanup_description = "engine cleanup failed"
            if commit_uncertain or (commit_started and not commit_acknowledged):
                raise Csi500SampleQuotaCommitUncertainError(
                    "quota commit status is uncertain and "
                    f"{cleanup_description}; never retry this gate"
                ) from None
            if commit_acknowledged:
                raise Csi500SampleQuotaCommittedCleanupError(
                    f"quota commit was acknowledged but {cleanup_description}; do not retry"
                ) from None
            raise Csi500SampleQuotaCleanupError(
                f"gate {cleanup_description}; resolve gate state before retrying"
            ) from None


def _validate_inputs(
    *,
    database_path: str | Path,
    expected_identity: DatabaseFileIdentity,
    grant_id: str,
    principal_scope: str,
    actor: str,
    evidence_reference: str,
    expires_at: datetime,
) -> tuple[Path, DatabaseFileIdentity, int]:
    """Validate bounded caller data before opening any storage handle."""
    for name, value in (
        ("grant_id", grant_id),
        ("principal_scope", principal_scope),
        ("actor", actor),
    ):
        if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
            raise Csi500SampleQuotaInputError(f"{name} is invalid")
        _reject_secret_like_text(value, name)
    if type(expected_identity) is not DatabaseFileIdentity:
        raise Csi500SampleQuotaInputError("expected_identity must be a DatabaseFileIdentity")
    if (
        type(expected_identity.device) is not int
        or expected_identity.device < 0
        or type(expected_identity.inode) is not int
        or expected_identity.inode <= 0
    ):
        raise Csi500SampleQuotaInputError("expected_identity is invalid")
    if type(evidence_reference) is not str:
        raise Csi500SampleQuotaInputError("evidence_reference must be text")
    decoded_reference = unquote(evidence_reference)
    if (
        not evidence_reference
        or evidence_reference != evidence_reference.strip()
        or len(evidence_reference) > 512
        or _CONTROL_RE.search(evidence_reference) is not None
    ):
        raise Csi500SampleQuotaInputError("evidence_reference is invalid")
    _reject_secret_like_text(decoded_reference, "evidence_reference")
    if type(expires_at) is not datetime:
        raise Csi500SampleQuotaInputError("expires_at must be a timezone-aware datetime")
    try:
        offset = expires_at.utcoffset()
        if expires_at.tzinfo is None or offset is None:
            raise Csi500SampleQuotaInputError("expires_at must be a timezone-aware datetime")
        utc_naive = expires_at.replace(tzinfo=None) - offset
    except Csi500SampleQuotaInputError:
        raise
    except Exception:
        raise Csi500SampleQuotaInputError("expires_at is outside the supported range") from None
    utc_value = utc_naive.replace(tzinfo=timezone.utc)
    delta = utc_value - _EPOCH_UTC
    expiry_us = (
        delta.days * 86_400_000_000 + delta.seconds * _MICROSECONDS_PER_SECOND + delta.microseconds
    )
    if not 0 < expiry_us <= MAX_TIMESTAMP_US:
        raise Csi500SampleQuotaInputError("expires_at is outside the supported range")
    try:
        path, actual_identity = inspect_existing_database_path(database_path)
    except Exception:
        raise Csi500SampleQuotaInputError(
            "database_path must name an existing canonical regular file"
        ) from None
    if actual_identity != expected_identity:
        raise Csi500SampleQuotaUnavailableError("source gate target identity does not match")
    return path, actual_identity, expiry_us


def _open_explicit_engine(path: Path) -> Engine:
    """Open only the existing explicit SQLite file; ``mode=rw`` forbids creation."""
    encoded_path = quote(path.as_posix(), safe="/")
    url = URL.create(
        "sqlite+pysqlite",
        database=f"file:{encoded_path}",
        query={"mode": "rw", "uri": "true"},
    )
    try:
        return create_engine(
            url,
            connect_args={"timeout": 5.0},
            poolclass=NullPool,
        )
    except Exception:
        raise Csi500SampleQuotaUnavailableError("source gate engine is unavailable") from None


def _require_valid_schema(gate: SourceAttemptGate, connection: Connection) -> None:
    """Require the exact complete V1/V2/V3/V4 schema and current V4 marker."""
    try:
        raw_connection = connection.connection.driver_connection
        if type(raw_connection) is not sqlite3.Connection:
            raise Csi500SampleQuotaUnavailableError("source gate schema is unavailable")
        previous_row_factory = raw_connection.row_factory
        try:
            raw_connection.row_factory = sqlite3.Row
            schema_valid = gate._source_gate_snapshot_schema_valid(raw_connection)
        finally:
            raw_connection.row_factory = previous_row_factory
        if schema_valid is not True:
            raise Csi500SampleQuotaUnavailableError("source gate schema is unavailable")
        gate._require_v4_schema(connection)
    except Csi500SampleQuotaError:
        raise
    except Exception:
        raise Csi500SampleQuotaUnavailableError("source gate schema is unavailable") from None


def _require_fresh_gate(connection: Connection) -> None:
    """Reject mixed/old gate state and every pre-existing quota configuration."""
    try:
        counts = {
            table_name: connection.execute(count_query).scalar_one()
            for table_name, count_query in _FRESH_TABLE_QUERIES.items()
        }
        grant_count = connection.execute(text("SELECT COUNT(*) FROM od_source_grants")).scalar_one()
        if type(grant_count) is not int or grant_count != 1:
            raise Csi500SampleQuotaConflictError(
                "sample gate must contain exactly its one pre-provisioned grant"
            )
        if any(type(count) is not int or count != 0 for count in counts.values()):
            raise Csi500SampleQuotaConflictError(
                "sample gate contains old attempts or quota configuration"
            )
    except Csi500SampleQuotaError:
        raise
    except Exception:
        raise Csi500SampleQuotaUnavailableError("source gate state is unavailable") from None


def _require_exact_grant(
    gate: SourceAttemptGate,
    connection: Connection,
    *,
    effective_now: int,
    grant_id: str,
    principal_scope: str,
    actor: str,
    evidence_reference: str,
    grant_expiry_us: int,
) -> None:
    """Require the single active grant, exact Fuyao host, and matching seal event."""
    try:
        rows = (
            connection.execute(
                text("SELECT * FROM od_source_grants WHERE grant_id = :grant_id"),
                {"grant_id": grant_id},
            )
            .mappings()
            .all()
        )
        if len(rows) != 1:
            raise Csi500SampleQuotaGrantError("exact source grant is unavailable")
        grant = rows[0]
        attempt = TransportAttempt(
            attempt_id="quota-provision",
            request_id="quota-provision",
            attempt_no=1,
            grant_id=grant_id,
            expected_revision=1,
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=_OPERATION,
            principal_scope=principal_scope,
            host=_HOST,
            expected_rights_evidence_reference=evidence_reference,
        )
        denial = gate._grant_denial(connection, grant, attempt, effective_now)
        if (
            denial is not None
            or grant.get("revision") != 1
            or grant.get("attested_by") != actor
            or grant.get("expires_at_us") != grant_expiry_us
        ):
            raise Csi500SampleQuotaGrantError(
                "source grant does not match the exact active sample declaration"
            )
        host_rows = (
            connection.execute(
                text(
                    "SELECT host FROM od_source_grant_hosts "
                    "WHERE grant_id = :grant_id ORDER BY host"
                ),
                {"grant_id": grant_id},
            )
            .mappings()
            .all()
        )
        _require_exact_sample_host_set(host_rows)
        seal_request = SourceGrantInspectionRequest(
            grant_id=grant_id,
            revision=1,
            source=_SOURCE,
            canonical_model=_CANONICAL_MODEL,
            operation=_OPERATION,
            principal_scope=principal_scope,
            host=_HOST,
        )
        raw_connection = connection.connection.driver_connection
        if type(raw_connection) is not sqlite3.Connection:
            raise Csi500SampleQuotaUnavailableError("source grant seal event is unavailable")
        previous_row_factory = raw_connection.row_factory
        try:
            raw_connection.row_factory = sqlite3.Row
            seal_matches = gate._grant_seal_event_matches(raw_connection, seal_request)
        finally:
            raw_connection.row_factory = previous_row_factory
        if not seal_matches:
            raise Csi500SampleQuotaGrantError("source grant seal event is unavailable")
    except Csi500SampleQuotaError:
        raise
    except (SourceAttemptInputError, SQLAlchemyError):
        raise Csi500SampleQuotaGrantError("exact source grant is unavailable") from None
    except Exception:
        raise Csi500SampleQuotaUnavailableError("source grant state is unavailable") from None


def _require_exact_sample_host_set(host_rows: Sequence[Mapping[str, object]]) -> None:
    """Reject a sealed sample grant unless Fuyao is its sole exact DNS host."""
    if len(host_rows) != 1 or host_rows[0].get("host") != _HOST:
        raise Csi500SampleQuotaGrantError(
            "sample grant must be sealed to the exact Fuyao host only"
        )


def _insert_quota_rows(
    connection: Connection,
    *,
    actor: str,
    configured_at_us: int,
    policy_reference: str,
    policy_sha256: str,
    binding_reference: str,
    binding_sha256: str,
) -> None:
    """Insert the policy, source mapping, and one-token state atomically."""
    connection.execute(
        text(
            "INSERT INTO od_source_gate_policies "
            "(quota_key, policy_revision, rate_microtokens_per_second, capacity_microtokens, "
            "policy_reference, policy_sha256, configured_by, configured_at_us) "
            "VALUES (:quota_key, :revision, :rate, :capacity, :reference, :sha256, :actor, :now_us)"
        ),
        {
            "quota_key": _QUOTA_KEY,
            "revision": _POLICY_REVISION,
            "rate": _RATE_MICROTOKENS_PER_SECOND,
            "capacity": _CAPACITY_MICROTOKENS,
            "reference": policy_reference,
            "sha256": policy_sha256,
            "actor": actor,
            "now_us": configured_at_us,
        },
    )
    connection.execute(
        text(
            "INSERT INTO od_source_gate_source_bindings "
            "(source, quota_key, binding_reference, binding_sha256, configured_by, "
            "configured_at_us) VALUES (:source, :quota_key, :reference, :sha256, :actor, :now_us)"
        ),
        {
            "source": _SOURCE,
            "quota_key": _QUOTA_KEY,
            "reference": binding_reference,
            "sha256": binding_sha256,
            "actor": actor,
            "now_us": configured_at_us,
        },
    )
    connection.execute(
        text(
            "INSERT INTO od_source_gate_state "
            "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
            "VALUES (:quota_key, :tokens, 0, :now_us, :revision)"
        ),
        {
            "quota_key": _QUOTA_KEY,
            "tokens": _CAPACITY_MICROTOKENS,
            "now_us": configured_at_us,
            "revision": _POLICY_REVISION,
        },
    )


def _verify_quota_rows(
    connection: Connection,
    *,
    actor: str,
    configured_at_us: int,
    policy_reference: str,
    policy_sha256: str,
    binding_reference: str,
    binding_sha256: str,
) -> None:
    """Read back all three rows and reject any partial or altered local state."""
    policy = (
        connection.execute(
            text("SELECT * FROM od_source_gate_policies WHERE quota_key = :quota_key"),
            {"quota_key": _QUOTA_KEY},
        )
        .mappings()
        .all()
    )
    binding = (
        connection.execute(
            text("SELECT * FROM od_source_gate_source_bindings WHERE source = :source"),
            {"source": _SOURCE},
        )
        .mappings()
        .all()
    )
    state = (
        connection.execute(
            text("SELECT * FROM od_source_gate_state WHERE quota_key = :quota_key"),
            {"quota_key": _QUOTA_KEY},
        )
        .mappings()
        .all()
    )
    if len(policy) != 1 or len(binding) != 1 or len(state) != 1:
        raise Csi500SampleQuotaUnavailableError("quota configuration read-back failed")
    policy_row = policy[0]
    binding_row = binding[0]
    state_row = state[0]
    if (
        policy_row["quota_key"] != _QUOTA_KEY
        or policy_row["policy_revision"] != _POLICY_REVISION
        or policy_row["rate_microtokens_per_second"] != _RATE_MICROTOKENS_PER_SECOND
        or policy_row["capacity_microtokens"] != _CAPACITY_MICROTOKENS
        or policy_row["policy_reference"] != policy_reference
        or policy_row["policy_sha256"] != policy_sha256
        or policy_row["configured_by"] != actor
        or policy_row["configured_at_us"] != configured_at_us
        or binding_row["source"] != _SOURCE
        or binding_row["quota_key"] != _QUOTA_KEY
        or binding_row["binding_reference"] != binding_reference
        or binding_row["binding_sha256"] != binding_sha256
        or binding_row["configured_by"] != actor
        or binding_row["configured_at_us"] != configured_at_us
        or state_row["quota_key"] != _QUOTA_KEY
        or state_row["tokens_micro"] != _CAPACITY_MICROTOKENS
        or state_row["remainder"] != 0
        or state_row["last_refill_us"] != configured_at_us
        or state_row["policy_revision"] != _POLICY_REVISION
    ):
        raise Csi500SampleQuotaUnavailableError("quota configuration read-back failed")


def _canonical_sha256(payload: dict[str, object]) -> str:
    """Hash one deterministic UTF-8 JSON object without normalizing its values."""
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _identity_sha256(identity: DatabaseFileIdentity) -> str:
    """Hash only the target's numeric device/inode identity, not its path."""
    return _canonical_sha256({"device": identity.device, "inode": identity.inode})


def _reject_secret_like_text(value: str, name: str) -> None:
    """Reject common credential-shaped strings without echoing the supplied data."""
    if (
        _SECRET_LIKE_RE.search(value) is not None
        or _URI_USERINFO_RE.search(value) is not None
        or ".env" in value.lower()
    ):
        raise Csi500SampleQuotaInputError(f"{name} must not contain credential-like material")


__all__ = [
    "Csi500SampleQuotaCleanupError",
    "Csi500SampleQuotaCommitUncertainError",
    "Csi500SampleQuotaCommittedCleanupError",
    "Csi500SampleQuotaConflictError",
    "Csi500SampleQuotaError",
    "Csi500SampleQuotaGrantError",
    "Csi500SampleQuotaInputError",
    "Csi500SampleQuotaReceipt",
    "Csi500SampleQuotaStatus",
    "Csi500SampleQuotaUnavailableError",
    "provision_csi500_sample_quota",
]
