"""End-to-end scheduling loop on an isolated SQLite database.

Proves the post-port chain a scheduled task follows: a persisted DataScript
row -> AkshareScriptService lookup -> callable resolution into the real
ported collection class. No MySQL, network or warehouse access is involved.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import opendata.models  # noqa: F401  # register every table on Base.metadata
from opendata.afi_port.services.script import AkshareScriptService
from opendata.core.database import Base
from opendata.models.data_script import DataScript, ScriptFrequency


@pytest.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as db:
        yield db
    await engine.dispose()


@pytest.mark.asyncio
async def test_persisted_script_resolves_to_a_real_ported_entrypoint(session) -> None:
    row = DataScript(
        script_id="money_fund_hist_em",
        script_name="Money Fund Hist",
        category="funds",
        module_path="opendata.data_fetch.scripts.funds.weekly.money_fund_hist_em",
        function_name="main",
        frequency=ScriptFrequency.WEEKLY,
        source="akshare",
        is_active=True,
        is_custom=False,
    )
    session.add(row)
    await session.commit()

    service = AkshareScriptService(session)
    fetched = await service.get_script("money_fund_hist_em")
    assert fetched is not None
    assert fetched.module_path.startswith("opendata.data_fetch.scripts.")

    callable_obj = await service._resolve_callable(fetched)
    assert callable(callable_obj)
    # The pure-class module's run() entrypoint is reached via the fallback chain.
    assert callable_obj.__qualname__ == "MoneyFundHistEm.run"


@pytest.mark.asyncio
async def test_unknown_script_lookup_returns_none(session) -> None:
    service = AkshareScriptService(session)
    assert await service.get_script("does_not_exist") is None
