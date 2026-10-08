"""DataTable metadata refresh uses bounded warehouse aggregates."""

from datetime import date

import pytest
import pytest_asyncio
from sqlalchemy import Column, Date, MetaData, String, Table, create_engine, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from opendata.core.database import Base
from opendata.models.data_table import DataTable
from opendata.pipeline.metadata_refresh import refresh_pipeline_metadata


@pytest_asyncio.fixture
async def main_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _warehouse():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = MetaData()
    ods = Table(
        "ods_stock_daily_akshare",
        metadata,
        Column("股票代码", String, nullable=False),
        Column("日期", Date, nullable=False),
    )
    dwd = Table(
        "dwd_stock_daily",
        metadata,
        Column("symbol", String, nullable=False),
        Column("trade_date", Date, nullable=False),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            insert(ods),
            [
                {"股票代码": "600519.SH", "日期": date(2026, 9, 23)},
                {"股票代码": "600519.SH", "日期": date(2026, 9, 24)},
            ],
        )
        connection.execute(
            insert(dwd),
            [
                {"symbol": "600519", "trade_date": date(2026, 9, 23)},
                {"symbol": "600519", "trade_date": date(2026, 9, 24)},
                {"symbol": "000001", "trade_date": date(2026, 9, 24)},
            ],
        )
    return engine, ods, dwd


async def test_refreshes_ods_and_dwd_metadata_idempotently(main_db) -> None:
    warehouse, _ods, dwd = _warehouse()

    first = await refresh_pipeline_metadata(
        warehouse,
        main_db,
        domain="stock_daily",
        sources=("akshare",),
    )
    async with main_db() as session:
        first_rows = (
            (await session.execute(select(DataTable).order_by(DataTable.table_name)))
            .scalars()
            .all()
        )
    ids = {row.table_name: row.id for row in first_rows}

    with warehouse.begin() as connection:
        connection.execute(
            insert(dwd),
            {"symbol": "300001", "trade_date": date(2026, 9, 25)},
        )
    second = await refresh_pipeline_metadata(
        warehouse,
        main_db,
        domain="stock_daily",
        sources=("akshare",),
    )

    assert first["ods_stock_daily_akshare"] == {
        "row_count": 2,
        "data_start_date": "2026-09-23",
        "data_end_date": "2026-09-24",
        "last_update_status": "success",
    }
    assert second["dwd_stock_daily"]["row_count"] == 4
    async with main_db() as session:
        rows = (
            (await session.execute(select(DataTable).order_by(DataTable.table_name)))
            .scalars()
            .all()
        )
    assert len(rows) == 2
    assert {row.table_name: row.id for row in rows} == ids
    dwd_row = next(row for row in rows if row.table_name == "dwd_stock_daily")
    assert dwd_row.category == "pipeline"
    assert dwd_row.data_start_date == date(2026, 9, 23)
    assert dwd_row.data_end_date == date(2026, 9, 25)
    assert dwd_row.last_update_status == "success"
    warehouse.dispose()


async def test_missing_warehouse_table_fails_without_inserting_partial_metadata(main_db) -> None:
    warehouse = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    metadata = MetaData()
    Table(
        "ods_stock_daily_akshare",
        metadata,
        Column("股票代码", String, nullable=False),
        Column("日期", Date, nullable=False),
    )
    metadata.create_all(warehouse)

    with pytest.raises(LookupError, match="dwd_stock_daily.*missing"):
        await refresh_pipeline_metadata(
            warehouse,
            main_db,
            domain="stock_daily",
            sources=("akshare",),
        )
    async with main_db() as session:
        count = await session.scalar(select(DataTable.id))
    assert count is None
    warehouse.dispose()
