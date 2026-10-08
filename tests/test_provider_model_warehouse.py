"""Offline route-to-reader tests for the native provider-model warehouse API."""

from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Literal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql
from starlette.requests import Request

from opendata.api import provider_model_warehouse as warehouse_api
from opendata.api.dependencies import get_current_principal
from opendata.api.provider_models import router as provider_models_router
from opendata.data import async_execution, domains
from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)
from opendata.pipeline import ddl, provider_model_codec
from opendata.services import provider_model_store
from opendata.services.provider_model_warehouse import (
    ProviderModelWarehouseCancelledError,
    ProviderModelWarehouseTimeoutError,
    query_provider_model_warehouse,
)

_BODY_LIMIT = 1024 * 1024

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for source in ("fred", "bls", "fmp"):
        register_provider(source, registry)
    return registry


def _grant(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.QUERY,
    decision: GrantDecision = GrantDecision.ALLOWED,
    conditions: tuple[str, ...] = (),
    expires_at: datetime | None = None,
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="offline-test-query-rights-record",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts=("warehouse.example.test",),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
        conditions=conditions,
    )


def _context(
    source: str,
    model: str,
    *,
    grants: Sequence[RequestGrant] | None = None,
    timeout: float = 3.0,
    cancel: threading.Event | None = None,
    operation: RequestOperation = RequestOperation.QUERY,
    budget: RequestBudget | None = None,
) -> FetchContext:
    effective_budget = budget
    if effective_budget is None and grants is not None:
        effective_budget = RequestBudget(
            task_attempts=2,
            source_attempts=2,
            grants=grants,
        )
    if effective_budget is None:
        effective_budget = RequestBudget(
            task_attempts=2,
            source_attempts=2,
            grants=(_grant(source, model),),
        )
    return FetchContext(
        timeout=timeout,
        _thread_cancel_event=cancel,
        request_budget=effective_budget,
        operation=operation,
    )


def _app(
    registry: ProviderRegistry,
    *,
    ctx: FetchContext | None,
    allowed: bool = True,
) -> TestClient:
    app = FastAPI()
    app.include_router(provider_models_router, prefix="/api/v1/providers")
    principal = SimpleNamespace(allows_domain=lambda _domain: allowed)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[warehouse_api.get_provider_model_warehouse_registry] = lambda: registry
    app.dependency_overrides[warehouse_api.get_provider_model_warehouse_context] = lambda: ctx
    return TestClient(app)


def _path(source: str, model: str) -> str:
    return f"/api/v1/providers/{source}/models/{model}/warehouse/query"


def _request_for_receive(receive: Any) -> Request:
    """Build a minimal ASGI request for stalled-body deadline tests."""
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": _path("fred", "FredSeries"),
            "raw_path": _path("fred", "FredSeries").encode(),
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 1000),
            "server": ("testserver", 80),
            "root_path": "",
        },
        receive,
    )


def _fred_row(
    *,
    value: float | None,
    realtime_start: date,
    series_id: str = "CPIAUCSL",
) -> SeriesObservation:
    return SeriesObservation(
        series_id=series_id,
        date=date(2024, 2, 1),
        value=value,
        realtime_start=realtime_start,
        realtime_end=date(2024, 12, 31),
        transform_units="lin",
        output_type=1,
        requested_frequency=None,
        requested_aggregation_method="avg",
    )


def _bls_row(
    *,
    period: str = "M13",
    latest: bool | None = None,
    preliminary: bool = True,
) -> BlsObservation:
    return BlsObservation(
        series_id="LNS14000000",
        year=2025,
        period=period,
        period_name="Annual average" if period == "M13" else "January",
        value=4.1,
        footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
        latest=latest,
        preliminary=preliminary,
        api_version="v2",
    )


