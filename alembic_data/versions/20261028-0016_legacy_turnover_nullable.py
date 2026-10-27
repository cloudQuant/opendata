"""Allow legacy stock-daily transfer to preserve missing turnover amounts.

Revision ID: 0016_legacy_turnover_nullable
Revises: 0015_source_attempt_gate
Create Date: 2026-10-28

Only ``ods_stock_daily_akshare.成交额`` becomes nullable. This migration never
rewrites or backfills rows. It requires an online SQLite or MySQL 8.0.16+
connection so it can validate the existing table shape before changing it.
Downgrade is intentionally refused because older consumers may depend on the
nullable rows created after this revision.
"""

import re
from collections.abc import Iterable, Sequence

from sqlalchemy import Double, inspect, text

from alembic import op

revision = "0016_legacy_turnover_nullable"
down_revision = "0015_source_attempt_gate"
branch_labels = None
depends_on = None

_TABLE = "ods_stock_daily_akshare"
_COLUMN = "成交额"
_MYSQL_EXPECTED_PARTITIONS = (
    ("p2024", "range columns", "日期", "2025-01-01"),
    ("p2025", "range columns", "日期", "2026-01-01"),
    ("p2026", "range columns", "日期", "2027-01-01"),
    ("pmax", "range columns", "日期", "MAXVALUE"),
)
_COLUMNS = {
    "日期": ("date", False),
    "股票代码": ("varchar(64)", False),
    "开盘": ("double", False),
    "收盘": ("double", False),
    "最高": ("double", False),
    "最低": ("double", False),
    "成交量": ("double", False),
    "成交额": ("double", False),
    "振幅": ("double", True),
    "涨跌幅": ("double", True),
    "涨跌额": ("double", True),
    "换手率": ("double", True),
    "_source": ("varchar(32)", False),
    "_fetched_at": ("datetime", False),
    "_batch_id": ("char(36)", False),
}


def _normalize_mysql_partition_rows(
    rows: Iterable[Sequence[object]],
) -> tuple[tuple[str, str, str, str], ...]:
    """Normalize MySQL partition metadata to a stable comparison shape."""
    partitions: list[tuple[str, str, str, str]] = []
    for row in rows:
        if len(row) != 4:
            raise RuntimeError("revision 0016 cannot inspect the full MySQL partition metadata")
        name, method, expression, description = row
        normalized_description = str(description).strip().strip("'")
        if normalized_description.casefold() == "maxvalue":
            normalized_description = "MAXVALUE"
        partitions.append(
            (
                str(name).casefold(),
                str(method).casefold(),
                str(expression).replace("`", "").strip(),
                normalized_description,
            )
        )
    return tuple(partitions)


def _require_mysql_partition_layout(rows: Iterable[Sequence[object]]) -> None:
    """Require the migration's reviewed MySQL partition method, key, and bounds."""
    if _normalize_mysql_partition_rows(rows) != _MYSQL_EXPECTED_PARTITIONS:
        raise RuntimeError(
            "revision 0016 requires the reviewed ODS partition layout; "
            "reconcile partition maintenance before upgrading"
        )


def _require_simple_sqlite_index(index: dict[str, object], definition: str) -> None:
    """Reject SQLite index semantics that batch reflection may not preserve."""
    columns = index.get("column_names")
    options = index.get("dialect_options")
    unique = index.get("unique")
    if (
        not (unique is False or (type(unique) is int and unique == 0))
        or not isinstance(columns, (list, tuple))
        or not columns
        or any(not isinstance(name, str) or not name for name in columns)
        or not isinstance(options, dict)
        or options.get("sqlite_where") is not None
        or not definition
        or re.search(r"\b(?:ASC|DESC|COLLATE|WHERE)\b", definition, re.IGNORECASE)
    ):
        raise RuntimeError("revision 0016 refuses unsupported SQLite index definitions")


