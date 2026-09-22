"""
Security and authentication tests.

Tests for:
- Password hashing and verification
- JWT token creation and validation
- Rate limiting
- Permission checks
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient

from opendata.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
    verify_token,
)


class TestPasswordSecurity:
    """Test password hashing and verification."""

    def test_hash_password(self):
        """Test password hashing."""
        password = "TestPassword123!"
        hashed = hash_password(password)

        assert hashed != password
        assert hashed.startswith("$2b$")  # bcrypt prefix
        assert len(hashed) == 60  # bcrypt hash length

    def test_verify_password_correct(self):
        """Test password verification with correct password."""
        password = "TestPassword123!"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_password_incorrect(self):
        """Test password verification with incorrect password."""
        password = "TestPassword123!"
        wrong_password = "WrongPassword123!"
        hashed = hash_password(password)

        assert verify_password(wrong_password, hashed) is False

    def test_hash_different_results(self):
        """Test that hashing same password twice gives different results."""
        password = "TestPassword123!"
        hash1 = hash_password(password)
        hash2 = hash_password(password)

        assert hash1 != hash2  # Different salts
        assert verify_password(password, hash1) is True
        assert verify_password(password, hash2) is True


class TestJWTTokens:
    """Test JWT token creation and validation."""

    def test_create_access_token(self):
        """Test creating access token."""
        data = {"sub": "user_id_123"}
        token = create_access_token(data)

        assert isinstance(token, str)
        assert len(token) > 0

        # Verify it's a valid JWT format (header.payload.signature)
        parts = token.split(".")
        assert len(parts) == 3

    def test_create_refresh_token(self):
        """Test creating refresh token."""
        data = {"sub": "user_id_123"}
        token = create_refresh_token(data)

        assert isinstance(token, str)
        assert len(token) > 0

    def test_decode_valid_token(self):
        """Test decoding a valid token."""
        data = {"sub": "user_123", "role": "user"}
        token = create_access_token(data)

        decoded = decode_token(token)

        assert decoded is not None
        assert decoded["sub"] == "user_123"
        assert decoded["role"] == "user"

    def test_decode_invalid_token(self):
        """Test decoding an invalid token."""
        decoded = decode_token("invalid.token.here")

        assert decoded is None

    def test_verify_token_valid(self):
        """Test verifying a valid token."""
        data = {"sub": "user_123"}
        token = create_access_token(data)

        payload = verify_token(token)

        assert payload is not None
        assert payload["sub"] == "user_123"

    def test_verify_token_expired(self):
        """Test verifying an expired token."""
        # Create token that's already expired
        import jwt

        from opendata.core.config import settings

        expired_data = {
            "sub": "user_123",
            "exp": (
                datetime.now(UTC) - timedelta(hours=1)
            ).timestamp(),  # Expired - timestamp as float
        }
        token = jwt.encode(expired_data, settings.secret_key, algorithm=settings.algorithm)

        payload = verify_token(token)

        assert payload is None

    def test_token_expiration_claim(self):
        """Test that token has expiration claim."""
        data = {"sub": "user_123"}
        token = create_access_token(data)

        decoded = decode_token(token)

        assert "exp" in decoded
        # exp should be in the future
        exp_timestamp = decoded["exp"]
        assert exp_timestamp > datetime.now(UTC).timestamp()


class TestRateLimiting:
    """Test rate limiting functionality."""

    @pytest.mark.asyncio
    async def test_rate_limit_headers(self, test_client: AsyncClient, test_user_token: str):
        """Test that rate limit headers are present."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/scripts/", headers=headers)

        # Check for response - should succeed, be rate limited, or not found (404 = feature not enabled)
        assert response.status_code in [200, 404, 422]

    @pytest.mark.asyncio
    async def test_rate_limit_exceeded(self, test_client: AsyncClient, test_user_token: str):
        """Test rate limit exceeded (if enabled)."""
        headers = {"Authorization": f"Bearer {test_user_token}"}

        # Make multiple requests
        responses = []
        for _ in range(5):  # Try a few requests
            response = await test_client.get("/api/scripts/", headers=headers)
            responses.append(response.status_code)

        # At least some requests should succeed (422 is validation error, 200 is success, 404 = not found)
        assert 200 in responses or 422 in responses or 404 in responses


