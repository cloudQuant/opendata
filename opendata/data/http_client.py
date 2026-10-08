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

import json
import math
import random
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from opendata.data.raw_response_cache import (
    CachedRawResponse,
    RawResponseCache,
    get_configured_raw_response_cache,
)
from opendata.data.request_budget import (
    RequestAdapterRetryError,
    RequestAuthorizationError,
    RequestBudgetError,
    RequestExecutionScope,
    RequestRedirectError,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    reserve_scoped_attempt,
    scoped_host_slot,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_SENSITIVE_PARAMETER_PARTS = ("api_key", "apikey", "token", "secret", "password", "credential")


def _safe_endpoint(url: str) -> str:
    """Return a URL identity without user information, query, or fragment."""
    parts = urlsplit(url)
    hostname = parts.hostname or "unknown"
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = hostname if port is None else f"{hostname}:{port}"
    return f"{parts.scheme}://{netloc}{parts.path or '/'}"


def _parameter_summary(params: Mapping[str, str] | None) -> dict[str, tuple[str, ...]]:
    """Summarize names only, explicitly marking secret-like parameters."""
    keys = tuple(sorted(str(key) for key in (params or {})))
    redacted = tuple(
        key
        for key in keys
        if any(part in key.lower().replace("-", "_") for part in _SENSITIVE_PARAMETER_PARTS)
    )
    return {"keys": keys, "redacted_keys": redacted}


def _copy_json_value(value: object, ancestors: set[int]) -> object:
    """Copy one value from the ordinary JSON data model."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in ancestors:
            raise ValueError
        ancestors.add(identity)
        try:
            copied: dict[str, object] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError
                copied[key] = _copy_json_value(item, ancestors)
            return copied
        finally:
            ancestors.remove(identity)
    if isinstance(value, list):
        identity = id(value)
        if identity in ancestors:
            raise ValueError
        ancestors.add(identity)
        try:
            return [_copy_json_value(item, ancestors) for item in value]
        finally:
            ancestors.remove(identity)
    raise ValueError


def _snapshot_json_body(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate and deep-copy a JSON object without exposing rejected values."""
    if value is None:
        return None
    try:
        if not isinstance(value, Mapping):
            raise ValueError
        snapshot = _copy_json_value(value, set())
        if type(snapshot) is not dict:
            raise ValueError
        # Match the serializer used by requests and reject non-finite numbers.
        json.dumps(snapshot, allow_nan=False)
        return cast("dict[str, Any]", snapshot)
    except Exception:
        raise ValueError("json_body must be a finite JSON object") from None


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
            f"[{category.value}] host={host} status={shown_status} attempts={attempts} "
            f"url={_safe_endpoint(url)}"
        )
        self.category = category
        self.url = _safe_endpoint(url)
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
        max_concurrency_per_host: Maximum simultaneous requests to one host.
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

    connect_timeout: float = Field(default=10.0, gt=0)
    read_timeout: float = Field(default=30.0, gt=0)
    max_attempts: int = Field(default=3, gt=0)
    backoff_base: float = Field(default=0.5, ge=0)
    backoff_jitter: float = Field(default=0.25, ge=0)
    rate_limit_per_host: float | None = Field(default=5.0, gt=0)
    rate_burst: int = Field(default=10, gt=0)
    max_concurrency_per_host: int = Field(default=4, gt=0)
    rate_max_wait: float = Field(default=30.0, ge=0)
    breaker_threshold: int = Field(default=3, gt=0)
    breaker_cooldown: float = Field(default=60.0, ge=0)
    default_headers: dict[str, str] = {"User-Agent": "opendata/0.1"}
    proxies: dict[str, str] | None = None


