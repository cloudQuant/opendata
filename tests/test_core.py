"""
Core module tests.

Tests for core functionality including database and security.
"""

from datetime import UTC, datetime


class TestCoreSecurity:
    """Test core security functions."""

    def test_hash_password_function_exists(self):
        """Test hash_password function exists."""
        from opendata.core.security import hash_password

        assert callable(hash_password)

    def test_verify_password_function_exists(self):
        """Test verify_password function exists."""
        from opendata.core.security import verify_password

        assert callable(verify_password)

    def test_create_access_token_function_exists(self):
        """Test create_access_token function exists."""
        from opendata.core.security import create_access_token

        assert callable(create_access_token)

    def test_create_refresh_token_function_exists(self):
        """Test create_refresh_token function exists."""
        from opendata.core.security import create_refresh_token

        assert callable(create_refresh_token)

    def test_decode_token_function_exists(self):
        """Test decode_token function exists."""
        from opendata.core.security import decode_token

        assert callable(decode_token)

    def test_verify_token_function_exists(self):
        """Test verify_token function exists."""
        from opendata.core.security import verify_token

        assert callable(verify_token)

    def test_permission_checker_class_exists(self):
        """Test PermissionChecker class exists."""
        from opendata.core.security import PermissionChecker

        assert PermissionChecker is not None

    def test_get_password_hash(self):
        """Test getting password hash."""
        from opendata.core.security import hash_password

        password = "TestPassword123!"
        hashed = hash_password(password)

        assert hashed != password
        assert len(hashed) > 20
        # bcrypt hashes start with $2b$
        assert hashed.startswith("$2b$")

    def test_verify_correct_password(self):
        """Test verifying correct password."""
        from opendata.core.security import hash_password, verify_password

        password = "TestPassword123!"
        hashed = hash_password(password)

        assert verify_password(password, hashed) is True

    def test_verify_incorrect_password(self):
        """Test verifying incorrect password."""
        from opendata.core.security import hash_password, verify_password

        password = "TestPassword123!"
        wrong_password = "WrongPassword123!"
        hashed = hash_password(password)

        assert verify_password(wrong_password, hashed) is False

    def test_create_and_decode_access_token(self):
        """Test creating and decoding access token."""
        from opendata.core.security import create_access_token, decode_token

        data = {"sub": "123", "role": "user"}
        token = create_access_token(data)

        assert isinstance(token, str)
        assert len(token) > 0

        decoded = decode_token(token)
        assert decoded is not None
        assert decoded["sub"] == "123"
        assert decoded["role"] == "user"

    def test_create_and_decode_refresh_token(self):
        """Test creating and decoding refresh token."""
        from opendata.core.security import create_refresh_token, decode_token

        data = {"sub": "456", "role": "admin"}
        token = create_refresh_token(data)

        assert isinstance(token, str)
        assert len(token) > 0

        decoded = decode_token(token)
        assert decoded is not None
        assert decoded["sub"] == "456"

    def test_verify_token_valid(self):
        """Test verifying valid token."""
        from opendata.core.security import create_access_token, verify_token

        data = {"sub": "789"}
        token = create_access_token(data)

        payload = verify_token(token)
        assert payload is not None
        assert payload["sub"] == "789"

    def test_verify_token_invalid(self):
        """Test verifying invalid token."""
        from opendata.core.security import verify_token

        payload = verify_token("invalid_token_string")
        assert payload is None

    def test_verify_token_with_type(self):
        """Test verifying token with type check."""
        from opendata.core.security import create_access_token, create_refresh_token, verify_token

        access_data = {"sub": "123"}
        access_token = create_access_token(access_data)

        refresh_data = {"sub": "456"}
        refresh_token = create_refresh_token(refresh_data)

        # Verify access token as access
        access_payload = verify_token(access_token, "access")
        assert access_payload is not None
        assert access_payload["type"] == "access"

        # Verify refresh token as refresh
        refresh_payload = verify_token(refresh_token, "refresh")
        assert refresh_payload is not None
        assert refresh_payload["type"] == "refresh"

        # Verify access token as refresh should fail
        wrong_type = verify_token(access_token, "refresh")
        assert wrong_type is None

    def test_token_has_expiration(self):
        """Test token has expiration claim."""
        from opendata.core.security import create_access_token, decode_token

        data = {"sub": "test_user"}
        token = create_access_token(data)

        decoded = decode_token(token)
        assert "exp" in decoded

        # exp should be in the future
        exp_timestamp = decoded["exp"]
        now_timestamp = datetime.now(UTC).timestamp()
        assert exp_timestamp > now_timestamp

    def test_password_truncation_long_password(self):
        """Test that long passwords are truncated (bcrypt 72 byte limit)."""
        from opendata.core.security import hash_password, verify_password

        # Create a password longer than 72 bytes
        long_password = "a" * 100
        hashed = hash_password(long_password)

        # Should still verify correctly with the same long password
        assert verify_password(long_password, hashed) is True


