"""
Direct tests for users API endpoints to maximize coverage.
Calls endpoint functions directly rather than through HTTP client.
"""

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.core.security import hash_password
from opendata.models.user import User, UserRole


async def _make_admin(db: AsyncSession, email="admin_direct@test.com") -> User:
    admin = User(
        username="admin_d",
        email=email,
        hashed_password=hash_password("P123!"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    return admin


async def _make_user(db: AsyncSession, email="user_direct@test.com", username="user_d") -> User:
    user = User(
        username=username,
        email=email,
        hashed_password=hash_password("P123!"),
        role=UserRole.USER,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


class TestListUsersDirect:
    @pytest.mark.asyncio
    async def test_list_users(self, test_db):
        from opendata.api.schemas import PaginatedParams
        from opendata.api.users import list_users

        admin = await _make_admin(test_db)
        await _make_user(test_db)
        params = PaginatedParams(page=1, page_size=20)
        result = await list_users(
            current_admin=admin, params=params, role=None, is_active=None, search=None, db=test_db
        )
        assert result.total >= 2

    @pytest.mark.asyncio
    async def test_list_users_filter_role(self, test_db):
        from opendata.api.schemas import PaginatedParams
        from opendata.api.users import list_users

        admin = await _make_admin(test_db)
        await _make_user(test_db)
        params = PaginatedParams(page=1, page_size=20)
        result = await list_users(
            current_admin=admin,
            params=params,
            role=UserRole.USER,
            is_active=None,
            search=None,
            db=test_db,
        )
        assert result.total >= 1

    @pytest.mark.asyncio
    async def test_list_users_filter_active(self, test_db):
        from opendata.api.schemas import PaginatedParams
        from opendata.api.users import list_users

        admin = await _make_admin(test_db)
        params = PaginatedParams(page=1, page_size=20)
        result = await list_users(
            current_admin=admin, params=params, role=None, is_active=True, search=None, db=test_db
        )
        assert result.total >= 1

    @pytest.mark.asyncio
    async def test_list_users_search(self, test_db):
        from opendata.api.schemas import PaginatedParams
        from opendata.api.users import list_users

        admin = await _make_admin(test_db)
        await _make_user(test_db, email="searchme@test.com", username="searchme")
        params = PaginatedParams(page=1, page_size=20)
        result = await list_users(
            current_admin=admin,
            params=params,
            role=None,
            is_active=None,
            search="searchme",
            db=test_db,
        )
        assert result.total >= 1


class TestGetUserDirect:
    @pytest.mark.asyncio
    async def test_get_user(self, test_db):
        from opendata.api.users import get_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        result = await get_user(user_id=user.id, current_admin=admin, db=test_db)
        assert result.email == "user_direct@test.com"

    @pytest.mark.asyncio
    async def test_get_user_not_found(self, test_db):
        from opendata.api.users import get_user

        admin = await _make_admin(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_user(user_id=99999, current_admin=admin, db=test_db)
        assert exc.value.status_code == 404


class TestUpdateUserDirect:
    @pytest.mark.asyncio
    async def test_update_user(self, test_db):
        from opendata.api.schemas import UserUpdateRequest
        from opendata.api.users import update_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        req = UserUpdateRequest(full_name="Updated Name", is_active=True)
        result = await update_user(
            user_id=user.id, current_admin=admin, user_update=req, db=test_db
        )
        assert result.is_active is True

    @pytest.mark.asyncio
    async def test_update_user_email(self, test_db):
        from opendata.api.schemas import UserUpdateRequest
        from opendata.api.users import update_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        req = UserUpdateRequest(email="newemail@test.com")
        result = await update_user(
            user_id=user.id, current_admin=admin, user_update=req, db=test_db
        )
        assert result.email == "newemail@test.com"

    @pytest.mark.asyncio
    async def test_update_user_duplicate_email(self, test_db):
        from opendata.api.schemas import UserUpdateRequest
        from opendata.api.users import update_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db, email="u1@test.com", username="u1")
        await _make_user(test_db, email="u2@test.com", username="u2")
        req = UserUpdateRequest(email="u2@test.com")
        with pytest.raises(HTTPException) as exc:
            await update_user(user_id=user.id, current_admin=admin, user_update=req, db=test_db)
        assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_update_user_not_found(self, test_db):
        from opendata.api.schemas import UserUpdateRequest
        from opendata.api.users import update_user

        admin = await _make_admin(test_db)
        req = UserUpdateRequest(full_name="X")
        with pytest.raises(HTTPException) as exc:
            await update_user(user_id=99999, current_admin=admin, user_update=req, db=test_db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_update_user_role(self, test_db):
        from opendata.api.schemas import UserUpdateRequest
        from opendata.api.users import update_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        req = UserUpdateRequest(role=UserRole.ADMIN, is_verified=True)
        result = await update_user(
            user_id=user.id, current_admin=admin, user_update=req, db=test_db
        )
        assert result.role == UserRole.ADMIN


class TestDeleteUserDirect:
    @pytest.mark.asyncio
    async def test_delete_user(self, test_db):
        from opendata.api.users import delete_user

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        result = await delete_user(user_id=user.id, current_admin=admin, db=test_db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_delete_user_not_found(self, test_db):
        from opendata.api.users import delete_user

        admin = await _make_admin(test_db)
        with pytest.raises(HTTPException) as exc:
            await delete_user(user_id=99999, current_admin=admin, db=test_db)
        assert exc.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_last_admin(self, test_db):
        from opendata.api.users import delete_user

        admin = await _make_admin(test_db)
        with pytest.raises(HTTPException) as exc:
            await delete_user(user_id=admin.id, current_admin=admin, db=test_db)
        assert exc.value.status_code == 400
        assert "last admin" in exc.value.detail


class TestResetPasswordDirect:
    @pytest.mark.asyncio
    async def test_reset_password(self, test_db):
        from opendata.api.users import reset_user_password

        admin = await _make_admin(test_db)
        user = await _make_user(test_db)
        from opendata.api.schemas import ResetPasswordRequest

        body = ResetPasswordRequest(new_password="NewPass123!")
        result = await reset_user_password(
            user_id=user.id, body=body, current_admin=admin, db=test_db
        )
        assert result.success is True

    @pytest.mark.asyncio
    async def test_reset_password_not_found(self, test_db):
        from opendata.api.users import reset_user_password

        admin = await _make_admin(test_db)
        with pytest.raises(HTTPException) as exc:
            from opendata.api.schemas import ResetPasswordRequest

            body = ResetPasswordRequest(new_password="Xaaaaa123")
            await reset_user_password(user_id=99999, body=body, current_admin=admin, db=test_db)
        assert exc.value.status_code == 404
