"""Regression tests for safe warehouse-derived symbol-universe failures."""

from __future__ import annotations

import sqlite3
import sys
from datetime import date
from types import ModuleType
from typing import Any

import pytest
from pymysql.err import OperationalError as PyMySQLOperationalError
from sqlalchemy.exc import (
    ArgumentError,
    CompileError,
    DisconnectionError,
    InvalidRequestError,
    OperationalError,
)
from sqlalchemy.exc import (
    TimeoutError as PoolTimeoutError,
)

from opendata.pipeline import jobs
from opendata.pipeline.runner import Window
from opendata.pipeline.trading_calendar import calendar_from_days
from opendata.pipeline.universe_errors import (
    EmptySymbolUniverseError,
    UniverseLimitExceededError,
    UniverseQueryError,
)

WINDOW = Window(start=date(2026, 9, 24), end=date(2026, 9, 24))


class _Rows:
    """Minimal result carrying a successful row set."""

    def __init__(self, rows: list[tuple[str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str]]:
        return self._rows


class _Connection:
    """Context manager that returns either rows or a supplied error."""

    def __init__(self, *, rows: list[tuple[str]] | None = None, error: Exception | None = None):
        self.rows = rows
        self.error = error

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, int]) -> _Rows:
        if self.error is not None:
            raise self.error
        assert params["cap"] in {
            jobs.DEFAULT_SYMBOL_LIMIT,
            jobs.DEFAULT_SYMBOL_LIMIT + 1,
        }
        return _Rows(self.rows or [])


class _Warehouse:
    """Small engine double for universe-selection tests."""

    def __init__(self, *, rows: list[tuple[str]] | None = None, error: Exception | None = None):
        self.rows = rows
        self.error = error

    def connect(self) -> _Connection:
        return _Connection(rows=self.rows, error=self.error)


def test_successful_empty_read_is_distinct_from_empty_derived_universe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid zero-row query yields no eligible symbol, never a DB failure."""
    catalog_calls = 0

    def catalog_must_not_run(engine: object, **kwargs: object) -> list[Any]:
        nonlocal catalog_calls
        catalog_calls += 1
        raise AssertionError("an empty DWD universe must stop before catalog lookup")

    monkeypatch.setattr(jobs, "landed_instruments", catalog_must_not_run)
    warehouse = _Warehouse(rows=[])

    assert jobs.symbol_universe(warehouse, "stock_daily") == []
    with pytest.raises(EmptySymbolUniverseError, match="no eligible symbols") as caught:
        jobs.derive_universe(warehouse, "stock_daily", symbols=None, limit=None, window=WINDOW)

    assert isinstance(caught.value, ValueError)
    assert str(caught.value) == "no eligible symbols remain in the derived universe for stock_daily"
    assert catalog_calls == 0


def test_cap_overflow_uses_value_error_compatible_domain_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cap+1 witness is classified before catalog or provider work."""
    captured: dict[str, int] = {}

    def overflow(engine: object, domain: str, *, limit: int | None = None) -> list[str]:
        captured["limit"] = int(limit or 0)
        return ["000001", "000002", "000003"]

    monkeypatch.setattr(jobs, "symbol_universe", overflow)
    monkeypatch.setattr(
        jobs,
        "landed_instruments",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("cap overflow must stop before catalog lookup")
        ),
    )

    with pytest.raises(UniverseLimitExceededError, match="exceeds limit 2") as caught:
        jobs.derive_universe(object(), "stock_daily", symbols=None, limit=2, window=WINDOW)

    assert isinstance(caught.value, ValueError)
    assert captured == {"limit": 3}


@pytest.mark.parametrize(
    "database_error",
    [
        OperationalError(
            "SELECT SQL_CANARY FROM dwd_stock_daily",
            {"dsn": "DSN_CANARY", "parameter": "PARAM_CANARY"},
            RuntimeError("ORIGINAL_CANARY"),
        ),
        sqlite3.OperationalError("SQLITE_ORIGINAL_CANARY"),
        PyMySQLOperationalError(2003, "MYSQL_ORIGINAL_CANARY"),
        PoolTimeoutError("POOL_TIMEOUT_CANARY"),
        DisconnectionError("POOL_DISCONNECTION_CANARY"),
    ],
)
def test_database_errors_are_sanitized_without_retaining_context_or_logging_details(
    database_error: Exception,
) -> None:
    """Supported DB errors expose only the fixed public error and safe log."""
    from loguru import logger

    records: list[str] = []
    handler_id = logger.add(lambda message: records.append(message.record["message"]))
    try:
        with pytest.raises(UniverseQueryError) as caught:
            jobs.symbol_universe(
                _Warehouse(error=database_error), "stock_daily", limit=jobs.DEFAULT_SYMBOL_LIMIT
            )
    finally:
        logger.remove(handler_id)

    assert str(caught.value) == "symbol universe query failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert records == ["symbol universe query failed"]
    rendered = str(caught.value) + "\n".join(records)
    canaries = (
        "SQL_CANARY",
        "DSN_CANARY",
        "PARAM_CANARY",
        "ORIGINAL_CANARY",
        "SQLITE_",
        "MYSQL_",
        "POOL_TIMEOUT_",
        "POOL_DISCONNECTION_",
    )
    for canary in canaries:
        assert canary not in rendered


def test_programming_error_is_not_reclassified_as_a_query_failure() -> None:
    class _BrokenEngine:
        def connect(self) -> Any:
            raise RuntimeError("synthetic programming failure")

    with pytest.raises(RuntimeError, match="synthetic programming failure") as caught:
        jobs.symbol_universe(_BrokenEngine(), "stock_daily")

    assert type(caught.value) is RuntimeError


@pytest.mark.parametrize(
    "programming_error",
    [
        ArgumentError("argument construction canary"),
        InvalidRequestError("invalid request canary"),
        CompileError("statement compile canary"),
    ],
)
def test_sqlalchemy_programming_errors_propagate_unchanged(
    programming_error: Exception,
) -> None:
    """Only DBAPI and connection-pool failures become safe query errors."""
    with pytest.raises(type(programming_error)) as caught:
        jobs.symbol_universe(_Warehouse(error=programming_error), "stock_daily")

    assert caught.value is programming_error


@pytest.mark.asyncio
async def test_query_failure_stops_incremental_job_before_provider_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DWD read failure cannot trigger an empty or partial provider run."""
    database_module = ModuleType("opendata.core.database")
    database_module.async_session_maker = object()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opendata.core.database", database_module)

    calls: list[str] = []

    def provider_must_not_run(*args: object, **kwargs: object) -> Any:
        calls.append("provider")
        raise AssertionError("provider resolution must follow a successful universe read")

    monkeypatch.setattr(jobs, "resolve_fetcher", provider_must_not_run)
    monkeypatch.setattr(jobs, "make_fetch_symbol_async", provider_must_not_run)
    monkeypatch.setattr(jobs, "build_stock_daily_pipeline", provider_must_not_run)

    warehouse = _Warehouse(
        error=OperationalError(
            "SELECT SQL_CANARY FROM dwd_stock_daily",
            {"dsn": "DSN_CANARY"},
            RuntimeError("ORIGINAL_CANARY"),
        )
    )
    with pytest.raises(UniverseQueryError) as caught:
        await jobs.run_incremental_job(
            source="ths",
            engine=warehouse,
            session_maker=object(),
            as_of=WINDOW.start,
            calendar=calendar_from_days([WINDOW.start]),
        )

    assert str(caught.value) == "symbol universe query failed"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert calls == []