def _fmp_row(day: int, volume: int | float | None) -> EquityHistorical:
    return EquityHistorical(
        symbol="AAPL",
        date=date(2024, 1, day),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=volume,
        change=0.5,
        change_percent=0.5,
        vwap=100.25,
        currency=None,
        currency_semantics="source_unverified",
        volume_unit=None,
        volume_unit_semantics="source_unverified",
        query_window_scope="explicit",
        window_boundary_semantics="source_unverified",
        provider_default_window_semantics="source_unverified",
        close_adjustment_semantics="split_adjusted_per_source_faq",
        adj_close_provided=False,
    )


def _storage_record(
    domain: str,
    row: SeriesObservation | BlsObservation | EquityHistorical,
) -> dict[str, object]:
    record = provider_model_codec.encode_model_storage_row(domain, row)
    if domain == "bls_series":
        record["latest"] = None if row.latest is None else int(row.latest)
        record["preliminary"] = int(row.preliminary)
    elif domain == "equity_historical":
        record["adj_close_provided"] = 0
    record.update(
        {
            "source": {
                "fred_series": "fred",
                "bls_series": "bls",
                "equity_historical": "fmp",
            }[domain],
            "_merged_at": datetime(2026, 10, 8, 2, 30),
            "_diff_flag": 0,
            "_as_of": None,
        }
    )
    return record


def _column_type(sql_type: str) -> Any:
    match = re.match(r"^varchar\((\d+)\)", sql_type, re.IGNORECASE)
    if match is not None:
        kwargs: dict[str, object] = {}
        for option, key in (("CHARACTER SET", "charset"), ("COLLATE", "collation")):
            option_match = re.search(rf"\b{option}\s+(\w+)", sql_type, re.IGNORECASE)
            if option_match is not None:
                kwargs[key] = option_match.group(1)
        return mysql.VARCHAR(int(match.group(1)), **kwargs)
    normalized = sql_type.lower()
    if normalized == "date":
        return mysql.DATE()
    if normalized == "datetime":
        return mysql.DATETIME()
    if normalized == "double":
        return mysql.DOUBLE()
    if normalized == "bigint":
        return mysql.BIGINT()
    if normalized == "json":
        return mysql.JSON()
    if normalized == "tinyint(1)":
        return mysql.TINYINT(1)
    raise AssertionError(f"unexpected native type: {sql_type}")


def _expected_columns(domain: str) -> tuple[ddl.Column, ...]:
    return provider_model_store._expected_columns(domain)


class RecordingInspector:
    def __init__(self, domain: str) -> None:
        self.columns = [
            {
                "name": column.name,
                "type": _column_type(column.sql_type),
                "nullable": column.nullable,
                "default": column.sql_default,
            }
            for column in _expected_columns(domain)
        ]
        self.indexes = [
            {
                "INDEX_NAME": "PRIMARY",
                "NON_UNIQUE": 0,
                "SEQ_IN_INDEX": position,
                "COLUMN_NAME": name,
                "SUB_PART": None,
                "EXPRESSION": None,
                "INDEX_TYPE": "BTREE",
            }
            for position, name in enumerate(
                provider_model_codec.physical_model_key(domain),
                start=1,
            )
        ]

    def has_table(self, _table: str) -> bool:
        return True

    def get_columns(self, _table: str) -> list[dict[str, object]]:
        return self.columns

    def get_pk_constraint(self, _table: str) -> dict[str, object]:
        return {
            "name": "PRIMARY",
            "constrained_columns": list(self.physical_key),
        }

    @property
    def physical_key(self) -> tuple[str, ...]:
        return tuple(row["COLUMN_NAME"] for row in self.indexes)

    def get_unique_constraints(self, _table: str) -> list[dict[str, object]]:
        return []

    def get_indexes(self, _table: str) -> list[dict[str, object]]:
        return []


class RecordingResult:
    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]

    def mappings(self) -> RecordingResult:
        return self

    def all(self) -> list[dict[str, object]]:
        return self.rows

    def fetchmany(self, size: int) -> list[dict[str, object]]:
        return self.rows[:size]


