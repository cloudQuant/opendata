"""Exercise the narrowly scoped 0017 publication index migration."""

from __future__ import annotations

import importlib.util
import re
import typing
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects.mysql import TINYINT
from sqlalchemy.dialects.mysql import dialect as mysql_dialect

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType
    from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = ROOT / "alembic_data/versions/20261028-0017_sina_capture_discovery_index.py"
MIGRATION_NAME = "sina_capture_discovery_index_migration_0017"
PARENT_REVISION = "0016_legacy_turnover_nullable"
REVISION = "0017_sina_capture_discovery_index"
VERSION_TABLE = "alembic_version_data"
FACT_TABLE = "od_published_fact_versions"
OLD_INDEX = "ix_od_fact_capture_read"
DISCOVERY_INDEX = "ix_od_fact_capture_discovery"
COLLATION_PROBE_INDEX = "ix_od_fact_capture_discovery_probe"
DISCOVERY_COLUMNS = (
    "dataset_id",
    "publication_id",
    "instrument_id",
    "publication_sequence",
    "is_deleted",
    "observed_at",
    "capture_id",
)


def _load_migration() -> ModuleType:
    """Load only the revision under test, without evaluating Alembic env.py."""
    spec = importlib.util.spec_from_file_location(MIGRATION_NAME, MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_type_only_migration_imports_load_when_type_checking_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The migration's annotation-only names remain valid for type tooling."""
    monkeypatch.setattr(typing, "TYPE_CHECKING", True)
    migration = _load_migration()
    assert migration.Mapping is not None
    assert migration.Any is not None


def _create_sqlite_target(
    connection: sa.Connection,
    *,
    marker: str = PARENT_REVISION,
    dataset_id_nocase: bool = False,
) -> None:
    """Create a private SQLite schema matching the reviewed 0016 head."""
    connection.exec_driver_sql(
        f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
    )
    connection.execute(
        sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
        {"marker": marker},
    )
    fact_table_ddl = f"""CREATE TABLE {FACT_TABLE} (
            dataset_id VARCHAR(128) NOT NULL,
            business_key VARCHAR(512) NOT NULL,
            revision INTEGER NOT NULL,
            publication_id VARCHAR(32) NOT NULL,
            publication_sequence BIGINT NOT NULL,
            instrument_id VARCHAR(128),
            capture_id VARCHAR(64),
            business_date DATE,
            event_at VARCHAR(40),
            observed_at VARCHAR(40),
            source_published_at VARCHAR(40),
            available_at VARCHAR(40),
            record_json TEXT NOT NULL,
            record_hash VARCHAR(64) NOT NULL,
            is_deleted BOOLEAN NOT NULL,
            PRIMARY KEY (dataset_id, business_key, revision)
        )"""
    if dataset_id_nocase:
        fact_table_ddl = fact_table_ddl.replace(
            "dataset_id VARCHAR(128) NOT NULL",
            "dataset_id VARCHAR(128) COLLATE NOCASE NOT NULL",
            1,
        )
    connection.exec_driver_sql(fact_table_ddl)
    connection.exec_driver_sql(
        f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
        "(dataset_id, instrument_id, capture_id, publication_sequence)"
    )


def _run_sqlite_revision(connection: sa.Connection, operation_name: str) -> None:
    """Invoke one migration operation on the caller-owned SQLite transaction."""
    migration = _load_migration()
    context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
    with Operations.context(context):
        getattr(migration, operation_name)()


def _check_revision_marker(
    connection: sa.Connection,
    expected: str,
    *,
    version_table: str = VERSION_TABLE,
    mode: str = "sqlite",
) -> None:
    """Exercise the revision-marker guard against a private SQLite schema."""
    migration = _load_migration()
    context = MigrationContext.configure(connection, opts={"version_table": version_table})
    with Operations.context(context):
        migration._require_revision_marker(expected, mode)


def _new_sqlite_connection(tmp_path: Path) -> sa.Connection:
    """Return a connection to a fresh test-owned SQLite file."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'capture-discovery-0017.sqlite'}")
    connection = engine.connect()
    _create_sqlite_target(connection)
    return connection


def test_revision_follows_0016_and_sqlite_upgrade_preserves_rows(tmp_path: Path) -> None:
    """The new index is created over the exact seven-column order without rewriting data."""
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == PARENT_REVISION

    connection = _new_sqlite_connection(tmp_path)
    try:
        connection.execute(
            sa.text(
                "INSERT INTO od_published_fact_versions "
                "(dataset_id, business_key, revision, publication_id, publication_sequence, "
                "instrument_id, capture_id, observed_at, record_json, record_hash, is_deleted) "
                "VALUES ('d', 'k', 1, 'p', 2, 'i', 'c', '2026-01-01T00:00:00Z', '{}', 'h', 0)"
            )
        )
        _run_sqlite_revision(connection, "upgrade")

        indexes = {
            item["name"]: tuple(item["column_names"])
            for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
        assert indexes[OLD_INDEX] == (
            "dataset_id",
            "instrument_id",
            "capture_id",
            "publication_sequence",
        )
        assert indexes[DISCOVERY_INDEX] == DISCOVERY_COLUMNS
        assert COLLATION_PROBE_INDEX not in indexes
        assert connection.execute(
            sa.text(
                "SELECT record_json, observed_at FROM od_published_fact_versions "
                "WHERE business_key = 'k'"
            )
        ).one() == ("{}", "2026-01-01T00:00:00Z")
    finally:
        connection.close()


@pytest.mark.parametrize("marker", ["0015_source_attempt_gate", "unexpected", ""])
def test_sqlite_upgrade_rejects_unexpected_or_stale_version_marker(
    tmp_path: Path, marker: str
) -> None:
    """A stale marker blocks before the discovery index is added."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bad-marker.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection, marker=marker)
        with pytest.raises(RuntimeError, match="requires exactly one Alembic marker"):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_version_table_without_primary_key(tmp_path: Path) -> None:
    """A marker table without its sole version primary key is not accepted."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'unkeyed-marker.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql("DROP TABLE alembic_version_data")
        connection.exec_driver_sql(
            "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL)"
        )
        connection.exec_driver_sql(
            "INSERT INTO alembic_version_data (version_num) "
            "VALUES ('0016_legacy_turnover_nullable')"
        )
        with pytest.raises(RuntimeError, match="sole version-table primary key"):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_missing_version_table_before_ddl(tmp_path: Path) -> None:
    """A missing version marker table is a hard stop."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'no-marker-table.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql(f"DROP TABLE {VERSION_TABLE}")
        with pytest.raises(RuntimeError, match="requires the configured Alembic version table"):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_revision_marker_rejects_extra_version_column(tmp_path: Path) -> None:
    """The version table must have only its one reviewed marker column."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'extra-marker-column.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {VERSION_TABLE} ("
            "version_num VARCHAR(64) NOT NULL PRIMARY KEY, branch_label VARCHAR(32))"
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
            {"marker": PARENT_REVISION},
        )
        with pytest.raises(RuntimeError, match="containing only version_num"):
            _check_revision_marker(connection, PARENT_REVISION)
    engine.dispose()


def test_revision_marker_rejects_unbounded_or_wrong_type_version_column(
    tmp_path: Path,
) -> None:
    """A version marker with a non-string storage type cannot satisfy the guard."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'wrong-marker-type.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {VERSION_TABLE} (version_num INTEGER NOT NULL PRIMARY KEY)"
        )
        connection.exec_driver_sql("INSERT INTO alembic_version_data VALUES (16)")
        with pytest.raises(RuntimeError, match="bounded, non-null VARCHAR"):
            _check_revision_marker(connection, PARENT_REVISION)
    engine.dispose()


