"""Keep the derived stock-universe safety cap large enough for current catalogs."""

from __future__ import annotations

import sys
from datetime import date
from types import ModuleType
from typing import Any

import pytest

from opendata.pipeline import jobs
from opendata.pipeline.runner import Window
from opendata.pipeline.templates import ScheduleTemplate, TemplateKind
from opendata.pipeline.trading_calendar import weekday_calendar


class _Result:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    def all(self) -> list[tuple[str]]:
        return [(symbol,) for symbol in self._symbols]


class _Connection:
    def __init__(self, symbols: list[str], calls: list[dict[str, Any]]) -> None:
        self._symbols = symbols
        self._calls = calls

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, int]) -> _Result:
        self._calls.append({"statement": str(statement), "params": dict(params)})
        return _Result(self._symbols)


class _Warehouse:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.calls: list[dict[str, Any]] = []

    def connect(self) -> _Connection:
        return _Connection(self.symbols, self.calls)


WINDOW = Window(start=date(2026, 10, 28), end=date(2026, 10, 28))


def _incremental_template(payload: dict[str, object]) -> ScheduleTemplate:
    return ScheduleTemplate(
        name="synthetic-incremental",
        cron="0 18 * * 1-5",
        kind=TemplateKind.INCREMENTAL,
        payload=payload,  # type: ignore[arg-type]  # Tests numeric YAML payload validation.
    )


def _job_result() -> jobs.JobResult:
    return jobs.JobResult(domain="stock_daily", sources=("ths",), window=WINDOW, symbols=0)


def test_omitted_derived_limit_accepts_10000_and_probes_one_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _Warehouse([f"{index:06d}" for index in range(10_000)])
    monkeypatch.setattr(jobs, "landed_instruments", lambda engine: [])

    symbols, provenance = jobs.derive_universe(
        warehouse, "stock_daily", symbols=None, limit=None, window=WINDOW
    )

    assert jobs.DEFAULT_SYMBOL_LIMIT == 10_000
    assert len(symbols) == 10_000
    assert warehouse.calls[0]["params"] == {"cap": 10_001}
    assert provenance == {"from": "dwd_stock_daily", "catalog": "absent"}


def test_explicit_5000_limit_accepts_exact_capacity_and_probes_one_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _Warehouse([f"{index:06d}" for index in range(5_000)])
    monkeypatch.setattr(jobs, "landed_instruments", lambda engine: [])

    symbols, _ = jobs.derive_universe(
        warehouse, "stock_daily", symbols=None, limit=5_000, window=WINDOW
    )

    assert len(symbols) == 5_000
    assert warehouse.calls[0]["params"] == {"cap": 5_001}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_limit", [False, True])
async def test_10000_limit_rejects_overflow_before_provider_execution(
    monkeypatch: pytest.MonkeyPatch, explicit_limit: bool
) -> None:
    warehouse = _Warehouse([f"{index:06d}" for index in range(10_001)])
    database_module = ModuleType("opendata.core.database")
    database_module.async_session_maker = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opendata.core.database", database_module)

    def must_not_run(*args: object, **kwargs: object) -> Any:
        raise AssertionError("an over-cap universe must stop before provider execution")

    monkeypatch.setattr(jobs, "landed_instruments", must_not_run)
    monkeypatch.setattr(jobs, "make_fetch_symbol_async", must_not_run)
    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", must_not_run)

    message = "derived universe from dwd_stock_daily exceeds limit 10000"
    job_args: dict[str, Any] = {
        "domain": "stock_daily",
        "source": "ths",
        "as_of": WINDOW.start,
        "engine": warehouse,
        "calendar": weekday_calendar(),
    }
    if explicit_limit:
        job_args["limit"] = 10_000

    with pytest.raises(ValueError, match=message):
        await jobs.run_incremental_job(**job_args)

    assert warehouse.calls[0]["params"] == {"cap": 10_001}


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 5_000, 50_000])
async def test_builtin_incremental_schedule_passes_an_explicit_limit(
    monkeypatch: pytest.MonkeyPatch, limit: int
) -> None:
    captured: dict[str, Any] = {}

    async def fake_job(**kwargs: Any) -> jobs.JobResult:
        captured.update(kwargs)
        return _job_result()

    monkeypatch.setattr(jobs, "run_incremental_job", fake_job)
    template = _incremental_template({"domain": "stock_daily", "limit": limit})

    result = await jobs._execute_template(template)

    assert captured == {
        "domain": "stock_daily",
        "source": None,
        "second_source": None,
        "limit": limit,
    }
    assert result["symbols"] == 0


@pytest.mark.asyncio
async def test_builtin_incremental_schedule_without_limit_uses_job_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_job(**kwargs: Any) -> jobs.JobResult:
        captured.update(kwargs)
        return _job_result()

    monkeypatch.setattr(jobs, "run_incremental_job", fake_job)
    template = _incremental_template({"domain": "stock_daily"})

    await jobs._execute_template(template)

    assert "limit" not in captured
    assert jobs.DEFAULT_SYMBOL_LIMIT == 10_000


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [True, False, 0, -1, 50_001, 5_000.0, "5000", None])
async def test_builtin_incremental_schedule_rejects_invalid_limit_before_job(
    monkeypatch: pytest.MonkeyPatch, limit: object
) -> None:
    def must_not_run(*args: object, **kwargs: object) -> Any:
        raise AssertionError("an invalid schedule limit must fail before the job starts")

    monkeypatch.setattr(jobs, "run_incremental_job", must_not_run)
    template = _incremental_template({"domain": "stock_daily", "limit": limit})

    with pytest.raises(ValueError, match="exact integer from 1 through 50000"):
        await jobs._execute_template(template)
