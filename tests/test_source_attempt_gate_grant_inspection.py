"""Offline contract tests for read-only source-grant inspection."""

from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.pool import NullPool

import alembic_source_gate.schema_contract as schema_contract
from alembic_source_gate.schema_contract import V2_OBJECT_SQL, V2_REVISION
from opendata.data.source_attempt_gate import (
    GrantRevocationStatus,
    SourceAttemptGate,
    SourceAttemptInputError,
    SourceGrantInspectionReason,
    SourceGrantInspectionRequest,
    SourceGrantInspectionStatus,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0015_source_attempt_gate.py"
)
MIGRATION_NAME = "grant_inspection_source_gate_0015_test"
GRANT_ID = "grant-inspection-test"
SOURCE = "sina"
MODEL = "financial-statement"
OPERATION = "query"
PRINCIPAL = "offline-reviewer"
HOST = "api.sina.example"
MAX_TIMESTAMP_US = 253_402_300_799_999_999
SOURCE_GATE_TABLE_SNAPSHOT_QUERIES = (
    ("alembic_version_source_gate", "SELECT * FROM alembic_version_source_gate ORDER BY rowid"),
    ("od_source_gate_clock", "SELECT * FROM od_source_gate_clock ORDER BY rowid"),
    ("od_source_gate_policies", "SELECT * FROM od_source_gate_policies ORDER BY rowid"),
    (
        "od_source_gate_source_bindings",
        "SELECT * FROM od_source_gate_source_bindings ORDER BY rowid",
    ),
    ("od_source_gate_state", "SELECT * FROM od_source_gate_state ORDER BY rowid"),
    ("od_source_grants", "SELECT * FROM od_source_grants ORDER BY rowid"),
    ("od_source_grant_hosts", "SELECT * FROM od_source_grant_hosts ORDER BY rowid"),
    ("od_source_grant_host_sets", "SELECT * FROM od_source_grant_host_sets ORDER BY rowid"),
    ("od_source_grant_events", "SELECT * FROM od_source_grant_events ORDER BY rowid"),
    (
        "od_source_attempt_reservations",
        "SELECT * FROM od_source_attempt_reservations ORDER BY rowid",
    ),
    (
        "od_source_attempt_scope_v2",
        "SELECT * FROM od_source_attempt_scope_v2 ORDER BY rowid",
    ),
)


