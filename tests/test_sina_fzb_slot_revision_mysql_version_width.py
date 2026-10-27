"""Guard Alembic's version marker before the MySQL-only 0014 table DDL."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.mysql import VARCHAR
from sqlalchemy.dialects.mysql import dialect as mysql_dialect

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261027-0014_sina_fzb_slot_reconciliation.py"
MIGRATION_NAME = "sina_fzb_revision_mysql_version_width_0014"
STATE_TABLE = "od_sina_fzb_slot_reconciliation_state"
SLOTS_TABLE = "od_sina_fzb_reconciled_slots"
PARENT_REVISION = "0013_sina_fzb_fanout_groups"


class _ScalarRows:
    """Expose the scalar result contract used by the migration preflight."""

    def __init__(self, values: list[str]) -> None:
        self._values = values

    def scalars(self) -> _ScalarRows:
        return self

    def all(self) -> list[str]:
        return list(self._values)


class _SimulatedBind:
    """Record reads while exposing the configured MySQL dialect metadata."""

    def __init__(self, dialect: SimpleNamespace, version_rows: list[str]) -> None:
        self.dialect = dialect
        self.version_rows = version_rows
        self.statements: list[Any] = []

    def execute(self, statement: Any) -> _ScalarRows:
        self.statements.append(statement)
        return _ScalarRows(self.version_rows)


class _SimulatedInspector:
    """Return explicit version-table metadata without opening a database."""

    def __init__(
        self,
        *,
        version_type: object | None = None,
        nullable: bool = False,
        primary_key: list[str] | None = None,
        version_table_exists: bool = True,
        existing_tables: set[str] | None = None,
        default: str | None = None,
        extra_column: bool = False,
    ) -> None:
        self.version_type = (
            version_type
            if version_type is not None
            else VARCHAR(32, charset="utf8mb4", collation="utf8mb4_0900_bin")
        )
        self.nullable = nullable
        self.primary_key = primary_key if primary_key is not None else ["version_num"]
        self.version_table_exists = version_table_exists
        self.existing_tables = existing_tables or set()
        self.default = default
        self.extra_column = extra_column
        self.calls: list[tuple[str, str | None]] = []

    def has_table(self, name: str, schema: str | None = None) -> bool:
        self.calls.append((name, schema))
        if name in {STATE_TABLE, SLOTS_TABLE}:
            return name in self.existing_tables
        return self.version_table_exists

    def get_columns(self, name: str, schema: str | None = None) -> list[dict[str, object]]:
        self.calls.append((name, schema))
        columns: list[dict[str, object]] = [
            {
                "name": "version_num",
                "type": self.version_type,
                "nullable": self.nullable,
                "default": self.default,
                "comment": "version marker",
            }
        ]
        if self.extra_column:
            columns.append(
                {
                    "name": "unexpected_metadata",
                    "type": VARCHAR(16),
                    "nullable": False,
                    "default": None,
                    "comment": None,
                }
            )
        return columns

    def get_pk_constraint(self, name: str, schema: str | None = None) -> dict[str, object]:
        self.calls.append((name, schema))
        return {"constrained_columns": self.primary_key}


def _load_migration() -> ModuleType:
    """Load exactly revision 0014 without entering the project Alembic env."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _patch_mysql_target(
    monkeypatch: pytest.MonkeyPatch,
    migration: ModuleType,
    *,
    version_type: object | None = None,
    existing_tables: set[str] | None = None,
    version_rows: list[str] | None = None,
    version_table_exists: bool = True,
    nullable: bool = False,
    primary_key: list[str] | None = None,
    default: str | None = None,
    server_version: tuple[int, ...] | None = (8, 0, 36),
    dialect_name: str = "mysql",
    is_mariadb: bool = False,
    as_sql: bool = False,
    extra_column: bool = False,
) -> tuple[
    list[tuple[str, tuple[object, ...], dict[str, object]]], _SimulatedBind, _SimulatedInspector
]:
    """Install deterministic MySQL metadata and record all schema writes."""
    dialect = SimpleNamespace(
        name=dialect_name,
        server_version_info=server_version,
        is_mariadb=is_mariadb,
    )
    context = SimpleNamespace(
        dialect=dialect,
        as_sql=as_sql,
        version_table="alembic-version.data",
        version_table_schema="metadata-zone",
    )
    bind = _SimulatedBind(
        dialect,
        version_rows if version_rows is not None else [PARENT_REVISION],
    )
    inspector = _SimulatedInspector(
        version_type=version_type,
        nullable=nullable,
        primary_key=primary_key,
        version_table_exists=version_table_exists,
        existing_tables=existing_tables,
        default=default,
        extra_column=extra_column,
    )
    writes: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    monkeypatch.setattr(migration.op, "get_context", lambda: context)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    monkeypatch.setattr(migration, "inspect", lambda _bind: inspector)
    for operation in ("alter_column", "create_table", "create_index"):
        monkeypatch.setattr(
            migration.op,
            operation,
            lambda *args, _operation=operation, **kwargs: writes.append((_operation, args, kwargs)),
        )
    return writes, bind, inspector


