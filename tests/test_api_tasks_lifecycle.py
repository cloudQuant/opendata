"""
Comprehensive lifecycle tests for tasks API endpoints.

Covers full task lifecycle: create with valid script, list with items,
get own task, update own task, delete own task, trigger.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.data_script import DataScript, ScriptFrequency


async def _create_active_script(
    db: AsyncSession, script_id: str = "lifecycle_script"
) -> DataScript:
    """Create an active script for task tests."""
    script = DataScript(
        script_id=script_id,
        script_name="Lifecycle Test Script",
        category="stock",
        frequency=ScriptFrequency.DAILY,
        is_active=True,
    )
    db.add(script)
    await db.commit()
    await db.refresh(script)
    return script


class TestTaskFullLifecycle:
    """Test complete task lifecycle."""

    @pytest.mark.asyncio
    async def test_create_and_list_task(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test creating a task and then listing it."""
        await _create_active_script(test_db, "create_list_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        # Create task
        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            response = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Test Lifecycle Task",
                    "script_id": "create_list_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        assert response.status_code == 201
        task_data = response.json()["data"]
        task_id = task_data["id"]

        # List tasks - should include the created task
        response = await test_client.get("/api/tasks/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["total"] >= 1
        task_ids = [item["id"] for item in data["data"]["items"]]
        assert task_id in task_ids

    @pytest.mark.asyncio
    async def test_get_own_task(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting own task details."""
        await _create_active_script(test_db, "get_own_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Get Own Task",
                    "script_id": "get_own_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 9 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        # Get task details
        response = await test_client.get(f"/api/tasks/{task_id}", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["name"] == "Get Own Task"
        assert data["data"]["script_name"] == "Lifecycle Test Script"

    @pytest.mark.asyncio
    async def test_update_own_task(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test updating own task."""
        await _create_active_script(test_db, "upd_own_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Update Own Task",
                    "script_id": "upd_own_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 10 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        # Update task
        with patch("opendata.services.scheduler.task_scheduler.update_task", new_callable=AsyncMock):
            response = await test_client.put(
                f"/api/tasks/{task_id}",
                headers=headers,
                json={
                    "name": "Updated Task Name",
                    "schedule_expression": "0 12 * * *",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["name"] == "Updated Task Name"

    @pytest.mark.asyncio
    async def test_update_task_deactivate(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test deactivating a task via update."""
        await _create_active_script(test_db, "deact_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Deactivate Task",
                    "script_id": "deact_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        # Deactivate
        with patch("opendata.services.scheduler.task_scheduler.remove_task", new_callable=AsyncMock):
            response = await test_client.put(
                f"/api/tasks/{task_id}",
                headers=headers,
                json={
                    "is_active": False,
                },
            )
        assert response.status_code == 200
        assert response.json()["data"]["is_active"] is False

    @pytest.mark.asyncio
    async def test_delete_own_task(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test deleting own task."""
        await _create_active_script(test_db, "del_own_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Delete Own Task",
                    "script_id": "del_own_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        # Delete task
        with patch("opendata.services.scheduler.task_scheduler.remove_task", new_callable=AsyncMock):
            response = await test_client.delete(f"/api/tasks/{task_id}", headers=headers)
        assert response.status_code == 200

        # Verify it's gone
        response = await test_client.get(f"/api/tasks/{task_id}", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_trigger_own_task(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test triggering own task."""
        await _create_active_script(test_db, "trigger_own_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Trigger Own Task",
                    "script_id": "trigger_own_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        # Trigger task
        with patch(
            "opendata.services.scheduler.task_scheduler.trigger_task",
            new_callable=AsyncMock,
            return_value="exec_123",
        ):
            response = await test_client.post(f"/api/tasks/{task_id}/trigger", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["execution_id"] == "exec_123"

    @pytest.mark.asyncio
    async def test_admin_list_all_tasks(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test admin listing all tasks."""
        await _create_active_script(test_db, "admin_list_script")

        headers = {"Authorization": f"Bearer {test_admin_token}"}

        # Create task as admin
        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Admin Task",
                    "script_id": "admin_list_script",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )

        # List tasks as admin
        response = await test_client.get("/api/tasks/", headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["total"] >= 1

    @pytest.mark.asyncio
    async def test_create_inactive_task(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test creating an inactive task (should not be scheduled)."""
        await _create_active_script(test_db, "inactive_task_script")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        # Create inactive task - scheduler.add_task should NOT be called
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={
                "name": "Inactive Task",
                "script_id": "inactive_task_script",
                "schedule_type": "cron",
                "schedule_expression": "0 8 * * *",
                "is_active": False,
            },
        )
        assert response.status_code == 201
        assert response.json()["data"]["is_active"] is False

    @pytest.mark.asyncio
    async def test_update_task_invalid_schedule_type(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test updating task with invalid schedule type."""
        await _create_active_script(test_db, "inv_sched_upd")

        headers = {"Authorization": f"Bearer {test_user_token}"}

        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            create_resp = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Bad Schedule Update",
                    "script_id": "inv_sched_upd",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        task_id = create_resp.json()["data"]["id"]

        response = await test_client.put(
            f"/api/tasks/{task_id}",
            headers=headers,
            json={
                "schedule_type": "invalid_type",
            },
        )
        assert response.status_code in (400, 422)
