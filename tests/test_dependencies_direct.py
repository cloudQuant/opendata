"""
Direct tests for API dependencies to maximize coverage.
"""

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from opendata.core.security import create_access_token, hash_password
from opendata.models.user import User, UserRole


async def _user(db, role=UserRole.USER, active=True):
    u = User(
        username=f"dep_{role.value}",
        email=f"dep_{role.value}@t.com",
        hashed_password=hash_password("P!1"),
        role=role,
        is_active=active,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_no_credentials(self, test_db):
        from opendata.api.dependencies import get_current_user

        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=None, db=test_db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token(self, test_db):
        from opendata.api.dependencies import get_current_user

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid_token")
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=test_db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token(self, test_db):
        from opendata.api.dependencies import get_current_user

        user = await _user(test_db)
        token = create_access_token(data={"sub": str(user.id), "email": user.email})
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_current_user(credentials=creds, db=test_db)
        assert result.id == user.id

    @pytest.mark.asyncio
    async def test_user_not_found(self, test_db):
        from opendata.api.dependencies import get_current_user

        token = create_access_token(data={"sub": "99999", "email": "x@t.com"})
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=test_db)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_inactive_user(self, test_db):
        from opendata.api.dependencies import get_current_user

        user = await _user(test_db, active=False)
        token = create_access_token(data={"sub": str(user.id), "email": user.email})
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        with pytest.raises(HTTPException) as exc:
            await get_current_user(credentials=creds, db=test_db)
        assert exc.value.status_code == 403


class TestGetCurrentActiveUser:
    @pytest.mark.asyncio
    async def test_active(self, test_db):
        from opendata.api.dependencies import get_current_active_user

        user = await _user(test_db)
        result = await get_current_active_user(current_user=user)
        assert result.id == user.id

    @pytest.mark.asyncio
    async def test_inactive(self, test_db):
        from opendata.api.dependencies import get_current_active_user

        user = await _user(test_db, active=False)
        with pytest.raises(HTTPException) as exc:
            await get_current_active_user(current_user=user)
        assert exc.value.status_code == 403


class TestGetCurrentAdminUser:
    @pytest.mark.asyncio
    async def test_admin(self, test_db):
        from opendata.api.dependencies import get_current_admin_user

        admin = await _user(test_db, UserRole.ADMIN)
        result = await get_current_admin_user(current_user=admin)
        assert result.role == UserRole.ADMIN

    @pytest.mark.asyncio
    async def test_non_admin(self, test_db):
        from opendata.api.dependencies import get_current_admin_user

        user = await _user(test_db)
        with pytest.raises(HTTPException) as exc:
            await get_current_admin_user(current_user=user)
        assert exc.value.status_code == 403


class TestGetOptionalUser:
    @pytest.mark.asyncio
    async def test_no_credentials(self, test_db):
        from opendata.api.dependencies import get_optional_user

        result = await get_optional_user(credentials=None, db=test_db)
        assert result is None

    @pytest.mark.asyncio
    async def test_valid_credentials(self, test_db):
        from opendata.api.dependencies import get_optional_user

        user = await _user(test_db)
        token = create_access_token(data={"sub": str(user.id), "email": user.email})
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        result = await get_optional_user(credentials=creds, db=test_db)
        assert result.id == user.id

    @pytest.mark.asyncio
    async def test_invalid_credentials(self, test_db):
        from opendata.api.dependencies import get_optional_user

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials="bad")
        result = await get_optional_user(credentials=creds, db=test_db)
        assert result is None