class RecordingConnection:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine
        self.dialect = mysql.dialect()

    def execute(self, statement: object, parameters: object) -> RecordingResult:
        sql = str(statement)
        if not self.engine.in_transaction:
            raise AssertionError("schema metadata and SELECT must share one transaction")
        self.engine.executed_thread_names.append(threading.current_thread().name)
        if "INFORMATION_SCHEMA.STATISTICS" in sql:
            self.engine.metadata_queries.append((sql, parameters))
            return RecordingResult(self.engine.index_rows)
        if not sql.lstrip().startswith("SELECT "):
            raise AssertionError("warehouse reader executed non-SELECT SQL")
        self.engine.selects.append((sql, parameters))
        self.engine.select_entered.set()
        if self.engine.block_select and not self.engine.release_select.wait(timeout=5):
            raise AssertionError("test did not release blocked SELECT")
        return RecordingResult(self.engine.rows)


class _Transaction:
    def __init__(self, engine: RecordingEngine) -> None:
        self.engine = engine

    def __enter__(self) -> RecordingConnection:
        self.engine.begin_calls += 1
        self.engine.in_transaction = True
        return self.engine.connection

    def __exit__(self, exc_type: object, _exc: object, _traceback: object) -> Literal[False]:
        self.engine.in_transaction = False
        if exc_type is None:
            self.engine.commit_calls += 1
        else:
            self.engine.rollback_calls += 1
        self.engine.transaction_finished.set()
        return False


class RecordingEngine:
    def __init__(
        self,
        domain: str,
        rows: Sequence[Mapping[str, object]],
        *,
        block_select: bool = False,
    ) -> None:
        self.dialect = SimpleNamespace(name="mysql")
        self.connection = RecordingConnection(self)
        self.rows = [dict(row) for row in rows]
        self.index_rows = RecordingInspector(domain).indexes
        self.block_select = block_select
        self.release_select = threading.Event()
        self.select_entered = threading.Event()
        self.transaction_finished = threading.Event()
        self.in_transaction = False
        self.begin_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.executed_thread_names: list[str] = []
        self.metadata_queries: list[tuple[str, object]] = []
        self.selects: list[tuple[str, object]] = []

    def begin(self) -> _Transaction:
        return _Transaction(self)


def _install_inspector(monkeypatch: pytest.MonkeyPatch, domain: str) -> None:
    inspector = RecordingInspector(domain)

    def inspect_connection(connection: object) -> RecordingInspector:
        assert isinstance(connection, RecordingConnection)
        assert connection.engine.in_transaction
        return inspector

    monkeypatch.setattr(provider_model_store, "inspect", inspect_connection)


