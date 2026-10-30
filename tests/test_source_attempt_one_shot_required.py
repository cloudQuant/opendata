"""Offline V4 locked-reservation behavior for one-shot source attempts."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from opendata.data import source_attempt_gate as source_attempt_gate_module
from opendata.data.source_attempt_gate import (
    AttemptLookupReasonCode,
    OneShotScopeInspectionStatus,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceAttemptOneShotConflictError,
    SourceAttemptScopeV3,
    TransportAttempt,
)
from opendata.data.source_grant_provisioning import (
    SourceGrantProvisioner,
    SourceGrantProvisionRequest,
)
from scripts import migrate_source_gate

if TYPE_CHECKING:
    from collections.abc import Generator

    from sqlalchemy.engine import Engine


_ROOT = Path(__file__).resolve().parents[1]
_BASE_MIGRATION = _ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
_MIGRATION_NAME = "one_shot_required_gate_test"
_SOURCE = "synthetic-source"
_MODEL = "synthetic-daily-bars"
_OPERATION = "query"
_PRINCIPAL = "synthetic-one-shot-worker"
_GRANT_ID = "synthetic-one-shot-grant"
_HOST = "marketdata.example.test"
_QUOTA_KEY = "synthetic-one-shot-quota"
_RIGHTS_REFERENCE = "synthetic:test-rights-reference"
_POLICY_SHA256 = hashlib.sha256(b"synthetic policy").hexdigest()
_BINDING_SHA256 = hashlib.sha256(b"synthetic binding").hexdigest()
_SOURCE_POLICY_SHA256 = hashlib.sha256(b"synthetic source policy").hexdigest()


def _install_v4_schema(engine: Engine) -> None:
    """Create an isolated file-backed gate and upgrade it through its V4 head."""
    spec = importlib.util.spec_from_file_location(_MIGRATION_NAME, _BASE_MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    database_path = engine.url.database
    if not database_path:
        raise AssertionError("one-shot tests require a file-backed SQLite database")
    assert migrate_source_gate.main(["--database-path", database_path]) == 0


def _seed_quota(engine: Engine) -> None:
    """Seed one synthetic binding and policy; this is not supplier authorization."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1000000, 5000000, "
                "'synthetic:test-policy', :digest, 'offline-test', 1)"
            ),
            {"quota": _QUOTA_KEY, "digest": _POLICY_SHA256},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, 'synthetic:test-binding', "
                ":digest, 'offline-test', 1)"
            ),
            {"source": _SOURCE, "quota": _QUOTA_KEY, "digest": _BINDING_SHA256},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, 5000000, 0, 1, 1)"
            ),
            {"quota": _QUOTA_KEY},
        )


def _provision_synthetic_grant(gate: SourceAttemptGate) -> None:
    """Add one short-lived, synthetic grant so the fresh-gate lock can be tested."""
    now = datetime.now(timezone.utc)
    SourceGrantProvisioner(gate).provision(
        SourceGrantProvisionRequest(
            grant_id=_GRANT_ID,
            revision=1,
            source=_SOURCE,
            canonical_model=_MODEL,
            operation=_OPERATION,
            principal_scope=_PRINCIPAL,
            host=_HOST,
            not_before=now - timedelta(minutes=1),
            expires_at=now + timedelta(hours=1),
            rights_evidence_reference=_RIGHTS_REFERENCE,
            rights_evidence_sha256=hashlib.sha256(b"synthetic evidence metadata").hexdigest(),
            operator_confirms_authorized_use=True,
            operator_attestation_note=(
                "Synthetic offline fixture; supplier rights are not asserted."
            ),
            attested_by="offline-test-operator",
        )
    )


