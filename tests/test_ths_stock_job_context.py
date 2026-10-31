"""Trusted request-budget propagation for scheduled stock-daily jobs."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest

from opendata.data.request_budget import (
    RequestAttemptLimitError,
    RequestAuthorizationError,
    RequestBudget,
)
from opendata.pipeline import jobs
from opendata.pipeline.runner import Window

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext


WINDOW = Window(start=date(2024, 1, 2), end=date(2024, 1, 3))


@pytest.mark.asyncio
async def test_async_callback_reuses_budget_across_symbols_with_fresh_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run-scoped budget is shared, while each symbol owns its FetchContext."""
    budget = RequestBudget(task_attempts=4, source_attempts=2)
    contexts: list[FetchContext] = []

    class StubFetcher:
        canonical_model = "stock_daily"

        async def fetch_raw_async(
            self, *, ctx: FetchContext, symbol: str, **kwargs: object
        ) -> object:
            contexts.append(ctx)
            return pd.DataFrame([{"symbol": symbol}])

    monkeypatch.setattr(jobs, "resolve_fetcher", lambda _domain, _source: StubFetcher())
    fetch = jobs.make_fetch_symbol_async(
        "stock_daily", "ths", timeout=2.5, request_budget=budget
    )

    for symbol in ("600519", "000001"):
        frame = await fetch(symbol, WINDOW)
        assert frame["symbol"].tolist() == [symbol]

    assert len(contexts) == 2
    assert contexts[0] is not contexts[1]
    assert all(context.request_budget is budget for context in contexts)
    assert all(context.timeout == 2.5 for context in contexts)
    assert budget.attempts_used == 0


@pytest.mark.asyncio
async def test_legacy_factory_call_stays_fail_closed_and_propagates_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pre-budget call shape keeps a grant-free context and provider denial."""
    denial = RequestAuthorizationError("no exact request grant is configured")
    contexts: list[FetchContext] = []

    class StubFetcher:
        canonical_model = "stock_daily"

        async def fetch_raw_async(self, *, ctx: FetchContext, **kwargs: object) -> object:
            contexts.append(ctx)
            if ctx.request_budget is None:
                raise denial
            return pd.DataFrame()

    monkeypatch.setattr(jobs, "resolve_fetcher", lambda _domain, _source: StubFetcher())
    fetch = jobs.make_fetch_symbol_async("stock_daily", "ths")

    with pytest.raises(RequestAuthorizationError) as raised:
        await fetch("600519", WINDOW)

    assert raised.value is denial
    assert len(contexts) == 1
    assert contexts[0].request_budget is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RequestAttemptLimitError("request attempt allowance exhausted"),
        RequestAuthorizationError("request grant was revoked"),
    ],
)
async def test_provider_budget_failures_propagate_without_retry_or_replacement(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    """Attempt limits and revocations survive the ODS callback unchanged."""
    budget = RequestBudget(task_attempts=1, source_attempts=1)
    contexts: list[FetchContext] = []

    class StubFetcher:
        canonical_model = "stock_daily"

        async def fetch_raw_async(self, *, ctx: FetchContext, **kwargs: object) -> object:
            contexts.append(ctx)
            raise failure

    monkeypatch.setattr(jobs, "resolve_fetcher", lambda _domain, _source: StubFetcher())
    fetch = jobs.make_fetch_symbol_async("stock_daily", "ths", request_budget=budget)

    with pytest.raises(type(failure)) as raised:
        await fetch("600519", WINDOW)

    assert raised.value is failure
    assert len(contexts) == 1
    assert contexts[0].request_budget is budget
    assert budget.attempts_used == 0


def test_async_factory_validates_supplied_budget_with_fetch_context_rules() -> None:
    """Malformed budget inputs are rejected before resolving a provider."""
    with pytest.raises(ValueError, match="request_budget must be a RequestBudget"):
        jobs.make_fetch_symbol_async("stock_daily", "ths", request_budget=object())  # type: ignore[arg-type]


def test_budget_requires_the_exact_ths_stock_daily_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget cannot be attached to AkShare or a provider without model identity."""
    budget = RequestBudget(task_attempts=1, source_attempts=1)

    with pytest.raises(ValueError, match="single-source THS stock_daily"):
        jobs.make_fetch_symbol_async("stock_daily", "akshare", request_budget=budget)

    class MissingIdentityFetcher:
        async def fetch_raw_async(self, **kwargs: object) -> object:
            return pd.DataFrame()

    monkeypatch.setattr(
        jobs, "resolve_fetcher", lambda _domain, _source: MissingIdentityFetcher()
    )
    with pytest.raises(ValueError, match="exact canonical model"):
        jobs.make_fetch_symbol_async("stock_daily", "ths", request_budget=budget)


