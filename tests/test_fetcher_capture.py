"""Fetch capture contract tests for the shared async Fetcher template."""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, cast

import pytest

import opendata.data.async_execution as async_execution_module
import opendata.data.protocol as protocol_module
import opendata.data.request_budget as request_budget_module
from opendata.data.async_execution import AsyncExecutionTimeoutError
from opendata.data.capability import Capability
from opendata.data.protocol import (
    CapturedFetch,
    FetchContext,
    Fetcher,
    FetchResult,
    NativeAsyncExtractionNotImplementedError,
    QueryParams,
)
from opendata.data.request_budget import (
    GrantDecision,
    RequestAuthorizationError,
    RequestBudget,
    RequestExecutionCancelledError,
    RequestExecutionDeadlineError,
    RequestGrant,
    RequestOperation,
    request_execution_scope,
)

SOURCE = "fetch_capture_test"
MODEL = "CaptureFixture"


class CaptureQuery(QueryParams):
    """Small mutable query used to verify snapshot order."""

    value: int = 1


def make_budget(*, expires_at: datetime | None = None) -> RequestBudget:
    """Return an explicit finite grant for this offline-only fixture."""
    expiry = expires_at or datetime.now(timezone.utc) + timedelta(minutes=5)
    grant = RequestGrant(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-evidence:fetch-capture-offline",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts={"capture.example.test"},
        expires_at=expiry,
    )
    return RequestBudget(task_attempts=2, source_attempts=2, grants=(grant,))


def make_context(
    *,
    timeout: float | None = None,
    deadline: float | None = None,
    cancellation: threading.Event | None = None,
    budget: RequestBudget | None = None,
) -> FetchContext:
    """Build a scoped context without involving a provider transport."""
    return FetchContext(
        timeout=timeout,
        _deadline_monotonic=deadline,
        _thread_cancel_event=cancellation,
        request_budget=make_budget() if budget is None else budget,
    )