def _scope(*, attempt_id: str = "synthetic-attempt-001") -> SourceAttemptScopeV3:
    """Return a deterministic V3 scope with no credentials or external data."""
    return SourceAttemptScopeV3(
        attempt=TransportAttempt(
            attempt_id=attempt_id,
            request_id="synthetic-request-001",
            attempt_no=1,
            grant_id=_GRANT_ID,
            expected_revision=1,
            source=_SOURCE,
            canonical_model=_MODEL,
            operation=_OPERATION,
            principal_scope=_PRINCIPAL,
            host=_HOST,
            run_key="synthetic-one-shot-run",
            expected_rights_evidence_reference=_RIGHTS_REFERENCE,
        ),
        request_scheme="https",
        request_method="GET",
        request_host=_HOST,
        request_path="/api/a-share-index/prices/historical",
        capture_intent_id="synthetic-capture-intent-001",
        request_summary_sha256=hashlib.sha256(b"synthetic query summary").hexdigest(),
        source_policy_id="synthetic-source-policy",
        source_policy_revision="synthetic-revision-1",
        source_policy_sha256=_SOURCE_POLICY_SHA256,
        budget_binding_sha256=_BINDING_SHA256,
        budget_quota_key=_QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256=_POLICY_SHA256,
    )


@pytest.fixture
def configured_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Provide a fresh V4 SQLite gate with one synthetic grant and quota."""
    engine = sa.create_engine(
        f"sqlite:///{tmp_path / 'source-attempt-one-shot.sqlite'}",
        connect_args={"timeout": 10},
        poolclass=NullPool,
    )
    try:
        _install_v4_schema(engine)
        gate = SourceAttemptGate(engine)
        _seed_quota(engine)
        _provision_synthetic_grant(gate)
        yield engine, gate
    finally:
        engine.dispose()


def _table_count(engine: Engine, table_name: str) -> int:
    """Read one fixed internal row count from the temporary test database."""
    statements = {
        "od_source_attempt_reservations": text(
            "SELECT COUNT(*) FROM od_source_attempt_reservations"
        ),
        "od_source_attempt_scope_v3": text("SELECT COUNT(*) FROM od_source_attempt_scope_v3"),
        "od_source_one_shot_configs": text("SELECT COUNT(*) FROM od_source_one_shot_configs"),
        "od_source_one_shot_permits": text("SELECT COUNT(*) FROM od_source_one_shot_permits"),
    }
    statement = statements.get(table_name)
    if statement is None:
        raise AssertionError("test attempted to inspect an unexpected table")
    with engine.connect() as connection:
        row = connection.execute(statement).scalar_one()
        return int(row)


def _reservation_rows(engine: Engine) -> list[dict[str, object]]:
    """Read the local attempt tombstone from this test's temporary gate only."""
    with engine.connect() as connection:
        result = connection.execute(
            text(
                "SELECT outcome, denial_code, decided_at_us, reserved_at_us "
                "FROM od_source_attempt_reservations"
            )
        ).mappings()
        return [dict(row) for row in result]


def _gate_write_snapshot(engine: Engine) -> tuple[int, int, int, int, int]:
    """Capture the gate clock and configured bucket before a rejected operation."""
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT clock.highwater_us, state.tokens_micro, state.remainder, "
                "state.last_refill_us, state.policy_revision "
                "FROM od_source_gate_clock AS clock "
                "JOIN od_source_gate_state AS state ON state.quota_key = :quota "
                "WHERE clock.clock_id = 1"
            ),
            {"quota": _QUOTA_KEY},
        ).one()
        return (int(row[0]), int(row[1]), int(row[2]), int(row[3]), int(row[4]))