def _load_migration() -> ModuleType:
    """Load only revision 0015 without importing application settings."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(path: Path) -> Engine:
    """Create an explicitly owned file-backed SQLite database for one test."""
    return sa.create_engine(f"sqlite:///{path}", poolclass=NullPool)


def _install_gate_schema(engine: Engine) -> None:
    """Install the pinned v1 migration and isolated v2 marker in test-owned storage."""
    migration = _load_migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        connection.execute(
            text(
                "CREATE TABLE alembic_version_source_gate ("
                " version_num VARCHAR(32) NOT NULL, CONSTRAINT "
                "alembic_version_source_gate_pkc PRIMARY KEY (version_num))"
            )
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )
        for statement in V2_OBJECT_SQL:
            connection.exec_driver_sql(statement)


def _seed_allowed_grant(
    engine: Engine,
    *,
    conditions_json: str = "[]",
    expires_at_us: int = MAX_TIMESTAMP_US,
    malformed_timestamps: bool = False,
) -> None:
    """Insert a synthetic declaration, exact host set, and migration-owned seal event."""
    evidence_sha256 = hashlib.sha256(b"synthetic evidence reference").hexdigest()
    with engine.begin() as connection:
        if malformed_timestamps:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            text(
                "INSERT INTO od_source_grants "
                "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                "revision, not_before_us, expires_at_us, conditions_json, "
                "rights_evidence_reference, rights_evidence_sha256, operator_attestation_note, "
                "attested_by, attested_at_us, created_at_us, updated_at_us) VALUES "
                "(:grant_id, :source, :model, :operation, :principal, 'ALLOWED', 1, 0, "
                ":expires_at_us, :conditions_json, 'offline:synthetic-reference', "
                ":evidence_sha256, 'Synthetic declaration for offline testing.', "
                "'offline-reviewer', 1, :created_at_us, :updated_at_us)"
            ),
            {
                "grant_id": GRANT_ID,
                "source": SOURCE,
                "model": MODEL,
                "operation": OPERATION,
                "principal": PRINCIPAL,
                "expires_at_us": expires_at_us,
                "conditions_json": conditions_json,
                "evidence_sha256": evidence_sha256,
                "created_at_us": 2 if malformed_timestamps else 1,
                "updated_at_us": 1,
            },
        )
        connection.execute(
            text("INSERT INTO od_source_grant_hosts (grant_id, host) VALUES (:grant, :host)"),
            {"grant": GRANT_ID, "host": HOST},
        )
        connection.execute(
            text(
                "INSERT INTO od_source_grant_host_sets "
                "(grant_id, revision, host_count, sealed_at_us, sealed_by, reason_code) "
                "VALUES (:grant, 1, 1, 1, 'offline-reviewer', 'synthetic-test')"
            ),
            {"grant": GRANT_ID},
        )
        if malformed_timestamps:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints = OFF")


def _prepared_gate(
    tmp_path: Path,
    *,
    conditions_json: str = "[]",
    expires_at_us: int = MAX_TIMESTAMP_US,
    malformed_timestamps: bool = False,
) -> tuple[Engine, SourceAttemptGate]:
    """Create an explicitly migrated test database with one synthetic grant."""
    engine = _new_engine(tmp_path / "grant-inspection.sqlite")
    _install_gate_schema(engine)
    _seed_allowed_grant(
        engine,
        conditions_json=conditions_json,
        expires_at_us=expires_at_us,
        malformed_timestamps=malformed_timestamps,
    )
    return engine, SourceAttemptGate(engine)


def _gate_table_snapshot(engine: Engine) -> dict[str, tuple[tuple[object, ...], ...]]:
    """Read every gate-owned table for a before/after non-mutation assertion."""
    snapshot: dict[str, tuple[tuple[object, ...], ...]] = {}
    with engine.connect() as connection:
        for table_name, query in SOURCE_GATE_TABLE_SNAPSHOT_QUERIES:
            rows = connection.exec_driver_sql(query).all()
            snapshot[table_name] = tuple(tuple(row) for row in rows)
    return snapshot


def _request(**changes: object) -> SourceGrantInspectionRequest:
    """Build a bounded exact-scope request with safe synthetic identifiers."""
    values: dict[str, object] = {
        "grant_id": GRANT_ID,
        "revision": 1,
        "source": SOURCE,
        "canonical_model": MODEL,
        "operation": OPERATION,
        "principal_scope": PRINCIPAL,
        "host": HOST,
    }
    values.update(changes)
    return SourceGrantInspectionRequest(**values)  # type: ignore[arg-type]


@pytest.fixture
def allowed_gate(tmp_path: Path) -> Generator[tuple[Engine, SourceAttemptGate], None, None]:
    """Provide an explicitly migrated gate with one synthetic active declaration."""
    engine, gate = _prepared_gate(tmp_path)
    yield engine, gate
    engine.dispose()


def test_matching_grant_is_advisory_and_does_not_mutate_gate_file(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An exact declaration and seal match returns only a fixed advisory result."""
    engine, gate = allowed_gate
    database_path = Path(engine.url.database or "")
    before = hashlib.sha256(database_path.read_bytes()).digest()
    before_tables = _gate_table_snapshot(engine)

    result = gate.inspect_grant(_request())

    after = hashlib.sha256(database_path.read_bytes()).digest()
    after_tables = _gate_table_snapshot(engine)
    assert result.status is SourceGrantInspectionStatus.MATCHED
    assert result.reason is SourceGrantInspectionReason.GRANT_MATCHED
    assert before == after
    assert before_tables == after_tables


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"revision": 2}, SourceGrantInspectionReason.GRANT_REVISION_MISMATCH),
        ({"source": "other-source"}, SourceGrantInspectionReason.GRANT_SCOPE_MISMATCH),
        ({"canonical_model": "other-model"}, SourceGrantInspectionReason.GRANT_SCOPE_MISMATCH),
        ({"operation": "store"}, SourceGrantInspectionReason.GRANT_SCOPE_MISMATCH),
        ({"principal_scope": "other-principal"}, SourceGrantInspectionReason.GRANT_SCOPE_MISMATCH),
        ({"host": "other.sina.example"}, SourceGrantInspectionReason.GRANT_HOST_MISMATCH),
    ],
)
def test_scope_mismatch_is_rejected_with_fixed_reason(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    overrides: dict[str, object],
    expected_reason: SourceGrantInspectionReason,
) -> None:
    """A mismatch in any caller-bound scope field cannot return a match."""
    _engine, gate = allowed_gate

    result = gate.inspect_grant(_request(**overrides))

    assert result.status is SourceGrantInspectionStatus.REJECTED
    assert result.reason is expected_reason