class RecordingFetcher(Fetcher[CaptureQuery, list[int]]):
    """Bounded fixture whose normalizer mutates both pipeline inputs."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = MODEL
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fetch_capture_test",
        period="snapshot",
        market="test",
        source=SOURCE,
        verified=False,
    )

    def __init__(self) -> None:
        self.stages: list[str] = []
        self.extract_calls = 0
        self.normalize_calls = 0
        self.seen_deadlines: list[float | None] = []
        self.capture_entered = threading.Event()
        self.capture_release = threading.Event()
        self.capture_finished = threading.Event()

    def transform_query(self, **kwargs: object) -> CaptureQuery:
        self.stages.append("validate")
        return CaptureQuery.model_validate(kwargs)

    def extract_data(self, params: CaptureQuery, ctx: FetchContext) -> list[int]:
        self.stages.append("extract")
        self.extract_calls += 1
        self.seen_deadlines.append(ctx._deadline_monotonic)
        return [params.value]

    def transform_data(self, raw: list[int], params: CaptureQuery) -> FetchResult:
        self.stages.append("normalize")
        self.normalize_calls += 1
        raw.append(99)
        params.value += 100
        return cast("FetchResult", ((tuple(raw), params.value),))


class NativeRecordingFetcher(RecordingFetcher):
    """Native async sibling using the same validation/capture/normalize path."""

    async_mode = "native_async"

    async def extract_data_async(self, params: CaptureQuery, ctx: FetchContext) -> list[int]:
        self.stages.append("extract")
        self.extract_calls += 1
        self.seen_deadlines.append(ctx._deadline_monotonic)
        await asyncio.sleep(0)
        return [params.value]


class NativeWaitingFetcher(NativeRecordingFetcher):
    """Native extractor that can observe or defer cancellation in flight."""

    def __init__(self, *, stubborn: bool = False) -> None:
        super().__init__()
        self.stubborn = stubborn
        self.native_started = threading.Event()
        self.native_cancel_seen = threading.Event()
        self.release_native = asyncio.Event()

    async def extract_data_async(self, params: CaptureQuery, ctx: FetchContext) -> list[int]:
        self.stages.append("extract")
        self.extract_calls += 1
        self.seen_deadlines.append(ctx._deadline_monotonic)
        if self.extract_calls == 1:
            self.native_started.set()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                self.native_cancel_seen.set()
                if not self.stubborn:
                    raise
                await self.release_native.wait()
        return [params.value]


class NativeWithoutImplementation(RecordingFetcher):
    """A fail-closed declaration with no native extractor override."""

    async_mode = "native_async"


def _snapshot(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
    return tuple(raw), params.value


@pytest.mark.asyncio
async def test_bounded_capture_runs_once_between_extraction_and_mutating_normalizer() -> None:
    fetcher = RecordingFetcher()
    captured: list[object] = []

    def capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        fetcher.stages.append("capture")
        captured.append(None)
        return _snapshot(raw, params)

    result = await fetcher.fetch_captured_async(
        ctx=make_context(),
        capture=capture,
        value=7,
    )

    assert isinstance(result, CapturedFetch)
    assert result.capture == ((7,), 7)
    assert result.results == (((7, 99), 107),)
    assert fetcher.stages == ["validate", "extract", "capture", "normalize"]
    assert len(captured) == 1
    assert fetcher.extract_calls == fetcher.normalize_calls == 1
    with pytest.raises(FrozenInstanceError):
        result.capture = ((0,), 0)  # type: ignore[misc]


@pytest.mark.asyncio
async def test_native_capture_uses_one_native_extraction_and_shared_normalizer() -> None:
    fetcher = NativeRecordingFetcher()
    captured = await fetcher.fetch_captured_async(
        ctx=make_context(),
        capture=_snapshot,
        value=4,
    )

    assert captured.capture == ((4,), 4)
    assert captured.results == (((4, 99), 104),)
    assert fetcher.stages == ["validate", "extract", "normalize"]
    assert fetcher.extract_calls == fetcher.normalize_calls == 1


@pytest.mark.asyncio
async def test_raw_async_and_legacy_fetch_entries_keep_their_existing_shapes() -> None:
    raw_fetcher = RecordingFetcher()
    raw = await raw_fetcher.fetch_raw_async(ctx=make_context(), value=3)
    assert raw == [3]
    assert raw_fetcher.stages == ["validate", "extract"]
    assert raw_fetcher.normalize_calls == 0

    legacy_fetcher = RecordingFetcher()
    sync_result = legacy_fetcher.fetch(ctx=make_context(), value=3)
    async_result = await legacy_fetcher.fetch_async(ctx=make_context(), value=3)
    assert sync_result == async_result == (((3, 99), 103),)
    assert legacy_fetcher.extract_calls == legacy_fetcher.normalize_calls == 2


@pytest.mark.asyncio
async def test_missing_default_grant_rejects_before_extraction_or_capture() -> None:
    fetcher = RecordingFetcher()
    calls = 0

    def capture(raw: list[int], params: CaptureQuery) -> int:
        nonlocal calls
        calls += 1
        return params.value

    with pytest.raises(RequestAuthorizationError):
        await fetcher.fetch_captured_async(capture=capture, value=1)

    assert fetcher.stages == ["validate"]
    assert fetcher.extract_calls == fetcher.normalize_calls == calls == 0


@pytest.mark.asyncio
async def test_capture_callable_is_checked_before_query_validation() -> None:
    fetcher = RecordingFetcher()

    with pytest.raises(TypeError, match="capture must be callable"):
        await fetcher.fetch_captured_async(
            ctx=make_context(),
            capture=cast("Any", None),
            value=1,
        )

    assert fetcher.stages == []


@pytest.mark.asyncio
async def test_capture_failure_skips_normalization_and_preserves_exception() -> None:
    fetcher = RecordingFetcher()
    failure = RuntimeError("capture fixture failed")

    def fail_capture(_raw: list[int], _params: CaptureQuery) -> object:
        fetcher.stages.append("capture")
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await fetcher.fetch_captured_async(
            ctx=make_context(),
            capture=fail_capture,
            value=1,
        )

    assert caught.value is failure
    assert fetcher.stages == ["validate", "extract", "capture"]
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_bounded_caller_cancellation_during_capture_keeps_instance_gate() -> None:
    fetcher = RecordingFetcher()
    first_capture = True

    def blocking_capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        nonlocal first_capture
        fetcher.stages.append("capture")
        if first_capture:
            first_capture = False
            fetcher.capture_entered.set()
            try:
                if not fetcher.capture_release.wait(timeout=3):
                    raise TimeoutError("capture was not released by the test")
            finally:
                fetcher.capture_finished.set()
        return _snapshot(raw, params)

    first = asyncio.create_task(
        fetcher.fetch_captured_async(ctx=make_context(), capture=blocking_capture, value=1)
    )
    assert await asyncio.to_thread(fetcher.capture_entered.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(
        fetcher.fetch_captured_async(ctx=make_context(), capture=blocking_capture, value=2)
    )
    try:
        await asyncio.sleep(0.04)
        assert fetcher.extract_calls == 1
    finally:
        fetcher.capture_release.set()

    second_result = await second
    assert second_result.capture == ((2,), 2)
    assert fetcher.capture_finished.wait(timeout=1)
    assert fetcher.extract_calls == 2
    assert fetcher.normalize_calls == 1


@pytest.mark.asyncio
async def test_bounded_timeout_during_capture_keeps_instance_gate_until_worker_exit() -> None:
    fetcher = RecordingFetcher()

    def blocking_capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        fetcher.stages.append("capture")
        fetcher.capture_entered.set()
        try:
            if not fetcher.capture_release.wait(timeout=3):
                raise TimeoutError("capture was not released by the test")
        finally:
            fetcher.capture_finished.set()
        return _snapshot(raw, params)

    first = asyncio.create_task(
        fetcher.fetch_captured_async(
            ctx=make_context(timeout=0.04),
            capture=blocking_capture,
            value=1,
        )
    )
    assert await asyncio.to_thread(fetcher.capture_entered.wait, 1)
    with pytest.raises(AsyncExecutionTimeoutError):
        await first

    second = asyncio.create_task(
        fetcher.fetch_captured_async(ctx=make_context(), capture=_snapshot, value=2)
    )
    try:
        await asyncio.sleep(0.04)
        assert fetcher.extract_calls == 1
        assert fetcher.normalize_calls == 0
    finally:
        fetcher.capture_release.set()

    second_result = await second
    assert second_result.capture == ((2,), 2)
    assert fetcher.capture_finished.wait(timeout=1)
    assert fetcher.normalize_calls == 1


@pytest.mark.asyncio
async def test_fetch_context_cancel_during_capture_is_merged_and_keeps_instance_gate() -> None:
    fetcher = RecordingFetcher()
    cancellation = threading.Event()

    def blocking_capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        fetcher.stages.append("capture")
        fetcher.capture_entered.set()
        try:
            if not fetcher.capture_release.wait(timeout=3):
                raise TimeoutError("capture was not released by the test")
        finally:
            fetcher.capture_finished.set()
        return _snapshot(raw, params)

    first = asyncio.create_task(
        fetcher.fetch_captured_async(
            ctx=make_context(cancellation=cancellation),
            capture=blocking_capture,
            value=1,
        )
    )
    assert await asyncio.to_thread(fetcher.capture_entered.wait, 1)
    cancellation.set()
    with pytest.raises(RequestExecutionCancelledError):
        await first

    second = asyncio.create_task(
        fetcher.fetch_captured_async(ctx=make_context(), capture=_snapshot, value=2)
    )
    try:
        await asyncio.sleep(0.04)
        assert fetcher.extract_calls == 1
    finally:
        fetcher.capture_release.set()

    assert (await second).capture == ((2,), 2)
    assert fetcher.capture_finished.wait(timeout=1)
    assert fetcher.normalize_calls == 1


@pytest.mark.asyncio
async def test_native_caller_cancel_during_sync_capture_skips_normalization() -> None:
    fetcher = NativeRecordingFetcher()
    capture_entered = threading.Event()
    release_capture = threading.Event()
    loop = asyncio.get_running_loop()

    def blocking_capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        capture_entered.set()
        if not release_capture.wait(timeout=3):
            raise TimeoutError("native capture was not released by the test")
        return _snapshot(raw, params)

    task = asyncio.create_task(
        fetcher.fetch_captured_async(
            ctx=make_context(),
            capture=blocking_capture,
            value=5,
        )
    )

    def cancel_during_capture() -> None:
        if capture_entered.wait(timeout=2):
            loop.call_soon_threadsafe(task.cancel)
            release_capture.set()

    cancel_thread = threading.Thread(target=cancel_during_capture, daemon=True)
    cancel_thread.start()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_capture.set()
        cancel_thread.join(timeout=1)

    assert fetcher.normalize_calls == 0
    assert fetcher.extract_calls == 1


@pytest.mark.asyncio
async def test_native_ctx_event_cancels_awaited_extraction() -> None:
    fetcher = NativeWaitingFetcher()
    cancellation = threading.Event()
    capture_calls = 0

    def count_capture(raw: list[int], params: CaptureQuery) -> object:
        nonlocal capture_calls
        capture_calls += 1
        return _snapshot(raw, params)

    task = asyncio.create_task(
        fetcher.fetch_captured_async(
            ctx=make_context(timeout=2, cancellation=cancellation),
            capture=count_capture,
            value=3,
        )
    )
    assert await asyncio.to_thread(fetcher.native_started.wait, 1)
    cancellation.set()

    with pytest.raises(RequestExecutionCancelledError):
        await asyncio.wait_for(task, timeout=0.5)
    assert fetcher.native_cancel_seen.is_set()
    assert fetcher.extract_calls == 1
    assert fetcher.normalize_calls == capture_calls == 0


@pytest.mark.asyncio
async def test_native_inherited_scope_event_cancels_awaited_extraction() -> None:
    fetcher = NativeWaitingFetcher()
    budget = make_budget()
    cancellation = threading.Event()

    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
        cancellation=cancellation,
    ):
        task = asyncio.create_task(
            fetcher.fetch_captured_async(
                ctx=FetchContext(timeout=2, request_budget=budget),
                capture=_snapshot,
                value=4,
            )
        )
        assert await asyncio.to_thread(fetcher.native_started.wait, 1)
        cancellation.set()
        with pytest.raises(RequestExecutionCancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    assert fetcher.native_cancel_seen.is_set()
    assert fetcher.extract_calls == 1
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_native_external_cancel_keeps_instance_gate_until_stubborn_extractor_exits() -> None:
    fetcher = NativeWaitingFetcher(stubborn=True)
    cancellation = threading.Event()
    captured: list[object] = []

    def count_capture(raw: list[int], params: CaptureQuery) -> tuple[tuple[int, ...], int]:
        captured.append(None)
        return _snapshot(raw, params)

    first = asyncio.create_task(
        fetcher.fetch_captured_async(
            ctx=make_context(timeout=2, cancellation=cancellation),
            capture=count_capture,
            value=1,
        )
    )
    assert await asyncio.to_thread(fetcher.native_started.wait, 1)
    cancellation.set()
    with pytest.raises(RequestExecutionCancelledError):
        await asyncio.wait_for(first, timeout=0.5)
    assert await asyncio.to_thread(fetcher.native_cancel_seen.wait, 1)

    second = asyncio.create_task(
        fetcher.fetch_captured_async(ctx=make_context(), capture=count_capture, value=2)
    )
    try:
        await asyncio.sleep(0.04)
        assert fetcher.extract_calls == 1
        assert fetcher.normalize_calls == 0
        assert captured == []
    finally:
        fetcher.release_native.set()

    result = await second
    assert result.capture == ((2,), 2)
    assert fetcher.extract_calls == 2
    assert fetcher.normalize_calls == 1
    assert len(captured) == 1


@pytest.mark.asyncio
async def test_native_capture_deadline_expiring_inside_callback_is_not_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ManualClock:
        current = 100.0

        def monotonic(self) -> float:
            return self.current

    clock = ManualClock()
    monkeypatch.setattr(protocol_module, "time", clock)
    monkeypatch.setattr(async_execution_module, "time", clock)
    monkeypatch.setattr(request_budget_module, "time", clock)
    fetcher = NativeRecordingFetcher()

    def expire_inside_capture(_raw: list[int], params: CaptureQuery) -> int:
        clock.current = 102.0
        return params.value

    with pytest.raises(AsyncExecutionTimeoutError):
        await fetcher.fetch_captured_async(
            ctx=make_context(timeout=1.0),
            capture=expire_inside_capture,
            value=2,
        )

    assert fetcher.extract_calls == 1
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_context_inherited_deadlines_are_combined_before_worker_entry() -> None:
    fetcher = RecordingFetcher()
    budget = make_budget()
    inherited_deadline = time.monotonic() + 2.0
    context_deadline = inherited_deadline + 1.0

    with request_execution_scope(
        source=SOURCE,
        canonical_model=MODEL,
        operation=RequestOperation.QUERY,
        budget=budget,
        deadline=inherited_deadline,
    ):
        result = await fetcher.fetch_captured_async(
            ctx=FetchContext(
                timeout=5.0,
                _deadline_monotonic=context_deadline,
                request_budget=budget,
            ),
            capture=_snapshot,
            value=6,
        )

    assert result.capture == ((6,), 6)
    assert fetcher.seen_deadlines == [inherited_deadline]


@pytest.mark.asyncio
async def test_invalid_deadline_and_pre_cancelled_context_fail_before_validation() -> None:
    fetcher = RecordingFetcher()
    with pytest.raises(RequestExecutionDeadlineError):
        await fetcher.fetch_captured_async(
            ctx=FetchContext(_deadline_monotonic=float("nan"), request_budget=make_budget()),
            capture=_snapshot,
            value=1,
        )
    assert fetcher.stages == []

    budget = make_budget()
    with (
        request_execution_scope(
            source=SOURCE,
            canonical_model=MODEL,
            operation=RequestOperation.QUERY,
            budget=budget,
            deadline=float("inf"),
        ),
        pytest.raises(RequestExecutionDeadlineError),
    ):
        await fetcher.fetch_captured_async(
            ctx=FetchContext(request_budget=budget),
            capture=_snapshot,
            value=1,
        )
    assert fetcher.stages == []

    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(RequestExecutionCancelledError):
        await fetcher.fetch_captured_async(
            ctx=make_context(cancellation=cancellation),
            capture=_snapshot,
            value=1,
        )
    assert fetcher.stages == []


@pytest.mark.asyncio
async def test_capture_revalidates_grant_after_callback_before_normalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = datetime.now(timezone.utc)
    expiry = initial + timedelta(seconds=1)
    budget = make_budget(expires_at=expiry)

    class ManualDateTime:
        current = initial

        @staticmethod
        def now(tz: object = None) -> datetime:
            del tz
            return ManualDateTime.current

    monkeypatch.setattr(request_budget_module, "datetime", ManualDateTime)
    fetcher = RecordingFetcher()

    def expire_grant(_raw: list[int], params: CaptureQuery) -> int:
        ManualDateTime.current = expiry + timedelta(seconds=1)
        return params.value

    with pytest.raises(RequestAuthorizationError, match="expired"):
        await fetcher.fetch_captured_async(
            ctx=FetchContext(request_budget=budget),
            capture=expire_grant,
            value=9,
        )

    assert fetcher.extract_calls == 1
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_native_mode_without_extractor_fails_before_capture_or_validation() -> None:
    fetcher = NativeWithoutImplementation()
    capture_calls = 0

    def capture(_raw: list[int], _params: CaptureQuery) -> int:
        nonlocal capture_calls
        capture_calls += 1
        return 1

    with pytest.raises(NativeAsyncExtractionNotImplementedError):
        await fetcher.fetch_captured_async(ctx=make_context(), capture=capture, value=1)

    assert fetcher.stages == []
    assert capture_calls == 0


def test_capture_return_type_is_publicly_generic() -> None:
    """Keep the documented generic carrier usable from ordinary annotations."""
    payload: CapturedFetch[int] = CapturedFetch(capture=7, results=cast("FetchResult", ()))
    assert payload.capture == 7
    assert payload.results == ()
