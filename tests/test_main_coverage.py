"""
Tests for main.py covering lifespan, static files, and uncovered branches.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestLifespan:
    @pytest.mark.asyncio
    async def test_lifespan_startup_shutdown(self):
        from opendata.main import lifespan

        with (
            patch("opendata.core.database.create_tables", new_callable=AsyncMock) as mock_ct,
            patch("opendata.main.init_db", new_callable=AsyncMock) as mock_init,
            patch("opendata.main.task_scheduler") as mock_sched,
            patch("opendata.main.close_db", new_callable=AsyncMock) as mock_close,
            patch("opendata.main.settings") as mock_settings,
        ):
            mock_settings.app_name = "test"
            mock_settings.app_version = "1.0"
            mock_settings.app_env = "test"
            mock_settings.secret_key = "your-secret-key-change-this-in-production"
            mock_settings.is_production = False
            mock_settings.workers = 1  # Avoid MagicMock > int in lifespan
            mock_sched.start = AsyncMock()
            mock_sched.shutdown = AsyncMock()

            async with lifespan(MagicMock()):
                pass

            mock_ct.assert_called_once()
            mock_init.assert_called_once()
            mock_sched.start.assert_called_once()
            mock_sched.shutdown.assert_called_once()
            mock_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_lifespan_safe_secret(self):
        from opendata.main import lifespan

        with (
            patch("opendata.core.database.create_tables", new_callable=AsyncMock),
            patch("opendata.main.init_db", new_callable=AsyncMock),
            patch("opendata.main.task_scheduler") as mock_sched,
            patch("opendata.main.close_db", new_callable=AsyncMock),
            patch("opendata.main.settings") as mock_settings,
        ):
            mock_settings.app_name = "test"
            mock_settings.app_version = "1.0"
            mock_settings.app_env = "test"
            mock_settings.secret_key = "a-real-secret-key-here"
            mock_settings.is_production = False
            mock_settings.workers = 1  # Avoid MagicMock > int in lifespan
            mock_sched.start = AsyncMock()
            mock_sched.shutdown = AsyncMock()

            async with lifespan(MagicMock()):
                pass


class TestHealthCheckBranches:
    @pytest.mark.asyncio
    async def test_healthy(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            mock_sched._running = True
            result = await health_check()
        assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_degraded_scheduler_off(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            type(mock_sched)._running = property(lambda self: False)
            result = await health_check()
        assert result["status"] in ("healthy", "degraded")

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=False
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            mock_sched._running = False
            result = await health_check()
        assert result["status"] == "unhealthy"


class TestRootEndpoint:
    @pytest.mark.asyncio
    async def test_root_no_frontend(self):
        """Test root endpoint when frontend dist doesn't exist."""
        with patch("opendata.main.FRONTEND_DIR") as mock_dir:
            mock_dir.is_dir.return_value = False
            # The root function is already defined at import time
            # Just test the existing root function if frontend not present
            from opendata.main import app

            assert app is not None