def test_mysql_revision_marker_rejects_too_short_version_column(tmp_path: Path) -> None:
    """A MySQL marker column must fit both exact revision identifiers."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'short-marker.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(8) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
            {"marker": PARENT_REVISION},
        )
        with pytest.raises(RuntimeError, match="wide enough for both revision IDs"):
            _check_revision_marker(connection, PARENT_REVISION, mode="mysql")
    engine.dispose()


def test_version_table_name_uses_only_the_reviewed_fallback() -> None:
    """An omitted name uses the warehouse default; blank names fail closed."""
    migration = _load_migration()
    assert migration._version_table_name(SimpleNamespace(version_table=None)) == VERSION_TABLE
    with pytest.raises(RuntimeError, match="valid Alembic version table name"):
        migration._version_table_name(SimpleNamespace(version_table="  "))


def test_sqlite_upgrade_rejects_missing_publication_table(tmp_path: Path) -> None:
    """A valid marker cannot make a missing target table appear safe."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'no-target.sqlite'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TABLE {VERSION_TABLE} (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
            {"marker": PARENT_REVISION},
        )
        with pytest.raises(RuntimeError, match="requires od_published_fact_versions to exist"):
            _run_sqlite_revision(connection, "upgrade")
    engine.dispose()


@pytest.mark.parametrize(
    ("sql_type", "expected_message"),
    [
        ("VARCHAR(127)", "instrument_id column type"),
        ("VARCHAR(64)", "observed_at column type"),
        ("INTEGER", "publication_sequence column type"),
    ],
)
def test_sqlite_upgrade_rejects_key_column_shape_drift(
    tmp_path: Path, sql_type: str, expected_message: str
) -> None:
    """The actual indexed fields and widths must match the reviewed contract."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'wrong-column.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        column_name = expected_message.split()[0]
        connection.exec_driver_sql(
            f"ALTER TABLE {FACT_TABLE} RENAME COLUMN {column_name} TO old_{column_name}"
        )
        connection.exec_driver_sql(f"ALTER TABLE {FACT_TABLE} ADD COLUMN {column_name} {sql_type}")
        with pytest.raises(RuntimeError, match=expected_message):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


@pytest.mark.parametrize(
    "index_sql",
    [
        f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
        "(dataset_id, capture_id, instrument_id, publication_sequence)",
        f"CREATE UNIQUE INDEX {OLD_INDEX} ON {FACT_TABLE} "
        "(dataset_id, instrument_id, capture_id, publication_sequence)",
        f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
        "(dataset_id, instrument_id, capture_id, publication_sequence DESC)",
        f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
        "(dataset_id, instrument_id, capture_id, publication_sequence) "
        "WHERE capture_id IS NOT NULL",
    ],
)
def test_sqlite_upgrade_rejects_wrong_existing_capture_index(
    tmp_path: Path, index_sql: str
) -> None:
    """The predecessor index is checked before any new index DDL."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'wrong-old-index.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql(f"DROP INDEX {OLD_INDEX}")
        connection.exec_driver_sql(index_sql)
        with pytest.raises(RuntimeError, match="reviewed SQLite ix_od_fact_capture_read"):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_missing_capture_index_before_ddl(tmp_path: Path) -> None:
    """A missing predecessor index blocks the migration before any new index DDL."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'missing-old-index.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql(f"DROP INDEX {OLD_INDEX}")
        with pytest.raises(
            RuntimeError, match="reviewed SQLite ix_od_fact_capture_read index shape"
        ):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_old_index_inherited_nocase_collation(
    tmp_path: Path,
) -> None:
    """A table column's inherited NOCASE comparison is rejected on the old index."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old-index-inherited-nocase.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection, dataset_id_nocase=True)
        with pytest.raises(RuntimeError, match="BINARY collation"):
            _run_sqlite_revision(connection, "upgrade")
        assert DISCOVERY_INDEX not in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_candidate_index_inherited_nocase_before_full_ddl(
    tmp_path: Path,
) -> None:
    """A BINARY old index cannot hide NOCASE inherited by the new index."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'candidate-index-inherited-nocase.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection, dataset_id_nocase=True)
        connection.exec_driver_sql(f"DROP INDEX {OLD_INDEX}")
        connection.exec_driver_sql(
            f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
            "(dataset_id COLLATE BINARY, instrument_id, capture_id, publication_sequence)"
        )
        with pytest.raises(RuntimeError, match="BINARY collation for every SQLite discovery key"):
            _run_sqlite_revision(connection, "upgrade")
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)}
        assert OLD_INDEX in indexes
        assert DISCOVERY_INDEX not in indexes
        assert "ix_od_fact_capture_discovery_probe" not in indexes
    engine.dispose()


def test_sqlite_downgrade_rejects_target_index_inherited_nocase_collation(
    tmp_path: Path,
) -> None:
    """A same-named target index inheriting NOCASE is not dropped."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'target-index-inherited-nocase.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection, marker=REVISION, dataset_id_nocase=True)
        connection.exec_driver_sql(f"DROP INDEX {OLD_INDEX}")
        connection.exec_driver_sql(
            f"CREATE INDEX {OLD_INDEX} ON {FACT_TABLE} "
            "(dataset_id COLLATE BINARY, instrument_id, capture_id, publication_sequence)"
        )
        connection.exec_driver_sql(
            f"CREATE INDEX {DISCOVERY_INDEX} ON {FACT_TABLE} ({', '.join(DISCOVERY_COLUMNS)})"
        )
        with pytest.raises(RuntimeError, match="BINARY collation"):
            _run_sqlite_revision(connection, "downgrade")
        assert DISCOVERY_INDEX in {
            item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
        }
    engine.dispose()


