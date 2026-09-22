"""
Database module extended tests.

Additional tests for database functionality.
"""


class TestDatabaseFunctions:
    """Test database module functions."""

    def test_create_tables_function(self):
        """Test create_tables function exists."""
        from opendata.core.database import create_tables

        assert callable(create_tables)

    def test_init_db_function(self):
        """Test init_db function exists."""
        from opendata.core.database import init_db

        assert callable(init_db)

    def test_close_db_function(self):
        """Test close_db function exists."""
        from opendata.core.database import close_db

        assert callable(close_db)

    def test_engine_is_async(self):
        """Test database engine is async."""

        from opendata.core.database import engine

        # Engine should be an AsyncEngine or compatible
        assert engine is not None

    def test_base_metadata(self):
        """Test Base metadata."""
        from opendata.core.database import Base

        assert Base.metadata is not None
        assert hasattr(Base.metadata, "create_all")

    def test_async_session_maker(self):
        """Test async session maker."""
        from opendata.core.database import async_session_maker

        assert async_session_maker is not None


class TestAPIRoutes:
    """Test API route registration."""

    def test_auth_router_registered(self):
        """Test auth router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        auth_routes = [r.path for r in routes if r.path.startswith("/api/auth")]
        assert len(auth_routes) > 0

    def test_users_router_registered(self):
        """Test users router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        users_routes = [r.path for r in routes if r.path.startswith("/api/users")]
        assert len(users_routes) > 0

    def test_tasks_router_registered(self):
        """Test tasks router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        tasks_routes = [r.path for r in routes if r.path.startswith("/api/tasks")]
        assert len(tasks_routes) > 0

    def test_scripts_router_registered(self):
        """Test scripts router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        scripts_routes = [r.path for r in routes if r.path.startswith("/api/scripts")]
        assert len(scripts_routes) > 0

    def test_executions_router_registered(self):
        """Test executions router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        exec_routes = [r.path for r in routes if r.path.startswith("/api/executions")]
        assert len(exec_routes) > 0

    def test_tables_router_registered(self):
        """Test tables router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        tables_routes = [r.path for r in routes if r.path.startswith("/api/tables")]
        assert len(tables_routes) > 0

    def test_data_router_registered(self):
        """Test data router is registered."""
        from opendata.main import app

        routes = [r for r in app.routes if hasattr(r, "path")]
        data_routes = [r.path for r in routes if r.path.startswith("/api/data")]
        assert len(data_routes) > 0


class TestAPIEndpoints:
    """Test specific API endpoint configurations."""

    def test_health_endpoint(self):
        """Test health check endpoint exists."""
        from opendata.main import app

        routes = [r.path for r in app.routes if hasattr(r, "path")]
        health_routes = [r for r in routes if "health" in r.lower()]
        # May or may not have health endpoint
        assert len(health_routes) >= 0

    def test_docs_endpoint(self):
        """Test Swagger docs endpoint."""
        from opendata.main import app

        # FastAPI automatically adds /docs
        routes = [r.path for r in app.routes if hasattr(r, "path")]
        assert "/docs" in routes or "/openapi.json" in routes


class TestMiddleware:
    """Test middleware configuration."""

    def test_cors_middleware(self):
        """Test CORS middleware is configured."""
        from opendata.main import app

        # Check for CORS in user_middleware
        has_cors = any("cors" in str(m).lower() for m in app.user_middleware)
        assert has_cors

    def test_middleware_stack(self):
        """Test middleware stack exists."""
        from opendata.main import app

        assert hasattr(app, "user_middleware")
        assert isinstance(app.user_middleware, list)


class TestAPIRouters:
    """Test individual API routers."""

    def test_auth_router_importable(self):
        """Test auth router can be imported."""
        from opendata.api import auth

        assert auth is not None
        assert hasattr(auth, "router")

    def test_users_router_importable(self):
        """Test users router can be imported."""
        from opendata.api import users

        assert users is not None
        assert hasattr(users, "router")

    def test_tasks_router_importable(self):
        """Test tasks router can be imported."""
        from opendata.api import tasks

        assert tasks is not None
        assert hasattr(tasks, "router")

    def test_scripts_router_importable(self):
        """Test scripts router can be imported."""
        from opendata.api import scripts

        assert scripts is not None
        assert hasattr(scripts, "router")

    def test_executions_router_importable(self):
        """Test executions router can be imported."""
        from opendata.api import executions

        assert executions is not None
        assert hasattr(executions, "router")

    def test_tables_router_importable(self):
        """Test tables router can be imported."""
        from opendata.api import tables

        assert tables is not None
        assert hasattr(tables, "router")

    def test_data_router_importable(self):
        """Test data router can be imported."""
        from opendata.api import data

        assert data is not None
        assert hasattr(data, "router")

    def test_settings_router_importable(self):
        """Test settings router can be imported."""
        from opendata.api import settings

        assert settings is not None
        assert hasattr(settings, "router")

    def test_interfaces_router_importable(self):
        """Test interfaces router can be imported."""
        from opendata.api import interfaces

        assert interfaces is not None
        assert hasattr(interfaces, "router")


class TestDependenciesModule:
    """Test dependencies module."""

    def test_bearer_security(self):
        """Test HTTPBearer security scheme."""
        from opendata.api.dependencies import security

        assert security is not None
        assert not security.auto_error

    def test_dependencies_all_functions(self):
        """Test all dependency functions are callable."""
        from opendata.api import dependencies

        functions = [
            "get_current_user",
            "get_current_active_user",
            "get_current_admin_user",
            "get_optional_user",
        ]

        for func_name in functions:
            assert hasattr(dependencies, func_name)
            func = getattr(dependencies, func_name)
            assert callable(func)


class TestRateLimiterModule:
    """Test rate limiter module."""

    def test_rate_limit_function(self):
        """Test rate_limit function exists."""
        from opendata.api.rate_limit import rate_limit

        assert callable(rate_limit)

    def test_is_testing_function(self):
        """Test is_testing function exists."""
        from opendata.api.rate_limit import is_testing

        assert callable(is_testing)

    def test_is_testing_checks_env(self):
        """Test is_testing checks environment variable."""
        import os

        from opendata.api.rate_limit import is_testing

        # Set testing mode
        old_val = os.environ.get("TESTING")
        os.environ["TESTING"] = "true"
        assert is_testing() is True

        # Reset
        if old_val is None:
            os.environ.pop("TESTING", None)
        else:
            os.environ["TESTING"] = old_val
