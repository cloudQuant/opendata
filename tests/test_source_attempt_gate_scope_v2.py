"""Offline contract tests for exact source-attempt scope v2 behavior."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

from opendata.data.source_attempt_gate import (
    AttemptLookupAbsent,
    AttemptLookupDenied,
    AttemptLookupIndeterminate,
    AttemptLookupReasonCode,
    AttemptLookupReserved,
    GrantRevocationStatus,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptGateConfigurationError,
    SourceAttemptGateUnavailable,
    SourceAttemptInputError,
    SourceAttemptScopeV2,
    TransportAttempt,
    _bounded_int,
    _configured_database_path,
    _normalize_host,
    _refill_bucket,
    _scope_v2_bytes,
    _sqlite_parenthesized_content,
    _stat_database_file,
    _validate_identifier,
)

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "source_attempt_gate_v2_contract_0015_test"
SOURCE = "sina"
MODEL = "financial-statement"
PRINCIPAL = "offline-worker"
HOST = "api.sina.example"
QUOTA_KEY = "sina-quota-v1"
NOW_US = 1_700_000_000_000_000
MAX_TIMESTAMP_US = 253_402_300_799_999_999


def _load_base_migration() -> ModuleType:
    """Load only the existing base schema migration, without its application env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path, filename: str = "gate-v2.sqlite") -> Engine:
    """Create an explicitly owned, file-backed SQLite test database."""
    return sa.create_engine(
        f"sqlite:///{tmp_path / filename}",
        connect_args={"timeout": 5},
        poolclass=NullPool,
    )


