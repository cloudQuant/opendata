"""
Authentication API tests.
"""

import pytest
from httpx import AsyncClient


class TestAuthEndpoints:
    """Test authentication endpoints."""

    @pytest.mark.asyncio
    async def test_register_user(self, test_client: AsyncClient):
        """Test user registration."""
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
        assert "user_id" in data["data"]

    @pytest.mark.asyncio
    async def test_register_duplicate_email(self, test_client: AsyncClient):
        """Test registration with duplicate email."""
        user_data = {
            "email": "duplicate@example.com",
            "password": "Password123!",
            "password_confirm": "Password123!",
        }
        # First registration
        await test_client.post("/api/auth/register", json=user_data)

        # Duplicate registration
        response = await test_client.post("/api/auth/register", json=user_data)

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_register_invalid_email(self, test_client: AsyncClient):
        """Test registration with invalid email."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "invalid-email",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_register_password_mismatch(self, test_client: AsyncClient):
        """Test registration with mismatched passwords."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "test@example.com",
                "password": "Password123!",
                "password_confirm": "Different123!",
            },
        )

        # FastAPI returns 422 for Pydantic validation errors
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_register_weak_password(self, test_client: AsyncClient):
        """Test registration with weak password."""
        response = await test_client.post(
            "/api/auth/register",
            json={
                "email": "test@example.com",
                "password": "short",  # Less than 8 characters
                "password_confirm": "short",
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_login_success(self, test_client: AsyncClient):
        """Test successful login."""
        # Register first
        await test_client.post(
            "/api/auth/register",
            json={
                "email": "login@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )

        # Login
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "login@example.com",
                "password": "Password123!",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "access_token" in data["data"]
        assert "refresh_token" in data["data"]

    @pytest.mark.asyncio
    async def test_login_wrong_password(self, test_client: AsyncClient):
        """Test login with wrong password."""
        # Register first
        await test_client.post(
            "/api/auth/register",
            json={
                "email": "wrong@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )

        # Login with wrong password
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "wrong@example.com",
                "password": "WrongPassword123!",
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_login_nonexistent_user(self, test_client: AsyncClient):
        """Test login with non-existent user."""
        response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "nonexistent@example.com",
                "password": "Password123!",
            },
        )

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user(self, test_client: AsyncClient, test_user_token: str):
        """Test getting current user info."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/auth/me", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "user_id" in data["data"]
        assert "email" in data["data"]

    @pytest.mark.asyncio
    async def test_get_current_user_unauthorized(self, test_client: AsyncClient):
        """Test getting current user without auth."""
        response = await test_client.get("/api/auth/me")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_get_current_user_invalid_token(self, test_client: AsyncClient):
        """Test getting current user with invalid token."""
        headers = {"Authorization": "Bearer invalid_token"}
        response = await test_client.get("/api/auth/me", headers=headers)

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_logout(self, test_client: AsyncClient, test_user_token: str):
        """Test logout."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.post("/api/auth/logout", headers=headers)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_refresh_token(self, test_client: AsyncClient):
        """Test token refresh."""
        # Register and login to get tokens
        await test_client.post(
            "/api/auth/register",
            json={
                "email": "refresh@example.com",
                "password": "Password123!",
                "password_confirm": "Password123!",
            },
        )

        login_response = await test_client.post(
            "/api/auth/login",
            json={
                "email": "refresh@example.com",
                "password": "Password123!",
            },
        )
        login_data = login_response.json()
        refresh_token = login_data["data"]["refresh_token"]

        # Refresh token
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
                "refresh_token": "invalid_refresh_token",
            },
        )

        assert response.status_code == 401