@pytest.mark.asyncio
async def test_incremental_job_passes_one_budget_to_each_ths_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit run budget reaches each feed callback and each symbol."""
    budget = RequestBudget(task_attempts=4, source_attempts=2)
    contexts: list[tuple[str, str, FetchContext]] = []
    fake_calendar = SimpleNamespace(tier="fixture", answered_from=lambda _day: "fixture")

    class StubFetcher:
        canonical_model = "stock_daily"

        def __init__(self, source: str) -> None:
            self.source = source

        async def fetch_raw_async(
            self, *, ctx: FetchContext, symbol: str, **kwargs: object
        ) -> object:
            contexts.append((self.source, symbol, ctx))
            return pd.DataFrame([{"symbol": symbol}])

    class StubPipeline:
        def __init__(self, *, source: str, fetch_symbol: Any, symbols: list[str]) -> None:
            self.source = source
            self.fetch_symbol = fetch_symbol
            self.symbols = symbols

        async def run(self, window: Window, **kwargs: object) -> object:
            for symbol in self.symbols:
                await self.fetch_symbol(symbol, window)
            return SimpleNamespace(shards_failed=0)

    monkeypatch.setattr(jobs, "resolve_calendar", lambda _engine: fake_calendar)
    monkeypatch.setattr(jobs, "incremental_window", lambda *_args, **_kwargs: WINDOW)
    monkeypatch.setattr(
        jobs,
        "derive_universe",
        lambda *_args, **_kwargs: (["600519", "000001"], {"source": "fixture"}),
    )
    monkeypatch.setattr(jobs, "read_ods_watermarks", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        jobs, "effective_symbol_windows", lambda symbols, *_args, **_kwargs: {}
    )
    monkeypatch.setattr(jobs, "resolve_fetcher", lambda _domain, source: StubFetcher(source))

    def build_pipeline(**kwargs: Any) -> StubPipeline:
        return StubPipeline(
            source=kwargs["source"],
            fetch_symbol=kwargs["fetch_symbol"],
            symbols=list(kwargs["symbols"]),
        )

    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", build_pipeline)
    monkeypatch.setattr(jobs, "_count_in_window", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(jobs, "_freshness", lambda *_args, **_kwargs: {})

    await jobs.run_incremental_job(
        domain="stock_daily",
        source="ths",
        symbols=["600519", "000001"],
        engine=object(),  # type: ignore[arg-type]
        session_maker=object(),  # type: ignore[arg-type]
        calendar=fake_calendar,  # type: ignore[arg-type]
        request_budget=budget,
    )

    assert {(source, symbol) for source, symbol, _ctx in contexts} == {
        ("ths", "600519"),
        ("ths", "000001"),
    }
    assert len(contexts) == 2
    assert len({id(context) for _source, _symbol, context in contexts}) == 2
    assert all(context.request_budget is budget for _source, _symbol, context in contexts)


@pytest.mark.asyncio
async def test_unbudgeted_job_preserves_two_argument_factory_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy integrations that monkeypatch the original factory signature remain valid."""
    fake_calendar = SimpleNamespace(tier="fixture", answered_from=lambda _day: "fixture")
    factory_calls: list[tuple[str, str]] = []

    async def fake_fetch(_symbol: str, _window: Window) -> pd.DataFrame:
        return pd.DataFrame()

    def legacy_factory(domain: str, source: str) -> Any:
        factory_calls.append((domain, source))
        return fake_fetch

    class StubPipeline:
        async def run(self, _window: Window, **_kwargs: object) -> object:
            return SimpleNamespace(shards_failed=0)

    monkeypatch.setattr(jobs, "make_fetch_symbol_async", legacy_factory)
    monkeypatch.setattr(jobs, "incremental_window", lambda *_args, **_kwargs: WINDOW)
    monkeypatch.setattr(
        jobs,
        "derive_universe",
        lambda *_args, **_kwargs: (["600519"], {"source": "fixture"}),
    )
    monkeypatch.setattr(jobs, "read_ods_watermarks", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        jobs, "effective_symbol_windows", lambda _symbols, *_args, **_kwargs: {}
    )
    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", lambda **_kwargs: StubPipeline())
    monkeypatch.setattr(jobs, "_count_in_window", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(jobs, "_freshness", lambda *_args, **_kwargs: {})

    await jobs.run_incremental_job(
        domain="stock_daily",
        source="ths",
        symbols=["600519"],
        engine=object(),  # type: ignore[arg-type]
        session_maker=object(),  # type: ignore[arg-type]
        calendar=fake_calendar,  # type: ignore[arg-type]
    )

    assert factory_calls == [("stock_daily", "ths")]


@pytest.mark.asyncio
async def test_budgeted_incremental_job_rejects_dual_source_before_universe_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budgeted run cannot complete one feed before rejecting its fallback."""
    budget = RequestBudget(task_attempts=2, source_attempts=2)

    def fail_if_universe_is_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("dual-source budget validation must run before universe reads")

    monkeypatch.setattr(jobs, "derive_universe", fail_if_universe_is_read)

    with pytest.raises(ValueError, match="single-source THS stock_daily"):
        await jobs.run_incremental_job(
            domain="stock_daily",
            source="ths",
            second_source="akshare",
            engine=object(),  # type: ignore[arg-type]
            session_maker=object(),  # type: ignore[arg-type]
            request_budget=budget,
        )