def _apply_base_schema(engine: Engine) -> None:
    """Install the v1 schema only in the test-owned temporary SQLite file."""
    migration = _load_base_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def _install_v2_schema(engine: Engine) -> None:
    """Install the frozen v2 marker/sidecar SQL contract in a temporary database."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE alembic_version_source_gate "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO alembic_version_source_gate (version_num) "
                "VALUES ('sg0002_source_attempt_scope_v2')"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE od_source_attempt_scope_v2 ("
                "attempt_id TEXT NOT NULL PRIMARY KEY, "
                "scope_version INTEGER NOT NULL CHECK (scope_version = 2), "
                "scope_json TEXT NOT NULL CHECK "
                "(length(CAST(scope_json AS BLOB)) BETWEEN 1 AND 16384), "
                "scope_sha256 TEXT NOT NULL CHECK "
                "(length(scope_sha256) = 64 AND "
                "scope_sha256 NOT GLOB '*[^0-9a-f]*'), "
                "recorded_at_us INTEGER NOT NULL, "
                "FOREIGN KEY (attempt_id) REFERENCES "
                "od_source_attempt_reservations(attempt_id) ON DELETE RESTRICT)"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_update "
                "BEFORE UPDATE ON od_source_attempt_scope_v2 BEGIN "
                "SELECT RAISE(ABORT, 'source attempt scope v2 rows are immutable'); END"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_delete "
                "BEFORE DELETE ON od_source_attempt_scope_v2 BEGIN "
                "SELECT RAISE(ABORT, 'source attempt scope v2 rows are permanent'); END"
            )
        )


def _attempt(
    *,
    attempt_id: str = "attempt-1",
    request_id: str = "request-1",
    attempt_no: int = 1,
) -> TransportAttempt:
    """Build a synthetic transport attempt with a durable run identity."""
    return TransportAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=attempt_no,
        grant_id="grant-1",
        expected_revision=1,
        source=SOURCE,
        canonical_model=MODEL,
        operation="query",
        principal_scope=PRINCIPAL,
        host=HOST,
        run_key="run-1",
    )


def _scope(attempt: TransportAttempt | None = None) -> SourceAttemptScopeV2:
    """Return one complete scope using synthetic, non-secret digests."""
    return SourceAttemptScopeV2(
        attempt=attempt or _attempt(),
        capture_intent_id="intent-1",
        request_summary_sha256=hashlib.sha256(b"synthetic request").hexdigest(),
        source_policy_id="sina-financial-policy",
        source_policy_revision="rev-1",
        source_policy_sha256=hashlib.sha256(b"synthetic source policy").hexdigest(),
        budget_binding_sha256=hashlib.sha256(b"synthetic source binding").hexdigest(),
        budget_quota_key=QUOTA_KEY,
        budget_policy_revision=1,
        budget_policy_sha256=hashlib.sha256(b"synthetic gate budget policy").hexdigest(),
    )


def test_v2_scope_rejects_expected_grant_evidence_reference_until_scope_version_changes() -> None:
    """V2 cannot silently omit a binding field from its canonical audit scope."""
    with pytest.raises(SourceAttemptInputError, match="v2 source attempt scope"):
        _scope(
            replace(
                _attempt(),
                expected_rights_evidence_reference="synthetic:expected-grant-evidence",
            )
        )


@pytest.fixture
def gate_engine(tmp_path: Path) -> Engine:
    """Yield a test-owned database with both the base and exact v2 schema."""
    engine = _new_engine(tmp_path)
    _apply_base_schema(engine)
    _install_v2_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def v1_engine(tmp_path: Path) -> Engine:
    """Yield a test-owned base-only database to exercise fail-closed upgrades."""
    engine = _new_engine(tmp_path, "gate-v1.sqlite")
    _apply_base_schema(engine)
    yield engine
    engine.dispose()


def _seed_allow_configuration(engine: Engine, scope: SourceAttemptScopeV2) -> None:
    """Create one synthetic allowed grant and matching gate bucket for a test."""
    evidence_sha = hashlib.sha256(b"synthetic evidence reference").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_gate_policies "
                "(quota_key, policy_revision, rate_microtokens_per_second, "
                "capacity_microtokens, policy_reference, policy_sha256, configured_by, "
                "configured_at_us) VALUES (:quota, 1, 1000000, 1000000, :reference, "
                ":digest, :actor, 1)"
            ),
            {
                "quota": scope.budget_quota_key,
                "reference": "synthetic:gate-policy",
                "digest": scope.budget_policy_sha256,
                "actor": "offline-test",
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_source_bindings "
                "(source, quota_key, binding_reference, binding_sha256, configured_by, "
                "configured_at_us) VALUES (:source, :quota, :reference, :digest, :actor, 1)"
            ),
            {
                "source": scope.attempt.source,
                "quota": scope.budget_quota_key,
                "reference": "synthetic:source-binding",
                "digest": scope.budget_binding_sha256,
                "actor": "offline-test",
            },
        )
        connection.execute(
            text(
                "INSERT INTO od_source_gate_state "
                "(quota_key, tokens_micro, remainder, last_refill_us, policy_revision) "
                "VALUES (:quota, 1000000, 0, :refill, 1)"
            ),
            {"quota": scope.budget_quota_key, "refill": NOW_US},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant, :source, :model, 'query', :principal, 'ALLOWED', 1, 0, :expires, '[]', "
                ":reference, :digest, :note, :actor, 1, 1, 1)"
            ),
            {
                "grant": scope.attempt.grant_id,
                "source": scope.attempt.source,
                "model": scope.attempt.canonical_model,
                "principal": scope.attempt.principal_scope,
                "expires": MAX_TIMESTAMP_US,
                "reference": "synthetic:grant-evidence",
                "digest": evidence_sha,
                "note": "Synthetic test declaration; not legal evidence.",
                "actor": "offline-test",
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": scope.attempt.grant_id, "host": scope.attempt.host},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-test', 'synthetic-test')"
            ),
            {"grant": scope.attempt.grant_id},
        )


def _allowed_grant_row(scope: SourceAttemptScopeV2) -> dict[str, object]:
    """Build a synthetic, internally consistent grant row for policy-boundary checks."""
    return {
        "decision": "ALLOWED",
        "revision": 1,
        "created_at_us": 1,
        "updated_at_us": 1,
        "revoked_at_us": None,
        "revocation_actor": None,
        "revocation_reason_code": None,
        "source": scope.attempt.source,
        "canonical_model": scope.attempt.canonical_model,
        "operation": scope.attempt.operation,
        "principal_scope": scope.attempt.principal_scope,
        "not_before_us": 0,
        "expires_at_us": MAX_TIMESTAMP_US,
        "conditions_json": "[]",
        "rights_evidence_reference": "synthetic:grant-evidence",
        "rights_evidence_sha256": hashlib.sha256(b"synthetic grant evidence").hexdigest(),
        "operator_attestation_note": "Synthetic declaration for offline tests.",
        "attested_by": "offline-test",
        "attested_at_us": 1,
    }


def _rewrite_immutable_gate_row(
    engine: Engine,
    *,
    table: str,
    column: str,
    value: object,
    attempt_id: str,
) -> None:
    """Corrupt one temporary immutable row to check fail-closed lookup behavior."""
    table_contracts = {
        "od_source_attempt_reservations": (
            {
                "outcome",
                "denial_code",
                "reserved_at_us",
                "retry_after_us",
                "quota_key",
                "policy_revision",
                "rate_microtokens_per_second",
                "capacity_microtokens",
            },
            "trg_od_source_attempt_immutable_update",
            "BEFORE UPDATE ON od_source_attempt_reservations BEGIN "
            "SELECT RAISE(ABORT, 'source attempt tombstones are immutable'); END",
            {
                "outcome": "UPDATE od_source_attempt_reservations SET outcome = :value "
                "WHERE attempt_id = :attempt_id",
                "denial_code": "UPDATE od_source_attempt_reservations SET denial_code = :value "
                "WHERE attempt_id = :attempt_id",
                "reserved_at_us": "UPDATE od_source_attempt_reservations "
                "SET reserved_at_us = :value WHERE attempt_id = :attempt_id",
                "retry_after_us": "UPDATE od_source_attempt_reservations "
                "SET retry_after_us = :value WHERE attempt_id = :attempt_id",
                "quota_key": "UPDATE od_source_attempt_reservations SET quota_key = :value "
                "WHERE attempt_id = :attempt_id",
                "policy_revision": "UPDATE od_source_attempt_reservations "
                "SET policy_revision = :value WHERE attempt_id = :attempt_id",
                "rate_microtokens_per_second": "UPDATE od_source_attempt_reservations "
                "SET rate_microtokens_per_second = :value WHERE attempt_id = :attempt_id",
                "capacity_microtokens": "UPDATE od_source_attempt_reservations "
                "SET capacity_microtokens = :value WHERE attempt_id = :attempt_id",
            },
        ),
        "od_source_attempt_scope_v2": (
            {"scope_version", "scope_json", "scope_sha256", "recorded_at_us"},
            "trg_od_source_attempt_scope_v2_immutable_update",
            "BEFORE UPDATE ON od_source_attempt_scope_v2 BEGIN "
            "SELECT RAISE(ABORT, 'source attempt scope v2 rows are immutable'); END",
            {
                "scope_version": "UPDATE od_source_attempt_scope_v2 SET scope_version = :value "
                "WHERE attempt_id = :attempt_id",
                "scope_json": "UPDATE od_source_attempt_scope_v2 SET scope_json = :value "
                "WHERE attempt_id = :attempt_id",
                "scope_sha256": "UPDATE od_source_attempt_scope_v2 SET scope_sha256 = :value "
                "WHERE attempt_id = :attempt_id",
                "recorded_at_us": "UPDATE od_source_attempt_scope_v2 "
                "SET recorded_at_us = :value WHERE attempt_id = :attempt_id",
            },
        ),
    }
    allowed_columns, trigger_name, trigger_body, update_statements = table_contracts[table]
    assert column in allowed_columns
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP TRIGGER {trigger_name}")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            text(update_statements[column]),
            {"value": value, "attempt_id": attempt_id},
        )
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")
        connection.exec_driver_sql(f"CREATE TRIGGER {trigger_name} {trigger_body}")


def test_lookup_absent_is_read_only_and_requires_v2_marker(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified v2 read can return absent without touching the mutable clock."""
    gate = SourceAttemptGate(gate_engine)
    monkeypatch.setattr(gate, "_advance_clock", lambda *_args: pytest.fail("lookup wrote clock"))

    result = gate.lookup_attempt_v2(_scope())

    assert isinstance(result, AttemptLookupAbsent)
    assert result.status == "ABSENT"


