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
    schema_contract.validate_migration_state(connection)

    # End SQLAlchemy's read transaction from preflight before acquiring the
    # write reservation. The raw BEGIN is the first statement of this new
    # transaction, so Alembic's version table and both revisions share it.
    connection.rollback()
    try:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Check again after acquiring SQLite's write lock, before any migration DDL.
        path_contract.assert_database_file_identity(path, expected_identity)
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