def test_public_one_shot_apis_reject_wrong_scope_before_opening_gate(
    configured_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed strict API inputs fail before either writer or reader connection opens."""
    _engine, gate = configured_gate

    def unexpected_database_access() -> None:
        pytest.fail("invalid one-shot scope accessed the database")

    monkeypatch.setattr(gate, "_open_write_connection", unexpected_database_access)
    monkeypatch.setattr(gate, "_open_readonly_connection", unexpected_database_access)

    with pytest.raises(SourceAttemptInputError):
        gate.reserve_one_shot_attempt(None)  # type: ignore[arg-type]
    with pytest.raises(SourceAttemptInputError):
        gate.inspect_one_shot_scope(None)  # type: ignore[arg-type]


def test_required_api_fails_without_lock_while_legacy_v3_api_stays_compatible(
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """The strict entry point refuses an unlocked gate without disabling V3 callers."""
    engine, gate = configured_gate
    scope = _scope()

    assert gate.inspect_one_shot_scope(scope).status is OneShotScopeInspectionStatus.UNCONFIGURED
    with pytest.raises(SourceAttemptOneShotConflictError):
        gate.reserve_one_shot_attempt(scope)

    assert _table_count(engine, "od_source_one_shot_permits") == 0
    assert _table_count(engine, "od_source_attempt_reservations") == 0
    assert _table_count(engine, "od_source_attempt_scope_v3") == 0

    legacy_result = gate.reserve_attempt_v3(scope)
    assert legacy_result.status is ReservationStatus.RESERVED_NEW
    assert _table_count(engine, "od_source_one_shot_permits") == 0


def test_required_api_rejects_non_v4_marker_without_writing_any_permit(
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A V3 marker under V4 objects is not accepted as a one-shot schema."""
    engine, gate = configured_gate
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version_source_gate SET version_num = :revision"),
            {"revision": "sg0003_source_attempt_scope_v3"},
        )

    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_one_shot_attempt(_scope())

    assert _table_count(engine, "od_source_one_shot_permits") == 0
    assert _table_count(engine, "od_source_attempt_reservations") == 0
    inspection = gate.inspect_one_shot_scope(_scope())
    assert inspection.status is OneShotScopeInspectionStatus.INDETERMINATE


def test_exact_scope_is_reserved_once_and_drift_never_consumes_another_permit(
    configured_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching lock permits one write tuple; drift and replay stay non-sendable."""
    engine, gate = configured_gate
    scope = _scope()
    gate.lock_one_shot_scope(scope)
    assert gate.inspect_one_shot_scope(scope).status is OneShotScopeInspectionStatus.UNUSED

    before_legacy_call = _gate_write_snapshot(engine)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            gate,
            "_database_now_us",
            lambda _connection: before_legacy_call[0] + 1,
        )
        with pytest.raises(SourceAttemptOneShotConflictError):
            gate.reserve_attempt_v3(scope)
    assert _gate_write_snapshot(engine) == before_legacy_call
    assert _table_count(engine, "od_source_one_shot_permits") == 0
    assert _table_count(engine, "od_source_attempt_reservations") == 0
    assert _table_count(engine, "od_source_attempt_scope_v3") == 0

    changed = replace(
        scope,
        request_summary_sha256=hashlib.sha256(b"changed query summary").hexdigest(),
    )
    drifted = gate.reserve_one_shot_attempt(changed)
    assert drifted.status is ReservationStatus.ID_CONFLICT
    assert _table_count(engine, "od_source_one_shot_permits") == 0
    assert _table_count(engine, "od_source_attempt_reservations") == 0

    first = gate.reserve_one_shot_attempt(scope)
    assert first.status is ReservationStatus.RESERVED_NEW
    assert _table_count(engine, "od_source_one_shot_permits") == 1
    assert _table_count(engine, "od_source_attempt_reservations") == 1
    assert _table_count(engine, "od_source_attempt_scope_v3") == 1
    assert gate.inspect_one_shot_scope(scope).status is OneShotScopeInspectionStatus.CONSUMED

    replay = gate.reserve_one_shot_attempt(scope)
    changed_after_consumption = gate.reserve_one_shot_attempt(changed)
    assert replay.status is ReservationStatus.DUPLICATE
    assert changed_after_consumption.status is ReservationStatus.ID_CONFLICT
    assert _table_count(engine, "od_source_one_shot_permits") == 1
    assert _table_count(engine, "od_source_attempt_reservations") == 1
    assert _table_count(engine, "od_source_attempt_scope_v3") == 1
    assert _reservation_rows(engine)[0]["outcome"] == "RESERVED"


def test_concurrent_exact_calls_return_at_most_one_new_reservation(
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """SQLite's immediate writer transaction serializes concurrent one-shot callers."""
    engine, gate = configured_gate
    scope = _scope()
    gate.lock_one_shot_scope(scope)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(gate.reserve_one_shot_attempt, (scope, scope)))

    assert sorted(result.status.value for result in results) == ["DUPLICATE", "RESERVED_NEW"]
    assert _table_count(engine, "od_source_one_shot_permits") == 1
    assert _table_count(engine, "od_source_attempt_reservations") == 1
    assert _table_count(engine, "od_source_attempt_scope_v3") == 1


