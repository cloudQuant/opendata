"""
Data acquisition service.

Handles execution of akshare data interface calls and database storage.
"""

import asyncio
import functools
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pandas as pd
from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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


class DataAcquisitionService:
    """
    Service for acquiring financial data using akshare.

    Handles data retrieval, validation, and storage with progress tracking.
    """

    # Dedicated thread pool for blocking akshare calls (avoids starving default pool)
    _akshare_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="akshare")

    def __init__(self) -> None:
        self._active_executions: dict[int, dict] = {}

    async def execute_download(
        self,
        execution_id: int,
        interface_id: int,
        parameters: dict[str, Any],
        db: AsyncSession,
    ) -> int | None:
        """
        Execute data download for an interface.

        Args:
            execution_id: Task execution record ID
            interface_id: Data interface to execute
            parameters: Parameters for the interface
            db: Database session

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
            "started_at": datetime.now(UTC),
        }

        try:
            # Execute akshare function
            logger.info(f"Executing interface {interface.name} with parameters: {parameters}")

            # Call akshare function
            data = await self._call_akshare_function(interface, parameters)

            if data is None or (isinstance(data, pd.DataFrame) and data.empty):
                logger.warning(f"Interface {interface.name} returned no data")
                execution.status = TaskStatus.COMPLETED
                execution.end_time = datetime.now(UTC)
                execution.rows_after = 0
                await db.commit()
                return 0

            # Store data in database (single transaction for consistency)
            rows_affected = await self._store_data(
                data=data,
                interface=interface,
                execution_id=execution_id,
                db=db,
            )
            await db.commit()

            return rows_affected

        except Exception as e:
            logger.error(f"Data acquisition failed for {interface.name}: {e}")
            execution.status = TaskStatus.FAILED
            execution.error_message = str(e)
            execution.end_time = datetime.now(UTC)
            await db.commit()
            raise

        finally:
            # Cleanup tracking
            self._active_executions.pop(execution_id, None)

    async def _call_akshare_function(
        self,
        interface: DataInterface,
        parameters: dict[str, Any],
        timeout: int | None = None,
    ) -> pd.DataFrame | None:
        """
        Call akshare function with parameters.

        Args:
            interface: Data interface definition
            parameters: Function parameters

        Returns:
            DataFrame with data or None
        """
        try:
            # Get the akshare module function
            func = getattr(ak, interface.name, None)

            if func is None:
                raise AttributeError(f"akshare function {interface.name} not found")

            # Build arguments
            kwargs = {}
            for param_name, param_value in parameters.items():
                # Skip None values
                if param_value is not None:
                    kwargs[param_name] = param_value

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
    ) -> int:
        """
        Store data in database.

        Creates table if needed and inserts data.

        Args:
            data: DataFrame to store
            interface: Source interface
            execution_id: Associated execution record
            db: Database session

        Returns:
            Number of rows inserted
        """
        # Generate table name from interface name
        table_name = self._generate_table_name(interface.name)

        # Clean column names
        data.columns = self._clean_column_names(data.columns)

        # Create table if not exists
        await self._create_table_if_not_exists(
            table_name=table_name,
            data=data,
            db=db,
        )

        # Insert data
        rows_affected = await self._insert_data(
            table_name=table_name,
            data=data,
            db=db,
        )

        # Update or create data table metadata
        await self._update_table_metadata(
            table_name=table_name,
            interface_id=interface.id,
            execution_id=execution_id,
            row_count=rows_affected,
            db=db,
        )

        return rows_affected

    def _generate_table_name(self, interface_name: str) -> str:
        """Generate SQL table name from interface name."""
        return generate_table_name(interface_name)

    def _clean_column_names(self, columns) -> list[str]:
        """Clean DataFrame column names for SQL."""
        return clean_column_names(columns)

    async def _create_table_if_not_exists(
        self,
        table_name: str,
        data: pd.DataFrame,
        db: AsyncSession,
    ) -> None:
        """Create table if it doesn't exist."""
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

        await db.execute(text(create_sql))

    async def _insert_data(
        self,
        table_name: str,
        data: pd.DataFrame,
        db: AsyncSession,
    ) -> int:
        """Insert data into table using INSERT IGNORE to avoid duplicates."""
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
            result = await db.execute(text(insert_sql), batch)
            rows_inserted += get_rowcount(result)
            if i > 0 and i % flush_interval == 0:
                await db.flush()
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
    ) -> None:
        """Update data table metadata record."""
        from sqlalchemy import select

        # Get existing metadata
        result = await db.execute(select(DataTable).where(DataTable.table_name == table_name))
        table_meta = result.scalar_one_or_none()

        # Get current total row count
        quoted_name = safe_table_name(table_name)
        count_result = await db.execute(text(f"SELECT COUNT(*) FROM {quoted_name}"))
        total_rows = count_result.scalar() or 0

        if table_meta:
            # Update existing
            table_meta.row_count = total_rows
            table_meta.last_update_time = datetime.now(UTC)
            table_meta.last_update_status = "success"
        else:
            # Create new
            table_meta = DataTable(
                table_name=table_name,
                table_comment=table_name.replace("ak_", "").replace("_", " ").title(),
                row_count=total_rows,
                last_update_time=datetime.now(UTC),
                last_update_status="success",
            )
            db.add(table_meta)

        # Note: commit handled by caller for transaction consistency

    def get_progress(self, execution_id: int) -> dict[str, Any]:
        """
        Get progress of an active execution.

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
        """
        Cancel an active execution.

        Args:
            execution_id: Execution record ID

        Returns:
            True if cancelled, False otherwise
        """
        if execution_id in self._active_executions:
            self._active_executions.pop(execution_id, None)
            return True
        return False


# Note: asyncio import moved to top of file
