"""
Direct tests for executions API endpoints to maximize coverage.
"""

import pytest
from fastapi import HTTPException

from opendata.core.security import hash_password
from opendata.models.task import ScheduledTask, ScheduleType, TaskExecution, TaskStatus
from opendata.models.user import User, UserRole


async def _user(db, role=UserRole.USER):
    u = User(
        username=f"eu_{role.value}",
        email=f"eu_{role.value}@t.com",
        hashed_password=hash_password("P!1"),
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _task(db, user_id):
    t = ScheduledTask(
        name="ET",
        user_id=user_id,
        script_id="es1",
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 8 * * *",
        parameters={},
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _exec(db, task_id, eid, status=TaskStatus.COMPLETED, **kw):
    e = TaskExecution(
        execution_id=eid, task_id=task_id, script_id="es1", status=status, retry_count=0, **kw
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


class TestGetExecutionsDirect:
    @pytest.mark.asyncio
    async def test_list(self, test_db):
        from opendata.api.executions import get_executions

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "e1")
        await _exec(test_db, task.id, "e2", TaskStatus.FAILED)
        result = await get_executions(
            task_id=None,
            script_id=None,
            status=None,
            start_date=None,
            end_date=None,
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.data["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_filter_status(self, test_db):
        from opendata.api.executions import get_executions

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "ef1", TaskStatus.FAILED)
        result = await get_executions(
            task_id=None,
            script_id=None,
            status=TaskStatus.FAILED,
            start_date=None,
            end_date=None,
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.success is True


class TestGetStatsDirect:
    @pytest.mark.asyncio
    async def test_stats(self, test_db):
        from opendata.api.executions import get_execution_stats

        user = await _user(test_db)
        result = await get_execution_stats(
            start_date=None, end_date=None, db=test_db, current_user=user
        )
        assert result.success is True


class TestGetRecentDirect:
    @pytest.mark.asyncio
    async def test_recent(self, test_db):
        from opendata.api.executions import get_recent_executions

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "er1")
        result = await get_recent_executions(limit=10, db=test_db, current_user=user)
        assert result.success is True


class TestGetRunningDirect:
    @pytest.mark.asyncio
    async def test_running(self, test_db):
        from opendata.api.executions import get_running_executions

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "erun1", TaskStatus.RUNNING)
        result = await get_running_executions(db=test_db, current_user=user)
        assert result.success is True


class TestGetFailedDirect:
    @pytest.mark.asyncio
    async def test_failed(self, test_db):
        from opendata.api.executions import get_failed_executions

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "efail1", TaskStatus.FAILED)
        result = await get_failed_executions(limit=10, db=test_db, current_user=user)
        assert result.success is True


class TestGetExecutionDirect:
    @pytest.mark.asyncio
    async def test_get_by_id(self, test_db):
        from opendata.api.executions import get_execution

        user = await _user(test_db)
        task = await _task(test_db, user.id)
        await _exec(test_db, task.id, "eget1")
        result = await get_execution(execution_id="eget1", db=test_db, current_user=user)
        assert result.data["execution_id"] == "eget1"

    @pytest.mark.asyncio
    async def test_get_not_found(self, test_db):
        from opendata.api.executions import get_execution

        user = await _user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_execution(execution_id="nonexistent", db=test_db, current_user=user)
        assert exc.value.status_code == 404


class TestDeleteExecutionsDirect:
    @pytest.mark.asyncio
    async def test_delete_by_ids(self, test_db):
        from opendata.api.executions import delete_executions

        admin = await _user(test_db, UserRole.ADMIN)
        task = await _task(test_db, admin.id)
        await _exec(test_db, task.id, "edel1")
        result = await delete_executions(
            execution_ids=["edel1"],
            status_filter=None,
            current_admin=admin,
            db=test_db,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_by_status(self, test_db):
        from opendata.api.executions import delete_executions

        admin = await _user(test_db, UserRole.ADMIN)
        task = await _task(test_db, admin.id)
        await _exec(test_db, task.id, "edel2", TaskStatus.FAILED)
        result = await delete_executions(
            execution_ids=None,
            status_filter=TaskStatus.FAILED,
            current_admin=admin,
            db=test_db,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_no_params(self, test_db):
        from opendata.api.executions import delete_executions

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await delete_executions(
                execution_ids=None,
                status_filter=None,
                current_admin=admin,
                db=test_db,
            )
        assert exc.value.status_code == 400
