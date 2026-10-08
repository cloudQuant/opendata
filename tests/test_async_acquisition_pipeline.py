from __future__ import annotations

import asyncio
import threading
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from opendata.core.database import Base
from opendata.data.models import Bar
from opendata.data.protocol import FetchContext, Fetcher, QueryParams, UnsupportedAsyncFetcherError
from opendata.pipeline import jobs
from opendata.pipeline.affected_keys import SymbolPartitioning
from opendata.pipeline.runner import DataPipeline, PipelineSpec, Window
from opendata.services.data_acquisition import DataAcquisitionService

if TYPE_CHECKING:
    from opendata.data.protocol import FetchResult


WINDOW = Window(start=date(2024, 1, 2), end=date(2024, 1, 2))


class _Query(QueryParams):
    symbol: str


class _SourceFetcher(Fetcher[_Query, object]):
    """Test provider exposing both source-native and normalized output."""

    async_mode = "bounded_thread"

    def __init__(self, raw: object, *, fail_symbol_once: str | None = None) -> None:
        self.raw = raw
        self.validate_calls = 0
        self.extract_calls = 0
        self.native_extract_calls = 0
        self.normalize_calls = 0
        self.fail_symbol_once = fail_symbol_once
        self.failed_symbols: set[str] = set()

    def transform_query(self, **kwargs: object) -> _Query:
        self.validate_calls += 1
        return _Query(**kwargs)  # type: ignore[arg-type]

    def extract_data(self, params: _Query, ctx: FetchContext) -> object:
        self.extract_calls += 1
        if params.symbol == self.fail_symbol_once and params.symbol not in self.failed_symbols:
            self.failed_symbols.add(params.symbol)
            raise RuntimeError("temporary provider failure")
        if isinstance(self.raw, pd.DataFrame):
            result = self.raw.copy()
            if "symbol" in result:
                result["symbol"] = params.symbol
            return result
        return self.raw

    async def extract_data_async(self, params: _Query, ctx: FetchContext) -> object:
        self.native_extract_calls += 1
        await asyncio.sleep(0)
        return self.extract_data(params, ctx)

    def transform_data(self, raw: object, params: _Query) -> FetchResult:
        self.normalize_calls += 1
        return pd.DataFrame([{"normalized_symbol": params.symbol}])


class _NativeSourceFetcher(_SourceFetcher):
    async_mode = "native_async"


def _raw_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [{"股票代码": "600519", "日期": "2024-01-02", "成交量(手)": 100}],
        columns=["股票代码", "日期", "成交量(手)"],
    )


@pytest.mark.asyncio
async def test_raw_async_runs_validate_and_extract_once_without_normalizing() -> None:
    fetcher = _SourceFetcher(_raw_frame())

    raw = await fetcher.fetch_raw_async(
        ctx=FetchContext(timeout=1.0),
        symbol="600519",
        start_date=WINDOW.start,
        end_date=WINDOW.end,
    )

    pd.testing.assert_frame_equal(raw, _raw_frame())  # type: ignore[arg-type]
    assert fetcher.validate_calls == 1
    assert fetcher.extract_calls == 1
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bounded_thread", "native_async"])
async def test_async_ods_callback_preserves_full_raw_source_frame(monkeypatch, mode: str) -> None:
    fetcher = (
        _SourceFetcher(_raw_frame())
        if mode == "bounded_thread"
        else _NativeSourceFetcher(_raw_frame())
    )
    monkeypatch.setattr(jobs, "resolve_fetcher", lambda domain, source: fetcher)

    sync_fetch = jobs.make_fetch_symbol("stock_daily", "akshare")
    async_fetch = jobs.make_fetch_symbol_async("stock_daily", "akshare")
    sync_frame = sync_fetch("600519", WINDOW)
    async_frame = await async_fetch("600519", WINDOW)

    pd.testing.assert_frame_equal(async_frame, sync_frame)
    assert list(async_frame.columns) == ["股票代码", "日期", "成交量(手)"]
    assert async_frame["成交量(手)"].tolist() == [100]
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_async_ods_callback_denormalizes_contract_rows_like_sync(monkeypatch) -> None:
    bar = Bar(
        symbol="600519",
        trade_date=WINDOW.start,
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=1000.0,
        amount=1500.0,
    )
    fetcher = _SourceFetcher((bar,))
    monkeypatch.setattr(jobs, "resolve_fetcher", lambda domain, source: fetcher)

    sync_frame = jobs.make_fetch_symbol("stock_daily", "ths")("600519", WINDOW)
    async_frame = await jobs.make_fetch_symbol_async("stock_daily", "ths")("600519", WINDOW)

    pd.testing.assert_frame_equal(async_frame, sync_frame)
    assert list(async_frame.columns) == [
        "thscode",
        "trade_date",
        "open_price",
        "high_price",
        "low_price",
        "close_price",
        "volume",
        "turnover",
        "date_ms",
    ]
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_service_uses_async_fetcher_without_blocking_event_loop(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    raw = pd.DataFrame([{"source": "ready"}])

    class _BlockingFetcher(_SourceFetcher):
        def extract_data(self, params: _Query, ctx: FetchContext) -> object:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test provider was not released")
            return raw

        def transform_data(self, raw_value: object, params: _Query) -> FetchResult:
            self.normalize_calls += 1
            return raw_value  # type: ignore[return-value]

    fetcher = _BlockingFetcher(raw)
    service = DataAcquisitionService()
    monkeypatch.setattr(service, "_resolve_fetcher", lambda interface: fetcher)
    heartbeat = asyncio.Event()

    async def beat() -> None:
        await asyncio.sleep(0.01)
        heartbeat.set()

    try:
        task = asyncio.create_task(
            service._fetch_interface_data(SimpleNamespace(name="stock_daily"), {"symbol": "x"})
        )
        pulse = asyncio.create_task(beat())
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1)
        await asyncio.wait_for(heartbeat.wait(), timeout=1)
        release.set()
        result = await task
        await pulse
    finally:
        release.set()

    pd.testing.assert_frame_equal(result, raw)


