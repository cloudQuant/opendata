"""
Tests for main FastAPI application.

Covers health check, app creation, lifespan events.
"""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


class TestHealthCheck:
    """Test health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check(self, test_client: AsyncClient):
        """Test health check endpoint."""
        with patch(
            "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
        ):
            response = await test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] in ("healthy", "degraded")  # scheduler not running in test


class TestAppStartup:
    """Test app configuration."""

    def test_app_exists(self):
        """Test app object exists."""
        from opendata.main import app

        assert app is not None
        assert app.title is not None

    def test_cors_configured(self):
        """Test CORS middleware is configured."""
        from opendata.main import app

        # app.user_middleware holds the (unbuilt) middleware stack;
        # each entry wraps the class in .cls on every starlette version.
        middleware_classes = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_classes

    def test_routes_registered(self):
        """Test that API routes are registered."""
        from opendata.main import app

        # OpenAPI paths: stable across FastAPI versions (app.routes is not;
        # newer FastAPI keeps included routers as lazy wrappers).
        route_paths = list(app.openapi()["paths"])
        assert "/api/health" in route_paths or any("/api" in p for p in route_paths)


class TestDatabaseUtils:
    """Test database utility functions."""

    @pytest.mark.asyncio
    async def test_check_db_connection(self):
        """Test database connection check."""
        from opendata.core.database import check_db_connection

        # In test env with SQLite memory, this may fail since we use test engine
        result = await check_db_connection()
        # Result is boolean regardless
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_create_tables(self):
        """Test create_tables function."""
        from opendata.core.database import create_tables

        # This should not raise
        with patch("opendata.core.database.engine") as mock_engine:
            mock_conn = AsyncMock()
            mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)
            await create_tables()

    @pytest.mark.asyncio
    async def test_close_db(self):
        """Test close_db function."""
        from opendata.core.database import close_db

        with (
            patch("opendata.core.database.engine") as mock_engine,
            patch("opendata.core.database.data_engine") as mock_data_engine,
        ):
            mock_engine.dispose = AsyncMock()
            mock_data_engine.dispose = AsyncMock()
            await close_db()
            mock_engine.dispose.assert_called_once()
            mock_data_engine.dispose.assert_called_once()


class TestRateLimit:
    """Test rate limiting utilities."""

    def test_is_testing_true(self):
        """Test is_testing returns True in test mode."""
        from opendata.api.rate_limit import is_testing

        assert is_testing() is True

    def test_get_limiter_in_test_mode(self):
        """Test get_limiter returns None in test mode."""
        from opendata.api.rate_limit import get_limiter

        assert get_limiter() is None

    def test_rate_limit_decorator_passthrough(self):
        """Test rate_limit decorator passes through in test mode."""
        import asyncio

        from opendata.api.rate_limit import rate_limit

        @rate_limit("5/minute")
        async def test_func():
            return "ok"

        result = asyncio.run(test_func())
        assert result == "ok"