def test_missing_grant_is_rejected_without_returning_storage_details(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A missing grant produces a fixed reason without echoing identifiers."""
    _engine, gate = allowed_gate

    result = gate.inspect_grant(_request(grant_id="missing-grant"))

    assert result.status is SourceGrantInspectionStatus.REJECTED
    assert result.reason is SourceGrantInspectionReason.GRANT_NOT_FOUND
    assert not hasattr(result, "grant_id")


def test_invalid_high_water_clock_is_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An out-of-range persisted clock fails closed before grant evaluation."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE od_source_gate_clock SET highwater_us = :value WHERE clock_id = 1"),
            {"value": MAX_TIMESTAMP_US + 1},
        )

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.CLOCK_INVALID


def test_expired_grant_is_rejected(
    tmp_path: Path,
) -> None:
    """A valid declaration outside its stated time range is a definite rejection."""
    engine, gate = _prepared_gate(tmp_path, expires_at_us=1)
    try:
        result = gate.inspect_grant(_request())
    finally:
        engine.dispose()

    assert result.status is SourceGrantInspectionStatus.REJECTED
    assert result.reason is SourceGrantInspectionReason.GRANT_EXPIRED


def test_revoked_grant_is_rejected(
    tmp_path: Path,
) -> None:
    """A durable revocation remains a definite non-match for the old revision."""
    engine, gate = _prepared_gate(tmp_path)
    try:
        revocation = gate.revoke_grant(
            GRANT_ID,
            expected_revision=1,
            actor="offline-reviewer",
            reason_code="synthetic-test",
        )
        result = gate.inspect_grant(_request())
    finally:
        engine.dispose()

    assert revocation.status is GrantRevocationStatus.REVOKED
    assert result.status is SourceGrantInspectionStatus.REJECTED
    assert result.reason is SourceGrantInspectionReason.GRANT_NOT_ALLOWED


def test_malformed_grant_timestamps_are_indeterminate(tmp_path: Path) -> None:
    """A persisted declaration with contradictory lifecycle times is unverifiable."""
    engine, gate = _prepared_gate(tmp_path, malformed_timestamps=True)
    try:
        result = gate.inspect_grant(_request())
    finally:
        engine.dispose()

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE


def test_malformed_conditions_are_indeterminate(tmp_path: Path) -> None:
    """Malformed JSON or non-list condition declarations cannot be definite denials."""
    engine, gate = _prepared_gate(tmp_path, conditions_json='{"unexpected": true}')
    try:
        result = gate.inspect_grant(_request())
    finally:
        engine.dispose()

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE


def test_missing_clock_row_is_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A missing singleton clock row cannot be interpreted as a current grant."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM od_source_gate_clock WHERE clock_id = 1"))

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.CLOCK_INVALID


def test_unsealed_host_set_is_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing persisted host seal is unverifiable, not a definite scope denial."""
    _engine, gate = allowed_gate
    original_fetch_one = gate._fetch_one
    host_set_query = (
        "SELECT revision, host_count, sealed_at_us, sealed_by, reason_code "
        "FROM od_source_grant_host_sets WHERE grant_id = :grant_id"
    )

    def hide_host_set(
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> dict[str, object] | None:
        if statement == host_set_query:
            return None
        return original_fetch_one(connection, statement, parameters)

    monkeypatch.setattr(gate, "_fetch_one", hide_host_set)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE


def test_invalid_host_set_count_is_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seal whose declared host count disagrees with its rows is unverifiable."""
    _engine, gate = allowed_gate
    original_fetch_one = gate._fetch_one
    host_set_query = (
        "SELECT revision, host_count, sealed_at_us, sealed_by, reason_code "
        "FROM od_source_grant_host_sets WHERE grant_id = :grant_id"
    )

    def change_host_count(
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> dict[str, object] | None:
        row = original_fetch_one(connection, statement, parameters)
        if statement == host_set_query and row is not None:
            row["host_count"] = 2
        return row

    monkeypatch.setattr(gate, "_fetch_one", change_host_count)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE


def test_seal_event_actor_mismatch_is_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The seal event must bind to the persisted actor in the same snapshot."""
    _engine, gate = allowed_gate
    original_query_rows = gate._query_rows
    event_query_fragment = "FROM od_source_grant_events WHERE grant_id = :grant_id"

    def alter_seal_actor(
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        rows = original_query_rows(connection, statement, parameters)
        if event_query_fragment in statement and len(rows) == 1:
            rows[0]["actor"] = "different-actor"
        return rows

    monkeypatch.setattr(gate, "_query_rows", alter_seal_actor)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE


def test_added_source_gate_schema_object_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An unknown gate-owned table is treated as schema drift."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE od_source_unreviewed (id INTEGER)")

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_marker_ddl_drift_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """An extra marker constraint fails the exact version-table contract."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE alembic_version_source_gate")
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_source_gate ("
            " version_num VARCHAR(32) NOT NULL, CONSTRAINT "
            "alembic_version_source_gate_pkc PRIMARY KEY (version_num), "
            "CHECK (length(version_num) > 0))"
        )
        connection.execute(
            text("INSERT INTO alembic_version_source_gate (version_num) VALUES (:revision)"),
            {"revision": V2_REVISION},
        )

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_unknown_migration_marker_revision_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A structurally valid but unknown isolated marker cannot pass inspection."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version_source_gate SET version_num = 'unknown' "))

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_gate_foreign_key_violation_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """A dangling grant audit row is detected by the snapshot foreign-key check."""
    engine, gate = allowed_gate
    database_path = Path(engine.url.database or "")
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO od_source_grant_events "
            "(grant_id, event_type, old_revision, new_revision, actor, reason_code, "
            "occurred_at_us) VALUES (?, 'REVOKED', 1, 2, 'offline-reviewer', "
            "'synthetic-orphan', 1)",
            ("missing-parent-grant",),
        )
        connection.commit()
    finally:
        connection.close()

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_reference_schema_failure_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable pinned schema reference cannot produce a positive result."""
    _engine, gate = allowed_gate

    def fail_reference_schema(*, include_v2: bool) -> dict[tuple[str, str, str], str | None]:
        raise RuntimeError("synthetic private schema detail")

    monkeypatch.setattr(schema_contract, "_reference_schema", fail_reference_schema)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE
    assert "synthetic private schema detail" not in repr(result)


def test_non_utf8_database_metadata_fails_closed(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported encoding reported by SQLite blocks grant inspection."""
    _engine, gate = allowed_gate
    original_query_rows = gate._query_rows

    def report_unsupported_encoding(
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        if statement == "PRAGMA encoding":
            return [{"encoding": "UTF-16le"}]
        return original_query_rows(connection, statement, parameters)

    monkeypatch.setattr(gate, "_query_rows", report_unsupported_encoding)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_unknown_grant_denial_fails_closed_without_exposing_code(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A future denial code defaults to a fixed indeterminate record reason."""
    _engine, gate = allowed_gate

    def return_unknown_denial(
        connection: Connection | sqlite3.Connection,
        grant: Mapping[str, object] | None,
        attempt: TransportAttempt,
        effective_now: int,
    ) -> str:
        return "synthetic-private-denial-code"

    monkeypatch.setattr(gate, "_grant_denial", return_unknown_denial)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE
    assert "synthetic-private-denial-code" not in repr(result)


def test_unexpected_grant_validation_failure_is_redacted(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unexpected grant validation errors fail closed without exposing text."""
    _engine, gate = allowed_gate

    def raise_private_error(
        connection: Connection | sqlite3.Connection,
        grant: Mapping[str, object] | None,
        attempt: TransportAttempt,
        effective_now: int,
    ) -> str | None:
        raise RuntimeError("synthetic private validation detail")

    monkeypatch.setattr(gate, "_grant_denial", raise_private_error)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_RECORD_UNVERIFIABLE
    assert "synthetic private validation detail" not in repr(result)


def test_additional_seal_event_makes_grant_indeterminate(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Contradictory extra audit history prevents a positive inspection result."""
    engine, gate = allowed_gate
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO od_source_grant_events "
                "(grant_id, event_type, old_revision, new_revision, actor, reason_code, "
                "occurred_at_us) VALUES (:grant, 'REVOKED', 1, 2, 'offline-reviewer', "
                "'inconsistent-fixture', 1)"
            ),
            {"grant": GRANT_ID},
        )

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.GRANT_SEAL_UNVERIFIABLE


def test_v1_only_schema_is_indeterminate(tmp_path: Path) -> None:
    """A database without the exact v2 marker cannot return an inspection match."""
    engine = _new_engine(tmp_path / "grant-inspection-v1.sqlite")
    migration = _load_migration()
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
    gate = SourceAttemptGate(engine)
    try:
        result = gate.inspect_grant(_request())
    finally:
        engine.dispose()

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.SCHEMA_UNAVAILABLE


def test_invalid_request_fails_before_database_access(
    allowed_gate: tuple[Engine, SourceAttemptGate],
) -> None:
    """Malformed scope is rejected as input rather than interpreted as a grant."""
    _engine, gate = allowed_gate

    with pytest.raises(SourceAttemptInputError):
        gate.inspect_grant(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [{"revision": 0}, {"operation": "unsupported"}],
)
def test_request_dto_rejects_invalid_scope_fields(
    overrides: dict[str, object],
) -> None:
    """Invalid revision and operation values fail in the request value object."""
    with pytest.raises(SourceAttemptInputError):
        _request(**overrides)


def test_database_open_failure_returns_fixed_result_without_storage_path(
    tmp_path: Path,
) -> None:
    """An unavailable test-owned database returns only a fixed reason code."""
    engine, gate = _prepared_gate(tmp_path)
    database_path = Path(engine.url.database or "")
    engine.dispose()
    database_path.unlink()

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.DATABASE_UNAVAILABLE
    assert str(database_path) not in repr(result)


def test_database_query_failure_returns_fixed_result_without_error_details(
    allowed_gate: tuple[Engine, SourceAttemptGate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed snapshot query returns a fixed result without SQLite diagnostics."""
    _engine, gate = allowed_gate
    original_query_rows = gate._query_rows
    grant_query = "SELECT * FROM od_source_grants WHERE grant_id = :grant_id"

    def fail_grant_query(
        connection: Connection | sqlite3.Connection,
        statement: str,
        parameters: dict[str, object],
    ) -> list[dict[str, object]]:
        if statement == grant_query:
            raise sqlite3.OperationalError("synthetic private database detail")
        return original_query_rows(connection, statement, parameters)

    monkeypatch.setattr(gate, "_query_rows", fail_grant_query)

    result = gate.inspect_grant(_request())

    assert result.status is SourceGrantInspectionStatus.INDETERMINATE
    assert result.reason is SourceGrantInspectionReason.DATABASE_UNAVAILABLE
    assert "synthetic private database detail" not in repr(result)
