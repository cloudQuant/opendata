#!/usr/bin/env python3
"""Safely transfer the legacy AkShare stock-daily snapshot to an isolated ODS schema.

The default mode is offline. --apply is required for database access, and both
database URLs are read only from task-specific environment variables. The target
is never created or altered by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import resource
import struct
import sys
import time
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, Protocol
from uuid import uuid4

from sqlalchemy import (
    CHAR,
    Column,
    Date,
    DateTime,
    Float,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    insert,
    text,
)
from sqlalchemy.engine import URL, Connection, Engine, make_url

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_URL_ENV = "LEGACY_TRANSFER_SOURCE_URL"
TARGET_URL_ENV = "LEGACY_TRANSFER_TARGET_URL"
SOURCE_SCHEMA = "akshare_data"
SOURCE_TABLE = "STOCK_ZH_A_HIST"
TARGET_TABLE = "ods_stock_daily_akshare"
TARGET_SCHEMA_PREFIX = "opendata_c65_legacy_"
SOURCE_PORT = 3306
TARGET_PORT = 33565
EXPECTED_SOURCE_ROWS = 617_371
PAGE_SIZE = 1_000
SOURCE = "akshare"
MAX_EXACT_DOUBLE_INTEGER = 2**53

SOURCE_COLUMNS = (
    "日期",
    "股票代码",
    "开盘",
    "收盘",
    "最高",
    "最低",
    "成交量",
    "成交额",
    "振幅",
    "涨跌幅",
    "涨跌额",
    "换手率",
)
TARGET_KEY = ("股票代码", "日期")
OPTIONAL_FLOAT_FIELDS = frozenset({"振幅", "涨跌幅", "涨跌额"})
_NUMERIC_TEXT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
_STOCK_CODE = re.compile(r"^[0-9]{6}$")
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

SOURCE_COLUMN_TYPES = {
    "日期": "text",
    "股票代码": "text",
    "开盘": "double",
    "收盘": "double",
    "最高": "double",
    "最低": "double",
    "成交量": "bigint",
    "成交额": "double",
    "振幅": "double",
    "涨跌幅": "double",
    "涨跌额": "double",
    "换手率": "text",
}
TARGET_COLUMN_TYPES: dict[str, tuple[str, bool]] = {
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
EXPECTED_PARTITIONS = ("p2024", "p2025", "p2026", "pmax")
PROVENANCE_FILES = (
    "scripts/ops/migrate_legacy_stock_daily.py",
    "scripts/codemod/legacy_transfer.py",
    "alembic_data/versions/20260923-0001_ods_dwd_p0.py",
    "opendata/data/mappings/akshare.yaml",
    "docs/evidence/C65/legacy-current-inventory.json",
)


class TransferError(RuntimeError):
    """A failure with a safe, non-secret classification for the report."""

    def __init__(self, classification: str) -> None:
        """Store only a fixed classification, never an upstream error message."""
        super().__init__(classification)
        self.classification = classification


class Digest(Protocol):
    """Minimal hashlib interface consumed by the row encoder."""

    def update(self, data: bytes) -> None:
        """Add bytes to the running digest."""
        ...


@dataclass(frozen=True)
class DatabaseEndpoint:
    """Validated non-secret endpoint identity."""

    host: str
    port: int
    schema: str


@dataclass(frozen=True)
class TargetColumn:
    """One inspected target column, excluding any row values."""

    name: str
    column_type: str
    nullable: bool
    default: object | None
    extra: str


@dataclass(frozen=True)
class TargetShape:
    """Read-only target table shape and migration metadata."""

    columns: tuple[TargetColumn, ...]
    primary_key: tuple[str, ...]
    partitions: tuple[str, ...]
    engine: str


def _parse_url(value: str, *, source: bool) -> tuple[URL, DatabaseEndpoint]:
    """Parse and constrain a MySQL URL without exposing credentials."""
    label = "source" if source else "target"
    try:
        url = make_url(value)
    except Exception as exc:
        raise TransferError(f"{label}_url_invalid") from exc
    if set(url.query).difference({"charset"}):
        raise TransferError(f"{label}_url_options_not_allowed")
    charset = url.query.get("charset")
    if charset is not None and charset not in {"utf8", "utf8mb4"}:
        raise TransferError(f"{label}_charset_not_allowed")
    if url.drivername not in {"mysql", "mysql+pymysql"}:
        raise TransferError(f"{label}_driver_not_mysql")
    host = (url.host or "").casefold()
    if host not in {"localhost", "127.0.0.1"}:
        raise TransferError(f"{label}_host_not_allowed")
    expected_port = SOURCE_PORT if source else TARGET_PORT
    if url.port != expected_port:
        raise TransferError(f"{label}_port_not_allowed")
    schema = url.database or ""
    if source:
        if schema != SOURCE_SCHEMA:
            raise TransferError("source_schema_not_allowed")
    else:
        suffix = (
            schema[len(TARGET_SCHEMA_PREFIX) :] if schema.startswith(TARGET_SCHEMA_PREFIX) else ""
        )
        if not suffix or not re.fullmatch(r"[A-Za-z0-9_]+", suffix) or len(schema) > 64:
            raise TransferError("target_schema_not_isolated")
    return url, DatabaseEndpoint(host=host, port=expected_port, schema=schema)


def _validate_database_urls(
    source_value: str, target_value: str
) -> tuple[URL, URL, DatabaseEndpoint, DatabaseEndpoint]:
    """Require the exact loopback source and isolated target endpoints."""
    source_url, source = _parse_url(source_value, source=True)
    target_url, target = _parse_url(target_value, source=False)
    if (source.host, source.port, source.schema) == (target.host, target.port, target.schema):
        raise TransferError("source_target_endpoint_collision")
    return source_url, target_url, source, target


def _is_source_read_statement(
    connection: object,
    cursor: object,
    statement: str,
    parameters: object,
    context: object,
    executemany: bool,
) -> None:
    """Block non-SELECT SQL issued through the source SQLAlchemy engine."""
    del connection, cursor, parameters, context, executemany
    match = re.match(r"\s*([A-Za-z]+)", statement)
    if match is None or match.group(1).upper() != "SELECT":
        raise TransferError("source_sql_guard_blocked_statement")


def _driver_flag_is_true(value: object) -> bool:
    """Normalize MySQL boolean values returned by supported drivers."""
    if value is True or value == 1:
        return True
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    return isinstance(value, str) and value.casefold() in {"1", "on", "true"}


def _configure_source_session(connection: Connection) -> None:
    """Configure and verify repeatable-read/read-only mode before the snapshot."""
    raw = connection.connection.driver_connection
    if raw is None:
        raise TransferError("source_driver_connection_missing")
    cursor = raw.cursor()
    try:
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
        cursor.execute("SELECT @@session.transaction_read_only")
        row = cursor.fetchone()
    finally:
        cursor.close()
    raw.commit()
    if row is None or not row or not _driver_flag_is_true(row[0]):
        raise TransferError("source_read_only_not_confirmed")


@contextmanager
def _readonly_source_snapshot(engine: Engine) -> Iterator[Connection]:
    """Yield one guarded, read-only repeatable-read transaction."""
    event.listen(engine, "before_cursor_execute", _is_source_read_statement)
    try:
        with engine.connect() as connection:
            _configure_source_session(connection)
            with connection.begin():
                state = connection.execute(
                    text("SELECT @@session.transaction_read_only, @@session.transaction_isolation")
                ).one()
                isolation = str(state[1]).replace("_", "-").casefold()
                if not _driver_flag_is_true(state[0]) or isolation != "repeatable-read":
                    raise TransferError("source_snapshot_not_readonly_repeatable_read")
                yield connection
    finally:
        with suppress(Exception):
            event.remove(engine, "before_cursor_execute", _is_source_read_statement)


def _finite_number(value: object, *, optional: bool, field: str) -> float | None:
    """Convert a database numeric value to a finite Python double."""
    if value is None and optional:
        return None
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TransferError(f"invalid_numeric_field_{field}")
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise TransferError(f"nonfinite_numeric_field_{field}") from exc
    if not math.isfinite(converted):
        raise TransferError(f"nonfinite_numeric_field_{field}")
    return converted


def _numeric_text(value: object, *, field: str) -> float | None:
    """Strictly parse nullable decimal text, rejecting blanks and coercions."""
    if value is None:
        return None
    if not isinstance(value, str) or _NUMERIC_TEXT.fullmatch(value) is None:
        raise TransferError(f"invalid_numeric_text_{field}")
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise TransferError(f"invalid_numeric_text_{field}") from exc
    if not parsed.is_finite():
        raise TransferError(f"nonfinite_numeric_text_{field}")
    try:
        converted = float(parsed)
    except (OverflowError, ValueError) as exc:
        raise TransferError(f"nonfinite_numeric_text_{field}") from exc
    if not math.isfinite(converted):
        raise TransferError(f"nonfinite_numeric_text_{field}")
    return converted


def _source_date(value: object) -> date:
    """Parse only a full ISO date from the legacy text column."""
    if not isinstance(value, str) or _ISO_DATE.fullmatch(value) is None:
        raise TransferError("invalid_source_date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise TransferError("invalid_source_date") from exc
    if parsed.isoformat() != value:
        raise TransferError("invalid_source_date")
    return parsed


def _target_date(value: object) -> date:
    """Require the target DATE type without silently dropping time data."""
    if type(value) is not date:
        raise TransferError("invalid_target_date_type")
    return value


def _stock_code(value: object) -> str:
    """Require an exact six-digit A-share code."""
    if not isinstance(value, str) or _STOCK_CODE.fullmatch(value) is None:
        raise TransferError("invalid_stock_code")
    return value


def _source_volume(value: object) -> float:
    """Cast a BIGINT to DOUBLE only when the integer is exactly representable."""
    if type(value) is not int or value < 0 or abs(value) > MAX_EXACT_DOUBLE_INTEGER:
        raise TransferError("volume_not_lossless_double")
    converted = float(value)
    if int(converted) != value:
        raise TransferError("volume_not_lossless_double")
    return converted


def _normalize_source_row(values: Sequence[object]) -> tuple[object, ...]:
    """Convert one raw legacy row to the ODS's fixed twelve-column shape."""
    if len(values) != len(SOURCE_COLUMNS):
        raise TransferError("source_row_width_mismatch")
    row = dict(zip(SOURCE_COLUMNS, values, strict=True))
    normalized: list[object] = [_source_date(row["日期"]), _stock_code(row["股票代码"])]
    for field in SOURCE_COLUMNS[2:]:
        value = row[field]
        if field == "成交量":
            normalized.append(_source_volume(value))
        elif field == "换手率":
            normalized.append(_numeric_text(value, field=field))
        else:
            normalized.append(
                _finite_number(value, optional=field in OPTIONAL_FLOAT_FIELDS, field=field)
            )
    return tuple(normalized)


