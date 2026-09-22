"""Akshare data provider for data fetch operations.

Supports MySQL storage with automatic table creation.
"""

import logging
import os
import queue
import threading
import uuid
from datetime import datetime
from typing import Any

import pandas as pd
import pymysql
from loguru import logger as _default_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import opendata_http as ak
from opendata.core.config import settings

# Connection pool singleton
_connection_pool = None
_pool_lock = threading.Lock()


def _get_connection_pool(db_config: dict[str, Any]) -> Any:
    """Get or create a connection pool singleton."""
    global _connection_pool
    if _connection_pool is not None:
        return _connection_pool
    with _pool_lock:
        if _connection_pool is not None:
            return _connection_pool
        try:
            from dbutils.pooled_db import PooledDB

            _connection_pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=2,
                maxcached=5,
                blocking=True,
                **db_config,
            )
            _default_logger.info("MySQL connection pool created (DBUtils)")
        except ImportError:
            _default_logger.warning(
                "DBUtils not installed, falling back to direct connections. "
                "Install with: pip install DBUtils"
            )
            _connection_pool = None
        return _connection_pool


class FuncThread(threading.Thread):
    """Thread for executing a function with a timeout."""

    def __init__(self, func: Any, *args: Any, **kwargs: Any) -> None:
        """Store the callable and its arguments."""
        super().__init__()
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.result: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.exc: BaseException | None = None
        self.daemon = True

    def run(self) -> None:
        """Execute the function and capture its result or error."""
        try:
            result = self.func(*self.args, **self.kwargs)
            self.result.put(("success", result))
        except Exception as e:
            self.result.put(("error", str(e)))

    def get_result(self, timeout: float | None = None) -> tuple[str, Any]:
        """Wait for the thread result within the timeout."""
        try:
            return self.result.get(timeout=timeout)
        except queue.Empty:
            return ("timeout", None)


