"""Pin SQLite behavior and the MySQL 8 no-op contract for revision 0015."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0015_source_attempt_gate.py"
MIGRATION_NAME = "source_attempt_gate_migration_compat_0015"

GATE_TABLES = {
    "od_source_gate_clock",
    "od_source_gate_policies",
    "od_source_gate_source_bindings",
    "od_source_gate_state",
    "od_source_grants",
    "od_source_grant_hosts",
    "od_source_grant_host_sets",
    "od_source_grant_events",
    "od_source_attempt_reservations",
}
GATE_TRIGGERS = {
    "trg_od_source_attempt_immutable_update",
    "trg_od_source_attempt_immutable_delete",
    "trg_od_source_grant_revoke_only",
    "trg_od_source_grant_revoke_event",
    "trg_od_source_grant_host_insert_before_seal",
    "trg_od_source_grant_host_set_validate",
    "trg_od_source_grant_host_set_event",
    "trg_od_source_grant_host_set_immutable_update",
    "trg_od_source_grant_host_set_immutable_delete",
    "trg_od_source_grant_immutable_delete",
    "trg_od_source_grant_host_immutable_update",
    "trg_od_source_grant_host_immutable_delete",
    "trg_od_source_grant_event_immutable_update",
    "trg_od_source_grant_event_immutable_delete",
    "trg_od_source_gate_policy_revision",
    "trg_od_source_gate_policy_immutable_delete",
    "trg_od_source_gate_binding_immutable_update",
    "trg_od_source_gate_binding_immutable_delete",
}


def _load_migration() -> ModuleType:
    """Load revision 0015 without importing the Alembic environment."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reject_schema_operation(*_args: object, **_kwargs: object) -> None:
    """Fail if a simulated unsupported/no-op target reaches a DDL operation."""
    raise AssertionError("revision 0015 unexpectedly attempted a schema operation")


def _patch_simulated_target(
    monkeypatch: pytest.MonkeyPatch,
    migration: ModuleType,
    *,
    name: str,
    version: tuple[int, ...] | None,
    is_mariadb: bool = False,
) -> None:
    """Supply a dialect-only bind and make every migration write observable."""
    dialect = SimpleNamespace(
        name=name,
        server_version_info=version,
        is_mariadb=is_mariadb,
    )
    monkeypatch.setattr(
        migration.op,
        "get_bind",
        lambda: SimpleNamespace(dialect=dialect),
    )
    for operation in ("create_table", "drop_table", "execute"):
        monkeypatch.setattr(migration.op, operation, _reject_schema_operation)


@pytest.mark.parametrize("version", [(8, 0, 16), (8, 0, 36), (8, 4, 0)])
def test_supported_mysql8_upgrade_and_downgrade_are_explicit_noops(
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, ...],
) -> None:
    """Supported MySQL 8 runs the revision without issuing SQL or DDL."""
    migration = _load_migration()
    _patch_simulated_target(monkeypatch, migration, name="mysql", version=version)

    migration.upgrade()
    migration.downgrade()

    assert migration.revision == "0015_source_attempt_gate"
    assert migration.down_revision == "0014_sina_fzb_slot_reconciliation"


@pytest.mark.parametrize(
    ("name", "version", "is_mariadb"),
    [
        ("mysql", (8, 0, 15), False),
        ("mysql", (9, 0, 0), False),
        ("mysql", None, False),
        ("mysql", (10, 11, 0), True),
        ("mariadb", (10, 11, 0), True),
        ("postgresql", (16, 4, 0), False),
    ],
)
def test_unverified_targets_fail_before_any_schema_operation(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    version: tuple[int, ...] | None,
    is_mariadb: bool,
) -> None:
    """Unknown dialects and versions fail closed on both migration directions."""
    migration = _load_migration()
    _patch_simulated_target(
        monkeypatch,
        migration,
        name=name,
        version=version,
        is_mariadb=is_mariadb,
    )

    for operation in (migration.upgrade, migration.downgrade):
        with pytest.raises(RuntimeError, match="source attempt gate revision 0015"):
            operation()


def test_sqlite_upgrade_and_downgrade_keep_gate_schema_and_unrelated_data(
    tmp_path: Path,
) -> None:
    """The SQLite schema path remains intact and never touches other table data."""
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'source-gate.sqlite'}")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE business_sentinel (value TEXT NOT NULL)"))
            connection.execute(text("INSERT INTO business_sentinel (value) VALUES ('preserve-me')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            inspector = inspect(connection)
            actual_tables = set(inspector.get_table_names())
            assert actual_tables == GATE_TABLES | {"business_sentinel"}
            actual_triggers = {
                row[0]
                for row in connection.execute(
                    text("SELECT name FROM sqlite_master WHERE type = 'trigger'")
                )
            }
            assert actual_triggers == GATE_TRIGGERS
            assert (
                connection.execute(text("SELECT value FROM business_sentinel")).scalar_one()
                == "preserve-me"
            )

            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()

            assert not GATE_TABLES & set(inspect(connection).get_table_names())
            assert (
                connection.execute(text("SELECT value FROM business_sentinel")).scalar_one()
                == "preserve-me"
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("evidence_kind", ["grant", "attempt"])
def test_sqlite_downgrade_refuses_to_drop_grant_or_attempt_evidence(
    tmp_path: Path,
    evidence_kind: str,
) -> None:
    """Each populated grant or attempt record blocks downgrade and remains readable."""
    migration = _load_migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'source-gate-evidence.sqlite'}")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            if evidence_kind == "grant":
                connection.execute(
                    text(
                        "INSERT INTO od_source_grants "
                        "(grant_id, source, canonical_model, operation, principal_scope, decision, "
                        "revision, not_before_us, expires_at_us, conditions_json, created_at_us, "
                        "updated_at_us) VALUES "
                        "('grant-evidence', 'sina', 'financial-statement', 'query', 'worker', "
                        "'UNKNOWN', 1, 0, 100, '[]', 1, 1)"
                    )
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO od_source_attempt_reservations "
                        "(attempt_id, request_id, attempt_no, grant_id, expected_revision, source, "
                        "canonical_model, operation, principal_scope, host, outcome, denial_code, "
                        "decided_at_us) VALUES "
                        "('attempt-evidence', 'request-evidence', 1, 'grant-evidence', 1, 'sina', "
                        "'financial-statement', 'query', 'worker', 'api.sina.example', 'DENIED', "
                        "'fixture_denial', 1)"
                    )
                )

            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="source gate evidence must be archived"),
            ):
                migration.downgrade()

            actual_tables = set(inspect(connection).get_table_names())
            assert actual_tables == GATE_TABLES
            if evidence_kind == "grant":
                assert (
                    connection.execute(text("SELECT grant_id FROM od_source_grants")).scalar_one()
                    == "grant-evidence"
                )
                assert (
                    connection.execute(
                        text("SELECT COUNT(*) FROM od_source_attempt_reservations")
                    ).scalar_one()
                    == 0
                )
            else:
                assert (
                    connection.execute(text("SELECT COUNT(*) FROM od_source_grants")).scalar_one()
                    == 0
                )
                assert (
                    connection.execute(
                        text("SELECT attempt_id FROM od_source_attempt_reservations")
                    ).scalar_one()
                    == "attempt-evidence"
                )
    finally:
        engine.dispose()