def _normalize_target_row(values: Sequence[object]) -> tuple[object, ...]:
    """Normalize target rows to the same stable values used for source hashing."""
    if len(values) != len(SOURCE_COLUMNS):
        raise TransferError("target_row_width_mismatch")
    row = dict(zip(SOURCE_COLUMNS, values, strict=True))
    normalized: list[object] = [_target_date(row["日期"]), _stock_code(row["股票代码"])]
    normalized.extend(
        _finite_number(
            row[field],
            optional=field in OPTIONAL_FLOAT_FIELDS or field == "换手率",
            field=field,
        )
        for field in SOURCE_COLUMNS[2:]
    )
    return tuple(normalized)


def _encoded_value(value: object) -> tuple[bytes, bytes]:
    """Return a stable type tag and payload for one normalized hash value."""
    if value is None:
        return b"N", b""
    if type(value) is date:
        return b"D", value.isoformat().encode("ascii")
    if isinstance(value, str):
        return b"S", value.encode("utf-8")
    if type(value) is float and math.isfinite(value):
        return b"F", value.hex().encode("ascii")
    raise TransferError("unsupported_hash_value_type")


def _update_row_digest(digest: Digest, values: Sequence[object]) -> None:
    """Feed a length-prefixed, typed row into a SHA-256 digest."""
    if len(values) != len(SOURCE_COLUMNS):
        raise TransferError("hash_row_width_mismatch")
    digest.update(b"R")
    for name, value in zip(SOURCE_COLUMNS, values, strict=True):
        tag, payload = _encoded_value(value)
        for component in (name.encode("utf-8"), tag, payload):
            digest.update(struct.pack(">I", len(component)))
            digest.update(component)


