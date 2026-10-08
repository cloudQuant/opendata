"""Offline tests for bounded native capture-and-store ingestion."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from opendata.api import provider_model_ingest as ingest_api
from opendata.api.dependencies import get_current_principal
from opendata.api.provider_model_ingest import (
    get_provider_model_ingest_context,
    get_provider_model_ingest_registry,
    ingest_registered_provider_model,
)
from opendata.api.provider_models import router
from opendata.data import async_execution
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
from opendata.services import provider_model_ingest, provider_model_store
from opendata.services.provider_model_ingest import (
    ProviderModelIngestAuthorizationError,
    ProviderModelIngestCancelledError,
    ProviderModelIngestIdentityError,
    ProviderModelIngestTimeoutError,
    ProviderModelIngestUnavailableError,
    ProviderModelIngestValidationError,
    ingest_provider_model,
)

_HOSTS = {
    "fred": "api.stlouisfed.org",
    "bls": "api.bls.gov",
    "fmp": "financialmodelingprep.com",
}


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for source in ("fred", "bls", "fmp"):
        register_provider(source, registry)
    return registry


def _grant(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.STORE,
    decision: GrantDecision = GrantDecision.ALLOWED,
    conditions: tuple[str, ...] = (),
    expires_at: datetime | None = None,
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=decision,
        rights_evidence="offline-ingest-test-rights-record",
        task_attempts=3,
        source_attempts=3,
        allowed_hosts=(_HOSTS[source],),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(days=1),
        conditions=conditions,
    )


def _context(
    source: str = "fred",
    model: str = "FredSeries",
    *,
    operation: RequestOperation = RequestOperation.STORE,
    grants: tuple[RequestGrant, ...] | None = None,
    timeout: float = 3.0,
    cancel: threading.Event | None = None,
) -> FetchContext:
    effective = (_grant(source, model),) if grants is None else grants
    return FetchContext(
        timeout=timeout,
        _thread_cancel_event=cancel,
        request_budget=RequestBudget(task_attempts=3, source_attempts=3, grants=effective),
        operation=operation,
    )


def _raw_row(source: str) -> dict[str, object]:
    if source == "fred":
        return {
            "date": "2024-01-01",
            "value": "3.25",
            "realtime_start": "2024-01-01",
            "realtime_end": "9999-12-31",
        }
    if source == "bls":
        return {
            "series_id": "LNS14000000",
            "year": "2025",
            "period": "M13",
            "period_name": "Annual average",
            "value": "4.1",
            "footnotes": [{"code": "P", "text": "Preliminary."}, {"code": None, "text": None}],
            "latest": None,
            "api_version": "v2",
        }
    return {
        "symbol": "BRK.B",
        "date": "2024-01-02",
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 2**53 + 1,
        "change": 0.5,
        "changePercent": 0.5,
        "vwap": 100.25,
        "currency": None,
        "volume_unit": None,
    }


def _query(source: str) -> dict[str, object]:
    if source == "fred":
        return {"series_id": "GDP"}
    if source == "bls":
        return {
            "series_ids": ["LNS14000000"],
            "start_year": 2025,
            "end_year": 2025,
            "max_requests": 2,
        }
    return {"symbol": "BRK.B", "start_date": "2024-01-01", "end_date": "2024-01-03"}


def _identity(source: str) -> tuple[str, str]:
    return {
        "fred": ("fred", "FredSeries"),
        "bls": ("bls", "BlsSeries"),
        "fmp": ("fmp", "EquityHistorical"),
    }[source]


def _client(
    registry: ProviderRegistry,
    ctx: FetchContext | None,
    *,
    allowed_domains: tuple[str, ...] | None = None,
) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/providers")
    principal = SimpleNamespace(
        allows_domain=lambda domain: allowed_domains is None or domain in allowed_domains
    )
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_provider_model_ingest_registry] = lambda: registry
    app.dependency_overrides[get_provider_model_ingest_context] = lambda: ctx
    return TestClient(app)


def _patch_fetcher(
    monkeypatch: pytest.MonkeyPatch,
    registry: ProviderRegistry,
    source: str,
    *,
    transform_mutation: bool = False,
    extract_failure: Exception | None = None,
) -> tuple[Any, list[dict[str, object]]]:
    source, model = _identity(source)
    fetcher = registry.resolve_model(source, model)
    calls: list[dict[str, object]] = []
    source_row = _raw_row(source)

    def extract_data(params: object, ctx: FetchContext) -> list[dict[str, object]]:
        calls.append({"params": params, "ctx": ctx, "thread": threading.current_thread().name})
        if extract_failure is not None:
            raise extract_failure
        return [dict(source_row)]

    monkeypatch.setattr(fetcher, "extract_data", extract_data)
    if transform_mutation:
        original_transform = fetcher.transform_data

        def transform_data(raw: list[dict[str, object]], params: object) -> object:
            raw[0]["value"] = "77.0"
            params.series_id = "MUTATED"
            return original_transform(raw, params)

        monkeypatch.setattr(fetcher, "transform_data", transform_data)
    return fetcher, calls


def _capture_writer(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: int | None = None,
    failure: Exception | None = None,
) -> tuple[list[dict[str, object]], SimpleNamespace]:
    writes: list[dict[str, object]] = []
    factory_calls: list[str] = []
    fake_engine = SimpleNamespace(token="offline-recording-engine", factory_calls=factory_calls)

    def write_provider_model_batch(**kwargs: object) -> int:
        writes.append(
            {
                **kwargs,
                "thread": threading.current_thread().name,
                "writer_started_at": datetime.now(timezone.utc),
            }
        )
        if failure is not None:
            raise failure
        rows = kwargs["rows"]
        return len(rows) if result is None else result  # type: ignore[arg-type]

    monkeypatch.setattr(
        provider_model_store,
        "write_provider_model_batch",
        write_provider_model_batch,
    )

    def engine_factory() -> SimpleNamespace:
        factory_calls.append(threading.current_thread().name)
        return fake_engine

    monkeypatch.setattr(ingest_api, "ingest_engine_factory", engine_factory)
    return writes, fake_engine


@pytest.mark.parametrize("source", ("fred", "bls", "fmp"))
def test_ingest_route_captures_once_and_calls_writer_in_bounded_worker(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    registry = _registry()
    fetcher, extraction_calls = _patch_fetcher(monkeypatch, registry, source)
    transform_calls: list[object] = []
    original_transform_query = fetcher.transform_query

    def counted_transform_query(**kwargs: object) -> object:
        transform_calls.append(kwargs)
        return original_transform_query(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", counted_transform_query)
    writes, fake_engine = _capture_writer(monkeypatch)
    source, model = _identity(source)
    ctx = _context(source, model)
    client = _client(registry, ctx)
    query = _query(source)
    before = datetime.now(timezone.utc)

    response = client.post(
        f"/providers/{source}/models/{model}/ingest",
        json={"query": query},
    )

    assert response.status_code == 200, response.text
    payload = response.json()["data"]
    assert payload["source"] == source
    assert payload["model"] == model
    assert payload["verified"] is False
    assert payload["raw_rows"] == 1
    assert payload["stored_rows"] == 1
    assert payload["raw_scope"] == "extract_data_output"
    assert payload["completeness"] == "NOT_ASSESSED"
    assert payload["transaction_scope"] == "ods_and_dwd_single_transaction"
    assert before <= datetime.fromisoformat(payload["observed_at"])
    assert len(extraction_calls) == len(writes) == 1
    assert len(transform_calls) == 1
    assert writes[0]["engine"] is fake_engine
    assert writes[0]["source"] == source and writes[0]["model"] == model
    assert writes[0]["batch_id"] == payload["batch_id"]
    assert writes[0]["observed_at"].isoformat() == payload["observed_at"]
    assert writes[0]["thread"].startswith("opendata-fetch")
    assert extraction_calls[0]["thread"].startswith("opendata-fetch")
    assert fake_engine.factory_calls and fake_engine.factory_calls[0].startswith("opendata-fetch")
    assert "query" not in payload and "capture" not in payload
    if source == "fred":
        assert extraction_calls[0]["params"].max_records == 10_000
        assert extraction_calls[0]["params"].page_size == 10_000
        assert writes[0]["rows"][0].requested_frequency is None
        assert writes[0]["rows"][0].realtime_start == date(2024, 1, 1)
    elif source == "fmp":
        assert extraction_calls[0]["params"].max_records == 10_000
        assert writes[0]["rows"][0].volume == 2**53 + 1
        assert writes[0]["rows"][0].currency is None
        assert writes[0]["rows"][0].volume_unit is None
        assert writes[0]["rows"][0].adj_close_provided is False
    else:
        assert extraction_calls[0]["params"].max_requests == 2
        assert writes[0]["rows"][0].period == "M13"
        assert writes[0]["rows"][0].latest is None
        assert writes[0]["rows"][0].preliminary is True
        assert writes[0]["rows"][0].footnotes[0].text == "Preliminary."


def test_capture_is_snapshotted_before_normalizer_mutates_raw_and_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _patch_fetcher(monkeypatch, registry, "fred", transform_mutation=True)
    writes, _ = _capture_writer(monkeypatch)
    result = asyncio.run(
        ingest_provider_model(
            registry=registry,
            source="fred",
            model="FredSeries",
            query={"series_id": "GDP"},
            principal=SimpleNamespace(allows_domain=lambda _domain: True),
            engine_factory=lambda: SimpleNamespace(),
            ctx=_context(),
        )
    )

    capture = writes[0]["capture"]
    raw_json = json.loads(capture.raw_json)
    query_json = json.loads(capture.query_json)
    assert raw_json[0]["value"] == "3.25"
    assert query_json["series_id"] == "GDP"
    assert result["stored_rows"] == 1
    assert writes[0]["rows"][0].series_id == "MUTATED"
    assert writes[0]["rows"][0].value == 77.0


@pytest.mark.parametrize(
    ("source", "model", "query", "expected_status"),
    (
        ("fred", "FredSeries", {"series_id": "GDP", "max_records": 10_001}, 400),
        ("fred", "FredSeries", {"series_id": "GDP", "max_records": True}, 400),
        ("bls", "BlsSeries", {"series_ids": ["x"], "start_year": 2024, "end_year": 2024}, 400),
        (
            "bls",
            "BlsSeries",
            {"series_ids": ["x", "x"], "start_year": 2024, "end_year": 2024, "max_requests": 2},
            400,
        ),
        (
            "bls",
            "BlsSeries",
            {"series_ids": ["x"], "start_year": 2025, "end_year": 2024, "max_requests": 2},
            400,
        ),
        ("fmp", "EquityHistorical", {"symbol": "BRK.B", "max_records": 10_001}, 400),
        ("fmp", "EquityHistorical", {"symbol": "BRK.B", "start_date": "2024-01-02"}, 400),
    ),
)
def test_invalid_query_and_record_ceiling_fail_before_extraction_or_engine(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    query: dict[str, object],
    expected_status: int,
) -> None:
    registry = _registry()
    _, extraction_calls = _patch_fetcher(monkeypatch, registry, source)
    writes, _ = _capture_writer(monkeypatch)
    client = _client(registry, _context(source, model))

    response = client.post(
        f"/providers/{source}/models/{model}/ingest",
        json={"query": query},
    )

    assert response.status_code == expected_status
    assert extraction_calls == []
    assert writes == []


@pytest.mark.parametrize(
    "ctx",
    (
        None,
        _context(grants=()),
        _context(grants=(_grant("fred", "FredSeries", operation=RequestOperation.QUERY),)),
        _context(grants=(_grant("bls", "BlsSeries"),)),
        _context(grants=(_grant("fred", "FredSeries", decision=GrantDecision.UNKNOWN),)),
        _context(grants=(_grant("fred", "FredSeries", conditions=("approval",)),)),
        _context(
            grants=(
                _grant(
                    "fred",
                    "FredSeries",
                    expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
                ),
            )
        ),
        _context(operation=RequestOperation.QUERY),
    ),
)
def test_missing_or_wrong_store_authority_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    ctx: FetchContext | None,
) -> None:
    registry = _registry()
    _, extraction_calls = _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)
    client = _client(registry, ctx)

    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )

    assert response.status_code in {400, 403}
    assert extraction_calls == []
    assert writes == []


def test_acl_denial_unsupported_pair_and_capability_verified_mismatch_have_no_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    fetcher, extraction_calls = _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)

    denied = _client(registry, _context(), allowed_domains=())
    response = denied.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 403

    unsupported = denied.post(
        "/providers/fmp/models/EquityQuote/ingest",
        json={"query": {"symbol": "BRK.B"}},
    )
    assert unsupported.status_code == 404

    monkeypatch.setattr(
        fetcher,
        "capability",
        fetcher.capability.model_copy(update={"verified": True}),
    )
    descriptor = provider_model_ingest.find_provider_model(registry, "fred", "FredSeries")
    from dataclasses import replace

    monkeypatch.setattr(
        provider_model_ingest,
        "find_provider_model",
        lambda *_args, **_kwargs: replace(descriptor, verified=False),
    )
    mismatch = _client(registry, _context())
    response = mismatch.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 503
    assert extraction_calls == []
    assert writes == []


@pytest.mark.parametrize(
    "content",
    (
        b'{"query":{"series_id":"GDP","series_id":"CPI"}}',
        b'{"query":{"series_id":"GDP"},"extra":true}',
        b'{"query":{"series_id":"GDP"},"other":NaN}',
        b'{"query":{"series_id":"GDP"},',
    ),
)
def test_malformed_json_is_fixed_400_and_never_reaches_source(
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    registry = _registry()
    _, extraction_calls = _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)
    client = _client(registry, _context())
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        content=content,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert extraction_calls == [] and writes == []


def test_upstream_and_writer_errors_do_not_reflect_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    secret = "FRED_API_KEY=must-not-appear"
    _patch_fetcher(monkeypatch, registry, "fred", extract_failure=RuntimeError(secret))
    writes, _ = _capture_writer(monkeypatch)
    client = _client(registry, _context())
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 503
    assert secret not in response.text
    assert writes == []

    _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch, failure=RuntimeError(secret))
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 503
    assert secret not in response.text
    assert len(writes) == 1


def test_writer_must_report_all_rows_as_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()
    _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch, result=0)
    client = _client(registry, _context())
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 503
    assert len(writes) == 1


def test_observed_at_is_captured_before_writer_pool_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)
    original_run_bounded = async_execution.run_bounded

    async def delayed_writer_admission(
        owner: object, operation: Any, deadline: float | None
    ) -> Any:
        await asyncio.sleep(0.06)
        return await original_run_bounded(owner, operation, deadline)

    monkeypatch.setattr(async_execution, "run_bounded", delayed_writer_admission)
    client = _client(registry, _context(timeout=2))
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        json={"query": {"series_id": "GDP"}},
    )
    assert response.status_code == 200
    observed_at = datetime.fromisoformat(response.json()["data"]["observed_at"])
    writer_at = writes[0]["writer_started_at"]
    assert (writer_at - observed_at).total_seconds() >= 0.05


def test_stalled_body_times_out_and_cancels_receive_without_engine_or_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _, extraction_calls = _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)
    receive_cancelled = threading.Event()

    async def receive() -> dict[str, object]:
        try:
            await asyncio.Event().wait()
            return {"type": "http.request", "body": b"", "more_body": False}
        finally:
            receive_cancelled.set()

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/providers/fred/models/FredSeries/ingest",
        "raw_path": b"/providers/fred/models/FredSeries/ingest",
        "query_string": b"",
        "headers": [],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
    }
    request = Request(scope, receive)
    ctx = _context(timeout=0.04)

    async def invoke() -> HTTPException:
        with pytest.raises(HTTPException) as error:
            await ingest_registered_provider_model(
                "fred",
                "FredSeries",
                request,
                SimpleNamespace(allows_domain=lambda _domain: True),
                registry,
                ctx,
            )
        return error.value

    error = asyncio.run(invoke())
    assert error.status_code == 504
    assert receive_cancelled.is_set()
    assert extraction_calls == [] and writes == []


def test_body_context_cancel_and_inherited_deadline_are_honored() -> None:
    from opendata.services.provider_model_ingest import await_ingest_request_content

    cancelled = threading.Event()

    async def blocking_read() -> str:
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            cancelled.set()

    context_cancel = threading.Event()
    context = _context(timeout=3, cancel=context_cancel)

    async def cancel_body() -> None:
        task = asyncio.create_task(
            await_ingest_request_content(
                blocking_read,
                ctx=context,
                admission_started=time.monotonic(),
            )
        )
        await asyncio.sleep(0.02)
        context_cancel.set()
        with pytest.raises(ProviderModelIngestCancelledError):
            await task

    asyncio.run(cancel_body())
    assert cancelled.is_set()

    inherited_cancelled = threading.Event()

    async def inherited_read() -> str:
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            inherited_cancelled.set()

    async def expire_under_parent_scope() -> None:
        budget = RequestBudget(
            task_attempts=3,
            source_attempts=3,
            grants=(_grant("fred", "FredSeries"),),
        )
        deadline = time.monotonic() + 0.03
        with (
            request_execution_scope(
                source="fred",
                canonical_model="FredSeries",
                operation=RequestOperation.STORE,
                budget=budget,
                deadline=deadline,
            ),
            pytest.raises(ProviderModelIngestTimeoutError),
        ):
            await await_ingest_request_content(
                inherited_read,
                ctx=_context(timeout=3),
                admission_started=time.monotonic(),
            )

    asyncio.run(expire_under_parent_scope())
    assert inherited_cancelled.is_set()


def test_body_caller_cancellation_propagates_and_drains_receive() -> None:
    from opendata.services.provider_model_ingest import await_ingest_request_content

    receive_cancelled = threading.Event()

    async def receive() -> str:
        try:
            await asyncio.Event().wait()
            return "unreachable"
        finally:
            receive_cancelled.set()

    async def run() -> None:
        task = asyncio.create_task(
            await_ingest_request_content(
                receive, ctx=_context(), admission_started=time.monotonic()
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert receive_cancelled.is_set()


def test_oversized_body_is_rejected_before_source_or_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _, extraction_calls = _patch_fetcher(monkeypatch, registry, "fred")
    writes, _ = _capture_writer(monkeypatch)
    client = _client(registry, _context())
    response = client.post(
        "/providers/fred/models/FredSeries/ingest",
        content=b" " * (1024 * 1024 + 1),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413
    assert extraction_calls == [] and writes == []


def test_worker_timeout_keeps_shared_pool_slot_until_writer_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    _patch_fetcher(monkeypatch, registry, "fred")
    started = threading.Event()
    release = threading.Event()
    context_seen = threading.Event()
    active_before = async_execution._active_workers

    def blocking_writer(**kwargs: object) -> int:
        ctx = kwargs["ctx"]
        cancellation = ctx._thread_cancel_event
        started.set()
        while not release.wait(0.005):
            if cancellation is not None and cancellation.is_set():
                context_seen.set()
        return len(kwargs["rows"])  # type: ignore[arg-type]

    monkeypatch.setattr(provider_model_store, "write_provider_model_batch", blocking_writer)

    async def run() -> None:
        task = asyncio.create_task(
            ingest_provider_model(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"series_id": "GDP"},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: SimpleNamespace(),
                ctx=_context(timeout=0.3),
            )
        )
        for _ in range(400):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        with pytest.raises(ProviderModelIngestTimeoutError):
            await task
        assert context_seen.wait(1)
        assert async_execution._active_workers == active_before + 1

    try:
        asyncio.run(run())
    finally:
        release.set()
    for _ in range(200):
        if async_execution._active_workers == active_before:
            break
        time.sleep(0.005)
    assert async_execution._active_workers == active_before


def test_external_store_cancellation_reaches_writer_and_returns_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    fetcher = registry.resolve_model("fred", "FredSeries")
    monkeypatch_rows = [{**_raw_row("fred")}]
    monkeypatch.setattr(
        fetcher,
        "extract_data",
        lambda _params, _ctx: [dict(monkeypatch_rows[0])],
    )
    started = threading.Event()
    release = threading.Event()
    cancel_seen = threading.Event()

    def writer(**kwargs: object) -> int:
        ctx = kwargs["ctx"]
        cancellation = ctx._thread_cancel_event
        started.set()
        while not release.wait(0.005):
            if cancellation is not None and cancellation.is_set():
                cancel_seen.set()
        return len(kwargs["rows"])  # type: ignore[arg-type]

    monkeypatch.setattr(provider_model_store, "write_provider_model_batch", writer)
    cancellation = threading.Event()
    context = _context(cancel=cancellation, timeout=2)

    async def run() -> None:
        task = asyncio.create_task(
            ingest_provider_model(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"series_id": "GDP"},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: SimpleNamespace(),
                ctx=context,
            )
        )
        for _ in range(400):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        cancellation.set()
        with pytest.raises(ProviderModelIngestCancelledError):
            await task
        assert cancel_seen.wait(1)

    try:
        asyncio.run(run())
    finally:
        release.set()


@pytest.mark.parametrize(
    ("query", "error_type"),
    (
        ({"series_id": "GDP", "source": "bls"}, ProviderModelIngestIdentityError),
        ({"series_id": "GDP", "ctx": {}}, ProviderModelIngestValidationError),
    ),
)
def test_conflicting_or_control_query_is_rejected_before_extract(
    query: dict[str, object],
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    fetcher = registry.resolve_model("fred", "FredSeries")
    calls: list[object] = []
    monkeypatch.setattr(
        fetcher,
        "extract_data",
        lambda params, ctx: calls.append(params) or [_raw_row("fred")],
    )

    with pytest.raises(error_type):
        asyncio.run(
            ingest_provider_model(
                registry=registry,
                source="fred",
                model="FredSeries",
                query=query,
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: SimpleNamespace(),
                ctx=_context(),
            )
        )
    assert calls == []


def test_explicit_store_grant_missing_is_rejected_before_registry_or_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine_calls: list[object] = []
    fetcher = registry.resolve_model("fred", "FredSeries")
    extraction_calls: list[object] = []
    monkeypatch.setattr(
        fetcher,
        "extract_data",
        lambda params, ctx: extraction_calls.append(params) or [_raw_row("fred")],
    )

    with pytest.raises(ProviderModelIngestAuthorizationError):
        asyncio.run(
            ingest_provider_model(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"series_id": "GDP"},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: engine_calls.append(object()) or SimpleNamespace(),
                ctx=None,
            )
        )
    assert engine_calls == []
    assert extraction_calls == []


def test_service_rejects_wrong_typed_result_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    fetcher = registry.resolve_model("fred", "FredSeries")
    monkeypatch.setattr(fetcher, "extract_data", lambda _params, _ctx: [_raw_row("fred")])
    monkeypatch.setattr(fetcher, "transform_data", lambda _raw, _params: (object(),))
    engine_calls: list[object] = []

    from opendata.services import provider_model_store

    def fail_if_called(**_kwargs: object) -> int:
        pytest.fail("writer called for malformed provider output")

    monkeypatch.setattr(provider_model_store, "write_provider_model_batch", fail_if_called)
    with pytest.raises(ProviderModelIngestUnavailableError):
        asyncio.run(
            ingest_provider_model(
                registry=registry,
                source="fred",
                model="FredSeries",
                query={"series_id": "GDP"},
                principal=SimpleNamespace(allows_domain=lambda _domain: True),
                engine_factory=lambda: engine_calls.append(object()) or SimpleNamespace(),
                ctx=_context(),
            )
        )
    assert engine_calls == []