def test_lookup_connection_rejects_actual_dml(gate_engine: Engine) -> None:
    """URI read-only and query_only settings prevent writes through lookup handles."""
    gate = SourceAttemptGate(gate_engine)
    connection = gate._open_readonly_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE od_source_gate_clock SET highwater_us = 1 WHERE clock_id = 1"
            )
    finally:
        connection.close()

    with gate_engine.connect() as engine_connection:
        assert (
            engine_connection.execute(
                text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == 0
        )


def test_reservation_and_scope_sidecar_commit_atomically_as_denial(gate_engine: Engine) -> None:
    """The complete scope is available for exact lookup even when grant denies."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()

    reserved = gate.reserve_attempt_v2(scope)
    looked_up = gate.lookup_attempt_v2(scope)

    assert reserved.status is ReservationStatus.DENIED
    assert isinstance(looked_up, AttemptLookupDenied)
    assert looked_up.denial_code == "grant_not_found"
    with gate_engine.connect() as connection:
        base_count = connection.execute(
            text("SELECT COUNT(*) FROM od_source_attempt_reservations")
        ).scalar_one()
        sidecar_count = connection.execute(
            text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")
        ).scalar_one()
    assert base_count == sidecar_count == 1


def test_matching_budget_pins_can_reserve_and_lookup_exact_scope(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 reservation binds the consumed bucket to the request's policy pins."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_allow_configuration(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    reserved = gate.reserve_attempt_v2(scope)
    looked_up = gate.lookup_attempt_v2(scope)

    assert reserved.status is ReservationStatus.RESERVED_NEW
    assert isinstance(looked_up, AttemptLookupReserved)
    assert looked_up.scope_sha256
    assert looked_up.reserved_at_us == NOW_US


def test_repeated_v2_reservation_returns_duplicate_without_second_debit(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated exact scope cannot expose a reusable reservation or spend twice."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_allow_configuration(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    first = gate.reserve_attempt_v2(scope)
    repeated = gate.reserve_attempt_v2(scope)

    assert first.status is ReservationStatus.RESERVED_NEW
    assert repeated.status is ReservationStatus.DUPLICATE
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
                {"quota": scope.budget_quota_key},
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")).scalar_one()
            == 1
        )


def test_concurrent_exact_v2_reservations_have_one_winner(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SQLite writer serialization permits only one committed exact reservation."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_allow_configuration(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    start = Barrier(2)

    def reserve_after_barrier() -> ReservationStatus:
        start.wait(timeout=10)
        return gate.reserve_attempt_v2(scope).status

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: reserve_after_barrier(), range(2)))

    assert sorted(outcome.value for outcome in outcomes) == ["DUPLICATE", "RESERVED_NEW"]
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
                {"quota": scope.budget_quota_key},
            ).scalar_one()
            == 0
        )


def test_v2_lookup_never_promotes_a_legacy_row_to_a_complete_decision(
    gate_engine: Engine,
) -> None:
    """A matching v1 tombstone remains indeterminate because its scope is incomplete."""
    gate = SourceAttemptGate(gate_engine)
    attempt = _attempt()
    assert gate.reserve_attempt(attempt).status is ReservationStatus.DENIED

    result = gate.lookup_attempt_v2(_scope(attempt))

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.LEGACY_SCOPE_UNVERIFIABLE


def test_reused_keys_with_different_v2_scope_are_indeterminate(gate_engine: Engine) -> None:
    """A partial key match cannot be downgraded to absence or a prior decision."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    assert gate.reserve_attempt_v2(scope).status is ReservationStatus.DENIED

    result = gate.lookup_attempt_v2(replace(scope, source_policy_revision="rev-2"))

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCOPE_MISMATCH


def test_corrupt_scope_digest_is_indeterminate(gate_engine: Engine) -> None:
    """A digest mismatch is reported as corrupt evidence, never as a decision."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    gate.reserve_attempt_v2(scope)
    with gate_engine.begin() as connection:
        connection.execute(text("DROP TRIGGER trg_od_source_attempt_scope_v2_immutable_update"))
        connection.execute(
            text(
                "UPDATE od_source_attempt_scope_v2 SET scope_sha256 = :digest "
                "WHERE attempt_id = :attempt_id"
            ),
            {"digest": "0" * 64, "attempt_id": scope.attempt.attempt_id},
        )
        connection.exec_driver_sql(
            "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_update "
            "BEFORE UPDATE ON od_source_attempt_scope_v2 BEGIN "
            "SELECT RAISE(ABORT, 'source attempt scope v2 rows are immutable'); END"
        )

    result = gate.lookup_attempt_v2(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCOPE_CORRUPT


def test_missing_v2_migration_fails_closed_before_reservation(v1_engine: Engine) -> None:
    """The legacy schema neither authorizes lookup absence nor v2 reservation."""
    gate = SourceAttemptGate(v1_engine)
    scope = _scope()

    lookup = gate.lookup_attempt_v2(scope)
    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt_v2(scope)

    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
    with v1_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 0
        )


def test_unregistered_sidecar_trigger_fails_closed(gate_engine: Engine) -> None:
    """Lookup refuses migration drift that adds an unregistered sidecar trigger."""
    with gate_engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER test_unregistered_scope_v2_trigger "
            "AFTER INSERT ON od_source_attempt_scope_v2 BEGIN SELECT 1; END"
        )

    result = SourceAttemptGate(gate_engine).lookup_attempt_v2(_scope())

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE


def test_partial_request_unique_index_is_not_a_complete_lookup_key(tmp_path: Path) -> None:
    """A partial index cannot stand in for the base schema's full request key."""
    engine = _new_engine(tmp_path, "gate-partial-request-index.sqlite")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE od_source_attempt_reservations ("
            "attempt_id TEXT PRIMARY KEY, request_id TEXT, attempt_no INTEGER, outcome TEXT)"
        )
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX test_partial_request_attempt_no "
            "ON od_source_attempt_reservations (request_id, attempt_no) "
            "WHERE outcome = 'DENIED'"
        )

    gate = SourceAttemptGate(engine)
    with engine.connect() as connection:
        assert not gate._has_attempt_request_unique_key(connection)
    engine.dispose()


