"""
Direct tests for tasks API endpoints to maximize coverage.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from opendata.core.security import hash_password
from opendata.models.data_script import DataScript, ScriptFrequency
from opendata.models.task import ScheduledTask, ScheduleType
from opendata.models.user import User, UserRole


async def _make_user(db, role=UserRole.USER):
    u = User(
        username=f"tu_{role.value}",
        email=f"tu_{role.value}@t.com",
        hashed_password=hash_password("P!1"),
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _make_script(db, sid="ts1"):
    s = DataScript(
        script_id=sid,
        script_name=f"S {sid}",
        category="stock",
        frequency=ScriptFrequency.DAILY,
        is_active=True,
    )
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


async def _make_task(db, user_id, script_id="ts1"):
    t = ScheduledTask(
        name="T1",
        user_id=user_id,
        script_id=script_id,
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 8 * * *",
        parameters={},
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


class TestListTasksDirect:
    @pytest.mark.asyncio
    async def test_list_as_admin(self, test_db):
        from opendata.api.tasks import list_tasks

        admin = await _make_user(test_db, UserRole.ADMIN)
        await _make_script(test_db)
        await _make_task(test_db, admin.id)
        result = await list_tasks(
            current_user=admin, page=1, page_size=20, is_active=None, db=test_db
        )
        assert result.data["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_as_user(self, test_db):
        from opendata.api.tasks import list_tasks

        user = await _make_user(test_db)
        await _make_script(test_db)
        await _make_task(test_db, user.id)
        result = await list_tasks(
            current_user=user, page=1, page_size=20, is_active=None, db=test_db
        )
        assert result.data["total"] >= 1

    @pytest.mark.asyncio
    async def test_list_filter_active(self, test_db):
        from opendata.api.tasks import list_tasks

        admin = await _make_user(test_db, UserRole.ADMIN)
        result = await list_tasks(
            current_user=admin, page=1, page_size=20, is_active=True, db=test_db
        )
        assert result.success is True


class TestCreateTaskDirect:
    @pytest.mark.asyncio
    async def test_create_success(self, test_db):
        from opendata.api.tasks import TaskCreateRequest, create_task

        user = await _make_user(test_db)
        await _make_script(test_db, "create_s")
        req = TaskCreateRequest(
            name="New Task",
            script_id="create_s",
            schedule_type="cron",
            schedule_expression="0 8 * * *",
        )
        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            result = await create_task(request=req, current_user=user, db=test_db)
        assert result.data["name"] == "New Task"

    @pytest.mark.asyncio
    async def test_create_script_not_found(self, test_db):
        from opendata.api.tasks import TaskCreateRequest, create_task

        user = await _make_user(test_db)
        req = TaskCreateRequest(
            name="X", script_id="nonexistent", schedule_type="cron", schedule_expression="0 8 * * *"
        )
        with pytest.raises(HTTPException) as exc:
            await create_task(request=req, current_user=user, db=test_db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_inactive_script(self, test_db):
        from opendata.api.tasks import TaskCreateRequest, create_task

        user = await _make_user(test_db)
        s = DataScript(script_id="inactive_s", script_name="IS", category="x", is_active=False)
        test_db.add(s)
        await test_db.commit()
        req = TaskCreateRequest(
            name="X", script_id="inactive_s", schedule_type="cron", schedule_expression="0 8 * * *"
        )
        with pytest.raises(HTTPException) as exc:
            await create_task(request=req, current_user=user, db=test_db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_create_invalid_schedule_type(self, test_db):
        from pydantic import ValidationError

        from opendata.api.tasks import TaskCreateRequest

        await _make_user(test_db)
        await _make_script(test_db, "inv_sched")
        with pytest.raises(ValidationError):
            TaskCreateRequest(
                name="X", script_id="inv_sched", schedule_type="INVALID", schedule_expression="x"
            )

    @pytest.mark.asyncio
    async def test_create_inactive_task(self, test_db):
        from opendata.api.tasks import TaskCreateRequest, create_task

        user = await _make_user(test_db)
        await _make_script(test_db, "inact_task")
        req = TaskCreateRequest(
            name="Inactive",
            script_id="inact_task",
            schedule_type="cron",
            schedule_expression="0 8 * * *",
            is_active=False,
        )
        result = await create_task(request=req, current_user=user, db=test_db)
        assert result.data["is_active"] is False


class TestGetTaskDirect:
    @pytest.mark.asyncio
    async def test_get_own_task(self, test_db):
        from opendata.api.tasks import get_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        result = await get_task(task_id=task.id, current_user=user, db=test_db)
        assert result.data["name"] == "T1"

    @pytest.mark.asyncio
    async def test_get_not_found(self, test_db):
        from opendata.api.tasks import get_task

        user = await _make_user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_task(task_id=99999, current_user=user, db=test_db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_access_denied(self, test_db):
        from opendata.api.tasks import get_task

        owner = await _make_user(test_db)
        other = User(
            username="other",
            email="other@t.com",
            hashed_password=hash_password("P!1"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(other)
        await test_db.commit()
        await test_db.refresh(other)
        await _make_script(test_db)
        task = await _make_task(test_db, owner.id)
        with pytest.raises(HTTPException) as exc:
            await get_task(task_id=task.id, current_user=other, db=test_db)
        assert exc.value.status_code == 403


class TestUpdateTaskDirect:
    @pytest.mark.asyncio
    async def test_update_name(self, test_db):
        from opendata.api.tasks import TaskUpdateRequest, update_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        req = TaskUpdateRequest(name="Updated")
        with patch("opendata.services.scheduler.task_scheduler.update_task", new_callable=AsyncMock):
            result = await update_task(task_id=task.id, request=req, current_user=user, db=test_db)
        assert result.data["name"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_deactivate(self, test_db):
        from opendata.api.tasks import TaskUpdateRequest, update_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        req = TaskUpdateRequest(is_active=False)
        with patch("opendata.services.scheduler.task_scheduler.remove_task", new_callable=AsyncMock):
            result = await update_task(task_id=task.id, request=req, current_user=user, db=test_db)
        assert result.data["is_active"] is False

    @pytest.mark.asyncio
    async def test_update_invalid_schedule_type(self, test_db):
        from pydantic import ValidationError

        from opendata.api.tasks import TaskUpdateRequest

        with pytest.raises(ValidationError):
            TaskUpdateRequest(schedule_type="INVALID")

    @pytest.mark.asyncio
    async def test_update_all_fields(self, test_db):
        from opendata.api.tasks import TaskUpdateRequest, update_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        req = TaskUpdateRequest(
            name="Full",
            description="desc",
            schedule_type="daily",
            schedule_expression="08:00",
            parameters={"k": "v"},
            retry_on_failure=False,
            max_retries=5,
            timeout=600,
        )
        with patch("opendata.services.scheduler.task_scheduler.update_task", new_callable=AsyncMock):
            result = await update_task(task_id=task.id, request=req, current_user=user, db=test_db)
        assert result.data["name"] == "Full"


class TestDeleteTaskDirect:
    @pytest.mark.asyncio
    async def test_delete_own(self, test_db):
        from opendata.api.tasks import delete_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        with patch("opendata.services.scheduler.task_scheduler.remove_task", new_callable=AsyncMock):
            result = await delete_task(task_id=task.id, current_user=user, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_not_found(self, test_db):
        from opendata.api.tasks import delete_task

        user = await _make_user(test_db)
        with pytest.raises(HTTPException) as exc:
            await delete_task(task_id=99999, current_user=user, db=test_db)
        assert exc.value.status_code == 404


class TestTriggerTaskDirect:
    @pytest.mark.asyncio
    async def test_trigger(self, test_db):
        from opendata.api.tasks import trigger_task

        user = await _make_user(test_db)
        await _make_script(test_db)
        task = await _make_task(test_db, user.id)
        with patch(
            "opendata.services.scheduler.task_scheduler.trigger_task",
            new_callable=AsyncMock,
            return_value="exec_1",
        ):
            result = await trigger_task(task_id=task.id, current_user=user, db=test_db)
        assert result.data["execution_id"] == "exec_1"

    @pytest.mark.asyncio
    async def test_trigger_not_found(self, test_db):
        from opendata.api.tasks import trigger_task

        user = await _make_user(test_db)
        with pytest.raises(HTTPException) as exc:
            await trigger_task(task_id=99999, current_user=user, db=test_db)
        assert exc.value.status_code == 404


class TestScheduleTemplates:
    @pytest.mark.asyncio
    async def test_get_templates(self, test_db):
        from opendata.api.tasks import get_schedule_templates

        user = await _make_user(test_db)
        result = await get_schedule_templates(current_user=user)
        assert result.success is True
        assert "templates" in result.data
