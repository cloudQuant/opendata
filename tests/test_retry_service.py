"""
Retry service tests.

Tests for the retry mechanism with exponential backoff.
"""

import asyncio
import contextlib
from unittest.mock import AsyncMock, Mock

import pytest

from opendata.models.task import TaskExecution, TaskStatus


class TestRetryService:
    """Test RetryService class."""

    def test_init(self):
        """Test RetryService initialization."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        assert service.db == mock_db
        assert service._retry_queue == {}

    def test_base_retry_delay_constant(self):
        """Test BASE_RETRY_DELAY constant."""
        from opendata.services.retry_service import RetryService

        assert RetryService.BASE_RETRY_DELAY == 60

    def test_max_retry_delay_constant(self):
        """Test MAX_RETRY_DELAY constant."""
        from opendata.services.retry_service import RetryService

        assert RetryService.MAX_RETRY_DELAY == 3600


class TestCalculateRetryDelay:
    """Test retry delay calculation."""

    def test_calculate_retry_delay_zero_attempts(self):
        """Test delay calculation for first retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(0)
        assert delay == 60

    def test_calculate_retry_delay_one_attempt(self):
        """Test delay calculation for second retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(1)
        assert delay == 120  # 60 * 2^1

    def test_calculate_retry_delay_two_attempts(self):
        """Test delay calculation for third retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(2)
        assert delay == 240  # 60 * 2^2

    def test_calculate_retry_delay_three_attempts(self):
        """Test delay calculation for fourth retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(3)
        assert delay == 480  # 60 * 2^3

    def test_calculate_retry_delay_max_cap(self):
        """Test that delay is capped at MAX_RETRY_DELAY."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Should be capped at 3600
        delay = service.calculate_retry_delay(10)
        assert delay == 3600

    def test_calculate_retry_delay_exponential_growth(self):
        """Test that delay grows exponentially."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delays = [service.calculate_retry_delay(i) for i in range(5)]
        expected = [60, 120, 240, 480, 960]

        assert delays == expected


class TestShouldRetry:
    """Test should_retry logic."""

    @pytest.mark.asyncio
    async def test_should_retry_with_retry_enabled(self):
        """Test should retry when retry is enabled."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Create mock execution with proper model
        execution = TaskExecution()
        execution.retry_count = 1
        execution.status = TaskStatus.FAILED

        # Create mock task
        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is True

    @pytest.mark.asyncio
    async def test_should_not_retry_when_disabled(self):
        """Test should not retry when retry is disabled."""
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
    async def test_should_not_retry_when_max_retries_exceeded(self):
        """Test should not retry when max retries exceeded."""
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
    async def test_should_not_retry_when_not_failed(self):
        """Test should not retry when execution is not failed."""
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

    @pytest.mark.asyncio
    async def test_should_retry_boundary_case(self):
        """Test boundary case where retry_count equals max_retries."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 3
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is False

    @pytest.mark.asyncio
    async def test_should_retry_one_less_than_max(self):
        """Test retry when one less than max retries."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        execution = TaskExecution()
        execution.retry_count = 2
        execution.status = TaskStatus.FAILED

        task = Mock()
        task.retry_on_failure = True
        task.max_retries = 3

        result = await service.should_retry(execution, task)
        assert result is True


class TestCancelPendingRetries:
    """Test cancelling pending retries."""

    @pytest.mark.asyncio
    async def test_cancel_pending_retries_none_pending(self):
        """Test cancelling when no retries pending."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        result = await service.cancel_pending_retries("nonexistent_id")
        assert result is False

    @pytest.mark.asyncio
    async def test_cancel_pending_retries_with_pending(self):
        """Test cancelling a pending retry."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Create a mock task that waits
        stop_event = asyncio.Event()

        async def dummy_task():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                raise

        task = asyncio.create_task(dummy_task())
        # Give it a moment to start
        await asyncio.sleep(0.01)
        service._retry_queue["exec_123"] = task

        try:
            result = await service.cancel_pending_retries("exec_123")
            assert result is True
            assert "exec_123" not in service._retry_queue
        finally:
            # Cleanup
            stop_event.set()
            try:
                if not task.done():
                    task.cancel()
                    await asyncio.wait_for(task, timeout=0.1)
            except (Exception, asyncio.CancelledError):
                pass


