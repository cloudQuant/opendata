"""Run only the explicitly targeted local source-gate migration chain.

This environment does not load warehouse configuration or ``.env``. It opens
only the explicitly selected SQLite file, which may coexist with unrelated
warehouse tables. It checks the migration marker and complete gate schema
before writing, then applies the small migration chain in one SQLite write
transaction.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

from sqlalchemy import create_engine, event, pool

from alembic import context
from alembic_source_gate import path_contract, schema_contract

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

config = context.config
INITIALIZATION_MODE_ATTRIBUTE = "source_gate_initialization_mode"
INITIALIZE_EMPTY_ATTRIBUTE = "source_gate_initialize_empty"
REQUIRE_EMPTY_ATTRIBUTE = "source_gate_require_empty"


def _has_schema_objects(connection: Connection) -> bool:
    """Return whether the SQLite file contains any schema objects."""
    rows = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index', 'view', 'trigger')"
    )
    return rows.first() is not None


def _classify_initialization_state(
    connection: Connection,
    *,
    allow_empty_initialization: bool,
    require_empty_target: bool,
) -> str:
    """Validate a supported starting state before Alembic writes anything."""
    if require_empty_target and _has_schema_objects(connection):
        raise RuntimeError("source gate require-empty target contains schema objects")

    marker = schema_contract.read_migration_marker(connection)
    if marker is not None:
        schema_contract.validate_migration_state(connection)
        return f"existing-marked:{marker}"

    try:
        schema_contract.validate_v1_schema(connection)
        return "adopt-existing-v1"
    except RuntimeError as v1_error:
        if _has_schema_objects(connection):
            raise
        # Preserve the pinned 0015 source-integrity check before treating this
        # mismatch as the expected empty-file state.
        schema_contract._reference_schema(include_v2=False)
        if not allow_empty_initialization:
            raise RuntimeError(
                "an empty source-gate SQLite file requires --initialize-empty"
            ) from v1_error
        encoding = connection.exec_driver_sql("PRAGMA encoding").scalar_one()
        if encoding != "UTF-8":
            raise RuntimeError(
                "empty source-gate SQLite file must use UTF-8 encoding"
            ) from v1_error
        return "initialize-empty"


def _explicit_database_path() -> tuple[Path, path_contract.DatabaseFileIdentity]:
    """Return the validated target path and its expected file identity."""
    value = config.attributes.get("source_gate_path")
    if value is None:
        value = context.get_x_argument(as_dictionary=True).get("source_gate_path")
    if not isinstance(value, (str, Path)):
        raise RuntimeError(
            "source gate target is unset; pass --database-path to migrate_source_gate.py"
        )
    expected_identity = config.attributes.get("source_gate_file_identity")
    if expected_identity is None:
        return path_contract.inspect_existing_database_path(value)
    if not isinstance(expected_identity, path_contract.DatabaseFileIdentity):
        raise RuntimeError("source gate target identity is malformed")
    path = path_contract.validate_existing_database_path(value)
    path_contract.assert_database_file_identity(path, expected_identity)
    return path, expected_identity


def _open_existing_database(
    path: Path, expected_identity: path_contract.DatabaseFileIdentity
) -> sqlite3.Connection:
    """Open only an existing SQLite file; mode=rw prevents implicit creation."""
    path_contract.assert_database_file_identity(path, expected_identity)
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=rw"
    connection = sqlite3.connect(uri, uri=True, timeout=30.0)
    try:
        path_contract.assert_database_file_identity(path, expected_identity)
    except BaseException:
        connection.close()
        raise
    return connection


def _configure_sqlite(connection: sqlite3.Connection, _record: object) -> None:
    """Enable foreign-key enforcement before the migration transaction starts."""
    cursor = connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _run_migrations(
    connection: Connection,
    path: Path,
    expected_identity: path_contract.DatabaseFileIdentity,
) -> None:
    """Preflight, migrate, and verify the target under one explicit write lock."""
    # The DBAPI connection is open, but no target schema has been read yet.
    path_contract.assert_database_file_identity(path, expected_identity)
    allow_empty_initialization = config.attributes.get(INITIALIZE_EMPTY_ATTRIBUTE) is True
    require_empty_target = config.attributes.get(REQUIRE_EMPTY_ATTRIBUTE) is True
    initial_state = _classify_initialization_state(
        connection,
        allow_empty_initialization=allow_empty_initialization,
        require_empty_target=require_empty_target,
    )

    # End SQLAlchemy's read transaction from preflight before acquiring the
    # write reservation. The raw BEGIN is the first statement of this new
    # transaction, so Alembic's version table and both revisions share it.
    connection.rollback()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Check again after acquiring SQLite's write lock, before any migration DDL.
        path_contract.assert_database_file_identity(path, expected_identity)
        locked_state = _classify_initialization_state(
            connection,
            allow_empty_initialization=allow_empty_initialization,
            require_empty_target=require_empty_target,
        )
        if locked_state != initial_state:
            raise RuntimeError("source gate schema changed between preflight and write lock")
        config.attributes[INITIALIZATION_MODE_ATTRIBUTE] = locked_state
        context.configure(
            connection=connection,
            target_metadata=None,
            version_table=schema_contract.VERSION_TABLE,
            transactional_ddl=True,
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()
        schema_contract.validate_migration_state(connection)
        # If the pathname changed while migrating, leave the opened inode untouched.
        path_contract.assert_database_file_identity(path, expected_identity)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def run_migrations_online() -> None:
    """Run the source-gate chain against one explicitly named SQLite file."""
    path, expected_identity = _explicit_database_path()
    connectable = create_engine(
        "sqlite+pysqlite://",
        creator=lambda: _open_existing_database(path, expected_identity),
        poolclass=pool.NullPool,
    )
    event.listen(connectable, "connect", _configure_sqlite)
    try:
        with connectable.connect() as connection:
            if connection.dialect.name != "sqlite":
                raise RuntimeError("source gate migrations support SQLite files only")
            _run_migrations(connection, path, expected_identity)
    finally:
        connectable.dispose()


def run_migrations_offline() -> None:
    """Reject offline SQL generation because adoption needs schema inspection."""
    raise RuntimeError("offline migration is unsupported; an existing SQLite file is required")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
