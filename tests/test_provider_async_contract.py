from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, cast

import pytest

import opendata.data.async_execution as async_execution_module
import opendata.data.protocol as protocol_module
from opendata.data.async_execution import MAX_BOUNDED_WORKERS, AsyncExecutionTimeoutError
from opendata.data.protocol import (
    FetchContext,
    Fetcher,
    NativeAsyncExtractionNotImplementedError,
    QueryParams,
    UnknownAsyncFetcherModeError,
    UnsupportedAsyncFetcherError,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.data.protocol import FetchResult


class _Params(QueryParams):
    value: int = 1


class _RecordingFetcher(Fetcher[_Params, int]):
    async_mode = "bounded_thread"

    def __init__(self) -> None:
        self.validate_calls = 0
        self.extract_calls = 0
        self.normalize_calls = 0
        self.seen_params: list[int] = []
        self.seen_contexts: list[FetchContext] = []

    def transform_query(self, **kwargs: object) -> _Params:
        self.validate_calls += 1
        params = _Params(**kwargs)
        self.seen_params.append(params.value)
        return params

    def extract_data(self, params: _Params, ctx: FetchContext) -> int:
        self.extract_calls += 1
        self.seen_contexts.append(ctx)
        return params.value * 10

    def transform_data(self, raw: int, params: _Params) -> FetchResult:
        self.normalize_calls += 1
        return cast("FetchResult", ((raw, params.value),))


class _NativeFetcher(_RecordingFetcher):
    async_mode = "native_async"

    def __init__(self) -> None:
        super().__init__()
        self.native_extract_calls = 0

    async def extract_data_async(self, params: _Params, ctx: FetchContext) -> int:
        self.native_extract_calls += 1
        self.seen_contexts.append(ctx)
        await asyncio.sleep(0)
        return params.value * 10


class _UnsupportedFetcher(_RecordingFetcher):
    async_mode = "unsupported"


class _UnknownModeFetcher(_RecordingFetcher):
    async_mode = "future_mode"


class _NativeWithoutImplementation(_RecordingFetcher):
    async_mode = "native_async"


class _TransportError(Exception):
    pass


class _ProviderError(Exception):
    pass


class _RaisingFetcher(_RecordingFetcher):
    def extract_data(self, params: _Params, ctx: FetchContext) -> int:
        self.extract_calls += 1
        try:
            raise _TransportError("transport detail")
        except _TransportError as cause:
            raise _ProviderError("provider classification") from cause


class _Activity:
    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.entered += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class _BlockingFetcher(_RecordingFetcher):
    def __init__(self, activity: _Activity) -> None:
        super().__init__()
        self.activity = activity
        self.extraction_finished = threading.Event()

    def extract_data(self, params: _Params, ctx: FetchContext) -> int:
        self.extract_calls += 1
        self.activity.enter()
        try:
            if not self.activity.release.wait(timeout=3):
                raise TimeoutError("test extraction was not released")
            return params.value * 10
        finally:
            self.activity.leave()
            self.extraction_finished.set()


async def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_sync_and_async_entry_share_query_raw_and_normalization_stages() -> None:
    fetcher = _RecordingFetcher()

    sync_result = fetcher.fetch(value=7)
    async_result = await fetcher.fetch_async(value=7)

    assert async_result == sync_result == ((70, 7),)
    assert fetcher.validate_calls == 2
    assert fetcher.extract_calls == 2
    assert fetcher.normalize_calls == 2
    assert fetcher.seen_params == [7, 7]


@pytest.mark.asyncio
async def test_native_async_uses_one_async_extractor_and_shared_other_stages() -> None:
    fetcher = _NativeFetcher()

    result = await fetcher.fetch_async(ctx=FetchContext(timeout=1.0), value=4)

    assert result == ((40, 4),)
    assert fetcher.validate_calls == 1
    assert fetcher.native_extract_calls == 1
    assert fetcher.extract_calls == 0
    assert fetcher.normalize_calls == 1
    assert fetcher.seen_contexts[0].timeout == 1.0
    assert fetcher.seen_contexts[0]._deadline_monotonic is not None


@pytest.mark.asyncio
async def test_modes_fail_closed_before_validation_or_io() -> None:
    unsupported = _UnsupportedFetcher()
    with pytest.raises(UnsupportedAsyncFetcherError):
        await unsupported.fetch_async(value=1)
    assert (unsupported.validate_calls, unsupported.extract_calls, unsupported.normalize_calls) == (
        0,
        0,
        0,
    )

    unknown = _UnknownModeFetcher()
    with pytest.raises(UnknownAsyncFetcherModeError):
        await unknown.fetch_async(value=1)
    assert (unknown.validate_calls, unknown.extract_calls, unknown.normalize_calls) == (0, 0, 0)

    native_without_impl = _NativeWithoutImplementation()
    with pytest.raises(NativeAsyncExtractionNotImplementedError):
        await native_without_impl.fetch_async(value=1)
    assert native_without_impl.validate_calls == 0


@pytest.mark.asyncio
async def test_async_preserves_provider_exception_type_and_cause() -> None:
    fetcher = _RaisingFetcher()

    with pytest.raises(_ProviderError) as caught:
        await fetcher.fetch_async(value=2)

    assert isinstance(caught.value.__cause__, _TransportError)
    assert fetcher.normalize_calls == 0


@pytest.mark.asyncio
async def test_blocking_sync_extraction_does_not_block_event_loop() -> None:
    activity = _Activity()
    fetcher = _BlockingFetcher(activity)
    heartbeat = asyncio.Event()
    task = asyncio.create_task(fetcher.fetch_async(value=3))
    beat = asyncio.create_task(asyncio.sleep(0.01, result=None))
    beat.add_done_callback(lambda _task: heartbeat.set())

    try:
        await asyncio.wait_for(heartbeat.wait(), timeout=1.0)
        assert activity.entered == 1
    finally:
        activity.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_sync_and_async_calls_share_the_per_instance_serial_gate() -> None:
    activity = _Activity()
    fetcher = _BlockingFetcher(activity)
    caller = ThreadPoolExecutor(max_workers=1)
    async_task = asyncio.create_task(fetcher.fetch_async(value=1))
    sync_future = None

    try:
        await _wait_until(lambda: activity.entered == 1)
        sync_future = caller.submit(fetcher.fetch, value=2)
        await asyncio.sleep(0.03)
        assert activity.entered == 1

        activity.release.set()
        assert await async_task == ((10, 1),)
        assert await asyncio.wrap_future(sync_future) == ((20, 2),)
    finally:
        activity.release.set()
        await asyncio.gather(async_task, return_exceptions=True)
        caller.shutdown(wait=True)

    assert activity.entered == 2
    assert activity.max_active == 1


@pytest.mark.asyncio
async def test_shared_pool_never_runs_more_than_its_capacity() -> None:
    activity = _Activity()
    fetchers = [_BlockingFetcher(activity) for _ in range(MAX_BOUNDED_WORKERS + 4)]
    tasks = [
        asyncio.create_task(fetcher.fetch_async(value=index))
        for index, fetcher in enumerate(fetchers)
    ]

    try:
        await _wait_until(lambda: activity.entered >= MAX_BOUNDED_WORKERS)
        await asyncio.sleep(0.04)
        assert activity.entered == MAX_BOUNDED_WORKERS
        assert activity.max_active <= MAX_BOUNDED_WORKERS
    finally:
        activity.release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert activity.entered == len(fetchers)
    assert activity.max_active <= MAX_BOUNDED_WORKERS


@pytest.mark.asyncio
async def test_waiting_cancellation_and_timeout_do_not_claim_pool_capacity() -> None:
    activity = _Activity()
    blockers = [_BlockingFetcher(activity) for _ in range(MAX_BOUNDED_WORKERS)]
    blocker_tasks = [asyncio.create_task(fetcher.fetch_async(value=1)) for fetcher in blockers]
    waiting = _RecordingFetcher()
    timing_out = _RecordingFetcher()
    replacement = _RecordingFetcher()
    replacement_task: asyncio.Task[FetchResult] | None = None

    try:
        await _wait_until(lambda: activity.entered == MAX_BOUNDED_WORKERS)
        cancelled_waiter = asyncio.create_task(waiting.fetch_async(value=2))
        await asyncio.sleep(0.03)
        assert waiting.validate_calls == 0
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        timed_waiter = asyncio.create_task(
            timing_out.fetch_async(ctx=FetchContext(timeout=0.05), value=3)
        )
        with pytest.raises(AsyncExecutionTimeoutError):
            await timed_waiter
        assert timing_out.validate_calls == 0

        replacement_task = asyncio.create_task(replacement.fetch_async(value=4))
        await asyncio.sleep(0.03)
        assert replacement.extract_calls == 0
        assert activity.active == MAX_BOUNDED_WORKERS
    finally:
        activity.release.set()
        await asyncio.gather(*blocker_tasks, return_exceptions=True)

    assert replacement_task is not None
    assert await replacement_task == ((40, 4),)
    assert activity.max_active <= MAX_BOUNDED_WORKERS


@pytest.mark.asyncio
async def test_running_cancellation_and_timeout_retain_capacity_until_worker_exit() -> None:
    for use_timeout in (False, True):
        activity = _Activity()
        abandoned = _BlockingFetcher(activity)
        blockers = [abandoned] + [
            _BlockingFetcher(activity) for _ in range(MAX_BOUNDED_WORKERS - 1)
        ]
        tasks = [
            asyncio.create_task(
                fetcher.fetch_async(
                    ctx=FetchContext(timeout=0.15) if use_timeout and index == 0 else None,
                    value=5 + index,
                )
            )
            for index, fetcher in enumerate(blockers)
        ]
        task = tasks[0]
        replacement = _RecordingFetcher()
        replacement_task: asyncio.Task[FetchResult] | None = None

        try:
            await _wait_until(lambda current=activity: current.entered == MAX_BOUNDED_WORKERS)
            if use_timeout:
                with pytest.raises(AsyncExecutionTimeoutError):
                    await task
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

            replacement_task = asyncio.create_task(replacement.fetch_async(value=6))
            await asyncio.sleep(0.03)
            assert replacement.extract_calls == 0
        finally:
            activity.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

        assert replacement_task is not None
        assert await replacement_task == ((60, 6),)
        await _wait_until(abandoned.extraction_finished.is_set)
        assert abandoned.normalize_calls == 0


@pytest.mark.asyncio
async def test_late_bounded_completion_cannot_return_success_or_leave_unobserved_error() -> None:
    activity = _Activity()
    fetcher = _BlockingFetcher(activity)
    loop = asyncio.get_running_loop()
    loop_errors: list[dict[str, object]] = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    timer = threading.Timer(0.05, activity.release.set)

    try:
        task = asyncio.create_task(fetcher.fetch_async(ctx=FetchContext(timeout=0.02), value=7))
        await _wait_until(lambda: fetcher.extract_calls == 1)
        timer.start()
        time.sleep(0.08)
        with pytest.raises(AsyncExecutionTimeoutError):
            await task
        await _wait_until(fetcher.extraction_finished.is_set)
        await asyncio.sleep(0.02)
    finally:
        activity.release.set()
        timer.join(timeout=1)
        loop.set_exception_handler(previous_handler)

    assert fetcher.normalize_calls == 0
    assert not any("exception was never retrieved" in str(item) for item in loop_errors)


class _NativeIgnoresCancellationFetcher(_NativeFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.cancel_seen = asyncio.Event()
        self.release_first = asyncio.Event()

    async def extract_data_async(self, params: _Params, ctx: FetchContext) -> int:
        self.native_extract_calls += 1
        if self.native_extract_calls == 1:
            self.first_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancel_seen.set()
                await self.release_first.wait()
            return params.value * 10
        return params.value * 10


class _NativeDeadlineFetcher(_NativeFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release_extraction = asyncio.Event()

    async def extract_data_async(self, params: _Params, ctx: FetchContext) -> int:
        self.native_extract_calls += 1
        self.started.set()
        await self.release_extraction.wait()
        return params.value * 10


@pytest.mark.asyncio
async def test_native_cancel_keeps_instance_serial_gate_until_extractor_finishes() -> None:
    fetcher = _NativeIgnoresCancellationFetcher()
    first = asyncio.create_task(fetcher.fetch_async(value=8))
    await fetcher.first_started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await fetcher.cancel_seen.wait()

    second = asyncio.create_task(fetcher.fetch_async(value=9))
    await asyncio.sleep(0.03)
    assert fetcher.validate_calls == 1
    assert fetcher.native_extract_calls == 1

    fetcher.release_first.set()
    assert await second == ((90, 9),)
    assert fetcher.validate_calls == 2
    assert fetcher.native_extract_calls == 2
    assert fetcher.normalize_calls == 1


@pytest.mark.asyncio
async def test_native_raw_completion_after_deadline_is_not_returned_as_success(monkeypatch) -> None:
    class ManualClock:
        current = 100.0

        def monotonic(self) -> float:
            return self.current

    clock = ManualClock()
    monkeypatch.setattr(protocol_module, "time", clock)
    monkeypatch.setattr(async_execution_module, "time", clock)
    fetcher = _NativeDeadlineFetcher()
    pending = asyncio.create_task(fetcher.fetch_raw_async(ctx=FetchContext(timeout=1.0), value=8))
    await fetcher.started.wait()

    # Release the controlled native extraction only after its monotonic
    # deadline has elapsed. asyncio.wait observes completion, exercising the
    # post-extraction deadline guard in Fetcher rather than timeout polling.
    clock.current = 102.0
    fetcher.release_extraction.set()
    with pytest.raises(AsyncExecutionTimeoutError, match="after async extraction"):
        await asyncio.wait_for(pending, timeout=1.0)

    assert fetcher.normalize_calls == 0
    assert fetcher.native_extract_calls == 1
    # A normally completed but late native task releases its instance gate.
    assert await fetcher.fetch_raw_async(value=9) == 90


class _SharedCounterFetcher(_RecordingFetcher):
    def __init__(self) -> None:
        super().__init__()
        self.counter_lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def extract_data(self, params: _Params, ctx: FetchContext) -> int:
        self.extract_calls += 1
        with self.counter_lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.005)
            return params.value * 10
        finally:
            with self.counter_lock:
                self.active -= 1


def test_shared_fetcher_repeats_across_event_loops_with_serial_instance_access() -> None:
    fetcher = _SharedCounterFetcher()

    def run_loop() -> list[FetchResult]:
        async def repeated_calls() -> list[FetchResult]:
            return [await fetcher.fetch_async(value=index) for index in range(4)]

        return asyncio.run(repeated_calls())

    with ThreadPoolExecutor(max_workers=2) as loops:
        outputs = list(loops.map(lambda _index: run_loop(), range(2)))

    assert len(outputs) == 2
    assert all(len(output) == 4 for output in outputs)
    assert fetcher.validate_calls == 8
    assert fetcher.extract_calls == 8
    assert fetcher.normalize_calls == 8
    assert fetcher.max_active == 1


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_fetch_context_rejects_non_positive_or_non_finite_timeouts(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        FetchContext(timeout=timeout)
