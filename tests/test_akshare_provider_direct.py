"""
Direct tests for AkshareProvider to maximize coverage.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from opendata.data_fetch.providers.akshare_provider import AkshareProvider, FuncThread


class TestFuncThread:
    def test_success(self):
        def add(a, b):
            return a + b

        t = FuncThread(add, 1, 2)
        t.start()
        t.join(timeout=5)
        status, result = t.get_result(timeout=1)
        assert status == "success"
        assert result == 3

    def test_exception(self):
        def fail():
            raise ValueError("boom")

        t = FuncThread(fail)
        t.start()
        t.join(timeout=5)
        status, result = t.get_result(timeout=1)
        assert status == "error"
        assert "boom" in result

    def test_timeout(self):
        import time

        def slow():
            time.sleep(10)

        t = FuncThread(slow)
        t.start()
        status, result = t.get_result(timeout=0.1)
        assert status == "timeout"
        assert result is None


class TestAkshareProviderInit:
    def test_default_init(self):
        provider = AkshareProvider()
        assert provider.connection is None
        assert provider.cursor is None
        assert provider.batch_size == 1000
        assert provider.max_retries == 3

    def test_custom_db_url(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        assert provider.db_url == "sqlite:///test.db"

    def test_mysql_url_parsing(self):
        provider = AkshareProvider(db_url="mysql://user:pass@host:3307/mydb")
        assert provider.db_config["host"] == "host"
        assert provider.db_config["port"] == 3307
        assert provider.db_config["user"] == "user"
        assert provider.db_config["password"] == "pass"
        assert provider.db_config["database"] == "mydb"


class TestConnectDisconnect:
    def test_connect_non_mysql(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        result = provider.connect_db()
        assert result is False

    def test_disconnect_no_connection(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        provider.disconnect_db()  # Should not raise

    def test_disconnect_with_mocks(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        mock_cursor = MagicMock()
        mock_conn = MagicMock()
        mock_conn.open = True
        provider.cursor = mock_cursor
        provider.connection = mock_conn
        provider.disconnect_db()
        mock_cursor.close.assert_called_once()
        mock_conn.close.assert_called_once()
        assert provider.cursor is None
        assert provider.connection is None

    def test_context_manager(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        with (
            patch.object(provider, "connect_db") as mock_conn,
            patch.object(provider, "disconnect_db") as mock_disc,
        ):
            with provider:
                mock_conn.assert_called_once()
            mock_disc.assert_called_once()


class TestFetchAkData:
    def test_success_dataframe(self):
        provider = AkshareProvider()
        df = pd.DataFrame({"a": [1, 2]})
        with patch("opendata.data_fetch.providers.akshare_provider.ak") as mock_ak:
            mock_ak.test_func = MagicMock(return_value=df)
            result = provider.fetch_ak_data("test_func", _call_timeout=5)
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2

    def test_success_non_dataframe(self):
        provider = AkshareProvider()
        with patch("opendata.data_fetch.providers.akshare_provider.ak") as mock_ak:
            mock_ak.test_func = MagicMock(return_value="string_result")
            result = provider.fetch_ak_data("test_func", _call_timeout=5)
        assert result == "string_result"

    def test_function_not_found(self):
        provider = AkshareProvider()
        with pytest.raises(AttributeError):
            provider.fetch_ak_data("nonexistent_func_xyz_abc_123")

    def test_function_raises(self):
        provider = AkshareProvider()
        with patch("opendata.data_fetch.providers.akshare_provider.ak") as mock_ak:
            mock_ak.error_func = MagicMock(side_effect=RuntimeError("net error"))
            with pytest.raises(Exception):
                provider.fetch_ak_data("error_func", _call_timeout=5)

    def test_with_kwargs(self):
        provider = AkshareProvider()
        df = pd.DataFrame({"x": [1]})
        with patch("opendata.data_fetch.providers.akshare_provider.ak") as mock_ak:
            mock_ak.param_func = MagicMock(return_value=df)
            result = provider.fetch_ak_data("param_func", symbol="000001", _call_timeout=5)
        assert isinstance(result, pd.DataFrame)


class TestAutoCreateTable:
    def test_auto_create(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        df = pd.DataFrame(
            {
                "int_col": pd.Series([1, 2], dtype="int64"),
                "float_col": pd.Series([1.0, 2.0], dtype="float64"),
                "str_col": pd.Series(["a", "b"], dtype="object"),
            }
        )
        provider._auto_create_table("test_tbl", df)
        provider.cursor.execute.assert_called_once()
        call_sql = provider.cursor.execute.call_args[0][0]
        assert "CREATE TABLE" in call_sql
        assert "BIGINT" in call_sql
        assert "DOUBLE" in call_sql
        assert "TEXT" in call_sql


class TestExecuteBatch:
    def test_success(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        result = provider._execute_batch("INSERT ...", [(1,), (2,)])
        assert result is True
        provider.cursor.executemany.assert_called_once()


class TestGetTableRowCount:
    def test_empty_name(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        assert provider.get_table_row_count("") == 0
        assert provider.get_table_row_count(None) == 0


class TestGetUUID:
    def test_uuid(self):
        uid = AkshareProvider.get_uuid()
        assert len(uid) == 32
        assert "-" not in uid


class TestSafeDateFormat:
    def test_valid_dates(self):
        provider = AkshareProvider()
        s = pd.Series(["2023-01-01", "2023-12-31", None])
        result = provider.safe_date_format(s)
        assert result.iloc[0] == "2023-01-01"
        assert result.iloc[1] == "2023-12-31"
        assert pd.isna(result.iloc[2]) or result.iloc[2] is None

    def test_invalid_dates(self):
        provider = AkshareProvider()
        s = pd.Series(["not_a_date", "also_bad"])
        result = provider.safe_date_format(s)
        assert result.iloc[0] is None


class TestGetTableRowCountAsync:
    @pytest.mark.asyncio
    async def test_empty_name(self):
        provider = AkshareProvider()
        result = await provider.get_table_row_count_async("", AsyncMock())
        assert result is None

    @pytest.mark.asyncio
    async def test_success(self):
        provider = AkshareProvider()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar.return_value = 42
        mock_db.execute.return_value = mock_result
        result = await provider.get_table_row_count_async("test_tbl", mock_db)
        assert result == 42

    @pytest.mark.asyncio
    async def test_exception(self):
        provider = AkshareProvider()
        mock_db = AsyncMock()
        mock_db.execute.side_effect = Exception("fail")
        result = await provider.get_table_row_count_async("test_tbl", mock_db)
        assert result is None


class TestSaveDataEdgeCases:
    def test_empty_dataframe(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        result = provider.save_data(pd.DataFrame(), "test_tbl")
        assert result == 0

    def test_invalid_columns(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        df = pd.DataFrame({None: [1], float("nan"): [2]})
        result = provider.save_data(df, "test_tbl")
        assert result == 0

    def test_save_basic_insert(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        # SHOW TABLES returns existing table
        provider.cursor.fetchone.side_effect = [("tbl",)]  # table exists
        # SHOW COLUMNS returns columns
        provider.cursor.fetchall.return_value = [
            ("name", "TEXT", "YES", "", None, ""),
            ("val", "INT", "YES", "", None, ""),
        ]
        provider.cursor.executemany = MagicMock()
        df = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.save_data(df, "test_tbl")
        assert result == 2

    def test_save_ignore_duplicates(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        provider.cursor.fetchone.side_effect = [("tbl",)]
        provider.cursor.fetchall.return_value = [("name", "TEXT", "YES", "", None, "")]
        provider.cursor.executemany = MagicMock()
        df = pd.DataFrame({"name": ["a"]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.save_data(df, "test_tbl", ignore_duplicates=True)
        assert result == 1

    def test_save_on_duplicate_update(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        provider.cursor.fetchone.side_effect = [("tbl",)]
        provider.cursor.fetchall.return_value = [
            ("id", "INT", "NO", "PRI", None, ""),
            ("name", "TEXT", "YES", "", None, ""),
        ]
        provider.cursor.executemany = MagicMock()
        df = pd.DataFrame({"id": [1], "name": ["a"]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.save_data(
                df, "test_tbl", on_duplicate_update=True, unique_keys=["id"]
            )
        assert result == 1

    def test_save_create_table(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        # First fetchone: table doesn't exist (None), then SHOW COLUMNS
        provider.cursor.fetchone.return_value = None
        provider.cursor.fetchall.return_value = [("name", "TEXT", "YES", "", None, "")]
        provider.cursor.executemany = MagicMock()
        df = pd.DataFrame({"name": ["a"]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
            patch.object(provider, "_auto_create_table"),
        ):
            result = provider.save_data(df, "test_tbl", create_table=True)
        assert result == 1

    def test_save_no_valid_columns_after_align(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        provider.cursor.fetchone.side_effect = [("tbl",)]
        # Table has column "id" but df has "name" - no overlap
        provider.cursor.fetchall.return_value = [("id", "INT", "NO", "PRI", None, "")]
        df = pd.DataFrame({"name": ["a"]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.save_data(df, "test_tbl")
        assert result == 0

    def test_save_dropped_nan_col_name(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        provider.cursor.fetchone.side_effect = [("tbl",)]
        provider.cursor.fetchall.return_value = [("good", "TEXT", "YES", "", None, "")]
        provider.cursor.executemany = MagicMock()
        # One good column, one "nan" column
        df = pd.DataFrame({"good": ["a"], "nan": [1]})
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.save_data(df, "test_tbl")
        assert result == 1


class TestConnectDbMySQL:
    def test_connect_success(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        mock_conn = MagicMock()
        mock_conn.open = True
        mock_conn.cursor.return_value = MagicMock()
        with (
            patch(
                "opendata.data_fetch.providers.akshare_provider._get_connection_pool",
                return_value=None,
            ),
            patch(
                "opendata.data_fetch.providers.akshare_provider.pymysql.connect",
                return_value=mock_conn,
            ),
        ):
            result = provider.connect_db()
        assert result is True
        assert provider.connection is mock_conn

    def test_connect_already_open(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        mock_conn = MagicMock()
        mock_conn.open = True
        provider.connection = mock_conn
        result = provider.connect_db()
        assert result is True

    def test_connect_error(self):
        import pymysql

        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        with (
            patch(
                "opendata.data_fetch.providers.akshare_provider.pymysql.connect",
                side_effect=pymysql.Error("fail"),
            ),
            pytest.raises(pymysql.Error),
        ):
            provider.connect_db()


class TestGetTableRowCountMySQL:
    def test_success(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.fetchone.return_value = (42,)
        provider.connection = MagicMock()
        provider.connection.open = True
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.get_table_row_count("test_tbl")
        assert result == 42

    def test_error(self):
        import pymysql

        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.execute.side_effect = pymysql.Error("fail")
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.get_table_row_count("test_tbl")
        assert result == 0


class TestCreateTableIfNotExistsMySQL:
    def test_create_success(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.connection = MagicMock()
        provider.connection.open = True
        with (
            patch.object(provider, "table_exists", return_value=False),
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.create_table_if_not_exists("new_tbl", "CREATE TABLE new_tbl (id INT)")
        assert result is True

    def test_create_error(self):
        import pymysql

        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.execute.side_effect = pymysql.Error("fail")
        provider.connection = MagicMock()
        with (
            patch.object(provider, "table_exists", return_value=False),
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.create_table_if_not_exists("new_tbl", "CREATE TABLE ...")
        assert result is False


class TestTableExists:
    def test_table_found(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.fetchone.return_value = ("test_tbl",)
        provider.connection = MagicMock()
        provider.connection.open = True
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.table_exists("test_tbl")
        assert result is True

    def test_table_not_found(self):
        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.fetchone.return_value = None
        provider.connection = MagicMock()
        provider.connection.open = True
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.table_exists("test_tbl")
        assert result is False

    def test_error(self):
        import pymysql

        provider = AkshareProvider(db_url="mysql://u:p@h/db")
        provider.cursor = MagicMock()
        provider.cursor.execute.side_effect = pymysql.Error("fail")
        with (
            patch.object(provider, "connect_db", return_value=True),
            patch.object(provider, "disconnect_db"),
        ):
            result = provider.table_exists("test_tbl")
        assert result is False


class TestCreateTableIfNotExists:
    def test_table_already_exists(self):
        provider = AkshareProvider(db_url="sqlite:///test.db")
        with patch.object(provider, "table_exists", return_value=True):
            result = provider.create_table_if_not_exists("existing_tbl", "CREATE TABLE ...")
        assert result is False