def test_sqlite_upgrade_rejects_preexisting_discovery_index(tmp_path: Path) -> None:
    """A prior 0017-shaped index with a stale marker needs offline reconciliation."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'partial-index.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql(
            f"CREATE INDEX {DISCOVERY_INDEX} ON {FACT_TABLE} ({', '.join(DISCOVERY_COLUMNS)})"
        )
        with pytest.raises(RuntimeError, match="pre-existing discovery index"):
            _run_sqlite_revision(connection, "upgrade")
    engine.dispose()


def test_sqlite_upgrade_rejects_and_preserves_preexisting_collation_probe(
    tmp_path: Path,
) -> None:
    """A stale probe is not adopted or deleted during partial-state recovery."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'stale-collation-probe.sqlite'}")
    with engine.begin() as connection:
        _create_sqlite_target(connection)
        connection.exec_driver_sql(
            f"CREATE INDEX {COLLATION_PROBE_INDEX} ON {FACT_TABLE} "
            f"({', '.join(DISCOVERY_COLUMNS)}) WHERE 0"
        )
        with pytest.raises(RuntimeError, match="stale SQLite collation probe"):
            _run_sqlite_revision(connection, "upgrade")
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)}
        assert OLD_INDEX in indexes
        assert COLLATION_PROBE_INDEX in indexes
        assert DISCOVERY_INDEX not in indexes
    engine.dispose()


def test_sqlite_downgrade_drops_only_the_reviewed_index(tmp_path: Path) -> None:
    """Downgrade preserves source records and the previous capture index."""
    connection = _new_sqlite_connection(tmp_path)
    try:
        connection.execute(
            sa.text(
                "INSERT INTO od_published_fact_versions "
                "(dataset_id, business_key, revision, publication_id, publication_sequence, "
                "instrument_id, capture_id, record_json, record_hash, is_deleted) "
                "VALUES ('d', 'k', 1, 'p', 2, 'i', 'c', '{}', 'h', 0)"
            )
        )
        _run_sqlite_revision(connection, "upgrade")
        connection.execute(
            sa.text("UPDATE alembic_version_data SET version_num = :marker"),
            {"marker": REVISION},
        )
        _run_sqlite_revision(connection, "downgrade")
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)}
        assert OLD_INDEX in indexes
        assert DISCOVERY_INDEX not in indexes
        assert (
            connection.execute(
                sa.text("SELECT COUNT(*) FROM od_published_fact_versions WHERE business_key = 'k'")
            ).scalar_one()
            == 1
        )
    finally:
        connection.close()


