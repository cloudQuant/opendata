"""
Direct tests for auth endpoints to debug coverage issue.
Tests endpoint functions directly rather than through HTTP client.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.core.security import create_refresh_token, hash_password
from opendata.models.user import User, UserRole


class TestRegisterDirect:
    """Test register endpoint function directly."""

    @pytest.mark.asyncio
    async def test_register_success_direct(self, test_db: AsyncSession):
        """Call register function body directly."""
        from opendata.api.auth import register
        from opendata.api.schemas import RegisterRequest

        request = RegisterRequest(
            email="directtest@example.com",
            password="Password123!",
            password_confirm="Password123!",
        )

        # Call the original function directly (unwrap rate_limit)
        original_func = getattr(register, "__original_func__", register)
        result = await original_func(request=request, db=test_db)

        assert result.success is True
        assert result.data["email"] == "directtest@example.com"
        assert "access_token" in result.data
        assert "refresh_token" in result.data

    @pytest.mark.asyncio
    async def test_register_duplicate_email_direct(self, test_db: AsyncSession):
        """Test register with duplicate email."""
        from fastapi import HTTPException

        from opendata.api.auth import register
        from opendata.api.schemas import RegisterRequest

        # Create user first
        user = User(
            username="existing",
            email="existing@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()

        request = RegisterRequest(
            email="existing@example.com",
            password="Password123!",
            password_confirm="Password123!",
        )

        original_func = getattr(register, "__original_func__", register)
        with pytest.raises(HTTPException) as exc_info:
            await original_func(request=request, db=test_db)
        assert exc_info.value.status_code == 400
        assert "already registered" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_register_password_mismatch_direct(self, test_db: AsyncSession):
        """Reject mismatched passwords at the schema layer.

        The match check lives in RegisterRequest's model validator, so a
        mismatch never reaches the route (FastAPI rejects it while parsing).
        """
        from pydantic import ValidationError

        from opendata.api.schemas import RegisterRequest

        with pytest.raises(ValidationError, match="Passwords do not match"):
            RegisterRequest(
                email="mismatch@example.com",
                password="Password123!",
                password_confirm="DifferentPass123!",
            )

    @pytest.mark.asyncio
    async def test_register_duplicate_username_direct(self, test_db: AsyncSession):
        """Test register with duplicate base username."""
        from opendata.api.auth import register
        from opendata.api.schemas import RegisterRequest

        # Create user with username "duplicate"
        user = User(
            username="duplicate",
            email="duplicate@other.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()

        # Register with email that would generate same base username
        request = RegisterRequest(
            email="duplicate@example.com",
            password="Password123!",
            password_confirm="Password123!",
        )

        original_func = getattr(register, "__original_func__", register)
        result = await original_func(request=request, db=test_db)
        assert result.success is True


class TestLoginDirect:
    """Test login endpoint function directly."""

    @pytest.mark.asyncio
    async def test_login_success_direct(self, test_db: AsyncSession):
        """Call login function body directly."""
        from opendata.api.auth import login
        from opendata.api.schemas import LoginRequest

        # Create user
        user = User(
            username="logintest",
            email="logintest@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()

        request = LoginRequest(
            email="logintest@example.com",
            password="Password123!",
        )

        original_func = getattr(login, "__original_func__", login)
        result = await original_func(request=request, db=test_db)

        assert result.success is True
        assert "access_token" in result.data
        assert result.data["user"]["email"] == "logintest@example.com"

    @pytest.mark.asyncio
    async def test_login_wrong_password_direct(self, test_db: AsyncSession):
        """Test login with wrong password."""
        from fastapi import HTTPException

        from opendata.api.auth import login
        from opendata.api.schemas import LoginRequest

        user = User(
            username="wrongpw",
            email="wrongpw@example.com",
            hashed_password=hash_password("CorrectPass123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()

        request = LoginRequest(
            email="wrongpw@example.com",
            password="WrongPass123!",
        )

        original_func = getattr(login, "__original_func__", login)
        with pytest.raises(HTTPException) as exc_info:
            await original_func(request=request, db=test_db)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user_direct(self, test_db: AsyncSession):
        """Test login with non-existent user."""
        from fastapi import HTTPException

        from opendata.api.auth import login
        from opendata.api.schemas import LoginRequest

        request = LoginRequest(
            email="nonexistent@example.com",
            password="Password123!",
        )

        original_func = getattr(login, "__original_func__", login)
        with pytest.raises(HTTPException) as exc_info:
            await original_func(request=request, db=test_db)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_login_inactive_user_direct(self, test_db: AsyncSession):
        """Test login with inactive user."""
        from fastapi import HTTPException

        from opendata.api.auth import login
        from opendata.api.schemas import LoginRequest

        user = User(
            username="inactive",
            email="inactive@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=False,
        )
        test_db.add(user)
        await test_db.commit()

        request = LoginRequest(
            email="inactive@example.com",
            password="Password123!",
        )

        original_func = getattr(login, "__original_func__", login)
        with pytest.raises(HTTPException) as exc_info:
            await original_func(request=request, db=test_db)
        assert exc_info.value.status_code == 403


class TestRefreshTokenDirect:
    """Test refresh token endpoint function directly."""

    @pytest.mark.asyncio
    async def test_refresh_success_direct(self, test_db: AsyncSession):
        """Call refresh function body directly."""
        from opendata.api.auth import refresh_token
        from opendata.api.schemas import RefreshTokenRequest

        # Create user
        user = User(
            username="refreshtest",
            email="refreshtest@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=True,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        # Create a valid refresh token
        token = create_refresh_token(data={"sub": str(user.id)})

        request = RefreshTokenRequest(refresh_token=token)

        result = await refresh_token(request=request, db=test_db)
        assert result.success is True
        assert "access_token" in result.data

    @pytest.mark.asyncio
    async def test_refresh_invalid_token_direct(self, test_db: AsyncSession):
        """Test refresh with invalid token."""
        from fastapi import HTTPException

        from opendata.api.auth import refresh_token
        from opendata.api.schemas import RefreshTokenRequest

        request = RefreshTokenRequest(refresh_token="invalid_token_xyz")

        with pytest.raises(HTTPException) as exc_info:
            await refresh_token(request=request, db=test_db)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_inactive_user_direct(self, test_db: AsyncSession):
        """Test refresh for inactive user."""
        from fastapi import HTTPException

        from opendata.api.auth import refresh_token
        from opendata.api.schemas import RefreshTokenRequest

        user = User(
            username="inactiverefresh",
            email="inactiverefresh@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=False,
        )
        test_db.add(user)
        await test_db.commit()
        await test_db.refresh(user)

        token = create_refresh_token(data={"sub": str(user.id)})
        request = RefreshTokenRequest(refresh_token=token)

        with pytest.raises(HTTPException) as exc_info:
            await refresh_token(request=request, db=test_db)
        assert exc_info.value.status_code == 401
