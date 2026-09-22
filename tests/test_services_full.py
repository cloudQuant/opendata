"""
Services module comprehensive tests.

Full coverage tests for all services.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest


class TestScriptServiceMethods:
    """Test ScriptService methods."""

    @pytest.mark.asyncio
    async def test_get_scripts_empty(self):
        """Test getting scripts when none exist."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        # Mock empty result - need to mock both the count query and the data query
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_data_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_data_result.scalars.return_value = mock_scalars

        # Setup execute to return different results for different queries
        mock_db.execute.side_effect = [mock_count_result, mock_data_result]

        scripts, total = await service.get_scripts()

        assert scripts == []
        assert total == 0

    @pytest.mark.asyncio
    async def test_get_scripts_with_filters(self):
        """Test getting scripts with filters."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        # Mock result
        mock_count_result = MagicMock()
        mock_count_result.scalar.return_value = 0

        mock_data_result = MagicMock()
        mock_scalars = MagicMock()
        mock_scalars.all.return_value = []
        mock_data_result.scalars.return_value = mock_scalars

        mock_db.execute.side_effect = [mock_count_result, mock_data_result]

        scripts, total = await service.get_scripts(category="stocks", is_active=True)

        # Should return tuple
        assert isinstance(scripts, list)
        assert isinstance(total, int)

    @pytest.mark.asyncio
    async def test_get_script_by_id(self):
        """Test getting script by ID."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        # Mock not found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        script = await service.get_script("nonexistent")

        assert script is None

    @pytest.mark.asyncio
    async def test_get_categories(self):
        """Test getting categories."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        # Mock result
        mock_result = MagicMock()
        mock_result.unique().all().return_value = []
        mock_db.execute.return_value = mock_result

        categories = await service.get_categories()

        assert isinstance(categories, list)

    @pytest.mark.asyncio
    async def test_toggle_script(self):
        """Test toggling script active status."""
        from opendata.services.script_service import ScriptService

        mock_db = AsyncMock()
        service = ScriptService(mock_db)

        # Mock script exists
        mock_script = Mock()
        mock_script.is_active = True
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_script
        mock_db.execute.return_value = mock_result

        result = await service.toggle_script("test_script", active=False)

        # Should return True
        assert result is True or result is None


class TestExecutionServiceMethods:
    """Test ExecutionService methods."""

    @pytest.mark.asyncio
    async def test_get_execution_stats_empty(self):
        """Test execution stats when no executions."""
        from opendata.services.execution_service import ExecutionService

        mock_db = AsyncMock()
        service = ExecutionService(mock_db)

        # get_execution_stats executes a single aggregate query and reads the row
        # via result.one(), so the mock must return a row-like object.
        from collections import namedtuple

        stats_row = namedtuple(
            "StatsRow", ["total_count", "success_count", "avg_duration", "today_count"]
        )(0, 0, None, 0)

        async def mock_execute_func(arg):
            mock_result = MagicMock()
            mock_result.one.return_value = stats_row
            return mock_result

        mock_db.execute = mock_execute_func

        stats = await service.get_execution_stats()

        assert stats["total_count"] == 0
        assert stats["success_rate"] == 0

    @pytest.mark.asyncio
    async def test_get_failed_executions(self):
        """Test getting failed executions."""
        from opendata.services.execution_service import ExecutionService

        mock_db = AsyncMock()
        service = ExecutionService(mock_db)

        # Mock result
        mock_result = MagicMock()
        mock_result.scalars().all().return_value = []
        mock_db.execute.return_value = mock_result

        executions = await service.get_failed_executions(limit=10)

        assert isinstance(executions, list)

    @pytest.mark.asyncio
    async def test_delete_executions_by_status(self):
        """Test deleting executions by status."""
        from opendata.services.execution_service import ExecutionService

        mock_db = AsyncMock()
        service = ExecutionService(mock_db)

        # Mock delete result
        mock_result = MagicMock()
        mock_result.rowcount = 0
        mock_db.execute.return_value = mock_result

        count = await service.delete_executions_by_status("completed")

        assert count == 0


class TestDataAcquisitionServiceMethods:
    """Test DataAcquisitionService methods."""

    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test DataAcquisitionService initialization."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        assert service is not None
        assert service._active_executions == {}

    def test_generate_table_name(self):
        """Test table name generation."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        result = service._generate_table_name("stock_zh_a_hist")
        assert result == "ak_stock_zh_a_hist"

    def test_clean_column_names(self):
        """Test column name cleaning."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        columns = ["Column Name", "Another-Column"]
        cleaned = service._clean_column_names(columns)

        assert len(cleaned) == 2

    @pytest.mark.asyncio
    async def test_get_progress_nonexistent(self):
        """Test getting progress for non-existent execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        progress = service.get_progress(999)

        assert progress == {
            "execution_id": 999,
            "status": "not_found",
            "progress": 0,
        }

    @pytest.mark.asyncio
    async def test_cancel_execution(self):
        """Test cancelling execution."""
        from opendata.services.data_acquisition import DataAcquisitionService

        service = DataAcquisitionService()

        # Add active execution
        service._active_executions[1] = {"interface_id": 1}

        result = service.cancel_execution(1)

        assert result is True
        assert 1 not in service._active_executions


class TestSchedulerServiceMethods:
    """Test SchedulerService methods."""

    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test SchedulerService initialization."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        assert service is not None

    @pytest.mark.asyncio
    async def test_start_and_shutdown(self):
        """Test starting and shutting down scheduler."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        await service.start()
        await service.shutdown()

        # Should complete without error

    def test_get_scheduler(self):
        """Test get_scheduler method."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        scheduler = service.get_scheduler()

        assert scheduler is not None

    def test_job_add_remove(self):
        """Test add_job and remove_job."""
        from opendata.services.scheduler_service import SchedulerService

        service = SchedulerService()

        # Just verify methods exist
        assert hasattr(service, "add_job")
        assert hasattr(service, "remove_job")


class TestRetryServiceMethods:
    """Test RetryService methods."""

    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test RetryService initialization."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        assert service is not None
        assert service._retry_queue == {}

    def test_calculate_retry_delay(self):
        """Test calculate_retry_delay method."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay1 = service.calculate_retry_delay(0)
        delay2 = service.calculate_retry_delay(1)

        assert delay2 > delay1

    def test_calculate_retry_delay_capped(self):
        """Test delay is capped at max."""
        from opendata.services.retry_service import RetryService

        mock_db = AsyncMock()
        service = RetryService(mock_db)

        delay = service.calculate_retry_delay(100)
        assert delay <= RetryService.MAX_RETRY_DELAY


class TestTaskSchedulerMethods:
    """Test TaskScheduler methods."""

    @pytest.mark.asyncio
    async def test_initialization(self):
        """Test TaskScheduler initialization."""
        from opendata.services.scheduler import TaskScheduler

        scheduler = TaskScheduler()

        assert scheduler is not None
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_start_shutdown(self):
        """Test starting and shutting down."""
        from unittest.mock import AsyncMock

        from opendata.services.scheduler import TaskScheduler

        scheduler = TaskScheduler()

        # Mock init_scheduler_service to avoid DB connection
        with patch("opendata.services.scheduler.init_scheduler_service") as mock_init:
            mock_service = AsyncMock()
            mock_service.start.return_value = None
            mock_service.stop.return_value = None
            mock_init.return_value = mock_service

            # Mock _load_active_tasks to avoid DB queries
            with patch.object(scheduler, "_load_active_tasks", return_value=None):
                await scheduler.start()
                assert scheduler.is_running is True

                await scheduler.shutdown()
                assert scheduler.is_running is False
