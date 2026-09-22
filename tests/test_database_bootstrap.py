"""Behaviour tests for database bootstrap (``init_db``).

``init_db`` runs on every application startup, so it must be idempotent: a
second run against an already-seeded database must be a no-op, not an
IntegrityError on the unique ``interface_categories.name`` index.

The module-level session factory is replaced with a temporary SQLite database so
these tests never touch the configured MySQL instance.
"""

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import opendata.core.database as database
from opendata.models.interface import InterfaceCategory
from opendata.models.user import User


@pytest.fixture
async def sqlite_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    """Point ``opendata.core.database.async_session_maker`` at a temporary SQLite DB."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_maker", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


class TestInitDbIdempotency:
    """Startup bootstrap must tolerate being run more than once."""

    async def test_first_run_seeds_admin_and_categories(
        self,
        sqlite_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh database gets the default admin plus the eight categories."""
        monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "DrillPassword123!")
        await database.init_db()

        async with sqlite_session_factory() as session:
            users = (await session.execute(select(User))).scalars().all()
            categories = (await session.execute(select(InterfaceCategory))).scalars().all()

        assert [u.username for u in users] == ["admin"]
        assert len(categories) == 8
        assert {c.name for c in categories} == {
            "stock",
            "fund",
            "futures",
            "index",
            "bond",
            "forex",
            "economic",
            "macro",
        }

    async def test_second_run_is_a_no_op(
        self,
        sqlite_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Running init_db twice must not raise and must not duplicate rows."""
        monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "DrillPassword123!")
        await database.init_db()
        await database.init_db()

        async with sqlite_session_factory() as session:
            users = (await session.execute(select(User))).scalars().all()
            categories = (await session.execute(select(InterfaceCategory))).scalars().all()

        assert len(users) == 1
        assert len(categories) == 8

    async def test_recovers_when_categories_exist_without_admin(
        self,
        sqlite_session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A partially seeded database (categories but no admin) must still start.

        This is the state a first run leaves behind if it dies between the
        category insert and the admin provision, and it used to make every
        subsequent startup fail with a duplicate-key error.
        """
        async with sqlite_session_factory() as session:
            session.add(InterfaceCategory(name="stock", description="股票数据", sort_order=1))
            await session.commit()

        monkeypatch.setenv("ADMIN_DEFAULT_PASSWORD", "DrillPassword123!")
        await database.init_db()

        async with sqlite_session_factory() as session:
            users = (await session.execute(select(User))).scalars().all()
            categories = (await session.execute(select(InterfaceCategory))).scalars().all()

        assert [u.username for u in users] == ["admin"]
        # The pre-existing "stock" row is kept, the remaining seven are added.
        names = [c.name for c in categories]
        assert len(names) == 8
        assert len(set(names)) == 8