def test_sqlite_downgrade_rejects_a_wrong_target_index_shape(tmp_path: Path) -> None:
    """A same-named index is not dropped unless every key part matches."""
    wrong_definitions = [
        f"CREATE INDEX {DISCOVERY_INDEX} ON {FACT_TABLE} "
        "(publication_id, dataset_id, instrument_id, publication_sequence, "
        "is_deleted, observed_at, capture_id)",
        f"CREATE UNIQUE INDEX {DISCOVERY_INDEX} ON {FACT_TABLE} "
        "(dataset_id, publication_id, instrument_id, publication_sequence, "
        "is_deleted, observed_at, capture_id)",
        f"CREATE INDEX {DISCOVERY_INDEX} ON {FACT_TABLE} "
        "(dataset_id, publication_id, instrument_id, publication_sequence, "
        "is_deleted, observed_at, capture_id DESC)",
    ]
    for position, definition in enumerate(wrong_definitions):
        engine = sa.create_engine(f"sqlite:///{tmp_path / f'wrong-target-index-{position}.sqlite'}")
        with engine.begin() as connection:
            _create_sqlite_target(connection, marker=REVISION)
            connection.exec_driver_sql(definition)
            with pytest.raises(
                RuntimeError,
                match="reviewed SQLite ix_od_fact_capture_discovery index shape",
            ):
                _run_sqlite_revision(connection, "downgrade")
            assert DISCOVERY_INDEX in {
                item["name"] for item in sa.inspect(connection).get_indexes(FACT_TABLE)
            }
        engine.dispose()


@pytest.mark.parametrize(
    ("dialect_name", "server_version", "mariadb", "offline", "message"),
    [
        ("postgresql", (16, 1), False, False, "supports SQLite and MySQL"),
        ("mysql", (8, 0, 15), False, False, "requires MySQL 8.0.16"),
        ("mysql", (9, 0, 0), False, False, "requires MySQL 8.0.16"),
        ("mysql", (10, 11, 0), True, False, "not MariaDB"),
        ("mysql", (8, 0, 16), True, False, "not MariaDB"),
        ("mysql", (8, 0), False, False, "requires MySQL 8.0.16"),
        ("mysql", (8, 0, 16.1), False, False, "requires MySQL 8.0.16"),
        ("sqlite", None, False, True, "requires a live connection"),
    ],
)
def test_supported_target_rejects_unreviewed_modes(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    server_version: tuple[object, ...] | None,
    mariadb: bool,
    offline: bool,
    message: str,
) -> None:
    """No backend other than the reviewed live SQLite/MySQL targets proceeds."""
    migration = _load_migration()
    context = SimpleNamespace(
        as_sql=offline,
        dialect=SimpleNamespace(
            name=dialect_name,
            server_version_info=server_version,
            is_mariadb=mariadb,
        ),
    )
    monkeypatch.setattr(migration.op, "get_context", lambda: context)
    with pytest.raises(RuntimeError, match=message):
        migration._require_supported_target()


def test_supported_target_accepts_mysql_80016(monkeypatch: pytest.MonkeyPatch) -> None:
    """The minimum reviewed MySQL 8 release is accepted."""
    migration = _load_migration()
    context = SimpleNamespace(
        as_sql=False,
        dialect=SimpleNamespace(
            name="mysql",
            server_version_info=(8, 0, 16),
            is_mariadb=False,
        ),
    )
    monkeypatch.setattr(migration.op, "get_context", lambda: context)
    assert migration._require_supported_target() == "mysql"


@pytest.mark.parametrize(
    ("operation_name", "expected_marker", "downgrade", "drop"),
    [
        ("upgrade", PARENT_REVISION, False, False),
        ("downgrade", REVISION, True, True),
    ],
)
def test_mysql_revision_dispatches_only_to_reviewed_online_ddl(
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
    expected_marker: str,
    downgrade: bool,
    drop: bool,
) -> None:
    """MySQL revision entry points validate state before the online index DDL."""
    migration = _load_migration()
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(migration, "_require_supported_target", lambda: "mysql")
    monkeypatch.setattr(
        migration,
        "_require_revision_marker",
        lambda marker, mode: calls.append(("marker", marker, mode)),
    )
    monkeypatch.setattr(
        migration,
        "_require_table_and_indexes",
        lambda mode, *, downgrade: calls.append(("shape", mode, downgrade)),
    )
    monkeypatch.setattr(
        migration,
        "_mysql_index_ddl",
        lambda *, drop: calls.append(("ddl", drop)) or "ALTER TABLE ...",
    )
    monkeypatch.setattr(
        migration,
        "_execute_mysql_online_ddl",
        lambda statement: calls.append(("execute", statement)),
    )

    getattr(migration, operation_name)()

    assert calls == [
        ("marker", expected_marker, "mysql"),
        ("shape", "mysql", downgrade),
        ("ddl", drop),
        ("execute", "ALTER TABLE ..."),
    ]