@pytest.mark.parametrize(
    ("source", "model", "domain", "query", "rows"),
    [
        (
            "fred",
            "FredSeries",
            "fred_series",
            {
                "filters": {
                    "series_id": "CPIAUCSL' OR 1=1 --",
                    "requested_frequency": None,
                    "date": "2024-02-01",
                },
                "start": "2024-02-01",
                "end": "2024-02-28",
                "limit": 2,
                "offset": 3,
            },
            [
                _storage_record(
                    "fred_series",
                    _fred_row(
                        value=None,
                        realtime_start=date(2024, 2, 1),
                        series_id="CPIAUCSL' OR 1=1 --",
                    ),
                ),
                _storage_record(
                    "fred_series",
                    _fred_row(
                        value=3.2,
                        realtime_start=date(2024, 3, 1),
                        series_id="CPIAUCSL' OR 1=1 --",
                    ),
                ),
            ],
        ),
        (
            "bls",
            "BlsSeries",
            "bls_series",
            {
                "filters": {"series_id": "LNS14000000", "period": "M13"},
                "start": 2025,
                "end": 2025,
                "limit": 1,
            },
            [_storage_record("bls_series", _bls_row())],
        ),
        (
            "fmp",
            "EquityHistorical",
            "equity_historical",
            {
                "filters": {"symbol": "AAPL"},
                "start": "2024-01-01",
                "end": "2024-01-03",
                "limit": 3,
            },
            [
                _storage_record("equity_historical", _fmp_row(1, 2**53 + 1)),
                _storage_record("equity_historical", _fmp_row(2, -0.0)),
                _storage_record("equity_historical", _fmp_row(3, None)),
            ],
        ),
    ],
    ids=["fred-revisions-null-frequency", "bls-m13-footnotes-tinyints", "fmp-volume-types"],
)
def test_route_reads_typed_rows_through_bounded_worker_and_native_reader(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    domain: str,
    query: dict[str, object],
    rows: list[dict[str, object]],
) -> None:
    registry = _registry()
    engine = RecordingEngine(domain, rows)
    _install_inspector(monkeypatch, domain)
    factory_calls: list[str] = []
    extract_calls: list[str] = []
    fetcher = registry.resolve_model(source, model)
    monkeypatch.setattr(
        fetcher,
        "extract_data",
        lambda *_args, **_kwargs: extract_calls.append(model),
    )

    def factory() -> Any:
        factory_calls.append(threading.current_thread().name)
        return engine

    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", factory)
    context = _context(source, model)

    with _app(registry, ctx=context) as client:
        response = client.post(_path(source, model), json={"query": query})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["source"] == source
    assert data["model"] == model
    assert data["domain"] == domain
    assert data["verified"] is False
    assert data["completeness"] == "NOT_ASSESSED"
    assert datetime.fromisoformat(data["read_at"]).utcoffset() == timedelta(0)
    assert data["pagination"] == {
        "limit": query.get("limit", 1_000),
        "offset": query.get("offset", 0),
        "returned": len(rows),
        "total": None,
    }
    assert data["snapshot_scope"] == "single_read_transaction"
    assert factory_calls and all(name.startswith("opendata-fetch") for name in factory_calls)
    assert extract_calls == []
    assert engine.begin_calls == engine.commit_calls == 1
    assert engine.rollback_calls == 0
    assert len(engine.metadata_queries) == len(engine.selects) == 1
    assert all(name.startswith("opendata-fetch") for name in engine.executed_thread_names)
    assert context.request_budget is not None
    assert context.request_budget.attempts_used == 0
    assert "CREATE " not in engine.selects[0][0].upper()
    assert "INSERT " not in engine.selects[0][0].upper()
    assert "COUNT(" not in engine.selects[0][0].upper()

    if domain == "fred_series":
        output_rows = data["results"]
        assert len(output_rows) == 2
        assert output_rows[0]["date"] == "2024-02-01"
        assert output_rows[0]["value"] is None
        assert output_rows[0]["requested_frequency"] is None
        sql, bound = engine.selects[0]
        assert "`requested_frequency` IS NULL" in sql
        assert "CPIAUCSL' OR 1=1 --" not in sql
        assert "CPIAUCSL' OR 1=1 --" in bound.values()
        assert date(2024, 2, 1) in bound.values()
        assert bound["start"] == date(2024, 2, 1)
        assert bound["end"] == date(2024, 2, 28)
    elif domain == "bls_series":
        output = data["results"][0]
        assert output["period"] == "M13"
        assert output["period_name"] == "Annual average"
        assert output["footnotes"] == [
            {"code": "P", "text": "Preliminary."},
            {"code": None, "text": None},
        ]
        assert output["latest"] is None
        assert output["preliminary"] is True
        assert engine.selects[0][1]["start"] == 2025
    else:
        output_rows = data["results"]
        volumes = [row["volume"] for row in output_rows]
        assert volumes == [2**53 + 1, -0.0, None]
        assert type(volumes[0]) is int
        assert type(volumes[1]) is float
        assert math.copysign(1.0, volumes[1]) == -1.0
        assert all(row["currency"] is None and row["volume_unit"] is None for row in output_rows)
        assert all(row["adj_close_provided"] is False for row in output_rows)
        assert all(row["date"] == f"2024-01-0{day}" for day, row in enumerate(output_rows, start=1))


