"""
Execution service detailed tests.

Tests for ExecutionService methods.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession


async def create_user_script_task(test_db, suffix):
    """Helper to create user, script, and task."""
    from opendata.models.data_script import DataScript
    from opendata.models.task import ScheduledTask
    from opendata.models.user import User

    # Create user first and commit to get ID
    user = User(
        username=f"testuser_exec{suffix}",
        email=f"testexec{suffix}@example.com",
        hashed_password="hash",
    )
    test_db.add(user)
    await test_db.commit()
    await test_db.refresh(user)

    # Create script
    script = DataScript(
        script_id=f"test_script_exec{suffix}",
        script_name="Test Script",
        category="test",
        module_path="test.module",
    )
    test_db.add(script)

    # Create task with user_id
    task = ScheduledTask(
        name=f"Test Task {suffix}",
        script_id=f"test_script_exec{suffix}",
        user_id=user.id,
        schedule_type="manual",
        schedule_expression="0 0 * * *",
        is_active=True,
    )
    test_db.add(task)
    await test_db.commit()

    return user, script, task


class TestExecutionServiceCreate:
    """Test ExecutionService create operations."""

    @pytest.mark.asyncio
    async def test_create_execution(self, test_db: AsyncSession):
        """Test creating an execution record."""
        from opendata.models.task import TaskStatus
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "1")

        service = ExecutionService(test_db)

        # Create execution
        execution = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec1",
            params={"test": "value"},
            triggered_by="manual",
            operator_id=user.id,
        )

        assert execution is not None
        assert execution.task_id == task.id
        assert execution.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_create_execution_with_task_not_found(self, test_db: AsyncSession):
        """Test creating execution for non-existent task."""
        from opendata.models.data_script import DataScript
        from opendata.services.execution_service import ExecutionService

        service = ExecutionService(test_db)

        # Create a script first
        script = DataScript(
            script_id="test_script_not_found",
            script_name="Test Script",
            category="test",
            module_path="test.module",
        )
        test_db.add(script)
        await test_db.commit()

        # Create execution - should succeed even if task doesn't exist
        execution = await service.create_execution(
            task_id=999,
            script_id="test_script_not_found",
            params={},
        )

        # Should return execution record
        assert execution is not None
        assert execution.script_id == "test_script_not_found"


class TestExecutionServiceUpdate:
    """Test ExecutionService update operations."""

    @pytest.mark.asyncio
    async def test_update_execution_status(self, test_db: AsyncSession):
        """Test updating execution status."""
        from opendata.models.task import TaskStatus
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "2")

        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec2",
            params={},
        )

        # Update to running
        start_time = datetime.now(UTC)
        updated = await service.update_execution(
            execution.execution_id,
            status=TaskStatus.RUNNING,
            start_time=start_time,
        )

        assert updated is True

        # Verify update
        found = await service.get_execution(execution.execution_id)
        assert found.status == TaskStatus.RUNNING

    @pytest.mark.asyncio
    async def test_update_execution_complete(self, test_db: AsyncSession):
        """Test marking execution as complete."""
        from opendata.models.task import TaskStatus
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "3")

        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec3",
            params={},
        )

        # Update to completed
        end_time = datetime.now(UTC)
        result_data = {"rows_processed": 100}
        updated = await service.update_execution(
            execution.execution_id,
            status=TaskStatus.COMPLETED,
            end_time=end_time,
            result=result_data,
            rows_after=100,
        )

        assert updated is True

        # Verify update
        found = await service.get_execution(execution.execution_id)
        assert found.status == TaskStatus.COMPLETED


class TestExecutionServiceGetters:
    """Test ExecutionService getter methods."""

    @pytest.mark.asyncio
    async def test_get_execution_by_id(self, test_db: AsyncSession):
        """Test getting execution by ID."""
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "4")

        service = ExecutionService(test_db)

        execution = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec4",
            params={},
        )

        # Get by ID
        found = await service.get_execution(execution.execution_id)

        assert found is not None
        assert found.execution_id == execution.execution_id

    @pytest.mark.asyncio
    async def test_get_execution_not_found(self, test_db: AsyncSession):
        """Test getting non-existent execution."""
        from opendata.services.execution_service import ExecutionService

        service = ExecutionService(test_db)

        found = await service.get_execution("nonexistent_id")

        assert found is None

    @pytest.mark.asyncio
    async def test_get_executions_by_task(self, test_db: AsyncSession):
        """Test getting executions by task ID."""
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "5")

        service = ExecutionService(test_db)

        # Create multiple executions
        await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec5",
            params={},
        )
        await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec5",
            params={},
        )

        # Get executions for task
        executions, total = await service.get_executions(task_id=task.id)

        assert total == 2
        assert len(executions) == 2


class TestExecutionServiceStats:
    """Test ExecutionService statistics methods."""

    @pytest.mark.asyncio
    async def test_get_execution_stats_with_data(self, test_db: AsyncSession):
        """Test stats with execution data."""
        from opendata.models.task import TaskStatus
        from opendata.services.execution_service import ExecutionService

        user, script, task = await create_user_script_task(test_db, "6")

        service = ExecutionService(test_db)

        # Create executions
        exec1 = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec6",
            params={},
        )
        await service.update_execution(
            exec1.execution_id,
            status=TaskStatus.COMPLETED,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
        )

        exec2 = await service.create_execution(
            task_id=task.id,
            script_id="test_script_exec6",
            params={},
        )
        await service.update_execution(
            exec2.execution_id,
            status=TaskStatus.FAILED,
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            error_message="Test error",
        )

        # Get stats
        stats = await service.get_execution_stats()

        assert stats["total_count"] >= 2
        assert stats["success_count"] >= 1
        assert stats["failed_count"] >= 1