def test_mysql_ddl_requires_inplace_none_and_quotes_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Generated MySQL statements explicitly prohibit copy and exclusive DDL."""
    migration = _load_migration()
    bind = SimpleNamespace(dialect=mysql_dialect())
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)

    statement = migration._mysql_index_ddl(drop=False)
    assert "ALGORITHM=INPLACE, LOCK=NONE" in statement
    assert f"ADD INDEX `{DISCOVERY_INDEX}`" in statement
    assert "`publication_id`" in statement and "`capture_id`" in statement
    assert "COPY" not in statement and "LOCK=EXCLUSIVE" not in statement
    drop_statement = migration._mysql_index_ddl(drop=True)
    assert f"DROP INDEX `{DISCOVERY_INDEX}`" in drop_statement
    assert "ALGORITHM=INPLACE, LOCK=NONE" in drop_statement


class _DriverResult:
    """Expose the scalar contract used to read the MySQL session timeout."""

    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one(self) -> object:
        return self.value


class _DriverBind:
    """Record MySQL driver SQL and optionally fail the online DDL statement."""

    def __init__(
        self,
        *,
        timeout: object = 120,
        fail_ddl: bool = False,
        fail_restore: bool = False,
        fail_initial_set: bool = False,
    ) -> None:
        self.timeout = timeout
        self.fail_ddl = fail_ddl
        self.fail_restore = fail_restore
        self.fail_initial_set = fail_initial_set
        self.statements: list[str] = []
        self.dialect = mysql_dialect()

    def exec_driver_sql(self, statement: str) -> _DriverResult:
        self.statements.append(statement)
        if statement.startswith("SELECT"):
            return _DriverResult(self.timeout)
        if statement == "SET SESSION lock_wait_timeout = 10" and self.fail_initial_set:
            raise RuntimeError("session timeout set rejected")
        if statement == "SET SESSION lock_wait_timeout = 120" and self.fail_restore:
            raise RuntimeError("session timeout restore rejected")
        if statement.startswith("ALTER TABLE") and self.fail_ddl:
            raise RuntimeError("online DDL rejected")
        return _DriverResult(None)


@pytest.mark.parametrize("fail_ddl", [False, True])
def test_mysql_online_ddl_bounds_and_restores_metadata_lock_timeout(
    monkeypatch: pytest.MonkeyPatch, fail_ddl: bool
) -> None:
    """The migration changes only a session timeout and restores it on either outcome."""
    migration = _load_migration()
    bind = _DriverBind(fail_ddl=fail_ddl)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    statement = migration._mysql_index_ddl(drop=False)

    if fail_ddl:
        with pytest.raises(RuntimeError, match="online DDL rejected"):
            migration._execute_mysql_online_ddl(statement)
    else:
        migration._execute_mysql_online_ddl(statement)

    assert bind.statements[0] == "SELECT @@SESSION.lock_wait_timeout"
    assert bind.statements[1] == "SET SESSION lock_wait_timeout = 10"
    assert bind.statements[2] == statement
    assert bind.statements[-1] == "SET SESSION lock_wait_timeout = 120"


@pytest.mark.parametrize("timeout", [0, "120", None])
def test_mysql_online_ddl_refuses_unreadable_session_timeout(
    monkeypatch: pytest.MonkeyPatch, timeout: object
) -> None:
    """The migration never begins DDL unless it can restore the prior timeout."""
    migration = _load_migration()
    bind = _DriverBind(timeout=timeout)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="cannot safely inspect"):
        migration._execute_mysql_online_ddl("ALTER TABLE t ADD INDEX i (c)")
    assert len(bind.statements) == 1


def test_mysql_online_ddl_does_not_run_ddl_when_setting_timeout_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed session guard stops before the schema-changing statement."""
    migration = _load_migration()
    bind = _DriverBind(fail_initial_set=True)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="session timeout set rejected"):
        migration._execute_mysql_online_ddl("ALTER TABLE t ADD INDEX i (c)")
    assert bind.statements == [
        "SELECT @@SESSION.lock_wait_timeout",
        "SET SESSION lock_wait_timeout = 10",
    ]


def test_mysql_online_ddl_reports_failed_restore_after_ddl_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DDL failure plus failed restoration is surfaced as an operator action."""
    migration = _load_migration()
    bind = _DriverBind(fail_ddl=True, fail_restore=True)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="could not be restored"):
        migration._execute_mysql_online_ddl("ALTER TABLE t ADD INDEX i (c)")
    assert bind.statements[-1] == "SET SESSION lock_wait_timeout = 120"


def test_mysql_online_ddl_reports_restore_failure_after_ddl_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful DDL still fails the migration if session state cannot be restored."""
    migration = _load_migration()
    bind = _DriverBind(fail_restore=True)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    with pytest.raises(RuntimeError, match="session timeout restore rejected"):
        migration._execute_mysql_online_ddl("ALTER TABLE t ADD INDEX i (c)")
    assert bind.statements[-2:] == [
        "ALTER TABLE t ADD INDEX i (c)",
        "SET SESSION lock_wait_timeout = 120",
    ]


class _MappingRows:
    """Expose the mapping result methods used by MySQL metadata guards."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = list(rows)

    def mappings(self) -> _MappingRows:
        return self

    def all(self) -> list[Mapping[str, object]]:
        return list(self.rows)

    def one_or_none(self) -> Mapping[str, object] | None:
        if len(self.rows) > 1:
            raise AssertionError("expected at most one metadata row")
        return self.rows[0] if self.rows else None


class _MetadataBind:
    """Return a pre-built metadata snapshot without opening a MySQL connection."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = list(rows)
        self.statements: list[str] = []

    def execute(
        self, statement: Any, parameters: Mapping[str, object] | None = None
    ) -> _MappingRows:
        del parameters
        self.statements.append(str(statement))
        return _MappingRows(self.rows)


