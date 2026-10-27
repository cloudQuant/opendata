"""Exercise revision 0012 against the frozen revision 0011 schema."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from types import ModuleType

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FROZEN_MIGRATION_0011_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261026-0011_publication_snapshots.py"
)
FROZEN_MIGRATION_0011_NAME = "publication_snapshots_migration_0011"
FROZEN_MIGRATION_0011_SHA256 = "69a9203b0a34a1da0b71438d3d310f96c70683c594fecf328a85d1c54d4b08f0"
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261027-0012_instrument_capture_scope.py"
)
MIGRATION_NAME = "instrument_capture_scope_migration_0012"
MIGRATION_SHA256 = "24c55bce92d97aa65ae2afb7b7b516ade21704d1956aa85c50827177aca2b9f2"
FACT_TABLE = "od_published_fact_versions"
SNAPSHOT_TABLE = "od_publication_snapshots"
NEIGHBOR_TABLE = "od_migration_neighbor"
_INSERT_FACT_0011_SQL = (
    "INSERT INTO od_published_fact_versions (dataset_id, business_key, revision, "
    "publication_id, publication_sequence, business_date, event_at, "
    "observed_at, source_published_at, available_at, record_json, "
    "record_hash, is_deleted) VALUES (:dataset_id, :business_key, :revision, "
    ":publication_id, :publication_sequence, :business_date, :event_at, "
    ":observed_at, :source_published_at, :available_at, :record_json, "
    ":record_hash, :is_deleted)"
)
_INSERT_FACT_0012_SQL = (
    "INSERT INTO od_published_fact_versions (dataset_id, business_key, revision, "
    "publication_id, publication_sequence, business_date, event_at, "
    "observed_at, source_published_at, available_at, record_json, "
    "record_hash, is_deleted, instrument_id, capture_id) VALUES "
    "(:dataset_id, :business_key, :revision, :publication_id, "
    ":publication_sequence, :business_date, :event_at, :observed_at, "
    ":source_published_at, :available_at, :record_json, :record_hash, "
    ":is_deleted, :instrument_id, :capture_id)"
)
_INSERT_SNAPSHOT_0011_SQL = (
    "INSERT INTO od_publication_snapshots (snapshot_id, dataset_id, schema_version, "
    "publication_id, publication_sequence, start_date, end_date, page_size, "
    "access_scope, selection_sha256, content_hash, record_count, as_of_us, "
    "created_at_us, expires_at_us) VALUES (:snapshot_id, :dataset_id, :schema_version, "
    ":publication_id, :publication_sequence, :start_date, :end_date, :page_size, "
    ":access_scope, :selection_sha256, :content_hash, :record_count, :as_of_us, "
    ":created_at_us, :expires_at_us)"
)
_INSERT_SNAPSHOT_0012_SQL = (
    "INSERT INTO od_publication_snapshots (snapshot_id, dataset_id, schema_version, "
    "publication_id, publication_sequence, start_date, end_date, page_size, "
    "access_scope, selection_sha256, content_hash, record_count, as_of_us, "
    "created_at_us, expires_at_us, instrument_id, capture_id) VALUES "
    "(:snapshot_id, :dataset_id, :schema_version, :publication_id, "
    ":publication_sequence, :start_date, :end_date, :page_size, "
    ":access_scope, :selection_sha256, :content_hash, :record_count, "
    ":as_of_us, :created_at_us, :expires_at_us, :instrument_id, :capture_id)"
)


def _load_module(path: Path, module_name: str) -> ModuleType:
    """Load one revision source file without Alembic's project env."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration() -> ModuleType:
    """Load only the selected revision module, without evaluating Alembic env."""
    actual_sha256 = hashlib.sha256(MIGRATION_PATH.read_bytes()).hexdigest()
    assert actual_sha256 == MIGRATION_SHA256
    return _load_module(MIGRATION_PATH, MIGRATION_NAME)


