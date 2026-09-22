"""Data acquisition service.

Handles execution of akshare data interface calls and database storage.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import pandas as pd
from loguru import logger
from sqlalchemy import text

import akshare as ak
from opendata.models.data_table import DataTable
from opendata.models.interface import DataInterface
from opendata.models.task import TaskExecution, TaskStatus
from opendata.utils.constants import (
    BATCH_SIZE_LARGE,
    BATCH_SIZE_SMALL,
    BATCH_SIZE_THRESHOLD,
)
from opendata.utils.db_result import get_rowcount
from opendata.utils.helpers import (
    clean_column_names,
    generate_table_name,
    safe_column_name,
    safe_table_name,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from opendata.data.protocol import Fetcher, FetchResult


class DataAcquisitionService:
    """Service for acquiring financial data using akshare.

    Handles data retrieval, validation, and storage with progress tracking.
    """

    # Dedicated thread pool for blocking akshare calls (avoids starving default pool)
    _akshare_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare")

    def __init__(self) -> None:
        """Initialize the service with empty active-execution tracking."""
        self._active_executions: dict[int, dict[str, Any]] = {}

    async def execute_download(
        self,
        execution_id: int,
        interface_id: int,
        parameters: dict[str, Any],
        db: AsyncSession,
        *,
        data_db: AsyncSession,
    ) -> int | None:
        """Execute a data download for one interface.

        Metadata (interface, execution status, table registry) is read
        and written on the main-database session; the interface data
        table itself is created and filled on the data-warehouse
        session (FR-17 session routing fix).

        Args:
            execution_id: Task execution record ID
            interface_id: Data interface to execute
            parameters: Parameters for the interface
            db: Main-database session (metadata and execution state)
            data_db: Data-warehouse session (interface data tables)

        Returns:
            Number of rows affected or None if failed

        Raises:
            Exception: If data acquisition fails
        """
        from sqlalchemy import select

        # Get interface
        result = await db.execute(select(DataInterface).where(DataInterface.id == interface_id))
        interface = result.scalar_one_or_none()

        if interface is None:
            raise ValueError(f"Interface {interface_id} not found")

        # Get execution record
        exec_result = await db.execute(
            select(TaskExecution).where(TaskExecution.id == execution_id)
        )
        execution = exec_result.scalar_one_or_none()

        if execution is None:
            raise ValueError(f"Execution {execution_id} not found")

        # Track active execution
        self._active_executions[execution_id] = {
            "interface_id": interface_id,
            "parameters": parameters,
            "started_at": datetime.now(timezone.utc),
        }

        try:
            # Execute the interface fetch (registry-routed with legacy
            # fallback, A1.7)
            logger.info(f"Executing interface {interface.name} with parameters: {parameters}")

            data = await self._fetch_interface_data(interface, parameters)

            if data is None or (isinstance(data, pd.DataFrame) and data.empty):
                logger.warning(f"Interface {interface.name} returned no data")
                execution.status = TaskStatus.COMPLETED
                execution.end_time = datetime.now(timezone.utc)
                execution.rows_after = 0
                await db.commit()
                return 0

            # Store data (data table on the warehouse session, metadata
            # on the main session); commit the warehouse transaction
            # first so a later main-db failure leaves idempotent data.
            rows_affected = await self._store_data(
                data=data,
                interface=interface,
                execution_id=execution_id,
                db=db,
                data_db=data_db,
            )
            await data_db.commit()
            await db.commit()

            return rows_affected

        except Exception as e:
            logger.error(f"Data acquisition failed for {interface.name}: {e}")
            execution.status = TaskStatus.FAILED
            execution.error_message = str(e)
            execution.end_time = datetime.now(timezone.utc)
            await db.commit()
            raise

        finally:
            # Cleanup tracking
            self._active_executions.pop(execution_id, None)

    async def _fetch_interface_data(
        self,
        interface: DataInterface,
        parameters: dict[str, Any],
    ) -> pd.DataFrame | None:
        """Fetch interface data through the provider routing layer.

        A1.7 seam: registry-catalog interfaces are named by their
        domain identifier, so the fetch dispatches through
        ``ProviderRegistry.resolve_domain``. Until P0 fetchers
        register (A2.4) the lookup misses and the legacy direct call
        covers - its import is quarantined in
        ``_call_akshare_function`` and tracked by the zero-dep
        baseline.

        Args:
            interface: Data interface definition
            parameters: Fetch parameters (None values skipped)

        Returns:
            DataFrame with data or None
        """
        fetcher = self._resolve_fetcher(interface)
        if fetcher is None:
            return await self._call_akshare_function(interface, parameters)
        kwargs = {name: value for name, value in parameters.items() if value is not None}
        result = fetcher.fetch(**kwargs)
        return self._as_frame(result)

    def _resolve_fetcher(self, interface: DataInterface) -> Fetcher[Any, Any] | None:
        """Resolve the registry fetcher for an interface, None when unregistered.

        Args:
            interface: Data interface definition

        Returns:
            The routed fetcher, or None when no capability serves the
            interface's domain (legacy fallback applies).
        """
        from opendata.data.registry import get_registry

        try:
            fetcher = get_registry().resolve_domain(interface.name)
        except LookupError:
            logger.debug(f"no registry capability for interface {interface.name}; legacy path")
            return None
        logger.info(
            f"routing interface {interface.name} to source "
            f"{fetcher.capability.source} via provider registry"
        )
        return fetcher

    def _as_frame(self, result: FetchResult) -> pd.DataFrame | None:
        """Convert a fetcher result into the DataFrame the storage path expects.

        Args:
            result: Contract models or a ready DataFrame.

        Returns:
            The DataFrame, or None for an empty model sequence.
        """
        if isinstance(result, pd.DataFrame):
            return result
        rows = list(result)
        if not rows:
            return None
        return type(rows[0]).to_frame(rows)

    async def _call_akshare_function(
        self,
        interface: DataInterface,
        parameters: dict[str, Any],
        timeout: int | None = None,
    ) -> pd.DataFrame | None:
        """Call an upstream interface function with parameters.

        A1.7 staging: fetches run through the legacy akshare call until
        P0 fetchers register with the provider registry (A2.4); the
        direct import is quarantined here and tracked by the zero-dep
        baseline.

        Args:
            interface: Data interface definition
            parameters: Function parameters
            timeout: Optional timeout in seconds; the configured
                default applies when None or non-positive

        Returns:
            DataFrame with data or None
        """
        try:
            # Get the upstream module function
            func = getattr(ak, interface.name, None)

            if func is None:
                raise AttributeError(f"akshare function {interface.name} not found")

            # Build arguments, skipping None values
            kwargs = {
                param_name: param_value
                for param_name, param_value in parameters.items()
                if param_value is not None
            }

            # Call function (run in dedicated thread pool for blocking calls)
            # Use functools.partial instead of lambda to snapshot kwargs
            # and avoid closure capture issues
            loop = asyncio.get_running_loop()
            call = functools.partial(func, **kwargs) if kwargs else func
            coro = loop.run_in_executor(self._akshare_executor, call)

            # Apply timeout if specified
            effective_timeout = timeout if timeout and timeout > 0 else None
            if effective_timeout is None:
                from opendata.core.config import settings

                effective_timeout = settings.akshare_call_timeout or None

            if effective_timeout:
                try:
                    result = await asyncio.wait_for(coro, timeout=effective_timeout)
                except TimeoutError as e:
                    raise TimeoutError(
                        f"akshare function {interface.name} timed out after {effective_timeout}s"
                    ) from e
            else:
                result = await coro

            # Ensure result is DataFrame
            if not isinstance(result, pd.DataFrame):
                logger.warning(f"Interface {interface.name} did not return DataFrame")
                return None

            return result

        except AttributeError:
            logger.error(f"Function {interface.name} not found in akshare")
            raise
        except Exception as e:
            logger.error(f"Error calling akshare function {interface.name}: {e}")
            raise

    async def _store_data(
        self,
        data: pd.DataFrame,
        interface: DataInterface,
        execution_id: int,
        db: AsyncSession,
        *,
        data_db: AsyncSession,
    ) -> int:
        """Store fetched data and refresh its table registry entry.

        The data table is created and filled on the data-warehouse
        session; the registry metadata lives on the main session.

        Args:
            data: DataFrame to store
            interface: Source interface
            execution_id: Associated execution record
            db: Main-database session (metadata)
            data_db: Data-warehouse session (data table)

        Returns:
            Number of rows inserted
        """
        # Generate table name from interface name
        table_name = self._generate_table_name(interface.name)

        # Clean column names
        data.columns = self._clean_column_names(data.columns)

        # Create table if not exists (data warehouse)
        await self._create_table_if_not_exists(
            table_name=table_name,
            data=data,
            data_db=data_db,
        )

        # Insert data (data warehouse)
        rows_affected = await self._insert_data(
            table_name=table_name,
            data=data,
            data_db=data_db,
        )

        # Update or create data table metadata (main database)
        await self._update_table_metadata(
            table_name=table_name,
            interface_id=interface.id,
            execution_id=execution_id,
            row_count=rows_affected,
            db=db,
            data_db=data_db,
        )

        return rows_affected

    def _generate_table_name(self, interface_name: str) -> str:
        """Generate a SQL table name from an interface name."""
        return generate_table_name(interface_name)

    def _clean_column_names(self, columns: object) -> list[str]:
        """Clean DataFrame column names for SQL."""
        return clean_column_names(columns)

    async def _create_table_if_not_exists(
        self,
        table_name: str,
        data: pd.DataFrame,
        data_db: AsyncSession,
    ) -> None:
        """Create the data table on the warehouse session if missing."""
        # Build CREATE TABLE statement
        columns_defs = []
        for col in data.columns:
            dtype = data[col].dtype
            if pd.api.types.is_integer_dtype(dtype):
                sql_type = "BIGINT"
            elif pd.api.types.is_float_dtype(dtype):
                sql_type = "DOUBLE"
            elif pd.api.types.is_datetime64_any_dtype(dtype):
                sql_type = "DATETIME"
            else:
                # String type with 2x safety margin, min 255, max 2000
                max_len = data[col].astype(str).str.len().max()
                max_len = min(max(int((max_len or 1) * 2), 255), 2000)
                sql_type = f"VARCHAR({max_len})"

            columns_defs.append(f"{safe_column_name(col)} {sql_type}")

        # Add ID, row_hash (for dedup), and timestamp columns
        all_columns = (
            ["id BIGINT AUTO_INCREMENT PRIMARY KEY"]
            + columns_defs
            + [
                "`row_hash` CHAR(32) NOT NULL",
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP",
                "UNIQUE KEY `uq_row_hash` (`row_hash`)",
            ]
        )

        quoted_name = safe_table_name(table_name)
        create_sql = f"""
            CREATE TABLE IF NOT EXISTS {quoted_name} (
                {", ".join(all_columns)},
                INDEX idx_created_at (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """

        await data_db.execute(text(create_sql))

    async def _insert_data(
        self,
        table_name: str,
        data: pd.DataFrame,
        data_db: AsyncSession,
    ) -> int:
        """Insert data into the warehouse table with INSERT IGNORE dedup."""
        if data.empty:
            return 0

        # Prepare data for insertion (convert NaN/NaT to None for MySQL)
        data = data.where(pd.notna(data), None)

        # Compute row_hash for deduplication (MD5 of all data columns; non-crypto use)
        data_cols = list(data.columns)
        data["row_hash"] = (
            data[data_cols]
            .astype(str)
            .apply(
                lambda row: hashlib.md5(
                    "|".join(row.values).encode(), usedforsecurity=False
                ).hexdigest(),
                axis=1,
            )
        )
        records = data.to_dict("records")

        # Build INSERT IGNORE statement (duplicates rejected by UNIQUE row_hash)
        columns = list(data.columns)  # includes row_hash
        columns_str = ", ".join([safe_column_name(col) for col in columns])
        placeholders = ", ".join([f":{col}" for col in columns])

        quoted_name = safe_table_name(table_name)
        # safe_table_name validates against ^[A-Za-z_][A-Za-z0-9_]*$ (bandit B608 skip rationale).
        insert_sql = f"""
            INSERT IGNORE INTO {quoted_name} ({columns_str})
            VALUES ({placeholders})
        """

        # Adaptive batch size: larger batches for big datasets
        total_records = len(records)
        batch_size = BATCH_SIZE_LARGE if total_records > BATCH_SIZE_THRESHOLD else BATCH_SIZE_SMALL
        rows_inserted = 0
        flush_interval = 20000  # Flush every N rows to avoid large transaction memory

        for i in range(0, total_records, batch_size):
            batch = records[i : i + batch_size]
            result = await data_db.execute(text(insert_sql), batch)
            rows_inserted += get_rowcount(result)
            if i > 0 and i % flush_interval == 0:
                await data_db.flush()
                logger.debug(
                    f"Insert progress: {i + len(batch)}/{total_records} "
                    f"({rows_inserted} new rows so far)"
                )

        # Note: final commit is handled by the caller for transaction consistency
        logger.info(f"Inserted {rows_inserted} rows into {table_name} (ignored duplicates)")
        return rows_inserted

    async def _update_table_metadata(
        self,
        table_name: str,
        interface_id: int,
        execution_id: int,
        row_count: int,
        db: AsyncSession,
        *,
        data_db: AsyncSession,
    ) -> None:
        """Refresh the main-database table registry for a warehouse table.

        The registry row lives on the main session; the row count is
        queried on the warehouse session where the table resides.

        Args:
            table_name: Data table name
            interface_id: Source interface ID
            execution_id: Associated execution record
            row_count: Rows inserted by this run
            db: Main-database session (registry row)
            data_db: Data-warehouse session (row count query)
        """
        from sqlalchemy import select

        # Get existing metadata
        result = await db.execute(select(DataTable).where(DataTable.table_name == table_name))
        table_meta = result.scalar_one_or_none()

        # Get current total row count from the warehouse table
        quoted_name = safe_table_name(table_name)
        count_result = await data_db.execute(text(f"SELECT COUNT(*) FROM {quoted_name}"))
        total_rows = count_result.scalar() or 0

        if table_meta:
            # Update existing
            table_meta.row_count = total_rows
            table_meta.last_update_time = datetime.now(timezone.utc)
            table_meta.last_update_status = "success"
        else:
            # Create new
            table_meta = DataTable(
                table_name=table_name,
                table_comment=table_name.replace("ak_", "").replace("_", " ").title(),
                row_count=total_rows,
                last_update_time=datetime.now(timezone.utc),
                last_update_status="success",
            )
            db.add(table_meta)

        # Note: commit handled by caller for transaction consistency

    def get_progress(self, execution_id: int) -> dict[str, Any]:
        """Get progress of an active execution.

        Args:
            execution_id: Execution record ID

        Returns:
            Progress information dict
        """
        if execution_id not in self._active_executions:
            return {
                "execution_id": execution_id,
                "status": "not_found",
                "progress": 0,
            }

        info = self._active_executions[execution_id]
        return {
            "execution_id": execution_id,
            "status": "running",
            "interface_id": info.get("interface_id"),
            "started_at": info.get("started_at"),
        }

    def cancel_execution(self, execution_id: int) -> bool:
        """Cancel an active execution.

        Args:
            execution_id: Execution record ID

        Returns:
            True if cancelled, False otherwise
        """
        if execution_id in self._active_executions:
            self._active_executions.pop(execution_id, None)
            return True
        return False