def test_lookup_rejects_base_table_without_attempt_id_primary_key(tmp_path: Path) -> None:
    """A v2 marker cannot make a malformed base attempt table authoritative."""
    engine = _new_engine(tmp_path, "gate-no-attempt-pk.sqlite")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE od_source_attempt_reservations ("
            "attempt_id VARCHAR(128) NOT NULL, request_id VARCHAR(128) NOT NULL, "
            "attempt_no BIGINT NOT NULL, grant_id VARCHAR(128) NOT NULL, "
            "expected_revision BIGINT NOT NULL, source VARCHAR(128) NOT NULL, "
            "canonical_model VARCHAR(128) NOT NULL, operation VARCHAR(16) NOT NULL, "
            "principal_scope VARCHAR(128) NOT NULL, host VARCHAR(253) NOT NULL, "
            "run_key VARCHAR(128), outcome VARCHAR(8) NOT NULL, denial_code VARCHAR(64), "
            "retry_after_us BIGINT, decided_at_us BIGINT NOT NULL, reserved_at_us BIGINT, "
            "quota_key VARCHAR(128), policy_revision BIGINT, "
            "rate_microtokens_per_second BIGINT, capacity_microtokens BIGINT)"
        )
    _install_v2_schema(engine)

    result = SourceAttemptGate(engine).lookup_attempt_v2(_scope())

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE
    engine.dispose()