def _apply_frozen_0011(connection: Connection) -> None:
    """Run frozen 0011 on this owned connection without loading Alembic env."""
    actual_sha256 = hashlib.sha256(FROZEN_MIGRATION_0011_PATH.read_bytes()).hexdigest()
    assert actual_sha256 == FROZEN_MIGRATION_0011_SHA256
    migration = _load_module(FROZEN_MIGRATION_0011_PATH, FROZEN_MIGRATION_0011_NAME)
    operations_context = MigrationContext.configure(connection)
    with Operations.context(operations_context):
        migration.upgrade()


def _new_database(tmp_path: Path) -> Engine:
    """Create a caller-owned SQLite database in pytest's temporary directory."""
    database_path = tmp_path / "publication-capture-migration.sqlite"
    return sa.create_engine(f"sqlite:///{database_path}")


def _apply_revision(engine: Engine, operation_name: str) -> None:
    """Call one revision function through an owned connection and Operations."""
    migration = _load_migration()
    operation = getattr(migration, operation_name)
    with engine.begin() as connection:
        migration_context = MigrationContext.configure(connection)
        with Operations.context(migration_context):
            operation()


def _create_neighbor(connection: Connection) -> None:
    """Create a representative table owned by an adjacent migration."""
    connection.execute(
        sa.text(
            "CREATE TABLE od_migration_neighbor "
            "(id INTEGER PRIMARY KEY, marker VARCHAR(32) NOT NULL)"
        )
    )
    connection.execute(
        sa.text("INSERT INTO od_migration_neighbor (id, marker) VALUES (1, :marker)"),
        {"marker": "preserve-me"},
    )


def _insert_publication(connection: Connection) -> None:
    """Insert the publication referenced by the frozen fact and snapshot rows."""
    connection.execute(
        sa.text(
            "INSERT INTO od_publications (publication_id, dataset_id, source_id, "
            "sequence, schema_version, semantic_version, unit, adjustment_basis, "
            "quality_json, provenance_json, watermark_json, idempotency_key, "
            "payload_hash, record_count, published_at_us) VALUES "
            "(:publication_id, :dataset_id, :source_id, :sequence, :schema_version, "
            ":semantic_version, :unit, :adjustment_basis, :quality_json, "
            ":provenance_json, :watermark_json, :idempotency_key, :payload_hash, "
            ":record_count, :published_at_us)"
        ),
        {
            "publication_id": "publication-0001",
            "dataset_id": "sina_fzb_report_observations",
            "source_id": "sina-fzb",
            "sequence": 1,
            "schema_version": "source-native-report-v1",
            "semantic_version": "sina-fzb-source-native-v1",
            "unit": "per-field",
            "adjustment_basis": "not_applicable",
            "quality_json": "{}",
            "provenance_json": "{}",
            "watermark_json": None,
            "idempotency_key": "migration-fixture",
            "payload_hash": "a" * 64,
            "record_count": 1,
            "published_at_us": 1,
        },
    )


def _insert_fact(
    connection: Connection,
    *,
    business_key: str = "legacy-key",
    instrument_id: str | None = None,
    capture_id: str | None = None,
    include_scope_columns: bool = False,
) -> None:
    """Insert a fact using explicit 0011 or 0012 columns, never runtime metadata."""
    if not include_scope_columns and (instrument_id is not None or capture_id is not None):
        raise ValueError("Capture selectors require the 0012 schema columns.")
    values: dict[str, object] = {
        "dataset_id": "sina_fzb_report_observations",
        "business_key": business_key,
        "revision": 1,
        "publication_id": "publication-0001",
        "publication_sequence": 1,
        "business_date": None,
        "event_at": None,
        "observed_at": None,
        "source_published_at": None,
        "available_at": None,
        "record_json": '{"value":"source-value"}',
        "record_hash": "b" * 64,
        "is_deleted": False,
    }
    if include_scope_columns:
        values.update(instrument_id=instrument_id, capture_id=capture_id)
    statement = _INSERT_FACT_0012_SQL if include_scope_columns else _INSERT_FACT_0011_SQL
    connection.execute(sa.text(statement), values)


