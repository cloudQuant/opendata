"""Verify revision 0021 on a temporary SQLite warehouse schema."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    import sqlite3

    from sqlalchemy.engine import Connection, Engine

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPOSITORY_ROOT / "alembic_data" / "versions" / "20261029-0021_temporal_identity_snapshot.py"
)
VERSION_TABLE = "alembic_version_data"
HEADER = "od_temporal_identity_snapshots"
CHUNKS = "od_temporal_identity_snapshot_chunks"
HEADER_KEY = ("authority_id", "registry_id", "reference_version")
CHUNK_KEY = (*HEADER_KEY, "chunk_no")
MARKER = "0020_run_ledger_claim_policy"


def _load_migration() -> ModuleType:
    """Load revision 0021 without evaluating the Alembic environment."""
    spec = importlib.util.spec_from_file_location("temporal_identity_snapshot_0021", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_engine(tmp_path: Path, name: str = "temporal-identity.sqlite") -> Engine:
    """Create a test-owned SQLite file and enable declared foreign keys."""
    engine = sa.create_engine(f"sqlite+pysqlite:///{tmp_path / name}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: sqlite3.Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine


def _prepare_marker(engine: Engine) -> None:
    """Create the exact pre-revision marker in this temporary database."""
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE alembic_version_data (version_num VARCHAR(64) NOT NULL PRIMARY KEY)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO alembic_version_data (version_num) VALUES (:marker)"),
            {"marker": MARKER},
        )


def _run_revision(engine: Engine, migration: ModuleType, operation: str) -> None:
    """Run one migration function on the explicitly owned SQLite database."""
    with engine.begin() as connection:
        context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
        with Operations.context(context):
            getattr(migration, operation)()


def _apply(engine: Engine, migration: ModuleType | None = None) -> ModuleType:
    """Create the temporal snapshot tables and return the loaded revision."""
    revision_module = migration or _load_migration()
    _run_revision(engine, revision_module, "upgrade")
    return revision_module


def _insert_header(
    connection: Connection,
    *,
    authority_id: str = "akshare",
    registry_id: str = "cn-a-share",
    reference_version: str = "2026-10-29-v1",
    total_bytes: int = 1,
    chunk_count: int = 1,
    snapshot_sha256: bytes = b"s" * 32,
    created_at_us: int = 1_798_200_000_000_000,
) -> None:
    """Insert one synthetic snapshot header with explicit values."""
    connection.execute(
        sa.text(
            "INSERT INTO od_temporal_identity_snapshots "
            "(authority_id, registry_id, reference_version, total_bytes, chunk_count, "
            "snapshot_sha256, created_at_us) "
            "VALUES (:authority_id, :registry_id, :reference_version, :total_bytes, "
            ":chunk_count, :snapshot_sha256, :created_at_us)"
        ),
        {
            "authority_id": authority_id,
            "registry_id": registry_id,
            "reference_version": reference_version,
            "total_bytes": total_bytes,
            "chunk_count": chunk_count,
            "snapshot_sha256": snapshot_sha256,
            "created_at_us": created_at_us,
        },
    )


def _insert_chunk(
    connection: Connection,
    *,
    authority_id: str = "akshare",
    registry_id: str = "cn-a-share",
    reference_version: str = "2026-10-29-v1",
    chunk_no: int = 0,
    chunk_bytes: bytes = b"x",
    chunk_sha256: bytes = b"c" * 32,
) -> None:
    """Insert one synthetic chunk using the snapshot's composite identity."""
    connection.execute(
        sa.text(
            "INSERT INTO od_temporal_identity_snapshot_chunks "
            "(authority_id, registry_id, reference_version, chunk_no, chunk_bytes, chunk_sha256) "
            "VALUES (:authority_id, :registry_id, :reference_version, :chunk_no, "
            ":chunk_bytes, :chunk_sha256)"
        ),
        {
            "authority_id": authority_id,
            "registry_id": registry_id,
            "reference_version": reference_version,
            "chunk_no": chunk_no,
            "chunk_bytes": chunk_bytes,
            "chunk_sha256": chunk_sha256,
        },
    )