@pytest.mark.parametrize(
    ("source", "model", "ctx_factory", "allowed", "query", "expected_status"),
    [
        ("fred", "FredSeries", lambda: None, True, {"filters": {}}, 403),
        (
            "fred",
            "FredSeries",
            lambda: _context(
                "fred",
                "FredSeries",
                grants=(_grant("fred", "FredSeries", operation=RequestOperation.STORE),),
            ),
            True,
            {"filters": {}},
            403,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context(
                "fred",
                "FredSeries",
                grants=(
                    _grant(
                        "fred",
                        "FredSeries",
                        decision=GrantDecision.UNKNOWN,
                        conditions=("pending-rights-review",),
                    ),
                ),
            ),
            True,
            {"filters": {}},
            403,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context(
                "fred",
                "FredSeries",
                grants=(
                    _grant(
                        "fred",
                        "FredSeries",
                        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                    ),
                ),
            ),
            True,
            {"filters": {}},
            403,
        ),
        ("fred", "FredSeries", lambda: _context("fred", "FredSeries"), False, {"filters": {}}, 403),
        ("fmp", "EquityQuote", lambda: _context("fmp", "EquityQuote"), True, {"filters": {}}, 404),
        ("bls", "BlsSearch", lambda: _context("bls", "BlsSearch"), True, {"filters": {}}, 404),
        ("bls", "FredSeries", lambda: _context("bls", "FredSeries"), True, {"filters": {}}, 404),
        (
            "fred",
            "FredSeries",
            lambda: _context("fred", "FredSeries"),
            True,
            {"filters": {"series_id": "GDP", "sql": "DROP TABLE dwd_fred_series"}},
            400,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context("fred", "FredSeries"),
            True,
            {"filters": {}, "ctx": {"operation": "query"}},
            400,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context("fred", "FredSeries"),
            True,
            {"filters": {"series_id": "GDP"}, "start": "2024-1-01"},
            400,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context("fred", "FredSeries"),
            True,
            {"filters": {"date": "2024-2-01"}},
            400,
        ),
        (
            "bls",
            "BlsSeries",
            lambda: _context("bls", "BlsSeries"),
            True,
            {"filters": {"series_id": "SERIES"}, "start": True},
            400,
        ),
        (
            "fmp",
            "EquityHistorical",
            lambda: _context("fmp", "EquityHistorical"),
            True,
            {"filters": {"symbol": "\x00AAPL"}},
            400,
        ),
        (
            "fred",
            "FredSeries",
            lambda: _context("fred", "FredSeries"),
            True,
            {"filters": {}, "limit": True},
            400,
        ),
    ],
    ids=[
        "default-zero-grant",
        "wrong-operation-grant",
        "unknown-conditional-grant",
        "expired-grant",
        "domain-acl-denied",
        "quote-not-readable",
        "search-not-readable",
        "wrong-source-model",
        "unknown-filter",
        "client-context-rejected",
        "noncanonical-date",
        "noncanonical-date-filter",
        "year-bool",
        "invalid-fmp-symbol",
        "limit-bool",
    ],
)
def test_rejection_precedes_engine_factory_and_source_io(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    ctx_factory: Any,
    allowed: bool,
    query: dict[str, object],
    expected_status: int,
) -> None:
    registry = _registry()
    factory_calls: list[str] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: factory_calls.append(threading.current_thread().name),
    )

    with _app(registry, ctx=ctx_factory(), allowed=allowed) as client:
        response = client.post(_path(source, model), json={"query": query})

    assert response.status_code == expected_status
    assert factory_calls == []
    assert all(secret not in response.text for secret in ("DROP TABLE", "\x00AAPL"))


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":{"filters":{}},"query":{"filters":{}}}',
        b'{"query":{"filters":{"series_id":"GDP","series_id":"CPI"}}}',
        b'{"query":{"filters":{},"limit":NaN}}',
        b'{"query":{"filters":{},"other":' + b"[" * 260 + b"0" + b"]" * 260 + b"}}",
        b'{"query":{"filters":{}},"extra":"rejected"}',
    ],
    ids=["duplicate-root-key", "duplicate-filter-key", "nan", "excessive-depth", "extra-envelope"],
)
def test_strict_json_rejections_never_resolve_warehouse_engine(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    registry = _registry()
    context = _context("fred", "FredSeries")
    calls: list[None] = []
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", lambda: calls.append(None))

    with _app(registry, ctx=context) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid warehouse query request"
    assert calls == []


def test_oversized_stream_stops_before_engine_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()
    calls: list[None] = []
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", lambda: calls.append(None))
    body = b"{" + b" " * _BODY_LIMIT + b"}"

    with _app(registry, ctx=_context("fred", "FredSeries")) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            content=body,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["detail"] == "Warehouse query request is too large"
    assert calls == []


def test_stalled_body_is_cancelled_at_context_admission_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_calls: list[None] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: engine_calls.append(None),
    )
    receive_started = asyncio.Event()
    receive_cancelled = threading.Event()

    async def receive() -> Any:
        receive_started.set()
        try:
            await asyncio.Future()
        finally:
            receive_cancelled.set()

    async def call_route() -> HTTPException:
        with pytest.raises(HTTPException) as raised:
            await warehouse_api.query_registered_provider_model_warehouse(
                "fred",
                "FredSeries",
                _request_for_receive(receive),
                current_principal=SimpleNamespace(allows_domain=lambda _domain: True),
                registry=_registry(),
                ctx=_context("fred", "FredSeries", timeout=0.03),
            )
        return raised.value

    raised = asyncio.run(call_route())
    assert raised.status_code == 504
    assert raised.detail == "Warehouse query timed out"
    assert receive_started.is_set()
    assert receive_cancelled.wait(1)
    assert engine_calls == []