def _insert_snapshot(
    connection: Connection,
    *,
    snapshot_id: str = "legacy-snapshot",
    instrument_id: str | None = None,
    capture_id: str | None = None,
    include_scope_columns: bool = False,
) -> None:
    """Insert a snapshot using explicit 0011 or 0012 columns."""
    if not include_scope_columns and (instrument_id is not None or capture_id is not None):
        raise ValueError("Capture selectors require the 0012 schema columns.")
    values: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "dataset_id": "sina_fzb_report_observations",
        "schema_version": "source-native-report-v1",
        "publication_id": "publication-0001",
        "publication_sequence": 1,
        "start_date": None,
        "end_date": None,
        "page_size": 100,
        "access_scope": "test-scope",
        "selection_sha256": "c" * 64,
        "content_hash": "d" * 64,
        "record_count": 1,
        "as_of_us": 1,
        "created_at_us": 1,
        "expires_at_us": 2,
    }
    if include_scope_columns:
        values.update(instrument_id=instrument_id, capture_id=capture_id)
    statement = _INSERT_SNAPSHOT_0012_SQL if include_scope_columns else _INSERT_SNAPSHOT_0011_SQL
    connection.execute(sa.text(statement), values)


def _prepare_legacy_database(engine: Engine) -> None:
    """Apply the frozen base schema and save representative legacy data."""
    with engine.begin() as connection:
        _apply_frozen_0011(connection)
        _create_neighbor(connection)
        _insert_publication(connection)
        _insert_fact(connection)
        _insert_snapshot(connection)


def _schema_signature(connection: Connection) -> tuple[object, ...]:
    """Capture affected table columns, indexes, and check constraints."""
    inspector = sa.inspect(connection)
    signature: list[object] = []
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = tuple(
            (column["name"], str(column["type"]), column["nullable"])
            for column in inspector.get_columns(table_name)
        )
        indexes = tuple(
            sorted(
                (
                    index["name"],
                    tuple(index["column_names"]),
                    bool(index["unique"]),
                )
                for index in inspector.get_indexes(table_name)
            )
        )
        checks = tuple(
            sorted(
                (constraint["name"], " ".join(constraint["sqltext"].split()))
                for constraint in inspector.get_check_constraints(table_name)
            )
        )
        signature.append((table_name, columns, indexes, checks))
    return tuple(signature)


def _assert_scoped_columns(engine: Engine) -> None:
    """Assert the two exact nullable capture-key columns exist on both tables."""
    inspector = sa.inspect(engine)
    for table_name in (FACT_TABLE, SNAPSHOT_TABLE):
        columns = {column["name"]: column for column in inspector.get_columns(table_name)}
        assert columns["instrument_id"]["nullable"] is True
        assert columns["capture_id"]["nullable"] is True
        assert columns["instrument_id"]["type"].length == 128
        assert columns["capture_id"]["type"].length == 64


