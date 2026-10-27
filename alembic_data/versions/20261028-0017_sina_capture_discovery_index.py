"""Add the bounded lookup index required by the disabled capture-discovery prototype.

Revision ID: 0017_sina_capture_discovery_index
Revises: 0016_legacy_turnover_nullable
Create Date: 2026-10-28

This migration adds one secondary index to ``od_published_fact_versions``. It
does not enable or certify the capture-discovery API, its physical scan bound,
or any historical completeness claim. MySQL DDL is online-only and refuses a
copy algorithm or an exclusive table lock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    String,
    column,
    inspect,
    select,
    table,
    text,
)

from alembic import op

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

revision = "0017_sina_capture_discovery_index"
down_revision = "0016_legacy_turnover_nullable"
branch_labels = None
depends_on = None

_TABLE = "od_published_fact_versions"
_VERSION_TABLE_FALLBACK = "alembic_version_data"
_OLD_INDEX = "ix_od_fact_capture_read"
_INDEX = "ix_od_fact_capture_discovery"
_SQLITE_COLLATION_PROBE_INDEX = "ix_od_fact_capture_discovery_probe"
_OLD_INDEX_COLUMNS = (
    "dataset_id",
    "instrument_id",
    "capture_id",
    "publication_sequence",
)
_INDEX_COLUMNS = (
    "dataset_id",
    "publication_id",
    "instrument_id",
    "publication_sequence",
    "is_deleted",
    "observed_at",
    "capture_id",
)
_TEXT_KEY_LENGTHS = {
    "dataset_id": 128,
    "publication_id": 32,
    "instrument_id": 128,
    "observed_at": 40,
    "capture_id": 64,
}
_MYSQL_MDL_TIMEOUT_SECONDS = 10
_MYSQL_PAGE_SIZE_BYTES = 16_384
_MYSQL_MAX_KEY_BYTES = 3_072


def _require_supported_target() -> str:
    """Require a live SQLite or MySQL 8.0.16+ connection."""
    migration_context = op.get_context()
    if getattr(migration_context, "as_sql", False):
        raise RuntimeError("revision 0017 requires a live connection for schema and index checks")

    dialect = migration_context.dialect
    if dialect.name == "sqlite":
        return "sqlite"
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("revision 0017 supports MySQL 8.0.16+ but not MariaDB")
    if dialect.name != "mysql":
        raise RuntimeError("revision 0017 supports SQLite and MySQL 8.0.16+ only")

    version = dialect.server_version_info
    if (
        not isinstance(version, tuple)
        or len(version) < 3
        or any(type(part) is not int for part in version[:3])
        or version[0] != 8
        or version[:3] < (8, 0, 16)
    ):
        raise RuntimeError("revision 0017 requires MySQL 8.0.16 or later within MySQL 8")
    return "mysql"


def _version_table_name(migration_context: object) -> str:
    """Return the configured marker table, using the warehouse default if absent."""
    version_table = getattr(migration_context, "version_table", None)
    if version_table is None:
        version_table = _VERSION_TABLE_FALLBACK
    if not isinstance(version_table, str) or not version_table.strip():
        raise RuntimeError("revision 0017 requires a valid Alembic version table name")
    return version_table


def _require_revision_marker(expected: str, mode: str) -> None:
    """Validate the single exact Alembic marker before any DDL."""
    migration_context = op.get_context()
    version_table = _version_table_name(migration_context)
    version_schema = getattr(migration_context, "version_table_schema", None)
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(version_table, schema=version_schema):
        raise RuntimeError("revision 0017 requires the configured Alembic version table")

    columns = inspector.get_columns(version_table, schema=version_schema)
    if len(columns) != 1 or columns[0].get("name") != "version_num":
        raise RuntimeError("revision 0017 requires a version table containing only version_num")
    version_column = columns[0]
    version_type = version_column.get("type")
    if (
        not isinstance(version_type, String)
        or getattr(version_type, "__visit_name__", "").upper() != "VARCHAR"
        or not isinstance(version_type.length, int)
        or version_column.get("nullable") is not False
        or version_column.get("default") is not None
    ):
        raise RuntimeError("revision 0017 requires a bounded, non-null VARCHAR version_num")
    if mode == "mysql" and version_type.length < max(len(expected), len(revision)):
        raise RuntimeError("revision 0017 requires a version_num wide enough for both revision IDs")
    if inspector.get_pk_constraint(version_table, schema=version_schema).get(
        "constrained_columns"
    ) != ["version_num"]:
        raise RuntimeError(
            "revision 0017 requires version_num as the sole version-table primary key"
        )

    version_ref = table(
        version_table,
        column("version_num", version_type),
        schema=version_schema,
    )
    rows = bind.execute(select(version_ref.c.version_num)).scalars().all()
    if rows != [expected]:
        raise RuntimeError(
            f"revision 0017 requires exactly one Alembic marker matching {expected}; "
            "unexpected or partial migration state requires offline verification"
        )


def _index_columns(index: Mapping[str, object]) -> tuple[str, ...] | None:
    """Read a plain, full-column index shape from SQLAlchemy reflection."""
    raw_columns = index.get("column_names")
    unique = index.get("unique")
    options = index.get("dialect_options")
    if (
        not (unique is False or (type(unique) is int and unique == 0))
        or not isinstance(raw_columns, (list, tuple))
        or any(not isinstance(name, str) or not name for name in raw_columns)
        or not isinstance(options, dict)
    ):
        return None
    if options.get("sqlite_where") is not None:
        return None
    if options.get("mysql_length") not in (None, {}):
        return None
    return tuple(raw_columns)


def _sqlite_index_sql(name: str) -> str | None:
    """Return a SQLite index declaration, if that name exists in this database."""
    row = (
        op.get_bind()
        .execute(
            text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name"),
            {"name": name},
        )
        .first()
    )
    return str(row[0]) if row is not None and row[0] is not None else None


def _sqlite_index_key_parts(name: str) -> tuple[Mapping[str, object], ...]:
    """Read effective collation and ordering for each actual SQLite index key part."""
    return tuple(
        op.get_bind()
        .execute(
            text(
                'SELECT seqno, name, "desc" AS descending, coll, "key" AS is_key '
                "FROM pragma_index_xinfo(:index_name) ORDER BY seqno"
            ),
            {"index_name": name},
        )
        .mappings()
        .all()
    )


def _require_sqlite_plain_index(name: str, expected_columns: tuple[str, ...]) -> None:
    """Reject SQLite index definitions with expressions or altered comparison order."""
    inspector = inspect(op.get_bind())
    matches = [item for item in inspector.get_indexes(_TABLE) if item.get("name") == name]
    if len(matches) != 1 or _index_columns(matches[0]) != expected_columns:
        raise RuntimeError(f"revision 0017 requires the reviewed SQLite {name} index shape")
    definition = _sqlite_index_sql(name)
    if (
        not definition
        or " WHERE " in definition.upper()
        or "CREATE INDEX" not in definition.upper()
    ):
        raise RuntimeError(f"revision 0017 requires the reviewed SQLite {name} index shape")
    key_parts = tuple(row for row in _sqlite_index_key_parts(name) if row.get("is_key") == 1)
    if (
        len(key_parts) != len(expected_columns)
        or tuple(row.get("name") for row in key_parts) != expected_columns
        or any(row.get("descending") not in (0, False) for row in key_parts)
    ):
        raise RuntimeError(f"revision 0017 requires the reviewed SQLite {name} index shape")
    if any(str(row.get("coll", "")).casefold() != "binary" for row in key_parts):
        raise RuntimeError(
            f"revision 0017 requires BINARY collation for the reviewed SQLite {name} index"
        )


def _require_sqlite_discovery_column_collations() -> None:
    """Probe inherited key collations before building the full discovery index."""
    inspector = inspect(op.get_bind())
    if any(
        item.get("name") == _SQLITE_COLLATION_PROBE_INDEX for item in inspector.get_indexes(_TABLE)
    ):
        raise RuntimeError(
            "revision 0017 found a stale SQLite collation probe; inspect partial DDL offline"
        )

    probe_created = False
    validation_error: BaseException | None = None
    try:
        op.create_index(
            _SQLITE_COLLATION_PROBE_INDEX,
            _TABLE,
            list(_INDEX_COLUMNS),
            unique=False,
            sqlite_where=text("0"),
        )
        probe_created = True
        key_parts = tuple(
            row
            for row in _sqlite_index_key_parts(_SQLITE_COLLATION_PROBE_INDEX)
            if row.get("is_key") == 1
        )
        if (
            len(key_parts) != len(_INDEX_COLUMNS)
            or tuple(row.get("name") for row in key_parts) != _INDEX_COLUMNS
            or any(row.get("descending") not in (0, False) for row in key_parts)
            or any(str(row.get("coll", "")).casefold() != "binary" for row in key_parts)
        ):
            raise RuntimeError(
                "revision 0017 requires BINARY collation for every SQLite discovery key column"
            )
    except BaseException as error:
        validation_error = error
        raise
    finally:
        if probe_created:
            try:
                op.drop_index(_SQLITE_COLLATION_PROBE_INDEX, table_name=_TABLE)
            except Exception as cleanup_error:
                if validation_error is not None:
                    raise RuntimeError(
                        "revision 0017 collation validation failed and the SQLite probe "
                        "index could not be removed"
                    ) from cleanup_error
                raise RuntimeError(
                    "revision 0017 could not remove the SQLite collation probe"
                ) from (cleanup_error)


def _mysql_statistics(index_name: str) -> tuple[Mapping[str, object], ...]:
    """Read exact MySQL index parts, including visibility and prefix metadata."""
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART, "
                "INDEX_TYPE, IS_VISIBLE FROM information_schema.STATISTICS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
                "AND INDEX_NAME = :index_name ORDER BY SEQ_IN_INDEX"
            ),
            {"table": _TABLE, "index_name": index_name},
        )
        .mappings()
        .all()
    )
    return tuple(rows)


def _require_mysql_index_shape(index_name: str, expected_columns: tuple[str, ...]) -> None:
    """Require a visible, full-column, non-unique InnoDB BTREE index."""
    rows = _mysql_statistics(index_name)
    if len(rows) != len(expected_columns):
        raise RuntimeError(f"revision 0017 requires the reviewed MySQL {index_name} index shape")
    for position, row in enumerate(rows, start=1):
        if (
            row.get("INDEX_NAME") != index_name
            or row.get("NON_UNIQUE") not in (1, True)
            or row.get("SEQ_IN_INDEX") != position
            or row.get("COLUMN_NAME") != expected_columns[position - 1]
            or row.get("SUB_PART") is not None
            or str(row.get("INDEX_TYPE", "")).upper() != "BTREE"
            or str(row.get("IS_VISIBLE", "")).upper() != "YES"
        ):
            raise RuntimeError(
                f"revision 0017 requires the reviewed MySQL {index_name} index shape"
            )


def _mysql_storage_metadata() -> tuple[str, int]:
    """Return the InnoDB row format and page size used for the index-width guard."""
    row = (
        op.get_bind()
        .execute(
            text(
                "SELECT t.ENGINE, t.ROW_FORMAT, @@GLOBAL.innodb_page_size AS PAGE_SIZE "
                "FROM information_schema.TABLES AS t "
                "WHERE t.TABLE_SCHEMA = DATABASE() AND t.TABLE_NAME = :table"
            ),
            {"table": _TABLE},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise RuntimeError("revision 0017 cannot inspect the MySQL publication table storage")
    engine = str(row.get("ENGINE", "")).casefold()
    row_format = str(row.get("ROW_FORMAT", "")).casefold()
    page_size = row.get("PAGE_SIZE")
    if engine != "innodb" or row_format not in {"dynamic", "compressed"}:
        raise RuntimeError("revision 0017 requires an InnoDB DYNAMIC or COMPRESSED table")
    if type(page_size) is not int or page_size != _MYSQL_PAGE_SIZE_BYTES:
        raise RuntimeError("revision 0017 requires 16 KiB InnoDB pages for the full-width index")
    return row_format, page_size


def _mysql_index_key_bytes() -> int:
    """Validate indexed MySQL column widths and calculate full UTF-8 key bytes."""
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE, IS_NULLABLE, "
                "CHARACTER_MAXIMUM_LENGTH, CHARACTER_OCTET_LENGTH, CHARACTER_SET_NAME, "
                "COLLATION_NAME "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
            ),
            {"table": _TABLE},
        )
        .mappings()
        .all()
    )
    by_name: dict[str, Mapping[str, object]] = {str(row["COLUMN_NAME"]): row for row in rows}
    if not set(_INDEX_COLUMNS).issubset(by_name):
        raise RuntimeError("revision 0017 cannot inspect every MySQL indexed column")

    total = 0
    for name, expected_length in _TEXT_KEY_LENGTHS.items():
        column_row = by_name[name]
        if (
            str(column_row.get("DATA_TYPE", "")).casefold() != "varchar"
            or column_row.get("CHARACTER_MAXIMUM_LENGTH") != expected_length
            or str(column_row.get("CHARACTER_SET_NAME", "")).casefold() != "utf8mb4"
            or column_row.get("CHARACTER_OCTET_LENGTH") != expected_length * 4
        ):
            raise RuntimeError(
                f"revision 0017 requires {name} to use its reviewed utf8mb4 VARCHAR width"
            )
        if (
            name in {"dataset_id", "instrument_id", "capture_id"}
            and str(column_row.get("COLLATION_NAME", "")).casefold() != "utf8mb4_0900_bin"
        ):
            raise RuntimeError(
                f"revision 0017 requires {name} to preserve the reviewed binary collation"
            )
        total += int(column_row["CHARACTER_OCTET_LENGTH"])

    sequence = by_name["publication_sequence"]
    if (
        str(sequence.get("DATA_TYPE", "")).casefold() != "bigint"
        or str(sequence.get("COLUMN_TYPE", "")).casefold() != "bigint"
        or sequence.get("IS_NULLABLE") != "NO"
    ):
        raise RuntimeError("revision 0017 requires a non-null signed BIGINT publication_sequence")
    deleted = by_name["is_deleted"]
    if (
        str(deleted.get("DATA_TYPE", "")).casefold() != "tinyint"
        or str(deleted.get("COLUMN_TYPE", "")).casefold() != "tinyint(1)"
        or deleted.get("IS_NULLABLE") != "NO"
    ):
        raise RuntimeError("revision 0017 requires a non-null TINYINT(1) is_deleted column")
    total += 8 + 1
    if total > _MYSQL_MAX_KEY_BYTES:
        raise RuntimeError("revision 0017 full-width index exceeds the InnoDB key-size limit")
    return total


def _require_mysql_storage_budget() -> None:
    """Require the reviewed full-width key to fit the physical InnoDB page limit."""
    _mysql_storage_metadata()
    key_bytes = _mysql_index_key_bytes()
    if key_bytes > _MYSQL_MAX_KEY_BYTES:
        raise RuntimeError(
            "revision 0017 full-width utf8mb4 index does not fit this InnoDB page size"
        )


def _require_table_and_indexes(mode: str, *, downgrade: bool) -> None:
    """Validate the publication table, its current capture index, and target state."""
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table(_TABLE):
        raise RuntimeError("revision 0017 requires od_published_fact_versions to exist")

    expected_columns: dict[str, tuple[type[Any], int | None, bool]] = {
        "dataset_id": (String, 128, False),
        "publication_id": (String, 32, False),
        "instrument_id": (String, 128, True),
        "publication_sequence": (BigInteger, None, False),
        "is_deleted": (Boolean, None, False),
        "observed_at": (String, 40, True),
        "capture_id": (String, 64, True),
    }
    columns = {str(item.get("name")): item for item in inspector.get_columns(_TABLE)}
    for name, (expected_type, expected_length, nullable) in expected_columns.items():
        item = columns.get(name)
        actual_type = item.get("type") if item is not None else None
        if (
            item is None
            or not _matches_column_type(actual_type, expected_type, name, mode)
            or (
                expected_length is not None
                and getattr(actual_type, "length", None) != expected_length
            )
            or item.get("nullable") is not nullable
            or item.get("default") is not None
            or item.get("computed")
            or item.get("identity")
        ):
            raise RuntimeError(
                f"revision 0017 requires the reviewed {name} column type, width, and nullability"
            )
    if inspector.get_pk_constraint(_TABLE).get("constrained_columns") != [
        "dataset_id",
        "business_key",
        "revision",
    ]:
        raise RuntimeError("revision 0017 requires the reviewed publication fact primary key")

    if mode == "sqlite":
        _require_sqlite_plain_index(_OLD_INDEX, _OLD_INDEX_COLUMNS)
        target_sql = _sqlite_index_sql(_INDEX)
        target_indexes = [
            item for item in inspector.get_indexes(_TABLE) if item.get("name") == _INDEX
        ]
        if downgrade:
            _require_sqlite_plain_index(_INDEX, _INDEX_COLUMNS)
        elif target_sql is not None or target_indexes:
            raise RuntimeError(
                "revision 0017 found a pre-existing discovery index; inspect partial DDL offline"
            )
        else:
            _require_sqlite_discovery_column_collations()
        return

    options = inspector.get_table_options(_TABLE)
    engine = options.get("mysql_engine")
    if not isinstance(engine, str) or engine.casefold() != "innodb":
        raise RuntimeError("revision 0017 requires an InnoDB publication fact table")
    _require_mysql_storage_budget()
    _require_mysql_index_shape(_OLD_INDEX, _OLD_INDEX_COLUMNS)
    target_rows = _mysql_statistics(_INDEX)
    if downgrade:
        _require_mysql_index_shape(_INDEX, _INDEX_COLUMNS)
    elif target_rows:
        raise RuntimeError(
            "revision 0017 found a pre-existing discovery index; inspect partial DDL offline"
        )


def _matches_column_type(
    actual_type: object,
    expected_type: type[Any],
    column_name: str,
    mode: str,
) -> bool:
    """Account for MySQL reflecting SQLAlchemy Boolean as TINYINT(1)."""
    if column_name == "is_deleted" and mode == "mysql":
        return isinstance(actual_type, Boolean) or (
            getattr(actual_type, "__visit_name__", "").upper() == "TINYINT"
        )
    return isinstance(actual_type, expected_type)


def _quote_mysql_identifier(identifier: str) -> str:
    """Quote a static migration identifier with the active MySQL dialect."""
    return op.get_bind().dialect.identifier_preparer.quote_identifier(identifier)


def _mysql_index_ddl(*, drop: bool) -> str:
    """Build explicit online DDL so MySQL cannot silently fall back to COPY or EXCLUSIVE."""
    table_name = _quote_mysql_identifier(_TABLE)
    index_name = _quote_mysql_identifier(_INDEX)
    if drop:
        return f"ALTER TABLE {table_name} DROP INDEX {index_name}, ALGORITHM=INPLACE, LOCK=NONE"
    columns = ", ".join(_quote_mysql_identifier(name) for name in _INDEX_COLUMNS)
    return (
        f"ALTER TABLE {table_name} ADD INDEX {index_name} ({columns}), ALGORITHM=INPLACE, LOCK=NONE"
    )


def _execute_mysql_online_ddl(statement: str) -> None:
    """Bound metadata-lock wait for one online index DDL and restore session state."""
    bind = op.get_bind()
    previous = bind.exec_driver_sql("SELECT @@SESSION.lock_wait_timeout").scalar_one()
    if type(previous) is not int or previous < 1:
        raise RuntimeError("revision 0017 cannot safely inspect the current metadata-lock timeout")
    bind.exec_driver_sql(f"SET SESSION lock_wait_timeout = {_MYSQL_MDL_TIMEOUT_SECONDS}")
    try:
        bind.exec_driver_sql(statement)
    except BaseException:
        try:
            bind.exec_driver_sql(f"SET SESSION lock_wait_timeout = {previous}")
        except Exception as restore_error:
            raise RuntimeError(
                "revision 0017 DDL failed and the session metadata-lock timeout "
                "could not be restored"
            ) from restore_error
        raise
    else:
        bind.exec_driver_sql(f"SET SESSION lock_wait_timeout = {previous}")


def upgrade() -> None:
    """Add the full-column index while keeping capture discovery disabled."""
    mode = _require_supported_target()
    _require_revision_marker(down_revision, mode)
    _require_table_and_indexes(mode, downgrade=False)
    if mode == "sqlite":
        op.create_index(_INDEX, _TABLE, list(_INDEX_COLUMNS), unique=False)
        return
    _execute_mysql_online_ddl(_mysql_index_ddl(drop=False))


def downgrade() -> None:
    """Drop only the reviewed secondary index after validating its exact shape."""
    mode = _require_supported_target()
    _require_revision_marker(revision, mode)
    _require_table_and_indexes(mode, downgrade=True)
    if mode == "sqlite":
        op.drop_index(_INDEX, table_name=_TABLE)
        return
    _execute_mysql_online_ddl(_mysql_index_ddl(drop=True))