def _require_supported_target() -> str:
    """Require an online SQLite or supported MySQL warehouse connection."""
    migration_context = op.get_context()
    if getattr(migration_context, "as_sql", False):
        raise RuntimeError(
            "revision 0016 requires an online connection to inspect the target table"
        )

    dialect = migration_context.dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0016 supports MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0016 supports SQLite and MySQL 8.0.16+ only")

    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise RuntimeError("revision 0016 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _require_legacy_table(mode: str) -> None:
    """Fail closed unless the old ODS table matches its reviewed schema."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("revision 0016 requires ods_stock_daily_akshare to exist")

    columns = inspector.get_columns(_TABLE)
    actual = {
        str(column["name"]): (str(column.get("type", "")).casefold(), column.get("nullable"))
        for column in columns
    }
    if actual != _COLUMNS:
        raise RuntimeError("revision 0016 requires the reviewed pre-migration ODS column shape")
    if any(column.get("default") is not None for column in columns):
        raise RuntimeError("revision 0016 refuses ODS columns with server defaults")
    if any(column.get("computed") or column.get("identity") for column in columns):
        raise RuntimeError("revision 0016 refuses computed or identity ODS columns")

    if inspector.get_pk_constraint(_TABLE).get("constrained_columns") != ["股票代码", "日期"]:
        raise RuntimeError("revision 0016 requires the legacy stock-daily primary key")
    if inspector.get_foreign_keys(_TABLE):
        raise RuntimeError("revision 0016 refuses ODS tables with foreign keys")
    if inspector.get_unique_constraints(_TABLE):
        raise RuntimeError("revision 0016 refuses ODS tables with extra unique constraints")
    if inspector.get_check_constraints(_TABLE):
        raise RuntimeError("revision 0016 refuses ODS tables with extra check constraints")

    if mode == "mysql":
        engine = inspector.get_table_options(_TABLE).get("mysql_engine")
        if not isinstance(engine, str) or engine.casefold() != "innodb":
            raise RuntimeError("revision 0016 requires an InnoDB target table")
        partition_rows = bind.execute(
            text(
                "SELECT PARTITION_NAME, PARTITION_METHOD, PARTITION_EXPRESSION, "
                "PARTITION_DESCRIPTION FROM information_schema.PARTITIONS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                "ORDER BY PARTITION_ORDINAL_POSITION"
            ),
            {"table": _TABLE},
        ).all()
        _require_mysql_partition_layout(partition_rows)
    else:
        triggers = (
            bind.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
                {"table": _TABLE},
            )
            .scalars()
            .all()
        )
        if triggers:
            raise RuntimeError("revision 0016 refuses SQLite ODS tables with triggers")
        table_sql = bind.execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table"),
            {"table": _TABLE},
        ).scalar_one_or_none()
        normalized_sql = " ".join(str(table_sql or "").upper().split()).rstrip(";")
        if (
            not normalized_sql
            or "WITHOUT ROWID" in normalized_sql
            or normalized_sql.endswith(" STRICT")
            or " COLLATE " in normalized_sql
            or " ON CONFLICT " in normalized_sql
        ):
            raise RuntimeError("revision 0016 refuses unsupported SQLite table options")
        index_sql = {
            str(name): str(sql or "")
            for name, sql in bind.execute(
                text(
                    "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table"
                ),
                {"table": _TABLE},
            ).all()
        }
        for index in inspector.get_indexes(_TABLE):
            definition = index_sql.get(str(index.get("name")), "")
            _require_simple_sqlite_index(index, definition)


def upgrade() -> None:
    """Make only the raw turnover amount nullable without changing row values."""
    mode = _require_supported_target()
    _require_legacy_table(mode)
    if mode == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.alter_column(
                _COLUMN,
                existing_type=Double(),
                existing_nullable=False,
                nullable=True,
            )
        return

    op.alter_column(
        _TABLE,
        _COLUMN,
        existing_type=Double(),
        existing_nullable=False,
        nullable=True,
    )


def downgrade() -> None:
    """Refuse rollback so a downgrade can never coerce or discard NULL values."""
    raise RuntimeError(
        "revision 0016 downgrade is refused; preserve nullable turnover data and upgrade forward"
    )
