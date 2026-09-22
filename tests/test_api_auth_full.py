"""
Comprehensive tests for auth API endpoints.

Covers register, login, refresh, logout, me endpoints.
"""

import pytest
from httpx import AsyncClient


class TestRegister:
    """Test user registration endpoint."""

    @pytest.mark.asyncio
    async def test_register_success(self, test_client: AsyncClient):
        """Test successful registration."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "newuser@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["success"] is True
        assert "access_token" in data["data"]
        assert "refresh_token" in data["data"]

    @pytest.mark.asyncio
    async def test_register_password_mismatch(self, test_client: AsyncClient):
        """Test registration with mismatched passwords."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "mismatch@example.com",
                "password": "Password123!",
                "password_confirm": "DifferentPass!",
            },
        )
        # FastAPI returns 422 for Pydantic validation errors (e.g. password mismatch)
        assert response.status_code == 422
        detail = str(response.json())
        assert "match" in detail.lower() or "password" in detail.lower()

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, test_client: AsyncClient):
        """Test registration with already registered email."""
        user_data = {
            "email": "duplicate@example.com",
            "password": "Password123!",
            "password_confirm": "Password123!",
        }
        # Register first time
        await test_client.post("/api/auth/register", json=user_data)
        # Register again with same email
        response = await test_client.post("/api/auth/register", json=user_data)
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_register_email_normalization(self, test_client: AsyncClient):
        """Test that email is normalized to lowercase."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "UpperCase@Example.COM",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["data"]["email"] == "uppercase@example.com"


class TestLogin:
    """Test user login endpoint."""

    @pytest.mark.asyncio
    async def test_login_success(self, test_client: AsyncClient):
        """Test successful login."""
        # Register first
        await test_client.post(
            "/api/auth/register",
            json={
                "email": "logintest@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )
        # Login
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "logintest@example.com",
                "password": "Password123!",
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "access_token" in data["data"]
        assert "refresh_token" in data["data"]
        assert "user" in data["data"]

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, test_client: AsyncClient):
        """Test login with wrong password."""
        await test_client.post(
            "/api/auth/register",
            json={
                "email": "wrongpass@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "wrongpass@example.com",
                "password": "WrongPassword!",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, test_client: AsyncClient):
        """Test login with non-existent email."""
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "nobody@example.com",
                "password": "Password123!",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_inactive_user(self, test_client: AsyncClient, test_db):
        """Test login with inactive user."""
        from opendata.core.security import hash_password
        from opendata.models.user import User, UserRole

        user = User(
            username="inactive_user",
            email="inactive@example.com",
            hashed_password=hash_password("Password123!"),
            role=UserRole.USER,
            is_active=False,
        )
        test_db.add(user)
        await test_db.commit()

        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "inactive@example.com",
                "password": "Password123!",
            },
        )
        assert response.status_code == 403


class TestRefreshToken:
    """Test token refresh endpoint."""

    @pytest.mark.asyncio
    async def test_refresh_token_success(self, test_client: AsyncClient):
        """Test successful token refresh."""
        # Register and get tokens
        reg_response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "refresh@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )
        refresh_token = reg_response.json()["data"]["refresh_token"]

        # Refresh
        response = await test_client.post(
            "/api/auth/refresh",
            json={
                "refresh_token": refresh_token,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "access_token" in data["data"]

    @pytest.mark.asyncio
    async def test_refresh_token_invalid(self, test_client: AsyncClient):
        """Test refresh with invalid token."""
        response = await test_client.post(
            "/api/auth/refresh",
            json={
                "refresh_token": "invalid.token.here",
            },
        )
        assert response.status_code == 401


class TestLogout:
    """Test logout endpoint."""

    @pytest.mark.asyncio
    async def test_logout(self, test_client: AsyncClient, test_user_token: str):
        """Test logout endpoint revokes token."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/auth/logout", headers=headers)
        assert response.status_code == 200
        assert response.json()["success"] is True

        # Token should now be revoked - subsequent request should fail
        me_response = await test_client.get("/api/auth/me", headers=headers)
        assert me_response.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_no_token(self, test_client: AsyncClient):
        """Test logout without token returns 401/403."""
        response = await test_client.post("/api/auth/logout")
        assert response.status_code in (401, 403)


class TestGetCurrentUser:
    """Test get current user endpoint."""

    @pytest.mark.asyncio
    async def test_get_me(self, test_client: AsyncClient, test_user_token: str):
        """Test getting current user info."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/auth/me", headers=headers)
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "email" in data["data"]
        assert "role" in data["data"]

    @pytest.mark.asyncio
    async def test_get_me_unauthenticated(self, test_client: AsyncClient):
        """Test getting current user without auth."""
        response = await test_client.get("/api/auth/me")
        assert response.status_code == 401
