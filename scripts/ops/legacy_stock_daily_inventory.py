"""Compute a read-only inventory of the reviewed legacy stock-daily source.

The caller owns the MySQL connection and transaction. The calculator never opens,
commits, rolls back, or writes through a connection and emits only SELECT queries.
Counts, date bounds, NULL census, and digest describe one streaming SELECT; they
do not prove a stable snapshot, listing-date coverage, or full-history completeness.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import text

from scripts.ops.migrate_legacy_stock_daily import (
    SOURCE_COLUMNS,
    SOURCE_SCHEMA,
    SOURCE_TABLE,
    _normalize_source_row,
    _update_row_digest,
)
from scripts.ops.migrate_legacy_stock_daily import (
    TransferError as _LegacyTransferError,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


__all__ = ["LegacyStockDailyInventory", "LegacyStockDailyInventoryError", "inventory_source"]

BUSINESS_KEY = ("股票代码", "日期")
DIGEST_ALGORITHM = "sha256_typed_length_prefixed_v1"
_FETCH_SIZE = 1_000


@dataclass(frozen=True)
class _ColumnSpec:
    """One reviewed source column in physical ordinal order."""

    name: str
    column_type: str
    nullable: bool


_EXPECTED_COLUMNS = (
    _ColumnSpec("R_ID", "int", False),
    _ColumnSpec("symbol", "varchar(50)", True),
    _ColumnSpec("name", "varchar(100)", True),
    _ColumnSpec("data_date", "date", True),
    _ColumnSpec("created_at", "datetime", True),
    _ColumnSpec("updated_at", "datetime", True),
    _ColumnSpec("日期", "text", True),
    _ColumnSpec("开盘", "double", True),
    _ColumnSpec("收盘", "double", True),
    _ColumnSpec("最高", "double", True),
    _ColumnSpec("最低", "double", True),
    _ColumnSpec("成交量", "bigint", True),
    _ColumnSpec("成交额", "double", True),
    _ColumnSpec("涨跌额", "double", True),
    _ColumnSpec("涨跌幅", "double", True),
    _ColumnSpec("振幅", "double", True),
    _ColumnSpec("换手率", "text", True),
    _ColumnSpec("股票代码", "text", True),
)


class LegacyStockDailyInventoryError(RuntimeError):
    """A safe inventory failure code that never includes SQL or connection data."""

    def __init__(self, classification: str) -> None:
        """Set the fixed classification exposed to callers."""
        super().__init__(classification)
        self.classification = classification


@dataclass(frozen=True)
class LegacyStockDailyInventory:
    """Immutable scan facts and separately timed metadata observations."""

    source_schema: str
    source_table: str
    source_server_uuid: str
    observed_after_scan_source_engine: str
    observed_after_scan_source_primary_key: tuple[str, ...]
    business_key: tuple[str, ...]
    row_count: int
    distinct_business_key_count: int
    first_business_date: date | None
    last_business_date: date | None
    null_counts: tuple[tuple[str, int], ...]
    source_sha256: str
    digest_algorithm: str
    observed_after_scan_schema_sha256: str
    schema_observation_scope: str
    inventory_scope: str
    observed_session_isolation_setting: str
    observed_session_read_only_setting: bool


def _raise_legacy_error(error: _LegacyTransferError) -> None:
    """Translate the legacy row contract's safe error code without its traceback."""
    raise LegacyStockDailyInventoryError(error.classification) from None


def _canonical_uuid(value: object, *, classification: str) -> str:
    """Require a lowercase, canonical UUID string."""
    if not isinstance(value, str):
        raise LegacyStockDailyInventoryError(classification)
    try:
        normalized = str(UUID(value))
    except (ValueError, AttributeError):
        raise LegacyStockDailyInventoryError(classification) from None
    if normalized != value:
        raise LegacyStockDailyInventoryError(classification)
    return normalized


def _mysql_boolean(value: object) -> bool:
    """Parse an explicit MySQL boolean result without coercing unknown values."""
    if value is True or (type(value) is int and value == 1):
        return True
    if value is False or (type(value) is int and value == 0):
        return False
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    if isinstance(value, str):
        normalized = value.casefold()
        if normalized in {"1", "on", "true"}:
            return True
        if normalized in {"0", "off", "false"}:
            return False
    raise LegacyStockDailyInventoryError("source_read_only_setting_unavailable")


