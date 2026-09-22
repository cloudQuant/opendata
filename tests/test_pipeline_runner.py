"""Data pipeline runner tests (A4.4, design §9.1/§9.2).

The engine owns the six-step orchestration and the shard-level
resume bookkeeping; the domain-specific pieces (fetching one symbol,
writing ods, cross-check, merge, notify) are injected so the
orchestration can be tested without a warehouse. The ``e2e`` class
runs the real ods writer against MySQL and proves the interrupt and
resume path from the design: a killed run leaves ``done`` shards
behind, the next run skips them and finishes the rest.
"""

from datetime import date

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy import create_engine, pool, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from opendata.core.database import Base
from opendata.models.pipeline import PipelineProgress, ShardStatus
from opendata.pipeline.ddl import Column, ods_table_ddl
from opendata.pipeline.ods_writer import OdsWriter
from opendata.pipeline.runner import (
    DataPipeline,
    PipelineLockedError,
    PipelineSpec,
    Window,
    plan_shards,
    release_pipeline_lock,
)

WINDOW = Window(start=date(2024, 1, 1), end=date(2024, 1, 31))


def _frame(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "trade_date": [date(2024, 1, 2)],
            "close": [1.0],
        }
    )


class FakeWriter:
    """Records every ods write instead of touching a warehouse."""

    def __init__(self) -> None:
        """Start with no recorded writes."""
        self.frames: list[pd.DataFrame] = []

    def __call__(self, frame: pd.DataFrame) -> int:
        """Record a write and report its row count."""
        self.frames.append(frame)
        return len(frame)

    def symbols(self) -> list[str]:
        """Every symbol written, in order."""
        return [symbol for frame in self.frames for symbol in frame["symbol"]]


@pytest_asyncio.fixture
async def main_db():
    """Main-database session factory for progress rows."""
    from sqlalchemy.ext.asyncio import create_async_engine

    async_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(async_engine, expire_on_commit=False)
    await async_engine.dispose()


class TestPlanShards:
    def test_splits_symbols_into_fixed_size_shards(self):
        shards = plan_shards([f"{index:06d}" for index in range(1200)], shard_size=500)

        assert [len(shard) for shard in shards] == [500, 500, 200]

    def test_empty_symbol_list_has_no_shards(self):
        assert plan_shards([], shard_size=500) == []

    def test_rejects_non_positive_shard_size(self):
        with pytest.raises(ValueError, match="shard_size"):
            plan_shards(["600519"], shard_size=0)


class TestPipelineRun:
    def _pipeline(self, main_db, writer, fetch, **overrides):
        spec = PipelineSpec(
            domain="stock_daily",
            source="akshare",
            key=("symbol", "trade_date"),
            symbols=[f"{index:06d}" for index in range(5)],
            shard_size=overrides.pop("shard_size", 2),
        )
        return DataPipeline(
            spec,
            session_maker=main_db,
            write_ods=writer,
            fetch_symbol=fetch,
            **overrides,
        )

    async def test_runs_every_shard_in_order(self, main_db):
        writer = FakeWriter()
        calls: list[str] = []

        def fetch(symbol, window):
            calls.append(symbol)
            return _frame(symbol)

        pipeline = self._pipeline(main_db, writer, fetch)
        outcome = await pipeline.run(WINDOW)

        assert calls == ["000000", "000001", "000002", "000003", "000004"]
        assert outcome.shards_total == 3
        assert outcome.shards_done == 3
        assert outcome.rows_written == 5
        assert outcome.failures == []
        assert writer.symbols() == calls

    async def test_completed_shards_are_skipped_on_resume(self, main_db):
        writer = FakeWriter()
        calls: list[str] = []

        def fetch(symbol, window):
            calls.append(symbol)
            if symbol == "000004":
                raise RuntimeError("upstream hiccup")
            return _frame(symbol)

        pipeline = self._pipeline(main_db, writer, fetch)
        first = await pipeline.run(WINDOW)

        assert first.shards_done == 2
        assert first.shards_failed == 1
        assert [failure.symbol for failure in first.failures] == ["000004"]

        # Second run with a healthy source: the two good shards stay
        # done, only the failed one retries and succeeds.
        calls.clear()

        def fetch_ok(symbol, window):
            calls.append(symbol)
            return _frame(symbol)

        second = await self._pipeline(main_db, FakeWriter(), fetch_ok).run(WINDOW)

        assert calls == ["000004"]
        assert second.shards_done == 1
        assert second.shards_failed == 0
        assert second.resumed_shards == 2
        assert second.rows_written == 1

    async def test_single_symbol_failure_does_not_block_its_shard(self, main_db):
        writer = FakeWriter()

        def fetch(symbol, window):
            if symbol == "000001":
                raise RuntimeError("bad symbol")
            return _frame(symbol)

        pipeline = self._pipeline(main_db, writer, fetch)
        outcome = await pipeline.run(WINDOW)

        assert outcome.shards_done == 3  # every shard still wrote its good rows
        assert [failure.symbol for failure in outcome.failures] == ["000001"]
        assert "000000" in writer.symbols()
        assert "000002" in writer.symbols()

    async def test_hooks_receive_the_affected_keys_in_order(self, main_db):
        writer = FakeWriter()
        events: list[str] = []

        async def cross_check(context):
            events.append(f"cross_check:{len(context.affected_keys)}")

        async def merge(context):
            events.append("merge")

        async def notify(context):
            events.append("notify")

        pipeline = self._pipeline(
            main_db,
            writer,
            lambda symbol, window: _frame(symbol),
            cross_check=cross_check,
            merge=merge,
            notify=notify,
        )
        outcome = await pipeline.run(WINDOW)

        assert events == ["cross_check:5", "merge", "notify"]
        assert outcome.rows_written == 5

    async def test_progress_rows_record_every_shard(self, main_db):
        pipeline = self._pipeline(main_db, FakeWriter(), lambda s, w: _frame(s))
        outcome = await pipeline.run(WINDOW)

        async with main_db() as session:
            rows = (
                (
                    await session.execute(
                        select(PipelineProgress)
                        .where(PipelineProgress.pipeline_id == outcome.pipeline_id)
                        .order_by(PipelineProgress.shard)
                    )
                )
                .scalars()
                .all()
            )

        assert [row.status for row in rows] == [ShardStatus.DONE] * 3
        assert [row.shard for row in rows] == [0, 1, 2]
        assert all(row.rows_written for row in rows)

    async def test_resume_false_reruns_every_shard(self, main_db):
        calls: list[str] = []

        def fetch(symbol, window):
            calls.append(symbol)
            return _frame(symbol)

        pipeline = self._pipeline(main_db, FakeWriter(), fetch)
        await pipeline.run(WINDOW)
        calls.clear()
        outcome = await pipeline.run(WINDOW, resume=False)

        assert outcome.resumed_shards == 0
        assert len(calls) == 5

    async def test_concurrent_run_of_the_same_domain_is_locked_out(self, main_db):
        from opendata.pipeline import runner

        pipeline = self._pipeline(main_db, FakeWriter(), lambda symbol, window: _frame(symbol))

        # Hold the process-internal lock, then a second run must refuse.
        lock = runner._pipeline_lock("stock_daily:akshare")
        await lock.acquire()
        try:
            with pytest.raises(PipelineLockedError, match="already running"):
                await pipeline.run(WINDOW)
        finally:
            lock.release()
            release_pipeline_lock("stock_daily:akshare")


