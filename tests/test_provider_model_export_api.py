"""Offline route and ASGI lifecycle tests for NDJSON model exports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
import threading
from contextlib import suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

from opendata.api import api_router
from opendata.api import provider_model_export as export_api
from opendata.api.dependencies import get_current_principal
from opendata.data.models import BlsFootnote, BlsObservation, EquityHistorical, SeriesObservation
from opendata.data.protocol import FetchContext
from opendata.data.providers.catalog import register_provider
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_export
from opendata.services import provider_model_export_request as export_request
from opendata.services.provider_model_export import NativeModelExportArtifact

if TYPE_CHECKING:
    from opendata.services.provider_model_export_request import NativeModelExportLease

_MODELS = {
    "fred": ("FredSeries", "fred_series"),
    "bls": ("BlsSeries", "bls_series"),
    "fmp": ("EquityHistorical", "equity_historical"),
}
_HOSTS = {
    "fred": "api.stlouisfed.org",
    "bls": "api.bls.gov",
    "fmp": "financialmodelingprep.com",
}


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for source in _MODELS:
        register_provider(source, registry)
    return registry


def _context(
    source: str,
    model: str,
    *,
    timeout: float | None = 3.0,
    operation: RequestOperation = RequestOperation.EXPORT,
    grants: tuple[RequestGrant, ...] | None = None,
) -> FetchContext:
    selected_grants = grants or (
        RequestGrant(
            source=source,
            canonical_model=model,
            operation=RequestOperation.EXPORT,
            decision=GrantDecision.ALLOWED,
            rights_evidence="offline-export-api-test",
            task_attempts=0,
            source_attempts=0,
            allowed_hosts=(_HOSTS[source],),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    )
    return FetchContext(
        timeout=timeout,
        operation=operation,
        request_budget=RequestBudget(task_attempts=0, source_attempts=0, grants=selected_grants),
    )


def _principal(allowed: bool = True) -> Any:
    return SimpleNamespace(allows_domain=lambda _domain: allowed)


def _client(
    registry: ProviderRegistry,
    *,
    ctx: FetchContext | None,
    allowed: bool = True,
) -> TestClient:
    app = FastAPI()
    # These are the same two aggregate prefixes mounted by opendata.main.
    app.include_router(api_router, prefix="/api/v1")
    app.include_router(api_router, prefix="/api")
    app.dependency_overrides[get_current_principal] = lambda: _principal(allowed)
    app.dependency_overrides[export_api.get_provider_model_export_registry] = lambda: registry
    app.dependency_overrides[export_api.get_provider_model_export_context] = lambda: ctx
    return TestClient(app)


def _row(source: str) -> SeriesObservation | BlsObservation | EquityHistorical:
    if source == "fred":
        return SeriesObservation(
            series_id="CPIAUCSL",
            date=date(2024, 2, 1),
            value=3.25,
            realtime_start=date(2024, 2, 2),
            realtime_end=date(9999, 12, 31),
            transform_units="lin",
            output_type=1,
            requested_frequency=None,
            requested_aggregation_method="avg",
        )
    if source == "bls":
        return BlsObservation(
            series_id="LNS14000000",
            year=2025,
            period="M13",
            period_name="Annual average",
            value=4.1,
            footnotes=(BlsFootnote(code="P", text="Preliminary."), BlsFootnote()),
            latest=None,
            preliminary=True,
            api_version="v2",
        )
    return EquityHistorical(
        symbol="BRK.B",
        date=date(2026, 1, 2),
        open=100.0,
        high=103.0,
        low=99.5,
        close=102.0,
        volume=2**256 + 17,
        change=-0.0,
        change_percent=0.25,
        vwap=101.0,
        currency=None,
        currency_semantics="source_unverified",
        volume_unit=None,
        volume_unit_semantics="source_unverified",
        query_window_scope="provider_default_unknown",
        window_boundary_semantics="source_unverified",
        provider_default_window_semantics="source_unverified",
        close_adjustment_semantics="split_adjusted_per_source_faq",
        adj_close_provided=False,
    )


def _artifact(
    source: str,
    model: str,
    row_data: dict[str, object] | None = None,
    *,
    raw: bytes | None = None,
) -> NativeModelExportArtifact:
    created_at = datetime.now(timezone.utc)
    if raw is None:
        lines = [
            {
                "kind": "metadata",
                "schema_version": 1,
                "source": source,
                "model": model,
                "domain": _MODELS[source][1],
                "verified": False,
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "consistency": "single_repeatable_read_transaction",
                "completeness": "NOT_ASSESSED",
            },
            {"kind": "row", "data": row_data or {}},
            {"kind": "summary", "rows": 1, "snapshot_complete": True},
        ]
        raw = b"".join(
            json.dumps(line, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
            + b"\n"
            for line in lines
        )
    owner = tempfile.TemporaryDirectory(prefix="opendata-export-api-test-")
    directory = Path(owner.name)
    os.chmod(directory, 0o700)
    path = directory / "snapshot.ndjson"
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return NativeModelExportArtifact(
        schema_version=1,
        source=source,
        model=model,
        domain=_MODELS[source][1],
        verified=False,
        created_at=created_at,
        consistency="single_repeatable_read_transaction",
        completeness="NOT_ASSESSED",
        row_count=0 if row_data is None else 1,
        byte_count=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
        snapshot_complete=True,
        _temporary_directory=owner,
        _path=path,
    )


def _query(source: str) -> dict[str, object]:
    if source == "fred":
        return {
            "filters": {"date": "2024-02-01", "series_id": "CPIAUCSL"},
            "start": "2024-01-01",
            "end": "2024-12-31",
            "max_records": 10,
            "max_bytes": 4096,
        }
    if source == "bls":
        return {"filters": {"series_id": "LNS14000000"}, "start": 2025, "end": 2025}
    return {
        "filters": {"symbol": "BRK.B"},
        "start": "2026-01-01",
        "end": "2026-12-31",
    }


@pytest.mark.parametrize("source", tuple(_MODELS))
def test_real_router_returns_full_typed_ndjson_and_safe_integrity_headers(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    model, _domain = _MODELS[source]
    registry = _registry()
    ctx = _context(source, model)
    loop_thread = threading.current_thread()
    seen: list[dict[str, object]] = []
    artifacts: list[NativeModelExportArtifact] = []

    def fake_export(**kwargs: Any) -> NativeModelExportArtifact:
        assert threading.current_thread() is not loop_thread
        kwargs["engine_factory"]()
        seen.append(dict(kwargs))
        row_data = _row(source).model_dump(mode="json", by_alias=False)
        artifact = _artifact(source, model, row_data)
        artifacts.append(artifact)
        return artifact

    engine_threads: list[threading.Thread] = []

    def engine_factory() -> object:
        engine_threads.append(threading.current_thread())
        return object()

    monkeypatch.setattr(provider_model_export, "export_provider_model_snapshot", fake_export)
    monkeypatch.setattr(export_api, "export_engine_factory", engine_factory)
    with _client(registry, ctx=ctx) as client:
        prefix = "/api/providers" if source == "fmp" else "/api/v1/providers"
        response = client.post(
            f"{prefix}/{source}/models/{model}/warehouse/export",
            json={"query": _query(source)},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["content-length"] == str(len(response.content))
    assert response.headers["x-content-sha256"] == hashlib.sha256(response.content).hexdigest()
    assert response.headers["x-row-count"] == "1"
    assert response.headers["x-ndjson-schema-version"] == "1"
    assert response.headers["x-snapshot-complete"] == "true"
    assert response.headers["content-disposition"] == (
        'attachment; filename="provider-model-snapshot.ndjson"'
    )
    assert "_path" not in response.headers
    assert len(seen) == 1
    assert len(engine_threads) == 1 and engine_threads[0] is not loop_thread
    if source == "fred":
        assert seen[0]["filters"] == {
            "date": date(2024, 2, 1),
            "series_id": "CPIAUCSL",
        }
    elif source == "bls":
        assert seen[0]["start"] == 2025
    else:
        assert seen[0]["filters"] == {"symbol": "BRK.B"}
    lines = [json.loads(line) for line in response.content.splitlines()]
    assert [line["kind"] for line in lines] == ["metadata", "row", "summary"]
    assert lines[1]["data"] == _row(source).model_dump(mode="json", by_alias=False)
    assert lines[-1] == {"kind": "summary", "rows": 1, "snapshot_complete": True}
    assert len(artifacts) == 1
    assert artifacts[0]._cleaned is True
    assert not artifacts[0]._path.exists()


@pytest.mark.parametrize(
    "body",
    [
        b'{"query":{"filters":{"symbol":"BRK.B","symbol":"OTHER"}}}',
        b'{"query":{"max_records":NaN}}',
        b'{"query":{},"limit":1}',
        b'{"query":{"offset":1}}',
    ],
)
def test_duplicate_nonfinite_and_forbidden_request_controls_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
) -> None:
    registry = _registry()
    model = "EquityHistorical"
    calls: list[int] = []
    monkeypatch.setattr(
        provider_model_export,
        "export_provider_model_snapshot",
        lambda **_kwargs: calls.append(1),
    )
    monkeypatch.setattr(export_api, "export_engine_factory", lambda: calls.append(2))
    with _client(registry, ctx=_context("fmp", model)) as client:
        response = client.post(
            "/api/v1/providers/fmp/models/EquityHistorical/warehouse/export",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert calls == []


def test_request_size_limit_is_enforced_before_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()
    calls: list[int] = []
    monkeypatch.setattr(
        provider_model_export,
        "export_provider_model_snapshot",
        lambda **_kwargs: calls.append(1),
    )
    with _client(registry, ctx=_context("fmp", "EquityHistorical")) as client:
        response = client.post(
            "/api/v1/providers/fmp/models/EquityHistorical/warehouse/export",
            content=b" " * (1024 * 1024 + 1),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert calls == []


@pytest.mark.parametrize(
    ("ctx", "allowed", "source", "model", "expected_status"),
    [
        (None, True, "fred", "FredSeries", 403),
        (
            _context("fred", "FredSeries", operation=RequestOperation.QUERY),
            True,
            "fred",
            "FredSeries",
            400,
        ),
        (_context("fred", "FredSeries"), False, "fred", "FredSeries", 403),
        (_context("fmp", "EquityHistorical"), True, "fmp", "EquityQuote", 404),
        (_context("fred", "FredSeries"), True, "auto", "FredSeries", 400),
    ],
)
def test_auth_identity_and_supported_profile_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    ctx: FetchContext | None,
    allowed: bool,
    source: str,
    model: str,
    expected_status: int,
) -> None:
    registry = _registry()
    calls: list[int] = []
    monkeypatch.setattr(
        provider_model_export,
        "export_provider_model_snapshot",
        lambda **_kwargs: calls.append(1),
    )
    with _client(registry, ctx=ctx, allowed=allowed) as client:
        response = client.post(
            f"/api/v1/providers/{source}/models/{model}/warehouse/export",
            json={"query": {}},
        )
    assert response.status_code == expected_status
    assert calls == []


def test_query_grant_and_over_cap_limits_return_fixed_errors_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    query_grant = RequestGrant(
        source="fred",
        canonical_model="FredSeries",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="query-only-offline-test",
        task_attempts=0,
        source_attempts=0,
        allowed_hosts=(_HOSTS["fred"],),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    calls: list[int] = []
    monkeypatch.setattr(
        provider_model_export,
        "export_provider_model_snapshot",
        lambda **_kwargs: calls.append(1),
    )
    with _client(
        registry,
        ctx=_context("fred", "FredSeries", grants=(query_grant,)),
    ) as client:
        no_export = client.post(
            "/api/v1/providers/fred/models/FredSeries/warehouse/export",
            json={"query": {}},
        )
    with _client(registry, ctx=_context("fred", "FredSeries")) as client:
        over_cap = client.post(
            "/api/v1/providers/fred/models/FredSeries/warehouse/export",
            json={"query": {"max_bytes": 64 * 1024 * 1024 + 1}},
        )
    assert no_export.status_code == 403
    assert over_cap.status_code == 413
    assert calls == []


def test_bad_last_row_never_returns_partial_200(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = _registry()

    def bad_terminal_row(**_kwargs: Any) -> NativeModelExportArtifact:
        # A failed final row never gives the route an artifact to stream.
        raise provider_model_export.ProviderModelExportError("private database detail")

    monkeypatch.setattr(provider_model_export, "export_provider_model_snapshot", bad_terminal_row)
    with _client(registry, ctx=_context("fred", "FredSeries")) as client:
        response = client.post(
            "/api/v1/providers/fred/models/FredSeries/warehouse/export",
            json={"query": {}},
        )
    assert response.status_code == 503
    assert response.headers.get("content-disposition") is None
    assert b"private database detail" not in response.content


async def _make_lease(
    monkeypatch: pytest.MonkeyPatch,
    *,
    timeout: float | None = 3.0,
    expires_at: datetime | None = None,
) -> tuple[NativeModelExportLease, NativeModelExportArtifact]:
    grant = RequestGrant(
        source="fred",
        canonical_model="FredSeries",
        operation=RequestOperation.EXPORT,
        decision=GrantDecision.ALLOWED,
        rights_evidence="response-lifecycle-test",
        task_attempts=0,
        source_attempts=0,
        allowed_hosts=(_HOSTS["fred"],),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
    )
    created: list[NativeModelExportArtifact] = []

    def fake_export(**kwargs: Any) -> NativeModelExportArtifact:
        kwargs["engine_factory"]()
        artifact = _artifact("fred", "FredSeries", raw=b'{"kind":"metadata"}\n')
        created.append(artifact)
        return artifact

    monkeypatch.setattr(provider_model_export, "export_provider_model_snapshot", fake_export)
    lease = await export_request.export_provider_model_request(
        engine_factory=lambda: None,  # type: ignore[arg-type]
        registry=_registry(),
        source="fred",
        model="FredSeries",
        query={},
        principal=_principal(),
        ctx=_context("fred", "FredSeries", timeout=timeout, grants=(grant,)),
    )
    assert len(created) == 1
    return lease, created[0]


def _asgi_scope(spec_version: str = "2.4") -> dict[str, object]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/export",
        "raw_path": b"/export",
        "query_string": b"",
        "headers": [],
        "client": ("testclient", 1234),
        "server": ("testserver", 80),
        "root_path": "",
    }


@pytest.mark.asyncio
async def test_asgi_send_error_and_disconnect_both_clean_private_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, artifact = await _make_lease(monkeypatch)
    response = export_api.NativeModelExportResponse(lease)
    event_loop_thread = threading.current_thread()
    read_threads: list[threading.Thread] = []
    original_read_chunk = response._read_chunk

    def record_threaded_read() -> bytes:
        read_threads.append(threading.current_thread())
        return original_read_chunk()

    response._read_chunk = record_threaded_read  # type: ignore[method-assign]

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def failing_send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.body":
            raise OSError("client socket closed")

    with pytest.raises(ClientDisconnect):
        await response(_asgi_scope("2.4"), receive, failing_send)
    assert artifact._cleaned is True
    assert not artifact._path.exists()
    assert response._file is None
    assert read_threads and all(thread is not event_loop_thread for thread in read_threads)

    lease2, artifact2 = await _make_lease(monkeypatch)
    response2 = export_api.NativeModelExportResponse(lease2)
    sent: list[str] = []

    async def disconnected_receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(str(message["type"]))

    await response2(_asgi_scope("2.3"), disconnected_receive, send)
    assert artifact2._cleaned is True
    assert not artifact2._path.exists()
    assert sent


@pytest.mark.asyncio
async def test_never_consumed_response_is_cleaned_when_original_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease, artifact = await _make_lease(monkeypatch, timeout=0.12)
    response = export_api.NativeModelExportResponse(lease)
    assert artifact._path.exists()
    await asyncio.sleep(0.25)
    assert artifact._cleaned is True
    assert not artifact._path.exists()
    assert response._file is None


@pytest.mark.parametrize("cancel_mode", ["task", "watcher"])
@pytest.mark.asyncio
async def test_response_cancel_defers_resources_until_anyio_reader_really_ends(
    monkeypatch: pytest.MonkeyPatch,
    cancel_mode: str,
) -> None:
    timeout = 0.14 if cancel_mode == "watcher" else 3.0
    lease, artifact = await _make_lease(monkeypatch, timeout=timeout)

    class SlowFile:
        def __init__(self) -> None:
            self.read_started = threading.Event()
            self.release_read = threading.Event()
            self.read_done = threading.Event()
            self.closed = False
            self.close_calls = 0

        def read(self, size: int) -> bytes:
            assert size == export_api._STREAM_CHUNK_BYTES
            self.read_started.set()
            try:
                if not self.release_read.wait(5):
                    raise TimeoutError("reader fuse expired")
                return b'{"kind":"metadata"}\n'
            finally:
                self.read_done.set()

        def close(self) -> None:
            self.close_calls += 1
            self.closed = True

    slow_file = SlowFile()
    cleanup_calls = 0
    original_cleanup = NativeModelExportArtifact.cleanup

    def counted_cleanup(self: NativeModelExportArtifact) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        original_cleanup(self)

    monkeypatch.setattr(NativeModelExportArtifact, "open_binary", lambda _self: slow_file)
    monkeypatch.setattr(NativeModelExportArtifact, "cleanup", counted_cleanup)
    response = export_api.NativeModelExportResponse(lease)
    heartbeat_stop = asyncio.Event()
    heartbeat_count = 0

    async def heartbeat() -> None:
        nonlocal heartbeat_count
        while not heartbeat_stop.is_set():
            heartbeat_count += 1
            await asyncio.sleep(0.005)

    heartbeat_task = asyncio.create_task(heartbeat())

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict[str, object]) -> None:
        return

    response_task = asyncio.create_task(response(_asgi_scope("2.4"), receive, send))
    fuse = threading.Timer(3.0, slow_file.release_read.set)
    fuse.daemon = True
    fuse.start()
    try:
        assert await asyncio.wait_for(
            asyncio.to_thread(slow_file.read_started.wait, 1),
            timeout=1.5,
        )
        heartbeat_before = heartbeat_count
        await asyncio.sleep(0.04)
        assert heartbeat_count > heartbeat_before

        if cancel_mode == "task":
            response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(response_task), timeout=1)

        # The ASGI task is gone, but the AnyIO worker still owns the open handle,
        # artifact, and permit until its real blocking read reaches finally.
        assert not slow_file.read_done.is_set()
        assert not slow_file.closed
        assert slow_file.close_calls == 0
        assert cleanup_calls == 0
        assert not artifact._cleaned
        assert not lease._permit._released
    finally:
        slow_file.release_read.set()
        fuse.cancel()
        heartbeat_stop.set()
        if not response_task.done():
            response_task.cancel()
        with suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(response_task), timeout=2)
        await asyncio.wait_for(heartbeat_task, timeout=1)

    assert await asyncio.to_thread(slow_file.read_done.wait, 2)
    deadline = asyncio.get_running_loop().time() + 2
    while (
        cleanup_calls == 0 or not artifact._cleaned
    ) and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert slow_file.closed
    assert slow_file.close_calls == 1
    assert cleanup_calls == 1
    assert artifact._cleaned
    assert lease._permit._released
