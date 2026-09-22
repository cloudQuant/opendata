"""
Security module full coverage tests.

Tests for app.core.security to achieve 100% coverage.
"""

from datetime import timedelta


class TestHashPassword:
    """Test hash_password function."""

    def test_hash_password(self):
        """Test password hashing."""
        from opendata.core.security import hash_password

        hashed = hash_password("test_password")

        assert hashed is not None
        assert isinstance(hashed, str)
        assert hashed != "test_password"

    def test_hash_password_different_results(self):
        """Test hashing same password twice gives different hashes."""
        from opendata.core.security import hash_password

        hash1 = hash_password("test_password")
        hash2 = hash_password("test_password")

        # Due to bcrypt salt, hashes should be different
        assert hash1 != hash2


class TestVerifyPassword:
    """Test verify_password function."""

    def test_verify_password_correct(self):
        """Test verifying correct password."""
        from opendata.core.security import hash_password, verify_password

        hashed = hash_password("test_password")
        result = verify_password("test_password", hashed)

        assert result is True

    def test_verify_password_incorrect(self):
        """Test verifying incorrect password."""
        from opendata.core.security import hash_password, verify_password

        hashed = hash_password("test_password")
        result = verify_password("wrong_password", hashed)

        assert result is False


class TestCreateAccessToken:
    """Test create_access_token function."""

    def test_create_access_token_default_expiry(self):
        """Test creating access token with default expiry."""
        from opendata.core.security import create_access_token

        token = create_access_token({"sub": "user123"})

        assert token is not None
        assert isinstance(token, str)

    def test_create_access_token_custom_expiry(self):
        """Test creating access token with custom expiry."""
        from opendata.core.security import create_access_token

        token = create_access_token({"sub": "user123"}, expires_delta=timedelta(hours=1))

        assert token is not None
        assert isinstance(token, str)


class TestCreateRefreshToken:
    """Test create_refresh_token function."""

    def test_create_refresh_token_default_expiry(self):
        """Test creating refresh token with default expiry."""
        from opendata.core.security import create_refresh_token

        token = create_refresh_token({"sub": "user123"})

        assert token is not None
        assert isinstance(token, str)

    def test_create_refresh_token_custom_expiry(self):
        """Test creating refresh token with custom expiry."""
        from opendata.core.security import create_refresh_token

        token = create_refresh_token({"sub": "user123"}, expires_delta=timedelta(days=30))

        assert token is not None
        assert isinstance(token, str)


class TestDecodeToken:
    """Test decode_token function."""

    def test_decode_token_valid(self):
        """Test decoding a valid token."""
        from opendata.core.security import create_access_token, decode_token

        token = create_access_token({"sub": "user123"})
        payload = decode_token(token)

        assert payload is not None
        assert payload["sub"] == "user123"

    def test_decode_token_invalid(self):
        """Test decoding an invalid token."""
        from opendata.core.security import decode_token

        payload = decode_token("invalid_token")

        assert payload is None

    def test_decode_token_expired(self):
        """Test decoding an expired token."""
        from opendata.core.security import create_access_token, decode_token

        # Create token that's already expired
        token = create_access_token({"sub": "user123"}, expires_delta=timedelta(seconds=-1))

        payload = decode_token(token)

        assert payload is None


class TestVerifyToken:
    """Test verify_token function."""

    def test_verify_access_token_valid(self):
        """Test verifying a valid access token."""
        from opendata.core.security import create_access_token, verify_token

        token = create_access_token({"sub": "user123"})
        payload = verify_token(token, "access")

        assert payload is not None
        assert payload["sub"] == "user123"
        assert payload["type"] == "access"

    def test_verify_refresh_token_valid(self):
        """Test verifying a valid refresh token."""
        from opendata.core.security import create_refresh_token, verify_token

        token = create_refresh_token({"sub": "user123"})
        payload = verify_token(token, "refresh")

        assert payload is not None
        assert payload["sub"] == "user123"
        assert payload["type"] == "refresh"

    def test_verify_token_wrong_type(self):
        """Test verifying token with wrong type."""
        from opendata.core.security import create_access_token, verify_token

        token = create_access_token({"sub": "user123"})
        payload = verify_token(token, "refresh")

        # Should return None because type doesn't match
        assert payload is None

    def test_verify_token_invalid(self):
        """Test verifying an invalid token."""
        from opendata.core.security import verify_token

        payload = verify_token("invalid_token", "access")

        assert payload is None
