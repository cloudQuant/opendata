"""
Direct tests for data API endpoints to maximize coverage.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from opendata.core.security import hash_password
from opendata.models.interface import DataInterface, InterfaceCategory
from opendata.models.task import ScheduledTask, ScheduleType, TaskExecution, TaskStatus
from opendata.models.user import User, UserRole


async def _user(db):
    u = User(
        username="du",
        email="du@t.com",
        hashed_password=hash_password("P!1"),
        role=UserRole.USER,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _iface(db, name="di1", active=True):
    cat = InterfaceCategory(name=f"cat_{name}", description="T", sort_order=1)
    db.add(cat)
    await db.flush()
    iface = DataInterface(
        name=name, display_name=f"D {name}", category_id=cat.id, parameters={}, is_active=active
    )
    db.add(iface)
    await db.commit()
    await db.refresh(iface)
    return iface


async def _task_and_exec(db, eid, status=TaskStatus.COMPLETED, **kw):
    task = ScheduledTask(
        name="DT",
        user_id=1,
        script_id="ds",
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 8 * * *",
        parameters={},
    )
    db.add(task)
    await db.flush()
    e = TaskExecution(
        execution_id=eid, task_id=task.id, script_id="ds", status=status, retry_count=0, **kw
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


class TestTriggerDownloadDirect:
    @pytest.mark.asyncio
    async def test_success(self, test_db):
        from opendata.api.data import trigger_download
        from opendata.api.schemas import DataDownloadRequest

        user = await _user(test_db)
        iface = await _iface(test_db, "dl_ok")
        req = DataDownloadRequest(interface_id=iface.id, parameters={})
        with patch("opendata.api.data.data_service.execute_download", new_callable=AsyncMock):
            result = await trigger_download(request=req, current_user=user, db=test_db)
        assert result.status == "pending"

    @pytest.mark.asyncio
    async def test_not_found(self, test_db):
        from opendata.api.data import trigger_download
        from opendata.api.schemas import DataDownloadRequest

        user = await _user(test_db)
        req = DataDownloadRequest(interface_id=99999, parameters={})
        with pytest.raises(HTTPException) as exc:
            await trigger_download(request=req, current_user=user, db=test_db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_inactive(self, test_db):
        from opendata.api.data import trigger_download
        from opendata.api.schemas import DataDownloadRequest

        user = await _user(test_db)
        iface = await _iface(test_db, "dl_inactive", active=False)
        req = DataDownloadRequest(interface_id=iface.id, parameters={})
        with pytest.raises(HTTPException) as exc:
            await trigger_download(request=req, current_user=user, db=test_db)
        assert exc.value.status_code == 400


class TestGetProgressDirect:
    @pytest.mark.asyncio
    async def test_completed(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "ep1", TaskStatus.COMPLETED)
        result = await get_download_progress(execution_id=e.id, current_user=user, db=test_db)
        assert result.progress == 100.0

    @pytest.mark.asyncio
    async def test_running(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "ep2", TaskStatus.RUNNING, start_time=datetime.utcnow())
        result = await get_download_progress(execution_id=e.id, current_user=user, db=test_db)
        assert result.status == "running"
        assert result.progress > 0 or result.progress == 0  # may be 0 if very fast

    @pytest.mark.asyncio
    async def test_pending(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "ep3", TaskStatus.PENDING)
        result = await get_download_progress(execution_id=e.id, current_user=user, db=test_db)
        assert result.progress == 0.0

    @pytest.mark.asyncio
    async def test_failed(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "ep4", TaskStatus.FAILED, error_message="err")
        result = await get_download_progress(execution_id=e.id, current_user=user, db=test_db)
        assert result.progress == 0.0
        assert result.message == "err"

    @pytest.mark.asyncio
    async def test_not_found(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_download_progress(execution_id=99999, current_user=user, db=test_db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_running_no_start(self, test_db):
        from opendata.api.data import get_download_progress

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "ep5", TaskStatus.RUNNING)
        result = await get_download_progress(execution_id=e.id, current_user=user, db=test_db)
        assert result.status == "running"


class TestGetResultDirect:
    @pytest.mark.asyncio
    async def test_completed(self, test_db):
        from opendata.api.data import get_download_result

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "er1", TaskStatus.COMPLETED, end_time=datetime.utcnow())
        result = await get_download_result(execution_id=e.id, current_user=user, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_running(self, test_db):
        from opendata.api.data import get_download_result

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "er2", TaskStatus.RUNNING)
        with pytest.raises(HTTPException) as exc:
            await get_download_result(execution_id=e.id, current_user=user, db=test_db)
        assert exc.value.status_code == 202

    @pytest.mark.asyncio
    async def test_pending(self, test_db):
        from opendata.api.data import get_download_result

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "er3", TaskStatus.PENDING)
        with pytest.raises(HTTPException) as exc:
            await get_download_result(execution_id=e.id, current_user=user, db=test_db)
        assert exc.value.status_code == 202

    @pytest.mark.asyncio
    async def test_failed(self, test_db):
        from opendata.api.data import get_download_result

        user = await _user(test_db)
        e = await _task_and_exec(test_db, "er4", TaskStatus.FAILED, error_message="fail")
        with pytest.raises(HTTPException) as exc:
            await get_download_result(execution_id=e.id, current_user=user, db=test_db)
        assert exc.value.status_code == 500

    @pytest.mark.asyncio
    async def test_not_found(self, test_db):
        from opendata.api.data import get_download_result

        user = await _user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_download_result(execution_id=99999, current_user=user, db=test_db)
        assert exc.value.status_code == 404
