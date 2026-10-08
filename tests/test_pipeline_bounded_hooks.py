"""Bounded stock-daily pipeline orchestration checks using local SQLite only."""

from datetime import date

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opendata.core.database import Base
from opendata.pipeline.affected_keys import AffectedKeySpool, SymbolPartitioning
from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window

WINDOW = Window(start=date(2024, 1, 1), end=date(2024, 1, 31))


@pytest_asyncio.fixture
async def session_maker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _pipeline(session_maker, symbols, fetch_symbol, *, shard_size, hooks, loader=None):
    spec = PipelineSpec(
        domain="stock_daily",
        source="akshare",
        key=("symbol", "trade_date"),
        symbols=symbols,
        shard_size=shard_size,
    )
    return DataPipeline(
        spec,
        session_maker=session_maker,
        write_ods=lambda frame: len(frame),
        fetch_symbol=fetch_symbol,
        merge=hooks,
        load_affected_keys=loader,
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0,
            normalize_symbol=lambda value: str(value).split(".", maxsplit=1)[0],
            batch_size=50,
        ),
    )


def _frame(symbol: str, *, include_revision: bool = False) -> pd.DataFrame:
    rows = [(symbol, date(2024, 1, 2))]
    if include_revision:
        rows.append((symbol, date(2023, 12, 29)))
    return pd.DataFrame(rows, columns=["symbol", "trade_date"])


async def test_hook_batches_are_independent_of_fetch_shards_and_done_resume_skips_loader(
    session_maker,
):
    symbols = tuple(f"{index:06d}" for index in range(105))
    fetch_calls: list[str] = []
    hook_calls = []
    notifications = []
    loader_calls = []

    def fetch(symbol, window):
        fetch_calls.append(symbol)
        return _frame(symbol, include_revision=symbol == symbols[0])

    async def merge(context):
        partitions = list(context.partition_contexts())
        hook_calls.append(partitions)
        assert context.window == WINDOW
        assert context.pipeline_id
        assert [len(part.symbols) for part in partitions] == [50, 50, 5]
        assert max(len(part.symbols) for part in partitions) <= 50
        assert all(part.window == context.window for part in partitions)
        assert all(
            set(values) == set(part.symbols)
            for part in partitions
            for values in part.source_windows.values()
        )
        assert all(set(part.comparison_windows) == set(part.symbols) for part in partitions)
        partition_keys = [key for part in partitions for key in part.affected_keys]
        assert len(partition_keys) == 106
        assert partition_keys[0] == (symbols[0], date(2024, 1, 2))
        assert (symbols[0], date(2023, 12, 29)) in partition_keys

    async def notify(context):
        notifications.append(list(context.affected_keys))

    def load_keys(context):
        loader_calls.append(True)
        return ()

    pipeline = _pipeline(
        session_maker,
        symbols,
        fetch,
        shard_size=73,
        hooks=merge,
        loader=load_keys,
    )
    pipeline.notify = notify
    source_windows = {source: dict.fromkeys(symbols, WINDOW) for source in ("akshare", "ths")}

    first = await pipeline.run(WINDOW, source_windows=source_windows)
    assert first.shards_total == 2  # fetch shard size remains 73
    assert first.shards_done == 2
    assert len(fetch_calls) == 105
    assert len(hook_calls) == 1
    assert len(notifications) == 1
    assert len(notifications[0]) == 106
    assert loader_calls == []

    fetch_calls.clear()
    await pipeline.run(WINDOW, source_windows=source_windows)
    assert fetch_calls == []
    assert len(hook_calls) == 1
    assert len(notifications) == 1
    assert loader_calls == []  # done shards + done hooks return before ODS-key loading