class _PublicationInspector:
    """Expose a reviewed MySQL fact-table shape without opening a database."""

    def __init__(self, *, engine: str = "InnoDB", valid_primary_key: bool = True) -> None:
        self.engine = engine
        self.valid_primary_key = valid_primary_key
        self.columns = [
            {"name": "dataset_id", "type": sa.String(128), "nullable": False},
            {"name": "publication_id", "type": sa.String(32), "nullable": False},
            {"name": "instrument_id", "type": sa.String(128), "nullable": True},
            {
                "name": "publication_sequence",
                "type": sa.BigInteger(),
                "nullable": False,
            },
            {
                "name": "is_deleted",
                "type": TINYINT(display_width=1),
                "nullable": False,
            },
            {"name": "observed_at", "type": sa.String(40), "nullable": True},
            {"name": "capture_id", "type": sa.String(64), "nullable": True},
        ]

    def has_table(self, _table_name: str) -> bool:
        return True

    def get_columns(self, _table_name: str) -> list[dict[str, object]]:
        return self.columns

    def get_pk_constraint(self, _table_name: str) -> dict[str, object]:
        columns = (
            ["dataset_id", "business_key", "revision"]
            if self.valid_primary_key
            else ["dataset_id", "publication_id"]
        )
        return {"constrained_columns": columns}

    def get_table_options(self, _table_name: str) -> dict[str, object]:
        return {"mysql_engine": self.engine}


def _exercise_mysql_table_guard(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inspector: _PublicationInspector | None = None,
    target_statistics: tuple[Mapping[str, object], ...] = (),
    downgrade: bool = False,
) -> list[tuple[str, tuple[str, ...]]]:
    """Run only the pure MySQL shape orchestration against fake metadata."""
    migration = _load_migration()
    observed_shapes: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(migration, "inspect", lambda _bind: inspector or _PublicationInspector())
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(migration, "_require_mysql_storage_budget", lambda: None)
    monkeypatch.setattr(
        migration,
        "_require_mysql_index_shape",
        lambda name, columns: observed_shapes.append((name, columns)),
    )
    monkeypatch.setattr(migration, "_mysql_statistics", lambda _name: target_statistics)
    migration._require_table_and_indexes("mysql", downgrade=downgrade)
    return observed_shapes


def test_mysql_table_guard_requires_the_old_index_and_empty_upgrade_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Upgrade validates the old index and refuses to adopt an existing target."""
    shapes = _exercise_mysql_table_guard(monkeypatch)
    assert shapes == [
        (OLD_INDEX, ("dataset_id", "instrument_id", "capture_id", "publication_sequence"))
    ]

    with pytest.raises(RuntimeError, match="pre-existing discovery index"):
        _exercise_mysql_table_guard(
            monkeypatch, target_statistics=({"INDEX_NAME": DISCOVERY_INDEX},)
        )


def test_mysql_table_guard_requires_the_reviewed_target_index_on_downgrade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrade validates both the predecessor and complete target index shapes."""
    shapes = _exercise_mysql_table_guard(
        monkeypatch,
        target_statistics=({"INDEX_NAME": DISCOVERY_INDEX},),
        downgrade=True,
    )
    assert shapes == [
        (OLD_INDEX, ("dataset_id", "instrument_id", "capture_id", "publication_sequence")),
        (DISCOVERY_INDEX, DISCOVERY_COLUMNS),
    ]


@pytest.mark.parametrize(
    ("inspector", "message"),
    [
        (_PublicationInspector(valid_primary_key=False), "reviewed publication fact primary key"),
        (_PublicationInspector(engine="MyISAM"), "requires an InnoDB publication fact table"),
    ],
)
def test_mysql_table_guard_rejects_wrong_primary_key_or_engine(
    monkeypatch: pytest.MonkeyPatch,
    inspector: _PublicationInspector,
    message: str,
) -> None:
    """Unexpected table identity or storage engine blocks MySQL index DDL."""
    with pytest.raises(RuntimeError, match=message):
        _exercise_mysql_table_guard(monkeypatch, inspector=inspector)


def _mysql_key_rows() -> list[dict[str, object]]:
    """Return metadata for the reviewed 1,577-byte utf8mb4 index key."""
    rows: list[dict[str, object]] = []
    for name, length in {
        "dataset_id": 128,
        "publication_id": 32,
        "instrument_id": 128,
        "observed_at": 40,
        "capture_id": 64,
    }.items():
        rows.append(
            {
                "COLUMN_NAME": name,
                "DATA_TYPE": "varchar",
                "COLUMN_TYPE": f"varchar({length})",
                "IS_NULLABLE": "YES"
                if name in {"instrument_id", "observed_at", "capture_id"}
                else "NO",
                "CHARACTER_MAXIMUM_LENGTH": length,
                "CHARACTER_OCTET_LENGTH": length * 4,
                "CHARACTER_SET_NAME": "utf8mb4",
                "COLLATION_NAME": (
                    "utf8mb4_0900_bin"
                    if name in {"dataset_id", "instrument_id", "capture_id"}
                    else "utf8mb4_0900_ai_ci"
                ),
            }
        )
    rows.extend(
        [
            {
                "COLUMN_NAME": "publication_sequence",
                "DATA_TYPE": "bigint",
                "COLUMN_TYPE": "bigint",
                "IS_NULLABLE": "NO",
                "CHARACTER_MAXIMUM_LENGTH": None,
                "CHARACTER_OCTET_LENGTH": None,
                "CHARACTER_SET_NAME": None,
            },
            {
                "COLUMN_NAME": "is_deleted",
                "DATA_TYPE": "tinyint",
                "COLUMN_TYPE": "tinyint(1)",
                "IS_NULLABLE": "NO",
                "CHARACTER_MAXIMUM_LENGTH": None,
                "CHARACTER_OCTET_LENGTH": None,
                "CHARACTER_SET_NAME": None,
            },
        ]
    )
    return rows


