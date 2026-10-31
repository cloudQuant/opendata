"""Prepare one isolated CSI 500 Fuyao authorization candidate.

This module owns the sample target, offline calendar evidence, a locally
declared source grant and quota bucket, and deterministic one-shot intent
preparation. Authorization writes only to a fresh, identity-pinned SQLite
target after its real source-gate schema is checked. Preparation rereads the
actual grant, quota, and gate state before writing non-secret identity pins to
the isolated manifest. Neither operation locks the one-shot scope, enqueues a
run, reads credentials, starts a worker, or calls Fuyao. The pre-existing
authorized V1 job builder and its established gate path are not changed by
this module.

The code records a user-declared internal-use basis. It does not verify a
Fuyao contract, authenticate a local calendar artifact, establish source
history completeness, or authorize external redistribution.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import URL, create_engine
from sqlalchemy.pool import NullPool

from alembic_source_gate.path_contract import (
    DatabaseFileIdentity,
    assert_database_file_identity,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opendata.data.source_attempt_gate import SourceAttemptScopeV3
    from opendata.pipeline.ths_index_daily_publication import Csi500FuyaoV4SampleSpec

SAMPLE_TARGET_PREFIX = "opendata-csi500-sample-"
SAMPLE_TEMP_ROOT = Path("/tmp")  # noqa: S108 - fixed private target root, mode 0700 per target
SAMPLE_DATABASE_FILENAME = "sample.sqlite3"
SAMPLE_MANIFEST_FILENAME = "target.json"
SAMPLE_LOCK_FILENAME = ".operation.lock"
SAMPLE_SOURCE_GATE_MIGRATION_HEAD = "sg0004_source_one_shot_v4"
SAMPLE_SOURCE_GATE_VERSION_TABLE = "alembic_version_source_gate"
SAMPLE_TARGET_PROFILE = "csi500-source-gate-authorization-only-v1"
SAMPLE_WAREHOUSE_SCHEMA_STATUS = "NOT_APPLIED"
SAMPLE_DATASET_ID = "cn.csi500.index.daily"
SAMPLE_SOURCE = "ths"
SAMPLE_MODEL = "index_daily"
SAMPLE_SYMBOL = "000905.SH"
SAMPLE_QUOTA_KEY = "csi500-fuyao-sample-204"
SAMPLE_EVIDENCE_REFERENCE = "docs/data-rights-registry.md#0.1"
SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND = 1
SAMPLE_QUOTA_CAPACITY_MICROTOKENS = 1_000_000
SAMPLE_GRANT_LIFETIME = timedelta(minutes=15)

_PHASES = frozenset(
    {
        "created",
        "migration_started",
        "migration_uncertain",
        "source_gate_initialized",
        # Legacy dual-chain state is readable for preservation, but cannot
        # advance under the source-gate-only target profile.
        "migrated",
        "authorization_started",
        "authorization_uncertain",
        "authorized",
        "intent_prepared",
    }
)
_VALID_TRANSITIONS = {
    "created": frozenset({"migration_started"}),
    "migration_started": frozenset({"migration_uncertain", "source_gate_initialized"}),
    "migration_uncertain": frozenset(),
    "source_gate_initialized": frozenset({"authorization_started"}),
    "migrated": frozenset(),
    "authorization_started": frozenset({"authorization_uncertain", "authorized"}),
    "authorization_uncertain": frozenset(),
    "authorized": frozenset({"intent_prepared"}),
    "intent_prepared": frozenset(),
}
_SAMPLE_DIRECTORY_NAME = re.compile(r"^opendata-csi500-sample-[0-9a-f]{32}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_LIKE = re.compile(
    r"(?i)(?:\bsk-[A-Za-z0-9_-]{12,}\b|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*\S+|"
    r"\bbearer\s+\S+)"
)
_CALENDAR_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MAX_CALENDAR_MANIFEST_BYTES = 1_048_576
_MAX_CALENDAR_ARTIFACT_BYTES = 64 * 1024 * 1024
_MAX_STATE_BYTES = 32_768
_FRESH_SAMPLE_GATE_TABLES = (
    "od_source_gate_clock",
    "od_source_gate_policies",
    "od_source_gate_source_bindings",
    "od_source_gate_state",
    "od_source_grants",
    "od_source_grant_hosts",
    "od_source_grant_host_sets",
    "od_source_grant_events",
    "od_source_attempt_reservations",
    "od_source_attempt_scope_v2",
    "od_source_attempt_scope_v3",
    "od_source_one_shot_configs",
    "od_source_one_shot_permits",
)
_ALLOWED_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "target_profile",
        "warehouse_schema_status",
        "target_id",
        "phase",
        "database_filename",
        "database_device",
        "database_inode",
        "owner_uid",
        "created_at_utc",
        "updated_at_utc",
        "calendar_date",
        "calendar_manifest_sha256",
        "calendar_artifact_sha256",
        "calendar_source_reference",
        "calendar_version",
        "calendar_reviewed_by",
        "authorization_actor",
        "source_rights_basis",
        "grant_id",
        "principal_scope",
        "grant_expires_at_utc",
        "rights_evidence_sha256",
        "quota_key",
        "quota_policy_revision",
        "quota_policy_sha256",
        "quota_binding_sha256",
        "quota_target_identity_sha256",
        "sample_idempotency_key",
        "capture_intent_id",
        "one_shot_intent_sha256",
        "intent_schema_version",
        "intent_prepared_at_utc",
        "request_summary_sha256",
        "request_id",
        "attempt_id",
        "request_scheme",
        "request_method",
        "request_host",
        "request_path",
        "source_policy_id",
        "source_policy_revision",
        "source_policy_sha256",
        "one_shot_scope_status",
        "run_key",
        "job_id",
        "job_version",
        "dataset_id",
        "max_source_attempts",
    }
)


class Csi500SampleCompositionError(RuntimeError):
    """Fixed safe failure for sample-composition and target validation."""

    def __init__(self, code: str) -> None:
        """Create an error whose message cannot contain paths or credentials."""
        if not isinstance(code, str) or _IDENTIFIER.fullmatch(code) is None:
            code = "sample_composition_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class SampleTarget:
    """One exact program-created temporary SQLite target and its local state."""

    directory: Path
    database_path: Path
    identity: DatabaseFileIdentity
    phase: str
    state: Mapping[str, object]

    def __repr__(self) -> str:
        """Avoid exposing local filesystem paths in generic logs."""
        return f"SampleTarget(phase={self.phase!r}, identity=<pinned>)"


@dataclass(frozen=True, slots=True, repr=False)
class Csi500TradingDayEvidence:
    """Locally reviewed evidence for one historical SSE trading date."""

    business_date: date
    calendar_version: str
    source_reference: str
    reviewed_by: str
    manifest_sha256: str
    artifact_sha256: str

    def __post_init__(self) -> None:
        """Reject malformed evidence metadata without implying its authenticity."""
        if type(self.business_date) is not date or self.business_date >= _shanghai_today():
            raise Csi500SampleCompositionError("calendar_sample_date_invalid")
        _bounded_identifier(self.calendar_version, "calendar_version")
        _bounded_identifier(self.reviewed_by, "reviewed_by")
        _bounded_source_reference(self.source_reference)
        _bounded_sha(self.manifest_sha256, "calendar_manifest_sha256")
        _bounded_sha(self.artifact_sha256, "calendar_artifact_sha256")

    def __repr__(self) -> str:
        """Keep artifact references out of routine diagnostic output."""
        return f"Csi500TradingDayEvidence(business_date={self.business_date!s}, pinned=True)"


@dataclass(frozen=True, slots=True)
class SampleMigrationState:
    """Actual source-gate head and explicitly absent warehouse schema state."""

    warehouse_schema_status: Literal["NOT_APPLIED"]
    source_gate_head: str


SAMPLE_EXPECTED_MIGRATION_STATE = SampleMigrationState(
    warehouse_schema_status=SAMPLE_WAREHOUSE_SCHEMA_STATUS,
    source_gate_head=SAMPLE_SOURCE_GATE_MIGRATION_HEAD,
)


@dataclass(frozen=True, slots=True)
class Csi500SamplePreview:
    """Unauthenticated request-shape preview that touches no target or source."""

    status: Literal["preview_only"]
    authorization_status: Literal["not_established"]
    calendar_evidence_status: Literal["not_verified"]
    target_status: Literal["not_required_not_accessed"]
    gate_status: Literal["not_accessed"]
    execution_enabled: Literal[False]
    dataset_id: str
    symbol: str
    business_date: date
    method: str
    host: str
    path: str
    query_summary_sha256: str


def create_sample_target() -> SampleTarget:
    """Create an empty, mode-0600 SQLite file under a fresh private `/tmp` directory.

    Returns:
        A pinned target whose database has not had the warehouse schema applied.

    Raises:
        Csi500SampleCompositionError: If the operating-system temp root or
            created file does not meet the fixed local-target contract.
    """
    return _create_sample_target(SAMPLE_TEMP_ROOT)


def preview_csi500_sample_request(*, business_date: date) -> Csi500SamplePreview:
    """Describe the fixed one-date request shape without authorization or I/O.

    The date is caller supplied and is not verified against an exchange
    calendar. This preview does not inspect a sample target, SQLite, the
    environment, credentials, or a provider, and it is never a send permit.
    """
    if type(business_date) is not date:
        raise Csi500SampleCompositionError("calendar_date_invalid")
    from opendata.data.providers.ths.endpoints import (
        INDEX_PRICES_ENDPOINT,
        build_index_prices_request,
    )
    from opendata.data.providers.ths.transport.credentials import (
        DEFAULT_FUYAO_API_BASE_URL,
    )
    from opendata.data.request_summary import request_summary_sha256

    parameters = build_index_prices_request(
        symbol=SAMPLE_SYMBOL,
        start=business_date,
        end=date.fromordinal(business_date.toordinal() + 1),
    )
    query_digest = request_summary_sha256(
        {key: str(value) for key, value in parameters.items()},
        allowed_fields=("end", "interval", "start", "thscode"),
    )
    endpoint = urlsplit(DEFAULT_FUYAO_API_BASE_URL)
    if endpoint.scheme != "https" or endpoint.hostname != "fuyao.aicubes.cn":
        raise Csi500SampleCompositionError("preview_route_invalid")
    return Csi500SamplePreview(
        status="preview_only",
        authorization_status="not_established",
        calendar_evidence_status="not_verified",
        target_status="not_required_not_accessed",
        gate_status="not_accessed",
        execution_enabled=False,
        dataset_id=SAMPLE_DATASET_ID,
        symbol=SAMPLE_SYMBOL,
        business_date=business_date,
        method="GET",
        host=endpoint.hostname,
        path=INDEX_PRICES_ENDPOINT,
        query_summary_sha256=query_digest,
    )


def _create_sample_target(temp_root: Path) -> SampleTarget:
    """Create a target below an explicit root; the private helper supports tests."""
    if not isinstance(temp_root, Path) or not temp_root.is_absolute():
        raise Csi500SampleCompositionError("temporary_root_invalid")
    try:
        root = temp_root.resolve(strict=True)
        root_stat = temp_root.lstat()
    except OSError:
        raise Csi500SampleCompositionError("temporary_root_unavailable") from None
    if (
        root != temp_root
        or stat.S_ISLNK(root_stat.st_mode)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root_stat.st_uid not in {0, os.getuid()}
    ):
        raise Csi500SampleCompositionError("temporary_root_invalid")

    target_id = uuid4().hex
    directory = root / f"{SAMPLE_TARGET_PREFIX}{target_id}"
    database_path = directory / SAMPLE_DATABASE_FILENAME
    try:
        os.mkdir(directory, 0o700)
        directory_stat = directory.lstat()
        if (
            stat.S_IMODE(directory_stat.st_mode) != 0o700
            or directory_stat.st_uid != os.getuid()
            or stat.S_ISLNK(directory_stat.st_mode)
        ):
            raise Csi500SampleCompositionError("temporary_directory_permissions_invalid")
        descriptor = os.open(
            database_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            os.fsync(descriptor)
            database_stat = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (
            not stat.S_ISREG(database_stat.st_mode)
            or stat.S_IMODE(database_stat.st_mode) != 0o600
            or database_stat.st_uid != os.getuid()
        ):
            raise Csi500SampleCompositionError("temporary_database_permissions_invalid")
        identity = DatabaseFileIdentity(database_stat.st_dev, database_stat.st_ino)
        now = _utc_now_text()
        state: dict[str, object] = {
            "schema_version": 2,
            "target_profile": SAMPLE_TARGET_PROFILE,
            "warehouse_schema_status": SAMPLE_WAREHOUSE_SCHEMA_STATUS,
            "target_id": target_id,
            "phase": "created",
            "database_filename": SAMPLE_DATABASE_FILENAME,
            "database_device": identity.device,
            "database_inode": identity.inode,
            "owner_uid": os.getuid(),
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        _create_manifest_file(directory, state)
        return SampleTarget(directory, database_path, identity, "created", state)
    except Csi500SampleCompositionError:
        raise
    except OSError:
        raise Csi500SampleCompositionError("temporary_target_creation_failed") from None


def load_sample_target(directory_value: str | Path) -> SampleTarget:
    """Load and pin a target created by this composition; no defaults are used."""
    if isinstance(directory_value, Path):
        directory = directory_value
    elif isinstance(directory_value, str) and directory_value:
        directory = Path(directory_value)
    else:
        raise Csi500SampleCompositionError("target_directory_invalid")
    if not directory.is_absolute() or ".." in directory.parts:
        raise Csi500SampleCompositionError("target_directory_invalid")
    try:
        resolved = directory.resolve(strict=True)
        directory_stat = directory.lstat()
    except OSError:
        raise Csi500SampleCompositionError("target_directory_unavailable") from None
    if (
        resolved != directory
        or not _SAMPLE_DIRECTORY_NAME.fullmatch(directory.name)
        or directory.parent != SAMPLE_TEMP_ROOT
        or stat.S_ISLNK(directory_stat.st_mode)
        or not stat.S_ISDIR(directory_stat.st_mode)
        or directory_stat.st_uid != os.getuid()
        or stat.S_IMODE(directory_stat.st_mode) != 0o700
    ):
        raise Csi500SampleCompositionError("target_directory_identity_invalid")
    database_path = directory / SAMPLE_DATABASE_FILENAME
    state = _read_manifest(directory)
    directory_target_id = directory.name.removeprefix(SAMPLE_TARGET_PREFIX)
    if state.get("target_id") != directory_target_id:
        raise Csi500SampleCompositionError("target_manifest_identity_mismatch")
    try:
        database_stat = database_path.lstat()
    except OSError:
        raise Csi500SampleCompositionError("target_database_missing") from None
    identity = DatabaseFileIdentity(database_stat.st_dev, database_stat.st_ino)
    if (
        stat.S_ISLNK(database_stat.st_mode)
        or not stat.S_ISREG(database_stat.st_mode)
        or database_stat.st_uid != os.getuid()
        or stat.S_IMODE(database_stat.st_mode) != 0o600
        or state.get("database_filename") != SAMPLE_DATABASE_FILENAME
        or state.get("database_device") != identity.device
        or state.get("database_inode") != identity.inode
        or state.get("owner_uid") != os.getuid()
    ):
        raise Csi500SampleCompositionError("target_database_identity_invalid")
    phase = state.get("phase")
    if not isinstance(phase, str) or phase not in _PHASES:
        raise Csi500SampleCompositionError("target_phase_invalid")
    expected_files = {
        SAMPLE_DATABASE_FILENAME,
        SAMPLE_MANIFEST_FILENAME,
        f"{SAMPLE_DATABASE_FILENAME}-wal",
        f"{SAMPLE_DATABASE_FILENAME}-shm",
        f"{SAMPLE_DATABASE_FILENAME}-journal",
    }
    try:
        names = {item.name for item in directory.iterdir()}
    except OSError:
        raise Csi500SampleCompositionError("target_directory_unreadable") from None
    if not names.issubset(expected_files | {SAMPLE_LOCK_FILENAME}):
        raise Csi500SampleCompositionError("target_contains_unexpected_files")
    return SampleTarget(directory, database_path, identity, phase, state)


def _require_source_gate_only_target(target: SampleTarget) -> None:
    """Refuse legacy manifests instead of upgrading or reinterpreting candidates."""
    if (
        type(target.state.get("schema_version")) is not int
        or target.state.get("schema_version") != 2
        or target.state.get("target_profile") != SAMPLE_TARGET_PROFILE
        or target.state.get("warehouse_schema_status") != SAMPLE_WAREHOUSE_SCHEMA_STATUS
    ):
        raise Csi500SampleCompositionError("legacy_target_profile_read_only")


@contextmanager
def lock_sample_target(target: SampleTarget, *, operation: str) -> Iterator[None]:
    """Take an exclusive no-follow operation lock for one target invocation."""
    current = load_sample_target(target.directory)
    if current.identity != target.identity or current.phase != target.phase:
        raise Csi500SampleCompositionError("target_changed_before_lock")
    _require_source_gate_only_target(current)
    if not isinstance(operation, str) or _IDENTIFIER.fullmatch(operation) is None:
        raise Csi500SampleCompositionError("operation_invalid")
    lock_path = target.directory / SAMPLE_LOCK_FILENAME
    try:
        descriptor = os.open(
            lock_path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        raise Csi500SampleCompositionError("target_locked_or_lock_unavailable") from None
    try:
        os.write(descriptor, f"pid={os.getpid()} operation={operation}\n".encode("ascii"))
        os.fsync(descriptor)
        yield
    finally:
        os.close(descriptor)
        try:
            lock_stat = lock_path.lstat()
            if (
                stat.S_ISREG(lock_stat.st_mode)
                and lock_stat.st_uid == os.getuid()
                and stat.S_IMODE(lock_stat.st_mode) == 0o600
            ):
                lock_path.unlink()
        except OSError:
            raise Csi500SampleCompositionError("target_lock_cleanup_uncertain") from None


def _require_target_operation_lock(target: SampleTarget, *, operation: str) -> None:
    """Require this process to hold the exact private lock for one mutation."""
    lock_path = target.directory / SAMPLE_LOCK_FILENAME
    try:
        metadata = lock_path.lstat()
        contents = _read_bounded_file(lock_path, 256)
    except Exception:
        raise Csi500SampleCompositionError("target_operation_lock_required") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or contents != f"pid={os.getpid()} operation={operation}\n".encode("ascii")
    ):
        raise Csi500SampleCompositionError("target_operation_lock_required")


def transition_sample_target(
    target: SampleTarget,
    *,
    expected_phase: str,
    new_phase: str,
    updates: Mapping[str, object] | None = None,
) -> SampleTarget:
    """Atomically record a conservative phase transition in the target manifest."""
    current = load_sample_target(target.directory)
    if current.identity != target.identity or current.phase != expected_phase:
        raise Csi500SampleCompositionError("target_phase_conflict")
    _require_source_gate_only_target(current)
    if new_phase not in _VALID_TRANSITIONS.get(expected_phase, frozenset()):
        raise Csi500SampleCompositionError("target_phase_transition_invalid")
    changes = {} if updates is None else dict(updates)
    if not changes.keys() <= (_ALLOWED_MANIFEST_KEYS - _BASE_MANIFEST_KEYS):
        raise Csi500SampleCompositionError("target_state_field_invalid")
    if any(
        changes.get(name, current.state.get(name)) != current.state.get(name)
        for name in ("target_profile", "warehouse_schema_status")
    ):
        raise Csi500SampleCompositionError("target_profile_fields_immutable")
    if any(not _safe_manifest_value(value) for value in changes.values()):
        raise Csi500SampleCompositionError("target_state_value_invalid")
    state = dict(current.state)
    state.update(changes)
    state["phase"] = new_phase
    state["updated_at_utc"] = _utc_now_text()
    _write_manifest_atomically(current.directory, state)
    return load_sample_target(current.directory)


def prepare_csi500_sample_one_shot_intent(
    target: SampleTarget,
    *,
    calendar_evidence: Csi500TradingDayEvidence,
) -> Csi500FuyaoV4SampleSpec:
    """Persist one deterministic candidate after read-only gate verification.

    The exact V4 request IDs and scope are derived from the isolated target,
    reviewed local calendar bytes, and existing authorization/quota receipts.
    This operation checks the target's actual source-gate marker and schema,
    confirms that no warehouse schema is present, and verifies grant and quota
    rows, active grant status, and that no one-shot scope is configured. It
    writes only to the manifest; it does not read credentials, enqueue work, or
    send a request. Repeating it returns the same immutable spec only after the
    same read-only checks pass. The resulting scope is a candidate, not a gate
    lock or send permit.
    """
    current = load_sample_target(target.directory)
    if current.identity != target.identity:
        raise Csi500SampleCompositionError("target_identity_changed")
    with lock_sample_target(current, operation="intent-preparation"):
        return _prepare_csi500_sample_one_shot_intent(
            current,
            calendar_evidence=calendar_evidence,
        )


def _prepare_csi500_sample_one_shot_intent(
    current: SampleTarget,
    *,
    calendar_evidence: Csi500TradingDayEvidence,
) -> Csi500FuyaoV4SampleSpec:
    """Perform the manifest-only deterministic preparation under its file lock."""
    expected_identity = current.identity
    current = load_sample_target(current.directory)
    if current.identity != expected_identity or current.phase not in {
        "authorized",
        "intent_prepared",
    }:
        raise Csi500SampleCompositionError("target_not_authorized")
    if type(calendar_evidence) is not Csi500TradingDayEvidence:
        raise Csi500SampleCompositionError("calendar_evidence_invalid")
    _validate_sample_calendar_pin(current, calendar_evidence)
    if _read_migration_state(current) != SAMPLE_EXPECTED_MIGRATION_STATE:
        raise Csi500SampleCompositionError("migration_state_mismatch")
    _validate_authorization_configuration(current)
    spec = _build_sample_v4_spec(current, calendar_evidence)
    _inspect_sample_gate_configuration(current, scope=spec.source_attempt_scope)
    if current.phase == "intent_prepared":
        if not _manifest_matches_sample_spec(current.state, spec):
            raise Csi500SampleCompositionError("sample_intent_manifest_mismatch")
        return spec
    if current.phase != "authorized":
        raise Csi500SampleCompositionError("target_not_authorized")
    prepared = transition_sample_target(
        current,
        expected_phase="authorized",
        new_phase="intent_prepared",
        updates={
            **_sample_spec_manifest_values(spec),
            "intent_prepared_at_utc": _utc_now_text(),
            "one_shot_scope_status": "CANDIDATE_NOT_LOCKED",
        },
    )
    if not _manifest_matches_sample_spec(prepared.state, spec):
        raise Csi500SampleCompositionError("sample_intent_manifest_unconfirmed")
    return spec


def _build_sample_v4_spec(
    target: SampleTarget,
    calendar_evidence: Csi500TradingDayEvidence,
) -> Csi500FuyaoV4SampleSpec:
    """Create the pipeline-owned V4 intent from bounded manifest scalar data."""
    from opendata.pipeline.ths_index_daily_publication import build_csi500_v4_sample_spec

    state = target.state
    try:
        return build_csi500_v4_sample_spec(
            business_date=calendar_evidence.business_date,
            target_id=state["target_id"],
            target_identity_sha256=state["quota_target_identity_sha256"],
            calendar_manifest_sha256=calendar_evidence.manifest_sha256,
            calendar_artifact_sha256=calendar_evidence.artifact_sha256,
            authorization_actor=state["authorization_actor"],
            grant_id=state["grant_id"],
            grant_revision=1,
            principal_scope=state["principal_scope"],
            grant_expires_at_utc=state["grant_expires_at_utc"],
            rights_evidence_sha256=state["rights_evidence_sha256"],
            budget_binding_sha256=state["quota_binding_sha256"],
            budget_quota_key=state["quota_key"],
            budget_policy_revision=state["quota_policy_revision"],
            budget_policy_sha256=state["quota_policy_sha256"],
        )
    except Exception:
        raise Csi500SampleCompositionError("sample_intent_invalid") from None


def _sample_spec_manifest_values(spec: Csi500FuyaoV4SampleSpec) -> dict[str, object]:
    """Return only scalar, non-secret identity values for the target manifest."""
    from opendata.pipeline.ths_index_daily_publication import (
        Csi500FuyaoV4SampleSpec as SampleSpec,
    )

    if type(spec) is not SampleSpec:
        raise Csi500SampleCompositionError("sample_intent_type_invalid")
    attempt = spec.source_attempt_scope.attempt
    return {
        "intent_schema_version": 1,
        "capture_intent_id": spec.capture_intent_id,
        "one_shot_intent_sha256": spec.intent_sha256,
        "sample_idempotency_key": spec.capture_intent_id,
        "run_key": spec.run_key,
        "job_id": spec.task_definition.job_id,
        "job_version": spec.task_definition.version,
        "dataset_id": spec.task_definition.dataset_id,
        "request_summary_sha256": spec.source_attempt_scope.request_summary_sha256,
        "request_id": spec.request_id,
        "attempt_id": spec.attempt_id,
        "request_scheme": spec.source_attempt_scope.request_scheme,
        "request_method": spec.source_attempt_scope.request_method,
        "request_host": spec.source_attempt_scope.request_host,
        "request_path": spec.source_attempt_scope.request_path,
        "source_policy_id": spec.source_policy_id,
        "source_policy_revision": spec.source_policy_revision,
        "source_policy_sha256": spec.source_policy_sha256,
        "max_source_attempts": attempt.attempt_no,
        "calendar_date": spec.business_date.isoformat(),
        "one_shot_scope_status": "CANDIDATE_NOT_LOCKED",
    }


def _manifest_matches_sample_spec(
    state: Mapping[str, object], spec: Csi500FuyaoV4SampleSpec
) -> bool:
    """Compare persisted preparation fields with one recomputed immutable spec."""
    expected = _sample_spec_manifest_values(spec)
    return all(state.get(key) == value for key, value in expected.items())


def _validate_sample_calendar_pin(
    target: SampleTarget,
    calendar_evidence: Csi500TradingDayEvidence,
) -> None:
    """Require the requested date and both frozen local calendar digests to match."""
    state = target.state
    if (
        state.get("calendar_date") != calendar_evidence.business_date.isoformat()
        or state.get("calendar_manifest_sha256") != calendar_evidence.manifest_sha256
        or state.get("calendar_artifact_sha256") != calendar_evidence.artifact_sha256
    ):
        raise Csi500SampleCompositionError("calendar_evidence_pin_mismatch")


def provision_sample_source_authorization(
    target: SampleTarget,
    *,
    calendar_evidence: Csi500TradingDayEvidence,
    actor: str,
    principal_scope: str,
    confirm_user_declared_internal_use: bool,
) -> SampleTarget:
    """Provision one short-lived local self-declared grant and quota bucket.

    This writes only to the isolated sample SQLite file. It records the
    current user-declared Fuyao internal-use basis, not independently verified
    supplier rights. The target must be locked by this CLI, at the exact
    source-gate head, and have an empty V4 gate before writes. After grant and
    quota commits, the function reads back the actual rows and active grant
    before marking the manifest authorized. Any failure after the intent is
    recorded leaves the target sealed in ``authorization_started``; callers
    must inspect it and must never retry that target blindly.
    """
    if type(calendar_evidence) is not Csi500TradingDayEvidence:
        raise Csi500SampleCompositionError("calendar_evidence_invalid")
    if confirm_user_declared_internal_use is not True:
        raise Csi500SampleCompositionError("internal_use_confirmation_required")
    actor_value = _bounded_identifier(actor, "authorization_actor")
    principal = _bounded_identifier(principal_scope, "principal_scope")
    current = load_sample_target(target.directory)
    if current.identity != target.identity or current.phase != "source_gate_initialized":
        raise Csi500SampleCompositionError("target_not_source_gate_initialized")
    _require_source_gate_only_target(current)
    _require_target_operation_lock(current, operation="authorization")
    if _read_migration_state(current) != SAMPLE_EXPECTED_MIGRATION_STATE:
        raise Csi500SampleCompositionError("migration_state_mismatch")
    _require_fresh_sample_gate(current)
    scope_suffix = uuid4().hex
    grant_id = f"csi500-sample-{scope_suffix}"
    now = datetime.now(timezone.utc)
    expires_at = now + SAMPLE_GRANT_LIFETIME
    evidence_path = _canonical_regular_file(
        Path(__file__).resolve().parents[2] / SAMPLE_EVIDENCE_REFERENCE.split("#", 1)[0],
        max_bytes=1_048_576,
    )
    rights_sha256 = hashlib.sha256(_read_bounded_file(evidence_path, 1_048_576)).hexdigest()
    current = transition_sample_target(
        current,
        expected_phase="source_gate_initialized",
        new_phase="authorization_started",
        updates={
            "calendar_date": calendar_evidence.business_date.isoformat(),
            "calendar_manifest_sha256": calendar_evidence.manifest_sha256,
            "calendar_artifact_sha256": calendar_evidence.artifact_sha256,
            "calendar_source_reference": calendar_evidence.source_reference,
            "calendar_version": calendar_evidence.calendar_version,
            "calendar_reviewed_by": calendar_evidence.reviewed_by,
            "authorization_actor": actor_value,
            "source_rights_basis": "USER_DECLARED_UNVERIFIED",
            "grant_id": grant_id,
            "principal_scope": principal,
            "grant_expires_at_utc": _format_aware_timestamp(expires_at),
            "rights_evidence_sha256": rights_sha256,
        },
    )
    engine = None
    try:
        from opendata.data.source_attempt_gate import SourceAttemptGate
        from opendata.data.source_gate_quota_provisioning import (
            provision_csi500_sample_quota,
        )
        from opendata.data.source_grant_provisioning import (
            SourceGrantProvisioner,
            SourceGrantProvisionRequest,
        )

        engine = create_engine(
            URL.create("sqlite", database=str(current.database_path)),
            poolclass=NullPool,
            connect_args={"timeout": 5.0},
        )
        gate = SourceAttemptGate(engine)
        grant_request = SourceGrantProvisionRequest(
            grant_id=grant_id,
            revision=1,
            source=SAMPLE_SOURCE,
            canonical_model=SAMPLE_MODEL,
            operation="query",
            principal_scope=principal,
            host="fuyao.aicubes.cn",
            not_before=now - timedelta(seconds=5),
            expires_at=expires_at,
            rights_evidence_reference=SAMPLE_EVIDENCE_REFERENCE,
            rights_evidence_sha256=rights_sha256,
            operator_confirms_authorized_use=True,
            operator_attestation_note=(
                "User-declared Fuyao internal use for a one-day CSI 500 sample; "
                "supplier terms are not independently verified."
            ),
            attested_by=actor_value,
            conditions=(),
        )
        grant_result = SourceGrantProvisioner(gate).provision(grant_request)
        _validate_sample_grant_result(
            grant_result,
            grant_id=grant_id,
            host="fuyao.aicubes.cn",
            expires_at=expires_at,
            actor=actor_value,
        )
        quota_receipt = provision_csi500_sample_quota(
            database_path=current.database_path,
            expected_identity=current.identity,
            grant_id=grant_id,
            principal_scope=principal,
            actor=actor_value,
            evidence_reference=SAMPLE_EVIDENCE_REFERENCE,
            expires_at=expires_at,
        )
        receipt_state = _validate_sample_quota_receipt(
            quota_receipt,
            grant_id=grant_id,
            principal_scope=principal,
            expected_identity=current.identity,
        )
    except Csi500SampleCompositionError:
        raise
    except Exception:
        raise Csi500SampleCompositionError("authorization_outcome_uncertain") from None
    finally:
        if engine is not None:
            engine.dispose()
    verification_state = dict(current.state)
    verification_state.update(receipt_state)
    _validate_authorization_configuration(current, state=verification_state)
    verification_target = SampleTarget(
        current.directory,
        current.database_path,
        current.identity,
        current.phase,
        verification_state,
    )
    spec = _build_sample_v4_spec(verification_target, calendar_evidence)
    _inspect_sample_gate_configuration(
        current,
        scope=spec.source_attempt_scope,
        state=verification_state,
    )
    # Only this acknowledged path advances authorization. A commit/cleanup
    # ambiguity leaves the phase at authorization_started and is not retried.
    authorized = transition_sample_target(
        current,
        expected_phase="authorization_started",
        new_phase="authorized",
        updates=receipt_state,
    )
    return authorized


def load_csi500_trading_day_evidence(
    manifest_value: str | Path,
    *,
    expected_business_date: date | None = None,
) -> Csi500TradingDayEvidence:
    """Read a frozen local SSE calendar manifest and verify its artifact pin.

    The parser confirms internal consistency only. A human must establish that
    the artifact really came from the cited calendar authority before using it.
    """
    manifest_path = _canonical_regular_file(manifest_value, max_bytes=_MAX_CALENDAR_MANIFEST_BYTES)
    raw = _read_bounded_file(manifest_path, _MAX_CALENDAR_MANIFEST_BYTES)
    manifest_hash = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except Exception:
        raise Csi500SampleCompositionError("calendar_manifest_invalid") from None
    required = {
        "schema_version",
        "exchange",
        "calendar_version",
        "source_reference",
        "retrieved_at_utc",
        "reviewed_by",
        "artifact_path",
        "artifact_sha256",
        "open_sessions",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise Csi500SampleCompositionError("calendar_manifest_shape_invalid")
    if value.get("exchange") != "SSE":
        raise Csi500SampleCompositionError("calendar_exchange_invalid")
    calendar_version = _bounded_identifier(value.get("calendar_version"), "calendar_version")
    reviewed_by = _bounded_identifier(value.get("reviewed_by"), "reviewed_by")
    source_reference = _bounded_source_reference(value.get("source_reference"))
    retrieved = _parse_aware_timestamp(value.get("retrieved_at_utc"))
    if retrieved > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise Csi500SampleCompositionError("calendar_timestamp_invalid")
    artifact_path = _canonical_regular_file(
        value.get("artifact_path"), max_bytes=_MAX_CALENDAR_ARTIFACT_BYTES
    )
    artifact_hash = _bounded_sha(value.get("artifact_sha256"), "artifact_sha256")
    actual_artifact_hash = hashlib.sha256(
        _read_bounded_file(artifact_path, _MAX_CALENDAR_ARTIFACT_BYTES)
    ).hexdigest()
    if not hmac.compare_digest(actual_artifact_hash, artifact_hash):
        raise Csi500SampleCompositionError("calendar_artifact_pin_mismatch")
    sessions = value.get("open_sessions")
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 10_000:
        raise Csi500SampleCompositionError("calendar_sessions_invalid")
    parsed_sessions = tuple(_parse_calendar_date(item) for item in sessions)
    if tuple(sorted(set(parsed_sessions))) != parsed_sessions:
        raise Csi500SampleCompositionError("calendar_sessions_not_canonical")
    business_date = expected_business_date
    if business_date is None:
        if len(parsed_sessions) != 1:
            raise Csi500SampleCompositionError("calendar_sample_date_required")
        business_date = parsed_sessions[0]
    if type(business_date) is not date or business_date not in parsed_sessions:
        raise Csi500SampleCompositionError("sample_date_not_in_calendar")
    if business_date >= _shanghai_today():
        raise Csi500SampleCompositionError("sample_date_must_be_completed")
    return Csi500TradingDayEvidence(
        business_date=business_date,
        calendar_version=calendar_version,
        source_reference=source_reference,
        reviewed_by=reviewed_by,
        manifest_sha256=manifest_hash,
        artifact_sha256=artifact_hash,
    )


def _require_fresh_sample_gate(target: SampleTarget) -> None:
    """Require an exact V4 target whose gate rows are all empty before grants."""
    current = load_sample_target(target.directory)
    if current.identity != target.identity:
        raise Csi500SampleCompositionError("target_identity_changed")
    if _read_migration_state(current) != SAMPLE_EXPECTED_MIGRATION_STATE:
        raise Csi500SampleCompositionError("migration_state_mismatch")
    _assert_sample_database_identity(current)
    connection = _connect_read_only(current.database_path)
    try:
        connection.execute("BEGIN")
        clock_rows = connection.execute(
            "SELECT clock_id, highwater_us FROM od_source_gate_clock"
        ).fetchall()
        if (
            len(clock_rows) != 1
            or clock_rows[0][0] != 1
            or type(clock_rows[0][1]) is not int
            or clock_rows[0][1] < 0
        ):
            raise Csi500SampleCompositionError("source_gate_clock_invalid")
        for table_name in _FRESH_SAMPLE_GATE_TABLES:
            if table_name == "od_source_gate_clock":
                # The clock singleton is required and was validated above;
                # freshness means all other gate evidence tables are empty.
                continue
            # table_name comes from the fixed module-level gate-table allowlist.
            query = f"SELECT COUNT(*) FROM {table_name}"  # noqa: S608
            row = connection.execute(query).fetchone()
            if row is None or type(row[0]) is not int or row[0] != 0:
                raise Csi500SampleCompositionError("source_gate_target_not_fresh")
        _assert_sample_database_identity(current)
    except Csi500SampleCompositionError:
        raise
    except Exception:
        raise Csi500SampleCompositionError("source_gate_freshness_unavailable") from None
    finally:
        connection.close()


def _validate_authorization_configuration(
    target: SampleTarget,
    *,
    state: Mapping[str, object] | None = None,
) -> None:
    """Read back exact migration, grant, quota, and target identity pins."""
    current = load_sample_target(target.directory)
    if current.identity != target.identity:
        raise Csi500SampleCompositionError("target_identity_changed")
    _assert_sample_database_identity(current)
    if _read_migration_state(current) != SAMPLE_EXPECTED_MIGRATION_STATE:
        raise Csi500SampleCompositionError("migration_state_mismatch")
    _validate_authorization_state(current, state=state, require_unexpired=True)
    _validate_source_gate_rows(current, allow_one_attempt=False, state=state)
    _assert_sample_database_identity(current)


def _inspect_sample_gate_configuration(
    target: SampleTarget,
    *,
    scope: SourceAttemptScopeV3,
    state: Mapping[str, object] | None = None,
) -> None:
    """Require the persisted grant to match and the candidate scope to be unlocked."""
    from opendata.data.source_attempt_gate import (
        OneShotScopeInspectionStatus,
        SourceAttemptGate,
        SourceGrantInspectionRequest,
        SourceGrantInspectionStatus,
    )

    current = load_sample_target(target.directory)
    if current.identity != target.identity:
        raise Csi500SampleCompositionError("target_identity_changed")
    pins = current.state if state is None else state
    engine = None
    try:
        _assert_sample_database_identity(current)
        engine = create_engine(
            URL.create("sqlite", database=str(current.database_path)),
            poolclass=NullPool,
            connect_args={"timeout": 5.0},
        )
        gate = SourceAttemptGate(engine)
        grant = gate.inspect_grant(
            SourceGrantInspectionRequest(
                grant_id=str(pins.get("grant_id")),
                revision=1,
                source=SAMPLE_SOURCE,
                canonical_model=SAMPLE_MODEL,
                operation="query",
                principal_scope=str(pins.get("principal_scope")),
                host="fuyao.aicubes.cn",
            )
        )
        if grant.status is not SourceGrantInspectionStatus.MATCHED:
            raise Csi500SampleCompositionError("source_grant_readback_mismatch")
        inspection = gate.inspect_one_shot_scope(scope)
        if inspection.status is not OneShotScopeInspectionStatus.UNCONFIGURED:
            raise Csi500SampleCompositionError("one_shot_scope_not_unlocked")
        _assert_sample_database_identity(current)
    except Csi500SampleCompositionError:
        raise
    except Exception:
        raise Csi500SampleCompositionError("source_gate_inspection_unavailable") from None
    finally:
        if engine is not None:
            engine.dispose()


def _validate_authorization_state(
    target: SampleTarget,
    *,
    require_unexpired: bool = True,
    state: Mapping[str, object] | None = None,
) -> None:
    """Confirm the receipt-pinned grant and quota configuration without writes."""
    state = target.state if state is None else state
    for field_name in ("grant_id", "principal_scope", "authorization_actor"):
        _bounded_identifier(state.get(field_name), field_name)
    if (
        state.get("source_rights_basis") != "USER_DECLARED_UNVERIFIED"
        or state.get("quota_key") != SAMPLE_QUOTA_KEY
        or state.get("quota_policy_revision") != 1
        or not isinstance(state.get("quota_policy_sha256"), str)
        or not isinstance(state.get("quota_binding_sha256"), str)
        or not isinstance(state.get("quota_target_identity_sha256"), str)
    ):
        raise Csi500SampleCompositionError("authorization_receipt_incomplete")
    if any(
        _SHA256.fullmatch(str(state.get(name))) is None
        for name in (
            "rights_evidence_sha256",
            "quota_policy_sha256",
            "quota_binding_sha256",
            "quota_target_identity_sha256",
            "calendar_manifest_sha256",
            "calendar_artifact_sha256",
        )
    ):
        raise Csi500SampleCompositionError("authorization_receipt_invalid")
    expiry = _parse_aware_timestamp(state.get("grant_expires_at_utc"))
    if require_unexpired and expiry <= datetime.now(timezone.utc):
        raise Csi500SampleCompositionError("source_grant_expired")
    if state.get("quota_target_identity_sha256") != _sample_identity_sha256(target.identity):
        raise Csi500SampleCompositionError("quota_target_identity_mismatch")


def _validate_source_gate_rows(
    target: SampleTarget,
    *,
    allow_one_attempt: bool,
    state: Mapping[str, object] | None = None,
) -> None:
    """Require exactly the fresh sample's grant/quota rows and bounded attempts."""
    state = target.state if state is None else state
    _assert_sample_database_identity(target)
    connection = _connect_read_only(target.database_path)
    try:
        connection.execute("BEGIN")
        grant_rows = connection.execute(
            "SELECT grant_id, source, canonical_model, operation, principal_scope, decision, "
            "revision, rights_evidence_reference, rights_evidence_sha256, expires_at_us "
            "FROM od_source_grants"
        ).fetchall()
        host_rows = connection.execute(
            "SELECT host FROM od_source_grant_hosts WHERE grant_id = ?",
            (state.get("grant_id"),),
        ).fetchall()
        seal_rows = connection.execute(
            "SELECT revision, host_count, sealed_by, reason_code "
            "FROM od_source_grant_host_sets WHERE grant_id = ?",
            (state.get("grant_id"),),
        ).fetchall()
        event_rows = connection.execute(
            "SELECT event_type, old_revision, new_revision, actor, reason_code "
            "FROM od_source_grant_events WHERE grant_id = ?",
            (state.get("grant_id"),),
        ).fetchall()
        binding_rows = connection.execute(
            "SELECT source, quota_key, binding_sha256, binding_reference, configured_by "
            "FROM od_source_gate_source_bindings"
        ).fetchall()
        policy_rows = connection.execute(
            "SELECT quota_key, policy_revision, rate_microtokens_per_second, "
            "capacity_microtokens, policy_sha256, policy_reference, configured_by "
            "FROM od_source_gate_policies"
        ).fetchall()
        bucket_rows = connection.execute(
            "SELECT quota_key, tokens_micro, policy_revision, remainder, last_refill_us "
            "FROM od_source_gate_state"
        ).fetchall()
        attempts = connection.execute(
            "SELECT attempt_no, outcome, grant_id, run_key FROM od_source_attempt_reservations"
        ).fetchall()
        sidecar_counts = {
            table_name: connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"  # noqa: S608
            ).fetchone()[0]
            for table_name in (
                "od_source_attempt_scope_v2",
                "od_source_attempt_scope_v3",
                "od_source_one_shot_configs",
                "od_source_one_shot_permits",
            )
        }
    except Exception:
        raise Csi500SampleCompositionError("source_gate_readback_unavailable") from None
    finally:
        connection.close()
    _assert_sample_database_identity(target)
    if (
        len(grant_rows) != 1
        or len(host_rows) != 1
        or len(seal_rows) != 1
        or len(event_rows) != 1
        or len(binding_rows) != 1
        or len(policy_rows) != 1
        or len(bucket_rows) != 1
        or any(type(count) is not int or count != 0 for count in sidecar_counts.values())
        or (not allow_one_attempt and attempts)
        or (allow_one_attempt and len(attempts) != 1)
    ):
        raise Csi500SampleCompositionError("source_gate_target_not_fresh")
    grant = grant_rows[0]
    seal = seal_rows[0]
    event = event_rows[0]
    binding = binding_rows[0]
    policy = policy_rows[0]
    bucket = bucket_rows[0]
    if (
        tuple(grant[:8])
        != (
            state.get("grant_id"),
            SAMPLE_SOURCE,
            SAMPLE_MODEL,
            "query",
            state.get("principal_scope"),
            "ALLOWED",
            1,
            SAMPLE_EVIDENCE_REFERENCE,
        )
        or grant[8] != state.get("rights_evidence_sha256")
        or grant[9]
        != _epoch_microseconds(_parse_aware_timestamp(state.get("grant_expires_at_utc")))
        or host_rows[0][0] != "fuyao.aicubes.cn"
        or tuple(seal) != (1, 1, state.get("authorization_actor"), "operator-self-declaration")
        or tuple(event)
        != (
            "HOSTS_SEALED",
            1,
            1,
            state.get("authorization_actor"),
            "operator-self-declaration",
        )
        or binding[0] != SAMPLE_SOURCE
        or binding[1] != state.get("quota_key")
        or binding[2] != state.get("quota_binding_sha256")
        or binding[3] != SAMPLE_EVIDENCE_REFERENCE
        or binding[4] != state.get("authorization_actor")
        or policy[0] != state.get("quota_key")
        or policy[1] != state.get("quota_policy_revision")
        or policy[2] != SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND
        or policy[3] != SAMPLE_QUOTA_CAPACITY_MICROTOKENS
        or policy[4] != state.get("quota_policy_sha256")
        or policy[5] != SAMPLE_EVIDENCE_REFERENCE
        or policy[6] != state.get("authorization_actor")
        or bucket[0] != state.get("quota_key")
        or bucket[2] != state.get("quota_policy_revision")
        or bucket[3] != 0
        or type(bucket[4]) is not int
        or bucket[4] < 0
        or (not allow_one_attempt and bucket[1] != SAMPLE_QUOTA_CAPACITY_MICROTOKENS)
    ):
        raise Csi500SampleCompositionError("source_gate_configuration_mismatch")
    if allow_one_attempt:
        attempt = attempts[0]
        if attempt[0] != 1 or attempt[1] != "RESERVED" or attempt[2] != state.get("grant_id"):
            raise Csi500SampleCompositionError("source_gate_attempt_mismatch")


