"""Offline behavior tests for the persistent source-gate one-shot contract."""

from __future__ import annotations

import hashlib
import importlib.util
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from alembic_source_gate.schema_contract import (
    V2_OBJECT_SQL,
    V2_REVISION,
    V3_REVISION,
    V4_REVISION,
    validate_migration_state,
)
from opendata.data.source_attempt_gate import (
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceAttemptOneShotConflictError,
    SourceAttemptScopeV2,
    SourceAttemptScopeV3,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BASE_MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
HOST = "api.sina.example"
RIGHTS_REFERENCE = "offline-rights-record-1"
QUOTA_KEY = "sina-quota-v1"
NOW_US = 1_700_000_000_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999


def _load_migration(path: Path, module_name: str) -> ModuleType:
    """Import one repository-owned source-gate migration by exact local path."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_one_shot_migration() -> ModuleType:
    """Load the sg0004 revision without application settings or credentials."""
    path = (
        REPOSITORY_ROOT
        / "alembic_source_gate"
        / "versions"
        / "sg0004_source_attempt_one_shot_v4.py"
    )
    return _load_migration(path, "source_attempt_one_shot_v4_revision_test")


def _new_engine(tmp_path: Path, filename: str = "source-gate-one-shot.sqlite") -> Engine:
    """Create a test-owned persistent SQLite file with no inherited connections."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )


def _install_base(engine: Engine) -> None:
    """Install the pinned source-attempt base schema into a disposable file."""
    migration = _load_migration(BASE_MIGRATION_PATH, "source_attempt_one_shot_base_test")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def _install_v2(engine: Engine) -> None:
    """Install only the exact V2 sidecar for migration refusal tests."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate "
            "( version_num VARCHAR(32) NOT NULL, "
            "CONSTRAINT alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)",
            {"revision": V2_REVISION},
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)


def _install_v4(engine: Engine) -> None:
    """Run the additive V3 and V4 revisions against a clean V2 test database."""
    _install_v2(engine)
    v3_path = (
        REPOSITORY_ROOT / "alembic_source_gate" / "versions" / ("sg0003_source_attempt_scope_v3.py")
    )
    v3_migration = _load_migration(v3_path, "source_attempt_scope_v3_for_v4_test")
    v4_migration = _load_one_shot_migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            v3_migration.upgrade()
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V3_REVISION},
        )
        with Operations.context(MigrationContext.configure(connection)):
            v4_migration.upgrade()
        connection.exec_driver_sql(
            "UPDATE alembic_version_source_gate SET version_num = :revision",
            {"revision": V4_REVISION},
        )
        assert validate_migration_state(connection) == V4_REVISION


def _attempt(
    *,
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    attempt_no: int = 1,
    expected_reference: str | None = RIGHTS_REFERENCE,
) -> TransportAttempt:
    """Build one bounded synthetic attempt identity for offline tests."""
    return TransportAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=attempt_no,
        grant_id="grant-1",
        expected_revision=1,
        source="sina",
        canonical_model="financial-statement",
        operation="query",
        principal_scope="offline-worker",
        host=HOST,
        run_key="run-1",
        expected_rights_evidence_reference=expected_reference,
    )


def _scope(attempt: TransportAttempt | None = None) -> SourceAttemptScopeV3:
    """Return a canonical rights-bound request with synthetic policy pins."""
    return SourceAttemptScopeV3(
        attempt=attempt or _attempt(),
        request_scheme="https",
        request_method="GET",
        request_host=HOST,
        request_path="/v1/financial",
        capture_intent_id="intent-1",
        request_summary_sha256="a" * 64,
        source_policy_id="sina-financial-policy",
        source_policy_revision="rev-1",
        source_policy_sha256="b" * 64,
        budget_binding_sha256="c" * 64,
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256="d" * 64,
    )


def _scope_v2() -> SourceAttemptScopeV2:
    """Return a v2 scope used to verify legacy reservation rejection after lock."""
    attempt = replace(_attempt(expected_reference=None))
    return SourceAttemptScopeV2(
        attempt=attempt,
        capture_intent_id="intent-1",
        request_summary_sha256="a" * 64,
        source_policy_id="sina-financial-policy",
        source_policy_revision="rev-1",
        source_policy_sha256="b" * 64,
        budget_binding_sha256="c" * 64,
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256="d" * 64,
    )


def _seed_grant_and_quota(
    engine: Engine,
    scope: SourceAttemptScopeV3,
    *,
    include_grant: bool = True,
) -> None:
    """Insert exact quota pins and, unless omitted, one synthetic host-sealed grant."""
    evidence_sha256 = hashlib.sha256(b"synthetic evidence body").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota_key, 1, 1000000, 1000000, "
                "'offline:budget-policy', :digest, 'offline-test', 1)"
            ),
            {"quota_key": scope.budget_quota_key, "digest": scope.budget_policy_sha256},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota_key, 'offline:source-binding', "
                ":digest, 'offline-test', 1)"
            ),
            {
                "source": scope.attempt.source,
                "quota_key": scope.budget_quota_key,
                "digest": scope.budget_binding_sha256,
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota_key, 1000000, 0, :refill, 1)"
            ),
            {"quota_key": scope.budget_quota_key, "refill": NOW_US},
        )
        if not include_grant:
            return
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant_id, :source, :model, 'query', :principal, 'ALLOWED', 1, 0, :expires, "
                "'[]', :reference, :digest, 'Synthetic offline declaration.', "
                "'offline-test', 1, 1, 1)"
            ),
            {
                "grant_id": scope.attempt.grant_id,
                "source": scope.attempt.source,
                "model": scope.attempt.canonical_model,
                "principal": scope.attempt.principal_scope,
                "expires": MAX_TIMESTAMP_US,
                "reference": RIGHTS_REFERENCE,
                "digest": evidence_sha256,
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant_id, :host)"),
            {"grant_id": scope.attempt.grant_id, "host": scope.attempt.host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant_id, 1, 1, 1, 'offline-test', 'synthetic-test')"
            ),
            {"grant_id": scope.attempt.grant_id},
        )


@pytest.fixture
def gate_engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """Yield a V4-migrated file-backed source gate owned by one test."""
    engine = _new_engine(tmp_path)
    _install_base(engine)
    _install_v4(engine)
    yield engine
    engine.dispose()


def _lock_scope(
    gate: SourceAttemptGate,
    engine: Engine,
    scope: SourceAttemptScopeV3,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seed current synthetic authorization and lock the exact sample scope."""
    _seed_grant_and_quota(engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    gate.lock_one_shot_scope(scope)


def _counts(engine: Engine) -> tuple[int, int, int, int]:
    """Return permit, reservation, V2 scope, and V3 scope counts."""
    with engine.connect() as connection:
        return (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_one_shot_permits")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")
            ).scalar_one(),
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_scope_v3")
            ).scalar_one(),
        )


