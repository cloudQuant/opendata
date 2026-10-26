"""Exercise the frozen job-definition migration on owned temporary databases."""

from __future__ import annotations

import importlib.util
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError

from opendata.job_definition_store import JOB_DEFINITION_TABLE, JobDefinitionStore
from opendata.pipeline.run_identity import TaskDefinition

if TYPE_CHECKING:
    from collections.abc import Iterable
    from types import ModuleType

    from sqlalchemy.engine import Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPOSITORY_ROOT / "alembic" / "versions" / "2026_10_26-0007_job_definitions.py"
MIGRATION_NAME = "job_definition_migration_0007"
TABLE_NAME = "od_job_definitions"
ADJACENT_TABLES = ("pipeline_step_checkpoints", "pipeline_symbol_windows")
SHA256_FIELDS = ("identity_sha256", "provenance_sha256", "receipt_sha256")


def _load_migration() -> ModuleType:
    """Load the revision module without evaluating Alembic's application env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_database(tmp_path: Path) -> Engine:
    """Return a caller-owned temporary SQLite engine, isolated from settings."""
    return sa.create_engine(f"sqlite:///{tmp_path / 'job-definitions.sqlite'}")


def _apply_revision(engine: Engine, operation_name: str) -> None:
    """Run one revision function directly against the supplied test connection."""
    migration = _load_migration()
    operation = getattr(migration, operation_name)
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            operation()


def _create_adjacent_tables(engine: Engine) -> None:
    """Create representative tables owned by revision 0006."""
    metadata = sa.MetaData()
    sa.Table(
        "pipeline_step_checkpoints",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
    )
    sa.Table(
        "pipeline_symbol_windows",
        metadata,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
    )
    metadata.create_all(engine)


def _insert_values(
    *,
    legacy_task_id: str,
    job_id: str,
    version: str,
    overrides: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build one valid row payload for direct database constraint checks."""
    values: dict[str, object] = {
        "legacy_task_id": legacy_task_id,
        "job_id": job_id,
        "version": version,
        "definition_json": "{}",
        "identity_sha256": "1" * 64,
        "provenance_json": "{}",
        "provenance_sha256": "2" * 64,
        "receipt_sha256": "3" * 64,
        "created_at_us": 1_798_000_000_000_000,
    }
    if overrides:
        values.update(overrides)
    return values


def _assert_column_shape(engine: Engine) -> None:
    """Compare the migrated SQLite table's columns and constraints to the store."""
    inspector = sa.inspect(engine)
    columns = inspector.get_columns(TABLE_NAME)
    assert [str(column["name"]) for column in columns] == list(JOB_DEFINITION_TABLE.columns.keys())
    for reflected, declared in zip(columns, JOB_DEFINITION_TABLE.columns, strict=True):
        assert reflected["nullable"] is declared.nullable
        if isinstance(declared.type, sa.String):
            assert reflected["type"].length == declared.type.length
        elif isinstance(declared.type, sa.Text):
            assert isinstance(reflected["type"], sa.Text)
        elif isinstance(declared.type, sa.BigInteger):
            assert isinstance(reflected["type"], sa.BigInteger)

    primary_key = inspector.get_pk_constraint(TABLE_NAME)
    assert primary_key["constrained_columns"] == ["legacy_task_id"]
    indexes = inspector.get_indexes(TABLE_NAME)
    assert any(
        index["name"] == "ux_od_job_definitions_job_version"
        and index["unique"]
        and index["column_names"] == ["job_id", "version"]
        for index in indexes
    )
    constraints = inspector.get_check_constraints(TABLE_NAME)
    assert {constraint["name"] for constraint in constraints} == {
        "ck_od_job_def_identity_sha_len",
        "ck_od_job_def_provenance_sha_len",
        "ck_od_job_def_receipt_sha_len",
    }


def _assert_tables_present(engine: Engine, expected: Iterable[str]) -> None:
    """Assert each named table remains present in the owned database."""
    actual = set(sa.inspect(engine).get_table_names())
    assert set(expected) <= actual


def _normalize_sql(statement: str) -> str:
    """Normalize whitespace before comparing equivalent compiled DDL."""
    return " ".join(statement.split())


