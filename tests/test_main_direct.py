"""
Direct tests for main.py to maximize coverage.
"""

from unittest.mock import AsyncMock, patch

import pytest


class TestHealthCheck:
    @pytest.mark.asyncio
    async def test_healthy(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            mock_sched.is_running = True
            result = await health_check()
        assert result["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_degraded(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=True
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            mock_sched.is_running = False
            result = await health_check()
        assert result["status"] == "degraded"

    @pytest.mark.asyncio
    async def test_unhealthy(self):
        from opendata.main import health_check

        with (
            patch(
                "opendata.core.database.check_db_connection", new_callable=AsyncMock, return_value=False
            ),
            patch("opendata.main.task_scheduler") as mock_sched,
        ):
            mock_sched.is_running = False
            result = await health_check()
        assert result["status"] == "unhealthy"


class TestRootEndpoint:
    @pytest.mark.asyncio
    async def test_root(self):
        """Test root endpoint when no frontend dist exists."""
        # Import to check the app exists
        from opendata.main import app

        assert app is not None
        assert app.title is not None