def test_slow_fragmented_body_uses_one_admission_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_calls: list[None] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: engine_calls.append(None),
    )
    receive_calls = 0
    second_receive_started = asyncio.Event()
    second_receive_cancelled = threading.Event()

    async def receive() -> Any:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            await asyncio.sleep(0.07)
            return {"type": "http.request", "body": b'{"query":', "more_body": True}
        second_receive_started.set()
        try:
            await asyncio.sleep(0.07)
            return {"type": "http.request", "body": b'{"filters":{}}}', "more_body": False}
        finally:
            second_receive_cancelled.set()

    async def call_route() -> HTTPException:
        with pytest.raises(HTTPException) as raised:
            await warehouse_api.query_registered_provider_model_warehouse(
                "fred",
                "FredSeries",
                _request_for_receive(receive),
                current_principal=SimpleNamespace(allows_domain=lambda _domain: True),
                registry=_registry(),
                ctx=_context("fred", "FredSeries", timeout=0.1),
            )
        return raised.value

    raised = asyncio.run(call_route())
    assert raised.status_code == 504
    assert second_receive_started.is_set()
    assert second_receive_cancelled.wait(1)
    assert engine_calls == []


def test_context_cancellation_during_body_returns_503_and_cancels_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_calls: list[None] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: engine_calls.append(None),
    )
    receive_started = asyncio.Event()
    receive_cancelled = threading.Event()
    cancel = threading.Event()

    async def receive() -> Any:
        receive_started.set()
        try:
            await asyncio.Future()
        finally:
            receive_cancelled.set()

    async def call_route() -> HTTPException:
        async def signal_cancel() -> None:
            await receive_started.wait()
            cancel.set()

        cancel_task = asyncio.create_task(signal_cancel())
        try:
            with pytest.raises(HTTPException) as raised:
                await warehouse_api.query_registered_provider_model_warehouse(
                    "fred",
                    "FredSeries",
                    _request_for_receive(receive),
                    current_principal=SimpleNamespace(allows_domain=lambda _domain: True),
                    registry=_registry(),
                    ctx=_context("fred", "FredSeries", timeout=2, cancel=cancel),
                )
            return raised.value
        finally:
            await cancel_task

    raised = asyncio.run(call_route())
    assert raised.status_code == 503
    assert raised.detail == "Warehouse query was cancelled"
    assert receive_cancelled.wait(1)
    assert engine_calls == []