def test_upgrade_preserves_legacy_rows_and_enforces_paired_scope(tmp_path: Path) -> None:
    """Upgrade legacy rows unchanged, allow complete scopes, and reject half pairs."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        with engine.connect() as connection:
            legacy_fact_before = tuple(
                connection.execute(sa.text("SELECT * FROM od_published_fact_versions")).one()
            )
            legacy_snapshot_before = tuple(
                connection.execute(sa.text("SELECT * FROM od_publication_snapshots")).one()
            )
        _apply_revision(engine, "upgrade")
        _assert_scoped_columns(engine)

        with engine.begin() as connection:
            legacy_fact_after = tuple(
                connection.execute(sa.text("SELECT * FROM od_published_fact_versions")).one()
            )
            legacy_snapshot_after = tuple(
                connection.execute(sa.text("SELECT * FROM od_publication_snapshots")).one()
            )
            assert legacy_fact_after == (*legacy_fact_before, None, None)
            assert legacy_snapshot_after == (*legacy_snapshot_before, None, None)

            _insert_fact(
                connection,
                business_key="scoped-key",
                instrument_id="AAPL.US",
                capture_id="e" * 64,
                include_scope_columns=True,
            )
            _insert_snapshot(
                connection,
                snapshot_id="scoped-snapshot",
                instrument_id="AAPL.US",
                capture_id="e" * 64,
                include_scope_columns=True,
            )

            for instrument_id, capture_id in (("AAPL.US", None), (None, "f" * 64)):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _insert_fact(
                        connection,
                        business_key=f"invalid-fact-{instrument_id}-{capture_id}",
                        instrument_id=instrument_id,
                        capture_id=capture_id,
                        include_scope_columns=True,
                    )
                with pytest.raises(IntegrityError), connection.begin_nested():
                    _insert_snapshot(
                        connection,
                        snapshot_id=f"invalid-snapshot-{instrument_id}-{capture_id}",
                        instrument_id=instrument_id,
                        capture_id=capture_id,
                        include_scope_columns=True,
                    )

            checks = {
                table_name: {
                    constraint["name"]
                    for constraint in sa.inspect(connection).get_check_constraints(table_name)
                }
                for table_name in (FACT_TABLE, SNAPSHOT_TABLE)
            }
            assert "ck_od_fact_capture_pair" in checks[FACT_TABLE]
            assert "ck_od_published_fact_revision" in checks[FACT_TABLE]
            assert "ck_od_snapshot_capture_pair" in checks[SNAPSHOT_TABLE]
            assert checks[SNAPSHOT_TABLE] == {
                "ck_od_snapshot_capture_pair",
                "ck_od_snapshot_page_size",
                "ck_od_snapshot_record_count",
            }
            fact_indexes = {
                index["name"]: tuple(index["column_names"])
                for index in sa.inspect(connection).get_indexes(FACT_TABLE)
            }
            assert {
                "ix_od_published_fact_asof",
                "ix_od_published_fact_date",
                "ix_od_fact_capture_read",
            } <= fact_indexes.keys()
            assert fact_indexes["ix_od_fact_capture_read"] == (
                "dataset_id",
                "instrument_id",
                "capture_id",
                "publication_sequence",
            )
            snapshot_indexes = {
                index["name"] for index in sa.inspect(connection).get_indexes(SNAPSHOT_TABLE)
            }
            assert snapshot_indexes == {"ix_od_snapshot_expiry"}
            neighbor = connection.execute(
                sa.text("SELECT marker FROM od_migration_neighbor WHERE id = 1")
            ).scalar_one()
            assert neighbor == "preserve-me"
    finally:
        engine.dispose()


@pytest.mark.parametrize("scoped_table", [FACT_TABLE, SNAPSHOT_TABLE])
def test_downgrade_rejects_scope_before_any_ddl(
    tmp_path: Path,
    scoped_table: str,
) -> None:
    """Refuse rollback for scope in either table and preserve the full schema."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        _apply_revision(engine, "upgrade")
        with engine.begin() as connection:
            if scoped_table == FACT_TABLE:
                _insert_fact(
                    connection,
                    business_key="scoped-key",
                    instrument_id="AAPL.US",
                    capture_id="e" * 64,
                    include_scope_columns=True,
                )
            else:
                _insert_snapshot(
                    connection,
                    snapshot_id="scoped-snapshot",
                    instrument_id="AAPL.US",
                    capture_id="e" * 64,
                    include_scope_columns=True,
                )
        with engine.connect() as connection:
            schema_before = _schema_signature(connection)

        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            """Observe SQL sent to SQLite during the expected failed downgrade."""
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with pytest.raises(RuntimeError, match="Scoped publication data"):
                _apply_revision(engine, "downgrade")
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        assert any(statement.lstrip().upper().startswith("SELECT") for statement in statements)
        assert not any(
            re.match(r"^\s*(?:CREATE|ALTER|DROP)\b", statement, re.IGNORECASE)
            for statement in statements
        )

        with engine.connect() as connection:
            assert _schema_signature(connection) == schema_before
            scoped_table_expression = sa.table(
                scoped_table,
                sa.column("instrument_id"),
            )
            assert (
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(scoped_table_expression)
                    .where(scoped_table_expression.c.instrument_id.is_not(None))
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(
                    sa.text("SELECT count(*) FROM od_migration_neighbor")
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_legacy_only_downgrade_preserves_data_and_can_upgrade_again(tmp_path: Path) -> None:
    """Allow a legacy-only rollback, retain rows, and reapply the revision."""
    engine = _new_database(tmp_path)
    try:
        _prepare_legacy_database(engine)
        _apply_revision(engine, "upgrade")
        _apply_revision(engine, "downgrade")

        with engine.connect() as connection:
            inspector = sa.inspect(connection)
            fact_columns = {column["name"] for column in inspector.get_columns(FACT_TABLE)}
            snapshot_columns = {column["name"] for column in inspector.get_columns(SNAPSHOT_TABLE)}
            assert "instrument_id" not in fact_columns
            assert "capture_id" not in fact_columns
            assert "instrument_id" not in snapshot_columns
            assert "capture_id" not in snapshot_columns
            assert connection.execute(
                sa.text("SELECT business_key, record_json FROM od_published_fact_versions")
            ).one() == ("legacy-key", '{"value":"source-value"}')
            assert connection.execute(
                sa.text("SELECT snapshot_id, content_hash FROM od_publication_snapshots")
            ).one() == ("legacy-snapshot", "d" * 64)
            assert (
                connection.execute(
                    sa.text("SELECT marker FROM od_migration_neighbor WHERE id = 1")
                ).scalar_one()
                == "preserve-me"
            )

        with engine.begin() as connection:
            _insert_fact(connection, business_key="legacy-after-downgrade")
            _insert_snapshot(connection, snapshot_id="legacy-after-downgrade")

        _apply_revision(engine, "upgrade")
        _assert_scoped_columns(engine)
        with engine.connect() as connection:
            fact_scope_rows = {
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT business_key, instrument_id, capture_id "
                        "FROM od_published_fact_versions"
                    )
                )
            }
            snapshot_scope_rows = {
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        "SELECT snapshot_id, instrument_id, capture_id "
                        "FROM od_publication_snapshots"
                    )
                )
            }
            assert fact_scope_rows == {
                ("legacy-key", None, None),
                ("legacy-after-downgrade", None, None),
            }
            assert snapshot_scope_rows == {
                ("legacy-snapshot", None, None),
                ("legacy-after-downgrade", None, None),
            }
    finally:
        engine.dispose()