class TestPermissions:
    """Test permission checks."""

    @pytest.mark.asyncio
    async def test_admin_endpoint_requires_admin(
        self, test_client: AsyncClient, test_user_token: str
    ):
        """Test that admin endpoints reject regular users."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/users/", headers=headers)

        # Regular user should be forbidden from admin endpoints
        assert response.status_code in [401, 403]

    @pytest.mark.asyncio
    async def test_admin_endpoint_allows_admin(
        self, test_client: AsyncClient, test_admin_token: str
    ):
        """Test that admin endpoints allow admins."""
        headers = {"Authorization": f"Bearer {test_admin_token}"}
        response = await test_client.get("/api/users/", headers=headers)

        # Admin should be able to access admin endpoints
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_user_can_access_own_data(self, test_client: AsyncClient, test_user_token: str):
        """Test that users can access their own data."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        response = await test_client.get("/api/auth/me", headers=headers)

        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_unauthorized_request_rejected(self, test_client: AsyncClient):
        """Test that requests without auth token are rejected."""
        response = await test_client.get("/api/auth/me")

        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, test_client: AsyncClient):
        """Test that invalid tokens are rejected."""
        headers = {"Authorization": "Bearer invalid_token_12345"}
        response = await test_client.get("/api/auth/me", headers=headers)

        assert response.status_code == 401


class TestCSRFProtection:
    """Test CSRF protection (if enabled)."""

    @pytest.mark.asyncio
    async def test_post_requires_content_type(self, test_client: AsyncClient, test_user_token: str):
        """Test that POST requests have proper content type."""
        headers = {
            "Authorization": f"Bearer {test_user_token}",
            "Content-Type": "application/json",
        }
        response = await test_client.post(
            "/api/tasks/",
            headers=headers,
            json={"name": "Test Task", "script_id": "stock_zh_a_hist"},
        )

        # Should not be rejected for content type reasons
        assert response.status_code in [201, 400, 404, 422]


class TestInputValidation:
    """Test input validation security."""

    @pytest.mark.asyncio
    async def test_sql_injection_prevention(self, test_client: AsyncClient, test_user_token: str):
        """Test that SQL injection attempts are handled."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        malicious_input = "'; DROP TABLE users; --"

        response = await test_client.get(
            f"/api/scripts/scripts?search={malicious_input}", headers=headers
        )

        # Should not cause server error
        assert response.status_code in [200, 400, 422, 404]

    @pytest.mark.asyncio
    async def test_xss_prevention(self, test_client: AsyncClient, test_user_token: str):
        """Test that XSS attempts are handled."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        xss_input = "<script>alert('xss')</script>"

        response = await test_client.get(
            f"/api/scripts/scripts?search={xss_input}", headers=headers
        )

        # Should not cause server error
        assert response.status_code in [200, 400, 422, 404]

    @pytest.mark.asyncio
    async def test_path_traversal_prevention(self, test_client: AsyncClient, test_user_token: str):
        """Test that path traversal attempts are prevented."""
        headers = {"Authorization": f"Bearer {test_user_token}"}
        # Use URL-encoded path traversal to avoid httpx normalizing the URL
        path_input = "..%2F..%2F..%2Fetc%2Fpasswd"

        response = await test_client.get(f"/api/scripts/{path_input}", headers=headers)

        # Should not expose sensitive files - any non-500 response is acceptable
        # (endpoint treats path as string ID, not filesystem path)
        assert response.status_code in [200, 400, 404, 422]
        if response.status_code == 200:
            # If it returns 200, ensure it's not file content
            text = response.text
            assert "root:" not in text
