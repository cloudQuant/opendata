"""
Direct tests for DataAcquisitionService to maximize coverage.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from opendata.services.data_acquisition import DataAcquisitionService


class TestInit:
    def test_init(self):
        svc = DataAcquisitionService()
        assert svc._active_executions == {}


class TestGenerateTableName:
    def test_basic(self):
        svc = DataAcquisitionService()
        assert svc._generate_table_name("stock_zh_a_hist") == "ak_stock_zh_a_hist"

    def test_dots(self):
        svc = DataAcquisitionService()
        assert svc._generate_table_name("fund.etf.list") == "ak_fund_etf_list"

    def test_dashes(self):
        svc = DataAcquisitionService()
        assert svc._generate_table_name("bond-zh-hs") == "ak_bond_zh_hs"


class TestCleanColumnNames:
    def test_basic(self):
        svc = DataAcquisitionService()
        result = svc._clean_column_names(["Name", "Price USD", "Date-Time"])
        assert result == ["name", "price_usd", "date_time"]

    def test_special_chars(self):
        svc = DataAcquisitionService()
        result = svc._clean_column_names(["col(1)", "col%2"])
        assert result == ["col_1", "col_2"]

    def test_long_name(self):
        svc = DataAcquisitionService()
        long_name = "a" * 100
        result = svc._clean_column_names([long_name])
        assert len(result[0]) == 64


class TestGetProgress:
    def test_not_found(self):
        svc = DataAcquisitionService()
        result = svc.get_progress(999)
        assert result["status"] == "not_found"

    def test_active(self):
        svc = DataAcquisitionService()
        svc._active_executions[1] = {
            "interface_id": 10,
            "started_at": datetime.now(UTC),
        }
        result = svc.get_progress(1)
        assert result["status"] == "running"
        assert result["interface_id"] == 10


class TestCancelExecution:
    def test_cancel_active(self):
        svc = DataAcquisitionService()
        svc._active_executions[1] = {"interface_id": 10}
        assert svc.cancel_execution(1) is True
        assert 1 not in svc._active_executions

    def test_cancel_not_found(self):
        svc = DataAcquisitionService()
        assert svc.cancel_execution(999) is False


class TestCallAkshareFunction:
    @pytest.mark.asyncio
    async def test_function_not_found(self):
        svc = DataAcquisitionService()
        iface = MagicMock()
        iface.name = "nonexistent_function_xyz_abc"
        with pytest.raises(AttributeError):
            await svc._call_akshare_function(iface, {})

    @pytest.mark.asyncio
    async def test_function_returns_non_dataframe(self):
        svc = DataAcquisitionService()
        iface = MagicMock()
        iface.name = "test_func"
        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.test_func = MagicMock(return_value="not a dataframe")
            result = await svc._call_akshare_function(iface, {})
        assert result is None

    @pytest.mark.asyncio
    async def test_function_success(self):
        svc = DataAcquisitionService()
        iface = MagicMock()
        iface.name = "test_func"
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.test_func = MagicMock(return_value=df)
            result = await svc._call_akshare_function(iface, {})
        assert isinstance(result, pd.DataFrame)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_function_with_params(self):
        svc = DataAcquisitionService()
        iface = MagicMock()
        iface.name = "test_func"
        df = pd.DataFrame({"x": [1]})
        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.test_func = MagicMock(return_value=df)
            result = await svc._call_akshare_function(
                iface, {"symbol": "000001", "empty_param": None}
            )
        assert isinstance(result, pd.DataFrame)
        mock_ak.test_func.assert_called_once_with(symbol="000001")

    @pytest.mark.asyncio
    async def test_function_raises(self):
        svc = DataAcquisitionService()
        iface = MagicMock()
        iface.name = "test_func"
        with patch("opendata.services.data_acquisition.ak") as mock_ak:
            mock_ak.test_func = MagicMock(side_effect=RuntimeError("network error"))
            with pytest.raises(RuntimeError):
                await svc._call_akshare_function(iface, {})


class TestExecuteDownload:
    @pytest.mark.asyncio
    async def test_interface_not_found(self):
        svc = DataAcquisitionService()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        with pytest.raises(ValueError, match="Interface.*not found"):
            await svc.execute_download(1, 999, {}, mock_db)

    @pytest.mark.asyncio
    async def test_execution_not_found(self):
        svc = DataAcquisitionService()
        mock_db = AsyncMock()
        mock_iface = MagicMock()
        mock_result1 = MagicMock()
        mock_result1.scalar_one_or_none.return_value = mock_iface
        mock_result2 = MagicMock()
        mock_result2.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [mock_result1, mock_result2]
        with pytest.raises(ValueError, match="Execution.*not found"):
            await svc.execute_download(999, 1, {}, mock_db)


class TestInsertData:
    @pytest.mark.asyncio
    async def test_empty_dataframe(self):
        svc = DataAcquisitionService()
        mock_db = AsyncMock()
        result = await svc._insert_data("test_tbl", pd.DataFrame(), mock_db)
        assert result == 0
