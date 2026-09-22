"""
Direct tests for RetryService to maximize coverage.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from opendata.models.task import TaskStatus
from opendata.services.retry_service import RetryService


def _mock_execution(eid="e1", retry_count=0, status=TaskStatus.FAILED):
    e = MagicMock()
    e.execution_id = eid
    e.retry_count = retry_count
    e.status = status
    e.triggered_by = "scheduler"
    e.operator_id = None
    return e


def _mock_task(retry=True, max_retries=3):
    t = MagicMock()
    t.id = 1
    t.script_id = "s1"
    t.parameters = {}
    t.timeout = 300
    t.retry_on_failure = retry
    t.max_retries = max_retries
    return t


class TestShouldRetry:
    @pytest.mark.asyncio
    async def test_should_retry_true(self):
        db = AsyncMock()
        svc = RetryService(db)
        assert await svc.should_retry(_mock_execution(), _mock_task()) is True

    @pytest.mark.asyncio
    async def test_no_retry_disabled(self):
        db = AsyncMock()
        svc = RetryService(db)
        assert await svc.should_retry(_mock_execution(), _mock_task(retry=False)) is False

    @pytest.mark.asyncio
    async def test_no_retry_max_reached(self):
        db = AsyncMock()
        svc = RetryService(db)
        assert (
            await svc.should_retry(_mock_execution(retry_count=3), _mock_task(max_retries=3))
            is False
        )

    @pytest.mark.asyncio
    async def test_no_retry_not_failed(self):
        db = AsyncMock()
        svc = RetryService(db)
        assert (
            await svc.should_retry(_mock_execution(status=TaskStatus.COMPLETED), _mock_task())
            is False
        )


class TestCalculateRetryDelay:
    def test_first_retry(self):
        svc = RetryService(AsyncMock())
        assert svc.calculate_retry_delay(0) == 60

    def test_second_retry(self):
        svc = RetryService(AsyncMock())
        assert svc.calculate_retry_delay(1) == 120

    def test_max_cap(self):
        svc = RetryService(AsyncMock())
        assert svc.calculate_retry_delay(20) == 3600  # capped


class TestCancelPendingRetries:
    @pytest.mark.asyncio
    async def test_cancel_active(self):
        svc = RetryService(AsyncMock())
        mock_task = MagicMock()
        mock_task.done.return_value = False
        svc._retry_queue["e1"] = mock_task
        result = await svc.cancel_pending_retries("e1")
        assert result is True
        mock_task.cancel.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancel_not_found(self):
        svc = RetryService(AsyncMock())
        result = await svc.cancel_pending_retries("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_already_done(self):
        svc = RetryService(AsyncMock())
        mock_task = MagicMock()
        mock_task.done.return_value = True
        svc._retry_queue["e1"] = mock_task
        result = await svc.cancel_pending_retries("e1")
        assert result is False


class TestGetRetryStatus:
    @pytest.mark.asyncio
    async def test_pending(self):
        svc = RetryService(AsyncMock())
        mock_task = MagicMock()
        mock_task.done.return_value = False
        svc._retry_queue["e1"] = mock_task
        result = await svc.get_retry_status("e1")
        assert result["status"] == "pending"

    @pytest.mark.asyncio
    async def test_completed(self):
        svc = RetryService(AsyncMock())
        mock_task = MagicMock()
        mock_task.done.return_value = True
        mock_task.cancelled.return_value = False
        svc._retry_queue["e1"] = mock_task
        result = await svc.get_retry_status("e1")
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_not_found(self):
        svc = RetryService(AsyncMock())
        result = await svc.get_retry_status("nonexistent")
        assert result is None


class TestGetAllPendingRetries:
    @pytest.mark.asyncio
    async def test_mixed(self):
        svc = RetryService(AsyncMock())
        pending = MagicMock()
        pending.done.return_value = False
        done = MagicMock()
        done.done.return_value = True
        svc._retry_queue = {"e1": pending, "e2": done}
        result = await svc.get_all_pending_retries()
        assert result == ["e1"]


class TestCleanupCompletedRetries:
    @pytest.mark.asyncio
    async def test_cleanup(self):
        svc = RetryService(AsyncMock())
        pending = MagicMock()
        pending.done.return_value = False
        done = MagicMock()
        done.done.return_value = True
        svc._retry_queue = {"e1": pending, "e2": done}
        removed = await svc.cleanup_completed_retries()
        assert removed == 1
        assert "e1" in svc._retry_queue
        assert "e2" not in svc._retry_queue


class TestScheduleRetry:
    @pytest.mark.asyncio
    async def test_schedule_not_eligible(self):
        svc = RetryService(AsyncMock())
        result = await svc.schedule_retry(
            _mock_execution(status=TaskStatus.COMPLETED), _mock_task()
        )
        assert result is False

    @pytest.mark.asyncio
    async def test_schedule_eligible(self):
        svc = RetryService(AsyncMock())
        execution = _mock_execution()
        task = _mock_task()
        result = await svc.schedule_retry(execution, task)
        assert result is True
        assert execution.execution_id in svc._retry_queue
