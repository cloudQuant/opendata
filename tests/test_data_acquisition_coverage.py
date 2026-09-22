"""
Tests for DataAcquisitionService covering execute_download, _store_data,
_create_table_if_not_exists, _insert_data, _update_table_metadata.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from opendata.models.task import TaskStatus
from opendata.services.data_acquisition import DataAcquisitionService


@pytest.fixture
def svc():
    return DataAcquisitionService()


class TestExecuteDownload:
    @pytest.mark.asyncio
    async def test_interface_not_found(self, svc):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with pytest.raises(ValueError, match="not found"):
            await svc.execute_download(1, 999, {}, mock_db, data_db=mock_db)

    @pytest.mark.asyncio
    async def test_execution_not_found(self, svc):
        mock_db = AsyncMock()
        mock_interface = MagicMock()
        mock_interface.name = "test_func"

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = mock_interface  # interface found
        results[1].scalar_one_or_none.return_value = None  # execution not found
        mock_db.execute.side_effect = results

        with pytest.raises(ValueError, match="not found"):
            await svc.execute_download(999, 1, {}, mock_db, data_db=mock_db)

    @pytest.mark.asyncio
    async def test_empty_data_returned(self, svc):
        mock_db = AsyncMock()
        mock_interface = MagicMock()
        mock_interface.name = "test_func"
        mock_interface.id = 1

        mock_execution = MagicMock()
        mock_execution.execution_id = "exec1"
        mock_execution.status = TaskStatus.PENDING

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = mock_interface
        results[1].scalar_one_or_none.return_value = mock_execution
        mock_db.execute.side_effect = results

        with patch.object(
            svc, "_call_akshare_function", new_callable=AsyncMock, return_value=pd.DataFrame()
        ):
            result = await svc.execute_download(1, 1, {}, mock_db, data_db=mock_db)

        assert result == 0
        assert mock_execution.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_successful_download(self, svc):
        mock_db = AsyncMock()
        mock_interface = MagicMock()
        mock_interface.name = "test_func"
        mock_interface.id = 1

        mock_execution = MagicMock()
        mock_execution.execution_id = "exec1"

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = mock_interface
        results[1].scalar_one_or_none.return_value = mock_execution
        mock_db.execute.side_effect = results

        df = pd.DataFrame({"col": [1, 2, 3]})

        with (
            patch.object(svc, "_call_akshare_function", new_callable=AsyncMock, return_value=df),
            patch.object(svc, "_store_data", new_callable=AsyncMock, return_value=3),
        ):
            result = await svc.execute_download(1, 1, {}, mock_db, data_db=mock_db)

        assert result == 3
        assert 1 not in svc._active_executions  # cleaned up

    @pytest.mark.asyncio
    async def test_download_exception(self, svc):
        mock_db = AsyncMock()
        mock_interface = MagicMock()
        mock_interface.name = "test_func"
        mock_interface.id = 1

        mock_execution = MagicMock()
        mock_execution.execution_id = "exec1"

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = mock_interface
        results[1].scalar_one_or_none.return_value = mock_execution
        mock_db.execute.side_effect = results

        with (
            patch.object(
                svc,
                "_call_akshare_function",
                new_callable=AsyncMock,
                side_effect=RuntimeError("fail"),
            ),
            pytest.raises(RuntimeError),
        ):
            await svc.execute_download(1, 1, {}, mock_db, data_db=mock_db)

        assert mock_execution.status == TaskStatus.FAILED


class TestCallAkshareFunction:
    @pytest.mark.asyncio
    async def test_function_not_found(self, svc):
        mock_interface = MagicMock()
        mock_interface.name = "nonexistent_function_xyz"

        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.nonexistent_function_xyz = None
            delattr(mock_ak, "nonexistent_function_xyz")
            with pytest.raises(AttributeError):
                await svc._call_akshare_function(mock_interface, {})

    @pytest.mark.asyncio
    async def test_successful_call(self, svc):
        mock_interface = MagicMock()
        mock_interface.name = "stock_data"
        df = pd.DataFrame({"a": [1]})

        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.stock_data = MagicMock(return_value=df)
            result = await svc._call_akshare_function(mock_interface, {"symbol": "000001"})

        assert isinstance(result, pd.DataFrame)

    @pytest.mark.asyncio
    async def test_non_dataframe_result(self, svc):
        mock_interface = MagicMock()
        mock_interface.name = "some_func"

        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.some_func = MagicMock(return_value="not a dataframe")
            result = await svc._call_akshare_function(mock_interface, {})

        assert result is None


class TestStoreData:
    @pytest.mark.asyncio
    async def test_store_data_flow(self, svc):
        mock_db = AsyncMock()
        mock_interface = MagicMock()
        mock_interface.name = "test_func"
        mock_interface.id = 1
        df = pd.DataFrame({"col_a": [1, 2]})

        with (
            patch.object(svc, "_create_table_if_not_exists", new_callable=AsyncMock),
            patch.object(svc, "_insert_data", new_callable=AsyncMock, return_value=2),
            patch.object(svc, "_update_table_metadata", new_callable=AsyncMock),
        ):
            result = await svc._store_data(df, mock_interface, 1, mock_db, data_db=mock_db)

        assert result == 2


class TestCreateTableIfNotExists:
    @pytest.mark.asyncio
    async def test_creates_table_with_int_col(self, svc):
        mock_db = AsyncMock()
        df = pd.DataFrame({"count": [1, 2, 3]})
        await svc._create_table_if_not_exists("ak_test", df, mock_db)
        mock_db.execute.assert_called_once()
        # Note: commit is now handled by the caller for transaction consistency

    @pytest.mark.asyncio
    async def test_creates_table_with_float_col(self, svc):
        mock_db = AsyncMock()
        df = pd.DataFrame({"price": [1.5, 2.5]})
        await svc._create_table_if_not_exists("ak_test", df, mock_db)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_table_with_datetime_col(self, svc):
        mock_db = AsyncMock()
        df = pd.DataFrame({"date": pd.to_datetime(["2024-01-01"])})
        await svc._create_table_if_not_exists("ak_test", df, mock_db)
        mock_db.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_table_with_str_col(self, svc):
        mock_db = AsyncMock()
        df = pd.DataFrame({"name": ["abc", "def"]})
        await svc._create_table_if_not_exists("ak_test", df, mock_db)
        mock_db.execute.assert_called_once()


class TestInsertData:
    @pytest.mark.asyncio
    async def test_empty_df(self, svc):
        mock_db = AsyncMock()
        result = await svc._insert_data("ak_test", pd.DataFrame(), mock_db)
        assert result == 0

    @pytest.mark.asyncio
    async def test_insert_rows(self, svc):
        mock_db = AsyncMock()
        df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
        await svc._insert_data("ak_test", df, mock_db)
        # result is based on rowcount from execute; mock returns 0 by default
        mock_db.execute.assert_called_once()
        # Note: commit is now handled by the caller for transaction consistency

    @pytest.mark.asyncio
    async def test_insert_large_batch(self, svc):
        mock_db = AsyncMock()
        # Mock execute to return a result with rowcount matching batch size
        mock_result_1 = MagicMock()
        mock_result_1.rowcount = 2000
        mock_result_2 = MagicMock()
        mock_result_2.rowcount = 500
        mock_db.execute = AsyncMock(side_effect=[mock_result_1, mock_result_2])
        df = pd.DataFrame({"a": list(range(2500))})
        result = await svc._insert_data("ak_test", df, mock_db)
        assert result == 2500
        # Should have 2 batch calls (2000 + 500)
        assert mock_db.execute.call_count == 2


class TestUpdateTableMetadata:
    @pytest.mark.asyncio
    async def test_update_existing(self, svc):
        mock_db = AsyncMock()
        mock_meta = MagicMock()

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = mock_meta  # existing metadata
        results[1].scalar.return_value = 100  # total rows
        mock_db.execute.side_effect = results

        await svc._update_table_metadata("ak_test", 1, 1, 50, mock_db, data_db=mock_db)
        assert mock_meta.row_count == 100
        # Note: commit is now handled by the caller for transaction consistency

    @pytest.mark.asyncio
    async def test_create_new(self, svc):
        mock_db = AsyncMock()

        results = [MagicMock(), MagicMock()]
        results[0].scalar_one_or_none.return_value = None  # no existing
        results[1].scalar.return_value = 50
        mock_db.execute.side_effect = results

        await svc._update_table_metadata("ak_test", 1, 1, 50, mock_db, data_db=mock_db)
        mock_db.add.assert_called_once()
        # Note: commit is now handled by the caller for transaction consistency