def test_revision_identity_follows_frozen_0011() -> None:
    """Bind this test to the exact child/parent revision names."""
    migration = _load_migration()

    assert migration.revision == "0012_instrument_capture_scope"
    assert migration.down_revision == "0011_publication_snapshots"


class _FakeDialect:
    """Expose only the dialect identity and version used by the migration guard."""

    def __init__(
        self,
        *,
        name: str,
        server_version_info: tuple[int, ...] | None,
        is_mariadb: bool = False,
    ) -> None:
        self.name = name
        self.server_version_info = server_version_info
        self.is_mariadb = is_mariadb


class _FakeResult:
    """Return an empty result for safe offline downgrade preflight simulation."""

    def first(self) -> None:
        """Report no selected rows without connecting to a database."""
        return None


class _FakeBind:
    """Record any query attempt while exposing a fake SQLAlchemy dialect."""

    def __init__(self, dialect: _FakeDialect) -> None:
        self.dialect = dialect
        self.statements: list[object] = []

    def execute(self, statement: object) -> _FakeResult:
        """Record a would-be query without performing I/O."""
        self.statements.append(statement)
        return _FakeResult()


class _FakeBatch:
    """Record would-be DDL operations for a single named table."""

    def __init__(self, operations: _FakeOperations, table_name: str) -> None:
        self._operations = operations
        self._table_name = table_name

    def __enter__(self) -> _FakeBatch:
        """Return this recorder as the batch-operation facade."""
        return self

    def __exit__(self, *args: object) -> bool:
        """Propagate exceptions without suppressing them."""
        return False

    def add_column(self, column: object) -> None:
        """Record a would-be column addition."""
        self._operations.ddl_calls.append(("add_column", self._table_name, column))

    def create_check_constraint(self, name: str, condition: str) -> None:
        """Record a would-be check-constraint addition."""
        self._operations.ddl_calls.append(("create_check", self._table_name, name, condition))

    def create_index(self, name: str, columns: list[str], *, unique: bool = False) -> None:
        """Record a would-be index addition."""
        self._operations.ddl_calls.append(
            ("create_index", self._table_name, name, tuple(columns), unique)
        )

    def drop_constraint(self, name: str, *, type_: str) -> None:
        """Record a would-be constraint removal."""
        self._operations.ddl_calls.append(("drop_constraint", self._table_name, name, type_))

    def drop_column(self, name: str) -> None:
        """Record a would-be column removal."""
        self._operations.ddl_calls.append(("drop_column", self._table_name, name))

    def drop_index(self, name: str) -> None:
        """Record a would-be index removal."""
        self._operations.ddl_calls.append(("drop_index", self._table_name, name))