async def test_mixed_resume_rebuilds_all_keys_in_bounded_partitions(session_maker):
    symbols = tuple(f"{index:06d}" for index in range(60))
    calls: dict[str, int] = {}
    observed_key_counts = []
    loader_batches = []
    fail_once = {symbols[45]}

    def fetch(symbol, window):
        calls[symbol] = calls.get(symbol, 0) + 1
        if symbol in fail_once and calls[symbol] == 1:
            raise RuntimeError("temporary symbol failure")
        return _frame(symbol)

    async def merge(context):
        partitions = list(context.partition_contexts())
        observed_key_counts.append(sum(len(part.affected_keys) for part in partitions))
        assert all(len(part.symbols) <= 50 for part in partitions)

    def load_keys(context):
        assert context.partition_contexts is not None

        def iter_keys():
            for part in context.partition_contexts():
                loader_batches.append(tuple(part.symbols))
                assert len(part.symbols) <= 50
                for symbol in part.symbols:
                    yield (symbol, date(2024, 1, 2))

        return iter_keys()

    pipeline = _pipeline(
        session_maker,
        symbols,
        fetch,
        shard_size=30,
        hooks=merge,
        loader=load_keys,
    )

    first = await pipeline.run(WINDOW)
    assert first.shards_done == 1
    assert first.shards_failed == 1
    assert observed_key_counts == [59]
    assert loader_batches == []

    second = await pipeline.run(WINDOW)
    assert second.resumed_shards == 1
    assert second.shards_done == 1
    assert second.shards_failed == 0
    assert loader_batches == [symbols[:50], symbols[50:]]
    assert observed_key_counts == [59, 60]


async def test_full_resume_rebuilds_keys_after_a_hook_failure(session_maker):
    symbols = tuple(f"{index:06d}" for index in range(7))
    fetch_calls = []
    hook_calls = []
    loader_calls = []

    def fetch(symbol, window):
        fetch_calls.append(symbol)
        return _frame(symbol)

    async def merge(context):
        hook_calls.append(sum(len(part.affected_keys) for part in context.partition_contexts()))
        if len(hook_calls) == 1:
            raise RuntimeError("merge interrupted")

    def load_keys(context):
        def iter_keys():
            for part in context.partition_contexts():
                loader_calls.append(tuple(part.symbols))
                for symbol in part.symbols:
                    yield (symbol, date(2024, 1, 2))

        return iter_keys()

    pipeline = _pipeline(
        session_maker,
        symbols,
        fetch,
        shard_size=3,
        hooks=merge,
        loader=load_keys,
    )

    with pytest.raises(RuntimeError, match="merge interrupted"):
        await pipeline.run(WINDOW)
    assert len(fetch_calls) == len(symbols)
    assert hook_calls == [len(symbols)]

    fetch_calls.clear()
    result = await pipeline.run(WINDOW)
    assert result.resumed_shards == 3
    assert fetch_calls == []
    assert loader_calls == [symbols]
    assert hook_calls == [len(symbols), len(symbols)]


async def test_normalized_aliases_are_rejected_before_fetch(session_maker):
    fetch_calls = []

    def fetch(symbol, window):
        fetch_calls.append(symbol)
        return _frame(symbol)

    pipeline = _pipeline(
        session_maker,
        ("600519", "600519.SH"),
        fetch,
        shard_size=500,
        hooks=None,
    )

    with pytest.raises(ValueError, match="overlapping hook partitions"):
        await pipeline.run(WINDOW)
    assert fetch_calls == []


async def test_pipeline_spool_is_removed_when_hook_raises_baseexception(session_maker, monkeypatch):
    from opendata.pipeline import runner as runner_module

    class StopPipeline(BaseException):
        pass

    real_spool = runner_module.AffectedKeySpool
    created: list[AffectedKeySpool] = []

    def capture_spool():
        spool = real_spool()
        created.append(spool)
        return spool

    monkeypatch.setattr(runner_module, "AffectedKeySpool", capture_spool)

    async def stop(context):
        raise StopPipeline()

    pipeline = _pipeline(
        session_maker,
        ("600519",),
        lambda symbol, window: _frame(symbol),
        shard_size=10,
        hooks=stop,
    )
    with pytest.raises(StopPipeline):
        await pipeline.run(WINDOW)

    assert len(created) == 1
    assert not created[0].path.exists()
