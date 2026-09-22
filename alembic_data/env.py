"""Alembic environment for the data warehouse (design §8.1, milestone A4.1).

Independent from the application environment: it targets the
warehouse database (``settings.data_database_url``), records its own
version table (``alembic_version_data``) and has **no**
``target_metadata`` - the warehouse tables are hand-written DDL
rendered from :mod:`opendata.pipeline.ddl`, so there is nothing to
autogenerate against. Migrations call the generator, which keeps the
generated schema and the migration chain in sync by construction.

The URL can be overridden through the alembic config (tests do this
with a scratch target); otherwise the warehouse URL from settings is
used. The database itself must already exist.
"""

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

# Ensure the project root is importable so migrations can import
# opendata.pipeline.ddl.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import context
from opendata.core.config import settings

# Alembic config object.
config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# An explicitly provided URL wins; otherwise the warehouse URL.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", settings.data_database_url)

# Warehouse tables come from opendata/pipeline/ddl.py, not from ORM models.
target_metadata = None

#: Dedicated version table, separate from the application's alembic_version.
VERSION_TABLE = "alembic_version_data"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL, no database)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table=VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run migrations on an open connection."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


def _configured_url() -> str:
    """Return the resolved database URL, failing closed when unset.

    Returns:
        The alembic-configured or settings-provided warehouse URL.

    Raises:
        RuntimeError: If neither source provides a URL.
    """
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("no sqlalchemy.url configured for the warehouse environment")
    return url


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against the warehouse."""
    connectable = create_engine(
        _configured_url(),
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        do_run_migrations(connection)

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
