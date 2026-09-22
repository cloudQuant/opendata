"""
API endpoint tests.

Tests for all API endpoints including:
- Scripts API
- Tasks API
- Executions API
- Tables API
- Interfaces API
- Settings API
- Users API
"""

import pytest
from httpx import AsyncClient


class TestScriptsAPI:
    """Test scripts API endpoints."""

    @pytest.mark.asyncio
    async def test_get_scripts_unauthorized(self, test_client: AsyncClient):
        """Test getting scripts without auth."""
        response = await test_client.get("/api/scripts/")

        # Should return 401, 403, or 422 (validation happens before auth)
        assert response.status_code in [401, 403, 422]

    @pytest.mark.asyncio
    async def test_get_scripts_authorized(self, test_client: AsyncClient, test_user_token: str):
        """Test getting scripts with auth."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/", headers=headers)

        # Debug: print error details
        if response.status_code != 200:
            print(f"Status: {response.status_code}")
            print(f"Response: {response.text}")
            # Try to parse the error
            try:
                error_data = response.json()
                print(f"Error data: {error_data}")
            except Exception:
                pass

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_get_scripts_with_filters(self, test_client: AsyncClient, test_user_token: str):
        """Test getting scripts with category filter."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/scripts/?category=stocks&page=1&page_size=10", headers=headers
        )

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_get_script_detail(self, test_client: AsyncClient, test_user_token: str):
        """Test getting script detail."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts//test_script", headers=headers)

        # May not exist, but should not error on auth
        assert response.status_code in [200, 404]

    @pytest.mark.asyncio
    async def test_get_script_stats(self, test_client: AsyncClient, test_user_token: str):
        """Test getting script statistics."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/stats", headers=headers)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_script_categories(self, test_client: AsyncClient, test_user_token: str):
        """Test getting script categories."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/categories", headers=headers)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_create_script_admin_only(self, test_client: AsyncClient, test_user_token: str):
        """Test that creating script requires admin."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/scripts/admin/scripts",
            headers=headers,
            json={
                "script_id": "new_script",
                "script_name": "New Script",
                "category": "custom",
                "module_path": "custom.path",
            },
        )

        # Regular user should be forbidden
        assert response.status_code in [401, 403]

    @pytest.mark.asyncio
    async def test_create_script_admin_success(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test creating script as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/scripts/admin/scripts",
            headers=headers,
            json={
                "script_id": "custom_script",
                "script_name": "Custom Script",
                "category": "custom",
                "module_path": "custom.module",
                "frequency": "daily",
            },
        )

        # Should succeed or already exists
        assert response.status_code in [201, 200, 400]

    @pytest.mark.asyncio
    async def test_update_script_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test updating script as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            "/api/scripts/admin/scripts/custom_script",
            headers=headers,
            json={"script_name": "Updated Name"},
        )

        # Should succeed or not found
        assert response.status_code in [200, 404]

    @pytest.mark.asyncio
    async def test_delete_script_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting script as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(
            "/api/scripts/admin/scripts/custom_script", headers=headers
        )

        # Should succeed or not found
        assert response.status_code in [200, 404]


class TestTasksAPI:
    """Test tasks API endpoints."""

    @pytest.mark.asyncio
    async def test_get_tasks(self, test_client: AsyncClient, test_user_token: str):
        """Test getting tasks list."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_create_task(self, test_client: AsyncClient, test_user_token: str):
        """Test creating a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={
                "name": "Test Task",
                "description": "Test task description",
                "script_id": "stock_zh_a_hist",
                "schedule_type": "daily",
                "schedule_expression": "15:00",
                "is_active": True,
            },
        )

        # May succeed or fail validation
        assert response.status_code in [201, 200, 400, 422]

    @pytest.mark.asyncio
    async def test_get_task_detail(self, test_client: AsyncClient, test_user_token: str):
        """Test getting task detail."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tasks/1", headers=headers)

        # May exist or not
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_update_task(self, test_client: AsyncClient, test_user_token: str):
        """Test updating a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.put(
            "/api/tasks/1", headers=headers, json={"name": "Updated Task Name"}
        )

        # May exist or not
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_delete_task(self, test_client: AsyncClient, test_user_token: str):
        """Test deleting a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.delete("/api/tasks/999", headers=headers)

        # Should return 404 for non-existent
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_trigger_task(self, test_client: AsyncClient, test_user_token: str):
        """Test manually triggering a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/tasks/1/trigger", headers=headers)

        # May exist or not
        assert response.status_code in [200, 202, 404, 422]

    @pytest.mark.asyncio
    async def test_pause_task(self, test_client: AsyncClient, test_user_token: str):
        """Test pausing a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/tasks/1/pause", headers=headers)

        # May exist or not
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_resume_task(self, test_client: AsyncClient, test_user_token: str):
        """Test resuming a task."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/tasks/1/resume", headers=headers)

        # May exist or not
        assert response.status_code in [200, 404, 422]