def test_index_reflection_rejects_unique_partial_expression_and_prefix_shapes() -> None:
    """Only a plain full-width non-unique index maps to an accepted key tuple."""
    migration = _load_migration()
    expected = ("dataset_id", "capture_id")
    assert (
        migration._index_columns(
            {
                "column_names": list(expected),
                "unique": False,
                "dialect_options": {},
            }
        )
        == expected
    )
    rejected = [
        {
            "column_names": list(expected),
            "unique": True,
            "dialect_options": {},
        },
        {
            "column_names": ["dataset_id", None],
            "unique": False,
            "dialect_options": {},
        },
        {
            "column_names": list(expected),
            "unique": False,
            "dialect_options": {"sqlite_where": "capture_id IS NOT NULL"},
        },
        {
            "column_names": list(expected),
            "unique": False,
            "dialect_options": {"mysql_length": {"capture_id": 8}},
        },
        {"column_names": list(expected), "unique": False},
    ]
    assert all(migration._index_columns(item) is None for item in rejected)


def test_mysql_statistics_reads_the_named_index_from_information_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL index-shape guards consume the target database's metadata result."""
    migration = _load_migration()
    rows = [
        {
            "INDEX_NAME": OLD_INDEX,
            "NON_UNIQUE": 1,
            "SEQ_IN_INDEX": 1,
            "COLUMN_NAME": "dataset_id",
            "SUB_PART": None,
            "INDEX_TYPE": "BTREE",
            "IS_VISIBLE": "YES",
        }
    ]
    bind = _MetadataBind(rows)
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    assert migration._mysql_statistics(OLD_INDEX) == tuple(rows)
    assert "information_schema.STATISTICS" in bind.statements[0]


def test_mysql_utf8mb4_key_width_is_1577_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full declared key width is calculated from live column metadata."""
    migration = _load_migration()
    bind = _MetadataBind(_mysql_key_rows())
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    assert migration._mysql_index_key_bytes() == 1_577
    assert "information_schema.COLUMNS" in bind.statements[0]


def test_mysql_key_width_guard_rejects_missing_indexed_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete information_schema result cannot authorize index creation."""
    migration = _load_migration()
    rows = [row for row in _mysql_key_rows() if row["COLUMN_NAME"] != "capture_id"]
    monkeypatch.setattr(migration.op, "get_bind", lambda: _MetadataBind(rows))
    with pytest.raises(RuntimeError, match="cannot inspect every MySQL indexed column"):
        migration._mysql_index_key_bytes()


def test_mysql_key_width_guard_rejects_key_over_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The calculated full key is compared to the live engine's configured limit."""
    migration = _load_migration()
    monkeypatch.setattr(migration.op, "get_bind", lambda: _MetadataBind(_mysql_key_rows()))
    monkeypatch.setattr(migration, "_MYSQL_MAX_KEY_BYTES", 1_576)
    with pytest.raises(RuntimeError, match="exceeds the InnoDB key-size limit"):
        migration._mysql_index_key_bytes()


def test_boolean_reflection_is_checked_per_dialect() -> None:
    """SQLite uses Boolean while MySQL may reflect the same column as TINYINT(1)."""
    migration = _load_migration()
    boolean_type = sa.Boolean()
    tinyint_type = TINYINT(display_width=1)
    integer_type = sa.Integer()

    assert migration._matches_column_type(boolean_type, sa.Boolean, "is_deleted", "sqlite")
    assert not migration._matches_column_type(tinyint_type, sa.Boolean, "is_deleted", "sqlite")
    assert migration._matches_column_type(tinyint_type, sa.Boolean, "is_deleted", "mysql")
    assert migration._matches_column_type(boolean_type, sa.Boolean, "is_deleted", "mysql")
    assert not migration._matches_column_type(integer_type, sa.Boolean, "is_deleted", "mysql")


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("capture_id", {"CHARACTER_SET_NAME": "utf8mb3"}, "utf8mb4 VARCHAR width"),
        ("dataset_id", {"CHARACTER_OCTET_LENGTH": 128}, "utf8mb4 VARCHAR width"),
        ("publication_sequence", {"COLUMN_TYPE": "bigint unsigned"}, "signed BIGINT"),
        ("is_deleted", {"COLUMN_TYPE": "tinyint(2)"}, "TINYINT(1)"),
        (
            "dataset_id",
            {"COLLATION_NAME": "utf8mb4_0900_ai_ci"},
            "preserve the reviewed binary collation",
        ),
    ],
)
def test_mysql_key_width_guard_rejects_unreviewed_physical_columns(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: dict[str, object],
    message: str,
) -> None:
    """Different charset, width, signedness, or boolean encoding cannot pass."""
    migration = _load_migration()
    rows = _mysql_key_rows()
    next(row for row in rows if row["COLUMN_NAME"] == field).update(replacement)
    monkeypatch.setattr(migration.op, "get_bind", lambda: _MetadataBind(rows))
    with pytest.raises(RuntimeError, match=re.escape(message)):
        migration._mysql_index_key_bytes()