@dataclass(frozen=True)
class _CacheRequestIdentity:
    """Prepared, credential-complete GET identity used only by the raw cache."""

    endpoint: str
    headers: Mapping[str, str | bytes]


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
    probe_in_flight: bool = False


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
        raw_response_cache: RawResponseCache | None = None,
    ) -> None:
        """Initialize the client with injectable seams.

        Args:
            config: Governance configuration; defaults apply when None.
            session: Transport session; a fresh ``requests.Session``
                is created when None.
            sleep: Sleep function for backoff and token waits.
            time_fn: Monotonic clock for buckets and breakers.
            jitter_fn: Random jitter source for retry backoff.
            raw_response_cache: Explicit cache seam; when omitted, the
                process cache is used only if its application setting is enabled.
        """
        self._config = config if config is not None else HttpClientConfig()
        self._session_override = session
        self._thread_local = threading.local()
        self._injected_session_lock = threading.Lock()
        self._sleep = sleep if sleep is not None else time.sleep
        self._time = time_fn if time_fn is not None else time.monotonic
        self._jitter = jitter_fn if jitter_fn is not None else self._default_jitter
        self._raw_response_cache_override = raw_response_cache
        self._buckets: dict[str, TokenBucket] = {}
        self._breakers: dict[str, _BreakerState] = {}
        self._semaphores: dict[str, threading.BoundedSemaphore] = {}
        self._lock = threading.Lock()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
        cache_context: Mapping[str, Any] | None = None,
    ) -> requests.Response:
        """Issue a governed, retried GET request.

        Args:
            url: Absolute URL.
            params: Optional query parameters.
            headers: Per-request headers merged over the defaults.
            timeout: Optional timeout override in seconds or as a pair.
            source: Stable source name included in structured events.
            cache_context: Optional data-interpretation dimensions used only
                to partition raw-response cache entries.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            HttpFetchError: On terminal, classified failure.
        """
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            source=source,
            cache_context=cache_context,
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
        cache_context: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
    ) -> requests.Response:
        """Issue a governed request; retries apply to GET only.

        Args:
            method: HTTP method, e.g. ``"GET"``.
            url: Absolute URL.
            params: Optional query parameters.
            headers: Per-request headers merged over the defaults.
            timeout: Optional timeout override in seconds or as a pair.
            source: Stable source name included in structured events.
            cache_context: Optional data-interpretation dimensions used only
                to partition raw-response cache entries.
            json_body: Optional JSON object sent with non-GET/HEAD requests.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            HttpFetchError: On terminal, classified failure.
        """
        request_body = _snapshot_json_body(json_body)
        method_upper = method.upper()
        if request_body is not None and method_upper in {"GET", "HEAD"}:
            raise ValueError("json_body is not allowed for GET or HEAD requests")
        host = self._require_host(url)
        scopes = current_request_scopes()
        scoped = bool(scopes)
        if scoped:
            self._validate_scoped_url(url, host)
            authorize_request(scopes, source, host)
            # Authorization precedes both cache access and lazy Session creation.
            self._validate_scoped_session(self._get_session())
        is_get = method_upper == "GET"
        attempts = self._config.max_attempts if is_get else 1
        request_id = uuid.uuid4().hex
        raw_cache = self._raw_response_cache_override
        if raw_cache is None and is_get:
            raw_cache = get_configured_raw_response_cache()
        cache_identity: _CacheRequestIdentity | None = None
        if raw_cache is not None and is_get:
            session = self._get_session()
            cache_identity = self._prepare_cache_identity(
                session, url, params=params, headers=headers
            )
            if cache_identity is not None:
                cached = raw_cache.get(
                    source=source,
                    endpoint=cache_identity.endpoint,
                    headers=cache_identity.headers,
                    context=cache_context,
                )
                if cached is not None:
                    started = self._time()
                    cached_response = self._restore_cached_response(url, cached, started)
                    self._log_request_event(
                        source,
                        url,
                        params,
                        request_id,
                        0,
                        started,
                        cached.status_code,
                        None,
                        cache_hit=True,
                    )
                    return cached_response
        attempt = 0
        while True:
            if scoped:
                check_scope_liveness(scopes)
            attempt += 1
            attempt_response, error = self._attempt_once(
                method,
                url,
                host,
                params,
                headers,
                timeout,
                request_body,
                attempt,
                source,
                request_id,
                raw_cache if is_get and cache_identity is not None else None,
                cache_context,
                cache_identity,
                scopes,
            )
            if error is None:
                if attempt_response is None:  # pragma: no cover - invariant guard
                    raise RuntimeError("successful HTTP attempt returned no response")
                return attempt_response
            if not is_get or not error.retryable or attempt >= attempts:
                raise error
            if scoped:
                check_scope_liveness(scopes)
            self._sleep_backoff(attempt)
            if scoped:
                check_scope_liveness(scopes)

    def _attempt_once(
        self,
        method: str,
        url: str,
        host: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        timeout: float | tuple[float, float] | None,
        json_body: Mapping[str, Any] | None,
        attempt: int,
        source: str,
        request_id: str,
        raw_cache: RawResponseCache | None,
        cache_context: Mapping[str, Any] | None,
        cache_identity: _CacheRequestIdentity | None,
        scopes: tuple[RequestExecutionScope, ...],
    ) -> tuple[requests.Response | None, HttpFetchError | None]:
        """Run one governed transport attempt.

        Args:
            method: HTTP method.
            url: Absolute URL.
            host: Pre-parsed URL host.
            params: Optional query parameters.
            headers: Optional per-request headers.
            timeout: Optional timeout override.
            json_body: Validated, copied JSON body for a non-GET/HEAD request.
            attempt: 1-based attempt number.
            source: Stable source name included in structured events.
            request_id: Identifier shared by attempts for this request.
            raw_cache: Cache enabled for this GET, if any.
            cache_context: Data-interpretation dimensions used for cache keys.
            cache_identity: Prepared URL and headers, including merged session
                credentials and query parameters.
            scopes: The active Fetcher execution scopes, if any.

        Returns:
            ``(response, None)`` on success or ``(None, error)`` on
            classified failure.
        """
        started = self._time()
        probe_in_flight = False
        try:
            if scopes:
                check_scope_liveness(scopes)
            self._acquire_token(host, url)
            if scopes:
                check_scope_liveness(scopes)
            probe_in_flight = self._guard_breaker(host, url)
            if scopes:
                with self._scoped_semaphore(host, scopes):
                    check_scope_liveness(scopes)
                    with scoped_host_slot(scopes, source, host):
                        check_scope_liveness(scopes)
                        response = self._send(
                            method,
                            url,
                            params,
                            headers,
                            timeout,
                            json_body,
                            scopes=scopes,
                            source=source,
                            host=host,
                        )
            else:
                with self._semaphore_for(host):
                    response = self._send(method, url, params, headers, timeout, json_body)
        except RequestBudgetError:
            if probe_in_flight:
                self._release_probe_slot(host)
            raise
        except HttpFetchError as preflight_error:
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                preflight_error.attempts,
                started,
                preflight_error.status,
                preflight_error.category.value,
            )
            return None, preflight_error
        except requests.Timeout:
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.TIMEOUT)
            timeout_error = self._error(
                FailureCategory.TIMEOUT,
                url,
                host,
                attempt,
                retryable=not probe_in_flight,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                None,
                timeout_error.category.value,
            )
            return None, timeout_error
        except requests.RequestException:
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.NETWORK)
            network_error = self._error(
                FailureCategory.NETWORK,
                url,
                host,
                attempt,
                retryable=not probe_in_flight,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                None,
                network_error.category.value,
            )
            return None, network_error
        if scopes and 300 <= response.status_code < 400:
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.BLOCKED)
            raise RequestRedirectError("redirects are forbidden in a scoped request")
        category, retryable = _classify_status(response.status_code)
        if category is None:
            if (
                raw_cache is not None
                and cache_identity is not None
                and 200 <= response.status_code < 300
            ):
                raw_cache.put(
                    method="GET",
                    source=source,
                    endpoint=cache_identity.endpoint,
                    headers=cache_identity.headers,
                    context=cache_context,
                    status_code=response.status_code,
                    content=response.content,
                    encoding=response.encoding,
                )
            self._record_success(host, probe_in_flight=probe_in_flight)
            self._log_request_event(
                source, url, params, request_id, attempt, started, response.status_code, None
            )
            return response, None
        if probe_in_flight:
            self._record_probe_failure(host, category)
        elif category in (FailureCategory.RATE_LIMITED, FailureCategory.BLOCKED):
            self._record_limit_event(host, category)
        status_error = self._error(
            category,
            url,
            host,
            attempt,
            status=response.status_code,
            retryable=retryable and not probe_in_flight,
        )
        self._log_request_event(
            source,
            url,
            params,
            request_id,
            attempt,
            started,
            response.status_code,
            category.value,
        )
        return None, status_error

    def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        timeout: float | tuple[float, float] | None,
        json_body: Mapping[str, Any] | None,
        *,
        scopes: tuple[RequestExecutionScope, ...] = (),
        source: str = "unspecified",
        host: str = "",
    ) -> requests.Response:
        """Send one request through the session with governance kwargs."""
        merged = {**self._config.default_headers, **(headers or {})}
        request_timeout = (
            timeout
            if timeout is not None
            else (
                self._config.connect_timeout,
                self._config.read_timeout,
            )
        )
        session = self._get_session()
        request_kwargs: dict[str, Any] = {
            "params": dict(params) if params else None,
            "headers": merged,
            "timeout": request_timeout,
            "proxies": self._config.proxies,
        }
        if scopes:
            request_kwargs["allow_redirects"] = False
            self._validate_scoped_session(session)
        if json_body is not None:
            request_kwargs["json"] = json_body

        def send() -> requests.Response:
            if scopes:
                check_scope_liveness(scopes)
                reserve_scoped_attempt(scopes, source, host)
            return session.request(method, url, **request_kwargs)

        if self._session_override is not None:
            with self._injected_session_lock:
                return send()
        return send()

    def _get_session(self) -> requests.Session:
        """Return the injected session or a per-thread reusable session."""
        if self._session_override is not None:
            return self._session_override
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return cast("requests.Session", session)

    def _validate_scoped_url(self, url: str, host: str) -> None:
        """Require the exact HTTPS origin shape accepted by a trusted grant."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise RequestAuthorizationError("scoped request URL is invalid") from exc
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.hostname.lower() != host
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
        ):
            raise RequestAuthorizationError("scoped requests require an exact HTTPS host")

    def _validate_scoped_session(self, session: object) -> None:
        """Reject opaque authentication, hooks, or adapter retries when scoped."""
        if not isinstance(session, requests.Session):
            return
        session_auth = session.auth
        basic_auth = requests.auth.HTTPBasicAuth
        basic_tuple = (
            type(session_auth) is tuple
            and len(session_auth) == 2
            and all(type(item) is str for item in session_auth)
        )
        pure_basic_auth = type(session_auth) is basic_auth and all(
            type(getattr(session_auth, attribute, None)) is str
            for attribute in ("username", "password")
        )
        if session_auth is not None and not (basic_tuple or pure_basic_auth):
            raise RequestAuthorizationError(
                "scoped requests reject unsupported session authentication"
            )
        if any(session.hooks.values()):
            raise RequestAuthorizationError("scoped requests reject session response hooks")
        for adapter in session.adapters.values():
            retries = getattr(adapter, "max_retries", None)
            if retries is None or getattr(retries, "total", None) != 0:
                raise RequestAdapterRetryError(
                    "scoped requests require adapters with retries disabled"
                )
            if any(
                getattr(retries, attribute, 0) not in (None, 0, False)
                for attribute in ("connect", "read", "redirect", "status", "other")
            ):
                raise RequestAdapterRetryError(
                    "scoped requests require adapters with retries disabled"
                )

    @contextmanager
    def _scoped_semaphore(
        self,
        host: str,
        scopes: tuple[RequestExecutionScope, ...],
    ) -> Iterator[None]:
        """Acquire this client's host gate while honoring scope cancellation."""
        semaphore = self._semaphore_for(host)
        while not semaphore.acquire(timeout=0.05):
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            yield
        finally:
            semaphore.release()

    def _prepare_cache_identity(
        self,
        session: requests.Session,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
    ) -> _CacheRequestIdentity | None:
        """Fingerprint the prepared request, or skip cache for opaque session state.

        requests merges session-level headers, cookies, auth and params only
        while preparing a request. Using that prepared shape prevents different
        credentials or query state from sharing a raw response. Custom auth,
        response hooks, external proxy routing, and injected sessions are
        deliberately excluded because their effective identity can change
        outside this client's control.
        """
        if self._session_override is not None or type(session) is not requests.Session:
            return None
        if any(session.hooks.values()) or session.proxies or self._config.proxies:
            return None
        if session.cert is not None or session.verify is not True:
            return None
        session_auth = session.auth
        if session_auth is not None and not (
            type(session_auth) is requests.auth.HTTPBasicAuth
            or (
                isinstance(session_auth, tuple)
                and len(session_auth) == 2
                and all(isinstance(item, (str, bytes)) for item in session_auth)
            )
        ):
            return None
        try:
            prepared = session.prepare_request(
                requests.Request(
                    "GET",
                    url,
                    params=dict(params) if params else None,
                    headers={**self._config.default_headers, **(headers or {})},
                )
            )
            if prepared.url is None:
                return None
            if session.trust_env:
                if requests.utils.get_environ_proxies(prepared.url):
                    return None
                # Requests injects .netrc auth during send, after preparation.
                # Skip when present rather than risk omitting credentials.
                if (
                    "Authorization" not in prepared.headers
                    and requests.utils.get_netrc_auth(prepared.url) is not None
                ):
                    return None
        except (OSError, requests.RequestException, TypeError, ValueError):
            return None
        return _CacheRequestIdentity(endpoint=prepared.url, headers=prepared.headers)

    def _semaphore_for(self, host: str) -> threading.BoundedSemaphore:
        """Return the shared host concurrency gate."""
        with self._lock:
            return self._semaphores.setdefault(
                host, threading.BoundedSemaphore(self._config.max_concurrency_per_host)
            )

    def _log_request_event(
        self,
        source: str,
        url: str,
        params: Mapping[str, str] | None,
        request_id: str,
        attempt: int,
        started: float,
        status: int | None,
        failure_category: str | None,
        *,
        cache_hit: bool = False,
    ) -> None:
        """Emit one credential-safe structured event for an attempt."""
        elapsed = max(0.0, self._time() - started)
        event = {
            "event": "governed_http_request",
            "source": source,
            "endpoint": _safe_endpoint(url),
            "parameter_summary": _parameter_summary(params),
            "elapsed_seconds": elapsed,
            "request_id": request_id,
            "attempt": attempt,
            "status": status,
            "failure_category": failure_category,
        }
        if cache_hit:
            event["cache_hit"] = True
        logger.bind(**event).info("governed HTTP request")

    def _restore_cached_response(
        self, url: str, cached: CachedRawResponse, started: float
    ) -> requests.Response:
        """Recreate the requests response surface used by macro adapters."""
        response = requests.Response()
        response.status_code = cached.status_code
        response._content = cached.content
        content_consumed: bool = True
        # Requests stubs vary on this private response flag.
        setattr(response, "_content_consumed", content_consumed)  # noqa: B010
        response.encoding = cached.encoding
        response.url = _safe_endpoint(url)
        response.elapsed = timedelta(seconds=max(0.0, self._time() - started))
        return response

    def _require_host(self, url: str) -> str:
        """Parse the URL host, failing closed on host-less URLs.

        Raises:
            ValueError: If the URL has no host.
        """
        host = urlsplit(url).hostname
        if not host:
            raise ValueError(f"URL has no host: {_safe_endpoint(url)}")
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

    def _guard_breaker(self, host: str, url: str) -> bool:
        """Fail fast while a host's breaker is open; half-open after cooldown.

        Returns true when this call owns the single half-open probe slot.

        Raises:
            HttpFetchError: With the tripping category while open.
        """
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is None or breaker.opened_at is None:
                return False
            if (
                breaker.probe_in_flight
                or self._time() < breaker.opened_at + self._config.breaker_cooldown
            ):
                category = breaker.category or FailureCategory.RATE_LIMITED
                raise self._error(category, url, host, attempts=0, retryable=False)
            # Keep the breaker open while a single caller owns the probe.
            # Other callers fail fast until that probe succeeds or fails.
            breaker.probe_in_flight = True
            breaker.consecutive = 0
            return True

    def _record_probe_failure(self, host: str, category: FailureCategory) -> None:
        """Reopen a half-open breaker after any failed probe attempt."""
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is None or not breaker.probe_in_flight:
                return
            breaker.opened_at = self._time()
            breaker.consecutive = self._config.breaker_threshold
            breaker.category = category
            breaker.probe_in_flight = False

    def _release_probe_slot(self, host: str) -> None:
        """Release probe ownership after a pre-send governance denial.

        Keep the original failure category and open timestamp so the upstream
        failure continues to govern cooldown and diagnostics.
        """
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is not None and breaker.probe_in_flight:
                breaker.probe_in_flight = False

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

    def _record_success(self, host: str, *, probe_in_flight: bool = False) -> None:
        """Reset the breaker bookkeeping after a successful response."""
        with self._lock:
            breaker = self._breakers.get(host)
            if breaker is not None and breaker.opened_at is not None and not probe_in_flight:
                return
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


_shared_client: GovernedHttpClient | None = None
_shared_client_lock = threading.Lock()


def get_shared_http_client() -> GovernedHttpClient:
    """Return the process-wide governed transport for contract providers."""
    global _shared_client
    if _shared_client is None:
        with _shared_client_lock:
            if _shared_client is None:
                _shared_client = GovernedHttpClient()
    return _shared_client
