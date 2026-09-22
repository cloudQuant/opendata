"""
Comprehensive lifecycle tests for tables API endpoints.

Now that get_data_db is overridden in conftest, we can test schema, data, delete, refresh.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.data_table import DataTable


async def _create_table(db: AsyncSession, table_name: str, table_id: int, **kwargs) -> DataTable:
    """Create a test DataTable record."""
    defaults = {
        "table_comment": "Test table",
        "row_count": 100,
    }
    defaults.update(kwargs)
    table = DataTable(id=table_id, table_name=table_name, **defaults)
    db.add(table)
    await db.commit()
    await db.refresh(table)
    return table


class TestListTablesDetailed:
    """Test list tables with actual data."""

    @pytest.mark.asyncio
    async def test_list_tables_with_data(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test listing tables returns actual items."""
        await _create_table(test_db, "ak_list_test_1", 1001)
        await _create_table(test_db, "ak_list_test_2", 1002)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["total"] >= 2

    @pytest.mark.asyncio
    async def test_list_tables_search(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test searching tables by name."""
        await _create_table(test_db, "ak_unique_search_xyz", 1003)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/?search=unique_search_xyz", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["total"] >= 1


class TestGetTableDetailed:
    """Test get table with actual data."""

    @pytest.mark.asyncio
    async def test_get_table_success(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting table details successfully."""
        table = await _create_table(test_db, "ak_get_detail", 1010)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tables/{table.id}", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["table_name"] == "ak_get_detail"


class TestGetTableSchema:
    """Test get table schema endpoint."""

    @pytest.mark.asyncio
    async def test_get_schema_table_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test getting schema for non-existent table."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/99999/schema", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_schema_table_exists(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test getting schema for existing table (DESCRIBE will fail on SQLite but should handle gracefully)."""
        table = await _create_table(test_db, "ak_schema_test", 1020)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tables/{table.id}/schema", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["table_name"] == "ak_schema_test"
        # Columns will be empty since DESCRIBE doesn't work on SQLite
        assert data["columns"] == []


class TestGetTableData:
    """Test get table data endpoint."""

    @pytest.mark.asyncio
    async def test_get_data_table_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test getting data for non-existent table."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/99999/data", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_data_table_no_real_table(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test getting data when actual table doesn't exist in DB (should return 500)."""
        table = await _create_table(test_db, "ak_nonexistent_real_table", 1030)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tables/{table.id}/data", headers=headers)
        # Should get 500 because the actual table doesn't exist
        assert response.status_code == 500

    @pytest.mark.asyncio
    async def test_get_data_with_real_table(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test getting data from a real SQLite table."""
        # Create an actual table in the test database
        await test_db.execute(
            text("CREATE TABLE IF NOT EXISTS ak_real_data (id INTEGER, name TEXT)")
        )
        await test_db.execute(text("INSERT INTO ak_real_data VALUES (1, 'test')"))
        await test_db.execute(text("INSERT INTO ak_real_data VALUES (2, 'test2')"))
        await test_db.commit()

        # Create metadata record
        table = await _create_table(test_db, "ak_real_data", 1031, row_count=2)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tables/{table.id}/data", headers=headers)
        assert response.status_code == 200
        body = response.json()
        data = body.get("data", body)  # Handle APIResponse wrapper
        assert data["table_name"] == "ak_real_data"
        assert len(data["rows"]) == 2
        assert data["rows"][0]["id"] == 1

    @pytest.mark.asyncio
    async def test_get_data_pagination(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test getting data with pagination."""
        await test_db.execute(text("CREATE TABLE IF NOT EXISTS ak_paginated (id INTEGER)"))
        for i in range(10):
            await test_db.execute(text(f"INSERT INTO ak_paginated VALUES ({i})"))
        await test_db.commit()

        table = await _create_table(test_db, "ak_paginated", 1032, row_count=10)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(
            f"/api/tables/{table.id}/data?page=1&page_size=3", headers=headers
        )
        assert response.status_code == 200
        body = response.json()
        data = body.get("data", body)  # Handle APIResponse wrapper
        assert len(data["rows"]) == 3


class TestDeleteTable:
    """Test delete table endpoint (admin only)."""

    @pytest.mark.asyncio
    async def test_delete_table_not_found(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting non-existent table."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/tables/99999", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_table_forbidden_for_regular_user(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test that regular users cannot delete tables."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.delete("/api/tables/99999", headers=headers)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_table_success(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test deleting a table."""
        # Create actual table + metadata
        await test_db.execute(text("CREATE TABLE IF NOT EXISTS ak_delete_me (id INTEGER)"))
        await test_db.commit()
        table = await _create_table(test_db, "ak_delete_me", 1040)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(f"/api/tables/{table.id}", headers=headers)
        assert response.status_code == 200
        assert response.json()["success"] is True

        # Verify metadata is gone
        response = await test_client.get(f"/api/tables/{table.id}", headers=headers)
        assert response.status_code == 404


class TestRefreshTableMetadata:
    """Test refresh table metadata endpoint (admin only)."""

    @pytest.mark.asyncio
    async def test_refresh_metadata(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test refreshing metadata for existing tables."""
        # Create actual table + metadata
        await test_db.execute(text("CREATE TABLE IF NOT EXISTS ak_refresh_test (id INTEGER)"))
        await test_db.execute(text("INSERT INTO ak_refresh_test VALUES (1)"))
        await test_db.execute(text("INSERT INTO ak_refresh_test VALUES (2)"))
        await test_db.commit()

        await _create_table(test_db, "ak_refresh_test", 1050, row_count=0)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post("/api/tables/refresh", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "Refreshed" in data["message"]

    @pytest.mark.asyncio
    async def test_refresh_metadata_forbidden_for_regular_user(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test that regular users cannot refresh metadata."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/tables/refresh", headers=headers)
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_refresh_metadata_nonexistent_table(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test refreshing metadata when actual table doesn't exist (should skip)."""
        await _create_table(test_db, "ak_ghost_table", 1051, row_count=50)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post("/api/tables/refresh", headers=headers)
        assert response.status_code == 200
