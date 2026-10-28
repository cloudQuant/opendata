"""Verify the fenced run-ledger claim-policy migration on private SQLite."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261029-0020_run_ledger_claim_policy.py"
)
BASE_MIGRATION_PATH = REPOSITORY_ROOT / "alembic_data" / "versions" / "20261026-0010_run_ledger.py"
VERSION_TABLE = "alembic_version_data"
RUN_KEY = "legacy-run"
LEGACY_SNAPSHOT = '{"job":"legacy","limit":10}'


def _load_migration(path: Path, module_name: str) -> ModuleType:
    """Load one literal revision without evaluating the Alembic environment."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_revision(engine: Engine, module: ModuleType, operation_name: str) -> None:
    """Run one revision against the caller-owned SQLite test database."""
    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
        with Operations.context(context):
            getattr(module, operation_name)()


def _prepare_legacy_database(engine: Engine) -> None:
    """Create the frozen 0010 shape, one legacy row, and the 0019 marker."""
    base = _load_migration(BASE_MIGRATION_PATH, "run_ledger_claim_policy_base_0010")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
        with Operations.context(context):
            base.upgrade()
        connection.execute(
            sa.text(
                "INSERT INTO od_run_ledger "
                "(run_key, job_snapshot_json, status, fencing_token, created_at_us, updated_at_us) "
                "VALUES (:run_key, :snapshot, 'queued', 0, 100, 101)"
            ),
            {"run_key": RUN_KEY, "snapshot": LEGACY_SNAPSHOT},
        )
        connection.execute(
            sa.text(
                "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:revision)"),
            {"revision": "0019_publication_market_symbol_scope"},
        )


def _apply_target(engine: Engine) -> ModuleType:
    """Apply revision 0020 and return its loaded module for follow-up checks."""
    migration = _load_migration(MIGRATION_PATH, "run_ledger_claim_policy_0020")
    _run_revision(engine, migration, "upgrade")
    return migration


def _legacy_row(connection: Connection) -> tuple[object, ...]:
    """Read the legacy row in a stable order that excludes added columns."""
    row = connection.execute(
        sa.text(
            "SELECT run_key, job_snapshot_json, status, worker, lease_id, fencing_token, "
            "lease_expires_at_us, created_at_us, updated_at_us, started_at_us, "
            "heartbeat_at_us, finished_at_us, error_text FROM od_run_ledger "
            "WHERE run_key = :run_key"
        ),
        {"run_key": RUN_KEY},
    ).one()
    return tuple(row)


def _insert_run(connection: Connection, *, run_key: str, policy: str | None = None) -> None:
    """Insert a queued test run with an optional explicit claim policy."""
    if policy is None:
        connection.execute(
            sa.text(
                "INSERT INTO od_run_ledger "
                "(run_key, job_snapshot_json, status, fencing_token, created_at_us, updated_at_us) "
                "VALUES (:run_key, '{}', 'queued', 0, 200, 201)"
            ),
            {"run_key": run_key},
        )
    else:
        connection.execute(
            sa.text(
                "INSERT INTO od_run_ledger "
                "(run_key, job_snapshot_json, status, fencing_token, created_at_us, updated_at_us, "
                "claim_policy) VALUES (:run_key, '{}', 'queued', 0, 200, 201, :policy)"
            ),
            {"run_key": run_key, "policy": policy},
        )