def test_uncertain_commit_never_reissues_a_consumed_reservation(
    configured_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost acknowledgement remains consumed and a later call is duplicate only."""
    engine, gate = configured_gate
    scope = _scope()
    gate.lock_one_shot_scope(scope)

    def commit_then_lose_ack(connection: sa.engine.Connection) -> None:
        connection.commit()
        raise SourceAttemptGateUnavailable("synthetic commit acknowledgement loss")

    with monkeypatch.context() as patcher:
        patcher.setattr(gate, "_commit", commit_then_lose_ack)
        with pytest.raises(SourceAttemptGateUnavailable):
            gate.reserve_one_shot_attempt(scope)

    assert gate.inspect_one_shot_scope(scope).status is OneShotScopeInspectionStatus.CONSUMED
    replay = gate.reserve_one_shot_attempt(scope)
    assert replay.status is ReservationStatus.DUPLICATE
    assert _table_count(engine, "od_source_one_shot_permits") == 1
    assert _table_count(engine, "od_source_attempt_reservations") == 1


def test_inspection_scope_mismatch_is_bounded_and_not_a_recovery_permit(
    configured_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Read-only inspection reports only finite state and never returns reservation data."""
    _engine, gate = configured_gate
    locked_scope = _scope()
    gate.lock_one_shot_scope(locked_scope)
    other_scope = replace(
        locked_scope,
        request_summary_sha256=hashlib.sha256(b"different query").hexdigest(),
    )

    result = gate.inspect_one_shot_scope(other_scope)

    assert result.status is OneShotScopeInspectionStatus.SCOPE_MISMATCH
    assert result.reason_code is None
    assert not hasattr(result, "attempt_id")
    assert not hasattr(result, "scope_json")
    assert not hasattr(result, "reservation")


@pytest.mark.parametrize(
    ("error_type", "expected_reason"),
    [
        (sqlite3.OperationalError, AttemptLookupReasonCode.DATABASE_UNAVAILABLE),
        (SourceAttemptGateUnavailable, AttemptLookupReasonCode.RECORD_CORRUPT),
    ],
    ids=["storage-read-error", "invalid-record"],
)
def test_inspection_separates_storage_errors_from_corrupt_history(
    configured_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
    expected_reason: AttemptLookupReasonCode,
) -> None:
    """Read failures and malformed consumed history remain indeterminate and distinct."""
    _engine, gate = configured_gate
    scope = _scope()
    gate.lock_one_shot_scope(scope)

    def fail_consumed_state(_connection: object, _config: object) -> bool:
        raise error_type("synthetic private diagnostic")

    monkeypatch.setattr(gate, "_one_shot_consumed_state", fail_consumed_state)
    result = gate.inspect_one_shot_scope(scope)

    assert result.status is OneShotScopeInspectionStatus.INDETERMINATE
    assert result.reason_code is expected_reason
    assert "OneShotScopeInspection" in source_attempt_gate_module.__all__
    assert "OneShotScopeInspectionStatus" in source_attempt_gate_module.__all__