def _clock_and_bucket_snapshot(engine: Engine) -> tuple[int, int, int, int, int]:
    """Capture mutable clock and quota state to verify transaction rollback."""
    with engine.connect() as connection:
        clock = connection.execute(
            text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
        ).scalar_one()
        bucket = connection.execute(
            text(
                "SELECT tokens_micro, remainder, last_refill_us, policy_revision "
                "FROM od_source_gate_state WHERE quota_key = :key"
            ),
            {"key": QUOTA_KEY},
        ).one()
    return (clock, *bucket)


def test_v4_migration_adds_empty_permanent_one_shot_ledger(gate_engine: Engine) -> None:
    """The migration changes only the schema and refuses to drop its ledger."""
    with gate_engine.connect() as connection:
        assert validate_migration_state(connection) == V4_REVISION
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_configs")).scalar_one()
            == 0
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_permits")).scalar_one()
            == 0
        )
    migration = _load_one_shot_migration()
    with (
        gate_engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
        pytest.raises(RuntimeError, match="permanent"),
    ):
        migration.downgrade()


def test_v4_migration_rejects_v2_lineage_without_writing_schema(tmp_path: Path) -> None:
    """The V4 migration refuses a V2 marker before adding any one-shot objects."""
    engine = _new_engine(tmp_path, "source-gate-v2-only.sqlite")
    _install_base(engine)
    _install_v2(engine)
    migration = _load_one_shot_migration()
    with (
        engine.begin() as connection,
        Operations.context(MigrationContext.configure(connection)),
        pytest.raises(RuntimeError, match="exact v3"),
    ):
        migration.upgrade()
    with engine.connect() as connection:
        assert validate_migration_state(connection) == V2_REVISION
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'od_source_one_shot_%'"
            ).scalar_one()
            == 0
        )
    engine.dispose()


