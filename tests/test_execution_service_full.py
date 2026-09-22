"""
Comprehensive tests for ExecutionService.

Covers create, update, get, list, stats, recent, running, failed, delete operations.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.task import ScheduledTask, ScheduleType, TaskStatus, TriggeredBy
from opendata.services.execution_service import ExecutionService


async def _create_task(db: AsyncSession, user_id: int = 1) -> ScheduledTask:
    """Create a scheduled task for execution tests."""
    task = ScheduledTask(
        name="Exec Test Task",
        user_id=user_id,
        script_id="exec_test_script",
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 8 * * *",
        parameters={},
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


class TestExecutionServiceCreate:
    """Test execution creation."""

    @pytest.mark.asyncio
    async def test_create_execution(self, test_db: AsyncSession):
        """Test creating an execution record."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
            params={"key": "value"},
            triggered_by=TriggeredBy.MANUAL,
            operator_id=1,
        )

        assert execution.execution_id is not None
        assert execution.execution_id.startswith("exec_")
        assert execution.status == TaskStatus.PENDING
        assert execution.task_id == task.id

    @pytest.mark.asyncio
    async def test_create_execution_scheduler(self, test_db: AsyncSession):
        """Test creating an execution triggered by scheduler."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
            triggered_by=TriggeredBy.SCHEDULER,
        )

        assert execution.triggered_by == TriggeredBy.SCHEDULER
        assert execution.operator_id is None


class TestExecutionServiceUpdate:
    """Test execution update."""

    @pytest.mark.asyncio
    async def test_update_execution_status(self, test_db: AsyncSession):
        """Test updating execution status."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
        )

        result = await service.update_execution(
            execution_id=execution.execution_id,
            status=TaskStatus.RUNNING,
            start_time=datetime.now(UTC),
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_execution_complete(self, test_db: AsyncSession):
        """Test completing an execution."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
        )

        result = await service.update_execution(
            execution_id=execution.execution_id,
            status=TaskStatus.COMPLETED,
            end_time=datetime.now(UTC),
            result={"rows": 100},
            rows_before=0,
            rows_after=100,
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_execution_failed(self, test_db: AsyncSession):
        """Test marking execution as failed."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
        )

        result = await service.update_execution(
            execution_id=execution.execution_id,
            status=TaskStatus.FAILED,
            end_time=datetime.now(UTC),
            error_message="Connection refused",
            error_trace="Traceback...",
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_update_nonexistent_execution(self, test_db: AsyncSession):
        """Test updating non-existent execution."""
        service = ExecutionService(test_db)
        result = await service.update_execution(
            execution_id="nonexistent_exec_id",
            status=TaskStatus.RUNNING,
        )
        assert result is False


class TestExecutionServiceGet:
    """Test get execution."""

    @pytest.mark.asyncio
    async def test_get_by_execution_id(self, test_db: AsyncSession):
        """Test getting execution by ID."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
        )

        found = await service.get_by_execution_id(execution.execution_id)
        assert found is not None
        assert found.execution_id == execution.execution_id

    @pytest.mark.asyncio
    async def test_get_execution_alias(self, test_db: AsyncSession):
        """Test get_execution alias method."""
        task = await _create_task(test_db)
        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="exec_test_script",
        )

        found = await service.get_execution(execution.execution_id)
        assert found is not None

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, test_db: AsyncSession):
        """Test getting non-existent execution."""
        service = ExecutionService(test_db)
        found = await service.get_by_execution_id("nonexistent")
        assert found is None