def test_upgrade_preserves_legacy_rows_and_restricts_policy_and_status(tmp_path: Path) -> None:
    """Retain every legacy value, default old inserts, and reject unknown enums."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'claim-policy.sqlite'}")
    try:
        _prepare_legacy_database(engine)
        with engine.connect() as connection:
            legacy_before = _legacy_row(connection)

        _apply_target(engine)

        inspector = sa.inspect(engine)
        columns = {item["name"]: item for item in inspector.get_columns("od_run_ledger")}
        assert columns["claim_policy"]["nullable"] is False
        assert getattr(columns["claim_policy"]["type"], "length", None) == 16
        assert columns["claim_policy"]["default"] == "'recover_expired'"
        assert {item["name"] for item in inspector.get_check_constraints("od_run_ledger")} == {
            "ck_od_run_ledger_status",
            "ck_od_run_ledger_fencing_token",
            "ck_od_run_ledger_claim_policy",
        }
        assert {
            item["name"]: tuple(item["column_names"])
            for item in inspector.get_indexes("od_run_ledger")
        } == {
            "ix_od_run_ledger_claim": (
                "status",
                "lease_expires_at_us",
                "created_at_us",
            )
        }

        with engine.begin() as connection:
            assert _legacy_row(connection) == legacy_before
            assert (
                connection.execute(
                    sa.text("SELECT claim_policy FROM od_run_ledger WHERE run_key = :run_key"),
                    {"run_key": RUN_KEY},
                ).scalar_one()
                == "recover_expired"
            )
            _insert_run(connection, run_key="legacy-default")
            _insert_run(connection, run_key="single-attempt", policy="one_shot")
            assert tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT run_key, claim_policy FROM od_run_ledger "
                        "WHERE run_key IN ('legacy-default', 'single-attempt') ORDER BY run_key"
                    )
                ).all()
            ) == (("legacy-default", "recover_expired"), ("single-attempt", "one_shot"))

            for index, invalid in enumerate(("", "unexpected", "ONE_SHOT", "Recover_Expired")):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _insert_run(connection, run_key=f"invalid-policy-{index}", policy=invalid)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO od_run_ledger "
                        "(run_key, job_snapshot_json, status, fencing_token, created_at_us, "
                        "updated_at_us, claim_policy) "
                        "VALUES ('null-policy', '{}', 'queued', 0, 1, 1, NULL)"
                    )
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "INSERT INTO od_run_ledger "
                        "(run_key, job_snapshot_json, status, fencing_token, created_at_us, "
                        "updated_at_us, claim_policy) "
                        "VALUES ('invalid-status', '{}', 'unknown', 0, 1, 1, 'one_shot')"
                    )
                )
    finally:
        engine.dispose()


def test_upgrade_recovers_only_a_complete_ddl_before_marker_advance(tmp_path: Path) -> None:
    """Treat a complete schema as committed DDL but reject a missing CHECK."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'claim-policy-recovery.sqlite'}")
    migration = _load_migration(MIGRATION_PATH, "run_ledger_claim_policy_recovery_0020")
    try:
        _prepare_legacy_database(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql(migration._add_column_ddl("sqlite"))

        _run_revision(engine, migration, "upgrade")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text("SELECT claim_policy FROM od_run_ledger WHERE run_key = :run_key"),
                    {"run_key": RUN_KEY},
                ).scalar_one()
                == "recover_expired"
            )
            assert (
                connection.execute(
                    sa.text("SELECT version_num FROM alembic_version_data")
                ).scalar_one()
                == "0019_publication_market_symbol_scope"
            )

        partial = sa.create_engine(f"sqlite:///{tmp_path / 'claim-policy-partial.sqlite'}")
        try:
            _prepare_legacy_database(partial)
            with partial.begin() as connection:
                connection.exec_driver_sql(
                    "ALTER TABLE od_run_ledger ADD COLUMN claim_policy VARCHAR(16) "
                    "NOT NULL DEFAULT 'recover_expired'"
                )
                before = tuple(
                    connection.execute(
                        sa.text("SELECT * FROM od_run_ledger WHERE run_key = :run_key"),
                        {"run_key": RUN_KEY},
                    ).one()
                )
            with pytest.raises(RuntimeError, match="unexpected od_run_ledger CHECK set"):
                _run_revision(partial, migration, "upgrade")
            with partial.connect() as connection:
                assert (
                    tuple(
                        connection.execute(
                            sa.text("SELECT * FROM od_run_ledger WHERE run_key = :run_key"),
                            {"run_key": RUN_KEY},
                        ).one()
                    )
                    == before
                )
                assert {
                    item["name"]
                    for item in sa.inspect(connection).get_check_constraints("od_run_ledger")
                } == {"ck_od_run_ledger_status", "ck_od_run_ledger_fencing_token"}
        finally:
            partial.dispose()
    finally:
        engine.dispose()


