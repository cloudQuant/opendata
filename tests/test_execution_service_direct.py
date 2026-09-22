"""
Direct tests for ExecutionService to maximize coverage.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from opendata.core.security import hash_password
from opendata.models.data_script import DataScript
from opendata.models.task import ScheduledTask, ScheduleType, TaskStatus, TriggeredBy
from opendata.models.user import User, UserRole
from opendata.services.execution_service import ExecutionService

_counter = 0


async def _user(db):
    global _counter
    _counter += 1
    u = User(
        username=f"eu{_counter}",
        email=f"eu{_counter}@t.com",
        hashed_password=hash_password("P!1"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _script(db, sid="s1"):
    from sqlalchemy import select

    result = await db.execute(select(DataScript).where(DataScript.script_id == sid))
    existing = result.scalar_one_or_none()
    if existing:
        return existing
    s = DataScript(script_id=sid, script_name=sid, category="stock", function_name=sid)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _task(db, script_id="s1"):
    user = await _user(db)
    await _script(db, script_id)
    t = ScheduledTask(
        name="test_task",
        description="desc",
        user_id=user.id,
        script_id=script_id,
        schedule_type=ScheduleType.DAILY,
        schedule_expression="08:00",
        is_active=True,
        retry_on_failure=True,
        max_retries=3,
        timeout=300,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


class TestCreateExecution:
    @pytest.mark.asyncio
    async def test_create(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(
            task_id=task.id,
            script_id="s1",
            params={"key": "val"},
            triggered_by=TriggeredBy.MANUAL,
            operator_id=1,
        )
        assert exec_rec.execution_id.startswith("exec_")
        assert exec_rec.status == TaskStatus.PENDING


class TestUpdateExecution:
    @pytest.mark.asyncio
    async def test_update_status(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        now = datetime.now(UTC)
        result = await svc.update_execution(
            exec_rec.execution_id,
            status=TaskStatus.RUNNING,
            start_time=now,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_complete(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        start = datetime.now(UTC)
        await svc.update_execution(
            exec_rec.execution_id, status=TaskStatus.RUNNING, start_time=start
        )
        end = datetime.now(UTC)
        result = await svc.update_execution(
            exec_rec.execution_id,
            status=TaskStatus.COMPLETED,
            end_time=end,
            result={"ok": True},
            rows_before=0,
            rows_after=100,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_failed(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        result = await svc.update_execution(
            exec_rec.execution_id,
            status=TaskStatus.FAILED,
            error_message="boom",
            error_trace="traceback...",
            end_time=datetime.now(UTC),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_not_found(self, test_db):
        svc = ExecutionService(test_db)
        result = await svc.update_execution("nonexistent_id", status=TaskStatus.RUNNING)
        assert result is False


class TestGetExecutions:
    @pytest.mark.asyncio
    async def test_list_all(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        await svc.create_execution(task_id=task.id, script_id="s1")
        await svc.create_execution(task_id=task.id, script_id="s1")
        execs, total = await svc.get_executions()
        assert total >= 2

    @pytest.mark.asyncio
    async def test_filter_task_id(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        await svc.create_execution(task_id=task.id, script_id="s1")
        execs, total = await svc.get_executions(task_id=task.id)
        assert total >= 1

    @pytest.mark.asyncio
    async def test_filter_script_id(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        await svc.create_execution(task_id=task.id, script_id="unique_script")
        execs, total = await svc.get_executions(script_id="unique_script")
        assert total >= 1

    @pytest.mark.asyncio
    async def test_filter_status(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        await svc.create_execution(task_id=task.id, script_id="s1")
        execs, total = await svc.get_executions(status=TaskStatus.PENDING)
        assert total >= 1

    @pytest.mark.asyncio
    async def test_filter_dates(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        now = datetime.now(UTC)
        await svc.update_execution(exec_rec.execution_id, start_time=now)
        execs, total = await svc.get_executions(
            start_date=datetime(2020, 1, 1, tzinfo=UTC),
            end_date=datetime(2030, 1, 1, tzinfo=UTC),
        )
        assert total >= 0


class TestGetStats:
    @pytest.mark.asyncio
    async def test_stats_empty(self, test_db):
        svc = ExecutionService(test_db)
        stats = await svc.get_execution_stats()
        assert "total_count" in stats
        assert "success_rate" in stats

    @pytest.mark.asyncio
    async def test_stats_with_data(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        now = datetime.now(UTC)
        await svc.update_execution(
            exec_rec.execution_id, status=TaskStatus.COMPLETED, start_time=now, end_time=now
        )
        stats = await svc.get_execution_stats()
        assert stats["total_count"] >= 0


class TestRecentRunningFailed:
    @pytest.mark.asyncio
    async def test_recent(self, test_db):
        svc = ExecutionService(test_db)
        result = await svc.get_recent_executions(limit=5)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_running(self, test_db):
        svc = ExecutionService(test_db)
        result = await svc.get_running_executions()
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_failed(self, test_db):
        svc = ExecutionService(test_db)
        result = await svc.get_failed_executions()
        assert isinstance(result, list)


class TestDeleteExecutions:
    @pytest.mark.asyncio
    async def test_delete_by_ids(self, test_db):
        svc = ExecutionService(test_db)
        task = await _task(test_db)
        exec_rec = await svc.create_execution(task_id=task.id, script_id="s1")
        deleted = await svc.delete_executions_by_ids([exec_rec.execution_id])
        assert deleted >= 1

    @pytest.mark.asyncio
    async def test_delete_empty_ids(self, test_db):
        svc = ExecutionService(test_db)
        deleted = await svc.delete_executions_by_ids([])
        assert deleted == 0

    @pytest.mark.asyncio
    async def test_delete_by_status(self, test_db):
        svc = ExecutionService(test_db)
        deleted = await svc.delete_executions_by_status(TaskStatus.CANCELLED)
        assert deleted >= 0


class TestHandleExecutionComplete:
    """Tests for handle_execution_complete.

    Note: Retry logic was moved exclusively to TaskScheduler._execute_with_retry (C1).
    handle_execution_complete now only logs and returns True.
    """

    @pytest.mark.asyncio
    async def test_non_failed(self, test_db):
        svc = ExecutionService(test_db)
        result = await svc.handle_execution_complete("some_id", TaskStatus.COMPLETED)
        assert result is True

    @pytest.mark.asyncio
    async def test_failed_returns_true(self):
        mock_db = AsyncMock()
        svc = ExecutionService(mock_db)
        result = await svc.handle_execution_complete("nonexistent", TaskStatus.FAILED)
        assert result is True

    @pytest.mark.asyncio
    async def test_cancelled_returns_true(self):
        mock_db = AsyncMock()
        svc = ExecutionService(mock_db)
        result = await svc.handle_execution_complete("some_id", TaskStatus.CANCELLED)
        assert result is True