def test_locked_scope_reserves_once_and_replay_never_reissues_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One exact scope yields one new permit; retries only return non-permit statuses."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)

    first = gate.reserve_one_shot_attempt(scope)
    replay = gate.reserve_one_shot_attempt(scope)
    changed_id = replace(scope, attempt=replace(scope.attempt, attempt_id="attempt-2"))
    changed_request = replace(scope, attempt=replace(scope.attempt, request_id="request-2"))
    changed_intent = replace(scope, capture_intent_id="intent-2")
    changed_path = replace(scope, request_path="/v1/other")
    changed_query = replace(scope, request_summary_sha256="e" * 64)
    changed_source_policy = replace(scope, source_policy_sha256="f" * 64)
    changed_rights = replace(
        scope,
        attempt=replace(scope.attempt, expected_rights_evidence_reference="other-reference"),
    )
    changed_policy = replace(scope, budget_policy_sha256="f" * 64)

    assert first.status is ReservationStatus.RESERVED_NEW
    assert replay.status is ReservationStatus.DUPLICATE
    for changed in (
        changed_id,
        changed_request,
        changed_intent,
        changed_path,
        changed_query,
        changed_source_policy,
        changed_rights,
        changed_policy,
    ):
        assert gate.reserve_one_shot_attempt(changed).status is ReservationStatus.ID_CONFLICT
    assert _counts(gate_engine) == (1, 1, 0, 1)


def test_compat_v3_api_rejects_a_locked_scope_without_consuming_its_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compatibility V3 method cannot consume a scope sealed for one-shot use."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    before = _clock_and_bucket_snapshot(gate_engine)

    def unexpected_clock_advance(_connection: Connection) -> int:
        pytest.fail("the compatibility V3 method must reject before advancing the gate clock")

    monkeypatch.setattr(gate, "_advance_clock", unexpected_clock_advance)

    with pytest.raises(SourceAttemptOneShotConflictError, match="reserve_one_shot_attempt"):
        gate.reserve_attempt_v3(scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)
    assert _clock_and_bucket_snapshot(gate_engine) == before


