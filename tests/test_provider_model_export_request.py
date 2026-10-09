"""Offline async ownership tests for complete native model exports."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from opendata.data import async_execution
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
from opendata.services.provider_model_export_request import (
    ProviderModelExportRequestCancelledError,
    ProviderModelExportRequestForbiddenError,
    ProviderModelExportRequestIdentityError,
    ProviderModelExportRequestLimitError,
    ProviderModelExportRequestNotFoundError,
    ProviderModelExportRequestTimeoutError,
    ProviderModelExportRequestValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

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


class Principal:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.domains: list[str] = []

    def allows_domain(self, domain: str) -> bool:
        self.domains.append(domain)
        return self.allowed


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    for source in _MODELS:
        register_provider(source, registry)
    return registry


def _grant(
    source: str,
    model: str,
    *,
    operation: RequestOperation = RequestOperation.EXPORT,
    expires_at: datetime | None = None,
    conditions: tuple[str, ...] = (),
) -> RequestGrant:
    return RequestGrant(
        source=source,
        canonical_model=model,
        operation=operation,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-export-request-test",
        task_attempts=0,
        source_attempts=0,
        allowed_hosts=(_HOSTS[source],),
        expires_at=expires_at or datetime.now(timezone.utc) + timedelta(hours=1),
        conditions=conditions,
    )


def _context(
    source: str = "fred",
    model: str = "FredSeries",
    *,
    grants: Sequence[RequestGrant] | None = None,
    timeout: float | None = 3.0,
    operation: RequestOperation = RequestOperation.EXPORT,
    cancellation: threading.Event | None = None,
) -> FetchContext:
    return FetchContext(
        timeout=timeout,
        _thread_cancel_event=cancellation,
        request_budget=RequestBudget(
            task_attempts=0,
            source_attempts=0,
            grants=tuple(grants) if grants is not None else (_grant(source, model),),
        ),
        operation=operation,
    )


def _artifact(
    source: str, model: str, payload: bytes = b'{"kind":"metadata"}\n'
) -> NativeModelExportArtifact:
    owner = tempfile.TemporaryDirectory(prefix="opendata-export-request-test-")
    directory = Path(owner.name)
    os.chmod(directory, 0o700)
    path = directory / "snapshot.ndjson"
    path.write_bytes(payload)
    os.chmod(path, 0o600)
    return NativeModelExportArtifact(
        schema_version=1,
        source=source,
        model=model,
        domain=_MODELS[source][1],
        verified=False,
        created_at=datetime.now(timezone.utc),
        consistency="single_repeatable_read_transaction",
        completeness="NOT_ASSESSED",
        row_count=0,
        byte_count=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        snapshot_complete=True,
        _temporary_directory=owner,
        _path=path,
    )


def _install_fake_export(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returned: Callable[..., NativeModelExportArtifact] | None = None,
    calls: list[dict[str, object]] | None = None,
) -> None:
    def fake_export(**kwargs: Any) -> NativeModelExportArtifact:
        if calls is not None:
            calls.append(dict(kwargs))
        if returned is not None:
            return returned(**kwargs)
        return _artifact(kwargs["source"], kwargs["model"])

    monkeypatch.setattr(provider_model_export, "export_provider_model_snapshot", fake_export)


def _wait_for_artifact_cleanup(artifacts: list[NativeModelExportArtifact], timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if artifacts and artifacts[0]._cleaned:
            return True
        time.sleep(0.01)
    return bool(artifacts and artifacts[0]._cleaned)


@pytest.mark.parametrize(
    ("source", "model", "query", "expected_filter", "expected_start"),
    [
        (
            "fred",
            "FredSeries",
            {
                "filters": {"date": "2024-02-01", "series_id": "CPIAUCSL"},
                "start": "2024-01-01",
                "end": "2024-12-31",
                "max_records": 7,
                "max_bytes": 8192,
            },
            {"date": date(2024, 2, 1), "series_id": "CPIAUCSL"},
            date(2024, 1, 1),
        ),
        (
            "bls",
            "BlsSeries",
            {"filters": {"series_id": "LNS14000000"}, "start": 2025, "end": 2025},
            {"series_id": "LNS14000000"},
            2025,
        ),
        (
            "fmp",
            "EquityHistorical",
            {
                "filters": {"symbol": "BRK.B"},
                "start": "2026-01-01",
                "end": "2026-12-31",
            },
            {"symbol": "BRK.B"},
            date(2026, 1, 1),
        ),
    ],
)
@pytest.mark.asyncio
async def test_request_runs_all_native_exports_in_a_bounded_worker_and_normalizes_query(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    model: str,
    query: dict[str, object],
    expected_filter: dict[str, object],
    expected_start: date | int,
) -> None:
    calls: list[dict[str, object]] = []
    main_thread = threading.current_thread()

    def return_artifact(**kwargs: Any) -> NativeModelExportArtifact:
        assert threading.current_thread() is not main_thread
        assert kwargs["engine_factory"]() == "offline-engine-token"
        return _artifact(source, model)

    _install_fake_export(monkeypatch, returned=return_artifact, calls=calls)
    registry = _registry()
    lease = await export_request.export_provider_model_request(
        engine_factory=lambda: "offline-engine-token",  # type: ignore[arg-type]
        registry=registry,
        source=source,
        model=model,
        query=query,
        principal=Principal(),
        ctx=_context(source, model),
    )
    try:
        assert lease.artifact.source == source
        assert len(calls) == 1
        assert calls[0]["filters"] == expected_filter
        assert calls[0]["start"] == expected_start
        assert calls[0]["max_records"] == query.get("max_records", 200_000)
    finally:
        lease.cleanup()


@pytest.mark.asyncio
async def test_default_zero_grant_query_grant_wrong_operation_and_acl_never_start_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_export(monkeypatch)
    registry = _registry()
    engine_calls: list[int] = []
    initial_spools = export_request._ACTIVE_EXPORT_SPOOLS

    async def invoke(ctx: FetchContext | None, principal: Principal | None = None) -> None:
        await export_request.export_provider_model_request(
            engine_factory=lambda: engine_calls.append(1),  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=principal or Principal(),
            ctx=ctx,
        )

    with pytest.raises(ProviderModelExportRequestForbiddenError):
        await invoke(None)
    query_only = _context(
        grants=(_grant("fred", "FredSeries", operation=RequestOperation.QUERY),),
    )
    with pytest.raises(ProviderModelExportRequestForbiddenError):
        await invoke(query_only)
    with pytest.raises(ProviderModelExportRequestValidationError):
        await invoke(_context(operation=RequestOperation.QUERY))
    with pytest.raises(ProviderModelExportRequestForbiddenError):
        await invoke(_context(), Principal(allowed=False))
    assert engine_calls == []
    assert initial_spools == export_request._ACTIVE_EXPORT_SPOOLS


@pytest.mark.asyncio
async def test_identity_missing_model_and_query_shape_fail_without_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_export(monkeypatch)
    registry = _registry()
    engine_calls: list[int] = []
    initial_spools = export_request._ACTIVE_EXPORT_SPOOLS

    async def invoke(source: str, model: str, query: dict[str, object]) -> None:
        await export_request.export_provider_model_request(
            engine_factory=lambda: engine_calls.append(1),  # type: ignore[arg-type]
            registry=registry,
            source=source,
            model=model,
            query=query,
            principal=Principal(),
            ctx=_context(),
        )

    with pytest.raises(ProviderModelExportRequestIdentityError):
        await invoke("auto", "FredSeries", {})
    with pytest.raises(ProviderModelExportRequestNotFoundError):
        await invoke("fred", "MissingModel", {})
    with pytest.raises(ProviderModelExportRequestNotFoundError):
        await invoke("fmp", "EquityQuote", {})
    with pytest.raises(ProviderModelExportRequestValidationError):
        await invoke("fred", "FredSeries", {"limit": 20})
    with pytest.raises(ProviderModelExportRequestValidationError):
        await invoke("fred", "FredSeries", {"filters": {"not_a_dimension": "x"}})
    with pytest.raises(ProviderModelExportRequestValidationError):
        await invoke("bls", "BlsSeries", {"start": True})
    with pytest.raises(ProviderModelExportRequestLimitError):
        await invoke("fmp", "EquityHistorical", {"max_bytes": 64 * 1024 * 1024 + 1})
    assert engine_calls == []
    assert initial_spools == export_request._ACTIVE_EXPORT_SPOOLS


@pytest.mark.asyncio
async def test_http_body_read_uses_original_deadline_and_cancels_stalled_receive() -> None:
    registry = _registry()
    read_started = asyncio.Event()
    read_cancelled = asyncio.Event()

    async def stalled_body() -> bytes:
        read_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            read_cancelled.set()
            raise
        return b"{}"

    with pytest.raises(ProviderModelExportRequestTimeoutError):
        await export_request.await_provider_model_export_request_content(
            stalled_body,
            registry=registry,
            source="fred",
            model="FredSeries",
            principal=Principal(),
            ctx=_context(timeout=0.05),
            admission_started=time.monotonic(),
        )
    assert read_started.is_set()
    assert read_cancelled.is_set()


@pytest.mark.asyncio
async def test_late_success_after_request_cancellation_is_cleaned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    artifacts: list[NativeModelExportArtifact] = []

    def blocked_export(**_kwargs: Any) -> NativeModelExportArtifact:
        entered.set()
        release.wait(3)
        result = _artifact("fred", "FredSeries")
        artifacts.append(result)
        finished.set()
        return result

    _install_fake_export(monkeypatch, returned=blocked_export)
    cancel = threading.Event()
    task = asyncio.create_task(
        export_request.export_provider_model_request(
            engine_factory=lambda: None,  # type: ignore[arg-type]
            registry=_registry(),
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=_context(cancellation=cancel),
        )
    )
    await asyncio.wait_for(asyncio.to_thread(entered.wait, 1), timeout=1.5)
    cancel.set()
    with pytest.raises(ProviderModelExportRequestCancelledError):
        await asyncio.wait_for(task, timeout=1)
    assert not finished.is_set()
    release.set()
    assert await asyncio.to_thread(finished.wait, 2)
    assert len(artifacts) == 1

    async def wait_for_cleanup() -> None:
        while not artifacts[0]._cleaned:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait_for_cleanup(), timeout=2)
    assert not artifacts[0]._path.exists()
    assert artifacts[0]._cleaned is True


@pytest.mark.asyncio
async def test_canceled_waiter_keeps_shared_worker_slot_until_worker_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(async_execution, "MAX_BOUNDED_WORKERS", 1)
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    lock = threading.Lock()
    count = 0

    def serialized_export(**kwargs: Any) -> NativeModelExportArtifact:
        nonlocal count
        with lock:
            count += 1
            position = count
        if position == 1:
            first_entered.set()
            release_first.wait(3)
        else:
            second_entered.set()
            release_second.wait(3)
        return _artifact(kwargs["source"], kwargs["model"])

    _install_fake_export(monkeypatch, returned=serialized_export)
    registry = _registry()
    first_cancel = threading.Event()
    first = asyncio.create_task(
        export_request.export_provider_model_request(
            engine_factory=lambda: None,  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=_context(cancellation=first_cancel),
        )
    )
    await asyncio.wait_for(asyncio.to_thread(first_entered.wait, 1), timeout=1.5)
    first_cancel.set()
    with pytest.raises(ProviderModelExportRequestCancelledError):
        await asyncio.wait_for(first, timeout=1)

    second = asyncio.create_task(
        export_request.export_provider_model_request(
            engine_factory=lambda: None,  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=_context(),
        )
    )
    await asyncio.sleep(0.05)
    assert count == 1
    assert not second_entered.is_set()
    release_first.set()
    await asyncio.wait_for(asyncio.to_thread(second_entered.wait, 1), timeout=1.5)
    release_second.set()
    lease = await asyncio.wait_for(second, timeout=2)
    lease.cleanup()


@pytest.mark.asyncio
async def test_independent_requests_can_use_distinct_bounded_owners_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(async_execution, "MAX_BOUNDED_WORKERS", 2)
    both_entered = threading.Barrier(2)

    def parallel_export(**kwargs: Any) -> NativeModelExportArtifact:
        both_entered.wait(timeout=2)
        return _artifact(kwargs["source"], kwargs["model"])

    _install_fake_export(monkeypatch, returned=parallel_export)
    registry = _registry()

    async def invoke() -> object:
        return await export_request.export_provider_model_request(
            engine_factory=lambda: None,  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=_context(),
        )

    leases = await asyncio.wait_for(asyncio.gather(invoke(), invoke()), timeout=3)
    for lease in leases:
        lease.cleanup()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_eight_live_spool_leases_block_ninth_until_response_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine_calls: list[int] = []

    def export(**kwargs: Any) -> NativeModelExportArtifact:
        kwargs["engine_factory"]()
        return _artifact(kwargs["source"], kwargs["model"])

    _install_fake_export(monkeypatch, returned=export)

    async def invoke(*, ctx: FetchContext | None = None) -> object:
        return await export_request.export_provider_model_request(
            engine_factory=lambda: engine_calls.append(1),  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=ctx or _context(),
        )

    leases: list[Any] = []
    try:
        leases.extend([await invoke() for _ in range(export_request._MAX_ACTIVE_EXPORT_SPOOLS)])

        ninth = asyncio.create_task(invoke())
        await asyncio.sleep(0.05)
        assert not ninth.done()
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS

        leases.pop(0).cleanup()
        ninth_lease = await asyncio.wait_for(ninth, timeout=1)
        leases.append(ninth_lease)
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS + 1
    finally:
        for lease in leases:
            lease.cleanup()


@pytest.mark.asyncio
async def test_spool_wait_timeout_and_cancellation_do_not_leak_permits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine_calls: list[int] = []

    def export(**kwargs: Any) -> NativeModelExportArtifact:
        kwargs["engine_factory"]()
        return _artifact(kwargs["source"], kwargs["model"])

    _install_fake_export(monkeypatch, returned=export)

    async def invoke(ctx: FetchContext | None = None) -> object:
        return await export_request.export_provider_model_request(
            engine_factory=lambda: engine_calls.append(1),  # type: ignore[arg-type]
            registry=registry,
            source="fred",
            model="FredSeries",
            query={},
            principal=Principal(),
            ctx=ctx or _context(),
        )

    leases: list[Any] = []
    try:
        leases.extend([await invoke() for _ in range(export_request._MAX_ACTIVE_EXPORT_SPOOLS)])

        with pytest.raises(ProviderModelExportRequestTimeoutError):
            await invoke(_context(timeout=0.05))
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS

        cancellation = threading.Event()
        waiting = asyncio.create_task(invoke(_context(cancellation=cancellation)))
        await asyncio.sleep(0.03)
        cancellation.set()
        with pytest.raises(ProviderModelExportRequestCancelledError):
            await asyncio.wait_for(waiting, timeout=1)
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS

        leases.pop().cleanup()
        released_lease = await asyncio.wait_for(invoke(), timeout=1)
        leases.append(released_lease)
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS + 1
    finally:
        for lease in leases:
            lease.cleanup()


@pytest.mark.asyncio
async def test_abandoned_running_worker_keeps_eighth_spool_permit_until_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _registry()
    engine_calls: list[int] = []
    late_entered = threading.Event()
    release_late = threading.Event()
    late_artifacts: list[NativeModelExportArtifact] = []
    calls_lock = threading.Lock()

    def export(**kwargs: Any) -> NativeModelExportArtifact:
        kwargs["engine_factory"]()
        with calls_lock:
            position = len(engine_calls)
        if position == export_request._MAX_ACTIVE_EXPORT_SPOOLS:
            late_entered.set()
            release_late.wait(3)
        artifact = _artifact(kwargs["source"], kwargs["model"])
        if position == export_request._MAX_ACTIVE_EXPORT_SPOOLS:
            late_artifacts.append(artifact)
        return artifact

    def engine_factory() -> None:
        with calls_lock:
            engine_calls.append(1)

    _install_fake_export(monkeypatch, returned=export)
    held_leases: list[Any] = []
    cancelled = threading.Event()
    try:
        for _ in range(export_request._MAX_ACTIVE_EXPORT_SPOOLS - 1):
            held_leases.append(  # noqa: PERF401 - each request is intentionally admitted in sequence
                await export_request.export_provider_model_request(
                    engine_factory=engine_factory,  # type: ignore[arg-type]
                    registry=registry,
                    source="fred",
                    model="FredSeries",
                    query={},
                    principal=Principal(),
                    ctx=_context(),
                )
            )

        abandoned = asyncio.create_task(
            export_request.export_provider_model_request(
                engine_factory=engine_factory,  # type: ignore[arg-type]
                registry=registry,
                source="fred",
                model="FredSeries",
                query={},
                principal=Principal(),
                ctx=_context(cancellation=cancelled),
            )
        )
        await asyncio.wait_for(asyncio.to_thread(late_entered.wait, 1), timeout=1.5)
        cancelled.set()
        with pytest.raises(ProviderModelExportRequestCancelledError):
            await asyncio.wait_for(abandoned, timeout=1)

        ninth = asyncio.create_task(
            export_request.export_provider_model_request(
                engine_factory=engine_factory,  # type: ignore[arg-type]
                registry=registry,
                source="fred",
                model="FredSeries",
                query={},
                principal=Principal(),
                ctx=_context(),
            )
        )
        await asyncio.sleep(0.05)
        assert not ninth.done()
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS

        release_late.set()
        assert await asyncio.to_thread(_wait_for_artifact_cleanup, late_artifacts, 2)
        held_leases.append(await asyncio.wait_for(ninth, timeout=2))
        assert len(engine_calls) == export_request._MAX_ACTIVE_EXPORT_SPOOLS + 1
    finally:
        release_late.set()
        for lease in held_leases:
            lease.cleanup()
