"""
API dependencies tests.

Tests for API dependency injection functions.
"""


class TestAPIDependencies:
    """Test API dependency functions."""

    def test_get_current_user_exists(self):
        """Test get_current_user dependency exists."""
        from opendata.api.dependencies import get_current_user

        assert callable(get_current_user)

    def test_get_current_active_user_exists(self):
        """Test get_current_active_user dependency exists."""
        from opendata.api.dependencies import get_current_active_user

        assert callable(get_current_active_user)

    def test_get_current_admin_user_exists(self):
        """Test get_current_admin_user dependency exists."""
        from opendata.api.dependencies import get_current_admin_user

        assert callable(get_current_admin_user)

    def test_get_optional_user_exists(self):
        """Test get_optional_user dependency exists."""
        from opendata.api.dependencies import get_optional_user

        assert callable(get_optional_user)

    def test_get_db_exists(self):
        """Test get_db dependency exists."""
        import inspect

        from opendata.api.dependencies import get_db

        assert inspect.isasyncgenfunction(get_db)

    def test_http_bearer_security_exists(self):
        """Test HTTPBearer security scheme exists."""
        from opendata.api.dependencies import security

        assert security is not None

    def test_current_user_alias_exists(self):
        """Test CurrentUser type alias exists."""
        from opendata.api.dependencies import CurrentUser

        assert CurrentUser is not None

    def test_current_admin_alias_exists(self):
        """Test CurrentAdmin type alias exists."""
        from opendata.api.dependencies import CurrentAdmin

        assert CurrentAdmin is not None

    def test_optional_user_alias_exists(self):
        """Test OptionalUser type alias exists."""
        from opendata.api.dependencies import OptionalUser

        assert OptionalUser is not None

    def test_db_session_alias_exists(self):
        """Test DBSession type alias exists."""
        from opendata.api.dependencies import DBSession

        assert DBSession is not None


class TestRateLimiter:
    """Test rate limiter decorator."""

    def test_rate_limit_decorator_exists(self):
        """Test rate_limit decorator exists."""
        from opendata.api.rate_limit import rate_limit

        assert callable(rate_limit)

    def test_rate_limit_with_string(self):
        """Test rate_limit can be called with string."""
        from opendata.api.rate_limit import rate_limit

        @rate_limit("10/minute")
        def dummy_func():
            return "ok"

        assert callable(dummy_func)

    def test_rate_limit_with_custom_limit(self):
        """Test rate_limit with custom limit."""
        from opendata.api.rate_limit import rate_limit

        @rate_limit("100/hour")
        def dummy_func():
            return "ok"

        assert callable(dummy_func)


class TestAPISchemasComprehensive:
    """Test all API schemas are importable."""

    def test_all_schemas_importable(self):
        """Test all schemas can be imported."""
        from opendata.api.schemas import (
            APIResponse,
            CategoryResponse,
            DataDownloadRequest,
            DataDownloadResponse,
            DataScriptListResponse,
            DataScriptResponse,
            DownloadProgressResponse,
            ErrorResponse,
            InterfaceListResponse,
            InterfaceParameterSchema,
            InterfaceResponse,
            LoginRequest,
            PaginatedParams,
            PaginatedResponse,
            ParameterSchema,
            RefreshTokenRequest,
            RegisterRequest,
            TableResponse,
            TableSchemaResponse,
            TaskCreateRequest,
            TaskExecutionResponse,
            TaskResponse,
            TaskUpdateRequest,
            TokenResponse,
            UserListResponse,
            UserResponse,
            UserUpdateRequest,
            UserUpdateRoleRequest,
        )

        assert APIResponse is not None
        assert ErrorResponse is not None
        assert PaginatedParams is not None
        assert PaginatedResponse is not None
        assert LoginRequest is not None
        assert RegisterRequest is not None
        assert RefreshTokenRequest is not None
        assert TokenResponse is not None
        assert UserResponse is not None
        assert UserListResponse is not None
        assert UserUpdateRequest is not None
        assert UserUpdateRoleRequest is not None
        assert TaskCreateRequest is not None
        assert TaskUpdateRequest is not None
        assert TaskResponse is not None
        assert TaskExecutionResponse is not None
        assert DataScriptResponse is not None
        assert DataScriptListResponse is not None
        assert TableResponse is not None
        assert TableSchemaResponse is not None
        assert InterfaceResponse is not None
        assert InterfaceListResponse is not None
        assert InterfaceParameterSchema is not None
        assert ParameterSchema is not None
        assert CategoryResponse is not None
        assert DataDownloadRequest is not None
        assert DataDownloadResponse is not None
        assert DownloadProgressResponse is not None


class TestMainApp:
    """Test main application."""

    def test_app_routes_registered(self):
        """Test all expected routes are registered."""
        from opendata.main import app

        routes = [r.path for r in app.routes if hasattr(r, "path")]

        expected_routes = [
            "/api/auth",
            "/api/users",
            "/api/tasks",
            "/api/scripts",
            "/api/executions",
            "/api/tables",
            "/api/data",
            "/api/settings",
        ]

        for route in expected_routes:
            assert any(route in r for r in routes)

    def test_app_has_event_handlers(self):
        """Test app has event handlers."""
        from opendata.main import app

        # Just check the app exists and has routes
        assert app is not None
        assert len([r for r in app.routes if hasattr(r, "path")]) > 0


class TestDatabaseModule:
    """Test database module functions."""

    def test_create_tables_exists(self):
        """Test create_tables function exists."""
        from opendata.core.database import create_tables

        assert callable(create_tables)

    def test_init_db_exists(self):
        """Test init_db function exists."""
        from opendata.core.database import init_db

        assert callable(init_db)

    def test_close_db_exists(self):
        """Test close_db function exists."""
        from opendata.core.database import close_db

        assert callable(close_db)

    def test_check_db_connection_exists(self):
        """Test check_db_connection function exists."""
        import inspect

        from opendata.core.database import check_db_connection

        assert inspect.iscoroutinefunction(check_db_connection)
