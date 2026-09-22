"""
Direct tests for interfaces API endpoints to maximize coverage.
"""

import pytest
from fastapi import HTTPException

from opendata.core.security import hash_password
from opendata.models.interface import DataInterface, InterfaceCategory
from opendata.models.user import User, UserRole


async def _user(db, role=UserRole.USER):
    u = User(
        username=f"iu_{role.value}",
        email=f"iu_{role.value}@t.com",
        hashed_password=hash_password("P!1"),
        role=role,
        is_active=True,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _cat(db, name="cat1"):
    c = InterfaceCategory(name=name, description=f"Desc {name}", sort_order=1)
    db.add(c)
    await db.commit()
    await db.refresh(c)
    return c


async def _iface(db, name="iface1", cat_id=None, active=True):
    if cat_id is None:
        cat = await _cat(db, f"cat_for_{name}")
        cat_id = cat.id
    i = DataInterface(
        name=name, display_name=f"D {name}", category_id=cat_id, parameters={}, is_active=active
    )
    db.add(i)
    await db.commit()
    await db.refresh(i)
    return i


class TestGetCategoriesDirect:
    @pytest.mark.asyncio
    async def test_get_categories(self, test_db):
        from opendata.api.interfaces import list_categories

        await _cat(test_db, "stock")
        await _cat(test_db, "fund")
        result = await list_categories(db=test_db)
        assert result.success is True
        assert len(result.data) >= 2


class TestGetInterfacesDirect:
    @pytest.mark.asyncio
    async def test_list_all(self, test_db):
        from opendata.api.interfaces import list_interfaces
        from opendata.api.schemas import PaginatedParams

        await _iface(test_db, "li1")
        await _iface(test_db, "li2")
        params = PaginatedParams(page=1, page_size=20)
        result = await list_interfaces(
            db=test_db, params=params, category_id=None, search=None, is_active=None
        )
        assert result.data["total"] >= 2

    @pytest.mark.asyncio
    async def test_filter_category(self, test_db):
        from opendata.api.interfaces import list_interfaces
        from opendata.api.schemas import PaginatedParams

        cat = await _cat(test_db, "filter_cat")
        await _iface(test_db, "fc1", cat.id)
        params = PaginatedParams(page=1, page_size=20)
        result = await list_interfaces(
            db=test_db, params=params, category_id=cat.id, search=None, is_active=None
        )
        assert result.data["total"] >= 1

    @pytest.mark.asyncio
    async def test_filter_active(self, test_db):
        from opendata.api.interfaces import list_interfaces
        from opendata.api.schemas import PaginatedParams

        await _iface(test_db, "fa1", active=False)
        params = PaginatedParams(page=1, page_size=20)
        result = await list_interfaces(
            db=test_db, params=params, category_id=None, search=None, is_active=False
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_filter_keyword(self, test_db):
        from opendata.api.interfaces import list_interfaces
        from opendata.api.schemas import PaginatedParams

        await _iface(test_db, "keyword_test_unique")
        params = PaginatedParams(page=1, page_size=20)
        result = await list_interfaces(
            db=test_db, params=params, category_id=None, search="keyword_test", is_active=None
        )
        assert result.success is True


class TestGetInterfaceDirect:
    @pytest.mark.asyncio
    async def test_get_by_id(self, test_db):
        from opendata.api.interfaces import get_interface

        iface = await _iface(test_db, "gi1")
        result = await get_interface(interface_id=iface.id, db=test_db)
        assert result.data["name"] == "gi1"

    @pytest.mark.asyncio
    async def test_not_found(self, test_db):
        from opendata.api.interfaces import get_interface

        with pytest.raises(HTTPException) as exc:
            await get_interface(interface_id=99999, db=test_db)
        assert exc.value.status_code == 404


class TestCreateInterfaceDirect:
    @pytest.mark.asyncio
    async def test_create(self, test_db):
        from opendata.api.interfaces import create_interface

        admin = await _user(test_db, UserRole.ADMIN)
        cat = await _cat(test_db, "create_cat")
        result = await create_interface(
            current_admin=admin,
            name="new_iface",
            display_name="New",
            category_id=cat.id,
            db=test_db,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_create_invalid_category(self, test_db):
        from opendata.api.interfaces import create_interface

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await create_interface(
                current_admin=admin,
                name="bad_cat",
                display_name="X",
                category_id=99999,
                db=test_db,
            )
        assert exc.value.status_code == 400


class TestUpdateInterfaceDirect:
    @pytest.mark.asyncio
    async def test_update(self, test_db):
        from opendata.api.interfaces import update_interface

        admin = await _user(test_db, UserRole.ADMIN)
        iface = await _iface(test_db, "upd_iface")
        result = await update_interface(
            current_admin=admin,
            interface_id=iface.id,
            display_name="Updated",
            description="updated desc",
            is_active=False,
            db=test_db,
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_update_not_found(self, test_db):
        from opendata.api.interfaces import update_interface

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await update_interface(
                current_admin=admin,
                interface_id=99999,
                display_name="X",
                description=None,
                is_active=None,
                db=test_db,
            )
        assert exc.value.status_code == 404


class TestDeleteInterfaceDirect:
    @pytest.mark.asyncio
    async def test_delete(self, test_db):
        from opendata.api.interfaces import delete_interface

        admin = await _user(test_db, UserRole.ADMIN)
        iface = await _iface(test_db, "del_iface")
        result = await delete_interface(interface_id=iface.id, current_admin=admin, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_not_found(self, test_db):
        from opendata.api.interfaces import delete_interface

        admin = await _user(test_db, UserRole.ADMIN)
        with pytest.raises(HTTPException) as exc:
            await delete_interface(interface_id=99999, current_admin=admin, db=test_db)
        assert exc.value.status_code == 404