class _FakeOperations:
    """Implement only the Alembic operations touched by the new revision."""

    def __init__(self, bind: _FakeBind) -> None:
        self._bind = bind
        self.batch_calls: list[str] = []
        self.ddl_calls: list[tuple[object, ...]] = []

    def get_bind(self) -> _FakeBind:
        """Return the fake bind without opening a connection."""
        return self._bind

    def batch_alter_table(self, table_name: str) -> _FakeBatch:
        """Record a would-be batch DDL entry point."""
        self.batch_calls.append(table_name)
        return _FakeBatch(self, table_name)


@pytest.mark.parametrize(
    ("dialect_name", "version", "is_mariadb"),
    [
        pytest.param("mysql", None, False, id="unknown-version"),
        pytest.param("mysql", (8, 0), False, id="short-version-tuple"),
        pytest.param("mysql", (5, 7, 44), False, id="mysql-57"),
        pytest.param("mysql", (8, 0, 15), False, id="before-check-enforcement"),
        pytest.param("mysql", (9, 0, 0), False, id="unsupported-major"),
        pytest.param("mysql", (10, 11, 7), True, id="mariadb-mysql-dialect"),
        pytest.param("mariadb", (10, 11, 7), False, id="mariadb-dialect-name"),
    ],
)
def test_unsupported_mysql_guard_blocks_upgrade_and_downgrade_before_io(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    version: tuple[int, ...] | None,
    is_mariadb: bool,
) -> None:
    """Reject unsupported MySQL identities before queries or DDL are attempted."""
    migration = _load_migration()
    bind = _FakeBind(
        _FakeDialect(
            name=dialect_name,
            server_version_info=version,
            is_mariadb=is_mariadb,
        )
    )
    operations = _FakeOperations(bind)
    monkeypatch.setattr(migration, "op", operations)

    for operation in (migration.upgrade, migration.downgrade):
        with pytest.raises(RuntimeError, match=r"MySQL 8\.0\.16"):
            operation()
        assert operations.batch_calls == []
        assert operations.ddl_calls == []
        assert bind.statements == []


@pytest.mark.parametrize(
    "version",
    [(8, 0, 16), (8, 0, 17), (8, 4, 0)],
)
def test_mysql_guard_accepts_supported_version_boundary(
    monkeypatch: pytest.MonkeyPatch,
    version: tuple[int, ...],
) -> None:
    """Accept MySQL 8 at the minimum enforced-check version and later 8.x."""
    migration = _load_migration()
    bind = _FakeBind(_FakeDialect(name="mysql", server_version_info=version))
    operations = _FakeOperations(bind)
    monkeypatch.setattr(migration, "op", operations)

    migration._require_supported_database()

    assert operations.batch_calls == []
    assert operations.ddl_calls == []
    assert bind.statements == []


def test_mysql_guard_leaves_non_mysql_dialects_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep SQLite and other non-MySQL dialects outside the MySQL-only guard."""
    migration = _load_migration()
    bind = _FakeBind(_FakeDialect(name="sqlite", server_version_info=None))
    operations = _FakeOperations(bind)
    monkeypatch.setattr(migration, "op", operations)

    migration._require_supported_database()

    assert operations.batch_calls == []
    assert operations.ddl_calls == []
    assert bind.statements == []
