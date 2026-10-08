"""Bounded execution support for synchronous fetcher implementations."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeVar

MAX_BOUNDED_WORKERS = 8
_POLL_INTERVAL_SECONDS = 0.01
_MAX_ASYNC_WAIT_SECONDS = 3600.0
_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=MAX_BOUNDED_WORKERS,
    thread_name_prefix="opendata-fetch",
)
_CAPACITY_LOCK = threading.Lock()
_INSTANCE_LOCKS_GUARD = threading.Lock()
_INSTANCE_LOCKS: weakref.WeakKeyDictionary[object, threading.Lock] = weakref.WeakKeyDictionary()
_active_workers = 0

T = TypeVar("T")
BoundedOperation = Callable[[threading.Event, float | None], T]


class AsyncExecutionTimeoutError(TimeoutError):
    """The async fetch deadline expired before the full fetch completed."""


class _ExecutionAbandonedError(RuntimeError):
    """A worker observed that its awaiting async call had already left."""


def _instance_lock(owner: object) -> threading.Lock:
    with _INSTANCE_LOCKS_GUARD:
        lock = _INSTANCE_LOCKS.get(owner)
        if lock is None:
            lock = threading.Lock()
            _INSTANCE_LOCKS[owner] = lock
        return lock


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _check_deadline(deadline: float | None) -> None:
    if deadline is not None and time.monotonic() >= deadline:
        raise AsyncExecutionTimeoutError("fetch deadline expired")


async def _acquire(lock: threading.Lock, deadline: float | None, label: str) -> None:
    while not lock.acquire(blocking=False):
        remaining = _remaining(deadline)
        if remaining == 0:
            raise AsyncExecutionTimeoutError(f"fetch deadline expired while waiting for {label}")
        await asyncio.sleep(
            _POLL_INTERVAL_SECONDS if remaining is None else min(_POLL_INTERVAL_SECONDS, remaining)
        )


async def acquire_instance(owner: object, deadline: float | None) -> threading.Lock:
    """Acquire the per-instance serial gate without blocking an event loop."""
    lock = _instance_lock(owner)
    await _acquire(lock, deadline, "fetcher instance")
    return lock


def acquire_instance_sync(owner: object) -> threading.Lock:
    """Acquire the same per-instance gate for a synchronous fetch call."""
    lock = _instance_lock(owner)
    lock.acquire()
    return lock


async def _acquire_capacity(deadline: float | None) -> None:
    global _active_workers
    while True:
        with _CAPACITY_LOCK:
            if _active_workers < MAX_BOUNDED_WORKERS:
                _active_workers += 1
                return
        remaining = _remaining(deadline)
        if remaining == 0:
            raise AsyncExecutionTimeoutError(
                "fetch deadline expired while waiting for execution capacity"
            )
        await asyncio.sleep(
            _POLL_INTERVAL_SECONDS if remaining is None else min(_POLL_INTERVAL_SECONDS, remaining)
        )


async def _wait_for_completion(
    future: asyncio.Future[Any] | asyncio.Task[Any],
    deadline: float | None,
) -> bool:
    while True:
        remaining = _remaining(deadline)
        if remaining == 0:
            return future.done()
        timeout = None if remaining is None else min(remaining, _MAX_ASYNC_WAIT_SECONDS)
        done, _ = await asyncio.wait((future,), timeout=timeout)
        if done:
            return True
        if deadline is not None and _remaining(deadline) == 0:
            return future.done()


def _release_capacity(instance_lock: threading.Lock) -> None:
    global _active_workers
    with _CAPACITY_LOCK:
        _active_workers -= 1
    instance_lock.release()


async def run_bounded(
    owner: object,
    operation: BoundedOperation[T],
    deadline: float | None,
) -> T:
    """Run a full synchronous fetch pipeline in the shared bounded pool.

    Both permits are released by the concurrent future's completion callback,
    so cancellation of the awaiting coroutine never frees capacity while its
    synchronous provider call is still running.
    """
    instance_lock = await acquire_instance(owner, deadline)
    try:
        await _acquire_capacity(deadline)
    except BaseException:
        instance_lock.release()
        raise

    cancellation = threading.Event()
    completion_lock = threading.Lock()
    completion_time: list[float | None] = [None]

    def invoke() -> T:
        if cancellation.is_set():
            raise _ExecutionAbandonedError("async fetch ended before worker execution")
        _check_deadline(deadline)
        return operation(cancellation, deadline)

    try:
        concurrent_future = _EXECUTOR.submit(invoke)
    except BaseException:
        _release_capacity(instance_lock)
        raise

    def release_when_finished(_future: concurrent.futures.Future[T]) -> None:
        with completion_lock:
            completion_time[0] = time.monotonic()
        _release_capacity(instance_lock)

    concurrent_future.add_done_callback(release_when_finished)
    async_future = asyncio.wrap_future(concurrent_future)

    try:
        if deadline is None:
            return await asyncio.shield(async_future)

        if not await _wait_for_completion(async_future, deadline):
            with completion_lock:
                finished_at = completion_time[0]
            if finished_at is None and concurrent_future.done():
                # The concurrent future can finish just before its callback is
                # dispatched. Let wrap_future deliver that already-finished result.
                result = await asyncio.shield(async_future)
                with completion_lock:
                    finished_at = completion_time[0]
                if finished_at is None:
                    # A done concurrent future can be observed before its
                    # completion callback runs. Use the observation time as
                    # a conservative fallback so this race cannot return a
                    # late result as a success.
                    finished_at = time.monotonic()
                if finished_at > deadline:
                    cancellation.set()
                    raise AsyncExecutionTimeoutError(
                        "fetch deadline expired during bounded execution"
                    )
                return result
            if finished_at is None or finished_at > deadline:
                cancellation.set()
                async_future.add_done_callback(_consume_async_future)
                raise AsyncExecutionTimeoutError("fetch deadline expired during bounded execution")

        result = await asyncio.shield(async_future)
        with completion_lock:
            finished_at = completion_time[0]
        if finished_at is None:
            # asyncio's wrapped future can become visible just before the
            # completion callback records its timestamp. Conservatively
            # check the deadline at observation instead of treating the
            # missing timestamp as an on-time completion.
            finished_at = time.monotonic()
        if finished_at > deadline:
            cancellation.set()
            raise AsyncExecutionTimeoutError("fetch deadline expired during bounded execution")
        return result
    except asyncio.CancelledError:
        cancellation.set()
        async_future.add_done_callback(_consume_async_future)
        raise


def _consume_async_task(task: asyncio.Future[Any]) -> None:
    """Observe a native extraction task that outlived its caller."""
    with suppress(BaseException):
        task.exception()


def _consume_async_future(future: asyncio.Future[Any]) -> None:
    """Observe a bounded worker result after its caller has left."""
    with suppress(BaseException):
        future.exception()


async def await_native_extraction(
    awaitable: Awaitable[T],
    deadline: float | None,
    *,
    on_abandon: Callable[[], None] | None = None,
    on_terminal: Callable[[], None] | None = None,
) -> T:
    """Await native extraction with a deadline and discard late completion."""
    task: asyncio.Future[T] = asyncio.ensure_future(awaitable)

    def abandon() -> None:
        task.cancel()
        task.add_done_callback(_consume_async_task)
        if not task.done():
            if on_abandon is not None:
                on_abandon()
            if on_terminal is not None:
                task.add_done_callback(lambda _task: on_terminal())

    try:
        if not await _wait_for_completion(task, deadline):
            abandon()
            raise AsyncExecutionTimeoutError("fetch deadline expired during async extraction")
        return task.result()
    except asyncio.CancelledError:
        abandon()
        raise


def check_worker_liveness(cancellation: threading.Event, deadline: float | None) -> None:
    """Stop a worker between stages after its caller leaves or deadline expires."""
    if cancellation.is_set():
        raise _ExecutionAbandonedError("async fetch ended before normalization")
    _check_deadline(deadline)