def test_consumed_permit_without_exact_duplicate_fails_closed(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumed permit never becomes reusable when its exact tombstone is unreadable."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    assert gate.reserve_one_shot_attempt(scope).status is ReservationStatus.RESERVED_NEW
    monkeypatch.setattr(gate, "_find_existing_v3_attempt", lambda *_args: None)

    with pytest.raises(SourceAttemptGateUnavailable, match="history is incomplete"):
        gate.reserve_one_shot_attempt(scope)

    assert _counts(gate_engine) == (1, 1, 0, 1)


def test_exact_scope_lock_is_immutable_and_configuration_cannot_be_reopened(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked configuration cannot be changed, deleted, or locked again."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)

    with pytest.raises(SourceAttemptOneShotConflictError, match="already exists"):
        gate.lock_one_shot_scope(scope)
    with gate_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        with pytest.raises(sa.exc.IntegrityError, match="immutable"):
            connection.execute(
                text(
                    "UPDATE od_source_one_shot_configs SET locked_at_us = locked_at_us + 1 "
                    "WHERE config_id = 1"
                )
            )
        with pytest.raises(sa.exc.IntegrityError, match="permanent"):
            connection.execute(text("DELETE FROM od_source_one_shot_configs WHERE config_id = 1"))
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_lock_rejects_non_v3_scope_before_opening_a_write_transaction(gate_engine: Engine) -> None:
    """The one-shot lock API accepts only the exact scope type it persists."""
    gate = SourceAttemptGate(gate_engine)

    with pytest.raises(SourceAttemptInputError, match="SourceAttemptScopeV3"):
        gate.lock_one_shot_scope(object())  # type: ignore[arg-type]

    assert _counts(gate_engine) == (0, 0, 0, 0)


@pytest.mark.parametrize("freshness_drift", ("missing_grant", "extra_policy"))
def test_lock_requires_one_grant_and_one_policy_without_history(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freshness_drift: str,
) -> None:
    """A missing grant or extra quota policy keeps setup from locking a partial gate."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(
        gate_engine,
        scope,
        include_grant=freshness_drift != "missing_grant",
    )
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    if freshness_drift == "extra_policy":
        with gate_engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO od_source_gate_policies "
                    "(quota_key, policy_revision, rate_microtokens_per_second, "
                    "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                    "configured_at_us) VALUES ('extra-quota', 1, 1000000, 1000000, "
                    "'offline:extra-policy', :digest, 'offline-test', 1)"
                ),
                {"digest": "e" * 64},
            )

    with pytest.raises(SourceAttemptOneShotConflictError, match="empty history"):
        gate.lock_one_shot_scope(scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)
    with gate_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_configs")).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    "identity_mismatch",
    ("missing_grant", "revision", "source", "host", "rights_reference"),
)
def test_lock_rejects_identity_not_covered_by_the_active_grant(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    identity_mismatch: str,
) -> None:
    """No request identity may be locked unless the active grant covers it exactly."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    if identity_mismatch == "missing_grant":
        attempt = replace(scope.attempt, grant_id="unconfigured-grant")
        changed_scope = replace(scope, attempt=attempt)
    elif identity_mismatch == "revision":
        changed_scope = replace(
            scope,
            attempt=replace(scope.attempt, expected_revision=2),
        )
    elif identity_mismatch == "source":
        changed_scope = replace(scope, attempt=replace(scope.attempt, source="other-source"))
    elif identity_mismatch == "host":
        attempt = replace(scope.attempt, host="api.other.example")
        changed_scope = replace(scope, attempt=attempt, request_host=attempt.host)
    else:
        changed_scope = replace(
            scope,
            attempt=replace(
                scope.attempt,
                expected_rights_evidence_reference="different-rights-record",
            ),
        )

    with pytest.raises(SourceAttemptOneShotConflictError, match="exact active source grant"):
        gate.lock_one_shot_scope(changed_scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)
    with gate_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_configs")).scalar_one()
            == 0
        )


def test_lock_fails_closed_and_rolls_back_an_uncertain_clock_update(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error after clock advancement cannot leave a partially locked gate."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    before = _clock_and_bucket_snapshot(gate_engine)
    advance_clock = gate._advance_clock

    def fail_after_clock_write(connection: Connection) -> int:
        advance_clock(connection)
        raise SQLAlchemyError("synthetic clock acknowledgement failure")

    monkeypatch.setattr(gate, "_advance_clock", fail_after_clock_write)
    with pytest.raises(SourceAttemptGateUnavailable, match="configuration is unavailable"):
        gate.lock_one_shot_scope(scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)
    assert _clock_and_bucket_snapshot(gate_engine) == before


def test_lock_commit_uncertainty_returns_no_receipt_or_attempt_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persisted-but-unacknowledged configuration lock is not a returned permit."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    def commit_then_lose_acknowledgement(connection: Connection) -> None:
        connection.commit()
        raise SourceAttemptGateUnavailable("synthetic uncertain lock commit")

    monkeypatch.setattr(gate, "_commit", commit_then_lose_acknowledgement)
    with pytest.raises(SourceAttemptGateUnavailable, match="uncertain lock commit"):
        gate.lock_one_shot_scope(scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)
    with gate_engine.connect() as connection:
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_configs")).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_one_shot_permits")).scalar_one()
            == 0
        )


