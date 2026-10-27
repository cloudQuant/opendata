"""Run the isolated financial archive migration chain.

The target URL must be supplied explicitly with ``-x financial_archive_url``.
This module does not import application settings or read environment
credentials, and it refuses every database dialect except supported MySQL 8
and disposable SQLite test targets.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import URL, Connection, make_url
from sqlalchemy.exc import ArgumentError

from alembic import context

config = context.config
VERSION_TABLE = "alembic_version_financial_archive"
URL_ARGUMENT = "financial_archive_url"


def _explicit_url() -> URL:
    """Load and validate a URL explicitly passed to this migration invocation.

    Returns:
        The explicit target SQLAlchemy URL.

    Raises:
        RuntimeError: If the URL is absent, malformed, or targets an unsupported
            backend.
    """
    arguments = context.get_x_argument(as_dictionary=True)
    raw_url = arguments.get(URL_ARGUMENT) or config.get_main_option("sqlalchemy.url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise RuntimeError(
            "financial archive target is unset; pass -x financial_archive_url=<explicit URL>"
        )
    try:
        url = make_url(raw_url)
    except (ArgumentError, TypeError, ValueError) as exc:
        raise RuntimeError("financial archive target URL is invalid") from exc
    if url.get_backend_name() not in {"mysql", "sqlite"}:
        raise RuntimeError("financial archive supports MySQL 8.0.16+ and temporary SQLite only")
    if url.get_backend_name() == "sqlite":
        _require_temporary_sqlite_url(url)
    return url


def _is_below(path: Path, root: Path) -> bool:
    """Return whether a resolved path is inside a resolved directory."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_temporary_sqlite_url(url: URL) -> None:
    """Allow only in-memory SQLite or a named disposable file under tempdir."""
    database = url.database
    if database in {None, "", ":memory:"}:
        return
    if not isinstance(database, str) or database.startswith("file:"):
        raise RuntimeError("SQLite target must be in-memory or a disposable temporary file")
    path = Path(database).resolve()
    temporary_root = Path(tempfile.gettempdir()).resolve()
    if (
        not _is_below(path, temporary_root)
        or not path.name.startswith("test_financial_archive_")
        or path.suffix not in {".db", ".sqlite"}
    ):
        raise RuntimeError("SQLite target must be a test_financial_archive_*.db/.sqlite file")


def _validate_connection(connection: Connection) -> None:
    """Check the connected backend and MySQL server version before migration."""
    dialect = connection.dialect
    if dialect.name == "sqlite":
        url = connection.engine.url
        _require_temporary_sqlite_url(url)
        return
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("financial archive migrations do not support MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("financial archive supports MySQL 8.0.16+ and temporary SQLite only")
    version = dialect.server_version_info
    if version is None or len(version) < 3 or version[0] != 8 or version[:3] < (8, 0, 16):
        raise RuntimeError("financial archive requires MySQL 8.0.16 or later within MySQL 8")


def run_migrations_offline() -> None:
    """Reject offline SQL rendering because it cannot verify a MySQL server."""
    raise RuntimeError(
        "offline SQL rendering is unsupported; apply this migration to an explicitly "
        "configured supported database"
    )


def run_migrations_online() -> None:
    """Open only the explicit, prevalidated target and run this chain."""
    url = _explicit_url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    try:
        with connectable.connect() as connection:
            _validate_connection(connection)
            context.configure(connection=connection, version_table=VERSION_TABLE)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