def test_inherited_deadline_bounds_body_without_resolving_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_calls: list[None] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: engine_calls.append(None),
    )
    receive_started = asyncio.Event()
    receive_cancelled = threading.Event()

    async def receive() -> Any:
        receive_started.set()
        try:
            await asyncio.Future()
        finally:
            receive_cancelled.set()

    async def call_route() -> HTTPException:
        context = _context("fred", "FredSeries", timeout=2)
        assert context.request_budget is not None
        with (
            pytest.raises(HTTPException) as raised,
            request_execution_scope(
                source="fred",
                canonical_model="FredSeries",
                operation=RequestOperation.QUERY,
                budget=context.request_budget,
                deadline=time.monotonic() + 0.03,
            ),
        ):
            await warehouse_api.query_registered_provider_model_warehouse(
                "fred",
                "FredSeries",
                _request_for_receive(receive),
                current_principal=SimpleNamespace(allows_domain=lambda _domain: True),
                registry=_registry(),
                ctx=context,
            )
        return raised.value

    raised = asyncio.run(call_route())
    assert raised.status_code == 504
    assert receive_started.is_set()
    assert receive_cancelled.wait(1)
    assert engine_calls == []


def test_caller_cancellation_propagates_and_drains_body_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_calls: list[None] = []
    monkeypatch.setattr(
        warehouse_api,
        "warehouse_engine_factory",
        lambda: engine_calls.append(None),
    )
    receive_started = asyncio.Event()
    receive_cancelled = threading.Event()

    async def receive() -> Any:
        receive_started.set()
        try:
            await asyncio.Future()
        finally:
            receive_cancelled.set()

    async def run() -> None:
        task = asyncio.create_task(
            warehouse_api.query_registered_provider_model_warehouse(
                "fred",
                "FredSeries",
                _request_for_receive(receive),
                current_principal=SimpleNamespace(allows_domain=lambda _domain: True),
                registry=_registry(),
                ctx=_context("fred", "FredSeries", timeout=2),
            )
        )
        await receive_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert receive_cancelled.wait(1)
    assert engine_calls == []


def test_bad_last_row_fails_whole_page_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    rows = [
        _storage_record("fred_series", _fred_row(value=1.0, realtime_start=date(2024, 2, 1))),
        _storage_record("fred_series", _fred_row(value=2.0, realtime_start=date(2024, 3, 1))),
    ]
    rows[-1]["source"] = "bls"
    engine = RecordingEngine("fred_series", rows)
    _install_inspector(monkeypatch, "fred_series")
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", lambda: engine)

    with _app(registry, ctx=_context("fred", "FredSeries")) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            json={"query": {"filters": {"series_id": "CPIAUCSL"}, "limit": 2}},
        )

    assert response.status_code == 503
    assert response.json()["detail"] == "Warehouse query is unavailable"
    assert "CPIAUCSL" not in response.text
    assert engine.begin_calls == 1
    assert engine.commit_calls == 0
    assert engine.rollback_calls == 1
    assert len(engine.selects) == 1


def test_query_permission_configuration_fails_before_engine_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    original = domains.require_domain_semantics
    spec = original("fred_series")
    monkeypatch.setattr(
        domains,
        "require_domain_semantics",
        lambda domain: (
            spec.model_copy(update={"permissions": ("store",)})
            if domain == "fred_series"
            else original(domain)
        ),
    )
    calls: list[None] = []
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", lambda: calls.append(None))

    with _app(registry, ctx=_context("fred", "FredSeries")) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            json={"query": {"filters": {}}},
        )

    assert response.status_code == 503
    assert calls == []


def test_registry_configuration_error_precedes_engine_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", lambda: calls.append(None))

    with _app(object(), ctx=_context("fred", "FredSeries")) as client:  # type: ignore[arg-type]
        response = client.post(
            _path("fred", "FredSeries"),
            json={"query": {"filters": {}}},
        )

    assert response.status_code == 503
    assert calls == []


def test_engine_factory_failure_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "WAREHOUSE_FACTORY_SECRET"

    def fail_factory() -> Any:
        raise RuntimeError(secret)

    registry = _registry()
    monkeypatch.setattr(warehouse_api, "warehouse_engine_factory", fail_factory)
    with _app(registry, ctx=_context("fred", "FredSeries")) as client:
        response = client.post(
            _path("fred", "FredSeries"),
            json={"query": {"filters": {}}},
        )

    assert response.status_code == 503
    assert secret not in response.text