def _digest_rows(rows: Iterable[Sequence[object]]) -> tuple[int, str]:
    """Compute a streaming digest over already-normalized rows."""
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        _update_row_digest(digest, row)
        count += 1
    return count, digest.hexdigest()


def _source_schema(connection: Connection) -> dict[str, tuple[str, bool]]:
    """Inspect the mapped source columns, without reading source row values."""
    result = connection.execute(
        text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
        ),
        {"schema": SOURCE_SCHEMA, "table": SOURCE_TABLE},
    )
    columns = {
        str(name): (str(column_type).casefold(), str(nullable).casefold() == "yes")
        for name, column_type, nullable in result.fetchall()
    }
    for name, expected_type in SOURCE_COLUMN_TYPES.items():
        actual = columns.get(name)
        if actual is None or actual[0] != expected_type:
            raise TransferError("source_column_schema_mismatch")
    return {name: columns[name] for name in SOURCE_COLUMNS}


def _target_shape(connection: Connection) -> TargetShape:
    """Inspect the fixed target table, key, engine, and partition structure."""
    engine_name = connection.execute(
        text(
            "SELECT ENGINE FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
            "AND TABLE_TYPE = 'BASE TABLE'"
        ),
        {"table": TARGET_TABLE},
    ).scalar_one_or_none()
    if engine_name is None:
        raise TransferError("target_table_missing")
    column_rows = connection.execute(
        text(
            "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, EXTRA "
            "FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table"
        ),
        {"table": TARGET_TABLE},
    ).fetchall()
    columns = tuple(
        sorted(
            (
                TargetColumn(
                    name=str(name),
                    column_type=str(column_type).casefold(),
                    nullable=str(nullable).casefold() == "yes",
                    default=default,
                    extra=str(extra or "").casefold(),
                )
                for name, column_type, nullable, default, extra in column_rows
            ),
            key=lambda item: item.name,
        )
    )
    key_rows = connection.execute(
        text(
            "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
            "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
        ),
        {"table": TARGET_TABLE},
    ).fetchall()
    partitions = connection.execute(
        text(
            "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
            "AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_ORDINAL_POSITION"
        ),
        {"table": TARGET_TABLE},
    ).fetchall()
    return TargetShape(
        columns=columns,
        primary_key=tuple(str(row[0]) for row in key_rows),
        partitions=tuple(str(row[0]) for row in partitions),
        engine=str(engine_name).casefold(),
    )


