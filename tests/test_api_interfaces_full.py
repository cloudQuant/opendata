"""
Comprehensive tests for interfaces API endpoints.

Covers categories, list, get, create, update, delete endpoints.
"""

import pytest
from httpx import AsyncClient


class TestListCategories:
    """Test list categories endpoint."""

    @pytest.mark.asyncio
    async def test_list_categories(self, test_client: AsyncClient):
        """Test listing interface categories."""
        response = await test_client.get("/api/data/interfaces/categories")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestListInterfaces:
    """Test list interfaces endpoint."""

    @pytest.mark.asyncio
    async def test_list_interfaces(self, test_client: AsyncClient):
        """Test listing interfaces."""
        response = await test_client.get("/api/data/interfaces/")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_list_interfaces_with_category_filter(self, test_client: AsyncClient, test_db):
        """Test listing interfaces filtered by category."""
        from opendata.models.interface import DataInterface, InterfaceCategory

        cat = InterfaceCategory(name="test_filter_cat", description="Test", sort_order=99)
        test_db.add(cat)
        await test_db.flush()

        iface = DataInterface(
            name="test_filter_iface",
            display_name="Test Filter",
            category_id=cat.id,
            parameters={},
        )
        test_db.add(iface)
        await test_db.commit()

        response = await test_client.get(f"/api/data/interfaces/?category_id={cat.id}")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_interfaces_with_search(self, test_client: AsyncClient):
        """Test listing interfaces with search."""
        response = await test_client.get("/api/data/interfaces/?search=stock")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_interfaces_with_active_filter(self, test_client: AsyncClient):
        """Test listing interfaces with active filter."""
        response = await test_client.get("/api/data/interfaces/?is_active=true")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_list_interfaces_pagination(self, test_client: AsyncClient):
        """Test listing interfaces with pagination."""
        response = await test_client.get("/api/data/interfaces/?page=1&page_size=5")
        assert response.status_code == 200


class TestGetInterface:
    """Test get interface endpoint."""

    @pytest.mark.asyncio
    async def test_get_interface(self, test_client: AsyncClient, test_db):
        """Test getting interface details."""
        from opendata.models.interface import DataInterface, InterfaceCategory

        cat = InterfaceCategory(name="get_test_cat", description="Test", sort_order=99)
        test_db.add(cat)
        await test_db.flush()

        iface = DataInterface(
            name="test_get_iface",
            display_name="Test Get",
            category_id=cat.id,
            parameters={},
        )
        test_db.add(iface)
        await test_db.commit()
        await test_db.refresh(iface)

        response = await test_client.get(f"/api/data/interfaces/{iface.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_get_nonexistent_interface(self, test_client: AsyncClient):
        """Test getting non-existent interface."""
        response = await test_client.get("/api/data/interfaces/99999")
        assert response.status_code == 404


class TestCreateInterface:
    """Test create interface endpoint."""

    @pytest.mark.asyncio
    async def test_create_interface_as_admin(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test creating interface as admin."""
        from opendata.models.interface import InterfaceCategory

        cat = InterfaceCategory(name="create_test_cat", description="Test", sort_order=99)
        test_db.add(cat)
        await test_db.commit()
        await test_db.refresh(cat)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            f"/api/data/interfaces/?name=new_interface&display_name=New+Interface&category_id={cat.id}",
            headers=headers,
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_create_interface_invalid_category(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test creating interface with invalid category."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/data/interfaces/?name=bad_iface&display_name=Bad&category_id=99999",
            headers=headers,
        )
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_create_interface_as_user(self, test_client: AsyncClient, test_user_token: str):
        """Test creating interface as regular user (should be forbidden)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post(
            "/api/data/interfaces/?name=unauth&display_name=Unauth&category_id=1",
            headers=headers,
        )
        assert response.status_code == 403


class TestUpdateInterface:
    """Test update interface endpoint."""

    @pytest.mark.asyncio
    async def test_update_interface(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test updating interface."""
        from opendata.models.interface import DataInterface, InterfaceCategory

        cat = InterfaceCategory(name="upd_cat", description="Test", sort_order=99)
        test_db.add(cat)
        await test_db.flush()

        iface = DataInterface(
            name="update_iface",
            display_name="Original",
            category_id=cat.id,
            parameters={},
        )
        test_db.add(iface)
        await test_db.commit()
        await test_db.refresh(iface)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            f"/api/data/interfaces/{iface.id}?display_name=Updated&is_active=false",
            headers=headers,
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_update_nonexistent_interface(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test updating non-existent interface."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            "/api/data/interfaces/99999?display_name=Updated",
            headers=headers,
        )
        assert response.status_code == 404


class TestDeleteInterface:
    """Test delete interface endpoint."""

    @pytest.mark.asyncio
    async def test_delete_interface(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test deleting interface."""
        from opendata.models.interface import DataInterface, InterfaceCategory

        cat = InterfaceCategory(name="del_cat", description="Test", sort_order=99)
        test_db.add(cat)
        await test_db.flush()

        iface = DataInterface(
            name="delete_iface",
            display_name="Delete Me",
            category_id=cat.id,
            parameters={},
        )
        test_db.add(iface)
        await test_db.commit()
        await test_db.refresh(iface)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(f"/api/data/interfaces/{iface.id}", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_nonexistent_interface(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test deleting non-existent interface."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete("/api/data/interfaces/99999", headers=headers)
        assert response.status_code == 404
