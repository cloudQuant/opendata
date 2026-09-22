"""
Retry service full coverage tests.

Complete tests for retry_service module to achieve 100% coverage.
"""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest


class TestRetryServiceShouldRetry:
    """Test should_retry method."""

    @pytest.mark.asyncio
    async def test_should_retry_with_enabled_retry(self):
        """Test should retry when retry is enabled."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 1
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is True

    @pytest.mark.asyncio
    async def test_should_retry_disabled(self):
        """Test should not retry when disabled."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 1
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = False
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is False

    @pytest.mark.asyncio
    async def test_should_retry_max_exceeded(self):
        """Test should not retry when max retries exceeded."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 5
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is False

    @pytest.mark.asyncio
    async def test_should_retry_not_failed(self):
        """Test should not retry when not failed."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 1
        execution.status = TaskStatus.COMPLETED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is False


class TestRetryServiceScheduleRetry:
    """Test schedule_retry method."""

    @pytest.mark.asyncio
    async def test_schedule_retry_success(self):
        """Test successful retry scheduling."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.execution_id = "exec_123"
        execution.retry_count = 1
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3
        task.id = 1

        result = await service.schedule_retry(execution, task)

        # Should schedule retry
        assert result is True

    @pytest.mark.asyncio
    async def test_schedule_retry_should_not_retry(self):
        """Test schedule retry when should not retry."""
        from opendata.models.task import TaskExecution, TaskStatus
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.execution_id = "exec_123"
        execution.retry_count = 5
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3
        task.id = 1

        result = await service.schedule_retry(execution, task)

        # Should not schedule retry
        assert result is False


class TestRetryServiceCalculateDelay:
    """Test calculate_retry_delay method."""

    def test_calculate_retry_delay_exponential(self):
        """Test exponential backoff calculation."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay1 = service.calculate_retry_delay(0)
        delay2 = service.calculate_retry_delay(1)
        delay3 = service.calculate_retry_delay(2)

        assert delay2 > delay1
        assert delay3 > delay2

    def test_calculate_retry_delay_capped(self):
        """Test delay is capped at MAX."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(100)
        assert delay <= RetryService.MAX_RETRY_DELAY


class TestRetryServiceGetRetryStatus:
    """Test get_retry_status method."""

    @pytest.mark.asyncio
    async def test_get_retry_status_not_found(self):
        """Test getting status for non-existent execution."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        result = await service.get_retry_status("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_retry_status_pending(self):
        """Test getting status for pending retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        stop_event = asyncio.Event()

        async def long_task():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                raise

        task = asyncio.create_task(long_task())
        await asyncio.sleep(0.01)
        service._retry_queue["exec_123"] = task

        try:
            result = await service.get_retry_status("exec_123")
            assert result is not None
            assert result["execution_id"] == "exec_123"
            assert result["status"] == "pending"
        finally:
            stop_event.set()
            try:
                task.cancel()
                await asyncio.wait_for(task, timeout=0.1)
            except (Exception, asyncio.CancelledError):
                pass


class TestRetryServiceGetAllPending:
    """Test get_all_pending_retries method."""

    @pytest.mark.asyncio
    async def test_get_all_pending_empty(self):
        """Test getting pending when queue is empty."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        result = await service.get_all_pending_retries()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_pending_with_tasks(self):
        """Test getting pending retries with tasks."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        stop_event = asyncio.Event()

        async def dummy_task():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except (TimeoutError, asyncio.CancelledError):
                raise

        task1 = asyncio.create_task(dummy_task())
        task2 = asyncio.create_task(dummy_task())
        await asyncio.sleep(0.01)

        service._retry_queue["exec_1"] = task1
        service._retry_queue["exec_2"] = task2

        try:
            result = await service.get_all_pending_retries()
            assert len(result) == 2
            assert "exec_1" in result
            assert "exec_2" in result
        finally:
            stop_event.set()
            try:
                task1.cancel()
                task2.cancel()
                await asyncio.gather(task1, task2, return_exceptions=True)
            except Exception:
                pass


class TestRetryServiceCancelPending:
    """Test cancel_pending_retries method."""

    @pytest.mark.asyncio
    async def test_cancel_nonexistent(self):
        """Test cancelling non-existent execution."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        result = await service.cancel_pending_retries("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_pending(self):
        """Test cancelling pending retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        stop_event = asyncio.Event()

        async def dummy_task():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                raise

        task = asyncio.create_task(dummy_task())
        await asyncio.sleep(0.01)
        service._retry_queue["exec_123"] = task

        try:
            result = await service.cancel_pending_retries("exec_123")
            assert result is True
            assert "exec_123" not in service._retry_queue
        finally:
            stop_event.set()
            try:
                if not task.done():
                    task.cancel()
                    await asyncio.wait_for(task, timeout=0.1)
            except (Exception, asyncio.CancelledError):
                pass


class TestRetryServiceCleanup:
    """Test cleanup_completed_retries method."""

    @pytest.mark.asyncio
    async def test_cleanup_empty(self):
        """Test cleanup when queue is empty."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        count = await service.cleanup_completed_retries()
        assert count == 0

    @pytest.mark.asyncio
    async def test_cleanup_with_completed(self):
        """Test cleanup removes completed tasks."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        async def completed_task():
            pass

        task = asyncio.create_task(completed_task())
        await task
        service._retry_queue["exec_1"] = task

        count = await service.cleanup_completed_retries()
        assert count == 1
        assert "exec_1" not in service._retry_queue