@pytest.mark.parametrize(
    ("config_corruption", "error_message"),
    (
        ("multiple_rows", "configuration row is ambiguous"),
        ("wrong_version", "record is malformed"),
        ("invalid_digest", "record is malformed"),
        ("invalid_unicode", "record is malformed"),
        ("digest_mismatch", "digest is invalid"),
        ("invalid_clock", "configuration clock is invalid"),
    ),
)
def test_config_reader_fails_closed_on_inconsistent_locked_rows(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    config_corruption: str,
    error_message: str,
) -> None:
    """The reader rejects malformed or ambiguous scope records before reuse."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    query_rows = gate._query_rows

    def corrupted_config_rows(
        connection: Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        rows = query_rows(connection, statement, parameters)
        if "SELECT * FROM od_source_one_shot_configs" not in statement:
            return rows
        if config_corruption == "multiple_rows":
            return [rows[0], rows[0]]

        corrupted = dict(rows[0])
        if config_corruption == "wrong_version":
            corrupted["scope_version"] = 2
        elif config_corruption == "invalid_digest":
            corrupted["scope_sha256"] = "z" * 64
        elif config_corruption == "invalid_unicode":
            corrupted["scope_json"] = "\ud800"
        elif config_corruption == "digest_mismatch":
            corrupted["scope_json"] = "{}"
        else:
            corrupted["locked_at_us"] = MAX_TIMESTAMP_US + 1
        return [corrupted]

    monkeypatch.setattr(gate, "_query_rows", corrupted_config_rows)
    with (
        gate_engine.connect() as connection,
        pytest.raises(SourceAttemptGateUnavailable, match=error_message),
    ):
        gate._load_one_shot_config(connection)


@pytest.mark.parametrize(
    ("config_object", "error_message"),
    (
        ("missing", "one-shot v4 configuration table is missing"),
        ("view", "one-shot configuration object is ambiguous"),
    ),
)
def test_legacy_reservation_fails_closed_when_v4_config_object_is_not_a_table(
    gate_engine: Engine,
    config_object: str,
    error_message: str,
) -> None:
    """A V4 marker never lets a legacy caller proceed without the real config table."""
    with gate_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE od_source_one_shot_configs")
        if config_object == "view":
            connection.exec_driver_sql(
                "CREATE VIEW od_source_one_shot_configs AS SELECT 1 AS config_id"
            )

    gate = SourceAttemptGate(gate_engine)
    with pytest.raises(SourceAttemptGateUnavailable, match=error_message):
        gate.reserve_attempt(_attempt())

    assert _counts(gate_engine) == (0, 0, 0, 0)


@pytest.mark.parametrize("stored_scope_json", (None, "\ud800"), ids=("missing", "invalid-utf8"))
def test_scope_match_fails_closed_on_unreadable_locked_scope_json(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    stored_scope_json: str | None,
) -> None:
    """Malformed stored canonical JSON becomes an identity conflict, never a permit."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    monkeypatch.setattr(
        gate,
        "_load_one_shot_config",
        lambda _connection: {"scope_json": stored_scope_json},
    )

    result = gate.reserve_one_shot_attempt(scope)

    assert result.status is ReservationStatus.ID_CONFLICT
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_lock_rejects_a_scope_when_the_host_seal_cannot_be_confirmed(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed exact host-seal check aborts before persisting any configuration."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    monkeypatch.setattr(gate, "_grant_seal_event_matches", lambda *_args: False)

    with pytest.raises(SourceAttemptOneShotConflictError, match="immutable host-seal event"):
        gate.lock_one_shot_scope(scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)


@pytest.mark.parametrize("quota_drift", ("binding_digest", "bucket_revision"))
def test_lock_rejects_source_binding_or_bucket_revision_drift(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    quota_drift: str,
) -> None:
    """The lock compares both immutable source binding and current bucket revision pins."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    if quota_drift == "binding_digest":
        changed_scope = replace(scope, budget_binding_sha256="f" * 64)
        expected_error = "source binding"
    else:
        changed_scope = scope
        expected_error = "valid bucket"
        with gate_engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE od_source_gate_state SET policy_revision = 2 "
                    "WHERE quota_key = :quota_key"
                ),
                {"quota_key": scope.budget_quota_key},
            )

    with pytest.raises(SourceAttemptOneShotConflictError, match=expected_error):
        gate.lock_one_shot_scope(changed_scope)

    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_insert_or_replace_cannot_reopen_one_shot_or_attempt_rows(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BEFORE INSERT guards close SQLite REPLACE's DELETE-trigger bypass."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    with gate_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA recursive_triggers").scalar_one() == 0

    with (
        pytest.raises(sa.exc.IntegrityError, match="one-shot configuration requires"),
        gate_engine.begin() as connection,
    ):
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO od_source_one_shot_configs "
            "SELECT * FROM od_source_one_shot_configs WHERE config_id = 1"
        )
    assert _counts(gate_engine) == (0, 0, 0, 0)

    assert gate.reserve_one_shot_attempt(scope).status is ReservationStatus.RESERVED_NEW
    replacements = (
        (
            "INSERT OR REPLACE INTO od_source_one_shot_permits "
            "SELECT * FROM od_source_one_shot_permits WHERE config_id = 1",
            "one-shot permit does not match",
        ),
        (
            "INSERT OR REPLACE INTO od_source_attempt_reservations "
            "SELECT * FROM od_source_attempt_reservations WHERE attempt_id = 'attempt-1'",
            "requires the locked one-shot permit",
        ),
        (
            "INSERT OR REPLACE INTO od_source_attempt_scope_v3 "
            "SELECT * FROM od_source_attempt_scope_v3 WHERE attempt_id = 'attempt-1'",
            "v3 source scope requires",
        ),
    )
    for statement, error in replacements:
        with (
            pytest.raises(sa.exc.IntegrityError, match=error),
            gate_engine.begin() as connection,
        ):
            connection.exec_driver_sql(statement)
        assert _counts(gate_engine) == (1, 1, 0, 1)


def test_denied_exact_scope_consumes_the_only_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grant revoked after lock yields a durable denial that cannot be retried."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    revoked = gate.revoke_grant(
        scope.attempt.grant_id,
        expected_revision=1,
        actor="offline-test",
        reason_code="test-revocation",
    )

    assert revoked.revision == 2
    denied = gate.reserve_one_shot_attempt(scope)
    replay = gate.reserve_one_shot_attempt(scope)

    assert denied.status is ReservationStatus.DENIED
    assert denied.denial_code == "grant_not_allowed"
    assert replay.status is ReservationStatus.DUPLICATE
    assert _counts(gate_engine) == (1, 1, 0, 1)


def test_v1_and_v2_runtime_paths_and_direct_sql_are_blocked_after_lock(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy APIs fail before writes and schema triggers block direct old-path inserts."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)

    with pytest.raises(SourceAttemptGateUnavailable, match="rejects v1 and v2"):
        gate.reserve_attempt(replace(scope.attempt, expected_rights_evidence_reference=None))
    with pytest.raises(SourceAttemptGateUnavailable, match="rejects v1 and v2"):
        gate.reserve_attempt_v2(_scope_v2())

    attempt = scope.attempt
    with gate_engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys = ON")
        with pytest.raises(sa.exc.IntegrityError, match="one-shot permit"):
            connection.execute(
                text(
                    "INSERT INTO od_source_attempt_reservations "
                    "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
                    "canonical_model, operation, principal_scope, host, run_key, outcome, "
                    "denial_code, retry_after_us, decided_at_us, reserved_at_us, quota_key, "
                    "policy_revision, rate_microtokens_per_second, capacity_microtokens) VALUES "
                    "('legacy-attempt', 'legacy-request', 1, :grant_id, 1, :source, :model, "
                    "'query', :principal, :host, 'legacy-run', 'DENIED', 'grant_not_found', "
                    "NULL, 1, NULL, NULL, NULL, NULL, NULL)"
                ),
                {
                    "grant_id": attempt.grant_id,
                    "source": attempt.source,
                    "model": attempt.canonical_model,
                    "principal": attempt.principal_scope,
                    "host": attempt.host,
                },
            )
        with pytest.raises(sa.exc.IntegrityError, match="v2 source attempts"):
            connection.execute(
                text(
                    "INSERT INTO od_source_attempt_scope_v2 "
                    "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
                    "VALUES ('legacy-attempt', 2, '{}', :digest, 1)"
                ),
                {"digest": "a" * 64},
            )
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_new_grant_cannot_be_appended_after_lock(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database trigger prevents a trusted legacy grant path reopening scope."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    with (
        pytest.raises(sa.exc.IntegrityError, match="new grants are blocked"),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, "
                "operator_attestation_note, attested_by, attested_at_us, created_at_us, "
                "updated_at_us) VALUES ('grant-2', 'sina', 'financial-statement', 'query', "
                "'offline-worker', 'ALLOWED', 1, 0, :expires, '[]', :reference, :digest, "
                "'Synthetic declaration.', 'offline-test', 1, 1, 1)"
            ),
            {
                "expires": MAX_TIMESTAMP_US,
                "reference": RIGHTS_REFERENCE,
                "digest": hashlib.sha256(b"synthetic second evidence").hexdigest(),
            },
        )
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_quota_policy_and_source_binding_are_frozen_after_lock(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persisted scope pins cannot be reopened through quota setup SQL."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)

    mutations = (
        (
            "UPDATE od_source_gate_policies SET policy_revision = policy_revision + 1, "
            "policy_reference = 'offline:changed' "
            "WHERE quota_key = :key",
            {"key": QUOTA_KEY},
            "quota policy is locked",
        ),
        (
            "INSERT INTO od_source_gate_source_bindings "
            "(source, quota_key, binding_reference, binding_sha256, configured_by, "
            "configured_at_us) VALUES ('other-source', :key, 'offline:new-binding', "
            ":digest, 'offline-test', 1)",
            {"key": QUOTA_KEY, "digest": "f" * 64},
            "source binding is locked",
        ),
    )
    for statement, parameters, error in mutations:
        with pytest.raises(sa.exc.IntegrityError, match=error), gate_engine.begin() as connection:
            connection.execute(text(statement), parameters)


def test_concurrent_replay_serializes_to_one_reserved_result(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two same-scope writers serialize and only one receives RESERVED_NEW."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(gate.reserve_one_shot_attempt, (scope, scope)))

    statuses = sorted(result.status.value for result in results)
    assert statuses == sorted(
        (ReservationStatus.RESERVED_NEW.value, ReservationStatus.DUPLICATE.value)
    )
    assert _counts(gate_engine) == (1, 1, 0, 1)


def test_commit_acknowledgement_uncertainty_never_reissues_a_consumed_permit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed-but-unacknowledged permit is visible as a non-permit replay."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    original_commit = gate._commit

    def commit_then_raise(connection: Connection) -> None:
        """Model the database committing before its acknowledgement is lost."""
        connection.commit()
        raise SourceAttemptGateUnavailable("synthetic uncertain commit")

    monkeypatch.setattr(gate, "_commit", commit_then_raise)
    with pytest.raises(SourceAttemptGateUnavailable, match="uncertain"):
        gate.reserve_one_shot_attempt(scope)
    assert _counts(gate_engine) == (1, 1, 0, 1)

    monkeypatch.setattr(gate, "_commit", original_commit)
    assert gate.reserve_one_shot_attempt(scope).status is ReservationStatus.DUPLICATE
    assert _counts(gate_engine) == (1, 1, 0, 1)


def test_scope_sidecar_failure_rolls_back_the_permit_and_reservation(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late transactional failure leaves no permit that could authorize send."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    before = _clock_and_bucket_snapshot(gate_engine)
    insert_scope = gate._insert_attempt_scope_v3

    def fail_after_scope_insert(*args: object) -> None:
        """Fail after writing the sidecar to exercise the outer transaction rollback."""
        insert_scope(*args)  # type: ignore[arg-type]
        raise SQLAlchemyError("synthetic scope acknowledgement failure")

    monkeypatch.setattr(gate, "_insert_attempt_scope_v3", fail_after_scope_insert)
    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_one_shot_attempt(scope)
    assert _counts(gate_engine) == (0, 0, 0, 0)
    assert _clock_and_bucket_snapshot(gate_engine) == before


def test_lock_refuses_any_prior_attempt_history(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V4 remains opt-in and cannot be retrofitted onto an attempted gate."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    assert gate.reserve_attempt(scope.attempt).status is ReservationStatus.RESERVED_NEW

    with pytest.raises(SourceAttemptOneShotConflictError, match="empty history"):
        gate.lock_one_shot_scope(scope)
    assert _counts(gate_engine) == (0, 1, 0, 0)


def test_lock_refuses_nonfresh_history_and_policy_mismatch(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock requires empty attempts and matching persisted binding/policy pins."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_grant_and_quota(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    with gate_engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE od_source_gate_policies SET policy_revision = 2, policy_sha256 = :digest "
            "WHERE quota_key = :key",
            {"digest": "e" * 64, "key": QUOTA_KEY},
        )
        connection.exec_driver_sql(
            "UPDATE od_source_gate_state SET policy_revision = 2 WHERE quota_key = :key",
            {"key": QUOTA_KEY},
        )

    with pytest.raises(SourceAttemptOneShotConflictError, match="policy"):
        gate.lock_one_shot_scope(scope)
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_reservation_decision_time_must_equal_permit_consumption_time(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database guard binds the consumed permit to the reservation timestamp."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _lock_scope(gate, gate_engine, scope, monkeypatch)
    consumed_at = NOW_US + 1

    with (
        pytest.raises(sa.exc.IntegrityError, match="one-shot permit"),
        gate_engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO od_source_one_shot_permits "
                "(config_id, attempt_id, capture_intent_id, scope_sha256, consumed_at_us) "
                "SELECT config_id, attempt_id, capture_intent_id, scope_sha256, "
                ":consumed_at FROM od_source_one_shot_configs"
            ),
            {"consumed_at": consumed_at},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_attempt_reservations "
                "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
                "canonical_model, operation, principal_scope, host, run_key, outcome, "
                "denial_code, retry_after_us, decided_at_us, reserved_at_us, quota_key, "
                "policy_revision, rate_microtokens_per_second, capacity_microtokens) VALUES "
                "('attempt-1', 'request-1', 1, 'grant-1', 1, 'sina', "
                "'financial-statement', 'query', 'offline-worker', :host, 'run-1', "
                "'DENIED', 'grant_not_found', NULL, :decided_at, NULL, NULL, NULL, NULL, NULL)"
            ),
            {"host": HOST, "decided_at": consumed_at + 1},
        )
    assert _counts(gate_engine) == (0, 0, 0, 0)


def test_v4_schema_drift_fails_closed_before_lock_or_attempt(gate_engine: Engine) -> None:
    """A missing one-shot enforcement trigger invalidates the entire V4 schema."""
    with gate_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER trg_od_source_one_shot_reservation_permit")
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()

    with pytest.raises(SourceAttemptGateUnavailable, match="v4 schema"):
        gate.lock_one_shot_scope(scope)
    assert _counts(gate_engine) == (0, 0, 0, 0)
