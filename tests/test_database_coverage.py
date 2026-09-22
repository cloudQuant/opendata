"""
Tests for database.py covering get_data_db, get_db_context, init_db,
create_tables, close_db, check_db_connection (lines 94-176).
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestGetDataDb:
    @pytest.mark.asyncio
    async def test_normal_flow(self):
        mock_session = AsyncMock()

        with patch("opendata.core.database.data_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import get_data_db

            gen = get_data_db()
            session = await gen.__anext__()
            assert session is mock_session

    @pytest.mark.asyncio
    async def test_exception_rollback(self):
        mock_session = AsyncMock()

        with patch("opendata.core.database.data_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import get_data_db

            gen = get_data_db()
            session = await gen.__anext__()
            # Simulate exception during yield
            with contextlib.suppress(RuntimeError):
                await gen.athrow(RuntimeError("test"))
            mock_session.rollback.assert_called()


class TestGetDbContext:
    @pytest.mark.asyncio
    async def test_normal_flow(self):
        mock_session = AsyncMock()

        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import get_db_context

            async with get_db_context() as session:
                assert session is mock_session
            mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_exception_rollback(self):
        mock_session = AsyncMock()

        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import get_db_context

            with pytest.raises(RuntimeError):
                async with get_db_context() as session:
                    raise RuntimeError("test")
            mock_session.rollback.assert_called()


class TestInitDb:
    @pytest.mark.asyncio
    async def test_already_initialized(self):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()  # admin exists
        mock_session.execute.return_value = mock_result

        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import init_db

            await init_db()
            # Should return early without adding anything
            mock_session.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_fresh_init(self):
        mock_session = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # no admin
        mock_session.execute.return_value = mock_result

        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import init_db

            await init_db()
            mock_session.add.assert_called_once()  # admin user
            mock_session.add_all.assert_called_once()  # categories
            mock_session.commit.assert_called()


class TestCreateTables:
    @pytest.mark.asyncio
    async def test_create_tables(self):
        mock_conn = AsyncMock()

        with patch("opendata.core.database.engine") as mock_engine:
            mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=mock_conn)
            mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import create_tables

            await create_tables()
            mock_conn.run_sync.assert_called_once()


class TestCloseDb:
    @pytest.mark.asyncio
    async def test_close_db(self):
        with (
            patch("opendata.core.database.engine") as mock_engine,
            patch("opendata.core.database.data_engine") as mock_data_engine,
        ):
            mock_engine.dispose = AsyncMock()
            mock_data_engine.dispose = AsyncMock()

            from opendata.core.database import close_db

            await close_db()
            mock_engine.dispose.assert_called_once()
            mock_data_engine.dispose.assert_called_once()


class TestCheckDbConnection:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_session = AsyncMock()

        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import check_db_connection

            result = await check_db_connection()
        assert result is True

    @pytest.mark.asyncio
    async def test_failure(self):
        with patch("opendata.core.database.async_session_maker") as mock_sm:
            mock_sm.return_value.__aenter__ = AsyncMock(side_effect=RuntimeError("fail"))
            mock_sm.return_value.__aexit__ = AsyncMock(return_value=False)

            from opendata.core.database import check_db_connection

            result = await check_db_connection()
        assert result is False
