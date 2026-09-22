"""
Comprehensive tests for data acquisition API endpoints.

Covers download trigger, progress, and result endpoints.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.interface import DataInterface, InterfaceCategory
from opendata.models.task import TaskExecution, TaskStatus


async def _create_interface(
    db: AsyncSession, name: str = "test_iface", is_active: bool = True
) -> DataInterface:
    """Create a test interface."""
    cat = InterfaceCategory(name=f"cat_{name}", description="Test", sort_order=99)
    db.add(cat)
    await db.flush()

    iface = DataInterface(
        name=name,
        display_name=f"Test {name}",
        category_id=cat.id,
        parameters={},
        is_active=is_active,
    )
    db.add(iface)
    await db.commit()
    await db.refresh(iface)
    return iface


async def _create_execution(
    db: AsyncSession, status: TaskStatus = TaskStatus.PENDING, **kwargs
) -> TaskExecution:
    """Create a test execution with required parent task."""
    import uuid

    from opendata.models.task import ScheduledTask, ScheduleType

    # Create parent task if not provided
    if "task_id" not in kwargs:
        task = ScheduledTask(
            name="Test Task",
            user_id=1,
            script_id="test_script",
            schedule_type=ScheduleType.CRON,
            schedule_expression="0 8 * * *",
            parameters={},
        )
        db.add(task)
        await db.flush()
        kwargs["task_id"] = task.id

    defaults = {
        "execution_id": f"exec_test_{uuid.uuid4().hex[:8]}",
        "script_id": "test_script",
        "status": status,
        "retry_count": 0,
    }
    defaults.update(kwargs)
    execution = TaskExecution(**defaults)
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution


class TestTriggerDownload:
    """Test download trigger endpoint."""

    @pytest.mark.asyncio
    async def test_trigger_download_success(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test triggering a download."""
        iface = await _create_interface(test_db, "download_test")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        with patch("opendata.api.data.data_service.execute_download", new_callable=AsyncMock):
            response = await test_client.post(
                "/api/data/download",
                headers=headers,
                json={
                    "interface_id": iface.id,
                    "parameters": {},
                },
            )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_trigger_download_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test triggering download for non-existent interface."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/download",
            headers=headers,
            json={
                "interface_id": 99999,
                "parameters": {},
            },
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_download_inactive(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test triggering download for inactive interface."""
        iface = await _create_interface(test_db, "inactive_dl", is_active=False)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/download",
            headers=headers,
            json={
                "interface_id": iface.id,
                "parameters": {},
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_trigger_download_unauthenticated(self, test_client: AsyncClient):
        """Test triggering download without auth."""
        response = await test_client.post(
            "/api/data/download",
            json={
                "interface_id": 1,
                "parameters": {},
            },
        )
        assert response.status_code == 401


class TestDownloadProgress:
    """Test download progress endpoint."""

    @pytest.mark.asyncio
    async def test_progress_pending(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for pending execution."""
        execution = await _create_execution(test_db, TaskStatus.PENDING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 0.0

    @pytest.mark.asyncio
    async def test_progress_running(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for running execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.RUNNING,
            start_time=datetime.utcnow(),
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "running"

    @pytest.mark.asyncio
    async def test_progress_success(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for completed execution."""
        execution = await _create_execution(test_db, TaskStatus.COMPLETED)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 100.0

    @pytest.mark.asyncio
    async def test_progress_failed(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test progress for failed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.FAILED,
            error_message="Test error",
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/status", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["progress"] == 0.0

    @pytest.mark.asyncio
    async def test_progress_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test progress for non-existent execution."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/99999/status", headers=headers)
        assert response.status_code == 404


class TestDownloadResult:
    """Test download result endpoint."""

    @pytest.mark.asyncio
    async def test_result_success(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for completed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.COMPLETED,
            end_time=datetime.now(UTC),
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_result_running(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for running execution (should return 202)."""
        execution = await _create_execution(test_db, TaskStatus.RUNNING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_result_pending(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for pending execution (should return 202)."""
        execution = await _create_execution(test_db, TaskStatus.PENDING)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 202

    @pytest.mark.asyncio
    async def test_result_failed(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test result for failed execution."""
        execution = await _create_execution(
            test_db,
            TaskStatus.FAILED,
            error_message="Something went wrong",
        )

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/data/download/{execution.id}/result", headers=headers
        )
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_result_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test result for non-existent execution."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/99999/result", headers=headers)
        assert response.status_code == 404
