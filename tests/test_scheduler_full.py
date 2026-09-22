"""
Comprehensive tests for scheduler service.

Covers TaskScheduler start, shutdown, is_running, task management.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from opendata.services.scheduler import TaskScheduler


class TestTaskSchedulerInit:
    """Test TaskScheduler initialization."""

    def test_init(self):
        """Test scheduler initialization."""
        scheduler = TaskScheduler()
        assert scheduler._running is False
        assert scheduler.scheduler_service is None

    def test_is_running_false(self):
        """Test is_running when not started."""
        scheduler = TaskScheduler()
        assert scheduler.is_running is False

    def test_is_running_true(self):
        """Test is_running when started."""
        scheduler = TaskScheduler()
        scheduler._running = True
        scheduler.scheduler_service = MagicMock()
        assert scheduler.is_running is True

    def test_is_running_no_service(self):
        """Test is_running when service is None."""
        scheduler = TaskScheduler()
        scheduler._running = True
        scheduler.scheduler_service = None
        assert scheduler.is_running is False


class TestTaskSchedulerShutdown:
    """Test TaskScheduler shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_when_not_running(self):
        """Test shutdown when not running (no-op)."""
        scheduler = TaskScheduler()
        await scheduler.shutdown()
        assert scheduler._running is False

    @pytest.mark.asyncio
    async def test_shutdown_when_running(self):
        """Test shutdown when running."""
        scheduler = TaskScheduler()
        scheduler._running = True
        mock_service = AsyncMock()
        scheduler.scheduler_service = mock_service

        await scheduler.shutdown()

        mock_service.shutdown.assert_called_once()
        assert scheduler._running is False
        assert scheduler.scheduler_service is None


class TestTaskSchedulerGetTriggerConfig:
    """Test trigger configuration."""

    def test_get_trigger_config_cron(self):
        """Test getting cron trigger config."""
        from opendata.models.task import ScheduleType

        scheduler = TaskScheduler()
        task = MagicMock()
        task.schedule_type = ScheduleType.CRON
        task.schedule_expression = "0 8 * * *"

        trigger_type, trigger_args = scheduler._get_trigger_config(task)
        assert trigger_type == "cron"
        assert trigger_args["cron_expression"] == "0 8 * * *"

    def test_get_trigger_config_daily(self):
        """Test getting daily trigger config."""
        from opendata.models.task import ScheduleType

        scheduler = TaskScheduler()
        task = MagicMock()
        task.schedule_type = ScheduleType.DAILY
        task.schedule_expression = "08:30"

        trigger_type, trigger_args = scheduler._get_trigger_config(task)
        assert trigger_type == "cron"

    def test_get_trigger_config_weekly(self):
        """Test getting weekly trigger config."""
        from opendata.models.task import ScheduleType

        scheduler = TaskScheduler()
        task = MagicMock()
        task.schedule_type = ScheduleType.WEEKLY
        task.schedule_expression = "0 8 * * 1"

        trigger_type, trigger_args = scheduler._get_trigger_config(task)
        assert trigger_type == "cron"

    def test_get_trigger_config_monthly(self):
        """Test getting monthly trigger config."""
        from opendata.models.task import ScheduleType

        scheduler = TaskScheduler()
        task = MagicMock()
        task.schedule_type = ScheduleType.MONTHLY
        task.schedule_expression = "0 8 1 * *"

        trigger_type, trigger_args = scheduler._get_trigger_config(task)
        assert trigger_type == "cron"

    def test_get_trigger_config_interval(self):
        """Test getting interval trigger config."""
        from opendata.models.task import ScheduleType

        scheduler = TaskScheduler()
        task = MagicMock()
        task.schedule_type = ScheduleType.INTERVAL
        task.schedule_expression = "5m"

        trigger_type, trigger_args = scheduler._get_trigger_config(task)
        assert trigger_type == "interval"
        assert trigger_args["minutes"] == 5


class TestParseInterval:
    """Test interval parsing."""

    def test_seconds(self):
        scheduler = TaskScheduler()
        result = scheduler._parse_interval("30s")
        assert result == {"seconds": 30}

    def test_minutes(self):
        scheduler = TaskScheduler()
        result = scheduler._parse_interval("5m")
        assert result == {"minutes": 5}

    def test_hours(self):
        scheduler = TaskScheduler()
        result = scheduler._parse_interval("2h")
        assert result == {"hours": 2}

    def test_days(self):
        scheduler = TaskScheduler()
        result = scheduler._parse_interval("1d")
        assert result == {"days": 1}

    def test_default_minutes(self):
        scheduler = TaskScheduler()
        result = scheduler._parse_interval("10")
        assert result == {"minutes": 10}


class TestTaskSchedulerRemoveTask:
    """Test remove task."""

    @pytest.mark.asyncio
    async def test_remove_task_no_service(self):
        """Test removing task when service is None."""
        scheduler = TaskScheduler()
        scheduler.scheduler_service = None
        await scheduler.remove_task(1)  # Should not raise

    @pytest.mark.asyncio
    async def test_remove_task_success(self):
        """Test successfully removing task."""
        scheduler = TaskScheduler()
        mock_service = AsyncMock()
        scheduler.scheduler_service = mock_service
        await scheduler.remove_task(1)
        mock_service.remove_job.assert_called_once_with("task_1")

    @pytest.mark.asyncio
    async def test_remove_task_error(self):
        """Test removing task with error (should not raise)."""
        scheduler = TaskScheduler()
        mock_service = AsyncMock()
        mock_service.remove_job.side_effect = Exception("not found")
        scheduler.scheduler_service = mock_service
        await scheduler.remove_task(1)  # Should not raise
