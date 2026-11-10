"""Database configuration for the ported collection scripts.

Ported from AFI's ``data_fetch/configs/db_config.py``. The original read
AFI-specific settings (``AKSHARE_DATA_DATABASE_URL``, ``SYNC_LOCAL_MYSQL_*``);
OpenData expresses the same warehouse connection through its
``data_mysql_*`` settings and the ``data_database_url`` property. The
``DB_CONFIG`` mapping shape (host/user/password/database/port) is preserved
because the 1044 ported scripts and the ``mysql_base`` cursor read those keys.
"""

from __future__ import annotations

from sqlalchemy.engine import make_url

from opendata.core.config import get_settings

settings = get_settings()


def _build_db_config() -> dict[str, str | int]:
    """Build the legacy DB_CONFIG mapping from OpenData's warehouse settings."""
    host = settings.data_mysql_host
    user = settings.data_mysql_user
    password = settings.data_mysql_password
    port = settings.data_mysql_port
    database = settings.data_mysql_database

    # Prefer parsing the explicit warehouse URL when it is a MySQL DSN, so a
    # full connection string still wins over the discrete field defaults.
    configured_url = (settings.data_database_url or "").strip()
    if configured_url:
        try:
            parsed = make_url(configured_url)
        except Exception:  # fall back to discrete settings on a bad URL
            parsed = None
        if parsed is not None and parsed.drivername.startswith("mysql"):
            return {
                "host": parsed.host or host,
                "user": parsed.username or user,
                "password": parsed.password or password,
                "database": parsed.database or database,
                "port": parsed.port or port,
            }

    return {
        "host": host,
        "user": user,
        "password": password,
        "database": database,
        "port": port,
    }


DB_CONFIG = _build_db_config()