def test_revision_graph_and_fixed_ddl_contract() -> None:
    """Pin revision ancestry, table identities, binary types, and dialect DDL."""
    migration = _load_migration()
    assert migration.revision == "0021_temporal_identity_snapshot"
    assert migration.down_revision == "0020_run_ledger_claim_policy"

    sqlite_header, sqlite_chunks = migration._sqlite_ddl()
    assert 'CREATE TABLE "od_temporal_identity_snapshots"' in sqlite_header
    assert 'CREATE TABLE "od_temporal_identity_snapshot_chunks"' in sqlite_chunks
    assert "COLLATE BINARY" in sqlite_header
    assert "COLLATE BINARY" in sqlite_chunks
    assert "ON DELETE RESTRICT" in sqlite_chunks

    mysql_header, mysql_chunks = migration._mysql_ddl()
    assert "BINARY(32) NOT NULL" in mysql_header
    assert "MEDIUMBLOB NOT NULL" in mysql_chunks
    assert "utf8mb4_0900_bin" in mysql_header and "utf8mb4_0900_bin" in mysql_chunks
    assert "ENGINE=InnoDB" in mysql_header and "ENGINE=InnoDB" in mysql_chunks
    assert "CHECK (total_bytes BETWEEN 1 AND 67108864)" in mysql_header


