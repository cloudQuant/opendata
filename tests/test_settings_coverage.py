"""
Tests for settings.py covering test_database_connection with aiomysql and fallback paths.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opendata.api.settings import TestConnectionRequest, test_database_connection


@pytest.fixture
def admin_user():
    user = MagicMock()
    user.role = "admin"
    return user


@pytest.fixture
def db():
    return AsyncMock()


@pytest.fixture
def req():
    return TestConnectionRequest(
        host="localhost", port=3306, database="testdb", user="root", password="pass"
    )


class TestDatabaseConnection:
    def _make_aiomysql_mock(self, fetchone_result):
        import types

        mock_aiomysql = types.ModuleType("aiomysql")
        mock_cursor = AsyncMock()
        mock_cursor.execute = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=fetchone_result)

        # cursor() must return a sync object that supports async context manager
        class FakeCursorCtx:
            async def __aenter__(self_inner):
                return mock_cursor

            async def __aexit__(self_inner, *args):
                pass

        mock_conn = MagicMock()
        mock_conn.cursor.return_value = FakeCursorCtx()
        mock_conn.close = AsyncMock()

        mock_aiomysql.connect = AsyncMock(return_value=mock_conn)
        return mock_aiomysql

    @pytest.mark.asyncio
    async def test_aiomysql_success(self, req, admin_user, db):
        import sys

        mock_aiomysql = self._make_aiomysql_mock((1,))
        with patch.dict(sys.modules, {"aiomysql": mock_aiomysql}):
            result = await test_database_connection(req, admin_user, db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_aiomysql_bad_result(self, req, admin_user, db):
        import sys

        mock_aiomysql = self._make_aiomysql_mock((0,))

        with patch.dict(sys.modules, {"aiomysql": mock_aiomysql}):
            result = await test_database_connection(req, admin_user, db)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_import_error_fallback_success(self, req, admin_user, db):
        """When aiomysql not available, fall back to SQLAlchemy."""
        import builtins
        import sys

        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "aiomysql":
                raise ImportError("No module")
            return real_import(name, *args, **kwargs)

        mock_engine = MagicMock()
        mock_conn_ctx = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn_ctx)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        # Remove aiomysql from sys.modules to force re-import
        with (
            patch.dict(sys.modules, {"aiomysql": None}),
            patch("builtins.__import__", side_effect=mock_import),
            patch("sqlalchemy.create_engine", return_value=mock_engine),
        ):
            result = await test_database_connection(req, admin_user, db)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_general_exception(self, req, admin_user, db):
        import sys
        import types

        mock_aiomysql = types.ModuleType("aiomysql")
        mock_aiomysql.connect = AsyncMock(side_effect=ConnectionError("refused"))

        with patch.dict(sys.modules, {"aiomysql": mock_aiomysql}):
            result = await test_database_connection(req, admin_user, db)
        assert result.success is False
        assert "refused" in result.message