@pytest.mark.parametrize(
    "deadline_source", ["inherited", "fetch-context"], ids=["inherited", "context"]
)
def test_stricter_deadline_cancels_response_but_keeps_worker_slot_until_read_ends(
    monkeypatch: pytest.MonkeyPatch,
    deadline_source: str,
) -> None:
    registry = _registry()
    engine = RecordingEngine(
        "fred_series",
        [_storage_record("fred_series", _fred_row(value=1.0, realtime_start=date(2024, 2, 1)))],
        block_select=True,
    )
    _install_inspector(monkeypatch, "fred_series")
    grant_context = _context("fred", "FredSeries")
    assert grant_context.request_budget is not None
    parent_deadline = time.monotonic() + 0.15
    before = async_execution._active_workers

    context = FetchContext(
        timeout=2.0,
        _deadline_monotonic=parent_deadline if deadline_source == "fetch-context" else None,
        request_budget=grant_context.request_budget if deadline_source == "fetch-context" else None,
        operation=RequestOperation.QUERY,
    )

    async def run_read() -> None:
        task = asyncio.create_task(
            query_provider_model_warehouse(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"filters": {"series_id": "CPIAUCSL"}},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: engine,
                ctx=context,
            )
        )
        await _wait_for_async_event(engine.select_entered)
        with pytest.raises(ProviderModelWarehouseTimeoutError):
            await task
        assert async_execution._active_workers == before + 1

    async def run() -> None:
        if deadline_source == "inherited":
            with request_execution_scope(
                source="fred",
                canonical_model="FredSeries",
                operation=RequestOperation.QUERY,
                budget=grant_context.request_budget,
                deadline=parent_deadline,
            ):
                await run_read()
        else:
            await run_read()
        engine.release_select.set()
        await _wait_for_async_event(engine.transaction_finished)
        await _wait_for_worker_count(before)

    asyncio.run(run())
    assert engine.rollback_calls == 1
    assert async_execution._active_workers == before


async def _wait_for_async_event(event: threading.Event) -> None:
    deadline = time.monotonic() + 2.0
    while not event.is_set():
        if time.monotonic() >= deadline:
            raise AssertionError("bounded worker event was not observed")
        await asyncio.sleep(0.005)


async def _wait_for_worker_count(expected: int) -> None:
    deadline = time.monotonic() + 2.0
    while async_execution._active_workers != expected:
        if time.monotonic() >= deadline:
            raise AssertionError("bounded worker slot was not released after work ended")
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("cancel_caller", [False, True], ids=["context-cancel", "caller-cancel"])
def test_cancellation_never_frees_worker_slot_before_blocking_read_ends(
    monkeypatch: pytest.MonkeyPatch,
    cancel_caller: bool,
) -> None:
    registry = _registry()
    engine = RecordingEngine(
        "fred_series",
        [_storage_record("fred_series", _fred_row(value=1.0, realtime_start=date(2024, 2, 1)))],
        block_select=True,
    )
    _install_inspector(monkeypatch, "fred_series")
    cancel = threading.Event()
    context = _context("fred", "FredSeries", timeout=2, cancel=cancel)
    before = async_execution._active_workers

    async def run() -> None:
        task = asyncio.create_task(
            query_provider_model_warehouse(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"filters": {"series_id": "CPIAUCSL"}},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: engine,
                ctx=context,
            )
        )
        await _wait_for_async_event(engine.select_entered)
        if cancel_caller:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            cancel.set()
            with pytest.raises(ProviderModelWarehouseCancelledError):
                await task
        assert async_execution._active_workers == before + 1
        engine.release_select.set()
        await _wait_for_async_event(engine.transaction_finished)
        await _wait_for_worker_count(before)

    asyncio.run(run())
    assert engine.rollback_calls == 1
    assert async_execution._active_workers == before