@pytest.mark.e2e
class TestInterruptedResumeAgainstMysql:
    """Design verification: interrupt, then resume skips done shards."""

    TABLE = "ods_stock_daily_akshare"

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            [
                Column("symbol", "varchar(64)", nullable=False),
                Column("trade_date", "date", nullable=False),
                Column("close", "double", nullable=False),
            ],
            key=("symbol", "trade_date"),
        )
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS `{self.TABLE}`"))
            connection.execute(text(ddl))
        yield engine
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS `{self.TABLE}`"))
        engine.dispose()

    async def test_interrupted_run_resumes_without_rewriting_done_shards(self, warehouse, main_db):
        batch_id = "11111111-2222-4333-8444-555555555555"
        writer = OdsWriter(warehouse, batch_size=100)

        def write_ods(frame: pd.DataFrame) -> int:
            return writer.write(
                frame,
                table=self.TABLE,
                key=("symbol", "trade_date"),
                source="akshare",
                batch_id=batch_id,
            ).rows

        state = {"interrupt_after": 2, "writes": 0}

        def fetch(symbol, window):
            if state["writes"] >= state["interrupt_after"]:
                raise KeyboardInterrupt("simulated interruption")
            return _frame(symbol)

        def counting_write(frame: pd.DataFrame) -> int:
            state["writes"] += 1
            return write_ods(frame)

        spec = PipelineSpec(
            domain="stock_daily",
            source="akshare",
            key=("symbol", "trade_date"),
            symbols=[f"{index:06d}" for index in range(6)],
            shard_size=2,
        )
        pipeline = DataPipeline(
            spec, session_maker=main_db, write_ods=counting_write, fetch_symbol=fetch
        )

        with pytest.raises(KeyboardInterrupt):
            await pipeline.run(WINDOW)
        release_pipeline_lock("stock_daily:akshare")

        async with main_db() as session:
            done = (
                (
                    await session.execute(
                        select(PipelineProgress).where(PipelineProgress.status == ShardStatus.DONE)
                    )
                )
                .scalars()
                .all()
            )
        assert len(done) == 2

        # Resuming must not re-fetch the completed shards.
        fetched: list[str] = []

        def fetch_ok(symbol, window):
            fetched.append(symbol)
            return _frame(symbol)

        resumed = DataPipeline(
            spec,
            session_maker=main_db,
            write_ods=lambda frame: (
                writer.write(
                    frame,
                    table=self.TABLE,
                    key=("symbol", "trade_date"),
                    source="akshare",
                    batch_id=batch_id,
                ).rows
            ),
            fetch_symbol=fetch_ok,
        )
        outcome = await resumed.run(WINDOW)

        assert outcome.resumed_shards == 2
        assert fetched == ["000004", "000005"]
        assert outcome.shards_done == 1

        with warehouse.connect() as connection:
            rows = (
                connection.execute(
                    text("SELECT symbol FROM `ods_stock_daily_akshare` ORDER BY symbol")
                )
                .scalars()
                .all()
            )
        assert rows == ["000000", "000001", "000002", "000003", "000004", "000005"]