def test_orphan_sidecar_for_request_key_prevents_absent(tmp_path: Path) -> None:
    """A FK-disabled orphan with matching request keys makes absence indeterminate."""
    engine = _new_engine(tmp_path, "gate-orphan-scope.sqlite")
    _apply_base_schema(engine)
    _install_v2_schema(engine)
    scope = _scope()
    orphan_document = json.loads(_scope_v2_bytes(scope))
    orphan_document["attempt_id"] = "orphan-attempt"
    orphan_bytes = json.dumps(
        orphan_document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    orphan_digest = hashlib.sha256(orphan_bytes).hexdigest()

    connection = engine.connect()
    try:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 0
        connection.execute(
            text(
                "INSERT INTO od_source_attempt_scope_v2 "
                "(attempt_id, scope_version, scope_json, scope_sha256, recorded_at_us) "
                "VALUES ('orphan-attempt', 2, :scope_json, :scope_sha256, :recorded_at_us)"
            ),
            {
                "scope_json": orphan_bytes.decode("utf-8"),
                "scope_sha256": orphan_digest,
                "recorded_at_us": NOW_US,
            },
        )
        connection.commit()
    finally:
        connection.close()

    result = SourceAttemptGate(engine).lookup_attempt_v2(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCOPE_CORRUPT
    engine.dispose()


def test_sidecar_insert_failure_rolls_back_reservation_and_clock(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The base tombstone cannot commit without its matching v2 scope sidecar."""
    gate = SourceAttemptGate(gate_engine)
    original_insert = gate._insert_attempt_scope_v2

    def fail_sidecar_insert(
        connection: Connection,
        scope_value: SourceAttemptScopeV2,
        recorded_at_us: int,
    ) -> None:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_test_v2_sidecar_insert "
            "BEFORE INSERT ON od_source_attempt_scope_v2 BEGIN "
            "SELECT RAISE(ABORT, 'synthetic sidecar failure'); END"
        )
        original_insert(connection, scope_value, recorded_at_us)

    monkeypatch.setattr(gate, "_insert_attempt_scope_v2", fail_sidecar_insert)

    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt_v2(_scope())

    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == 0
        )


def test_sidecar_failure_after_grant_and_bucket_debit_rolls_back_all(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid grant and consumed token stay uncommitted unless the scope sidecar commits."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    _seed_allow_configuration(gate_engine, scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)
    original_insert = gate._insert_attempt_scope_v2

    def fail_sidecar_insert(
        connection: Connection,
        scope_value: SourceAttemptScopeV2,
        recorded_at_us: int,
    ) -> None:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_test_v2_sidecar_after_debit "
            "BEFORE INSERT ON od_source_attempt_scope_v2 BEGIN "
            "SELECT RAISE(ABORT, 'synthetic sidecar failure after debit'); END"
        )
        original_insert(connection, scope_value, recorded_at_us)

    monkeypatch.setattr(gate, "_insert_attempt_scope_v2", fail_sidecar_insert)

    with pytest.raises(SourceAttemptGateUnavailable):
        gate.reserve_attempt_v2(scope)

    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
                {"quota": scope.budget_quota_key},
            ).scalar_one()
            == 1_000_000
        )
        assert (
            connection.execute(
                text("SELECT highwater_us FROM od_source_gate_clock WHERE clock_id = 1")
            ).scalar_one()
            == 0
        )


def test_v1_tombstone_blocks_v2_without_adopting_legacy_scope(gate_engine: Engine) -> None:
    """A matching v1 base key remains a conflict and receives no v2 sidecar."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()

    assert gate.reserve_attempt(scope.attempt).status is ReservationStatus.DENIED
    v2_result = gate.reserve_attempt_v2(scope)

    assert v2_result.status is ReservationStatus.ID_CONFLICT
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")).scalar_one()
            == 0
        )


def test_v2_tombstone_is_not_reissued_through_v1_api(gate_engine: Engine) -> None:
    """The legacy API can report duplicate but cannot re-debit or expose the v2 scope."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()

    assert gate.reserve_attempt_v2(scope).status is ReservationStatus.DENIED
    legacy_result = gate.reserve_attempt(scope.attempt)

    assert legacy_result.status is ReservationStatus.DUPLICATE
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM od_source_attempt_reservations")
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(text("SELECT COUNT(*) FROM od_source_attempt_scope_v2")).scalar_one()
            == 1
        )


@pytest.mark.parametrize(
    ("field", "value", "expected_message"),
    [
        ("operation", "download", "operation is not supported"),
        ("expected_revision", True, "expected_revision is outside"),
        ("host", "https://api.sina.example/path", "exact DNS hostname"),
    ],
)
def test_transport_attempt_rejects_invalid_scope_fields(
    field: str,
    value: object,
    expected_message: str,
) -> None:
    """Transport identity rejects invalid operation, bool revisions, and URL hosts."""
    with pytest.raises(SourceAttemptInputError, match=expected_message):
        replace(_attempt(), **{field: value})


@pytest.mark.parametrize(
    "invalid_scope",
    [
        {"attempt": object()},
        {"attempt": replace(_attempt(), run_key=None)},
        {"request_summary_sha256": "A" * 64},
        {"budget_policy_revision": True},
    ],
)
def test_v2_scope_rejects_missing_or_malformed_contract_values(
    invalid_scope: dict[str, object],
) -> None:
    """The closed scope contract rejects absent identity and noncanonical pins."""
    with pytest.raises(SourceAttemptInputError):
        replace(_scope(), **invalid_scope)


@pytest.mark.parametrize(
    ("host", "normalized"),
    [
        ("BÜCHER.example.", "xn--bcher-kva.example"),
        ("API.SINA.EXAMPLE", "api.sina.example"),
    ],
)
def test_dns_host_normalization_is_exact_and_ascii_canonical(host: str, normalized: str) -> None:
    """Unicode DNS input is converted to one canonical hostname before pinning."""
    assert _normalize_host(host) == normalized


@pytest.mark.parametrize(
    "host",
    [
        "",
        " api.sina.example",
        "api..sina.example",
        "-api.sina.example",
        "api.sina.example:443",
        "*.sina.example",
        "\ud800",
        42,
    ],
)
def test_dns_host_normalization_rejects_non_host_values(host: object) -> None:
    """URLs, malformed labels, whitespace, surrogates, and non-text hosts fail closed."""
    with pytest.raises(SourceAttemptInputError):
        _normalize_host(host)


def test_database_path_resolution_requires_local_existing_file_uri(tmp_path: Path) -> None:
    """Database URI parsing accepts only explicitly enabled local file URIs."""
    local_file = tmp_path / "gate path.sqlite"
    local_file.touch()

    assert _configured_database_path(local_file.as_uri(), {"uri": "true"}) == local_file
    with pytest.raises(ValueError, match="uri=true"):
        _configured_database_path(local_file.as_uri(), {})
    with pytest.raises(ValueError, match="local file"):
        _configured_database_path("file://remote.example/tmp/gate.sqlite", {"uri": "true"})
    with pytest.raises(ValueError, match="local file"):
        _configured_database_path(f"{local_file.as_uri()}?mode=ro", {"uri": "true"})


def test_database_file_identity_rejects_missing_and_nonregular_paths(tmp_path: Path) -> None:
    """Only an existing regular file can be pinned as the SQLite main database."""
    regular_file = tmp_path / "existing.sqlite"
    regular_file.touch()
    resolved, metadata = _stat_database_file(regular_file)
    assert resolved == regular_file.resolve()
    assert metadata.st_ino > 0

    with pytest.raises(ValueError, match="regular file"):
        _stat_database_file(tmp_path)
    with pytest.raises(FileNotFoundError):
        _stat_database_file(tmp_path / "missing.sqlite")


def test_gate_constructor_rejects_memory_and_unverified_dialects() -> None:
    """The gate refuses ephemeral databases and every non-SQLite dialect."""
    memory_engine = sa.create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(SourceAttemptGateConfigurationError, match="file-backed"):
            SourceAttemptGate(memory_engine)
    finally:
        memory_engine.dispose()

    unsupported_engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    with pytest.raises(SourceAttemptGateConfigurationError, match="SQLite only"):
        SourceAttemptGate(unsupported_engine)  # type: ignore[arg-type]


def test_gate_write_connection_failure_is_unavailable(gate_engine: Engine) -> None:
    """A failed writer acquisition never degrades into a reservation denial."""
    gate = SourceAttemptGate(gate_engine)

    def unavailable_connect() -> None:
        raise OSError("synthetic closed storage")

    gate._engine = SimpleNamespace(connect=unavailable_connect)  # type: ignore[assignment]
    with pytest.raises(SourceAttemptGateUnavailable):
        gate._open_write_connection()


def test_lookup_rejects_wrong_input_and_reports_open_failure(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid lookup requests raise; a read connection failure stays indeterminate."""
    gate = SourceAttemptGate(gate_engine)
    with pytest.raises(SourceAttemptInputError, match="SourceAttemptScopeV2"):
        gate.lookup_attempt_v2(object())  # type: ignore[arg-type]
    with pytest.raises(SourceAttemptInputError, match="SourceAttemptScopeV2"):
        gate.reserve_attempt_v2(object())  # type: ignore[arg-type]
    with pytest.raises(SourceAttemptInputError, match="TransportAttempt"):
        gate.reserve_attempt(object())  # type: ignore[arg-type]

    def failed_read() -> sqlite3.Connection:
        raise OSError("synthetic read-only open failure")

    monkeypatch.setattr(gate, "_open_readonly_connection", failed_read)
    result = gate.lookup_attempt_v2(_scope())
    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.DATABASE_UNAVAILABLE


@pytest.mark.parametrize(
    ("grant_changes", "host_set", "hosts", "expected"),
    [
        ({"decision": "DENIED"}, "valid", [HOST], "grant_not_allowed"),
        ({"revision": True}, "valid", [HOST], "grant_invalid"),
        ({"created_at_us": 2}, "valid", [HOST], "grant_invalid"),
        ({"revoked_at_us": 1}, "valid", [HOST], "grant_invalid"),
        ({"revision": 2}, "valid", [HOST], "grant_revision_mismatch"),
        ({"source": "another-source"}, "valid", [HOST], "grant_scope_mismatch"),
        ({"not_before_us": NOW_US + 1}, "valid", [HOST], "grant_not_yet_valid"),
        ({"expires_at_us": NOW_US}, "valid", [HOST], "grant_expired"),
        ({"rights_evidence_sha256": "invalid"}, "valid", [HOST], "grant_evidence_incomplete"),
        ({"conditions_json": None}, "valid", [HOST], "grant_conditions_invalid"),
        ({"conditions_json": "{broken"}, "valid", [HOST], "grant_conditions_invalid"),
        ({"conditions_json": "{}"}, "valid", [HOST], "grant_conditions_invalid"),
        ({"conditions_json": "[1]"}, "valid", [HOST], "grant_conditions_invalid"),
        ({"conditions_json": '["redistribution"]'}, "valid", [HOST], "grant_conditions_unresolved"),
        ({}, None, [HOST], "grant_hosts_unsealed"),
        (
            {},
            {
                "revision": 2,
                "host_count": 1,
                "sealed_at_us": 1,
                "sealed_by": "offline-test",
                "reason_code": "synthetic-test",
            },
            [HOST],
            "grant_hosts_invalid",
        ),
        ({}, "valid", [], "grant_hosts_invalid"),
        ({}, "valid", ["API.SINA.EXAMPLE."], "grant_hosts_invalid"),
        ({}, "valid", ["api.sina.example:443"], "grant_hosts_invalid"),
        ({}, "valid", ["other.sina.example"], "grant_host_mismatch"),
        ({}, "valid", [HOST], None),
    ],
)
def test_grant_policy_boundaries_never_expand_authorization(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    grant_changes: dict[str, object],
    host_set: str | dict[str, object] | None,
    hosts: list[str],
    expected: str | None,
) -> None:
    """Grant evaluation rejects invalid declarations, clocks, conditions, and host seals."""
    gate = SourceAttemptGate(gate_engine)
    valid_host_set = {
        "revision": 1,
        "host_count": 1,
        "sealed_at_us": 1,
        "sealed_by": "offline-test",
        "reason_code": "synthetic-test",
    }
    selected_host_set = valid_host_set if host_set == "valid" else host_set
    monkeypatch.setattr(gate, "_fetch_one", lambda *_args: selected_host_set)
    monkeypatch.setattr(
        gate,
        "_fetch_all",
        lambda *_args: [{"host": host} for host in hosts],
    )
    grant = _allowed_grant_row(_scope())
    grant.update(grant_changes)

    assert gate._grant_denial(None, grant, _attempt(), NOW_US) == expected  # type: ignore[arg-type]


def test_grant_policy_treats_missing_grant_as_denied(gate_engine: Engine) -> None:
    """An absent grant has no authorization path, even with a complete attempt scope."""
    gate = SourceAttemptGate(gate_engine)

    assert gate._grant_denial(None, None, _attempt(), NOW_US) == "grant_not_found"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "scope_changes",
    [
        {"budget_quota_key": "other-quota"},
        {"budget_binding_sha256": hashlib.sha256(b"different binding").hexdigest()},
        {"budget_policy_revision": 2},
        {"budget_policy_sha256": hashlib.sha256(b"different policy").hexdigest()},
    ],
)
def test_budget_pin_mismatch_denies_without_consuming_token(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    scope_changes: dict[str, object],
) -> None:
    """Any mismatch in the shared budget pins denies before bucket debit."""
    gate = SourceAttemptGate(gate_engine)
    original_scope = _scope()
    _seed_allow_configuration(gate_engine, original_scope)
    monkeypatch.setattr(gate, "_database_now_us", lambda _connection: NOW_US)

    result = gate.reserve_attempt_v2(replace(original_scope, **scope_changes))

    assert result.status is ReservationStatus.DENIED
    assert result.denial_code == "budget_scope_mismatch"
    with gate_engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT tokens_micro FROM od_source_gate_state WHERE quota_key = :quota"),
                {"quota": original_scope.budget_quota_key},
            ).scalar_one()
            == 1_000_000
        )


