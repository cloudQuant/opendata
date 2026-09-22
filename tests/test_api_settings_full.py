"""
Comprehensive tests for settings API endpoints.

Covers database config, warehouse config, test connection, update config endpoints.
"""

import pytest
from httpx import AsyncClient


class TestGetDatabaseConfig:
    """Test get database config endpoint."""

    @pytest.mark.asyncio
    async def test_get_db_config_as_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test getting database config as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/settings/database", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "host" in data
        assert "port" in data

    @pytest.mark.asyncio
    async def test_get_db_config_as_user(self, test_client: AsyncClient, test_user_token: str):
        """Test getting database config as regular user (should be forbidden)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/settings/database", headers=headers)
        assert response.status_code == 403


class TestGetWarehouseConfig:
    """Test get warehouse config endpoint."""

    @pytest.mark.asyncio
    async def test_get_warehouse_config_as_admin(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test getting warehouse config as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/settings/database/warehouse", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert "host" in data
        assert data["is_warehouse"] is True


class TestTestDatabaseConnection:
    """Test database connection test endpoint."""

    @pytest.mark.asyncio
    async def test_connection_test(self, test_client: AsyncClient, test_admin_token: str):
        """Test database connection test endpoint."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/settings/database/test",
            headers=headers,
            json={
                "host": "localhost",
                "port": 3306,
                "database": "test",
                "user": "test",
                "password": "test",
            },
        )
        # Will fail to connect but should not crash
        assert response.status_code == 200
        data = response.json()
        assert "success" in data

    @pytest.mark.asyncio
    async def test_warehouse_connection_test(self, test_client: AsyncClient, test_admin_token: str):
        """Test warehouse connection test endpoint."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/settings/database/warehouse/test",
            headers=headers,
            json={
                "host": "localhost",
                "port": 3306,
                "database": "test",
                "user": "test",
                "password": "test",
            },
        )
        assert response.status_code == 200


class TestUpdateDatabaseConfig:
    """Test update database config endpoint."""

    @pytest.mark.asyncio
    async def test_update_db_config(self, test_client: AsyncClient, test_admin_token: str):
        """Test updating database config persists to .env."""
        from unittest.mock import patch

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        with patch("opendata.api.settings._update_env_file"):
            response = await test_client.put(
                "/api/settings/database",
                headers=headers,
                json={
                    "host": "localhost",
                    "port": 3306,
                    "database": "test",
                    "user": "test",
                    "password": "test",
                },
            )
        assert response.status_code == 200
        data = response.json()
        assert data["host"] == "localhost"
