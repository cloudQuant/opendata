"""
Direct tests for TaskScheduler to maximize coverage.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.models.task import ScheduledTask, ScheduleType
from opendata.services.scheduler import TaskScheduler


class TestTaskSchedulerInit:
    def test_init(self):
        ts = TaskScheduler()
        assert ts._running is False
        assert ts.scheduler_service is None

    def test_is_running_false(self):
        ts = TaskScheduler()
        assert ts.is_running is False

    def test_is_running_true(self):
        ts = TaskScheduler()
        ts._running = True
        ts.scheduler_service = MagicMock()
        assert ts.is_running is True


class TestGetTriggerConfig:
    def _make_task(self, stype, expr):
        t = MagicMock(spec=ScheduledTask)
        t.schedule_type = stype
        t.schedule_expression = expr
        return t

    def test_cron(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.CRON, "0 8 * * *")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "cron"
        assert args["cron_expression"] == "0 8 * * *"

    def test_daily_hhmm(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.DAILY, "08:30")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "cron"
        assert args["cron_expression"] == "30 08 * * *"

    def test_daily_raw(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.DAILY, "0 8 * * *")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "cron"

    def test_weekly(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.WEEKLY, "0 8 * * 1")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "cron"

    def test_monthly(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.MONTHLY, "0 0 1 * *")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "cron"

    def test_interval(self):
        ts = TaskScheduler()
        t = self._make_task(ScheduleType.INTERVAL, "5m")
        trigger_type, args = ts._get_trigger_config(t)
        assert trigger_type == "interval"
        assert args["minutes"] == 5


class TestParseInterval:
    def test_seconds(self):
        ts = TaskScheduler()
        assert ts._parse_interval("30s") == {"seconds": 30}

    def test_minutes(self):
        ts = TaskScheduler()
        assert ts._parse_interval("5m") == {"minutes": 5}

    def test_hours(self):
        ts = TaskScheduler()
        assert ts._parse_interval("2h") == {"hours": 2}

    def test_days(self):
        ts = TaskScheduler()
        assert ts._parse_interval("1d") == {"days": 1}

    def test_default_minutes(self):
        ts = TaskScheduler()
        assert ts._parse_interval("10") == {"minutes": 10}


class TestStartShutdown:
    @pytest.mark.asyncio
    async def test_start(self):
        ts = TaskScheduler()
        mock_svc = AsyncMock()
        with (
            patch("opendata.services.scheduler.init_scheduler_service", return_value=mock_svc),
            patch.object(ts, "_load_active_tasks", new_callable=AsyncMock),
        ):
            await ts.start()
        assert ts._running is True
        assert ts.scheduler_service is mock_svc

    @pytest.mark.asyncio
    async def test_start_already_running(self):
        ts = TaskScheduler()
        ts._running = True
        await ts.start()  # Should just return

    @pytest.mark.asyncio
    async def test_shutdown(self):
        ts = TaskScheduler()
        ts._running = True
        ts.scheduler_service = AsyncMock()
        await ts.shutdown()
        assert ts._running is False
        assert ts.scheduler_service is None

    @pytest.mark.asyncio
    async def test_shutdown_not_running(self):
        ts = TaskScheduler()
        await ts.shutdown()  # Should just return


class TestRemoveTask:
    @pytest.mark.asyncio
    async def test_remove_no_service(self):
        ts = TaskScheduler()
        ts.scheduler_service = None
        await ts.remove_task(1)  # Should just return

    @pytest.mark.asyncio
    async def test_remove_success(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        await ts.remove_task(1)
        ts.scheduler_service.remove_job.assert_called_once_with("task_1")

    @pytest.mark.asyncio
    async def test_remove_failure(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        ts.scheduler_service.remove_job.side_effect = Exception("not found")
        await ts.remove_task(1)  # Should not raise


class TestScheduleTask:
    @pytest.mark.asyncio
    async def test_schedule_no_service(self):
        ts = TaskScheduler()
        ts.scheduler_service = None
        task = MagicMock(spec=ScheduledTask)
        await ts._schedule_task(task, None)  # Should just return

    @pytest.mark.asyncio
    async def test_schedule_success(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        task = MagicMock(spec=ScheduledTask)
        task.id = 1
        task.name = "T1"
        task.schedule_type = ScheduleType.CRON
        task.schedule_expression = "0 8 * * *"
        await ts._schedule_task(task, None)
        ts.scheduler_service.add_job.assert_called_once()


class TestExecuteTaskWrapper:
    @pytest.mark.asyncio
    async def test_task_not_found(self):
        ts = TaskScheduler()
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_maker:
            mock_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)
            await ts._execute_task_wrapper(task_id=999)

    @pytest.mark.asyncio
    async def test_task_inactive(self):
        ts = TaskScheduler()
        mock_task = MagicMock()
        mock_task.is_active = False
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_maker:
            mock_maker.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_maker.return_value.__aexit__ = AsyncMock(return_value=False)
            await ts._execute_task_wrapper(task_id=1)


class TestAddUpdateTask:
    @pytest.mark.asyncio
    async def test_add_not_found(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        await ts.add_task(999, mock_db)

    @pytest.mark.asyncio
    async def test_add_found(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        mock_task = MagicMock(spec=ScheduledTask)
        mock_task.id = 1
        mock_task.name = "T"
        mock_task.schedule_type = ScheduleType.CRON
        mock_task.schedule_expression = "0 8 * * *"
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_db.execute.return_value = mock_result
        await ts.add_task(1, mock_db)
        ts.scheduler_service.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_update(self):
        ts = TaskScheduler()
        ts.scheduler_service = AsyncMock()
        mock_task = MagicMock(spec=ScheduledTask)
        mock_task.id = 1
        mock_task.name = "T"
        mock_task.schedule_type = ScheduleType.CRON
        mock_task.schedule_expression = "0 8 * * *"
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_db.execute.return_value = mock_result
        await ts.update_task(1, mock_db)