def _require_target_shape(shape: TargetShape) -> None:
    """Fail closed unless the existing table matches the reviewed ODS DDL."""
    actual = {
        column.name: (column.column_type, column.nullable, column.default, column.extra)
        for column in shape.columns
    }
    expected = {
        name: (column_type, nullable, None, "")
        for name, (column_type, nullable) in TARGET_COLUMN_TYPES.items()
    }
    if actual != expected:
        raise TransferError("target_column_schema_mismatch")
    if shape.primary_key != TARGET_KEY:
        raise TransferError("target_primary_key_mismatch")
    if shape.engine != "innodb":
        raise TransferError("target_engine_mismatch")
    if shape.partitions != EXPECTED_PARTITIONS:
        raise TransferError("target_partition_schema_mismatch")


def _expected_data_heads() -> tuple[str, ...]:
    """Read repository migration heads without opening a database connection."""
    try:
        from alembic.script import ScriptDirectory

        heads = tuple(sorted(ScriptDirectory(str(REPOSITORY_ROOT / "alembic_data")).get_heads()))
    except Exception as exc:
        raise TransferError("repository_migration_head_unavailable") from exc
    if not heads:
        raise TransferError("repository_migration_head_unavailable")
    return heads


def _target_migration_heads(connection: Connection) -> tuple[str, ...]:
    """Require the pre-migrated target schema to be at the repository head."""
    table_exists = connection.execute(
        text(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'alembic_version_data'"
        )
    ).scalar_one_or_none()
    if table_exists is None:
        raise TransferError("target_migration_version_table_missing")
    result = connection.execute(
        text("SELECT version_num FROM alembic_version_data ORDER BY version_num")
    )
    actual = tuple(str(row[0]) for row in result.fetchall())
    if actual != _expected_data_heads():
        raise TransferError("target_migration_head_stale")
    return actual