def _session_state(
    connection: Connection,
    expected_server_uuid: str,
) -> tuple[str, str, bool]:
    """Observe source identity and session variables without certifying the transaction."""
    if getattr(getattr(connection, "dialect", None), "name", None) != "mysql":
        raise LegacyStockDailyInventoryError("mysql_connection_required")
    if not callable(getattr(connection, "in_transaction", None)) or not connection.in_transaction():
        raise LegacyStockDailyInventoryError("caller_transaction_required")
    pinned_uuid = _canonical_uuid(
        expected_server_uuid,
        classification="reviewed_server_uuid_pin_invalid",
    )
    try:
        row = connection.execute(
            text(
                "SELECT DATABASE(), @@server_uuid, @@session.transaction_read_only, "
                "@@session.transaction_isolation"
            )
        ).one()
    except Exception:
        raise LegacyStockDailyInventoryError("source_identity_query_failed") from None
    if len(row) != 4 or row[0] != SOURCE_SCHEMA:
        raise LegacyStockDailyInventoryError("source_schema_identity_mismatch")
    observed_uuid = _canonical_uuid(
        row[1],
        classification="source_server_uuid_unavailable",
    )
    if observed_uuid != pinned_uuid:
        raise LegacyStockDailyInventoryError("source_server_uuid_pin_mismatch")
    if not isinstance(row[3], str) or not row[3].strip():
        raise LegacyStockDailyInventoryError("source_isolation_setting_unavailable")
    isolation = str(row[3]).strip().casefold().replace("_", "-").replace(" ", "-")
    read_only_setting = _mysql_boolean(row[2])
    return observed_uuid, isolation, read_only_setting


