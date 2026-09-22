"""
Comprehensive tests for tasks API endpoints.

Covers list, create, get, update, delete, trigger, schedule templates.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


class TestScheduleTemplates:
    """Test schedule templates endpoint."""

    @pytest.mark.asyncio
    async def test_get_schedule_templates(self, test_client: AsyncClient, test_user_token: str):
        """Test getting schedule templates."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/schedule/templates", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestListTasks:
    """Test list tasks endpoint."""

    @pytest.mark.asyncio
    async def test_list_tasks_as_user(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tasks as regular user."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "items" in data["data"]

    @pytest.mark.asyncio
    async def test_list_tasks_as_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test listing tasks as admin (sees all tasks)."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/tasks/", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_tasks_with_filter(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tasks with is_active filter."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/?is_active=true", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_tasks_pagination(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tasks with pagination."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/?page=1&page_size=5", headers=headers)
        assert response.status_code == 200


class TestCreateTask:
    """Test create task endpoint."""

    @pytest.mark.asyncio
    async def test_create_task_script_not_found(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test creating task with non-existent script."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={
                "name": "Test Task",
                "script_id": "nonexistent_script",
                "schedule_type": "cron",
                "schedule_expression": "0 8 * * *",
            },
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_task_with_script(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test creating task with valid script."""
        from opendata.models.data_script import DataScript, ScriptFrequency

        script = DataScript(
            script_id="test_script_for_task",
            script_name="Test Script",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        headers = {"Authorization": f"Bearer {test_user_token}"}
        with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
            response = await test_client.post(
                "/api/tasks/",
                headers=headers,
                json={
                    "name": "Test Task",
                    "script_id": "test_script_for_task",
                    "schedule_type": "cron",
                    "schedule_expression": "0 8 * * *",
                },
            )
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_create_task_invalid_schedule_type(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test creating task with invalid schedule type."""
        from opendata.models.data_script import DataScript, ScriptFrequency

        script = DataScript(
            script_id="test_invalid_sched",
            script_name="Test",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=True,
        )
        test_db.add(script)
        await test_db.commit()

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={
                "name": "Test Task",
                "script_id": "test_invalid_sched",
                "schedule_type": "invalid_type",
                "schedule_expression": "0 8 * * *",
            },
        )
        assert response.status_code in (400, 422)

    @pytest.mark.asyncio
    async def test_create_task_inactive_script(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test creating task with inactive script."""
        from opendata.models.data_script import DataScript, ScriptFrequency

        script = DataScript(
            script_id="inactive_script_task",
            script_name="Inactive Script",
            category="stock",
            frequency=ScriptFrequency.DAILY,
            is_active=False,
        )
        test_db.add(script)
        await test_db.commit()

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={
                "name": "Test Task",
                "script_id": "inactive_script_task",
                "schedule_type": "cron",
                "schedule_expression": "0 8 * * *",
            },
        )
        assert response.status_code == 400


class TestGetTask:
    """Test get task endpoint."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_task(self, test_client: AsyncClient, test_user_token: str):
        """Test getting non-existent task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/99999", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_task_access_denied(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test getting another user's task."""
        from opendata.models.task import ScheduledTask, ScheduleType

        task = ScheduledTask(
            name="Other User Task",
            user_id=99999,
            script_id="some_script",
            schedule_type=ScheduleType.CRON,
            schedule_expression="0 8 * * *",
            parameters={},
        )
        test_db.add(task)
        await test_db.commit()
        await test_db.refresh(task)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tasks/{task.id}", headers=headers)
        assert response.status_code == 403


class TestUpdateTask:
    """Test update task endpoint."""

    @pytest.mark.asyncio
    async def test_update_nonexistent_task(self, test_client: AsyncClient, test_user_token: str):
        """Test updating non-existent task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.put(
            "/api/tasks/99999",
            headers=headers,
            json={
                "name": "Updated",
            },
        )
        assert response.status_code == 404


class TestDeleteTask:
    """Test delete task endpoint."""

    @pytest.mark.asyncio
    async def test_delete_nonexistent_task(self, test_client: AsyncClient, test_user_token: str):
        """Test deleting non-existent task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.delete("/api/tasks/99999", headers=headers)
        assert response.status_code == 404


class TestTriggerTask:
    """Test trigger task endpoint."""

    @pytest.mark.asyncio
    async def test_trigger_nonexistent_task(self, test_client: AsyncClient, test_user_token: str):
        """Test triggering non-existent task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/tasks/99999/trigger", headers=headers)
        assert response.status_code == 404