def _read_migration_state(target: SampleTarget) -> SampleMigrationState:
    """Read actual source-gate schema state and prove warehouse tables are absent."""
    current = load_sample_target(target.directory)
    if current.identity != target.identity:
        raise Csi500SampleCompositionError("target_identity_changed")
    _require_source_gate_only_target(current)
    _assert_sample_database_identity(current)
    engine = None
    try:
        engine = create_engine(
            URL.create(
                "sqlite+pysqlite",
                database=f"file:{current.database_path}",
                query={"mode": "ro", "uri": "true"},
            ),
            poolclass=NullPool,
            connect_args={"timeout": 2.0},
        )
        from alembic_source_gate import schema_contract

        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA query_only=ON")
            connection.exec_driver_sql("BEGIN")
            tables = {
                str(row[0])
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type = 'table' "
                    "AND name NOT LIKE 'sqlite_%'"
                ).all()
            }
            expected_tables = {*_FRESH_SAMPLE_GATE_TABLES, SAMPLE_SOURCE_GATE_VERSION_TABLE}
            if tables != expected_tables:
                if "alembic_version_data" in tables or "alembic_version" in tables:
                    raise Csi500SampleCompositionError("warehouse_schema_must_remain_unapplied")
                raise Csi500SampleCompositionError("source_gate_schema_mismatch")
            views = connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'view' AND name NOT LIKE 'sqlite_%'"
            ).all()
            if views:
                raise Csi500SampleCompositionError("source_gate_schema_mismatch")
            source_gate_head = schema_contract.validate_migration_state(connection)
            if not isinstance(source_gate_head, str):
                raise Csi500SampleCompositionError("migration_marker_invalid")
            state = SampleMigrationState(
                warehouse_schema_status=SAMPLE_WAREHOUSE_SCHEMA_STATUS,
                source_gate_head=source_gate_head,
            )
            _assert_sample_database_identity(current)
            return state
    except Csi500SampleCompositionError:
        raise
    except Exception:
        raise Csi500SampleCompositionError("migration_state_unavailable") from None
    finally:
        if engine is not None:
            engine.dispose()


