"""Shared per-consumer-key rolling-window rate limiting."""

from __future__ import annotations

from collections import deque
from math import ceil
from threading import Lock
from time import monotonic
from typing import TYPE_CHECKING, Protocol, cast

from loguru import logger

from opendata.core.config import settings

if TYPE_CHECKING:
    from collections.abc import Callable

WINDOW_SECONDS = 60
LOCAL_KEY_CAPACITY = 100_000
LOCAL_EVENT_CAPACITY = 1_000_000
LOCAL_PRUNE_INTERVAL_SECONDS = 1.0
REDIS_KEY_PREFIX = "opendata:consumer-key-rate-limit"


class APIKeyRateLimitUnavailableError(RuntimeError):
    """Raised when the configured rate-limit backend cannot enforce policy."""


class RateLimitBackend(Protocol):
    """Atomic interface implemented by local and shared rolling-window stores."""

    async def consume(
        self,
        api_key_id: int,
        limit: int,
        *,
        window_seconds: int,
    ) -> int | None:
        """Consume one request or return the required retry delay."""


class RedisEvalClient(Protocol):
    """Subset of the async Redis client used for the atomic Lua operation."""

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> object:
        """Run one Redis script atomically."""


class LocalSlidingWindowBackend:
    """Concurrency-safe, pruned sliding-window state for single-process mode."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = monotonic,
        max_keys: int = LOCAL_KEY_CAPACITY,
        max_events: int = LOCAL_EVENT_CAPACITY,
    ) -> None:
        """Initialize local request buckets.

        Args:
            clock: Monotonic clock used for event timestamps.
            max_keys: Maximum number of active consumer IDs retained.
            max_events: Maximum number of active request timestamps retained.
        """
        if max_keys <= 0 or max_events <= 0:
            raise ValueError("local limiter capacities must be positive")
        self._clock = clock
        self._max_keys = max_keys
        self._max_events = max_events
        self._windows: dict[int, deque[float]] = {}
        self._event_count = 0
        self._last_prune = float("-inf")
        self._lock = Lock()

    async def consume(
        self,
        api_key_id: int,
        limit: int,
        *,
        window_seconds: int,
    ) -> int | None:
        """Count one request under a rolling window.

        Args:
            api_key_id: Numeric API key row identity.
            limit: Maximum accepted calls in the window.
            window_seconds: Window length.

        Returns:
            ``None`` when accepted, otherwise positive seconds until retry.

        Raises:
            APIKeyRateLimitUnavailableError: If local state is at its configured cap.
        """
        now = self._clock()
        cutoff = now - window_seconds
        with self._lock:
            if now - self._last_prune >= LOCAL_PRUNE_INTERVAL_SECONDS or now < self._last_prune:
                self._prune(cutoff)
                self._last_prune = now

            events = self._windows.get(api_key_id)
            if events is not None:
                self._event_count -= self._expire(events, cutoff)
                if not events:
                    del self._windows[api_key_id]
                    events = None

            if events is not None and len(events) >= limit:
                return max(1, ceil(events[0] + window_seconds - now))

            if self._event_count >= self._max_events:
                raise APIKeyRateLimitUnavailableError
            if events is None:
                if len(self._windows) >= self._max_keys:
                    raise APIKeyRateLimitUnavailableError
                events = deque()
                self._windows[api_key_id] = events

            events.append(now)
            self._event_count += 1
        return None

    def reset(self) -> None:
        """Clear local buckets, primarily for isolated tests."""
        with self._lock:
            self._windows.clear()
            self._event_count = 0
            self._last_prune = float("-inf")

    def _prune(self, cutoff: float) -> None:
        """Drop expired events and empty consumer buckets while locked."""
        for api_key_id, events in tuple(self._windows.items()):
            self._event_count -= self._expire(events, cutoff)
            if not events:
                del self._windows[api_key_id]

    @staticmethod
    def _expire(events: deque[float], cutoff: float) -> int:
        """Remove expired timestamps and return the number removed."""
        expired = 0
        while events and events[0] <= cutoff:
            events.popleft()
            expired += 1
        return expired


REDIS_SLIDING_WINDOW_SCRIPT = """
local now = redis.call('TIME')
local now_ms = (tonumber(now[1]) * 1000) + math.floor(tonumber(now[2]) / 1000)
local window_ms = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local cutoff = now_ms - window_ms
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff)
local count = redis.call('ZCARD', KEYS[1])
if count >= limit then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_ms = tonumber(oldest[2]) + window_ms - now_ms
  if retry_ms < 1 then retry_ms = 1 end
  redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
  return {0, math.max(1, math.ceil(retry_ms / 1000))}