def test_lookup_returns_id_conflict_when_the_two_unique_keys_diverge(
    gate_engine: Engine,
) -> None:
    """A matching attempt id and request key that point to different rows are ambiguous."""
    gate = SourceAttemptGate(gate_engine)
    first = _scope()
    second_attempt = replace(
        _attempt(attempt_id="attempt-2", request_id="request-2"),
        attempt_no=2,
    )
    second = _scope(second_attempt)
    assert gate.reserve_attempt_v2(first).status is ReservationStatus.DENIED
    assert gate.reserve_attempt_v2(second).status is ReservationStatus.DENIED
    conflicting = replace(
        first,
        attempt=replace(first.attempt, request_id=second.attempt.request_id, attempt_no=2),
    )

    lookup = gate.lookup_attempt_v2(conflicting)
    reservation = gate.reserve_attempt_v2(conflicting)

    assert isinstance(lookup, AttemptLookupIndeterminate)
    assert lookup.reason_code is AttemptLookupReasonCode.ID_CONFLICT
    assert reservation.status is ReservationStatus.ID_CONFLICT


@pytest.mark.parametrize(
    ("column", "value", "reason"),
    [
        ("scope_version", 3, AttemptLookupReasonCode.SCOPE_CORRUPT),
        ("scope_json", b"\xff", AttemptLookupReasonCode.SCOPE_CORRUPT),
        ("recorded_at_us", NOW_US, AttemptLookupReasonCode.RECORD_CORRUPT),
    ],
)
def test_lookup_marks_corrupt_sidecar_fields_indeterminate(
    gate_engine: Engine,
    column: str,
    value: object,
    reason: AttemptLookupReasonCode,
) -> None:
    """Malformed sidecar version, UTF-8 type, or timestamp cannot look like a decision."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    gate.reserve_attempt_v2(scope)
    _rewrite_immutable_gate_row(
        gate_engine,
        table="od_source_attempt_scope_v2",
        column=column,
        value=value,
        attempt_id=scope.attempt.attempt_id,
    )

    result = gate.lookup_attempt_v2(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is reason


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("denial_code", "unknown-denial"),
        ("outcome", "PENDING"),
        ("reserved_at_us", 1),
    ],
)
def test_lookup_rejects_corrupt_base_decision_fields(
    gate_engine: Engine,
    column: str,
    value: object,
) -> None:
    """Unrecognized decisions and malformed denial rows never appear reusable."""
    gate = SourceAttemptGate(gate_engine)
    scope = _scope()
    gate.reserve_attempt_v2(scope)
    _rewrite_immutable_gate_row(
        gate_engine,
        table="od_source_attempt_reservations",
        column=column,
        value=value,
        attempt_id=scope.attempt.attempt_id,
    )

    result = gate.lookup_attempt_v2(scope)

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.RECORD_CORRUPT


@pytest.mark.parametrize(
    "drift", ["unknown-marker", "multiple-markers", "extra-index", "trigger-body"]
)
def test_v2_lookup_rejects_marker_and_schema_drift(gate_engine: Engine, drift: str) -> None:
    """A v2 marker cannot mask migration lineage or sidecar structure drift."""
    with gate_engine.begin() as connection:
        if drift == "unknown-marker":
            connection.execute(
                text("UPDATE alembic_version_source_gate SET version_num = 'unknown' ")
            )
        elif drift == "multiple-markers":
            connection.execute(text("DELETE FROM alembic_version_source_gate"))
            connection.execute(
                text(
                    "INSERT INTO alembic_version_source_gate (version_num) "
                    "VALUES ('sg0001_gate_v1_adopt'), ('sg0002_source_attempt_scope_v2')"
                )
            )
        elif drift == "extra-index":
            connection.exec_driver_sql(
                "CREATE INDEX test_scope_v2_extra_index "
                "ON od_source_attempt_scope_v2 (scope_sha256)"
            )
        else:
            connection.exec_driver_sql(
                "DROP TRIGGER trg_od_source_attempt_scope_v2_immutable_update"
            )
            connection.exec_driver_sql(
                "CREATE TRIGGER trg_od_source_attempt_scope_v2_immutable_update "
                "BEFORE UPDATE ON od_source_attempt_scope_v2 WHEN 1 BEGIN "
                "SELECT RAISE(ABORT, 'source attempt scope v2 rows are immutable'); END"
            )

    result = SourceAttemptGate(gate_engine).lookup_attempt_v2(_scope())

    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.SCHEMA_UNAVAILABLE


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            sqlite3.OperationalError("synthetic schema read failure"),
            AttemptLookupReasonCode.DATABASE_UNAVAILABLE,
        ),
        (
            RuntimeError("synthetic schema verifier failure"),
            AttemptLookupReasonCode.SCHEMA_UNAVAILABLE,
        ),
    ],
)
def test_schema_verifier_exceptions_fail_closed(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: AttemptLookupReasonCode,
) -> None:
    """Unexpected verifier errors never turn an unverified schema into absence."""
    gate = SourceAttemptGate(gate_engine)
    connection = gate._open_readonly_connection()

    def fail_query(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        raise error

    monkeypatch.setattr(gate, "_query_rows", fail_query)
    try:
        assert gate._v2_schema_reason(connection) is expected
    finally:
        connection.close()


def test_revoke_grant_validates_inputs_and_missing_grant(gate_engine: Engine) -> None:
    """Compare-and-revoke rejects invalid arguments and reports a missing id safely."""
    gate = SourceAttemptGate(gate_engine)
    with pytest.raises(SourceAttemptInputError):
        gate.revoke_grant("grant-1", expected_revision=True, actor="operator", reason_code="test")
    with pytest.raises(SourceAttemptInputError):
        gate.revoke_grant("grant-1", expected_revision=1, actor="operator", reason_code="x" * 65)

    result = gate.revoke_grant(
        "grant-missing",
        expected_revision=1,
        actor="offline-test",
        reason_code="synthetic-test",
    )

    assert result.status is GrantRevocationStatus.NOT_FOUND


def test_readonly_database_identity_rejects_a_replaced_path(
    gate_engine: Engine,
    tmp_path: Path,
) -> None:
    """The read-only helper refuses a file other than the path pinned at construction."""
    gate = SourceAttemptGate(gate_engine)
    replacement = tmp_path / "replacement.sqlite"
    replacement.touch()
    connection = gate._open_readonly_connection()
    try:
        gate._expected_main_path = replacement.resolve()
        with pytest.raises(sqlite3.OperationalError, match="identity changed"):
            gate._verify_readonly_main_database(connection)
    finally:
        connection.close()


def test_persisted_bucket_metadata_rejects_invalid_rows(gate_engine: Engine) -> None:
    """Policy, binding, and mutable state readers validate pins before use."""
    gate = SourceAttemptGate(gate_engine)
    binding = {
        "quota_key": QUOTA_KEY,
        "configured_at_us": NOW_US,
        "binding_reference": "synthetic:binding",
        "binding_sha256": hashlib.sha256(b"synthetic binding").hexdigest(),
        "configured_by": "offline-test",
    }
    policy = {
        "policy_revision": 1,
        "rate_microtokens_per_second": 1_000_000,
        "capacity_microtokens": 1_000_000,
        "policy_sha256": hashlib.sha256(b"synthetic policy").hexdigest(),
        "configured_by": "offline-test",
        "policy_reference": "synthetic:policy",
        "configured_at_us": NOW_US,
    }
    state = {
        "policy_revision": 1,
        "tokens_micro": 0,
        "remainder": 0,
        "last_refill_us": NOW_US,
    }

    assert gate._binding_value(binding, NOW_US) == QUOTA_KEY
    assert gate._binding_value({**binding, "configured_at_us": NOW_US + 1}, NOW_US) is None
    assert gate._policy_values(policy, NOW_US) == (1, 1_000_000, 1_000_000)
    assert gate._policy_values({**policy, "configured_by": " "}, NOW_US) is None
    assert gate._state_values(state, NOW_US) == (1, 0, 0, NOW_US)
    assert gate._state_values({**state, "last_refill_us": NOW_US + 1}, NOW_US) is None


@pytest.mark.parametrize(
    ("last_refill", "now", "tokens", "rate", "capacity", "expected_tokens", "allowed"),
    [
        (0, 500_000, 0, 1_000_000, 2_000_000, 500_000, False),
        (0, 2_000_000, 0, 1_000_000, 1_000_000, 1_000_000, True),
    ],
)
def test_refill_bucket_handles_partial_and_full_refills(
    last_refill: int,
    now: int,
    tokens: int,
    rate: int,
    capacity: int,
    expected_tokens: int,
    allowed: bool,
) -> None:
    """Integer refill arithmetic preserves fractional progress and caps at capacity."""
    result = _refill_bucket(
        tokens_micro=tokens,
        remainder=0,
        last_refill_us=last_refill,
        effective_now_us=now,
        policy_revision=1,
        rate_microtokens_per_second=rate,
        capacity_microtokens=capacity,
    )
    assert result.tokens_micro == expected_tokens
    assert result.allowed is allowed


def test_refill_bucket_rejects_boolean_and_backwards_clock() -> None:
    """Exact integer checks reject bools and a clock that moves before refill time."""
    base = {
        "tokens_micro": 0,
        "remainder": 0,
        "last_refill_us": 1,
        "effective_now_us": 1,
        "policy_revision": 1,
        "rate_microtokens_per_second": 1_000_000,
        "capacity_microtokens": 1_000_000,
    }
    with pytest.raises(SourceAttemptGateUnavailable):
        _refill_bucket(**{**base, "tokens_micro": True})
    with pytest.raises(SourceAttemptGateUnavailable):
        _refill_bucket(**{**base, "effective_now_us": 0})


@pytest.mark.parametrize(
    ("sql", "opening", "expected"),
    [
        ("('a''b')", 0, "'a''b'"),
        ('("a)")', 0, '"a)"'),
        ("(`a)`)", 0, "`a)`"),
        ("([a)])", 0, "[a)]"),
        ("(unterminated", 0, None),
        ("plain", 0, None),
    ],
)
def test_sqlite_check_parser_balances_quoted_parentheses(
    sql: str,
    opening: int,
    expected: str | None,
) -> None:
    """The DDL parser ignores parentheses inside SQL-quoted identifiers and values."""
    parsed = _sqlite_parenthesized_content(sql, opening)
    if expected is None:
        assert parsed is None
    else:
        assert parsed == (expected, len(sql) - 1)


def test_transport_attempt_rejects_zero_attempt_number() -> None:
    """Attempt numbers are positive before they can reach a uniqueness lookup."""
    with pytest.raises(SourceAttemptInputError, match="attempt_no"):
        replace(_attempt(), attempt_no=0)


def test_path_and_persisted_integer_helpers_reject_empty_or_invalid_values() -> None:
    """Low-level contracts reject empty database names and booleans as integers."""
    with pytest.raises(ValueError, match="empty or in-memory"):
        _configured_database_path("", {})
    with pytest.raises(ValueError, match="empty or in-memory"):
        _configured_database_path(":memory:", {})
    assert _bounded_int(5, 0, 10) == 5
    with pytest.raises(SourceAttemptGateUnavailable):
        _bounded_int(True, 0, 10)
    with pytest.raises(SourceAttemptInputError, match="bounded opaque identifier"):
        _validate_identifier("not an identifier", "request_id")


@pytest.mark.parametrize(
    "error",
    [RuntimeError("synthetic verifier failure"), sa.exc.SQLAlchemyError("synthetic SQL failure")],
)
def test_writer_setup_errors_close_connection_and_fail_unavailable(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    """A failure after acquiring the writer cannot be reported as a gate decision."""
    gate = SourceAttemptGate(gate_engine)

    def fail_verification(_connection: object) -> None:
        raise error

    monkeypatch.setattr(gate, "_verify_main_database", fail_verification)
    with pytest.raises(SourceAttemptGateUnavailable):
        gate._open_write_connection()


def test_readonly_lookup_failure_after_open_is_indeterminate(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snapshot query failure after schema validation never becomes ABSENT."""
    gate = SourceAttemptGate(gate_engine)

    def fail_snapshot(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("synthetic snapshot read failure")

    monkeypatch.setattr(gate, "_lookup_v2_snapshot", fail_snapshot)
    result = gate.lookup_attempt_v2(_scope())
    assert isinstance(result, AttemptLookupIndeterminate)
    assert result.reason_code is AttemptLookupReasonCode.DATABASE_UNAVAILABLE


def test_clock_failures_do_not_produce_a_reservation(gate_engine: Engine) -> None:
    """Missing clock state, invalid database time, and failed update all fail closed."""
    gate = SourceAttemptGate(gate_engine)
    gate._fetch_one = lambda *_args: None  # type: ignore[method-assign]
    with pytest.raises(SourceAttemptGateUnavailable, match="clock is missing"):
        gate._advance_clock(None)  # type: ignore[arg-type]

    invalid_clock_connection = SimpleNamespace(
        exec_driver_sql=lambda _sql: SimpleNamespace(scalar_one=lambda: 0)
    )
    with pytest.raises(SourceAttemptGateUnavailable, match="clock is invalid"):
        gate._database_now_us(invalid_clock_connection)  # type: ignore[arg-type]

    gate._fetch_one = lambda *_args: {"highwater_us": 4}  # type: ignore[method-assign]
    gate._database_now_us = lambda _connection: 5  # type: ignore[method-assign]
    update_miss_connection = SimpleNamespace(execute=lambda *_args: SimpleNamespace(rowcount=0))
    with pytest.raises(SourceAttemptGateUnavailable, match="clock update failed"):
        gate._advance_clock(update_miss_connection)  # type: ignore[arg-type]


def test_commit_acknowledgement_failure_is_uncertain(gate_engine: Engine) -> None:
    """A commit exception is surfaced as uncertainty rather than success."""
    gate = SourceAttemptGate(gate_engine)

    def fail_commit() -> None:
        raise OSError("synthetic missing commit acknowledgement")

    with pytest.raises(SourceAttemptGateUnavailable, match="commit is uncertain"):
        gate._commit(SimpleNamespace(commit=fail_commit))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    (
        "decision",
        "revision",
        "expected_revision",
        "revoked_at_us",
        "revocation_actor",
        "revocation_reason_code",
        "expected_status",
        "expected_result_revision",
    ),
    [
        ("DENIED", 1, 1, None, None, None, GrantRevocationStatus.NOT_REVOCABLE, 1),
        ("ALLOWED", 2, 1, None, None, None, GrantRevocationStatus.STALE_REVISION, 2),
        (
            "ALLOWED",
            2_147_483_647,
            2_147_483_647,
            None,
            None,
            None,
            GrantRevocationStatus.NOT_REVOCABLE,
            2_147_483_647,
        ),
        ("ALLOWED", 1, 1, None, None, None, GrantRevocationStatus.REVOKED, 2),
    ],
)
def test_revocation_compare_and_set_boundary_statuses(
    gate_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
    revision: int,
    expected_revision: int,
    revoked_at_us: int | None,
    revocation_actor: str | None,
    revocation_reason_code: str | None,
    expected_status: GrantRevocationStatus,
    expected_result_revision: int,
) -> None:
    """Non-allowed, stale, exhausted, and current revisions have distinct outcomes."""
    gate = SourceAttemptGate(gate_engine)
    connection = SimpleNamespace(
        close=lambda: None,
        execute=lambda *_args: SimpleNamespace(rowcount=1),
    )
    row = {
        "decision": decision,
        "revision": revision,
        "created_at_us": 1,
        "updated_at_us": 1,
        "revoked_at_us": revoked_at_us,
        "revocation_actor": revocation_actor,
        "revocation_reason_code": revocation_reason_code,
    }
    monkeypatch.setattr(gate, "_open_write_connection", lambda: connection)
    monkeypatch.setattr(gate, "_advance_clock", lambda _connection: NOW_US)
    monkeypatch.setattr(gate, "_fetch_one", lambda *_args: row)
    monkeypatch.setattr(gate, "_commit", lambda _connection: None)

    result = gate.revoke_grant(
        "grant-1",
        expected_revision=expected_revision,
        actor="offline-test",
        reason_code="synthetic-test",
    )

    assert result.status is expected_status
    assert result.revision == expected_result_revision
