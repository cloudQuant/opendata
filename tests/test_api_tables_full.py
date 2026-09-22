"""
Comprehensive tests for tables API endpoints.

Covers list, get, schema, data, delete, refresh endpoints.
"""

import pytest
from httpx import AsyncClient


class TestListTables:
    """Test list tables endpoint."""

    @pytest.mark.asyncio
    async def test_list_tables(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tables."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_list_tables_with_search(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tables with search."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/?search=stock", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_tables_pagination(self, test_client: AsyncClient, test_user_token: str):
        """Test listing tables with pagination."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/?page=1&page_size=5", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_tables_unauthenticated(self, test_client: AsyncClient):
        """Test listing tables without auth."""
        response = await test_client.get("/api/tables/")
        assert response.status_code == 401


class TestGetTable:
    """Test get table endpoint."""

    @pytest.mark.asyncio
    async def test_get_table(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting table details."""
        from opendata.models.data_table import DataTable

        table = DataTable(
            id=100,
            table_name="test_get_table",
            table_comment="Test",
            row_count=50,
        )
        test_db.add(table)
        await test_db.commit()

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get(f"/api/tables/{table.id}", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_nonexistent_table(self, test_client: AsyncClient, test_user_token: str):
        """Test getting non-existent table."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/tables/99999", headers=headers)
        assert response.status_code == 404


class TestTableSafeName:
    """Test table name validation."""

    def test_safe_table_name_valid(self):
        """Test valid table name."""
        from opendata.utils.helpers import safe_table_name

        assert safe_table_name("valid_name") == "`valid_name`"

    def test_safe_table_name_invalid(self):
        """Test invalid table name raises error."""
        from opendata.utils.helpers import safe_table_name

        with pytest.raises(ValueError):
            safe_table_name("invalid-name!")

    def test_safe_table_name_sql_injection(self):
        """Test SQL injection attempt in table name."""
        from opendata.utils.helpers import safe_table_name

        with pytest.raises(ValueError):
            safe_table_name("table; DROP TABLE users")

    def test_get_safe_table_name_raises_400_on_invalid(self):
        """Test _get_safe_table_name returns HTTP 400 for invalid names."""
        from fastapi import HTTPException

        from opendata.api.tables import _get_safe_table_name

        with pytest.raises(HTTPException) as exc_info:
            _get_safe_table_name("invalid; DROP TABLE")
        assert exc_info.value.status_code == 400
