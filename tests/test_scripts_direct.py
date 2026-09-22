"""
Direct tests for scripts API endpoints to maximize coverage.
"""

import pytest
from fastapi import HTTPException

from opendata.core.security import hash_password
from opendata.models.data_script import DataScript, ScriptFrequency
from opendata.models.user import User, UserRole


async def _user(db, role=UserRole.USER):
    u = User(
        username=f"su_{role.value}",
        email=f"su_{role.value}@t.com",
        hashed_password=hash_password("P!1"),
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _script(db, sid="ds1", **kw):
    defaults = dict(
        script_name=f"S {sid}",
        category="stock",
        frequency=ScriptFrequency.DAILY,
        is_active=True,
        is_custom=False,
    )
    defaults.update(kw)
    s = DataScript(script_id=sid, **defaults)
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return s


class TestGetScriptsDirect:
    @pytest.mark.asyncio
    async def test_get_scripts(self, test_db):
        from opendata.api.scripts import get_scripts

        user = await _user(test_db)
        await _script(test_db, "gs1")
        await _script(test_db, "gs2", category="fund")
        result = await get_scripts(
            category=None,
            frequency=None,
            is_active=None,
            keyword=None,
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.data["total"] >= 2

    @pytest.mark.asyncio
    async def test_get_scripts_filter_category(self, test_db):
        from opendata.api.scripts import get_scripts

        user = await _user(test_db)
        await _script(test_db, "fc1", category="futures")
        result = await get_scripts(
            category="futures",
            frequency=None,
            is_active=None,
            keyword=None,
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_get_scripts_filter_keyword(self, test_db):
        from opendata.api.scripts import get_scripts

        user = await _user(test_db)
        await _script(test_db, "kw1", script_name="UniqueKeyword")
        result = await get_scripts(
            category=None,
            frequency=None,
            is_active=None,
            keyword="UniqueKeyword",
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_get_scripts_filter_active(self, test_db):
        from opendata.api.scripts import get_scripts

        user = await _user(test_db)
        await _script(test_db, "af1", is_active=False)
        result = await get_scripts(
            category=None,
            frequency=None,
            is_active=False,
            keyword=None,
            page=1,
            page_size=20,
            db=test_db,
            current_user=user,
        )
        assert result.success is True


class TestGetScriptStatsDirect:
    @pytest.mark.asyncio
    async def test_stats(self, test_db):
        from opendata.api.scripts import get_script_stats

        user = await _user(test_db)
        await _script(test_db)
        result = await get_script_stats(db=test_db, current_user=user)
        assert result.success is True
        assert "total" in result.data


class TestScanScriptsDirect:
    @pytest.mark.asyncio
    async def test_scan(self, test_db):
        from opendata.api.scripts import scan_scripts

        admin = await _user(test_db, UserRole.ADMIN)
        result = await scan_scripts(db=test_db, current_admin=admin)
        assert result.success is True


class TestGetCategoriesDirect:
    @pytest.mark.asyncio
    async def test_categories(self, test_db):
        from opendata.api.scripts import get_script_categories

        user = await _user(test_db)
        await _script(test_db, "cat1", category="bond")
        result = await get_script_categories(db=test_db, current_user=user)
        assert result.success is True


class TestGetScriptDirect:
    @pytest.mark.asyncio
    async def test_get_script(self, test_db):
        from opendata.api.scripts import get_script

        user = await _user(test_db)
        await _script(test_db, "detail1")
        result = await get_script(script_id="detail1", db=test_db, current_user=user)
        assert result.data["script_id"] == "detail1"

    @pytest.mark.asyncio
    async def test_get_script_not_found(self, test_db):
        from opendata.api.scripts import get_script

        user = await _user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_script(script_id="nonexistent_xyz", db=test_db, current_user=user)
        assert exc.value.status_code == 404


class TestToggleScriptDirect:
    @pytest.mark.asyncio
    async def test_toggle(self, test_db):
        from opendata.api.scripts import toggle_script

        admin = await _user(test_db, UserRole.ADMIN)
        await _script(test_db, "tog1", is_active=True)
        result = await toggle_script(script_id="tog1", current_admin=admin, db=test_db)
        assert result.data["is_active"] is False

    @pytest.mark.asyncio
    async def test_toggle_not_found(self, test_db):
        from opendata.api.scripts import toggle_script

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await toggle_script(script_id="nonexistent", current_admin=admin, db=test_db)
        assert exc.value.status_code == 404


class TestCreateCustomScriptDirect:
    @pytest.mark.asyncio
    async def test_create(self, test_db):
        from opendata.api.scripts import ScriptCreateRequest, create_custom_script

        admin = await _user(test_db, UserRole.ADMIN)
        req = ScriptCreateRequest(script_id="custom1", script_name="Custom 1", category="stock")
        result = await create_custom_script(request=req, current_admin=admin, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_create_duplicate(self, test_db):
        from opendata.api.scripts import ScriptCreateRequest, create_custom_script

        admin = await _user(test_db, UserRole.ADMIN)
        await _script(test_db, "dup1")
        req = ScriptCreateRequest(script_id="dup1", script_name="X", category="x")
        with pytest.raises(HTTPException) as exc:
            await create_custom_script(request=req, current_admin=admin, db=test_db)
        assert exc.value.status_code == 400


class TestUpdateScriptDirect:
    @pytest.mark.asyncio
    async def test_update(self, test_db):
        from opendata.api.scripts import ScriptUpdateRequest, update_script

        admin = await _user(test_db, UserRole.ADMIN)
        await _script(test_db, "upd1")
        req = ScriptUpdateRequest(
            script_name="Updated",
            description="desc",
            sub_category="sub",
            parameters={"k": "v"},
            estimated_duration=120,
            timeout=600,
        )
        result = await update_script(script_id="upd1", request=req, current_admin=admin, db=test_db)
        assert result.data["script_name"] == "Updated"

    @pytest.mark.asyncio
    async def test_update_not_found(self, test_db):
        from opendata.api.scripts import ScriptUpdateRequest, update_script

        admin = await _user(test_db, UserRole.ADMIN)
        req = ScriptUpdateRequest(script_name="X")
        with pytest.raises(HTTPException) as exc:
            await update_script(
                script_id="nonexistent", request=req, current_admin=admin, db=test_db
            )
        assert exc.value.status_code == 404


class TestDeleteScriptDirect:
    @pytest.mark.asyncio
    async def test_delete_custom(self, test_db):
        from opendata.api.scripts import delete_script

        admin = await _user(test_db, UserRole.ADMIN)
        await _script(test_db, "del_c", is_custom=True)
        result = await delete_script(script_id="del_c", current_admin=admin, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_system(self, test_db):
        from opendata.api.scripts import delete_script

        admin = await _user(test_db, UserRole.ADMIN)
        await _script(test_db, "del_s", is_custom=False)
        with pytest.raises(HTTPException) as exc:
            await delete_script(script_id="del_s", current_admin=admin, db=test_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_not_found(self, test_db):
        from opendata.api.scripts import delete_script

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await delete_script(script_id="nonexistent", current_admin=admin, db=test_db)
        assert exc.value.status_code == 404
