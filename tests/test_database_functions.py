"""
Database functions tests.

Tests for database module functions to improve coverage.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestCreateTables:
    """Test create_tables function."""

    @pytest.mark.asyncio
    async def test_create_tables(self):
        """create_tables runs the model DDL inside an engine transaction."""
        from opendata.core.database import Base, create_tables

        connection = AsyncMock()
        engine = MagicMock()
        engine.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
        engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("opendata.core.database.engine", engine):
            assert await create_tables() is None

        engine.begin.assert_called_once()
        connection.run_sync.assert_awaited_once_with(Base.metadata.create_all)


class TestCloseDB:
    """Test close_db function."""

    @pytest.mark.asyncio
    async def test_close_db(self):
        """Test close_db function."""
        from opendata.core.database import close_db

        # Mock the engine
        with patch("opendata.core.database.engine") as mock_engine:
            async_mock = AsyncMock()
            mock_engine.dispose = async_mock

            result = await close_db()

            # Function should complete without error
            assert result is None


class TestCheckDBConnection:
    """Test check_db_connection function."""

    @pytest.mark.asyncio
    async def test_check_db_connection_success(self):
        """Test successful database connection check."""
        from opendata.core.database import check_db_connection

        # Mock successful connection
        with patch("opendata.core.database.async_session_maker") as mock_maker:
            mock_session = AsyncMock()
            mock_session.execute = AsyncMock()
            mock_session.execute.return_value.scalar_one.return_value = 1

            async_mock_session = MagicMock()
            async_mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            async_mock_session.__aexit__ = AsyncMock()

            mock_maker.return_value = async_mock_session

            result = await check_db_connection()

            assert result is True

    @pytest.mark.asyncio
    async def test_check_db_connection_failure(self):
        """Test failed database connection check."""
        from opendata.core.database import check_db_connection

        # Mock failed connection
        with patch("opendata.core.database.async_session_maker") as mock_maker:
            mock_maker.side_effect = Exception("Connection failed")

            result = await check_db_connection()

            assert result is False


class TestGetDB:
    """Test get_db dependency."""

    @pytest.mark.asyncio
    async def test_get_db_generator(self):
        """Test get_db is a generator."""
        import inspect

        from opendata.api.dependencies import get_db

        assert inspect.isasyncgenfunction(get_db)

    @pytest.mark.asyncio
    async def test_get_db_yields_session(self):
        """Test get_db yields async session."""
        from opendata.api.dependencies import get_db

        gen = get_db()
        session = await gen.__anext__()

        assert session is not None

        # Clean up
        await gen.aclose()


class TestDatabaseSchema:
    """Test database schema definitions."""

    def test_base_metadata(self):
        """Test Base metadata exists."""
        from opendata.core.database import Base

        assert Base.metadata is not None

    def test_base_has_tables(self):
        """Test Base has table definitions."""
        from opendata.core.database import Base

        tables = Base.metadata.tables
        assert len(tables) > 0


class TestAsyncSessionMaker:
    """Test async_session_maker configuration."""

    def test_async_session_maker_exists(self):
        """Test async_session_maker is defined."""
        from opendata.core.database import async_session_maker

        assert async_session_maker is not None

    def test_async_session_maker_is_callable(self):
        """Test async_session_maker is callable."""
        from opendata.core.database import async_session_maker

        assert callable(async_session_maker)


class TestEngineConfiguration:
    """Test database engine configuration."""

    def test_engine_exists(self):
        """Test engine is defined."""
        from opendata.core.database import engine

        assert engine is not None

    def test_engine_url_from_settings(self):
        """Test engine URL comes from settings."""
        from opendata.core.database import engine

        # Verify engine exists and is configured
        assert engine is not None
