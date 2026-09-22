"""
Integration tests for TaskScheduler core logic.

Tests the scheduler's task loading, trigger config parsing, and
execution flow using mocked APScheduler and database.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.core.security import hash_password
from opendata.models.data_script import DataScript
from opendata.models.task import ScheduledTask, ScheduleType, TaskStatus, TriggeredBy
from opendata.models.user import User, UserRole


@pytest_asyncio.fixture
async def seed_data(test_db: AsyncSession):
    """Create user + script + task for scheduler tests."""
    user = User(
        username="sched_user",
        email="sched@test.com",
        hashed_password=hash_password("Pass123!"),
        role=UserRole.USER,
        is_active=True,
    )
    test_db.add(user)
    await test_db.flush()

    script = DataScript(
        script_id="test_stock_hist",
        script_name="Test Stock Hist",
        category="stock",
        module_path="opendata.data_fetch.scripts.stocks.daily.stock_zh_a_hist",
        is_active=True,
    )
    test_db.add(script)
    await test_db.flush()

    task = ScheduledTask(
        name="Test Daily Task",
        user_id=user.id,
        script_id=script.script_id,
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 8 * * *",
        parameters={},
        is_active=True,
    )
    test_db.add(task)
    await test_db.commit()

    return {"user": user, "script": script, "task": task}


class TestSchedulerTriggerConfig:
    """Test _get_trigger_config parsing logic."""

    def setup_method(self):
        from opendata.services.scheduler import TaskScheduler

        self.scheduler = TaskScheduler()

    def _make_task(self, schedule_type, expression):
        task = MagicMock(spec=ScheduledTask)
        task.schedule_type = schedule_type
        task.schedule_expression = expression
        return task

    def test_cron_trigger(self):
        task = self._make_task(ScheduleType.CRON, "0 8 * * *")
        trigger_type, args = self.scheduler._get_trigger_config(task)
        assert trigger_type == "cron"
        assert args["cron_expression"] == "0 8 * * *"

    def test_daily_trigger_hhmm(self):
        task = self._make_task(ScheduleType.DAILY, "09:30")
        trigger_type, args = self.scheduler._get_trigger_config(task)
        assert trigger_type == "cron"
        assert args["cron_expression"] == "30 09 * * *"

    def test_interval_minutes(self):
        task = self._make_task(ScheduleType.INTERVAL, "5m")
        trigger_type, args = self.scheduler._get_trigger_config(task)
        assert trigger_type == "interval"
        assert args["minutes"] == 5

    def test_interval_hours(self):
        task = self._make_task(ScheduleType.INTERVAL, "2h")
        trigger_type, args = self.scheduler._get_trigger_config(task)
        assert trigger_type == "interval"
        assert args["hours"] == 2

    def test_interval_seconds(self):
        task = self._make_task(ScheduleType.INTERVAL, "30s")
        trigger_type, args = self.scheduler._get_trigger_config(task)
        assert trigger_type == "interval"
        assert args["seconds"] == 30

    def test_once_trigger(self):
        task = self._make_task(ScheduleType.ONCE, "")
        trigger_type, _ = self.scheduler._get_trigger_config(task)
        assert trigger_type == "once"


class TestExecutionServiceIntegration:
    """Test ExecutionService create/update flow with real DB."""

    @pytest.mark.asyncio
    async def test_create_and_update_execution(self, test_db, seed_data):
        from opendata.services.execution_service import ExecutionService

        data = await seed_data if asyncio.iscoroutine(seed_data) else seed_data

        service = ExecutionService(test_db)

        # Create execution
        execution = await service.create_execution(
            task_id=data["task"].id,
            script_id=data["script"].script_id,
            params={"symbol": "000001"},
            triggered_by=TriggeredBy.MANUAL,
            operator_id=data["user"].id,
        )

        assert execution.execution_id is not None
        assert execution.status == TaskStatus.PENDING
        assert execution.script_id == "test_stock_hist"

        # Update to running
        now = datetime.now(UTC)
        ok = await service.update_execution(
            execution_id=execution.execution_id,
            status=TaskStatus.RUNNING,
            start_time=now,
        )
        assert ok is True

        # Refresh and check
        updated = await service.get_by_execution_id(execution.execution_id)
        assert updated.status == TaskStatus.RUNNING

        # Update to completed
        end = datetime.now(UTC)
        ok = await service.update_execution(
            execution_id=execution.execution_id,
            status=TaskStatus.COMPLETED,
            end_time=end,
            rows_before=100,
            rows_after=150,
        )
        assert ok is True

        final = await service.get_by_execution_id(execution.execution_id)
        assert final.status == TaskStatus.COMPLETED
        assert final.rows_before == 100
        assert final.rows_after == 150

    @pytest.mark.asyncio
    async def test_get_executions_with_filters(self, test_db, seed_data):
        from opendata.services.execution_service import ExecutionService

        data = await seed_data if asyncio.iscoroutine(seed_data) else seed_data

        service = ExecutionService(test_db)

        # Create two executions
        exec1 = await service.create_execution(
            task_id=data["task"].id,
            script_id=data["script"].script_id,
            triggered_by=TriggeredBy.SCHEDULER,
        )
        await service.update_execution(exec1.execution_id, status=TaskStatus.COMPLETED)

        exec2 = await service.create_execution(
            task_id=data["task"].id,
            script_id=data["script"].script_id,
            triggered_by=TriggeredBy.MANUAL,
        )
        await service.update_execution(exec2.execution_id, status=TaskStatus.FAILED)

        # Filter by status
        completed, total = await service.get_executions(
            status=TaskStatus.COMPLETED,
        )
        assert total >= 1
        assert all(e.status == TaskStatus.COMPLETED for e in completed)

        failed, total_f = await service.get_executions(
            status=TaskStatus.FAILED,
        )
        assert total_f >= 1