@pytest.mark.parametrize(
    ("engine", "row_format", "page_size", "message"),
    [
        ("MyISAM", "Dynamic", 16384, "InnoDB DYNAMIC"),
        ("InnoDB", "Compact", 16384, "InnoDB DYNAMIC"),
        ("InnoDB", "Dynamic", 8192, "requires 16 KiB"),
    ],
)
def test_mysql_storage_guard_rejects_unsafe_index_storage(
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    row_format: str,
    page_size: int,
    message: str,
) -> None:
    """Non-InnoDB, compact rows, and too-small pages fail closed."""
    migration = _load_migration()
    storage = _MetadataBind([{"ENGINE": engine, "ROW_FORMAT": row_format, "PAGE_SIZE": page_size}])
    monkeypatch.setattr(migration.op, "get_bind", lambda: storage)
    if engine == "InnoDB" and row_format == "Dynamic":
        # Reinsert storage metadata as the first query, then the key columns.
        combined = _SequentialMetadataBind(
            [
                [{"ENGINE": engine, "ROW_FORMAT": row_format, "PAGE_SIZE": page_size}],
                _mysql_key_rows(),
            ]
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: combined)
    else:
        combined = _SequentialMetadataBind(
            [[{"ENGINE": engine, "ROW_FORMAT": row_format, "PAGE_SIZE": page_size}]]
        )
        monkeypatch.setattr(migration.op, "get_bind", lambda: combined)
    with pytest.raises(RuntimeError, match=message):
        migration._require_mysql_storage_budget()


@pytest.mark.parametrize("row_format", ["Dynamic", "Compressed"])
def test_mysql_storage_guard_accepts_reviewed_16k_layout(
    monkeypatch: pytest.MonkeyPatch, row_format: str
) -> None:
    """Both reviewed row formats accept the candidate's explicit 1,577-byte key."""
    migration = _load_migration()
    bind = _SequentialMetadataBind(
        [
            [{"ENGINE": "InnoDB", "ROW_FORMAT": row_format, "PAGE_SIZE": 16_384}],
            _mysql_key_rows(),
        ]
    )
    monkeypatch.setattr(migration.op, "get_bind", lambda: bind)
    migration._require_mysql_storage_budget()
    assert len(bind.statements) == 2


def test_mysql_storage_guard_rejects_missing_table_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing information_schema storage row blocks the DDL preflight."""
    migration = _load_migration()
    monkeypatch.setattr(migration.op, "get_bind", lambda: _MetadataBind([]))
    with pytest.raises(RuntimeError, match="cannot inspect the MySQL publication table storage"):
        migration._mysql_storage_metadata()


def test_mysql_storage_budget_rejects_a_key_over_the_engine_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The composite preflight fails if its measured key exceeds the engine limit."""
    migration = _load_migration()
    monkeypatch.setattr(migration, "_mysql_storage_metadata", lambda: ("dynamic", 16_384))
    monkeypatch.setattr(migration, "_mysql_index_key_bytes", lambda: 3_073)
    with pytest.raises(RuntimeError, match="does not fit this InnoDB page size"):
        migration._require_mysql_storage_budget()


class _SequentialMetadataBind:
    """Return successive fake metadata results for storage and column probes."""

    def __init__(self, result_rows: Sequence[Sequence[Mapping[str, object]]]) -> None:
        self.result_rows = list(result_rows)
        self.statements: list[str] = []

    def execute(
        self, statement: Any, parameters: Mapping[str, object] | None = None
    ) -> _MappingRows:
        del parameters
        self.statements.append(str(statement))
        if not self.result_rows:
            raise AssertionError("unexpected metadata query")
        return _MappingRows(self.result_rows.pop(0))


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [
            {
                "INDEX_NAME": "ix_od_fact_capture_read",
                "NON_UNIQUE": 1,
                "SEQ_IN_INDEX": 1,
                "COLUMN_NAME": "dataset_id",
                "SUB_PART": 12,
                "INDEX_TYPE": "BTREE",
                "IS_VISIBLE": "YES",
            }
        ]
        + [
            {
                "INDEX_NAME": "ix_od_fact_capture_read",
                "NON_UNIQUE": 1,
                "SEQ_IN_INDEX": sequence,
                "COLUMN_NAME": name,
                "SUB_PART": None,
                "INDEX_TYPE": "BTREE",
                "IS_VISIBLE": "YES",
            }
            for sequence, name in enumerate(
                ("instrument_id", "capture_id", "publication_sequence"), start=2
            )
        ],
    ],
)
def test_mysql_index_shape_guard_rejects_missing_or_prefix_index(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, object]]
) -> None:
    """Missing or prefix-only index metadata cannot satisfy the predecessor contract."""
    migration = _load_migration()
    bind = _MetadataBind(rows)
    monkeypatch.setattr(migration, "_mysql_statistics", lambda _name: tuple(bind.rows))
    with pytest.raises(RuntimeError, match="reviewed MySQL ix_od_fact_capture_read index shape"):
        migration._require_mysql_index_shape(
            OLD_INDEX,
            ("dataset_id", "instrument_id", "capture_id", "publication_sequence"),
        )


def test_mysql_index_shape_guard_accepts_exact_visible_full_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exact visible non-unique BTREE satisfies the predecessor contract."""
    migration = _load_migration()
    columns = ("dataset_id", "instrument_id", "capture_id", "publication_sequence")
    rows = tuple(
        {
            "INDEX_NAME": OLD_INDEX,
            "NON_UNIQUE": 1,
            "SEQ_IN_INDEX": sequence,
            "COLUMN_NAME": name,
            "SUB_PART": None,
            "INDEX_TYPE": "BTREE",
            "IS_VISIBLE": "YES",
        }
        for sequence, name in enumerate(columns, start=1)
    )
    monkeypatch.setattr(migration, "_mysql_statistics", lambda _name: rows)
    migration._require_mysql_index_shape(OLD_INDEX, columns)