@pytest.mark.asyncio
async def test_service_does_not_fallback_after_async_mode_rejection(monkeypatch) -> None:
    fetcher = _SourceFetcher(_raw_frame())
    fetcher.async_mode = "unsupported"
    service = DataAcquisitionService()
    monkeypatch.setattr(service, "_resolve_fetcher", lambda interface: fetcher)
    fallback_calls: list[bool] = []

    async def fallback(*args: Any, **kwargs: Any) -> None:
        fallback_calls.append(True)

    monkeypatch.setattr(service, "_call_akshare_function", fallback)
    with pytest.raises(UnsupportedAsyncFetcherError):
        await service._fetch_interface_data(SimpleNamespace(name="stock_daily"), {})
    assert fallback_calls == []
    assert fetcher.validate_calls == fetcher.extract_calls == 0


@pytest_asyncio.fixture
async def main_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["bounded_thread", "native_async"])
async def test_pipeline_awaits_provider_callback_and_resumes_async_symbol_failures(
    main_db, mode: str
) -> None:
    fetcher_type = _SourceFetcher if mode == "bounded_thread" else _NativeSourceFetcher
    fetcher = fetcher_type(
        pd.DataFrame([{"symbol": "600519", "trade_date": WINDOW.start, "close": 1.5}]),
        fail_symbol_once="000002",
    )
    calls: list[str] = []
    merge_keys: list[list[tuple[object, ...]]] = []

    async def fetch_symbol(symbol: str, window: Window) -> pd.DataFrame:
        calls.append(symbol)
        raw = await fetcher.fetch_raw_async(
            symbol=symbol, start_date=window.start, end_date=window.end
        )
        assert isinstance(raw, pd.DataFrame)
        raw["symbol"] = symbol
        return raw

    async def merge(context) -> None:
        merge_keys.append(list(context.affected_keys))

    pipeline = DataPipeline(
        PipelineSpec(
            domain="stock_daily",
            source=f"async-{mode}",
            key=("symbol", "trade_date"),
            symbols=["000001", "000002"],
            shard_size=2,
        ),
        session_maker=main_db,
        write_ods=lambda frame: len(frame),
        fetch_symbol=fetch_symbol,
        merge=merge,
        bounded_partitions=SymbolPartitioning(
            symbol_key_index=0,
            normalize_symbol=lambda value: str(value),
            batch_size=2,
        ),
    )

    first = await pipeline.run(WINDOW)
    second = await pipeline.run(WINDOW)

    assert first.shards_failed == 1
    assert first.rows_written == 1
    assert first.failures[0].symbol == "000002"
    assert second.shards_done == 1
    assert second.shards_failed == 0
    assert second.rows_written == 2
    assert calls == ["000001", "000002", "000001", "000002"]
    assert merge_keys[0] == [("000001", WINDOW.start)]
    assert len(merge_keys[1]) == 2
    assert fetcher.normalize_calls == 0
    assert fetcher.native_extract_calls == (4 if mode == "native_async" else 0)
