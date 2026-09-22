"""
Comprehensive tests for executions API endpoints.

Covers list, stats, recent, running, failed, get, delete endpoints.
"""

import pytest
from httpx import AsyncClient


class TestGetExecutions:
    """Test get executions endpoint."""

    @pytest.mark.asyncio
    async def test_get_executions(self, test_client: AsyncClient, test_user_token: str):
        """Test listing executions."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_get_executions_with_filters(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test listing executions with filters."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            "/api/executions/?page=1&page_size=10",
            headers=headers,
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_executions_unauthenticated(self, test_client: AsyncClient):
        """Test listing executions without auth."""
        response = await test_client.get("/api/executions/")
        assert response.status_code == 401


class TestGetExecutionStats:
    """Test execution stats endpoint."""

    @pytest.mark.asyncio
    async def test_get_stats(self, test_client: AsyncClient, test_user_token: str):
        """Test getting execution statistics."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/stats", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestGetRecentExecutions:
    """Test recent executions endpoint."""

    @pytest.mark.asyncio
    async def test_get_recent(self, test_client: AsyncClient, test_user_token: str):
        """Test getting recent executions."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/recent", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_recent_with_limit(self, test_client: AsyncClient, test_user_token: str):
        """Test getting recent executions with custom limit."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/recent?limit=5", headers=headers)
        assert response.status_code == 200


class TestGetRunningExecutions:
    """Test running executions endpoint."""

    @pytest.mark.asyncio
    async def test_get_running(self, test_client: AsyncClient, test_user_token: str):
        """Test getting running executions."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/running", headers=headers)
        assert response.status_code == 200


class TestGetFailedExecutions:
    """Test failed executions endpoint."""

    @pytest.mark.asyncio
    async def test_get_failed(self, test_client: AsyncClient, test_user_token: str):
        """Test getting failed executions."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/failed", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_failed_with_limit(self, test_client: AsyncClient, test_user_token: str):
        """Test getting failed executions with custom limit."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/failed?limit=5", headers=headers)
        assert response.status_code == 200


class TestGetExecutionById:
    """Test get execution by ID endpoint."""

    @pytest.mark.asyncio
    async def test_get_nonexistent_execution(self, test_client: AsyncClient, test_user_token: str):
        """Test getting non-existent execution."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/executions/nonexistent-id", headers=headers)
        assert response.status_code == 404


class TestDeleteExecutions:
    """Test delete executions endpoint."""

    @pytest.mark.asyncio
    async def test_delete_no_params(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting executions without required params."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/executions/", headers=headers)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_delete_as_regular_user(self, test_client: AsyncClient, test_user_token: str):
        """Test deleting executions as regular user (should be forbidden)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.delete("/api/executions/", headers=headers)
        assert response.status_code == 403