def test_mysql_widens_exact_version_column_before_0014_table_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live 0013 marker is safely widened before either target table is created."""
    migration = _load_migration()
    original_type = VARCHAR(32, charset="utf8mb4", collation="utf8mb4_0900_bin")
    writes, bind, inspector = _patch_mysql_target(
        monkeypatch,
        migration,
        version_type=original_type,
    )

    migration.upgrade()

    assert [write[0] for write in writes] == [
        "alter_column",
        "create_table",
        "create_table",
        "create_index",
    ]
    operation, args, kwargs = writes[0]
    assert operation == "alter_column"
    assert args == ("alembic-version.data", "version_num")
    assert kwargs["schema"] == "metadata-zone"
    assert kwargs["existing_type"] is original_type
    widened_type = kwargs["type_"]
    assert isinstance(widened_type, VARCHAR)
    assert widened_type.length == 64
    assert widened_type.charset == original_type.charset
    assert widened_type.collation == original_type.collation
    assert kwargs["existing_nullable"] is False
    assert kwargs["existing_comment"] == "version marker"
    assert inspector.calls[:2] == [(STATE_TABLE, None), (SLOTS_TABLE, None)]

    statement = bind.statements[0]
    assert statement.get_final_froms()[0].name == "alembic-version.data"
    assert statement.get_final_froms()[0].schema == "metadata-zone"
    rendered = str(statement.compile(dialect=mysql_dialect()))
    assert "`metadata-zone`.`alembic-version.data`" in rendered
    assert "version_num" in rendered
    assert migration.revision == "0014_sina_fzb_slot_reconciliation"
    assert migration.down_revision == PARENT_REVISION


def test_mysql_keeps_an_already_wide_version_column_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wider pre-existing marker column is never narrowed or needlessly altered."""
    migration = _load_migration()
    writes, _bind, _inspector = _patch_mysql_target(
        monkeypatch,
        migration,
        version_type=VARCHAR(96, charset="utf8mb4"),
    )

    migration.upgrade()

    assert [write[0] for write in writes] == ["create_table", "create_table", "create_index"]


@pytest.mark.parametrize("existing_table", [STATE_TABLE, SLOTS_TABLE])
def test_mysql_partial_or_prior_0014_tables_block_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    existing_table: str,
) -> None:
    """Any extant 0014 table requires offline review rather than automatic retry."""
    migration = _load_migration()
    writes, _bind, _inspector = _patch_mysql_target(
        monkeypatch,
        migration,
        existing_tables={existing_table},
    )

    with pytest.raises(RuntimeError, match="possible partial or prior DDL"):
        migration.upgrade()

    assert writes == []


