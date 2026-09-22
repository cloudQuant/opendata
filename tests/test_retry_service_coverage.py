"""
Tests for RetryService covering schedule_retry, execute_retry (lines 104, 129-216).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.models.task import TaskStatus
from opendata.services.retry_service import RetryService


@pytest.fixture
def mock_db():
    return AsyncMock()


@pytest.fixture
def svc(mock_db):
    return RetryService(mock_db)


class TestScheduleRetry:
    @pytest.mark.asyncio
    async def test_should_not_retry(self, svc):
        execution = MagicMock()
        execution.retry_count = 3
        execution.execution_id = "e1"
        task = MagicMock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await svc.schedule_retry(execution, task)
        assert result is False

    @pytest.mark.asyncio
    async def test_schedule_success(self, svc):
        execution = MagicMock()
        execution.execution_id = "e1"
        execution.retry_count = 0
        execution.status = TaskStatus.FAILED

        task = MagicMock()
        task.retry_on_failure = True
        task.max_retries = 3
        task.id = 1

        result = await svc.schedule_retry(execution, task)
        assert result is True
        assert "e1" in svc._retry_queue


class TestExecuteRetry:
    @pytest.mark.asyncio
    async def test_success(self, svc, mock_db):
        """Mock the inner imports since DataService module doesn't exist."""
        original_exec = MagicMock()
        original_exec.execution_id = "orig1"
        original_exec.triggered_by = "scheduler"
        original_exec.operator_id = None

        task = MagicMock()
        task.id = 1
        task.script_id = "s1"
        task.parameters = {"key": "val"}
        task.timeout = 30

        new_exec = MagicMock()
        new_exec.execution_id = "new1"
        new_exec.retry_count = 0

        mock_es_cls = MagicMock()
        mock_es_cls.return_value.create_execution = AsyncMock(return_value=new_exec)
        mock_es_cls.return_value.update_execution = AsyncMock()

        mock_ds_cls = MagicMock()
        mock_ds_cls.return_value.execute_script = AsyncMock(return_value={"rows_processed": 10})

        # Create a fake data_service module
        import types

        fake_ds = types.ModuleType("opendata.services.data_service")
        fake_ds.DataService = mock_ds_cls

        import sys

        with (
            patch.dict(sys.modules, {"opendata.services.data_service": fake_ds}),
            patch("opendata.services.execution_service.ExecutionService", mock_es_cls),
        ):
            result = await svc.execute_retry(original_exec, task, 1)
        assert result is True

    @pytest.mark.asyncio
    async def test_failure_with_retry(self, svc, mock_db):
        original_exec = MagicMock()
        original_exec.execution_id = "orig1"
        original_exec.triggered_by = "scheduler"
        original_exec.operator_id = None

        task = MagicMock()
        task.id = 1
        task.script_id = "s1"
        task.parameters = {}
        task.timeout = 30

        new_exec = MagicMock()
        new_exec.execution_id = "new1"
        new_exec.retry_count = 1

        mock_es_cls = MagicMock()
        mock_es_cls.return_value.create_execution = AsyncMock(return_value=new_exec)
        mock_es_cls.return_value.update_execution = AsyncMock()

        mock_ds_cls = MagicMock()
        mock_ds_cls.return_value.execute_script = AsyncMock(side_effect=RuntimeError("fail"))

        import sys
        import types

        fake_ds = types.ModuleType("opendata.services.data_service")
        fake_ds.DataService = mock_ds_cls

        with (
            patch.dict(sys.modules, {"opendata.services.data_service": fake_ds}),
            patch("opendata.services.execution_service.ExecutionService", mock_es_cls),
            patch.object(svc, "should_retry", new_callable=AsyncMock, return_value=False),
        ):
            result = await svc.execute_retry(original_exec, task, 1)
        assert result is False

    @pytest.mark.asyncio
    async def test_outer_exception(self, svc, mock_db):
        original_exec = MagicMock()
        original_exec.execution_id = "orig1"
        original_exec.triggered_by = "scheduler"
        original_exec.operator_id = None

        task = MagicMock()
        task.id = 1
        task.script_id = "s1"
        task.parameters = {}
        task.timeout = 30

        mock_es_cls = MagicMock()
        mock_es_cls.return_value.create_execution = AsyncMock(side_effect=RuntimeError("db error"))

        with patch("opendata.services.execution_service.ExecutionService", mock_es_cls):
            result = await svc.execute_retry(original_exec, task, 1)
        assert result is False
