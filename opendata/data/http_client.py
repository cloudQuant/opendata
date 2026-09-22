"""Unified governed HTTP client (design §5.3, milestone A1.4).

New contract-layer code fetches through this module exclusively;
ported akshare direct calls stay as registered ratchet debt (FR-4
"请求层渐进收口").

Governance features (all per §5.3):

* session reuse via ``requests.Session`` with configurable
  UA/Referer/headers and optional proxies;
* separate connect/read timeouts (conservative defaults);
* retries with exponential backoff and jitter, **idempotent GET
  only**;
* per-host token-bucket rate limiting;
* circuit breaker per host: 429/403 trips mark the host and open the
  breaker after consecutive failures, failing fast during the cooldown;
* failure classification (network / timeout / rate_limited / blocked /
  upstream_error) with a diagnostic error string.

Time, sleep, jitter and the transport session are injectable so tests
run without real delays or network access.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class FailureCategory(str, Enum):
    """Failure classification of one governed request (§5.3)."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    UPSTREAM_ERROR = "upstream_error"


class HttpFetchError(RuntimeError):
    """Terminal failure of a governed request.

    Attributes:
        category: The failure classification.
        url: The requested URL.
        host: The URL's host.
        attempts: Transport attempts made (0 = failed before sending).
        status: HTTP status code, if a response was received.
        retryable: Whether the category is retryable in principle
            (the request may still have exhausted its attempts).
    """

    def __init__(
        self,
        category: FailureCategory,
        *,
        url: str,
        host: str,
        attempts: int,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        """Initialize the error and its diagnostic message.

        Args:
            category: The failure classification.
            url: The requested URL.
            host: The URL's host.
            attempts: Transport attempts made.
            status: HTTP status code, if any.
            retryable: Whether the category is retryable in principle.
        """
        shown_status = "-" if status is None else str(status)
        super().__init__(
            f"[{category.value}] host={host} status={shown_status} attempts={attempts} url={url}"
        )
        self.category = category
        self.url = url
        self.host = host
        self.attempts = attempts
        self.status = status
        self.retryable = retryable


class HttpClientConfig(BaseModel):
    """Governance configuration of the HTTP client (§5.3).

    Attributes:
        connect_timeout: Connect timeout in seconds.
        read_timeout: Read timeout in seconds.
        max_attempts: Total transport attempts for a GET request.
        backoff_base: First retry delay in seconds; doubles per retry.
        backoff_jitter: Upper bound of the random jitter added to
            each retry delay.
        rate_limit_per_host: Tokens per second per host; None
            disables rate limiting.
        rate_burst: Token bucket capacity (immediate burst size).
        rate_max_wait: Longest acceptable wait for a token; beyond it
            the request fails as rate limited.
        breaker_threshold: Consecutive 429/403 failures that open the
            host's circuit breaker.
        breaker_cooldown: Seconds the breaker stays open before
            allowing one probe attempt (half-open).
        default_headers: Merged under every request's headers.
        proxies: Optional requests-style proxy mapping.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    max_attempts: int = 3
    backoff_base: float = 0.5
    backoff_jitter: float = 0.25
    rate_limit_per_host: float | None = None
    rate_burst: int = 4
    rate_max_wait: float = 30.0
    breaker_threshold: int = 3
    breaker_cooldown: float = 60.0
    default_headers: dict[str, str] = {"User-Agent": "opendata/0.1"}
    proxies: dict[str, str] | None = None


class TokenBucket:
    """Per-host token bucket limiter (§5.3 限流).

    Tokens refill continuously at ``rate`` per second up to
    ``capacity``; each request attempt consumes one.
    """

    def __init__(self, rate: float, capacity: int, time_fn: Callable[[], float]) -> None:
        """Initialize a full bucket.

        Args:
            rate: Refill rate in tokens per second.
            capacity: Bucket capacity (burst size).
            time_fn: Monotonic clock used for refills (injectable).
        """
        self._rate = rate
        self._capacity = capacity
        self._time_fn = time_fn
        self._tokens = float(capacity)
        self._updated = time_fn()

    def try_acquire(self, now: float) -> bool:
        """Consume one token if available.

        Args:
            now: Current monotonic time.

        Returns:
            True if a token was consumed, False if the bucket is empty.
        """
        self._refill(now)
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    def wait_seconds(self, now: float) -> float:
        """Seconds until the next token becomes available.

        Args:
            now: Current monotonic time.

        Returns:
            Zero when a token is available now.
        """
        self._refill(now)
        if self._tokens >= 1.0:
            return 0.0
        return (1.0 - self._tokens) / self._rate

    def _refill(self, now: float) -> None:
        """Refill according to elapsed time (never beyond capacity)."""
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(float(self._capacity), self._tokens + elapsed * self._rate)
        self._updated = now


@dataclass
class _BreakerState:
    """Circuit breaker bookkeeping of one host (§5.3 熔断)."""

    consecutive: int = 0
    opened_at: float | None = None
    category: FailureCategory | None = None


def _classify_status(status: int) -> tuple[FailureCategory | None, bool]:
    """Classify an HTTP status into a failure category.

    Args:
        status: The HTTP status code.

    Returns:
        A ``(category, retryable)`` pair; ``(None, False)`` means the
        status is a success and needs no classification.
    """
    if status == 429:
        return FailureCategory.RATE_LIMITED, True
    if status == 403:
        return FailureCategory.BLOCKED, False
    if status >= 500:
        return FailureCategory.UPSTREAM_ERROR, True
    if status >= 400:
        return FailureCategory.UPSTREAM_ERROR, False
    return None, False


class GovernedHttpClient:
    """Governed HTTP client over a reusable ``requests.Session``.

    Only idempotent GET requests are retried (with exponential backoff
    and jitter); every other method gets exactly one attempt. Rate
    limiting and breaker state are tracked per host; the transport
    session, clock, sleep, and jitter functions are injectable.
    """

    def __init__(
        self,
        config: HttpClientConfig | None = None,
        *,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] | None = None,
        time_fn: Callable[[], float] | None = None,
        jitter_fn: Callable[[], float] | None = None,
    ) -> None:
        """Initialize the client with injectable seams.

        Args:
            config: Governance configuration; defaults apply when None.
            session: Transport session; a fresh ``requests.Session``
                is created when None.
            sleep: Sleep function for backoff and token waits.
            time_fn: Monotonic clock for buckets and breakers.
            jitter_fn: Random jitter source for retry backoff.
        """
        self._config = config if config is not None else HttpClientConfig()
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep if sleep is not None else time.sleep
        self._time = time_fn if time_fn is not None else time.monotonic
        self._jitter = jitter_fn if jitter_fn is not None else self._default_jitter
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, _BreakerState] = {}
        self._lock = threading.Lock()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        """Issue a governed, retried GET request.

        Args:
            url: Absolute URL.
            params: Optional query parameters.
            headers: Per-request headers merged over the defaults.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            HttpFetchError: On terminal, classified failure.
        """
        return self.request("GET", url, params=params, headers=headers)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> requests.Response:
        """Issue a governed request; retries apply to GET only.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            url: Absolute URL.
            params: Optional query parameters.
            headers: Per-request headers merged over the defaults.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            HttpFetchError: On terminal, classified failure.
        """
        host = self._require_host(url)
        is_get = method.upper() == "GET"
        attempts = self._config.max_attempts if is_get else 1
        attempt = 0
        while True:
            attempt += 1
            response, error = self._attempt_once(method, url, host, params, headers, attempt)
            if error is None:
                return response
            if not is_get or not error.retryable or attempt >= attempts:
                raise error
            logger.warning(f"{error} - retrying in backoff (attempt {attempt}/{attempts})")
            self._sleep_backoff(attempt)

    def _attempt_once(
        self,
        method: str,
        url: str,
        host: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        attempt: int,
    ) -> tuple[requests.Response | None, HttpFetchError | None]:
        """Run one governed transport attempt.

        Args:
            method: HTTP method.
            url: Absolute URL.
            host: Pre-parsed URL host.
            params: Optional query parameters.
            headers: Optional per-request headers.
            attempt: 1-based attempt number.

        Returns:
            ``(response, None)`` on success or ``(None, error)`` on
            classified failure.
        """
        self._acquire_token(host, url)
        self._guard_breaker(host, url)
        try:
            response = self._send(method, url, params, headers)
        except requests.Timeout as exc:
            error = self._error(FailureCategory.TIMEOUT, url, host, attempt, retryable=True)
            logger.warning(f"{error} - cause: {exc}")
            return None, error
        except requests.RequestException as exc:
            error = self._error(FailureCategory.NETWORK, url, host, attempt, retryable=True)
            logger.warning(f"{error} - cause: {exc}")
            return None, error
        category, retryable = _classify_status(response.status_code)
        if category is None:
            self._record_success(host)
            return response, None
        if category in (FailureCategory.RATE_LIMITED, FailureCategory.BLOCKED):
            self._record_limit_event(host, category)
        return None, self._error(
            category,
            url,
            host,
            attempt,
            status=response.status_code,
            retryable=retryable,
        )

    def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> requests.Response:
        """Send one request through the session with governance kwargs."""
        merged = {**self._config.default_headers, **(headers or {})}
        return self._session.request(
            method,
            url,
            params=dict(params) if params else None,
            headers=merged,
            timeout=(self._config.connect_timeout, self._config.read_timeout),
            proxies=self._config.proxies,
        )

    def _require_host(self, url: str) -> str:
        """Parse the URL host, failing closed on host-less URLs.

        Raises:
            ValueError: If the URL has no host.
        """
        host = urlsplit(url).hostname
        if not host:
            raise ValueError(f"URL has no host: {url!r}")
        return host.lower()

    def _acquire_token(self, host: str, url: str) -> None:
        """Consume a rate-limit token, waiting up to the configured max.

        Raises:
            HttpFetchError: When the wait would exceed ``rate_max_wait``
                or the token was taken while waiting.
        """
        limit = self._config.rate_limit_per_host
        if limit is None:
            return
        with self._lock:
            bucket = self._buckets.setdefault(
                host, TokenBucket(limit, self._config.rate_burst, self._time)
            )
            if bucket.try_acquire(self._time()):
                return
            wait = bucket.wait_seconds(self._time())
        if wait > self._config.rate_max_wait:
            raise self._error(FailureCategory.RATE_LIMITED, url, host, attempts=0, retryable=False)
        if wait > 0.0:
            self._sleep(wait)
        with self._lock:
            if not bucket.try_acquire(self._time()):
                raise self._error(
                    FailureCategory.RATE_LIMITED, url, host, attempts=0, retryable=False
                )

    def _guard_breaker(self, host: str, url: str) -> None:
        """Fail fast while a host's breaker is open; half-open after cooldown.

        Raises:
            HttpFetchError: With the tripping category while open.
        """
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is None or breaker.opened_at is None:
                return
            if self._time() < breaker.opened_at + self._config.breaker_cooldown:
                category = breaker.category or FailureCategory.RATE_LIMITED
                raise self._error(category, url, host, attempts=0, retryable=False)
            # Cooldown elapsed: allow one probe attempt (half-open).
            breaker.opened_at = None
            breaker.consecutive = 0

    def _record_limit_event(self, host: str, category: FailureCategory) -> None:
        """Count a 429/403 failure and open the breaker at the threshold."""
        with self._lock:
            breaker = self._breakers.setdefault(host, _BreakerState())
            breaker.consecutive += 1
            breaker.category = category
            if breaker.consecutive >= self._config.breaker_threshold:
                breaker.opened_at = self._time()
                logger.warning(
                    f"circuit breaker OPEN for host={host} "
                    f"after {breaker.consecutive} consecutive {category.value} responses "
                    f"(cooldown {self._config.breaker_cooldown}s)"
                )

    def _record_success(self, host: str) -> None:
        """Reset the breaker bookkeeping after a successful response."""
        with self._lock:
            self._breakers.pop(host, None)

    def _sleep_backoff(self, attempt: int) -> None:
        """Sleep the exponential backoff with jitter for one attempt."""
        delay = self._config.backoff_base * (2 ** (attempt - 1)) + self._jitter()
        self._sleep(delay)

    def _default_jitter(self) -> float:
        """Random jitter within the configured bound (injectable seam)."""
        # Retry jitter, not cryptography: the standard PRNG is intended here.
        return random.uniform(0.0, self._config.backoff_jitter)  # noqa: S311  # nosec B311

    def _error(
        self,
        category: FailureCategory,
        url: str,
        host: str,
        attempts: int,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> HttpFetchError:
        """Build a classified error for this host."""
        return HttpFetchError(
            category,
            url=url,
            host=host,
            attempts=attempts,
            status=status,
            retryable=retryable,
        )
