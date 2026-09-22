"""Tests for DataService."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from opendata.services.data_service import DataService


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def svc(mock_db):
    return DataService(mock_db)


class TestExecuteScript:
    @pytest.mark.asyncio
    async def test_success_no_timeout(self, svc):
        with patch.object(
            svc._script_service, "execute_script", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = {"success": True, "result": {"rows": 10}}
            result = await svc.execute_script("test_script", {"key": "val"})
        assert result["success"] is True
        mock_exec.assert_called_once()

    @pytest.mark.asyncio
    async def test_success_with_timeout(self, svc):
        with patch.object(
            svc._script_service, "execute_script", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = {"success": True, "result": {}}
            result = await svc.execute_script("test_script", {}, timeout=30)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_script_failure(self, svc):
        with patch.object(
            svc._script_service, "execute_script", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = {"success": False, "error": "script error"}
            result = await svc.execute_script("bad_script")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_timeout_error(self, svc):
        async def slow_func(**kwargs):
            await asyncio.sleep(10)

        with patch.object(svc._script_service, "execute_script", side_effect=asyncio.TimeoutError):
            result = await svc.execute_script("slow_script", {}, timeout=1)
        assert result["success"] is False
        assert "timed out" in result["error"]

    @pytest.mark.asyncio
    async def test_unexpected_exception(self, svc):
        with patch.object(svc._script_service, "execute_script", side_effect=RuntimeError("boom")):
            result = await svc.execute_script("crash_script")
        assert result["success"] is False
        assert "boom" in result["error"]

    @pytest.mark.asyncio
    async def test_none_params(self, svc):
        with patch.object(
            svc._script_service, "execute_script", new_callable=AsyncMock
        ) as mock_exec:
            mock_exec.return_value = {"success": True}
            result = await svc.execute_script("s1", None, None)
        assert result["success"] is True
