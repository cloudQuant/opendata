"""fuyao 限流器：令牌桶 + 限流熔断（A3.1）.

设计口径：限流与 HTTP 429 等效，触发后进入退避冷却；冷却期内 ``acquire`` 直接
抛错而不是静默等待，避免批量任务在冷却期堆积。冷却结束后按令牌桶速率恢复。
线程安全（批量任务可能在多线程中调用）。
"""

from __future__ import annotations

import math
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opendata.data.providers.ths.transport.errors import error_for_transport

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


_HOST_CONCURRENCY = 4
_HOST_CONCURRENCY_WAIT_SECONDS = 30.0
_shared_semaphores: dict[str, threading.BoundedSemaphore] = {}
_shared_semaphores_lock = threading.Lock()
_shared_limiters: dict[str, FuyaoRateLimiter] = {}
_shared_limiters_lock = threading.Lock()


def get_shared_concurrency_gate(host: str) -> threading.BoundedSemaphore:
    """Return the bounded request gate shared by clients for one host."""
    normalized = host.lower().strip()
    if not normalized:
        raise ValueError("host must not be empty")
    with _shared_semaphores_lock:
        return _shared_semaphores.setdefault(
            normalized, threading.BoundedSemaphore(_HOST_CONCURRENCY)
        )


class FuyaoRateLimiter:
    """令牌桶限流器，含限流熔断冷却.

    Attributes:
        rate_per_second: 令牌补充速率。
        burst: 桶容量（单次突发上限）。
    """

    def __init__(
        self,
        *,
        rate_per_second: float = 5.0,
        burst: int = 10,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """构造限流器.

        Args:
            rate_per_second: 令牌补充速率。
            burst: 桶容量。
            clock: 时钟实现（测试注入）。

        Raises:
            ValueError: 速率或桶容量非正。
        """
        if rate_per_second <= 0:
            raise ValueError("rate_per_second must be positive")
        if burst <= 0:
            raise ValueError("burst must be positive")
        self.rate_per_second = rate_per_second
        self.burst = burst
        self._clock = clock
        self._lock = threading.Lock()
        self._tokens = float(burst)
        self._updated_at = clock()
        self._cooldown_until = 0.0

    def acquire(self) -> None:
        """消费一个令牌；冷却期或令牌不足时抛错.

        Raises:
            FuyaoError: 处于限流冷却期（``rate_limited``，可重试）。
        """
        with self._lock:
            now = self._clock()
            if now < self._cooldown_until:
                remaining = self._cooldown_until - now
                raise error_for_transport("http", detail=f"rate_limited_wait_{remaining:.1f}s")
            self._refill(now)
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
        # 令牌不足：让调用方按退避重试，而不是阻塞事件循环。
        raise error_for_transport("http", detail="rate_limited_local_bucket")

    def wait_and_acquire(self, sleep: Callable[[float], None]) -> None:
        """先等待冷却结束（消费冷却）再取令牌.

        与 :meth:`acquire` 的区别：``acquire`` 面向批量调用方，冷却期直接失败
        以避免堆积；本方法面向"已决定等待"的调用方（HTTP 客户端的重试循环），
        按 ``Retry-After`` 等待后清除冷却，避免自身冷却挡住重试。

        Args:
            sleep: 等待实现（测试可注入，避免真实休眠）。
        """
        with self._lock:
            observed_deadline = self._cooldown_until
            remaining = max(0.0, observed_deadline - self._clock())
        if remaining > 0:
            sleep(remaining)
            with self._lock:
                # Only consume the deadline this caller observed. A concurrent
                # 429 may have extended the shared host cooldown while sleeping.
                now = self._clock()
                if self._cooldown_until <= observed_deadline and now >= observed_deadline:
                    self._cooldown_until = 0.0
        self.acquire()

    def record_rate_limit(self, *, retry_after_seconds: float | None = None) -> None:
        """记录一次上游限流，进入冷却.

        Args:
            retry_after_seconds: 上游给出的重试等待；缺省按桶速率推算一秒。
        """
        wait = retry_after_seconds if retry_after_seconds and retry_after_seconds > 0 else 1.0
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self._clock() + wait)

    @property
    def cooldown_remaining(self) -> float:
        """返回剩余冷却秒数（用于日志与告警）."""
        with self._lock:
            return max(0.0, self._cooldown_until - self._clock())

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        if elapsed:
            self._tokens = min(float(self.burst), self._tokens + elapsed * self.rate_per_second)
            self._updated_at = now


def get_shared_rate_limiter(host: str) -> FuyaoRateLimiter:
    """Return the process-wide token bucket and cooldown for one host."""
    normalized = host.lower().strip()
    if not normalized:
        raise ValueError("host must not be empty")
    with _shared_limiters_lock:
        return _shared_limiters.setdefault(normalized, FuyaoRateLimiter())


@contextmanager
def shared_host_request(
    host: str,
    *,
    wait_timeout_seconds: float = _HOST_CONCURRENCY_WAIT_SECONDS,
) -> Iterator[None]:
    """Hold one of the process-wide request slots for ``host``."""
    if not math.isfinite(wait_timeout_seconds) or wait_timeout_seconds <= 0:
        raise ValueError("wait_timeout_seconds must be finite and positive")
    gate = get_shared_concurrency_gate(host)
    if not gate.acquire(timeout=wait_timeout_seconds):
        raise error_for_transport("rate_limited", detail="host_concurrency_wait_timeout")
    try:
        yield
    finally:
        gate.release()


__all__ = [
    "FuyaoRateLimiter",
    "get_shared_concurrency_gate",
    "get_shared_rate_limiter",
    "shared_host_request",
]