end
local sequence = redis.call('INCR', KEYS[2])
local member = tostring(now_ms) .. ':' .. tostring(sequence)
redis.call('ZADD', KEYS[1], now_ms, member)
redis.call('PEXPIRE', KEYS[1], window_ms + 1000)
redis.call('PEXPIRE', KEYS[2], window_ms + 1000)
return {1, 0}
"""


class RedisSlidingWindowBackend:
    """Atomic shared rolling-window backend backed by Redis sorted sets."""

    def __init__(self, client: RedisEvalClient) -> None:
        """Bind the async Redis client used by the backend."""
        self._client = client

    async def consume(
        self,
        api_key_id: int,
        limit: int,
        *,
        window_seconds: int,
    ) -> int | None:
        """Atomically check and record one API key request in Redis.

        Args:
            api_key_id: Numeric API key row identity.
            limit: Maximum accepted calls in the window.
            window_seconds: Window length.

        Returns:
            ``None`` when accepted, otherwise positive seconds until retry.
        """
        bucket_key = redis_bucket_key(api_key_id)
        sequence_key = f"{bucket_key}:sequence"
        result = await self._client.eval(
            REDIS_SLIDING_WINDOW_SCRIPT,
            2,
            bucket_key,
            sequence_key,
            window_seconds * 1000,
            limit,
        )
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise APIKeyRateLimitUnavailableError
        allowed, retry_after = int(result[0]), int(result[1])
        return None if allowed == 1 else max(1, retry_after)


def redis_bucket_key(api_key_id: int) -> str:
    """Return a Redis namespace containing only the numeric key record ID."""
    if api_key_id <= 0:
        raise ValueError("API key record ID must be positive")
    return f"{REDIS_KEY_PREFIX}:{api_key_id}"


def _redis_backend_from_url(redis_url: str) -> RateLimitBackend:
    """Create a lazy Redis backend without connecting until its first use."""
    import redis.asyncio as redis

    client = cast("RedisEvalClient", redis.from_url(redis_url, decode_responses=True))
    return RedisSlidingWindowBackend(client)


class APIKeyRateLimiter:
    """Select local or shared enforcement from the configured Redis URL."""

    def __init__(
        self,
        *,
        backend: RateLimitBackend | None = None,
        clock: Callable[[], float] = monotonic,
        max_local_keys: int = LOCAL_KEY_CAPACITY,
        redis_backend_factory: Callable[[str], RateLimitBackend] = _redis_backend_from_url,
    ) -> None:
        """Initialize the limiter and its local single-process backend.

        Args:
            backend: Explicit backend override for deterministic tests.
            clock: Monotonic clock for local mode and test injection.
            max_local_keys: Maximum retained consumer buckets in local mode.
            redis_backend_factory: Factory for the configured shared backend.
        """
        self._backend_override = backend
        self._local_backend = LocalSlidingWindowBackend(clock=clock, max_keys=max_local_keys)
        self._redis_backend_factory = redis_backend_factory
        self._redis_backends: dict[str, RateLimitBackend] = {}

    async def check(self, api_key_id: int, limit: int) -> int | None:
        """Consume a request or return the positive retry delay when limited.

        Args:
            api_key_id: Numeric API key row identity.
            limit: Requests allowed in the rolling minute.

        Returns:
            ``None`` when accepted, otherwise a positive Retry-After value.

        Raises:
            APIKeyRateLimitUnavailableError: If the selected backend cannot enforce
                the limit. Configured Redis failures never fall back to local state.
        """
        try:
            if api_key_id <= 0 or limit <= 0:
                raise ValueError("invalid consumer API key rate-limit parameters")
            backend = self._select_backend()
            return await backend.consume(
                api_key_id,
                limit,
                window_seconds=WINDOW_SECONDS,
            )
        except Exception:
            logger.error("consumer API key rate limiter unavailable (key_id={})", api_key_id)
            raise APIKeyRateLimitUnavailableError from None

    def reset_local_state(self) -> None:
        """Reset the local buckets for isolated test runs."""
        self._local_backend.reset()

    def _select_backend(self) -> RateLimitBackend:
        """Select the injected, local, or configured shared backend."""
        if self._backend_override is not None:
            return self._backend_override
        redis_url = settings.redis_url
        if not redis_url:
            return self._local_backend
        backend = self._redis_backends.get(redis_url)
        if backend is None:
            backend = self._redis_backend_factory(redis_url)
            self._redis_backends[redis_url] = backend
        return backend


api_key_rate_limiter = APIKeyRateLimiter()


__all__ = [
    "APIKeyRateLimitUnavailableError",
    "APIKeyRateLimiter",
    "LocalSlidingWindowBackend",
    "REDIS_KEY_PREFIX",
    "REDIS_SLIDING_WINDOW_SCRIPT",
    "RateLimitBackend",
    "RedisSlidingWindowBackend",
    "api_key_rate_limiter",
    "redis_bucket_key",
]