def _assert_sample_database_identity(target: SampleTarget) -> None:
    """Confirm the SQLite path still names this target without exposing paths."""
    try:
        assert_database_file_identity(target.database_path, target.identity)
    except Exception:
        raise Csi500SampleCompositionError("target_database_identity_changed") from None


def _connect_read_only(database_path: Path) -> sqlite3.Connection:
    """Open the exact file in SQLite read-only mode and recheck its inode."""
    current = database_path.lstat()
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise Csi500SampleCompositionError("target_database_identity_invalid")
    uri = f"file:{database_path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("PRAGMA database_list").fetchone()
        if row is None or Path(str(row[2])) != database_path:
            connection.close()
            raise Csi500SampleCompositionError("target_database_identity_invalid")
        after = database_path.lstat()
        if (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            connection.close()
            raise Csi500SampleCompositionError("target_database_identity_changed")
        return connection
    except Csi500SampleCompositionError:
        raise
    except Exception:
        raise Csi500SampleCompositionError("target_database_read_only_open_failed") from None


def _read_manifest(directory: Path) -> dict[str, object]:
    """Read a bounded no-follow manifest owned by the current user."""
    path = directory / SAMPLE_MANIFEST_FILENAME
    try:
        metadata = path.lstat()
        raw = _read_bounded_file(path, _MAX_STATE_BYTES)
    except Exception:
        raise Csi500SampleCompositionError("target_manifest_unavailable") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise Csi500SampleCompositionError("target_manifest_permissions_invalid")
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_unique_object)
    except Exception:
        raise Csi500SampleCompositionError("target_manifest_invalid") from None
    if (
        not isinstance(value, dict)
        or not set(value).issubset(_ALLOWED_MANIFEST_KEYS)
        or not _BASE_MANIFEST_KEYS.issubset(value)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") not in {1, 2}
        or not isinstance(value.get("target_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", value["target_id"]) is None
    ):
        raise Csi500SampleCompositionError("target_manifest_shape_invalid")
    if value["schema_version"] == 1:
        if "target_profile" in value or "warehouse_schema_status" in value:
            raise Csi500SampleCompositionError("target_manifest_shape_invalid")
    elif (
        value.get("target_profile") != SAMPLE_TARGET_PROFILE
        or value.get("warehouse_schema_status") != SAMPLE_WAREHOUSE_SCHEMA_STATUS
    ):
        raise Csi500SampleCompositionError("target_manifest_profile_invalid")
    return value


def _create_manifest_file(directory: Path, state: Mapping[str, object]) -> None:
    """Create the initial exclusive mode-0600 target manifest."""
    path = directory / SAMPLE_MANIFEST_FILENAME
    payload = _manifest_bytes(state)
    descriptor = os.open(
        path,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_directory(directory)


def _write_manifest_atomically(directory: Path, state: Mapping[str, object]) -> None:
    """Replace only the exact safe manifest using a same-directory temporary file."""
    current = _read_manifest(directory)
    path = directory / SAMPLE_MANIFEST_FILENAME
    payload = _manifest_bytes(state)
    temporary = directory / f".target-{uuid4().hex}.tmp"
    descriptor = os.open(
        temporary,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if current.get("target_id") != state.get("target_id"):
            raise Csi500SampleCompositionError("target_manifest_identity_changed")
        os.replace(temporary, path)
        _fsync_directory(directory)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def _manifest_bytes(state: Mapping[str, object]) -> bytes:
    """Serialize bounded, non-secret target state as canonical UTF-8 JSON."""
    if not set(state).issubset(_ALLOWED_MANIFEST_KEYS) or not _BASE_MANIFEST_KEYS.issubset(state):
        raise Csi500SampleCompositionError("target_state_field_invalid")
    try:
        payload = json.dumps(
            dict(state),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError):
        raise Csi500SampleCompositionError("target_state_value_invalid") from None
    if len(payload) > _MAX_STATE_BYTES:
        raise Csi500SampleCompositionError("target_manifest_too_large")
    return payload


def _read_bounded_file(path: Path, maximum: int) -> bytes:
    """Read one regular no-follow file descriptor under a fixed byte bound."""
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_NONBLOCK
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise Csi500SampleCompositionError("bounded_file_invalid")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(maximum + 1)
    except Csi500SampleCompositionError:
        raise
    except OSError:
        raise Csi500SampleCompositionError("bounded_file_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(raw) > maximum:
        raise Csi500SampleCompositionError("bounded_file_too_large")
    return raw


def _canonical_regular_file(value: object, *, max_bytes: int) -> Path:
    """Require an explicit absolute regular file with no symlink component."""
    if isinstance(value, Path):
        path = value
    elif isinstance(value, str) and value:
        path = Path(value)
    else:
        raise Csi500SampleCompositionError("evidence_path_invalid")
    if not path.is_absolute() or ".." in path.parts:
        raise Csi500SampleCompositionError("evidence_path_invalid")
    current = Path(path.anchor)
    try:
        for part in path.parts[1:]:
            current = current / part
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise Csi500SampleCompositionError("evidence_path_symlink")
        resolved = path.resolve(strict=True)
        metadata = path.lstat()
    except Csi500SampleCompositionError:
        raise
    except OSError:
        raise Csi500SampleCompositionError("evidence_file_unavailable") from None
    if resolved != path or not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
        raise Csi500SampleCompositionError("evidence_file_invalid")
    return path


def _validate_sample_quota_receipt(
    receipt: object,
    *,
    grant_id: str,
    principal_scope: str,
    expected_identity: DatabaseFileIdentity,
) -> dict[str, object]:
    """Accept only the frozen quota-provisioning receipt contract."""
    from opendata.data.source_gate_quota_provisioning import Csi500SampleQuotaReceipt

    if type(receipt) is not Csi500SampleQuotaReceipt:
        raise Csi500SampleCompositionError("quota_receipt_type_invalid")
    expected = {
        "source": SAMPLE_SOURCE,
        "grant_id": grant_id,
        "quota_key": SAMPLE_QUOTA_KEY,
        "rate_microtokens_per_second": SAMPLE_QUOTA_RATE_MICROTOKENS_PER_SECOND,
        "capacity_microtokens": SAMPLE_QUOTA_CAPACITY_MICROTOKENS,
    }
    for name, wanted in expected.items():
        if getattr(receipt, name, None) != wanted:
            raise Csi500SampleCompositionError("quota_receipt_scope_mismatch")
    if getattr(receipt, "status", None) != "PROVISIONED":
        raise Csi500SampleCompositionError("quota_receipt_not_committed")
    if (
        type(getattr(receipt, "policy_revision", None)) is not int
        or type(getattr(receipt, "rate_microtokens_per_second", None)) is not int
        or type(getattr(receipt, "capacity_microtokens", None)) is not int
    ):
        raise Csi500SampleCompositionError("quota_receipt_invalid")
    values: dict[str, object] = {
        "quota_key": getattr(receipt, "quota_key", None),
        "quota_policy_revision": getattr(receipt, "policy_revision", None),
        "quota_policy_sha256": getattr(receipt, "policy_sha256", None),
        "quota_binding_sha256": getattr(receipt, "binding_sha256", None),
        "quota_target_identity_sha256": getattr(receipt, "target_identity_sha256", None),
    }
    if (
        not isinstance(values["quota_key"], str)
        or _IDENTIFIER.fullmatch(values["quota_key"]) is None
        or values["quota_policy_revision"] != 1
        or any(
            _SHA256.fullmatch(str(values[key])) is None
            for key in (
                "quota_policy_sha256",
                "quota_binding_sha256",
                "quota_target_identity_sha256",
            )
        )
    ):
        raise Csi500SampleCompositionError("quota_receipt_invalid")
    expected_target_digest = _sample_identity_sha256(expected_identity)
    if values["quota_target_identity_sha256"] != expected_target_digest:
        raise Csi500SampleCompositionError("quota_receipt_target_identity_mismatch")
    del principal_scope  # The quota binding is source-wide; it does not bind a principal.
    return values


def _validate_sample_grant_result(
    result: object,
    *,
    grant_id: str,
    host: str,
    expires_at: datetime,
    actor: str,
) -> None:
    """Validate only the existing local SourceGrantProvisioner receipt shape."""
    status = getattr(result, "status", None)
    status_value = getattr(status, "value", status)
    if (
        status_value != "PROVISIONED"
        or getattr(result, "grant_id", None) != grant_id
        or getattr(result, "revision", None) != 1
        or getattr(result, "host", None) != host
        or getattr(result, "attested_by", None) != actor
        or getattr(result, "expires_at_us", None) is None
    ):
        raise Csi500SampleCompositionError("source_grant_receipt_invalid")
    expected_expiry_us = int(expires_at.timestamp() * 1_000_000)
    if abs(int(result.expires_at_us) - expected_expiry_us) > 1_000_000:
        raise Csi500SampleCompositionError("source_grant_expiry_mismatch")


def _bounded_identifier(value: object, label: str) -> str:
    """Validate one safe metadata identifier without rendering its value."""
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or _SECRET_LIKE.search(value) is not None
    ):
        raise Csi500SampleCompositionError(f"{label}_invalid")
    return value


def _bounded_source_reference(value: object) -> str:
    """Require a bounded evidence reference without embedded credentials."""
    if not isinstance(value, str) or not 1 <= len(value) <= 512 or value != value.strip():
        raise Csi500SampleCompositionError("calendar_source_reference_invalid")
    lowered = value.lower()
    if (
        any(token in lowered for token in ("api_key=", "token=", "password=", "secret="))
        or _SECRET_LIKE.search(value) is not None
    ):
        raise Csi500SampleCompositionError("calendar_source_reference_invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise Csi500SampleCompositionError("calendar_source_reference_invalid")
    return value


def _bounded_sha(value: object, label: str) -> str:
    """Validate one lowercase SHA-256 string."""
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise Csi500SampleCompositionError(f"{label}_invalid")
    return value


def _parse_calendar_date(value: object) -> date:
    """Parse one exact canonical ISO business date."""
    if not isinstance(value, str) or _CALENDAR_DATE.fullmatch(value) is None:
        raise Csi500SampleCompositionError("calendar_date_invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise Csi500SampleCompositionError("calendar_date_invalid") from None
    if parsed.isoformat() != value:
        raise Csi500SampleCompositionError("calendar_date_invalid")
    return parsed


def _parse_aware_timestamp(value: object) -> datetime:
    """Parse a timezone-aware ISO timestamp and normalize it to UTC."""
    if not isinstance(value, str) or len(value) > 40:
        raise Csi500SampleCompositionError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise Csi500SampleCompositionError("timestamp_invalid") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise Csi500SampleCompositionError("timestamp_invalid")
    return parsed.astimezone(timezone.utc)


def _format_aware_timestamp(value: datetime) -> str:
    """Serialize one aware timestamp at microsecond precision in UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise Csi500SampleCompositionError("timestamp_invalid")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _epoch_microseconds(value: datetime) -> int:
    """Convert a UTC-aware timestamp to the source gate's integer epoch unit."""
    normalized = value.astimezone(timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = normalized - epoch
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _sample_identity_sha256(identity: DatabaseFileIdentity) -> str:
    """Hash the quota owner's exact device/inode identity JSON contract."""
    payload = json.dumps(
        {"device": identity.device, "inode": identity.inode},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON keys in manifest and evidence files."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _safe_manifest_value(value: object) -> bool:
    """Accept only bounded JSON scalar values in target state updates."""
    if value is None or type(value) in {bool, int}:
        return True
    if isinstance(value, str):
        return (
            len(value) <= 2_048
            and _SECRET_LIKE.search(value) is None
            and not any(ord(character) < 32 for character in value)
        )
    return False


def _fsync_directory(directory: Path) -> None:
    """Persist one directory-entry update where the platform supports it."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now_text() -> str:
    """Format the current UTC instant without locale-dependent output."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _shanghai_today() -> date:
    """Return the current Shanghai business date without consulting a source."""
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


_BASE_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "target_id",
        "phase",
        "database_filename",
        "database_device",
        "database_inode",
        "owner_uid",
        "created_at_utc",
        "updated_at_utc",
    }
)


__all__ = [
    "Csi500SampleCompositionError",
    "Csi500TradingDayEvidence",
    "SAMPLE_DATASET_ID",
    "SAMPLE_SOURCE_GATE_MIGRATION_HEAD",
    "SAMPLE_SYMBOL",
    "SampleTarget",
    "create_sample_target",
    "load_csi500_trading_day_evidence",
    "load_sample_target",
    "lock_sample_target",
    "prepare_csi500_sample_one_shot_intent",
    "provision_sample_source_authorization",
    "transition_sample_target",
]