def test_upgrade_builds_case_sensitive_composite_keys_and_rejects_duplicate(tmp_path: Path) -> None:
    """Create exact tables and retain case distinctions in all identity keys."""
    engine = _new_engine(tmp_path)
    try:
        _prepare_marker(engine)
        _apply(engine)

        inspector = sa.inspect(engine)
        assert set(inspector.get_table_names()) >= {HEADER, CHUNKS, VERSION_TABLE}
        header_columns = inspector.get_columns(HEADER)
        chunk_columns = inspector.get_columns(CHUNKS)
        assert {column["name"] for column in header_columns} == {
            "authority_id",
            "registry_id",
            "reference_version",
            "total_bytes",
            "chunk_count",
            "snapshot_sha256",
            "created_at_us",
        }
        assert {column["name"] for column in chunk_columns} == {
            "authority_id",
            "registry_id",
            "reference_version",
            "chunk_no",
            "chunk_bytes",
            "chunk_sha256",
        }
        assert all(
            column["nullable"] is False and column["default"] is None for column in header_columns
        )
        assert all(
            column["nullable"] is False and column["default"] is None for column in chunk_columns
        )
        assert (
            getattr(
                next(column for column in header_columns if column["name"] == "authority_id")[
                    "type"
                ],
                "length",
                None,
            )
            == 128
        )
        assert (
            getattr(
                next(column for column in header_columns if column["name"] == "snapshot_sha256")[
                    "type"
                ],
                "length",
                None,
            )
            is None
        )
        assert inspector.get_indexes(HEADER) == []
        assert inspector.get_indexes(CHUNKS) == []
        assert tuple(inspector.get_pk_constraint(HEADER)["constrained_columns"]) == HEADER_KEY
        assert tuple(inspector.get_pk_constraint(CHUNKS)["constrained_columns"]) == CHUNK_KEY
        assert {item["name"] for item in inspector.get_check_constraints(HEADER)} == set(
            _load_migration()._HEADER_CHECKS
        )
        assert {item["name"] for item in inspector.get_check_constraints(CHUNKS)} == set(
            _load_migration()._CHUNK_CHECKS
        )
        foreign_keys = inspector.get_foreign_keys(CHUNKS)
        assert len(foreign_keys) == 1
        assert foreign_keys[0]["referred_table"] == HEADER
        assert tuple(foreign_keys[0]["constrained_columns"]) == HEADER_KEY
        assert tuple(foreign_keys[0]["referred_columns"]) == HEADER_KEY
        assert foreign_keys[0]["options"]["ondelete"] == "RESTRICT"

        variants = (
            ("Authority", "Registry", "Version"),
            ("authority", "Registry", "Version"),
            ("Authority", "registry", "Version"),
            ("Authority", "Registry", "version"),
        )
        with engine.begin() as connection:
            for index, (authority_id, registry_id, reference_version) in enumerate(variants):
                _insert_header(
                    connection,
                    authority_id=authority_id,
                    registry_id=registry_id,
                    reference_version=reference_version,
                )
                _insert_chunk(
                    connection,
                    authority_id=authority_id,
                    registry_id=registry_id,
                    reference_version=reference_version,
                    chunk_bytes=bytes([index + 1]),
                )
            with pytest.raises(IntegrityError), connection.begin_nested():
                _insert_header(
                    connection,
                    authority_id="Authority",
                    registry_id="Registry",
                    reference_version="Version",
                )
        with engine.connect() as connection:
            header = sa.table(HEADER)
            chunks = sa.table(CHUNKS)
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(header)).scalar_one() == 4
            )
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(chunks)).scalar_one() == 4
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("authority_id", ""),
        ("registry_id", "r" * 129),
        ("reference_version", "版本-1"),
    ],
)
def test_sqlite_rejects_invalid_ascii_identity_keys(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    """Enforce nonempty, bounded, single-byte ASCII identifiers at the schema."""
    engine = _new_engine(tmp_path, f"invalid-id-{field}.sqlite")
    try:
        _prepare_marker(engine)
        _apply(engine)
        identifiers = {
            "authority_id": "akshare",
            "registry_id": "cn-a-share",
            "reference_version": "invalid-id",
        }
        identifiers[field] = value
        with engine.begin() as connection, pytest.raises(IntegrityError):
            _insert_header(connection, **identifiers)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("total_bytes", 0),
        ("total_bytes", 67_108_865),
        ("chunk_count", 0),
        ("chunk_count", 257),
        ("created_at_us", -1),
        ("snapshot_sha256", b"s" * 31),
        ("snapshot_sha256", "s" * 32),
    ],
)
def test_sqlite_enforces_header_size_and_digest_constraints(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """Reject invalid header ranges and non-binary or wrong-sized digests."""
    engine = _new_engine(tmp_path, f"invalid-header-{field}-{str(value)[:3]}.sqlite")
    try:
        _prepare_marker(engine)
        _apply(engine)
        arguments: dict[str, object] = {
            "reference_version": f"invalid-{field}-{str(value)[:3]}",
            field: value,
        }
        with engine.begin() as connection, pytest.raises(IntegrityError):
            _insert_header(connection, **arguments)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("chunk_no", "chunk_bytes", "chunk_sha256"),
    [
        (-1, b"x", b"c" * 32),
        (256, b"x", b"c" * 32),
        (0, b"", b"c" * 32),
        (0, b"x" * 262_145, b"c" * 32),
        (0, b"x", b"c" * 31),
        (0, b"x", "c" * 32),
    ],
)
def test_sqlite_enforces_chunk_ranges_and_binary_digest(
    tmp_path: Path,
    chunk_no: int,
    chunk_bytes: bytes,
    chunk_sha256: bytes | str,
) -> None:
    """Reject out-of-range ordinals, chunk sizes, and malformed SHA-256 values."""
    engine = _new_engine(tmp_path, f"invalid-chunk-{chunk_no}-{len(chunk_bytes)}.sqlite")
    try:
        _prepare_marker(engine)
        _apply(engine)
        with engine.begin() as connection:
            _insert_header(connection)
            with pytest.raises(IntegrityError), connection.begin_nested():
                _insert_chunk(
                    connection,
                    chunk_no=chunk_no,
                    chunk_bytes=chunk_bytes,
                    chunk_sha256=chunk_sha256,
                )
    finally:
        engine.dispose()


def test_sqlite_accepts_maximum_chunk_and_restricts_parent_delete(tmp_path: Path) -> None:
    """Accept the maximum chunk size and prevent deleting its snapshot header."""
    engine = _new_engine(tmp_path)
    try:
        _prepare_marker(engine)
        _apply(engine)
        with engine.begin() as connection:
            boundary = connection.begin_nested()
            _insert_header(
                connection,
                reference_version="maximum-header-bounds",
                total_bytes=67_108_864,
                chunk_count=256,
                created_at_us=0,
            )
            boundary.rollback()
            _insert_header(connection, total_bytes=262_144, chunk_count=1)
            _insert_chunk(connection, chunk_bytes=b"x" * 262_144)
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(
                    sa.text(
                        "DELETE FROM od_temporal_identity_snapshots "
                        "WHERE authority_id='akshare' AND registry_id='cn-a-share' "
                        "AND reference_version='2026-10-29-v1'"
                    )
                )
    finally:
        engine.dispose()