def _exact_row_count(connection: Connection, table: str) -> int:
    """Return a count from one of the two fixed table identifiers."""
    if table == SOURCE_TABLE:
        statement = text(f"SELECT COUNT(*) FROM `{SOURCE_TABLE}`")  # noqa: S608  # nosec B608  # SOURCE_TABLE is a module literal
    elif table == TARGET_TABLE:
        statement = text(f"SELECT COUNT(*) FROM `{TARGET_TABLE}`")  # noqa: S608  # nosec B608  # TARGET_TABLE is a module literal
    else:
        raise TransferError("row_count_table_not_allowed")
    return int(connection.execute(statement).scalar_one())


def _new_engine(url: URL) -> Engine:
    """Create a quiet synchronous MySQL engine with parameter hiding enabled."""
    return create_engine(url, pool_pre_ping=True, hide_parameters=True)


def _provenance_hashes() -> dict[str, str]:
    """Hash reviewed source inputs without reading or reporting their contents."""
    hashes: dict[str, str] = {}
    for relative_path in PROVENANCE_FILES:
        path = REPOSITORY_ROOT / relative_path
        try:
            hashes[relative_path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise TransferError("provenance_source_file_unavailable") from exc
    return hashes


def _target_table() -> Table:
    """Return the fixed SQLAlchemy Core table definition; never create it."""
    metadata = MetaData()
    columns: list[Column[Any]] = [
        Column("日期", Date(), nullable=False),
        Column("股票代码", String(64), nullable=False),
        Column("开盘", Float(), nullable=False),
        Column("收盘", Float(), nullable=False),
        Column("最高", Float(), nullable=False),
        Column("最低", Float(), nullable=False),
        Column("成交量", Float(), nullable=False),
        Column("成交额", Float(), nullable=False),
        Column("振幅", Float(), nullable=True),
        Column("涨跌幅", Float(), nullable=True),
        Column("涨跌额", Float(), nullable=True),
        Column("换手率", Float(), nullable=True),
        Column("_source", String(32), nullable=False),
        Column("_fetched_at", DateTime(), nullable=False),
        Column("_batch_id", CHAR(36), nullable=False),
    ]
    return Table(TARGET_TABLE, metadata, *columns)


def _write_batch(connection: Connection, table: Table, batch: list[dict[str, object]]) -> int:
    """Insert one bounded batch and reject all MySQL warnings before commit."""
    statement = insert(table).prefix_with("IGNORE", dialect="mysql")
    with connection.begin():
        result = connection.execute(statement, batch)
        inserted = int(result.rowcount)
        warnings = connection.exec_driver_sql("SHOW WARNINGS").fetchall()
        if warnings:
            raise TransferError("target_insert_warnings_present")
        if inserted != len(batch):
            raise TransferError("target_insert_count_mismatch")
    return inserted


def _stream_source(
    connection: Connection,
    target_engine: Engine,
    table: Table,
    *,
    fetched_at: datetime,
    batch_id: str,
    source_count: int,
    progress: Callable[[int, int], None],
) -> tuple[int, int, str]:
    """Stream, validate, hash, and batch-insert all source rows."""
    selected = ", ".join(f"`{column}`" for column in SOURCE_COLUMNS)
    statement = text(
        f"SELECT {selected} FROM `{SOURCE_TABLE}` "  # noqa: S608  # nosec B608  # selected from SOURCE_COLUMNS literal
        "ORDER BY `股票代码`, STR_TO_DATE(`日期`, '%Y-%m-%d')"
    )
    result = connection.execution_options(
        stream_results=True,
        max_row_buffer=PAGE_SIZE,
    ).execute(statement)
    digest = hashlib.sha256()
    rows_read = 0
    rows_inserted = 0
    previous_key: tuple[str, date] | None = None
    batch: list[dict[str, object]] = []
    try:
        with target_engine.connect() as target_connection:
            while True:
                rows = result.fetchmany(PAGE_SIZE)
                if not rows:
                    break
                if len(rows) > PAGE_SIZE:
                    raise TransferError("source_fetch_page_too_large")
                for source_row in rows:
                    normalized = _normalize_source_row(tuple(source_row))
                    symbol = str(normalized[1])
                    trade_date = normalized[0]
                    if type(trade_date) is not date:
                        raise TransferError("source_date_normalization_failed")
                    key = (symbol, trade_date)
                    if previous_key is not None and key <= previous_key:
                        raise TransferError("source_business_key_not_unique_or_sorted")
                    previous_key = key
                    _update_row_digest(digest, normalized)
                    batch_row = dict(zip(SOURCE_COLUMNS, normalized, strict=True))
                    batch_row.update(
                        _source=SOURCE,
                        _fetched_at=fetched_at,
                        _batch_id=batch_id,
                    )
                    batch.append(batch_row)
                    rows_read += 1
                progress(rows_read, rows_inserted)
                if len(batch) == PAGE_SIZE:
                    rows_inserted += _write_batch(target_connection, table, batch)
                    batch.clear()
                    progress(rows_read, rows_inserted)
            if batch:
                rows_inserted += _write_batch(target_connection, table, batch)
                batch.clear()
                progress(rows_read, rows_inserted)
    finally:
        result.close()
    if rows_read != source_count:
        raise TransferError("source_stream_count_mismatch")
    return rows_read, rows_inserted, digest.hexdigest()


def _stream_target_hash(
    connection: Connection,
    *,
    fetched_at: datetime,
    batch_id: str,
    target_count: int,
) -> tuple[int, str]:
    """Read and hash every target row in key order, checking transfer metadata."""
    raw_columns = ", ".join(f"`{column}`" for column in SOURCE_COLUMNS)
    statement = text(
        f"SELECT {raw_columns}, `_source`, `_fetched_at`, `_batch_id` "  # noqa: S608  # nosec B608  # raw_columns from SOURCE_COLUMNS literal
        f"FROM `{TARGET_TABLE}` ORDER BY `股票代码`, `日期`"
    )
    result = connection.execution_options(
        stream_results=True,
        max_row_buffer=PAGE_SIZE,
    ).execute(statement)
    digest = hashlib.sha256()
    count = 0
    previous_key: tuple[str, date] | None = None
    try:
        while True:
            rows = result.fetchmany(PAGE_SIZE)
            if not rows:
                break
            if len(rows) > PAGE_SIZE:
                raise TransferError("target_fetch_page_too_large")
            for row in rows:
                normalized = _normalize_target_row(tuple(row[: len(SOURCE_COLUMNS)]))
                symbol = str(normalized[1])
                trade_date = normalized[0]
                if type(trade_date) is not date:
                    raise TransferError("target_date_normalization_failed")
                key = (symbol, trade_date)
                if previous_key is not None and key <= previous_key:
                    raise TransferError("target_business_key_not_unique_or_sorted")
                previous_key = key
                metadata_source, metadata_fetched_at, metadata_batch_id = row[len(SOURCE_COLUMNS) :]
                if (
                    metadata_source != SOURCE
                    or metadata_fetched_at != fetched_at
                    or metadata_batch_id != batch_id
                ):
                    raise TransferError("target_transfer_metadata_mismatch")
                _update_row_digest(digest, normalized)
                count += 1
    finally:
        result.close()
    if count != target_count:
        raise TransferError("target_stream_count_mismatch")
    return count, digest.hexdigest()


def _perform_apply(state: dict[str, object], source_url: URL, target_url: URL) -> None:
    """Run the only permitted full transfer, updating safe report facts."""
    source_engine: Engine | None = None
    target_engine: Engine | None = None
    try:
        state["connection_attempted"] = True
        source_engine = _new_engine(source_url)
        target_engine = _new_engine(target_url)
        with target_engine.connect() as connection:
            target_heads = _target_migration_heads(connection)
            target_shape = _target_shape(connection)
            _require_target_shape(target_shape)
            target_before = _exact_row_count(connection, TARGET_TABLE)
            connection.commit()
        state.update(
            target_migration_head=list(target_heads),
            target_columns=[
                {
                    "name": column.name,
                    "column_type": column.column_type,
                    "nullable": column.nullable,
                }
                for column in target_shape.columns
            ],
            target_primary_key=list(target_shape.primary_key),
            target_partitions=list(target_shape.partitions),
            target_engine=target_shape.engine,
            target_rows_before=target_before,
        )
        if target_before != 0:
            raise TransferError("target_must_be_empty_before_apply")

        with _readonly_source_snapshot(source_engine) as source_connection:
            source_columns = _source_schema(source_connection)
            source_count = _exact_row_count(source_connection, SOURCE_TABLE)
            state["source_columns"] = {
                name: {"column_type": column_type, "nullable": nullable}
                for name, (column_type, nullable) in source_columns.items()
            }
            state["source_rows"] = source_count
            if source_count != EXPECTED_SOURCE_ROWS:
                raise TransferError("source_row_count_differs_from_reviewed_snapshot")
            fetched_at = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
            batch_id = str(uuid4())
            state["fetched_at_utc"] = fetched_at.replace(tzinfo=timezone.utc).isoformat()
            state["batch_id"] = batch_id
            rows_read, rows_inserted, source_digest = _stream_source(
                source_connection,
                target_engine,
                _target_table(),
                fetched_at=fetched_at,
                batch_id=batch_id,
                source_count=source_count,
                progress=lambda read, inserted: state.update(
                    rows_read=read,
                    rows_inserted=inserted,
                ),
            )
            state.update(
                rows_read=rows_read,
                rows_inserted=rows_inserted,
                source_sha256=source_digest,
            )
        if rows_read != source_count or rows_inserted != source_count:
            raise TransferError("source_target_insert_count_mismatch")

        with target_engine.connect() as target_connection:
            target_after = _exact_row_count(target_connection, TARGET_TABLE)
            state["target_rows_after"] = target_after
            if target_after != source_count:
                raise TransferError("target_final_count_mismatch")
            target_rows, target_digest = _stream_target_hash(
                target_connection,
                fetched_at=fetched_at,
                batch_id=batch_id,
                target_count=target_after,
            )
            target_connection.commit()
        state["target_sha256"] = target_digest
        state["target_rows_hashed"] = target_rows
        if source_digest != target_digest:
            raise TransferError("source_target_full_hash_mismatch")
        with target_engine.connect() as final_connection:
            final_count = _exact_row_count(final_connection, TARGET_TABLE)
            final_connection.commit()
        if final_count != source_count:
            raise TransferError("target_final_count_changed")
        state.update(full_stream_hash_match=True, full_source_transfer_verified=True)
    except TransferError:
        raise
    except Exception as exc:
        raise TransferError("database_operation_failed") from exc
    finally:
        for engine in (source_engine, target_engine):
            if engine is not None:
                with suppress(Exception):
                    engine.dispose()


def _empty_state(*, apply: bool) -> dict[str, object]:
    """Initialize a secret-free, machine-readable report state."""
    return {
        "format_version": 1,
        "status": "RUNNING",
        "mode": "apply" if apply else "dry-run",
        "connection_attempted": False,
        "source_schema": SOURCE_SCHEMA,
        "source_table": SOURCE_TABLE,
        "source_rows_expected": EXPECTED_SOURCE_ROWS,
        "conversion_policy": {
            "columns": list(SOURCE_COLUMNS),
            "business_key": list(TARGET_KEY),
            "date": "strict YYYY-MM-DD text to DATE",
            "volume": "BIGINT to exactly representable DOUBLE; no unit scaling",
            "turnover_rate": "strict finite decimal text to DOUBLE",
            "auxiliary_metrics": "preserve NULL; finite numeric values to DOUBLE",
        },
        "source_rows": None,
        "source_columns": {},
        "source_endpoint": None,
        "target_schema": None,
        "target_table": TARGET_TABLE,
        "target_endpoint": None,
        "target_rows_before": None,
        "target_rows_after": None,
        "target_rows_hashed": 0,
        "target_columns": [],
        "target_primary_key": [],
        "target_partitions": [],
        "target_migration_head": [],
        "target_engine": None,
        "rows_read": 0,
        "rows_inserted": 0,
        "page_size": PAGE_SIZE,
        "source_sha256": None,
        "target_sha256": None,
        "full_stream_hash_match": False,
        "full_source_transfer_verified": False,
        "fetched_at_utc": None,
        "batch_id": None,
        "source_files_sha256": {},
        "elapsed_seconds": 0.0,
        "peak_rss_bytes": 0,
        "rss_unit": "bytes",
        "error_classification": None,
    }


def run_transfer(*, apply: bool) -> dict[str, object]:
    """Run offline by default or execute the guarded explicit full transfer."""
    state = _empty_state(apply=apply)
    started = time.monotonic()
    state["started_at_utc"] = datetime.now(timezone.utc).isoformat()
    try:
        state["source_files_sha256"] = _provenance_hashes()
        if not apply:
            state["status"] = "DRY_RUN"
            return state
        source_value = os.environ.get(SOURCE_URL_ENV)
        target_value = os.environ.get(TARGET_URL_ENV)
        if not source_value or not target_value:
            raise TransferError("required_database_environment_missing")
        source_url, target_url, source_endpoint, target_endpoint = _validate_database_urls(
            source_value, target_value
        )
        state.update(
            source_endpoint={
                "host": source_endpoint.host,
                "port": source_endpoint.port,
                "schema": source_endpoint.schema,
            },
            target_endpoint={
                "host": target_endpoint.host,
                "port": target_endpoint.port,
                "schema": target_endpoint.schema,
            },
            target_schema=target_endpoint.schema,
        )
        _perform_apply(state, source_url, target_url)
        state["status"] = "PASS"
    except TransferError as exc:
        state["status"] = "FAIL"
        state["error_classification"] = exc.classification
    except Exception:
        state["status"] = "FAIL"
        state["error_classification"] = "internal_error"
    finally:
        state["elapsed_seconds"] = max(time.monotonic() - started, 1e-9)
        raw_peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        state["peak_rss_bytes"] = int(raw_peak if sys.platform == "darwin" else raw_peak * 1024)
        state["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return state


def _report_command(output: Path, *, apply: bool) -> list[str]:
    """Record the invocation without database environment values."""
    command = [sys.executable, "scripts/ops/migrate_legacy_stock_daily.py"]
    if apply:
        command.append("--apply")
    command.extend(("--output", str(output)))
    return command


def _write_report(path: Path, report: dict[str, object]) -> None:
    """Create a new JSON report with owner-only permissions; never overwrite."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _parser() -> argparse.ArgumentParser:
    """Build the bounded CLI; database URLs are intentionally not arguments."""
    parser = _SafeArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="perform the reviewed full transfer")
    parser.add_argument("--output", required=True, type=Path, help="new mode-0600 JSON report path")
    return parser


class _SafeArgumentParser(argparse.ArgumentParser):
    """Avoid echoing rejected arguments that may contain sensitive values."""

    def error(self, message: str) -> NoReturn:
        del message
        raise ValueError("invalid command line")


def main(argv: list[str] | None = None) -> int:
    """Run the CLI and write a redacted report."""
    try:
        args = _parser().parse_args(argv)
    except ValueError:
        print("invalid_command_line", file=sys.stderr)
        return 2
    report = run_transfer(apply=args.apply)
    report["command"] = _report_command(args.output, apply=args.apply)
    try:
        _write_report(args.output, report)
    except FileExistsError:
        print("report_output_refused: path already exists", file=sys.stderr)
        return 2
    except OSError:
        print("report_output_failed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "error_classification": report["error_classification"],
                "report": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] in {"DRY_RUN", "PASS"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