@pytest.mark.parametrize(
    (
        "version_type",
        "nullable",
        "primary_key",
        "version_table_exists",
        "default",
        "rows",
        "extra_column",
    ),
    [
        (sa.Integer(), False, ["version_num"], True, None, [PARENT_REVISION], False),
        (VARCHAR(16), False, ["version_num"], True, None, [PARENT_REVISION], False),
        (VARCHAR(32), True, ["version_num"], True, None, [PARENT_REVISION], False),
        (VARCHAR(32), False, [], True, None, [PARENT_REVISION], False),
        (VARCHAR(32), False, ["version_num"], False, None, [PARENT_REVISION], False),
        (VARCHAR(32), False, ["version_num"], True, "unexpected", [PARENT_REVISION], False),
        (VARCHAR(32), False, ["version_num"], True, None, ["0014_sina_fzb_reconciliatio"], False),
        (
            VARCHAR(32),
            False,
            ["version_num"],
            True,
            None,
            [PARENT_REVISION, PARENT_REVISION],
            False,
        ),
        (VARCHAR(32), False, ["version_num"], True, None, [], False),
        (VARCHAR(32), False, ["version_num"], True, None, [PARENT_REVISION], True),
    ],
)
def test_mysql_unknown_version_table_shape_or_value_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    version_type: object,
    nullable: bool,
    primary_key: list[str],
    version_table_exists: bool,
    default: str | None,
    rows: list[str],
    extra_column: bool,
) -> None:
    """Unknown marker metadata and unexpected rows cannot reach ALTER or create_table."""
    migration = _load_migration()
    writes, _bind, _inspector = _patch_mysql_target(
        monkeypatch,
        migration,
        version_type=version_type,
        nullable=nullable,
        primary_key=primary_key,
        version_table_exists=version_table_exists,
        default=default,
        version_rows=rows,
        extra_column=extra_column,
    )

    with pytest.raises(RuntimeError):
        migration.upgrade()

    assert writes == []


@pytest.mark.parametrize(
    ("dialect_name", "server_version", "is_mariadb", "error"),
    [
        ("mysql", (8, 0, 15), False, "MySQL 8.0.16"),
        ("mysql", (9, 0, 0), False, "MySQL 8.0.16"),
        ("mysql", None, False, "MySQL 8.0.16"),
        ("mysql", (10, 11, 0), True, "MariaDB"),
        ("postgresql", (16, 4, 0), False, "SQLite and MySQL 8"),
    ],
)
def test_unverified_mysql_version_or_dialect_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    server_version: tuple[int, ...] | None,
    is_mariadb: bool,
    error: str,
) -> None:
    """Only supported MySQL 8 and unchanged SQLite behavior are admitted."""
    migration = _load_migration()
    writes, _bind, _inspector = _patch_mysql_target(
        monkeypatch,
        migration,
        dialect_name=dialect_name,
        server_version=server_version,
        is_mariadb=is_mariadb,
    )

    with pytest.raises(RuntimeError, match=error):
        migration.upgrade()

    assert writes == []


def test_mysql_offline_render_is_rejected_before_inspection_or_ddl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline SQL cannot bypass the live partial-DDL and marker-shape guards."""
    migration = _load_migration()
    writes, _bind, inspector = _patch_mysql_target(monkeypatch, migration, as_sql=True)

    with pytest.raises(RuntimeError, match="offline SQL generation is unsupported"):
        migration.upgrade()

    assert writes == []
    assert inspector.calls == []


def test_sqlite_migration_path_keeps_revision_ids_and_does_not_require_version_table() -> None:
    """SQLite can still run the original schema path without Alembic marker setup."""
    migration = _load_migration()
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()

            names = set(sa.inspect(connection).get_table_names())
            assert {STATE_TABLE, SLOTS_TABLE} <= names
            assert "alembic_version" not in names
            assert migration.revision == "0014_sina_fzb_slot_reconciliation"
            assert migration.down_revision == PARENT_REVISION
    finally:
        engine.dispose()