def _source_shape(connection: Connection) -> tuple[str, tuple[str, ...], str]:
    """Require the exact reviewed InnoDB table, columns, and legacy primary key."""
    try:
        table_row = connection.execute(
            text(
                "SELECT TABLE_TYPE, ENGINE FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table"
            ),
            {"schema": SOURCE_SCHEMA, "table": SOURCE_TABLE},
        ).one()
        column_rows = connection.execute(
            text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, ORDINAL_POSITION "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table "
                "ORDER BY ORDINAL_POSITION"
            ),
            {"schema": SOURCE_SCHEMA, "table": SOURCE_TABLE},
        ).fetchall()
        key_rows = connection.execute(
            text(
                "SELECT COLUMN_NAME FROM information_schema.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table "
                "AND CONSTRAINT_NAME = 'PRIMARY' ORDER BY ORDINAL_POSITION"
            ),
            {"schema": SOURCE_SCHEMA, "table": SOURCE_TABLE},
        ).fetchall()
    except Exception:
        raise LegacyStockDailyInventoryError("source_schema_inspection_failed") from None

    if len(table_row) != 2 or str(table_row[0]).casefold() != "base table":
        raise LegacyStockDailyInventoryError("source_table_identity_mismatch")
    engine = str(table_row[1]).casefold()
    if engine != "innodb":
        raise LegacyStockDailyInventoryError("source_table_engine_mismatch")

    observed_columns: list[_ColumnSpec] = []
    try:
        for name, column_type, nullable, ordinal in column_rows:
            if type(ordinal) is not int or ordinal != len(observed_columns) + 1:
                raise LegacyStockDailyInventoryError("source_column_order_mismatch")
            if str(nullable).casefold() not in {"yes", "no"}:
                raise LegacyStockDailyInventoryError("source_column_nullability_unknown")
            observed_columns.append(
                _ColumnSpec(
                    name=str(name),
                    column_type=str(column_type).casefold(),
                    nullable=str(nullable).casefold() == "yes",
                )
            )
    except LegacyStockDailyInventoryError:
        raise
    except Exception:
        raise LegacyStockDailyInventoryError("source_column_metadata_invalid") from None
    if tuple(observed_columns) != _EXPECTED_COLUMNS:
        raise LegacyStockDailyInventoryError("source_column_schema_mismatch")

    primary_key = tuple(str(row[0]) for row in key_rows)
    if primary_key != ("R_ID",):
        raise LegacyStockDailyInventoryError("source_primary_key_mismatch")
    schema_facts = {
        "database": SOURCE_SCHEMA,
        "table": SOURCE_TABLE,
        "engine": engine,
        "primary_key": primary_key,
        "columns": [
            {
                "name": column.name,
                "column_type": column.column_type,
                "nullable": column.nullable,
            }
            for column in observed_columns
        ],
    }
    canonical_schema = json.dumps(
        schema_facts,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return engine, primary_key, hashlib.sha256(canonical_schema).hexdigest()


def inventory_source(
    connection: Connection,
    *,
    expected_server_uuid: str,
) -> LegacyStockDailyInventory:
    """Inventory the reviewed source through a caller-owned connection.

    Args:
        connection: Caller-owned SQLAlchemy MySQL connection with an active
            transaction according to SQLAlchemy's client-side state. This
            function leaves it open, does not change its settings, and emits
            only SELECT statements. Keep the caller transaction short after
            this function returns.
        expected_server_uuid: Canonical UUID supplied by the integration layer
            from an operator-reviewed source pin. The function compares the
            observed UUID with this value but cannot verify the pin's approval or
            provenance. The integration layer must bind that provenance into its
            own manifest.

    Returns:
        Counts, business-date bounds, per-field NULL counts, a typed SHA-256
        digest over the twelve mapped source fields in business-key order, and
        the session settings observed before and after the scan, plus a
        separately timed post-scan schema observation.

    Raises:
        LegacyStockDailyInventoryError: If identity, session observations, source
            shape, row values, business-key uniqueness, or connection state is
            unverified.

    The row scan is one streaming SELECT. Table shape is inspected afterward as
    a separate metadata observation. Although MySQL documents transaction-
    duration metadata locks, this module does not verify server-side transaction
    state or lock ownership, and has not established that the post-scan schema
    observation is bound to the row scan in a real MySQL deployment. The
    returned schema digest is therefore explicitly an unbound post-scan
    observation, not proof of the schema used by the SELECT. SQLAlchemy's
    ``Connection.in_transaction()`` is only a client-side state check. Session
    variables are observations, not proof of the active transaction's actual
    mode or server grants, and the observed UUID match does not prove pin
    approval. Row facts describe only one streaming SELECT and are not a stable-
    snapshot proof; they make no claim about listing-date coverage, provider
    retention, missing interior dates, or full historical completeness. The
    caller owns transaction lifetime and cleanup. A successful result does not
    certify that SQLAlchemy's reported transaction state maps to a live explicit
    transaction on the server.
    """
    initial_state = _session_state(connection, expected_server_uuid)
    server_uuid, isolation, read_only_setting = initial_state
    quoted_fields = ", ".join(f"`{column}`" for column in SOURCE_COLUMNS)
    # Row facts come from one SELECT; metadata is observed separately afterward.
    statement = text(
        f"SELECT {quoted_fields} FROM `{SOURCE_TABLE}` "  # noqa: S608  # fixed module constants
        "ORDER BY BINARY `股票代码`, BINARY `日期`"
    )
    try:
        result = connection.execution_options(
            stream_results=True,
            max_row_buffer=_FETCH_SIZE,
        ).execute(statement)
    except Exception:
        raise LegacyStockDailyInventoryError("source_scan_query_failed") from None

    digest = hashlib.sha256()
    null_counts = dict.fromkeys(SOURCE_COLUMNS, 0)
    row_count = 0
    first_date: date | None = None
    last_date: date | None = None
    previous_key: tuple[str, date] | None = None
    try:
        while True:
            batch = result.fetchmany(_FETCH_SIZE)
            if not batch:
                break
            if len(batch) > _FETCH_SIZE:
                raise LegacyStockDailyInventoryError("source_fetch_page_too_large")
            for raw_row in batch:
                raw_values = tuple(raw_row)
                try:
                    normalized = _normalize_source_row(raw_values)
                except _LegacyTransferError as exc:
                    _raise_legacy_error(exc)
                symbol = str(normalized[1])
                trade_date = normalized[0]
                if type(trade_date) is not date:
                    raise LegacyStockDailyInventoryError("source_date_normalization_failed")
                key = (symbol, trade_date)
                if previous_key is not None and key <= previous_key:
                    classification = (
                        "source_business_key_duplicate"
                        if key == previous_key
                        else "source_business_key_order_mismatch"
                    )
                    raise LegacyStockDailyInventoryError(classification)
                previous_key = key
                first_date = trade_date if first_date is None else min(first_date, trade_date)
                last_date = trade_date if last_date is None else max(last_date, trade_date)
                for field, value in zip(SOURCE_COLUMNS, normalized, strict=True):
                    if value is None:
                        null_counts[field] += 1
                _update_row_digest(digest, normalized)
                row_count += 1
    except LegacyStockDailyInventoryError:
        raise
    except _LegacyTransferError as exc:
        _raise_legacy_error(exc)
    except Exception:
        raise LegacyStockDailyInventoryError("source_scan_failed") from None
    finally:
        try:
            result.close()
        except Exception:
            raise LegacyStockDailyInventoryError("source_scan_close_failed") from None

    if not connection.in_transaction():
        raise LegacyStockDailyInventoryError(
            "caller_client_transaction_state_lost_during_inventory"
        )
    engine, primary_key, schema_sha256 = _source_shape(connection)
    final_state = _session_state(connection, expected_server_uuid)
    if final_state != initial_state:
        raise LegacyStockDailyInventoryError(
            "source_observed_connection_state_changed_during_inventory"
        )
    return LegacyStockDailyInventory(
        source_schema=SOURCE_SCHEMA,
        source_table=SOURCE_TABLE,
        source_server_uuid=server_uuid,
        observed_after_scan_source_engine=engine,
        observed_after_scan_source_primary_key=primary_key,
        business_key=BUSINESS_KEY,
        row_count=row_count,
        distinct_business_key_count=row_count,
        first_business_date=first_date,
        last_business_date=last_date,
        null_counts=tuple((field, null_counts[field]) for field in SOURCE_COLUMNS),
        source_sha256=digest.hexdigest(),
        digest_algorithm=DIGEST_ALGORITHM,
        observed_after_scan_schema_sha256=schema_sha256,
        schema_observation_scope="post_scan_unbound_metadata_observation",
        inventory_scope="single_streaming_select_observation",
        observed_session_isolation_setting=isolation,
        observed_session_read_only_setting=read_only_setting,
    )
