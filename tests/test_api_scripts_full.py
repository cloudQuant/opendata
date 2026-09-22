"""
Comprehensive tests for scripts API endpoints.

Covers list, stats, scan, categories, get, toggle, admin CRUD endpoints.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.data_script import DataScript, ScriptFrequency


async def _create_script(db: AsyncSession, script_id: str, **kwargs) -> DataScript:
    """Helper to create a test script."""
    defaults = {
        "script_name": f"Script {script_id}",
        "category": "stock",
        "frequency": ScriptFrequency.DAILY,
        "is_active": True,
        "is_custom": False,
    }
    defaults.update(kwargs)
    script = DataScript(script_id=script_id, **defaults)
    db.add(script)
    await db.commit()
    await db.refresh(script)
    return script


class TestGetScripts:
    """Test list scripts endpoint."""

    @pytest.mark.asyncio
    async def test_get_scripts(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test listing scripts."""
        await _create_script(test_db, "list_test_1")
        await _create_script(test_db, "list_test_2", category="fund")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["total"] >= 2

    @pytest.mark.asyncio
    async def test_get_scripts_with_category(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test listing scripts filtered by category."""
        await _create_script(test_db, "cat_filter_1", category="futures")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/?category=futures", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_scripts_with_keyword(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test listing scripts with keyword search."""
        await _create_script(test_db, "kw_search", script_name="UniqueKeywordName")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/?keyword=UniqueKeyword", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_scripts_with_active_filter(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test listing scripts with active filter."""
        await _create_script(test_db, "active_filter", is_active=False)

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/?is_active=false", headers=headers)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_get_scripts_pagination(self, test_client: AsyncClient, test_user_token: str):
        """Test listing scripts with pagination."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/?page=1&page_size=5", headers=headers)
        assert response.status_code == 200


class TestGetScriptStats:
    """Test script stats endpoint."""

    @pytest.mark.asyncio
    async def test_get_stats(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting script statistics."""
        await _create_script(test_db, "stats_test")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/stats", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "total" in data["data"]


class TestScanScripts:
    """Test scan scripts endpoint."""

    @pytest.mark.asyncio
    async def test_scan_as_admin(self, test_client: AsyncClient, test_admin_token: str):
        """Test scanning scripts as admin."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post("/api/scripts/scan", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_scan_as_user(self, test_client: AsyncClient, test_user_token: str):
        """Test scanning scripts as regular user (should be forbidden)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/scripts/scan", headers=headers)
        assert response.status_code == 403


class TestGetScriptCategories:
    """Test script categories endpoint."""

    @pytest.mark.asyncio
    async def test_get_categories(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting script categories."""
        await _create_script(test_db, "cat_test", category="bond")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/categories", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestGetScript:
    """Test get single script endpoint."""

    @pytest.mark.asyncio
    async def test_get_script(self, test_client: AsyncClient, test_user_token: str, test_db):
        """Test getting script details."""
        await _create_script(test_db, "get_detail_test")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/get_detail_test", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["data"]["script_id"] == "get_detail_test"

    @pytest.mark.asyncio
    async def test_get_script_not_found(self, test_client: AsyncClient, test_user_token: str):
        """Test getting non-existent script."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/nonexistent_script_xyz", headers=headers)
        assert response.status_code == 404


class TestToggleScript:
    """Test toggle script endpoint."""

    @pytest.mark.asyncio
    async def test_toggle_script(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test toggling script status."""
        await _create_script(test_db, "toggle_test", is_active=True)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put("/api/scripts/toggle_test/toggle", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["is_active"] is False

    @pytest.mark.asyncio
    async def test_toggle_script_not_found(self, test_client: AsyncClient, test_admin_token: str):
        """Test toggling non-existent script."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put("/api/scripts/nonexistent_xyz/toggle", headers=headers)
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_toggle_script_as_user(
        self, test_client: AsyncClient, test_user_token: str, test_db
    ):
        """Test toggling script as regular user (should be forbidden)."""
        await _create_script(test_db, "toggle_user_test")

        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.put("/api/scripts/toggle_user_test/toggle", headers=headers)
        assert response.status_code == 403


class TestCreateCustomScript:
    """Test create custom script endpoint."""

    @pytest.mark.asyncio
    async def test_create_custom_script(self, test_client: AsyncClient, test_admin_token: str):
        """Test creating a custom script."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/scripts/admin/scripts",
            headers=headers,
            json={
                "script_id": "custom_new_script",
                "script_name": "Custom New Script",
                "category": "stock",
                "description": "A custom test script",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_create_duplicate_script(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test creating script with duplicate ID."""
        await _create_script(test_db, "dup_script")

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.post(
            "/api/scripts/admin/scripts",
            headers=headers,
            json={
                "script_id": "dup_script",
                "script_name": "Duplicate",
                "category": "stock",
            },
        )
        assert response.status_code == 400


class TestUpdateScript:
    """Test update script endpoint."""

    @pytest.mark.asyncio
    async def test_update_script(self, test_client: AsyncClient, test_admin_token: str, test_db):
        """Test updating a script."""
        await _create_script(test_db, "upd_script")

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            "/api/scripts/admin/scripts/upd_script",
            headers=headers,
            json={
                "script_name": "Updated Name",
                "description": "Updated description",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["script_name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_update_nonexistent_script(self, test_client: AsyncClient, test_admin_token: str):
        """Test updating non-existent script."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.put(
            "/api/scripts/admin/scripts/nonexistent_xyz",
            headers=headers,
            json={
                "script_name": "X",
            },
        )
        assert response.status_code == 404


class TestDeleteScript:
    """Test delete script endpoint."""

    @pytest.mark.asyncio
    async def test_delete_custom_script(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test deleting a custom script."""
        await _create_script(test_db, "del_custom", is_custom=True)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(
            "/api/scripts/admin/scripts/del_custom", headers=headers
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_delete_system_script(
        self, test_client: AsyncClient, test_admin_token: str, test_db
    ):
        """Test deleting a system script (should be forbidden)."""
        await _create_script(test_db, "del_system", is_custom=False)

        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(
            "/api/scripts/admin/scripts/del_system", headers=headers
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_nonexistent_script(self, test_client: AsyncClient, test_admin_token: str):
        """Test deleting non-existent script."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.delete(
            "/api/scripts/admin/scripts/nonexistent_xyz", headers=headers
        )
        assert response.status_code == 404