def test_upgrade_is_safe_to_repeat_after_both_tables_exist(tmp_path: Path) -> None:
    """Accept only a complete exact schema when MySQL DDL preceded marker advance."""
    engine = _new_engine(tmp_path)
    try:
        _prepare_marker(engine)
        migration = _apply(engine)
        with engine.begin() as connection:
            _insert_header(connection)
            _insert_chunk(connection)
        _run_revision(engine, migration, "upgrade")
        with engine.connect() as connection:
            header = sa.table(HEADER)
            chunks = sa.table(CHUNKS)
            version = sa.table(VERSION_TABLE, sa.column("version_num"))
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(header)).scalar_one() == 1
            )
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(chunks)).scalar_one() == 1
            )
            assert connection.execute(sa.select(version.c.version_num)).scalar_one() == MARKER
    finally:
        engine.dispose()


def test_upgrade_fails_closed_on_a_partial_schema_without_removing_it(tmp_path: Path) -> None:
    """Leave unexpected preexisting DDL untouched for manual inspection."""
    engine = _new_engine(tmp_path, "partial.sqlite")
    try:
        _prepare_marker(engine)
        with engine.begin() as connection:
            connection.execute(sa.text(f"CREATE TABLE {HEADER} (unexpected TEXT)"))
        with pytest.raises(RuntimeError, match="partial snapshot schema"):
            _apply(engine)
        inspector = sa.inspect(engine)
        assert inspector.has_table(HEADER)
        assert not inspector.has_table(CHUNKS)
        assert [column["name"] for column in inspector.get_columns(HEADER)] == ["unexpected"]
    finally:
        engine.dispose()


def test_downgrade_refuses_nonempty_snapshot_history_without_deleting_rows(tmp_path: Path) -> None:
    """Never drop persisted identity versions or chunks during automatic rollback."""
    engine = _new_engine(tmp_path)
    try:
        _prepare_marker(engine)
        migration = _apply(engine)
        with engine.begin() as connection:
            _insert_header(connection)
            _insert_chunk(connection)
        with pytest.raises(RuntimeError, match="downgrade is refused"):
            _run_revision(engine, migration, "downgrade")
        with engine.connect() as connection:
            header = sa.table(HEADER)
            chunks = sa.table(CHUNKS)
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(header)).scalar_one() == 1
            )
            assert (
                connection.execute(sa.select(sa.func.count()).select_from(chunks)).scalar_one() == 1
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("name", "is_mariadb", "version", "error"),
    [
        ("sqlite", False, None, None),
        ("mysql", False, (8, 0, 16), None),
        ("mysql", False, (8, 4, 0), None),
        ("mysql", False, (8, 0, 15), "requires MySQL 8.0.16"),
        ("mysql", False, (9, 0, 0), "requires MySQL 8.0.16"),
        ("mysql", True, (10, 11, 0), "not MariaDB"),
        ("mariadb", True, (10, 11, 0), "not MariaDB"),
        ("postgresql", False, None, "supports SQLite and MySQL"),
    ],
)
def test_supported_dialect_gate_without_opening_a_database(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    is_mariadb: bool,
    version: tuple[int, int, int] | None,
    error: str | None,
) -> None:
    """Reject unsupported or old servers before issuing any migration DDL."""
    migration = _load_migration()
    context = SimpleNamespace(
        as_sql=False,
        dialect=SimpleNamespace(
            name=name,
            is_mariadb=is_mariadb,
            server_version_info=version,
        ),
    )
    monkeypatch.setattr(migration, "op", SimpleNamespace(get_context=lambda: context))
    if error is None:
        assert migration._require_supported_target() == name
    else:
        with pytest.raises(RuntimeError, match=error):
            migration._require_supported_target()


def test_downgrade_refusal_does_not_depend_on_the_database() -> None:
    """Keep the rollback guard unconditional and independent of connection state."""
    migration = _load_migration()
    with pytest.raises(RuntimeError, match="downgrade is refused"):
        migration.downgrade()
