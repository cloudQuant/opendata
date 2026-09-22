"""
Tests for TaskScheduler covering _load_active_tasks, _schedule_task,
_execute_task_wrapper, _execute_with_retry, add_task, trigger_task, etc.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.models.task import ScheduleType
from opendata.services.scheduler import TaskScheduler


@pytest.fixture
def scheduler():
    s = TaskScheduler()
    s.scheduler_service = MagicMock()
    s.scheduler_service.add_job = AsyncMock()
    s.scheduler_service.remove_job = AsyncMock()
    s._running = True
    return s


class TestGetTriggerConfig:
    def test_cron(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.CRON
        task.schedule_expression = "0 8 * * *"
        t, args = scheduler._get_trigger_config(task)
        assert t == "cron"
        assert args == {"cron_expression": "0 8 * * *"}

    def test_daily_hhmm(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.DAILY
        task.schedule_expression = "08:30"
        t, args = scheduler._get_trigger_config(task)
        assert t == "cron"
        assert "cron_expression" in args

    def test_daily_no_colon(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.DAILY
        task.schedule_expression = "0 8 * * *"
        t, args = scheduler._get_trigger_config(task)
        assert t == "cron"

    def test_weekly(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.WEEKLY
        task.schedule_expression = "0 8 * * 1"
        t, args = scheduler._get_trigger_config(task)
        assert t == "cron"

    def test_monthly(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.MONTHLY
        task.schedule_expression = "0 8 1 * *"
        t, args = scheduler._get_trigger_config(task)
        assert t == "cron"

    def test_interval(self, scheduler):
        task = MagicMock()
        task.schedule_type = ScheduleType.INTERVAL
        task.schedule_expression = "5m"
        t, args = scheduler._get_trigger_config(task)
        assert t == "interval"
        assert args == {"minutes": 5}

    def test_default_once(self, scheduler):
        task = MagicMock()
        task.schedule_type = "unknown"
        task.schedule_expression = ""
        t, args = scheduler._get_trigger_config(task)
        assert t == "once"


class TestParseInterval:
    def test_seconds(self, scheduler):
        assert scheduler._parse_interval("30s") == {"seconds": 30}

    def test_minutes(self, scheduler):
        assert scheduler._parse_interval("5m") == {"minutes": 5}

    def test_hours(self, scheduler):
        assert scheduler._parse_interval("2h") == {"hours": 2}

    def test_days(self, scheduler):
        assert scheduler._parse_interval("1d") == {"days": 1}

    def test_default_minutes(self, scheduler):
        assert scheduler._parse_interval("10") == {"minutes": 10}


class TestLoadActiveTasks:
    @pytest.mark.asyncio
    async def test_load_tasks(self, scheduler):
        mock_task = MagicMock()
        mock_task.name = "task1"
        mock_task.id = 1

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_task]
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch.object(scheduler, "_schedule_task", new_callable=AsyncMock):
                await scheduler._load_active_tasks()
                scheduler._schedule_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_load_tasks_error(self, scheduler):
        mock_task = MagicMock()
        mock_task.name = "task1"
        mock_task.id = 1

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [mock_task]
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch.object(
                scheduler,
                "_schedule_task",
                new_callable=AsyncMock,
                side_effect=RuntimeError("fail"),
            ):
                await scheduler._load_active_tasks()  # should not raise


class TestScheduleTask:
    @pytest.mark.asyncio
    async def test_schedule(self, scheduler):
        task = MagicMock()
        task.id = 1
        task.name = "t"
        task.schedule_type = ScheduleType.CRON
        task.schedule_expression = "0 8 * * *"
        mock_db = AsyncMock()

        await scheduler._schedule_task(task, mock_db)
        scheduler.scheduler_service.add_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_scheduler_service(self):
        s = TaskScheduler()
        s.scheduler_service = None
        task = MagicMock()
        await s._schedule_task(task, AsyncMock())  # should return early


class TestExecuteTaskWrapper:
    @pytest.mark.asyncio
    async def test_task_not_found(self, scheduler):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            await scheduler._execute_task_wrapper(task_id=999)

    @pytest.mark.asyncio
    async def test_task_inactive(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.is_active = False

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            await scheduler._execute_task_wrapper(task_id=1)

    @pytest.mark.asyncio
    async def test_task_active(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.is_active = True

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            with patch.object(scheduler, "_execute_with_retry", new_callable=AsyncMock):
                await scheduler._execute_task_wrapper(task_id=1)
                scheduler._execute_with_retry.assert_called_once()


class TestExecuteWithRetry:
    @pytest.mark.asyncio
    async def test_success(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.name = "test"
        mock_task.retry_on_failure = False
        mock_task.timeout = 0
        mock_task.script_id = "s1"
        mock_task.parameters = {}

        mock_db = AsyncMock()

        mock_execution = MagicMock()
        mock_execution.execution_id = "e1"

        mock_script = MagicMock()
        mock_script.target_table = None

        with (
            patch("opendata.services.scheduler.ExecutionService") as MockES,
            patch("opendata.services.scheduler.ScriptService") as MockSS,
        ):
            MockES.return_value.create_execution = AsyncMock(return_value=mock_execution)
            MockES.return_value.update_execution = AsyncMock()
            MockES.return_value.handle_execution_complete = AsyncMock()
            MockSS.return_value.get_script = AsyncMock(return_value=mock_script)
            MockSS.return_value.execute_script = AsyncMock(return_value={"success": True})

            await scheduler._execute_with_retry(mock_task, mock_db)

    @pytest.mark.asyncio
    async def test_failure_no_retry(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.name = "test"
        mock_task.retry_on_failure = False
        mock_task.timeout = 30
        mock_task.script_id = "s1"
        mock_task.parameters = {}

        mock_db = AsyncMock()
        mock_execution = MagicMock()
        mock_execution.execution_id = "e1"

        mock_script = MagicMock()
        mock_script.target_table = None

        with (
            patch("opendata.services.scheduler.ExecutionService") as MockES,
            patch("opendata.services.scheduler.ScriptService") as MockSS,
        ):
            MockES.return_value.create_execution = AsyncMock(return_value=mock_execution)
            MockES.return_value.update_execution = AsyncMock()
            MockES.return_value.handle_execution_complete = AsyncMock()
            MockSS.return_value.get_script = AsyncMock(return_value=mock_script)
            MockSS.return_value.execute_script = AsyncMock(side_effect=RuntimeError("fail"))

            await scheduler._execute_with_retry(mock_task, mock_db)

    @pytest.mark.asyncio
    async def test_success_with_target_table(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.name = "test"
        mock_task.retry_on_failure = False
        mock_task.timeout = 0
        mock_task.script_id = "s1"
        mock_task.parameters = {}

        mock_db = AsyncMock()
        mock_execution = MagicMock()
        mock_execution.execution_id = "e1"

        mock_script = MagicMock()
        mock_script.target_table = "my_table"

        with (
            patch("opendata.services.scheduler.ExecutionService") as MockES,
            patch("opendata.services.scheduler.ScriptService") as MockSS,
            patch("opendata.data_fetch.providers.akshare_provider.AkshareProvider") as MockProv,
        ):
            MockES.return_value.create_execution = AsyncMock(return_value=mock_execution)
            MockES.return_value.update_execution = AsyncMock()
            MockSS.return_value.get_script = AsyncMock(return_value=mock_script)
            MockSS.return_value.execute_script = AsyncMock(return_value={"success": True})
            MockProv.return_value.get_table_row_count.return_value = 10

            await scheduler._execute_with_retry(mock_task, mock_db)


class TestAddTask:
    @pytest.mark.asyncio
    async def test_task_not_found(self, scheduler):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        await scheduler.add_task(999, mock_db)

    @pytest.mark.asyncio
    async def test_task_found(self, scheduler):
        mock_task = MagicMock()
        mock_task.name = "test"
        mock_task.id = 1
        mock_task.schedule_type = ScheduleType.CRON
        mock_task.schedule_expression = "0 8 * * *"

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_db.execute.return_value = mock_result

        await scheduler.add_task(1, mock_db)


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_update(self, scheduler):
        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        with (
            patch.object(scheduler, "remove_task", new_callable=AsyncMock),
            patch.object(scheduler, "add_task", new_callable=AsyncMock),
        ):
            await scheduler.update_task(1, mock_db)


class TestRemoveTask:
    @pytest.mark.asyncio
    async def test_remove_success(self, scheduler):
        await scheduler.remove_task(1)
        scheduler.scheduler_service.remove_job.assert_called_once_with("task_1")

    @pytest.mark.asyncio
    async def test_remove_no_service(self):
        s = TaskScheduler()
        s.scheduler_service = None
        await s.remove_task(1)  # should return silently

    @pytest.mark.asyncio
    async def test_remove_error(self, scheduler):
        scheduler.scheduler_service.remove_job = AsyncMock(side_effect=RuntimeError("fail"))
        await scheduler.remove_task(1)  # should not raise


class TestTriggerTask:
    @pytest.mark.asyncio
    async def test_task_not_found(self, scheduler):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = mock_result

        with patch("opendata.services.scheduler.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(ValueError, match="not found"):
                await scheduler.trigger_task(999)

    @pytest.mark.asyncio
    async def test_trigger_success(self, scheduler):
        mock_task = MagicMock()
        mock_task.id = 1
        mock_task.name = "test"
        mock_task.script_id = "s1"
        mock_task.parameters = {}

        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_task
        mock_session.execute.return_value = mock_result

        with (
            patch("opendata.services.scheduler.async_session_maker") as mock_sm,
            patch("asyncio.create_task"),
        ):
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            result = await scheduler.trigger_task(1, user_id=42)
            # New format: exec_YYYYMMDD_HHMMSS_{task_id}
            assert result.startswith("exec_")
            assert result.endswith("_1")
