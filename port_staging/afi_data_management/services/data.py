"""
Storage and query service for akshare data warehouse tables.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.akshare_data_database import _get_akshare_data_engine
from opendata.afi_port.models.akshare_mgmt import DataScript, DataTable
from opendata.afi_port.services.table_pagination import (
    TotalCountKey,
    WarehouseTablePage,
    get_total_count_cache,
    order_by_clause,
    reflect_table_ordering,
    resolve_total_count,
)

_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MYSQL_COMPACT_TABLE_COLUMN_THRESHOLD = 250
_COMPACT_ROW_COLUMN = "_akshare_row"
_COMPACT_PAYLOAD_COLUMN = "_akshare_payload"
_ADJUSTMENT_COLUMN = "adjustment"
_VALID_ADJUSTMENT_VALUE = re.compile(r"^[A-Za-z0-9_]{1,32}$")
# Bounds for the distinct-basis sample. ``SELECT DISTINCT adjustment FROM <table>
# LIMIT n`` is NOT a bounded read on MySQL: the engine scans the whole table
# whenever the true distinct count is below ``n`` (18M+ rows on STOCK_ZH_A_HIST).
# The inner subquery in ``_sample_observed_bases`` therefore caps engine work at
# ``_BASIS_SAMPLE_ROWS`` storage-order rows, and the outer LIMIT caps the
# reported cardinality at ``_BASIS_VALUE_LIMIT`` values.
_BASIS_SAMPLE_ROWS = 200_000
_BASIS_VALUE_LIMIT = 50


class AdjustmentBasisError(ValueError):
    """The requested adjustment basis is malformed or absent from the table.

    Raised instead of silently ignoring the filter, so the API layer can map it
    to a structured 400 rather than an unfiltered (or empty) page.
    """


class AkshareDataService:
    """Service for persisting and previewing akshare data tables."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    @staticmethod
    def _normalize_identifier(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
        normalized = re.sub(r"_+", "_", normalized).strip("_").lower()
        if not normalized:
            normalized = "data"
        if normalized[0].isdigit():
            normalized = f"t_{normalized}"
        return normalized

    @staticmethod
    def _validate_table_name(table_name: str) -> str:
        if not _VALID_IDENTIFIER.match(table_name):
            raise ValueError(f"Invalid table name: {table_name}")
        return table_name

    @classmethod
    def normalize_existing_table_name(cls, table_name: str) -> str:
        """Normalize a pre-existing warehouse table name without lowercasing it.

        Legacy data_fetch scripts create physical MySQL tables with uppercase
        names such as ``STOCK_ZH_A_DAILY``. MySQL table names are case-sensitive
        on Linux, so lowercasing those names makes row-count checks silently
        report zero even when the table has data.
        """
        raw_table_name = str(table_name or "").strip()
        if _VALID_IDENTIFIER.match(raw_table_name):
            return raw_table_name
        return cls._validate_table_name(cls._normalize_identifier(raw_table_name))

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return f"`{AkshareDataService._validate_table_name(identifier)}`"

    @staticmethod
    def _quote_mysql_identifier(identifier: str) -> str:
        return f"`{identifier.replace('`', '``')}`"

    @classmethod
    def _mysql_column_type(cls, series: pd.Series) -> str:
        if pd.api.types.is_bool_dtype(series):
            return "BOOLEAN"
        if pd.api.types.is_integer_dtype(series):
            return "BIGINT"
        if pd.api.types.is_float_dtype(series):
            return "DOUBLE"
        if pd.api.types.is_datetime64_any_dtype(series):
            return "DATETIME"
        return "LONGTEXT"

    @classmethod
    def _create_mysql_table_for_dataframe(
        cls,
        sync_conn: Any,
        table_name: str,
        dataframe: pd.DataFrame,
    ) -> None:
        """Create a MySQL table with a row format that can handle wide AkShare frames."""
        quoted_name = cls._quote_identifier(table_name)
        column_definitions = [
            f"{cls._quote_mysql_identifier(str(column))} {cls._mysql_column_type(dataframe[column])} NULL"
            for column in dataframe.columns
        ]
        sync_conn.exec_driver_sql(
            f"CREATE TABLE {quoted_name} ("
            + ", ".join(column_definitions)
            + ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC"
        )

    @classmethod
    def _create_mysql_compact_table(cls, sync_conn: Any, table_name: str) -> None:
        quoted_name = cls._quote_identifier(table_name)
        sync_conn.exec_driver_sql(
            f"CREATE TABLE {quoted_name} ("
            f"{cls._quote_mysql_identifier(_COMPACT_ROW_COLUMN)} BIGINT NULL, "
            f"{cls._quote_mysql_identifier(_COMPACT_PAYLOAD_COLUMN)} LONGTEXT NULL"
            ") ENGINE=InnoDB ROW_FORMAT=DYNAMIC"
        )

    @staticmethod
    def _compact_dataframe_for_mysql(dataframe: pd.DataFrame) -> pd.DataFrame:
        payloads = [
            json.dumps(row, ensure_ascii=False, default=str)
            for row in dataframe.to_dict(orient="records")
        ]
        return pd.DataFrame(
            {
                _COMPACT_ROW_COLUMN: range(1, len(payloads) + 1),
                _COMPACT_PAYLOAD_COLUMN: payloads,
            }
        )

    def build_table_name(self, script: DataScript, parameters: dict[str, Any]) -> str:
        """Build the physical warehouse table name for one script execution."""
        base_name = str(script.target_table or script.script_id)
        base_name = self._normalize_identifier(base_name)
        symbol = parameters.get("symbol") or parameters.get("code") or parameters.get("ticker")
        if symbol:
            base_name = f"{base_name}_{self._normalize_identifier(str(symbol))}"
        return self._validate_table_name(base_name)

    def normalize_dataframe(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Normalize column names and cell values before persistence."""
        normalized = dataframe.copy()
        used_columns: set[str] = set()
        suffix_counters: dict[str, int] = {}
        normalized_columns: list[str] = []
        for index, column in enumerate(normalized.columns, start=1):
            raw_column = str(column)
            base_column_name = self._normalize_identifier(raw_column)
            if (
                base_column_name == "data"
                and raw_column.strip()
                and not re.search(r"[A-Za-z0-9_]", raw_column)
            ):
                base_column_name = f"col_{index}"

            column_name = base_column_name
            while column_name in used_columns:
                suffix_counters[base_column_name] = suffix_counters.get(base_column_name, 1) + 1
                column_name = f"{base_column_name}_{suffix_counters[base_column_name]}"
            used_columns.add(column_name)
            normalized_columns.append(column_name)

        normalized.columns = normalized_columns
        normalized = normalized.where(pd.notnull(normalized), None)
        normalized = normalized.map(self._normalize_cell_value)
        return normalized

    @staticmethod
    def _normalize_cell_value(value: Any) -> Any:
        if isinstance(value, (dict, list, tuple, set)):
            return json.dumps(value, ensure_ascii=False, default=str)
        return value

    async def get_row_count(self, table_name: str) -> int:
        """Return the current row count of a warehouse table."""
        if _get_akshare_data_engine() is None:
            raise RuntimeError("AKSHARE_DATA_DATABASE_URL is not configured")

        quoted_name = self._quote_identifier(table_name)
        async with _get_akshare_data_engine().connect() as conn:
            try:
                result = await conn.execute(text(f"SELECT COUNT(*) FROM {quoted_name}"))
            except Exception:
                return 0
        return int(result.scalar() or 0)

    @staticmethod
    def _is_historical_script(script: DataScript) -> bool:
        name = f"{script.script_id} {script.target_table or ''}".lower()
        return any(
            token in name
            for token in (
                "hist",
                "history",
                "kline",
                "minute",
                "_daily",
                "daily_",
            )
        )

    def _infer_date_range(self, dataframe: pd.DataFrame) -> tuple[datetime | None, datetime | None]:
        date_like_columns = [
            column
            for column in dataframe.columns
            if column.lower() in {"date", "trade_date", "datetime", "timestamp"}
        ]
        for column in date_like_columns:
            parsed = pd.to_datetime(dataframe[column], errors="coerce")
            parsed = parsed.dropna()
            if not parsed.empty:
                return parsed.min().to_pydatetime(), parsed.max().to_pydatetime()
        return None, None

    async def _upsert_table_metadata(
        self,
        script: DataScript,
        table_name: str,
        row_count: int,
        parameters: dict[str, Any],
        status: str,
        columns: list[str],
        data_start: datetime | None = None,
        data_end: datetime | None = None,
    ) -> DataTable:
        metadata_result = await self.db.execute(
            text("SELECT id FROM ak_data_tables WHERE table_name = :table_name"),
            {"table_name": table_name},
        )
        existing_id = metadata_result.scalar_one_or_none()
        if existing_id is None:
            record = DataTable(
                table_name=table_name,
                table_comment=script.script_name,
                category=script.category,
                script_id=script.script_id,
                row_count=row_count,
                last_update_time=datetime.utcnow(),
                last_update_status=status,
                data_start_date=data_start.date() if data_start else None,
                data_end_date=data_end.date() if data_end else None,
                symbol_raw=str(parameters.get("symbol")) if parameters.get("symbol") else None,
                symbol_normalized=self._normalize_identifier(str(parameters.get("symbol")))
                if parameters.get("symbol")
                else None,
                market=str(parameters.get("market")) if parameters.get("market") else None,
                asset_type=str(parameters.get("asset_type"))
                if parameters.get("asset_type")
                else None,
                metadata_json={"columns": columns},
            )
            self.db.add(record)
            await self.db.commit()
            await self.db.refresh(record)
            return record

        table = await self.db.get(DataTable, existing_id)
        if table is None:
            raise RuntimeError(f"DataTable metadata row vanished for table_name={table_name}")
        # ORM model uses legacy ``Column`` (not ``Mapped[]``), so mypy types the
        # instance attributes as ``Column[T]``; assigning Python values through an
        # ``Any`` alias keeps the writes type-clean without a project-wide migration.
        table_row: Any = table
        table_row.table_comment = script.script_name
        table_row.category = script.category
        table_row.script_id = script.script_id
        table_row.row_count = row_count
        table_row.last_update_time = datetime.utcnow()
        table_row.last_update_status = status
        table_row.data_start_date = data_start.date() if data_start else None
        table_row.data_end_date = data_end.date() if data_end else None
        table_row.symbol_raw = str(parameters.get("symbol")) if parameters.get("symbol") else None
        table_row.symbol_normalized = (
            self._normalize_identifier(str(parameters.get("symbol")))
            if parameters.get("symbol")
            else None
        )
        table_row.market = str(parameters.get("market")) if parameters.get("market") else None
        table_row.asset_type = (
            str(parameters.get("asset_type")) if parameters.get("asset_type") else None
        )
        table_row.metadata_json = {"columns": columns}
        await self.db.commit()
        await self.db.refresh(table)
        return table

    async def sync_existing_table_metadata(
        self,
        script: DataScript,
        table_name: str,
        parameters: dict[str, Any],
        status: str = "success",
    ) -> DataTable:
        normalized_table_name = self.normalize_existing_table_name(table_name)
        row_count = await self.get_row_count(normalized_table_name)
        try:
            schema = await self.get_table_schema(normalized_table_name)
            columns = [column["name"] for column in schema]
        except Exception:
            columns = []
        return await self._upsert_table_metadata(
            script=script,
            table_name=normalized_table_name,
            row_count=row_count,
            parameters=parameters,
            status=status,
            columns=columns,
            data_start=None,
            data_end=None,
        )

    async def persist_dataframe(
        self,
        script: DataScript,
        dataframe: pd.DataFrame,
        parameters: dict[str, Any],
        status: str = "success",
    ) -> DataTable:
        """Persist a dataframe into the warehouse and update metadata."""
        if _get_akshare_data_engine() is None:
            raise RuntimeError("AKSHARE_DATA_DATABASE_URL is not configured")

        normalized_df = self.normalize_dataframe(dataframe)
        table_name = self.build_table_name(script, parameters)
        row_count = len(normalized_df.index)
        data_start, data_end = self._infer_date_range(normalized_df)

        if normalized_df.empty:
            try:
                existing_row_count = await self.get_row_count(table_name)
            except Exception:
                existing_row_count = 0
            try:
                existing_schema = await self.get_table_schema(table_name)
                existing_columns = [column["name"] for column in existing_schema]
            except Exception:
                existing_columns = []
            return await self._upsert_table_metadata(
                script=script,
                table_name=table_name,
                row_count=existing_row_count,
                parameters=parameters,
                status=status,
                columns=existing_columns or list(normalized_df.columns),
                data_start=None,
                data_end=None,
            )

        if len(normalized_df.columns) > 0:
            quoted_name = self._quote_identifier(table_name)
            existing_row_count = await self.get_row_count(table_name)
            if (
                existing_row_count > 0
                and row_count < existing_row_count
                and self._is_historical_script(script)
            ):
                try:
                    existing_schema = await self.get_table_schema(table_name)
                    existing_columns = [column["name"] for column in existing_schema]
                except Exception:
                    existing_columns = []
                return await self._upsert_table_metadata(
                    script=script,
                    table_name=table_name,
                    row_count=existing_row_count,
                    parameters=parameters,
                    status=status,
                    columns=existing_columns or list(normalized_df.columns),
                    data_start=None,
                    data_end=None,
                )

            def replace_table(sync_conn: Any) -> None:
                sync_conn.exec_driver_sql(f"DROP TABLE IF EXISTS {quoted_name}")
                if sync_conn.dialect.name == "mysql":
                    if len(normalized_df.columns) > _MYSQL_COMPACT_TABLE_COLUMN_THRESHOLD:
                        self._create_mysql_compact_table(sync_conn, table_name)
                        self._compact_dataframe_for_mysql(normalized_df).to_sql(
                            table_name,
                            con=sync_conn,
                            if_exists="append",
                            index=False,
                        )
                    else:
                        self._create_mysql_table_for_dataframe(
                            sync_conn,
                            table_name,
                            normalized_df,
                        )
                        normalized_df.to_sql(
                            table_name,
                            con=sync_conn,
                            if_exists="append",
                            index=False,
                        )
                else:
                    normalized_df.to_sql(
                        table_name,
                        con=sync_conn,
                        if_exists="fail",
                        index=False,
                    )

            async with _get_akshare_data_engine().begin() as conn:
                await conn.run_sync(replace_table)
            # The rows were just replaced, so any cached total for this table is
            # now wrong; drop it so the next page re-measures instead of serving a
            # count that no longer describes the table.
            get_total_count_cache().evict_table(table_name)

        return await self._upsert_table_metadata(
            script=script,
            table_name=table_name,
            row_count=row_count,
            parameters=parameters,
            status=status,
            columns=list(normalized_df.columns),
            data_start=data_start,
            data_end=data_end,
        )

    async def list_tables(
        self,
        search: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[DataTable], int]:
        """List akshare data tables from metadata storage."""
        stmt = select(DataTable)
        count_stmt = select(func.count(DataTable.id))
        if search:
            pattern = f"%{search}%"
            filters = or_(
                DataTable.table_name.ilike(pattern),
                DataTable.table_comment.ilike(pattern),
            )
            stmt = stmt.where(filters)
            count_stmt = count_stmt.where(filters)

        total = int((await self.db.execute(count_stmt)).scalar() or 0)
        stmt = (
            # ``updated_at`` alone ties for rows written in the same tick, and a
            # tied ORDER BY is not a total order: MySQL could then serve one metadata
            # row on two pages and skip another.  ``id`` breaks every tie.
            stmt.order_by(DataTable.updated_at.desc(), DataTable.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total

    async def get_table(self, table_id: int) -> DataTable | None:
        """Get one metadata table by ID."""
        return await self.db.get(DataTable, table_id)

    async def get_table_schema(self, table_name: str) -> list[dict[str, Any]]:
        """Inspect a warehouse table schema."""
        if _get_akshare_data_engine() is None:
            raise RuntimeError("AKSHARE_DATA_DATABASE_URL is not configured")

        self._validate_table_name(table_name)

        async with _get_akshare_data_engine().connect() as conn:
            columns = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(table_name)
            )
        return [
            {
                "name": column["name"],
                "type": str(column["type"]),
                "nullable": bool(column.get("nullable", True)),
                "default": column.get("default"),
            }
            for column in columns
        ]

    async def _sample_observed_bases(self, conn: Any, quoted_name: str) -> list[str]:
        """Collect distinct adjustment values from a bounded storage-order sample.

        The inner ``LIMIT :sample_rows`` subquery is what bounds engine work: a
        plain ``SELECT DISTINCT adjustment FROM <table> LIMIT n`` would scan an
        18M-row table in full whenever fewer than ``n`` bases exist. Bases that
        occur only beyond the sampled rows are under-reported here; the exact
        existence probe in :meth:`get_table_rows` is what guards filtering.
        """
        result = await conn.execute(
            text(
                f"SELECT DISTINCT adjustment FROM ("
                f"SELECT {_ADJUSTMENT_COLUMN} AS adjustment FROM {quoted_name} "
                f"LIMIT :sample_rows"
                f") AS basis_sample LIMIT :max_values"
            ),
            {"sample_rows": _BASIS_SAMPLE_ROWS, "max_values": _BASIS_VALUE_LIMIT},
        )
        return sorted(str(value) for (value,) in result.all() if value is not None)

    async def get_table_rows(
        self,
        table_name: str,
        page: int = 1,
        page_size: int = 50,
        adjustment: str | None = None,
    ) -> WarehouseTablePage:
        """Preview one page of rows from a warehouse table.

        Ordering is a total order derived from the table's own key constraints (see
        :mod:`app.services.akshare.table_pagination`); a table for which no such
        order is provable raises ``StableOrderingUnavailableError`` instead of
        serving unordered pages.  The exact ``COUNT(*)`` is measured once per
        (database, table, filter) shape and reused inside the cache TTL, so walking
        the pages of a large table no longer pays a full count per request.  The
        response says which of the two happened through ``total_source``.

        ``adjustment`` optionally narrows rows and ``total`` to one basis, and the
        resulting ``adjustment_basis`` block reports the echoed filter, the
        observed distinct bases and whether the table mixes more than one; nothing
        is silently dropped or hidden.  That block is ``None`` unless the physical
        table actually has an ``adjustment`` column, so tables without that column
        keep the exact pre-existing response shape.  A malformed or unknown basis
        raises ``AdjustmentBasisError`` rather than serving an unfiltered page.
        """
        engine = _get_akshare_data_engine()
        if engine is None:
            raise RuntimeError("AKSHARE_DATA_DATABASE_URL is not configured")

        quoted_name = self._quote_identifier(table_name)
        offset = (page - 1) * page_size
        if adjustment is not None and not _VALID_ADJUSTMENT_VALUE.match(adjustment):
            raise AdjustmentBasisError(f"Invalid adjustment basis: {adjustment!r}")

        # The basis filter is part of the count's query shape: a filtered total can
        # never be reused for an unfiltered page (or for another basis).
        filters: tuple[tuple[str, str], ...] = (
            ((_ADJUSTMENT_COLUMN, adjustment),) if adjustment is not None else ()
        )
        cache_key = TotalCountKey(
            database=str(engine.url.database or ""),
            table_name=table_name,
            filters=filters,
        )
        async with engine.connect() as conn:
            ordering = await conn.run_sync(reflect_table_ordering, table_name)
            order_sql = order_by_clause(ordering)
            column_names = await conn.run_sync(
                lambda sync_conn: [
                    column["name"] for column in inspect(sync_conn).get_columns(table_name)
                ]
            )
            has_adjustment = _ADJUSTMENT_COLUMN in column_names

            where_clause = ""
            filter_params: dict[str, Any] = {}
            if adjustment is not None:
                if not has_adjustment:
                    raise AdjustmentBasisError(
                        f"Table {table_name} has no adjustment column; "
                        f"cannot filter by {adjustment!r}"
                    )
                # Exact existence probe: bounded at the first matching row, and
                # at most the same scan the filtered data query would perform.
                probe = await conn.execute(
                    text(
                        f"SELECT 1 FROM {quoted_name}"
                        f" WHERE {_ADJUSTMENT_COLUMN} = :adjustment LIMIT 1"
                    ),
                    {"adjustment": adjustment},
                )
                if probe.scalar() is None:
                    observed = await self._sample_observed_bases(conn, quoted_name)
                    raise AdjustmentBasisError(
                        f"Unknown adjustment basis {adjustment!r} for table "
                        f"{table_name}; observed bases: {observed}"
                    )
                where_clause = f" WHERE {_ADJUSTMENT_COLUMN} = :adjustment"
                filter_params["adjustment"] = adjustment

            total, total_source, total_age_seconds = await resolve_total_count(
                conn,
                count_sql=f"SELECT COUNT(*) FROM {quoted_name}{where_clause}",
                cache=get_total_count_cache(),
                key=cache_key,
                params=filter_params,
            )
            result = await conn.execute(
                text(
                    f"SELECT * FROM {quoted_name}{where_clause} {order_sql} "
                    "LIMIT :limit OFFSET :offset"
                ),
                {"limit": page_size, "offset": offset, **filter_params},
            )
            mappings = result.mappings().all()

            basis_meta: dict[str, Any] | None = None
            if has_adjustment:
                observed_bases = await self._sample_observed_bases(conn, quoted_name)
                basis_meta = {
                    "filter": adjustment,
                    "observed_bases": observed_bases,
                    "mixed_adjustment_basis": len(observed_bases) > 1,
                }

        rows = [dict(row) for row in mappings]
        if rows and set(rows[0]) == {_COMPACT_ROW_COLUMN, _COMPACT_PAYLOAD_COLUMN}:
            expanded_rows = [
                json.loads(str(row[_COMPACT_PAYLOAD_COLUMN]))
                for row in rows
                if row.get(_COMPACT_PAYLOAD_COLUMN)
            ]
            columns = list(expanded_rows[0].keys()) if expanded_rows else []
            return WarehouseTablePage(
                columns=columns,
                rows=expanded_rows,
                total=total,
                total_source=total_source,
                total_age_seconds=total_age_seconds,
                ordering=ordering,
                adjustment_basis=basis_meta,
            )

        columns = list(rows[0].keys()) if rows else []
        return WarehouseTablePage(
            columns=columns,
            rows=rows,
            total=total,
            total_source=total_source,
            total_age_seconds=total_age_seconds,
            ordering=ordering,
            adjustment_basis=basis_meta,
        )