def test_upgrade_downgrade_upgrade_preserves_neighbor_tables_and_store_interop(
    tmp_path: Path,
) -> None:
    """Replay the migration and use the resulting table through JobDefinitionStore."""
    engine = _new_database(tmp_path)
    _create_adjacent_tables(engine)

    try:
        _apply_revision(engine, "upgrade")
        _assert_tables_present(engine, (*ADJACENT_TABLES, TABLE_NAME))
        _assert_column_shape(engine)

        store = JobDefinitionStore(engine)
        task = TaskDefinition(
            job_id="market.daily_bars.refresh",
            version="v1",
            dataset_id="market.daily_bars",
            provider="openbb",
            model="DailyBarsFetcher",
        )
        saved = store._save_reviewed_definition(
            legacy_task_id="scheduled:7",
            definition=task,
            provenance={"source": "migration-fixture"},
            receipt_sha256="a" * 64,
            verify_target_unchanged=lambda: None,
        )
        assert saved.created is True
        assert store.get("scheduled:7") == saved.record
        assert store.list_all() == (saved.record,)

        _apply_revision(engine, "downgrade")
        assert TABLE_NAME not in sa.inspect(engine).get_table_names()
        _assert_tables_present(engine, ADJACENT_TABLES)

        _apply_revision(engine, "upgrade")
        _assert_tables_present(engine, (*ADJACENT_TABLES, TABLE_NAME))
        _assert_column_shape(engine)
        assert JobDefinitionStore(engine).list_all() == ()
    finally:
        engine.dispose()


def test_sqlite_migration_enforces_unique_identity_and_sha256_lengths(tmp_path: Path) -> None:
    """The migration must enforce the store's version key and digest lengths."""
    engine = _new_database(tmp_path)
    _apply_revision(engine, "upgrade")
    try:
        with engine.begin() as connection:
            connection.execute(
                JOB_DEFINITION_TABLE.insert().values(
                    **_insert_values(
                        legacy_task_id="scheduled:1",
                        job_id="market.daily_bars.refresh",
                        version="v1",
                    )
                )
            )

        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                JOB_DEFINITION_TABLE.insert().values(
                    **_insert_values(
                        legacy_task_id="scheduled:2",
                        job_id="market.daily_bars.refresh",
                        version="v1",
                    )
                )
            )

        for index, field in enumerate(SHA256_FIELDS, start=1):
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(
                    JOB_DEFINITION_TABLE.insert().values(
                        **_insert_values(
                            legacy_task_id=f"invalid-digest:{field}",
                            job_id=f"market.invalid_digest_{index}",
                            version="v1",
                            overrides={field: "f" * 63},
                        )
                    )
                )
    finally:
        engine.dispose()


def test_mysql_ddl_compiles_with_binary_keys_innodb_and_mediumtext() -> None:
    """Compile the frozen revision for MySQL without connecting to a server."""
    buffer = StringIO()
    context = MigrationContext.configure(
        dialect=mysql.dialect(),
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        _load_migration().upgrade()

    ddl = buffer.getvalue()
    migration_statements = [statement.strip() for statement in ddl.split(";") if statement.strip()]
    store_index = next(
        index
        for index in JOB_DEFINITION_TABLE.indexes
        if index.name == "ux_od_job_definitions_job_version"
    )
    expected_table = str(
        sa.schema.CreateTable(JOB_DEFINITION_TABLE).compile(dialect=mysql.dialect())
    )
    expected_index = str(sa.schema.CreateIndex(store_index).compile(dialect=mysql.dialect()))

    assert len(migration_statements) == 2
    assert _normalize_sql(migration_statements[0]) == _normalize_sql(expected_table)
    assert _normalize_sql(migration_statements[1]) == _normalize_sql(expected_index)
    assert "ENGINE=InnoDB" in ddl
    assert "CHARSET=utf8mb4" in ddl
    assert ddl.count("COLLATE utf8mb4_bin") == 3
    assert ddl.count("MEDIUMTEXT") == 2
    assert "CREATE UNIQUE INDEX ux_od_job_definitions_job_version" in ddl
    for field in SHA256_FIELDS:
        assert f"length({field}) = 64" in ddl


def test_revision_graph_has_one_head_at_0007_without_loading_default_env() -> None:
    """The new revision is the sole head and directly follows 0006."""
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)

    assert script.get_heads() == ["0007"]
    revision = script.get_revision("0007")
    assert revision is not None
    assert revision.down_revision == "0006"