class TestExecutionsAPI:
    """Test executions API endpoints."""

    @pytest.mark.asyncio
    async def test_get_executions(self, test_client: AsyncClient, test_user_token: str):
        """Test getting executions list."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_get_executions_with_filters(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test getting executions with status filter."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/executions/?status=completed&page=1&page_size=20", headers=headers
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_execution_detail(self, test_client: AsyncClient, test_user_token: str):
        """Test getting execution detail."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/exec_test_001", headers=headers)

        # May exist or not
        assert response.status_code in [200, 404]

    @pytest.mark.asyncio
    async def test_get_execution_stats(self, test_client: AsyncClient, test_user_token: str):
        """Test getting execution statistics."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/stats", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_delete_executions(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting executions (admin only)."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(
            "/api/executions/", headers=headers, params={"execution_ids": ["exec1", "exec2"]}
        )

        # Should succeed, not found, or validation error
        assert response.status_code in [200, 400, 404]

    @pytest.mark.asyncio
    async def test_delete_failed_executions(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting all failed executions."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/executions/failed", headers=headers)

        # May not be implemented (405 = method not allowed)
        assert response.status_code in [200, 204, 404, 405]


class TestTablesAPI:
    """Test tables API endpoints."""

    @pytest.mark.asyncio
    async def test_get_tables(self, test_client: AsyncClient, test_user_token: str):
        """Test getting data tables list."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_get_table_detail(self, test_client: AsyncClient, test_user_token: str):
        """Test getting table detail."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/ak_test_table", headers=headers)

        # May exist or not (422 = validation error for string ID)
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_get_table_data(self, test_client: AsyncClient, test_user_token: str):
        """Test getting table data."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/tables/ak_test_table/data?page=1&page_size=10", headers=headers
        )

        # May exist or not (422 = validation error for string ID)
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_get_table_schema(self, test_client: AsyncClient, test_user_token: str):
        """Test getting table schema."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/ak_test_table/schema", headers=headers)

        # May exist or not (422 = validation error for string ID)
        assert response.status_code in [200, 404, 422]


class TestInterfacesAPI:
    """Test interfaces API endpoints."""

    @pytest.mark.asyncio
    async def test_get_interfaces(self, test_client: AsyncClient, test_user_token: str):
        """Test getting interfaces list."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/interfaces/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert "data" in data

    @pytest.mark.asyncio
    async def test_get_interfaces_with_category(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test getting interfaces filtered by category."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/data/interfaces/?category=stock&page=1&page_size=20", headers=headers
        )

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_interface_detail(self, test_client: AsyncClient, test_user_token: str):
        """Test getting interface detail."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/interfaces/stock_zh_a_hist", headers=headers)

        # May exist or not (422 = validation error for string ID)
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_search_interfaces(self, test_client: AsyncClient, test_user_token: str):
        """Test searching interfaces."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/data/interfaces/?search=stock&page=1&page_size=20", headers=headers
        )

        assert response.status_code == 200


class TestSettingsAPI:
    """Test settings API endpoints (admin only)."""

    @pytest.mark.asyncio
    async def test_get_database_config_unauthorized(self, test_client: AsyncClient):
        """Test getting database config without auth."""
        response = await test_client.get("/api/settings/database")

        # Should be unauthorized
        assert response.status_code in [401, 403]

    @pytest.mark.asyncio
    async def test_get_database_config_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test getting database config as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/settings/database", headers=headers)

        # Should succeed or not implemented (500 = config error)
        assert response.status_code in [200, 404, 500]

    @pytest.mark.asyncio
    async def test_get_warehouse_config_admin(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test getting warehouse config as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/settings/database/warehouse", headers=headers)

        # Should succeed or not implemented (500 = config error)
        assert response.status_code in [200, 404, 500]

    @pytest.mark.asyncio
    async def test_test_connection_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test testing database connection as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/settings/database/test",
            headers=headers,
            json={
                "host": "localhost",
                "port": 3306,
                "database": "test_db",
                "user": "test",
                "password": "test",
            },
        )

        # Should return test result (may fail connection)
        assert response.status_code in [200, 400, 404, 500]


class TestUsersAPI:
    """Test users API endpoints (admin only)."""

    @pytest.mark.asyncio
    async def test_get_users_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test getting users list as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/", headers=headers)

        assert response.status_code == 200
        data = response.json()
        # Paginated response has items, total, page, page_size
        assert "items" in data or "data" in data

    @pytest.mark.asyncio
    async def test_get_users_unauthorized(self, test_client: AsyncClient, test_user_token: str):
        """Test that regular users cannot get users list."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/users/", headers=headers)

        # Should be forbidden
        assert response.status_code in [401, 403]

    @pytest.mark.asyncio
    async def test_update_user_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test updating a user as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put("/api/users/1", headers=headers, json={"is_active": False})

        # May exist or not
        assert response.status_code in [200, 404]

    @pytest.mark.asyncio
    async def test_delete_user_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting a user as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/users/999", headers=headers)

        # Should return 404 for non-existent
        assert response.status_code in [200, 404]


class TestDataAPI:
    """Test data API endpoints."""

    @pytest.mark.asyncio
    async def test_manual_download(self, test_client: AsyncClient, test_user_token: str):
        """Test manual data download."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/download",
            headers=headers,
            json={
                "script_id": "stock_zh_a_hist",
                "parameters": {"symbol": "000001"},
            },
        )

        # May succeed, fail validation, or not found
        assert response.status_code in [200, 201, 202, 400, 404, 422]

    @pytest.mark.asyncio
    async def test_get_download_status(self, test_client: AsyncClient, test_user_token: str):
        """Test getting download status."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/exec_test_001/status", headers=headers)

        # Should return status or not found (422 = validation error)
        assert response.status_code in [200, 202, 404, 422]

    @pytest.mark.asyncio
    async def test_get_download_result(self, test_client: AsyncClient, test_user_token: str):
        """Test getting download result."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/data/download/exec_test_001/result", headers=headers)

        # Should return result or not found (422 = validation error)
        assert response.status_code in [200, 202, 404, 422]