class TestPermissionChecker:
    """Test PermissionChecker class."""

    def test_is_admin_with_string(self):
        """Test is_admin with string role."""
        from opendata.core.security import PermissionChecker

        assert PermissionChecker.is_admin("admin") is True
        assert PermissionChecker.is_admin("user") is False

    def test_is_admin_with_enum(self):
        """Test is_admin with UserRole enum."""
        from opendata.core.security import PermissionChecker
        from opendata.models.user import UserRole

        assert PermissionChecker.is_admin(UserRole.ADMIN) is True
        assert PermissionChecker.is_admin(UserRole.USER) is False

    def test_can_access_resource_admin(self):
        """Test admin can access any resource."""
        from opendata.core.security import PermissionChecker

        can_access = PermissionChecker.can_access_resource(
            user_role="admin", user_id=1, resource_owner_id=2
        )
        assert can_access is True

    def test_can_access_resource_owner(self):
        """Test user can access own resource."""
        from opendata.core.security import PermissionChecker

        can_access = PermissionChecker.can_access_resource(
            user_role="user", user_id=1, resource_owner_id=1
        )
        assert can_access is True

    def test_cannot_access_resource_other(self):
        """Test user cannot access other's resource."""
        from opendata.core.security import PermissionChecker

        can_access = PermissionChecker.can_access_resource(
            user_role="user", user_id=1, resource_owner_id=2
        )
        assert can_access is False

    def test_can_modify_user_admin(self):
        """Test admin can modify any user."""
        from opendata.core.security import PermissionChecker

        can_modify = PermissionChecker.can_modify_user(
            current_user_role="admin", current_user_id=1, target_user_id=2
        )
        assert can_modify is True

    def test_can_modify_self(self):
        """Test user can modify themselves."""
        from opendata.core.security import PermissionChecker

        can_modify = PermissionChecker.can_modify_user(
            current_user_role="user", current_user_id=1, target_user_id=1
        )
        assert can_modify is True

    def test_cannot_modify_other_user(self):
        """Test user cannot modify other user."""
        from opendata.core.security import PermissionChecker

        can_modify = PermissionChecker.can_modify_user(
            current_user_role="user", current_user_id=1, target_user_id=2
        )
        assert can_modify is False


class TestCoreDatabase:
    """Test core database functions."""

    def test_engine_exists(self):
        """Test database engine exists."""
        from opendata.core.database import engine

        assert engine is not None

    def test_base_exists(self):
        """Test Base metadata exists."""
        from opendata.core.database import Base

        assert Base is not None
        assert hasattr(Base, "metadata")

    def test_async_session_maker_exists(self):
        """Test async session maker exists."""
        from opendata.core.database import async_session_maker

        assert async_session_maker is not None

    def test_get_db_function_exists(self):
        """Test get_db function exists."""
        import inspect

        from opendata.core.database import get_db

        assert inspect.isasyncgenfunction(get_db)


class TestCoreConfig:
    """Test core configuration."""

    def test_settings_singleton(self):
        """Test settings is a singleton."""
        from opendata.core.config import settings

        assert settings is not None

    def test_settings_app_name(self):
        """Test app name setting."""
        from opendata.core.config import settings

        assert settings.app_name == "opendata"

    def test_settings_secret_key(self):
        """Test secret key setting."""
        from opendata.core.config import settings

        assert hasattr(settings, "secret_key")
        assert len(settings.secret_key) > 10

    def test_settings_database_url(self):
        """Test database URL setting."""
        from opendata.core.config import settings

        assert hasattr(settings, "database_url")
        assert "mysql" in settings.database_url or "sqlite" in settings.database_url

    def test_settings_jwt_settings(self):
        """Test JWT settings."""
        from opendata.core.config import settings

        assert hasattr(settings, "access_token_expire_minutes")
        assert hasattr(settings, "refresh_token_expire_days")
        assert settings.access_token_expire_minutes > 0
        assert settings.refresh_token_expire_days > 0

    def test_settings_algorithm(self):
        """Test JWT algorithm setting."""
        from opendata.core.config import settings

        assert settings.algorithm == "HS256"

    def test_settings_cors_origins(self):
        """Test CORS origins setting."""
        from opendata.core.config import settings

        assert hasattr(settings, "cors_origins")
        assert isinstance(settings.cors_origins, list)


class TestMainApp:
    """Test main application setup."""

    def test_app_exists(self):
        """Test FastAPI app exists."""
        from opendata.main import app

        assert app is not None

    def test_app_title(self):
        """Test app title."""
        from opendata.main import app

        assert "opendata" in app.title

    def test_app_has_routes(self):
        """Test app has routes."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        assert len(routes) > 0

    def test_app_has_cors_middleware(self):
        """Test app has CORS middleware."""
        from opendata.main import app

        # Check for CORS middleware in middleware stack
        has_cors = any("CORSMiddleware" in str(m) for m in app.user_middleware)
        assert has_cors