def test_downgrade_refuses_to_erase_one_shot_policy(tmp_path: Path) -> None:
    """Leave the column and one-shot row intact when automatic rollback is refused."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'claim-policy-downgrade.sqlite'}")
    migration = _load_migration(MIGRATION_PATH, "run_ledger_claim_policy_downgrade_0020")
    try:
        _prepare_legacy_database(engine)
        _apply_target(engine)
        with engine.begin() as connection:
            _insert_run(connection, run_key="single-attempt", policy="one_shot")
        with pytest.raises(RuntimeError, match="downgrade is refused"):
            _run_revision(engine, migration, "downgrade")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    sa.text(
                        "SELECT claim_policy FROM od_run_ledger WHERE run_key = 'single-attempt'"
                    )
                ).scalar_one()
                == "one_shot"
            )
            assert "claim_policy" in {
                item["name"] for item in sa.inspect(connection).get_columns("od_run_ledger")
            }
    finally:
        engine.dispose()


def test_upgrade_refuses_rows_written_while_checks_were_disabled(tmp_path: Path) -> None:
    """Fail closed when an out-of-band writer persisted a value outside either enum."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'claim-policy-invalid-row.sqlite'}")
    try:
        _prepare_legacy_database(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                sa.text(
                    "INSERT INTO od_run_ledger "
                    "(run_key, job_snapshot_json, status, fencing_token, created_at_us, "
                    "updated_at_us) "
                    "VALUES ('bad-status', '{}', 'RUNNING', 0, 1, 1)"
                )
            )
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")

        migration = _load_migration(MIGRATION_PATH, "run_ledger_claim_policy_bad_status_0020")
        with pytest.raises(RuntimeError, match="persisted status outside its exact enum"):
            _run_revision(engine, migration, "upgrade")
        with engine.connect() as connection:
            assert "claim_policy" not in {
                item["name"] for item in sa.inspect(connection).get_columns("od_run_ledger")
            }
        with engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM od_run_ledger WHERE run_key = 'bad-status'"))

        _apply_target(engine)
        with engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
            _insert_run(connection, run_key="bad-policy", policy="ONE_SHOT")
            connection.exec_driver_sql("PRAGMA ignore_check_constraints=OFF")
        with pytest.raises(RuntimeError, match="persisted status or claim_policy outside"):
            _run_revision(engine, migration, "upgrade")
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("dialect", "version", "expected_error"),
    [
        ("mariadb", (10, 11, 0), "revision 0020 supports SQLite and MySQL 8.0.16+ but not MariaDB"),
        ("mysql", (8, 0, 15), "revision 0020 requires MySQL 8.0.16 or later within MySQL 8"),
        ("mysql", (5, 7, 44), "revision 0020 requires MySQL 8.0.16 or later within MySQL 8"),
        ("postgresql", (16, 1), "revision 0020 supports SQLite and MySQL 8.0.16+ only"),
    ],
)
def test_unsupported_targets_fail_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
    dialect: str,
    version: tuple[int, int, int],
    expected_error: str,
) -> None:
    """Reject unsupported engines before any migration connection is requested."""
    migration = _load_migration(MIGRATION_PATH, f"claim_policy_target_{dialect}_{version[0]}")
    context = SimpleNamespace(
        as_sql=False,
        dialect=SimpleNamespace(
            name=dialect,
            is_mariadb=dialect == "mariadb",
            server_version_info=version,
        ),
    )

    class OperationsStub:
        """Expose only Alembic context access for the target guard."""

        @staticmethod
        def get_context() -> SimpleNamespace:
            return context

        @staticmethod
        def get_bind() -> None:
            raise AssertionError("unsupported targets must fail before requesting a connection")

    monkeypatch.setattr(migration, "op", OperationsStub())
    with pytest.raises(RuntimeError, match=re.escape(expected_error)):
        migration._require_supported_target()


def test_sqlite_and_mysql_ddl_are_single_atomic_add_column_statements() -> None:
    """Keep exact column defaults, collation, CHECK name, and DDL grouping visible."""
    migration = _load_migration(MIGRATION_PATH, "claim_policy_ddl_contract_0020")
    sqlite_ddl = migration._add_column_ddl("sqlite")
    mysql_ddl = migration._add_column_ddl("mysql")

    assert len(migration._CLAIM_POLICY_CONSTRAINT) == 29
    assert "VARCHAR(16) NOT NULL DEFAULT 'recover_expired'" in sqlite_ddl
    assert 'CONSTRAINT "ck_od_run_ledger_claim_policy" CHECK' in sqlite_ddl
    assert "('recover_expired', 'one_shot')" in sqlite_ddl
    assert mysql_ddl.count("ALTER TABLE") == 1
    assert mysql_ddl.count("ADD COLUMN") == 1
    assert mysql_ddl.count("ADD CONSTRAINT") == 1
    assert "CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin" in mysql_ddl
    assert "NOT NULL DEFAULT 'recover_expired'" in mysql_ddl
    assert "CHECK (`claim_policy` IN ('recover_expired', 'one_shot')) ENFORCED" in mysql_ddl
