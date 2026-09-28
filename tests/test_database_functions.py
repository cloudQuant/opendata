"""
Database functions tests.

Tests for database module functions to improve coverage.
"""

import ast
from pathlib import Path
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


def _is_create_tables_call(node: ast.AST) -> bool:
    """Whether this node is a bare ``create_tables(...)`` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "create_tables"
    )


def _calls_create_tables(nodes: list[ast.stmt] | ast.AST) -> bool:
    """Whether a create_tables call sits anywhere inside these statements."""
    roots = nodes if isinstance(nodes, list) else [nodes]
    return any(_is_create_tables_call(node) for root in roots for node in ast.walk(root))


class TestWarehouseDdlOwnership:
    """AC-8|02: the warehouse DDL belongs to ``alembic_data``, and startup cannot reach it."""

    def test_control_metadata_names_no_warehouse_table(self):
        """``create_all`` can only build what is in ``Base.metadata`` - and the warehouse isn't."""
        from opendata.core.database import Base
        from opendata.data.providers import register_providers
        from opendata.pipeline.alert_matrix import warehouse_tables

        register_providers()
        warehouse = set(warehouse_tables())
        assert warehouse, "仓库表注册表读空了 —— 分离面无从谈起"
        assert any(name.startswith("ods_") for name in warehouse), "census 里没有 ods 表"
        assert sorted(set(Base.metadata.tables) & warehouse) == []
        assert [n for n in Base.metadata.tables if n.startswith(("ods_", "dwd_"))] == []

    def test_the_warehouse_has_its_own_alembic_environment(self):
        """Two configs, two script locations, two version tables, one warehouse URL."""
        import re

        root = Path(__file__).resolve().parent.parent
        location = {
            name: re.search(
                r"^script_location = (.+)$", (root / name).read_text(encoding="utf-8"), re.M
            )
            .group(1)
            .strip()
            for name in ("alembic.ini", "alembic_data.ini")
        }
        assert location["alembic.ini"] != location["alembic_data.ini"]

        warehouse_env = (root / location["alembic_data.ini"] / "env.py").read_text("utf-8")
        control_env = (root / location["alembic.ini"] / "env.py").read_text("utf-8")

        # The warehouse chain renders its DDL from opendata/pipeline/ddl.py, so there is
        # nothing to autogenerate against -- and no ORM metadata to leak control tables in.
        assert "target_metadata = None" in warehouse_env
        assert "settings.data_database_url" in warehouse_env
        assert 'VERSION_TABLE = "alembic_version_data"' in warehouse_env
        assert "data_database_url" not in control_env

    @pytest.mark.asyncio
    async def test_create_tables_never_touches_the_warehouse_engine(self):
        """The warehouse engine is a bomb here: any DDL against it fails the test."""

        class _WarehouseBomb:
            def begin(self):
                raise AssertionError("启动建表打到了仓库 engine")

            def connect(self):
                raise AssertionError("启动建表打到了仓库 engine")

        from opendata.core.database import Base, create_tables

        connection = AsyncMock()
        control = MagicMock()
        control.begin.return_value.__aenter__ = AsyncMock(return_value=connection)
        control.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("opendata.core.database.engine", control),
            patch("opendata.core.database.data_engine", _WarehouseBomb()),
        ):
            assert await create_tables() is None

        control.begin.assert_called_once()
        connection.run_sync.assert_awaited_once_with(Base.metadata.create_all)

    def test_startup_creates_tables_only_off_the_production_branch(self):
        """One startup call site for ``create_tables``, guarded off the production branch."""
        import opendata.main as main_module

        tree = ast.parse(Path(main_module.__file__).read_text(encoding="utf-8"))
        lifespan = next(
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
        )
        calls = [node for node in ast.walk(lifespan) if _is_create_tables_call(node)]
        assert len(calls) == 1, "启动建表的调用点数必须唯一，否则「生产不建表」无保障"

        guards = [
            node
            for node in ast.walk(lifespan)
            if isinstance(node, ast.If) and "is_production" in ast.unparse(node.test)
        ]
        assert len(guards) == 1
        guard = guards[0]
        assert _calls_create_tables(guard.orelse), "生产分支之外才该建表"
        assert not _calls_create_tables(guard.body)
