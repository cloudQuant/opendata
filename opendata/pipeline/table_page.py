"""Table pagination that works with and without an ``id`` column (A4.3).

The legacy ``/tables`` endpoint paginated with a hardcoded
``ORDER BY id``. ods/dwd tables have no surrogate ``id`` - they are
keyed by the business key (design §8.1) - so the order clause must be
derived from the actual table shape: ``id`` when it exists, otherwise
the primary key, otherwise the first column so the order stays
deterministic.

``table_shape`` reads the shape through SQLAlchemy's inspector, which
keeps it dialect-agnostic (MySQL in production, SQLite in tests).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.engine import Connection

#: Word characters only (Unicode letters included), never a backtick.
_IDENTIFIER_RE = re.compile(r"^[^\W\d]\w*$", re.UNICODE)


def order_columns(columns: Sequence[str], key_columns: Sequence[str]) -> list[str]:
    """Choose the deterministic ordering columns for a table page.

    Args:
        columns: Table columns in table order.
        key_columns: Primary-key columns in key order.

    Returns:
        ``["id"]`` when the table has a surrogate id, else the primary
        key, else the first column; empty for a table without columns.

    Raises:
        ValueError: If ``key_columns`` names a column the table lacks.
    """
    if "id" in columns:
        return ["id"]
    unknown = [column for column in key_columns if column not in columns]
    if unknown:
        raise ValueError(f"key columns {unknown} are not columns of the table")
    if key_columns:
        return list(key_columns)
    return list(columns[:1])


def page_sql(table: str, *, columns: Sequence[str], key_columns: Sequence[str]) -> str:
    """Build the paginated ``SELECT`` for one table page.

    Args:
        table: Table name (validated before quoting).
        columns: Table columns in table order.
        key_columns: Primary-key columns in key order.

    Returns:
        ``SELECT *`` ordered deterministically with limit/offset bind
        parameters.

    Raises:
        ValueError: If the table name is not a safe identifier.
    """
    if not _IDENTIFIER_RE.match(table):
        raise ValueError(f"invalid SQL identifier {table!r}")
    ordering = ", ".join(f"`{column}`" for column in order_columns(columns, key_columns))
    if not ordering:
        raise ValueError(f"table {table!r} has no columns to order by")
    return f"SELECT * FROM `{table}` ORDER BY {ordering} LIMIT :limit OFFSET :offset"


def table_shape(connection: Connection, table: str) -> tuple[list[str], list[str]]:
    """Read a table's columns and primary key through the inspector.

    Args:
        connection: Sync SQLAlchemy connection (warehouse database);
            callers holding an ``AsyncSession`` unwrap it with
            ``run_sync(lambda session: table_shape(session.connection(), ...))``.
        table: Table name.

    Returns:
        ``(columns, key_columns)`` in declaration order; the key list is
        empty when the table declares no primary key.
    """
    from sqlalchemy import inspect

    inspector = inspect(connection)
    columns = [column["name"] for column in inspector.get_columns(table)]
    primary_key = inspector.get_pk_constraint(table) or {}
    ordered = primary_key.get("constrained_columns") or []
    return columns, list(ordered)