class TestGetRetryStatus:
    """Test getting retry status."""

    @pytest.mark.asyncio
    async def test_get_retry_status_none(self):
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

        # Create a long-running task with event for cleanup
        stop_event = asyncio.Event()
        task_complete = False

        async def long_task():
            nonlocal task_complete
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=10)
            except (TimeoutError, asyncio.CancelledError):
                raise
            finally:
                task_complete = True

        task = asyncio.create_task(long_task())
        # Give it moment to start
        await asyncio.sleep(0.01)
        service._retry_queue["exec_123"] = task

        try:
            result = await service.get_retry_status("exec_123")
            assert result is not None
            assert result["execution_id"] == "exec_123"
            assert result["status"] == "pending"
        finally:
            # Cleanup
            stop_event.set()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                task.cancel()
                await asyncio.wait_for(task, timeout=0.1)


class TestGetAllPendingRetries:
    """Test getting all pending retries."""

    @pytest.mark.asyncio
    async def test_get_all_pending_retries_empty(self):
        """Test getting pending retries when queue is empty."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        result = await service.get_all_pending_retries()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_all_pending_retries_with_tasks(self):
        """Test getting pending retries with tasks."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Add some tasks with stop events
        stop_event = asyncio.Event()

        async def dummy_task():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=1)
            except (TimeoutError, asyncio.CancelledError):
                raise

        task1 = asyncio.create_task(dummy_task())
        task2 = asyncio.create_task(dummy_task())

        # Give them a moment to start
        await asyncio.sleep(0.01)

        service._retry_queue["exec_1"] = task1
        service._retry_queue["exec_2"] = task2

        try:
            result = await service.get_all_pending_retries()
            # Returns list of execution IDs (strings)
            assert len(result) == 2
            assert "exec_1" in result
            assert "exec_2" in result
        finally:
            # Cleanup
            stop_event.set()
            try:
                task1.cancel()
                task2.cancel()
                await asyncio.gather(task1, task2, return_exceptions=True)
            except (Exception, asyncio.CancelledError):
                pass


class TestCleanupCompletedRetries:
    """Test cleanup of completed retries."""

    @pytest.mark.asyncio
    async def test_cleanup_empty_queue(self):
        """Test cleanup when queue is empty."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        count = await service.cleanup_completed_retries()
        assert count == 0

    @pytest.mark.asyncio
    async def test_cleanup_with_completed_tasks(self):
        """Test cleanup removes completed tasks."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Add completed task - create and immediately wait for it
        async def completed_task():
            pass

        task = asyncio.create_task(completed_task())
        # Wait for completion
        await task
        service._retry_queue["exec_1"] = task

        count = await service.cleanup_completed_retries()
        assert count == 1
        assert "exec_1" not in service._retry_queue


class TestRetryServiceIntegration:
    """Integration tests for retry service."""

    def test_service_constants(self):
        """Test all service constants."""
        from opendata.services.retry_service import RetryService

        assert hasattr(RetryService, "BASE_RETRY_DELAY")
        assert hasattr(RetryService, "MAX_RETRY_DELAY")
        assert RetryService.BASE_RETRY_DELAY < RetryService.MAX_RETRY_DELAY

    def test_service_methods_exist(self):
        """Test all expected methods exist."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        # Check all methods exist
        assert hasattr(service, "should_retry")
        assert hasattr(service, "calculate_retry_delay")
        assert hasattr(service, "schedule_retry")
        assert hasattr(service, "execute_retry")
        assert hasattr(service, "cancel_pending_retries")
        assert hasattr(service, "get_retry_status")
        assert hasattr(service, "get_all_pending_retries")
        assert hasattr(service, "cleanup_completed_retries")
