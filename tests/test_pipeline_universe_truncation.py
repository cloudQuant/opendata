"""Guard the implicit DWD universe against silent cap truncation."""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from types import ModuleType
from typing import Any

import pytest

from opendata.pipeline import jobs
from opendata.pipeline.runner import Window
from opendata.pipeline.trading_calendar import weekday_calendar


class _SyntheticResult:
    """Small in-memory stand-in for the DWD ``SELECT DISTINCT`` result."""

    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    def all(self) -> list[tuple[str]]:
        return [(symbol,) for symbol in self._symbols]


class _SyntheticConnection:
    """Record the SQL cap and return only fixture-owned rows."""

    def __init__(self, symbols: list[str], calls: list[dict[str, Any]]) -> None:
        self._symbols = symbols
        self._calls = calls

    def __enter__(self) -> _SyntheticConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, int]) -> _SyntheticResult:
        self._calls.append({"statement": str(statement), "params": dict(params)})
        return _SyntheticResult(self._symbols)


class _SyntheticWarehouse:
    """Warehouse-shaped fixture with no database or network connection."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.calls: list[dict[str, Any]] = []

    def connect(self) -> _SyntheticConnection:
        return _SyntheticConnection(self.symbols, self.calls)


WINDOW = Window(start=date(2026, 9, 24), end=date(2026, 9, 24))


def test_implicit_dwd_read_probes_one_past_cap_and_accepts_exact_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _SyntheticWarehouse(["000001", "600519"])
    monkeypatch.setattr(jobs, "landed_instruments", lambda engine: [])

    symbols, provenance = jobs.derive_universe(
        warehouse, "stock_daily", symbols=None, limit=2, window=WINDOW
    )

    assert symbols == ["000001", "600519"]
    assert warehouse.calls[0]["params"] == {"cap": 3}
    assert "dwd_stock_daily" in warehouse.calls[0]["statement"]
    assert provenance == {"from": "dwd_stock_daily", "catalog": "absent"}


def test_implicit_dwd_read_fails_before_catalog_when_cap_is_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _SyntheticWarehouse(["000001", "000002", "000003"])

    def catalog_must_not_run(engine: object) -> list[object]:
        raise AssertionError("cap overflow must be rejected before catalog filtering")

    monkeypatch.setattr(jobs, "landed_instruments", catalog_must_not_run)

    with pytest.raises(ValueError, match="derived universe from dwd_stock_daily exceeds limit 2"):
        jobs.derive_universe(warehouse, "stock_daily", symbols=None, limit=2, window=WINDOW)

    assert warehouse.calls[0]["params"] == {"cap": 3}


def test_zero_limit_keeps_existing_minimum_one_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _SyntheticWarehouse(["600519"])
    monkeypatch.setattr(jobs, "landed_instruments", lambda engine: [])

    symbols, _ = jobs.derive_universe(warehouse, "stock_daily", symbols=[], limit=0, window=WINDOW)

    assert symbols == ["600519"]
    assert warehouse.calls[0]["params"] == {"cap": 2}


def test_explicit_nonempty_symbols_ignore_derived_cap_and_warehouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def derived_read_must_not_run(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("explicit symbols must bypass DWD derivation")

    monkeypatch.setattr(jobs, "symbol_universe", derived_read_must_not_run)
    monkeypatch.setattr(jobs, "landed_instruments", derived_read_must_not_run)

    symbols, provenance = jobs.derive_universe(
        object(),
        "stock_daily",
        symbols=["000001", "600519"],
        limit=True,
        window=WINDOW,
    )

    assert symbols == ["000001", "600519"]
    assert provenance == {"from": "caller", "requested": "2"}


def test_max_integer_cap_fails_before_warehouse_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def warehouse_read_must_not_run(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("an unrepresentable cap must fail before querying")

    monkeypatch.setattr(jobs, "symbol_universe", warehouse_read_must_not_run)

    with pytest.raises(ValueError, match="cannot be probed safely"):
        jobs.derive_universe(
            object(), "stock_daily", symbols=None, limit=sys.maxsize, window=WINDOW
        )


@pytest.mark.parametrize("limit", [True, 2.5, Decimal("2")])
def test_implicit_universe_rejects_non_integer_limit_before_warehouse_query(
    monkeypatch: pytest.MonkeyPatch, limit: Any
) -> None:
    def warehouse_read_must_not_run(*args: object, **kwargs: object) -> list[str]:
        raise AssertionError("an invalid cap must fail before querying")

    monkeypatch.setattr(jobs, "symbol_universe", warehouse_read_must_not_run)

    with pytest.raises(ValueError, match="limit must be an integer or None"):
        jobs.derive_universe(object(), "stock_daily", symbols=None, limit=limit, window=WINDOW)


@pytest.mark.asyncio
async def test_run_rejects_truncated_implicit_universe_before_provider_or_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    warehouse = _SyntheticWarehouse(["000001", "000002", "000003"])
    database_module = ModuleType("opendata.core.database")
    database_module.async_session_maker = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opendata.core.database", database_module)

    def must_not_be_called(*args: object, **kwargs: object) -> Any:
        raise AssertionError("provider and pipeline execution must follow cap validation")

    monkeypatch.setattr(jobs, "landed_instruments", lambda engine: [])
    monkeypatch.setattr(jobs, "make_fetch_symbol_async", must_not_be_called)
    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", must_not_be_called)

    with pytest.raises(ValueError, match="derived universe from dwd_stock_daily exceeds limit 2"):
        await jobs.run_incremental_job(
            domain="stock_daily",
            source="ths",
            limit=2,
            as_of=WINDOW.start,
            engine=warehouse,
            calendar=weekday_calendar(),
        )

    assert warehouse.calls[0]["params"] == {"cap": 3}