class AkshareProvider:
    """Akshare数据提供者.

    支持从opendata_http获取数据并存储到数据库。
    """

    def __init__(self, db_url: str | None = None, logger: logging.Logger | None = None) -> None:
        """初始化数据提供者.

        Args:
            db_url: 数据库连接URL
            logger: 日志记录器
        """
        self.db_url = db_url or settings.database_url
        self.logger = logger or _default_logger
        self.connection = None
        self.cursor = None
        self.batch_size = 1000
        self.max_retries = 3
        self.retry_delay = 5

        # MySQL连接配置
        if self.db_url.startswith("mysql"):
            self._parse_db_url()

    def _parse_db_url(self) -> None:
        """解析数据库URL."""
        from urllib.parse import urlparse

        parsed = urlparse(self.db_url)
        self.db_config = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username or "root",
            "password": parsed.password or "",
            "database": parsed.path.lstrip("/") if parsed.path else "opendata_data",
            "charset": "utf8mb4",
        }

    def connect_db(self) -> bool:
        """建立数据库连接（优先使用连接池）."""
        if not self.db_url.startswith("mysql"):
            self.logger.warning("Only MySQL is supported for data storage")
            return False

        try:
            if not self.connection or not self.connection.open:
                pool = _get_connection_pool(self.db_config)
                if pool is not None:
                    self.connection = pool.connection()
                else:
                    self.connection = pymysql.connect(**self.db_config)
                self.cursor = self.connection.cursor()
            return True
        except pymysql.Error as err:
            self.logger.error(f"数据库连接失败: {err}")
            raise

    def disconnect_db(self) -> None:
        """归还连接到连接池（或关闭直接连接）."""
        if self.cursor:
            self.cursor.close()
            self.cursor = None
        if self.connection:
            self.connection.close()  # Returns to pool if pooled
            self.connection = None

    def __enter__(self) -> "AkshareProvider":
        """Connect on context entry."""
        self.connect_db()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Disconnect on context exit."""
        self.disconnect_db()

    def fetch_ak_data(self, function_name: str, *args: Any, **kwargs: Any) -> pd.DataFrame:
        """从Akshare获取数据.

        Args:
            function_name: Akshare函数名
            *args: 位置参数，透传给目标函数
            **kwargs: 关键字参数，透传给目标函数

        Returns:
            pd.DataFrame: 获取的数据
        """
        try:
            call_timeout_s = kwargs.pop("_call_timeout", None)
            if call_timeout_s is None:
                call_timeout_s = int(os.getenv("AKSHARE_CALL_TIMEOUT", "120"))
            else:
                call_timeout_s = int(call_timeout_s)

            func = getattr(ak, function_name)
            self.logger.info(f"调用Akshare函数: {function_name}")

            thread = FuncThread(func, *args, **kwargs)
            thread.start()

            status, result = thread.get_result(timeout=call_timeout_s)

            if status == "success":
                if isinstance(result, pd.DataFrame):
                    self.logger.info(f"获取数据成功，共{len(result)}行")
                else:
                    self.logger.info(f"获取数据成功，类型: {type(result)}")
                return result
            if status == "timeout":
                raise TimeoutError(f"Function call timed out after {call_timeout_s} seconds")
            raise Exception(result)

        except TimeoutError as te:
            self.logger.error(f"获取数据超时: {te}")
            raise
        except AttributeError:
            self.logger.error(f"Akshare中不存在函数: {function_name}")
            raise
        except Exception as e:
            self.logger.error(f"从Akshare获取数据失败: {e}")
            raise

    def _auto_create_table(self, table_name: str, df: pd.DataFrame) -> None:
        """根据DataFrame自动创建表."""
        type_map = {
            "int64": "BIGINT",
            "int32": "INT",
            "int16": "SMALLINT",
            "int8": "TINYINT",
            "uint64": "BIGINT UNSIGNED",
            "uint32": "INT UNSIGNED",
            "float64": "DOUBLE",
            "float32": "FLOAT",
            "bool": "TINYINT(1)",
            "datetime64[ns]": "DATETIME",
            "object": "TEXT",
        }

        col_defs = []
        for col in df.columns:
            dtype_str = str(df[col].dtype)
            mysql_type = type_map.get(dtype_str, "TEXT")
            esc = col.replace("`", "``")
            col_defs.append(f"  `{esc}` {mysql_type}")

        cols_sql = ",\n".join(col_defs)
        create_sql = (
            f"CREATE TABLE IF NOT EXISTS `{table_name}` (\n"
            f"{cols_sql}\n"
            f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )

        try:
            self.cursor.execute(create_sql)
            self.connection.commit()
            self.logger.info(f"自动建表成功: {table_name}")
        except pymysql.Error as err:
            self.logger.warning(f"自动建表失败 {table_name}: {err}")

    def _execute_batch(self, insert_sql: str, batch: list) -> bool:
        """执行批量插入."""
        try:
            self.cursor.executemany(insert_sql, batch)
            self.connection.commit()
            return True
        except pymysql.Error as err:
            self.connection.rollback()
            self.logger.error(f"批量执行失败: {err}")
            raise

    @staticmethod
    def _validate_identifier(name: str) -> str:
        """Validate and sanitize a SQL identifier (table/column name).

        Only allows alphanumeric characters and underscores to prevent
        SQL injection via identifier names.
        """
        import re

        cleaned = str(name).strip()
        if not re.match(r"^[A-Za-z0-9_]+$", cleaned):
            raise ValueError(f"Invalid SQL identifier: {cleaned!r}")
        return cleaned

    def _normalize_dataframe_columns(
        self, df: pd.DataFrame, table_name: str
    ) -> pd.DataFrame | None:
        """Normalize and filter DataFrame columns; return None if no valid columns."""
        raw_cols = list(df.columns)
        valid_idx = []
        normalized_cols = []
        dropped = []

        for idx, col in enumerate(raw_cols):
            if col is None or (isinstance(col, float) and pd.isna(col)):
                dropped.append(str(col))
                continue
            col_str = str(col).strip()
            if not col_str or col_str.lower() == "nan":
                dropped.append(col_str or "<empty>")
                continue
            valid_idx.append(idx)
            normalized_cols.append(col_str)

        if dropped:
            self.logger.warning(f"表 {table_name} 将丢弃 {len(dropped)} 个无效列名")
        if not valid_idx:
            self.logger.warning(f"表 {table_name} 所有列名均无效，跳过保存")
            return None

        out = df.iloc[:, valid_idx].copy()
        out.columns = normalized_cols
        return out

    def _align_df_to_table(
        self, df: pd.DataFrame, safe_table: str, table_name: str
    ) -> pd.DataFrame | None:
        """Align DataFrame columns to existing table schema; return None if no writable columns."""
        try:
            self.cursor.execute(f"SHOW COLUMNS FROM `{safe_table}`")
            table_rows = self.cursor.fetchall() or []
            table_cols_ci = {row[0].lower(): row[0] for row in table_rows}
        except pymysql.Error as err:
            self.logger.warning(f"无法读取表字段列表: {err}")
            return df

        if not table_cols_ci:
            return df

        column_mapping = {}
        unknown = []
        for c in df.columns:
            c_lower = str(c).lower()
            if c_lower in table_cols_ci:
                column_mapping[c] = table_cols_ci[c_lower]
            else:
                unknown.append(c)

        if unknown:
            self.logger.warning(f"表 {table_name} 将忽略 {len(unknown)} 个不存在的列")
        if not column_mapping:
            self.logger.warning(f"表 {table_name} 无可写入的有效列")
            return None
        return df.rename(columns=column_mapping).copy()

    def _build_insert_sql(
        self,
        safe_table: str,
        cols: list[str],
        on_duplicate_update: bool,
        unique_keys: list[str] | None,
        ignore_duplicates: bool,
    ) -> str:
        """Build INSERT SQL statement."""
        quoted_cols = [f"`{str(col).replace('`', '``')}`" for col in cols]
        placeholders = ", ".join(["%s"] * len(cols))
        cols_str = ", ".join(quoted_cols)

        if ignore_duplicates:
            return f"INSERT IGNORE INTO `{safe_table}` ({cols_str}) VALUES ({placeholders})"
        if on_duplicate_update and unique_keys:
            insert_sql = f"INSERT INTO `{safe_table}` ({cols_str}) VALUES ({placeholders})"
            update_clauses = []
            for col in cols:
                if col not in unique_keys:
                    esc = str(col).replace("`", "``")
                    update_clauses.append(f"`{esc}`=VALUES(`{esc}`)")
            if update_clauses:
                insert_sql += f" ON DUPLICATE KEY UPDATE {', '.join(update_clauses)}"
            return insert_sql
        return f"INSERT INTO `{safe_table}` ({cols_str}) VALUES ({placeholders})"

    def save_data(
        self,
        df: pd.DataFrame,
        table_name: str,
        on_duplicate_update: bool = False,
        unique_keys: list[str] | None = None,
        ignore_duplicates: bool = False,
        create_table: bool = True,
    ) -> int:
        """保存数据到数据库.

        Args:
            df: 要保存的数据
            table_name: 表名
            on_duplicate_update: 重复时更新
            unique_keys: 唯一键
            ignore_duplicates: 忽略重复
            create_table: 表不存在时自动创建

        Returns:
            保存的行数
        """
        if df.empty:
            self.logger.warning(f"DataFrame为空，无需保存到 {table_name}")
            return 0

        df = self._normalize_dataframe_columns(df, table_name)
        if df is None:
            return 0

        self.connect_db()
        safe_table = self._validate_identifier(table_name)

        if create_table:
            try:
                self.cursor.execute("SHOW TABLES LIKE %s", (safe_table,))
                if not self.cursor.fetchone():
                    self._auto_create_table(safe_table, df)
            except pymysql.Error as err:
                self.logger.warning(f"检查表是否存在时出错: {err}")

        df = self._align_df_to_table(df, safe_table, table_name)
        if df is None:
            return 0

        cols = list(df.columns)
        insert_sql = self._build_insert_sql(
            safe_table, cols, on_duplicate_update, unique_keys, ignore_duplicates
        )

        df = df.astype(object).where(pd.notnull(df), None)
        values = df.values.tolist()
        total_rows = len(values)

        try:
            start_time = datetime.now()
            processed = 0

            for i in range(0, total_rows, self.batch_size):
                batch = values[i : i + self.batch_size]
                self._execute_batch(insert_sql, batch)
                processed += len(batch)

                if (i + self.batch_size) % (self.batch_size * 10) == 0 or (
                    i + self.batch_size
                ) >= total_rows:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    self.logger.info(f"已处理 {processed}/{total_rows} 行，耗时 {elapsed:.2f}s")

            elapsed_seconds = (datetime.now() - start_time).total_seconds()
            self.logger.info(
                f"成功保存 {total_rows} 行数据到 {table_name}，耗时 {elapsed_seconds:.2f}s"
            )

            return total_rows

        except Exception as e:
            self.logger.error(f"保存数据到 {table_name} 失败: {e!s}")
            raise
        finally:
            self.disconnect_db()

    def table_exists(self, table_name: str) -> bool:
        """检查表是否存在."""
        try:
            self.connect_db()
            self.cursor.execute("SHOW TABLES LIKE %s", (table_name,))
            result = self.cursor.fetchone()
            return result is not None
        except pymysql.Error as err:
            self.logger.error(f"检查表是否存在时出错: {err}")
            return False
        finally:
            self.disconnect_db()

    def get_table_row_count(self, table_name: str) -> int:
        """获取表的行数."""
        if not table_name:
            return 0

        try:
            safe_name = self._validate_identifier(table_name)
            self.connect_db()
            self.cursor.execute(f"SELECT COUNT(*) FROM `{safe_name}`")
            return self.cursor.fetchone()[0] or 0
        except pymysql.Error:
            return 0
        finally:
            self.disconnect_db()

    def create_table_if_not_exists(
        self, table_name: str = None, create_table_sql: str = None
    ) -> bool:
        """创建表（如果不存在）."""
        if not self.table_exists(table_name):
            try:
                self.connect_db()
                self.cursor.execute(create_table_sql)
                self.connection.commit()
                self.logger.info(f"成功创建表: {table_name}")
                return True
            except pymysql.Error as err:
                self.logger.error(f"创建表失败: {err}")
                return False
            finally:
                self.disconnect_db()
        return False

    async def get_table_row_count_async(
        self, table_name: str, db_session: AsyncSession
    ) -> int | None:
        """异步获取表的行数."""
        if not table_name:
            return None

        try:
            safe_name = self._validate_identifier(table_name)
            result = await db_session.execute(text(f"SELECT COUNT(*) FROM `{safe_name}`"))
            return result.scalar() or 0
        except Exception as e:
            self.logger.debug("Could not get row count for table %s: %s", table_name, e)
            return None

    @staticmethod
    def get_uuid() -> str:
        """生成UUID."""
        return str(uuid.uuid4()).replace("-", "").upper()

    def safe_date_format(self, series: pd.Series) -> pd.Series:
        """安全格式化日期序列."""
        datetime_series = pd.to_datetime(series, errors="coerce")
        return datetime_series.apply(lambda x: x.strftime("%Y-%m-%d") if not pd.isnull(x) else None)
