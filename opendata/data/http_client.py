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
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlencode, urlsplit

import requests
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from urllib3.exceptions import ReadTimeoutError
from urllib3.util.retry import Retry

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
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    reserve_scoped_attempt,
    scoped_host_slot,
)
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    TransportAttempt,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_SENSITIVE_PARAMETER_PARTS = ("api_key", "apikey", "token", "secret", "password", "credential")
_STREAM_CHUNK_BYTES = 64 * 1024
_MAX_BOUNDED_FORM_BYTES = 1024 * 1024
_MAX_BOUNDED_FORM_FIELDS = 256
_BOUNDED_FORM_CALLER_HEADERS = frozenset(
    {"accept", "accept-language", "origin", "referer", "user-agent", "x-requested-with"}
)
_BOUNDED_FORM_SESSION_HEADERS = _BOUNDED_FORM_CALLER_HEADERS | frozenset(
    {"accept-encoding", "connection", "content-type"}
)
_UNSAFE_FORM_TRANSPORT_HEADERS = frozenset(
    {
        "authorization",
        "cookie",
        "content-length",
        "transfer-encoding",
        "content-encoding",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "x-http-method-override",
        "x-method-override",
    }
)
_BOUNDED_SESSION_METHODS = (
    "request",
    "prepare_request",
    "send",
    "merge_environment_settings",
    "get_adapter",
    "resolve_redirects",
    "get_redirect_target",
)


class _OverallTimeoutError(Exception):
    """Internal signal that a bounded request exhausted its wall-time budget."""


class _ResponseBodyPolicyError(Exception):
    """Internal signal for a bounded response rejected by local body policy."""

    def __init__(self, category: FailureCategory) -> None:
        self.category = category


class _BoundedRedirectError(Exception):
    """Internal signal raised before Requests can consume a redirect body."""

    def __init__(self, response: requests.Response) -> None:
        super().__init__("bounded request redirect rejected")
        self.response = response


class _BoundedFormInputError(ValueError):
    """Internal safe diagnostic for a rejected form value or header."""


@dataclass(frozen=True, slots=True)
class BoundedHttpResponse:
    """Immutable result of a successful body-limited HTTP GET.

    Attributes:
        body: Response body bytes, bounded by the call's application-level cap.
        status_code: The actual successful HTTP status code.
        retrieved_at: UTC time when the response body finished streaming.
    """

    body: bytes
    status_code: int
    retrieved_at: datetime


def _strict_positive_seconds(name: str, value: object) -> float:
    """Validate one finite positive duration, rejecting bool and numeric lookalikes."""
    if type(value) is int:
        try:
            seconds = float(value)
        except OverflowError:
            raise ValueError(f"{name} must be a finite positive number") from None
    elif type(value) is float:
        seconds = value
    else:
        raise TypeError(f"{name} must be an int or float")
    if not math.isfinite(seconds) or seconds <= 0.0:
        raise ValueError(f"{name} must be a finite positive number")
    return seconds


def _strict_timeout(
    timeout: float | tuple[float, float] | None,
) -> float | tuple[float, float] | None:
    """Validate a bounded GET's connect/read timeout override."""
    if timeout is None:
        return None
    if type(timeout) in (int, float):
        return _strict_positive_seconds("timeout", timeout)
    if type(timeout) is not tuple or len(timeout) != 2:
        raise TypeError("timeout must be a positive number or a pair of positive numbers")
    connect_timeout = _strict_positive_seconds("connect timeout", timeout[0])
    read_timeout = _strict_positive_seconds("read timeout", timeout[1])
    return connect_timeout, read_timeout


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


def _encode_bounded_form(form: Mapping[str, str], max_bytes: int) -> bytes:
    """Snapshot and encode a form without retaining more than its byte cap."""
    if not isinstance(form, Mapping):
        raise TypeError("form must be a mapping of strings")

    encoded = bytearray()
    field_count = 0
    seen_keys: set[str] = set()
    try:
        items = iter(form.items())
        while True:
            try:
                key, value = next(items)
            except StopIteration:
                break
            field_count += 1
            if field_count > _MAX_BOUNDED_FORM_FIELDS:
                raise _BoundedFormInputError("form contains too many fields")
            if type(key) is not str or type(value) is not str:
                raise _BoundedFormInputError("form fields must be exact strings")
            if key in seen_keys:
                raise _BoundedFormInputError("form contains duplicate field names")
            seen_keys.add(key)
            # A character-count precheck bounds the temporary UTF-8 conversion.
            if len(key) > max_bytes or len(value) > max_bytes:
                raise _BoundedFormInputError("form field exceeds its byte limit")
            if len(key.encode("utf-8")) > max_bytes or len(value.encode("utf-8")) > max_bytes:
                raise _BoundedFormInputError("form field exceeds its byte limit")
            pair = urlencode(((key, value),), encoding="utf-8", errors="strict").encode("ascii")
            separator_bytes = 1 if encoded else 0
            if len(encoded) + separator_bytes + len(pair) > max_bytes:
                raise _BoundedFormInputError("encoded form exceeds max_form_bytes")
            if separator_bytes:
                encoded.extend(b"&")
            encoded.extend(pair)
    except _BoundedFormInputError:
        raise
    except Exception:
        # Mapping and Unicode implementations can raise messages containing
        # caller data; replace those with a stable, value-free diagnostic.
        raise ValueError("form could not be encoded safely") from None
    return bytes(encoded)


def _snapshot_form_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Copy only the bounded form API's safe caller-controlled headers."""
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise TypeError("headers must be a mapping of strings")
    copied: dict[str, str] = {}
    seen_names: set[str] = set()
    try:
        for name, value in headers.items():
            if type(name) is not str or type(value) is not str:
                raise _BoundedFormInputError("form headers must be exact strings")
            if not name or not name.isascii():
                raise _BoundedFormInputError("form request header is malformed")
            normalized = name.lower()
            if normalized not in _BOUNDED_FORM_CALLER_HEADERS:
                raise _BoundedFormInputError("form request contains an unsupported header")
            if normalized in seen_names:
                raise _BoundedFormInputError("form request contains duplicate headers")
            seen_names.add(normalized)
            if not value.isascii() or any(
                ord(character) < 32 or ord(character) == 127 for character in value
            ):
                raise _BoundedFormInputError("form request header is malformed")
            copied[name] = value
    except _BoundedFormInputError:
        raise
    except Exception:
        raise ValueError("form request headers could not be copied safely") from None
    return copied


class FailureCategory(str, Enum):
    """Failure classification of one governed request (§5.3)."""

    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    REDIRECT = "redirect"
    UPSTREAM_ERROR = "upstream_error"
    BODY_TOO_LARGE = "body_too_large"
    UNSUPPORTED_CONTENT_ENCODING = "unsupported_content_encoding"


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
        source_attempt_gate: SourceAttemptGate | None = None,
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
            source_attempt_gate: Optional persistent gate for every physical
                scoped transport attempt. It is never selected from configuration
                or injected into the shared singleton implicitly.
        """
        self._config = config if config is not None else HttpClientConfig()
        if source_attempt_gate is not None and type(source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be a SourceAttemptGate or None")
        self._session_override = session
        self._thread_local = threading.local()
        self._injected_session_lock = threading.Lock()
        self._sleep = sleep if sleep is not None else time.sleep
        self._time = time_fn if time_fn is not None else time.monotonic
        self._jitter = jitter_fn if jitter_fn is not None else self._default_jitter
        self._raw_response_cache_override = raw_response_cache
        self._source_attempt_gate = source_attempt_gate
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
        use_cache: bool = True,
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
            use_cache: Whether to read or write the raw-response cache.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            TypeError: If ``use_cache`` is not a bool.
            RequestAuthorizationError: If active trusted scopes, transport, or the
                optional persistent gate reject the request.
            HttpFetchError: On terminal, classified failure.
        """
        if type(use_cache) is not bool:
            raise TypeError("use_cache must be a bool")
        return self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            source=source,
            cache_context=cache_context,
            use_cache=use_cache,
        )

    def get_bounded(
        self,
        url: str,
        *,
        max_body_bytes: int,
        overall_timeout: float,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Stream a governed GET into a body bounded by an explicit byte cap.

        Args:
            url: Absolute URL.
            max_body_bytes: Maximum response-body bytes retained by this call.
            overall_timeout: Cooperative wall-time budget in seconds. It is checked
                during rate-token, local semaphore, and shared scoped host-slot waits,
                before retries, after EOF, and between body chunks. Active request
                scope deadline/cancellation checks remain in force alongside it.
                Connect/read timeouts remain socket-operation limits; a blocking read
                can overrun this budget by up to its read timeout before control returns.
            params: Optional query parameters.
            headers: Per-request headers merged over defaults. Any supplied
                ``Accept-Encoding`` value is replaced with ``identity``.
            timeout: Optional connect/read timeout override in seconds or as a pair.
            source: Stable source name included in structured events.

        Returns:
            The actual HTTP status, UTC retrieval time, and bounded body bytes.

        Raises:
            TypeError: If either explicit bound has the wrong exact type.
            ValueError: If a bound is non-finite, non-positive, or the URL has no host.
            RequestAuthorizationError: If the session does not use the standard Requests
                request/response pipeline or has session-level response hooks.
            RequestAdapterRetryError: If a mounted adapter can retry outside this client.
            HttpFetchError: On transport, status, deadline, size, or encoding failure.

        The method does not read from or write to the raw-response cache. It asks
        for identity encoding and rejects other content encodings before reading,
        because Requests decodes compressed bodies while iterating. The byte cap
        limits response-body bytes collected and returned. It is not a hard limit on
        transient allocations, transport buffers, or process RSS. Bounded calls
        require the standard Requests session request/prepare/send/redirect pipeline,
        empty session response hooks, and adapters with retries disabled. Injected
        adapters are trusted transport components and must honor streaming without
        reading bodies or issuing hidden attempts.
        """
        if type(max_body_bytes) is not int:
            raise TypeError("max_body_bytes must be an exact int")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        total_seconds = _strict_positive_seconds("overall_timeout", overall_timeout)
        checked_timeout = _strict_timeout(timeout)
        started = self._time()
        deadline = started + total_seconds
        if not math.isfinite(deadline):
            raise ValueError("overall_timeout produces an invalid deadline")
        bounded_headers = dict(headers or {})
        for name in tuple(bounded_headers):
            if name.lower() == "accept-encoding":
                del bounded_headers[name]
        bounded_headers["Accept-Encoding"] = "identity"
        result = self._request(
            "GET",
            url,
            params=params,
            headers=bounded_headers,
            timeout=checked_timeout,
            source=source,
            use_cache=False,
            body_limit=max_body_bytes,
            deadline=deadline,
        )
        if not isinstance(result, BoundedHttpResponse):
            raise RuntimeError("bounded HTTP attempt returned an ordinary response")
        return result

    def post_form_bounded(
        self,
        url: str,
        form: Mapping[str, str],
        *,
        max_form_bytes: int,
        max_body_bytes: int,
        overall_timeout: float,
        headers: Mapping[str, str] | None = None,
        timeout: float | tuple[float, float] | None = None,
        source: str = "unspecified",
    ) -> BoundedHttpResponse:
        """Send one authorized form POST and stream its bounded response.

        The form is snapshotted and URL-encoded locally. Its encoded ASCII byte
        length must fit ``max_form_bytes``. The request is authorized by an
        active QUERY scope, consumes its normal request budget, uses one
        transport attempt, and never enters the raw response cache. Caller
        headers are limited to presentation and source-origin headers; request
        framing, authentication, content type, and compression headers stay
        under this transport's control.

        The response uses the same streaming, identity-encoding, body-limit,
        redirect, rate-limit, and cooperative deadline rules as
        :meth:`get_bounded`. As with that method, a blocking socket read may
        exceed the overall deadline by up to its currently applied read timeout.

        Args:
            url: Absolute HTTPS URL covered by the active source grant.
            form: Ordered mapping of exact string field names and values.
            max_form_bytes: Maximum encoded request-body bytes.
            max_body_bytes: Maximum response-body bytes retained.
            overall_timeout: Cooperative wall-time budget in seconds.
            headers: Optional safe source request headers.
            timeout: Optional connect/read timeout override.
            source: Exact source name present in the active QUERY grant.

        Returns:
            The successful HTTP status, UTC retrieval time, and bounded body.

        Raises:
            TypeError: If bounds, form, or headers have unsupported types.
            ValueError: If a bound is invalid or the encoded form exceeds its cap.
            RequestAuthorizationError: If no active QUERY grant authorizes the
                request or the session could hide authentication or retries.
            RequestAdapterRetryError: If a transport adapter can retry requests.
            HttpFetchError: On transport, status, deadline, size, or encoding failure.

        Form field values are never included in logs or exception messages.
        """
        if type(max_form_bytes) is not int:
            raise TypeError("max_form_bytes must be an exact int")
        if max_form_bytes <= 0:
            raise ValueError("max_form_bytes must be positive")
        if max_form_bytes > _MAX_BOUNDED_FORM_BYTES:
            raise ValueError("max_form_bytes exceeds the local safety limit")
        if type(max_body_bytes) is not int:
            raise TypeError("max_body_bytes must be an exact int")
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        total_seconds = _strict_positive_seconds("overall_timeout", overall_timeout)
        checked_timeout = _strict_timeout(timeout)
        started = self._time()
        deadline = started + total_seconds
        if not math.isfinite(deadline):
            raise ValueError("overall_timeout produces an invalid deadline")

        scopes = current_request_scopes()
        if not scopes or any(scope.operation is not RequestOperation.QUERY for scope in scopes):
            raise RequestAuthorizationError("bounded form POST requires an active QUERY scope")
        host = self._require_host(url)
        self._validate_scoped_url(url, host)
        if self._source_attempt_gate is not None:
            self._validate_source_attempt_scopes(scopes)
        elif any(scope.source_attempt_binding is not None for scope in scopes):
            raise RequestAuthorizationError(
                "source attempt binding requires a configured durable gate"
            )
        authorize_request(scopes, source, host)

        form_body = _encode_bounded_form(form, max_form_bytes)
        request_headers = _snapshot_form_headers(headers)
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        request_headers["Accept-Encoding"] = "identity"

        session = self._get_form_session()
        self._validate_bounded_form_session(session)
        try:
            result = self._request(
                "POST",
                url,
                headers=request_headers,
                timeout=checked_timeout,
                source=source,
                use_cache=False,
                body_limit=max_body_bytes,
                deadline=deadline,
                form_body=form_body,
                transport_session=session,
            )
        except HttpFetchError as error:
            # The POST may have reached the source even when its outcome is unknown.
            # Never signal that a caller may safely replay the form automatically.
            error.retryable = False
            raise
        if not isinstance(result, BoundedHttpResponse):
            raise RuntimeError("bounded form POST returned an ordinary response")
        return result

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
        use_cache: bool = True,
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
            use_cache: Whether to read or write the raw-response cache.

        Returns:
            The successful transport response.

        Raises:
            ValueError: If the URL has no host.
            TypeError: If ``use_cache`` is not a bool.
            RequestAuthorizationError: If active trusted scopes, transport, or the
                optional persistent gate reject the request.
            HttpFetchError: On terminal, classified failure.
        """
        result = self._request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            source=source,
            cache_context=cache_context,
            json_body=json_body,
            use_cache=use_cache,
        )
        if isinstance(result, BoundedHttpResponse):
            raise RuntimeError("ordinary HTTP request returned a bounded response")
        return result

    def _request(
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
        form_body: bytes | None = None,
        transport_session: requests.Session | None = None,
        use_cache: bool = True,
        body_limit: int | None = None,
        deadline: float | None = None,
    ) -> requests.Response | BoundedHttpResponse:
        """Run the shared governed loop for ordinary and bounded requests."""
        if type(use_cache) is not bool:
            raise TypeError("use_cache must be a bool")
        if body_limit is not None and (type(body_limit) is not int or body_limit <= 0):
            raise ValueError("body_limit must be a positive exact int")
        request_body = _snapshot_json_body(json_body)
        method_upper = method.upper()
        if request_body is not None and method_upper in {"GET", "HEAD"}:
            raise ValueError("json_body is not allowed for GET or HEAD requests")
        if form_body is not None:
            if type(form_body) is not bytes:
                raise TypeError("form_body must be exact bytes")
            if request_body is not None or method_upper != "POST" or body_limit is None:
                raise ValueError("form_body requires a bounded POST without a JSON body")
        host = self._require_host(url)
        scopes = current_request_scopes()
        scoped = bool(scopes)
        if self._source_attempt_gate is not None:
            self._validate_source_attempt_scopes(scopes)
        elif any(scope.source_attempt_binding is not None for scope in scopes):
            raise RequestAuthorizationError(
                "source attempt binding requires a configured durable gate"
            )
        if scoped:
            self._validate_scoped_url(url, host)
            authorize_request(scopes, source, host)
            # Authorization precedes both cache access and lazy Session creation.
            session = transport_session if transport_session is not None else self._get_session()
            if body_limit is None:
                self._validate_scoped_session(session)
            elif form_body is not None:
                self._validate_bounded_form_session(session)
            else:
                self._validate_bounded_session(session)
            if self._source_attempt_gate is not None:
                self._validate_attempt_gated_session(session)
        elif body_limit is not None:
            session = transport_session if transport_session is not None else self._get_session()
            self._validate_bounded_session(session)
        is_get = method_upper == "GET"
        attempts = self._config.max_attempts if is_get else 1
        request_id = uuid.uuid4().hex
        request_started = self._time() if deadline is not None else 0.0
        raw_cache: RawResponseCache | None = None
        if (
            use_cache
            and body_limit is None
            and not (self._source_attempt_gate is not None and scoped)
        ):
            raw_cache = self._raw_response_cache_override
            if raw_cache is None and is_get:
                raw_cache = get_configured_raw_response_cache()
        cache_identity: _CacheRequestIdentity | None = None
        if use_cache and body_limit is None and raw_cache is not None and is_get:
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
        last_error: HttpFetchError | None = None
        while True:
            if scoped:
                check_scope_liveness(scopes)
            if deadline is not None and self._time() >= deadline:
                if last_error is not None:
                    raise last_error
                deadline_error = self._error(
                    FailureCategory.TIMEOUT, url, host, attempt, retryable=False
                )
                self._log_request_event(
                    source,
                    url,
                    params,
                    request_id,
                    attempt,
                    request_started,
                    None,
                    deadline_error.category.value,
                )
                raise deadline_error
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
                raw_cache if use_cache and is_get and cache_identity is not None else None,
                cache_context,
                cache_identity,
                scopes,
                form_body=form_body,
                transport_session=transport_session,
                body_limit=body_limit,
                deadline=deadline,
            )
            if error is None:
                if attempt_response is None:  # pragma: no cover - invariant guard
                    raise RuntimeError("successful HTTP attempt returned no response")
                return attempt_response
            last_error = error
            if not is_get or not error.retryable or attempt >= attempts:
                raise error
            if scoped:
                check_scope_liveness(scopes)
            if deadline is None:
                self._sleep_backoff(attempt)
            else:
                remaining = deadline - self._time()
                if remaining <= 0.0:
                    delay = None
                else:
                    delay = self._config.backoff_base * (2 ** (attempt - 1)) + self._jitter()
                if delay is None or delay >= remaining:
                    raise error
                if not math.isfinite(delay) or delay < 0.0:
                    raise ValueError("retry delay must be a finite non-negative number")
                self._sleep(delay)
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
        *,
        form_body: bytes | None = None,
        transport_session: requests.Session | None = None,
        body_limit: int | None = None,
        deadline: float | None = None,
    ) -> tuple[requests.Response | BoundedHttpResponse | None, HttpFetchError | None]:
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
            form_body: Optional pre-encoded body for a bounded form POST.
            transport_session: Optional form-only Requests session already
                validated by the bounded form transport.
            body_limit: Optional application-level body cap that enables streaming.
            deadline: Optional cooperative monotonic deadline for a bounded request.

        Returns:
            ``(response, None)`` on success or ``(None, error)`` on
            classified failure. Bounded calls return ``BoundedHttpResponse``.
        """
        started = self._time()
        probe_in_flight = False
        transport_started = False
        response: requests.Response | None = None
        streamed_body: bytes | None = None

        def mark_transport_started() -> None:
            nonlocal transport_started
            transport_started = True

        try:
            if scopes:
                check_scope_liveness(scopes)
            if deadline is None:
                self._acquire_token(host, url)
            else:
                self._acquire_token(host, url, deadline=deadline)
            self._check_deadline(deadline)
            if scopes:
                check_scope_liveness(scopes)
            probe_in_flight = self._guard_breaker(host, url)
            if scopes:
                scoped_semaphore = (
                    self._scoped_semaphore(host, scopes)
                    if deadline is None
                    else self._scoped_semaphore(host, scopes, deadline=deadline)
                )
                with scoped_semaphore:
                    check_scope_liveness(scopes)
                    self._check_deadline(deadline)
                    with scoped_host_slot(
                        scopes,
                        source,
                        host,
                        check_wait_liveness=(
                            None if deadline is None else lambda: self._check_deadline(deadline)
                        ),
                    ):
                        check_scope_liveness(scopes)
                        self._check_deadline(deadline)
                        request_timeout = self._remaining_timeout(timeout, deadline)
                        response = self._send(
                            method,
                            url,
                            params,
                            headers,
                            request_timeout,
                            json_body,
                            form_body=form_body,
                            transport_session=transport_session,
                            scopes=scopes,
                            source=source,
                            host=host,
                            request_id=request_id,
                            attempt_no=attempt,
                            deadline=deadline,
                            on_transport_start=mark_transport_started,
                            stream=body_limit is not None,
                        )
                        if self._should_read_bounded_response(response, body_limit):
                            streamed_body = self._read_bounded_response(
                                response,
                                body_limit=body_limit,
                                deadline=deadline,
                                scopes=scopes,
                            )
            else:
                with self._request_semaphore(host, deadline):
                    self._check_deadline(deadline)
                    request_timeout = self._remaining_timeout(timeout, deadline)
                    response = self._send(
                        method,
                        url,
                        params,
                        headers,
                        request_timeout,
                        json_body,
                        form_body=form_body,
                        transport_session=transport_session,
                        stream=body_limit is not None,
                        request_id=request_id,
                        attempt_no=attempt,
                        deadline=deadline,
                        on_transport_start=mark_transport_started,
                    )
                    if self._should_read_bounded_response(response, body_limit):
                        streamed_body = self._read_bounded_response(
                            response,
                            body_limit=body_limit,
                            deadline=deadline,
                            scopes=scopes,
                        )
        except _BoundedRedirectError as redirect_signal:
            response = redirect_signal.response
            response.close()
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.REDIRECT)
            else:
                self._record_success(host)
            redirect_error = self._error(
                FailureCategory.REDIRECT,
                url,
                host,
                attempt,
                status=response.status_code,
                retryable=False,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                response.status_code,
                redirect_error.category.value,
            )
            return None, redirect_error
        except RequestBudgetError:
            if probe_in_flight:
                self._release_probe_slot(host)
            raise
        except _OverallTimeoutError:
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.TIMEOUT)
            timeout_error = self._error(
                FailureCategory.TIMEOUT,
                url,
                host,
                attempt if transport_started else attempt - 1,
                status=response.status_code if response is not None else None,
                retryable=False,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt if transport_started else attempt - 1,
                started,
                response.status_code if response is not None else None,
                timeout_error.category.value,
            )
            if response is not None and body_limit is not None:
                response.close()
            return None, timeout_error
        except _ResponseBodyPolicyError as policy_error:
            if probe_in_flight:
                self._record_success(host, probe_in_flight=True)
            if response is None:
                raise RuntimeError("body policy rejected a missing HTTP response") from None
            policy_failure = self._error(
                policy_error.category,
                url,
                host,
                attempt,
                status=response.status_code,
                retryable=False,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                response.status_code,
                policy_failure.category.value,
            )
            return None, policy_failure
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
                status=response.status_code if response is not None else None,
                retryable=not probe_in_flight,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                response.status_code if response is not None else None,
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
                status=response.status_code if response is not None else None,
                retryable=not probe_in_flight,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                response.status_code if response is not None else None,
                network_error.category.value,
            )
            return None, network_error
        if response is None:
            raise RuntimeError("transport attempt returned no response")
        if body_limit is not None and 300 <= response.status_code < 400:
            response.close()
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.REDIRECT)
            else:
                self._record_success(host)
            redirect_error = self._error(
                FailureCategory.REDIRECT,
                url,
                host,
                attempt,
                status=response.status_code,
                retryable=False,
            )
            self._log_request_event(
                source,
                url,
                params,
                request_id,
                attempt,
                started,
                response.status_code,
                redirect_error.category.value,
            )
            return None, redirect_error
        if scopes and 300 <= response.status_code < 400:
            if probe_in_flight:
                self._record_probe_failure(host, FailureCategory.BLOCKED)
            raise RequestRedirectError("redirects are forbidden in a scoped request")
        category, retryable = _classify_status(response.status_code)
        if body_limit is not None and not 200 <= response.status_code < 300 and category is None:
            # Bounded responses succeed only with an ordinary 2xx status. In
            # particular, 1xx upgrade responses are never body data.
            category = FailureCategory.UPSTREAM_ERROR
            retryable = False
        if category is None:
            if (
                body_limit is None
                and raw_cache is not None
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
            if body_limit is not None:
                if streamed_body is None:
                    raise RuntimeError("bounded response body was not streamed")
                return (
                    BoundedHttpResponse(
                        body=streamed_body,
                        status_code=response.status_code,
                        retrieved_at=datetime.now(timezone.utc),
                    ),
                    None,
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
        if body_limit is not None:
            response.close()
        return None, status_error

    @staticmethod
    def _should_read_bounded_response(
        response: requests.Response,
        body_limit: int | None,
    ) -> bool:
        """Return whether a successful 2xx response can be read under the body cap."""
        if body_limit is None:
            return False
        return bool(200 <= response.status_code < 300)

    @staticmethod
    def _reject_bounded_redirect_before_requests_resolution(
        response: requests.Response,
        **_kwargs: object,
    ) -> requests.Response:
        """Stop redirect resolution in the response-hook phase, before body reads."""
        if 300 <= response.status_code < 400:
            raise _BoundedRedirectError(response)
        return response

    def _read_bounded_response(
        self,
        response: requests.Response,
        *,
        body_limit: int | None,
        deadline: float | None,
        scopes: tuple[RequestExecutionScope, ...],
    ) -> bytes:
        """Read a response in bounded chunks and always close its transport."""
        if body_limit is None:
            raise RuntimeError("bounded response reading requires a body cap")
        try:
            encoding = response.headers.get("Content-Encoding", "")
            normalized_encoding = encoding.strip().lower()
            if normalized_encoding not in ("", "identity"):
                raise _ResponseBodyPolicyError(FailureCategory.UNSUPPORTED_CONTENT_ENCODING)
            declared_length = self._declared_content_length(response)
            if declared_length is not None and declared_length > body_limit:
                raise _ResponseBodyPolicyError(FailureCategory.BODY_TOO_LARGE)
            chunks = iter(
                response.iter_content(chunk_size=min(_STREAM_CHUNK_BYTES, body_limit + 1))
            )
            body = bytearray()
            while True:
                self._check_deadline(deadline)
                if scopes:
                    check_scope_liveness(scopes)
                try:
                    chunk = next(chunks)
                except StopIteration:
                    self._check_deadline(deadline)
                    if scopes:
                        check_scope_liveness(scopes)
                    break
                if not isinstance(chunk, bytes):
                    raise requests.RequestException("response stream yielded non-bytes data")
                if len(body) + len(chunk) > body_limit:
                    raise _ResponseBodyPolicyError(FailureCategory.BODY_TOO_LARGE)
                body.extend(chunk)
            return bytes(body)
        except requests.ConnectionError as error:
            if any(isinstance(reason, ReadTimeoutError) for reason in error.args):
                raise requests.Timeout("response body read timed out") from None
            raise
        finally:
            response.close()

    @staticmethod
    def _declared_content_length(response: requests.Response) -> int | None:
        """Return a strictly decimal Content-Length, treating malformed values as unknown."""
        declared = response.headers.get("Content-Length")
        if declared is None:
            return None
        normalized = declared.strip()
        if not normalized or not normalized.isascii() or not normalized.isdecimal():
            return None
        try:
            return int(normalized)
        except ValueError:
            return None

    def _send(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        timeout: float | tuple[float, float] | None,
        json_body: Mapping[str, Any] | None,
        *,
        request_id: str,
        attempt_no: int,
        on_transport_start: Callable[[], None],
        scopes: tuple[RequestExecutionScope, ...] = (),
        source: str = "unspecified",
        host: str = "",
        deadline: float | None = None,
        stream: bool = False,
        form_body: bytes | None = None,
        transport_session: requests.Session | None = None,
    ) -> requests.Response:
        """Send one request through the session with governance kwargs."""
        if form_body is not None:
            merged = requests.structures.CaseInsensitiveDict(self._config.default_headers)
            merged.update(headers or {})
            merged["Content-Type"] = "application/x-www-form-urlencoded"
            merged["Accept-Encoding"] = "identity"
        else:
            # Keep the ordinary Requests path's historical plain-dict contract.
            merged = {**self._config.default_headers, **(headers or {})}
        request_timeout = (
            timeout
            if timeout is not None
            else (
                self._config.connect_timeout,
                self._config.read_timeout,
            )
        )
        session = transport_session if transport_session is not None else self._get_session()
        request_kwargs: dict[str, Any] = {
            "params": dict(params) if params else None,
            "headers": merged,
            "timeout": request_timeout,
            "proxies": self._config.proxies,
        }
        if self._source_attempt_gate is not None:
            request_kwargs.update({"verify": True, "cert": None, "proxies": {}})
        if scopes or stream:
            request_kwargs["allow_redirects"] = False
        if scopes:
            self._validate_scoped_session(session)
        if stream:
            request_kwargs["stream"] = True
            # The bounded-only request hook runs before Session.send resolves redirects.
            # Session-level hooks are rejected by _validate_bounded_session.
            request_kwargs["hooks"] = {
                "response": [self._reject_bounded_redirect_before_requests_resolution]
            }
        if json_body is not None:
            request_kwargs["json"] = json_body
        if form_body is not None:
            request_kwargs["data"] = form_body

        def send() -> requests.Response:
            if scopes:
                check_scope_liveness(scopes)
                reserve_scoped_attempt(scopes, source, host)
                if self._source_attempt_gate is not None:
                    self._reserve_source_attempt(
                        scopes,
                        source=source,
                        host=host,
                        request_id=request_id,
                        attempt_no=attempt_no,
                    )
                    check_scope_liveness(scopes)
            self._check_deadline(deadline)
            if deadline is not None:
                request_kwargs["timeout"] = self._remaining_timeout(request_timeout, deadline)
                self._check_deadline(deadline)
            if scopes and self._source_attempt_gate is not None:
                check_scope_liveness(scopes)
            on_transport_start()
            response = session.request(method, url, **request_kwargs)
            if form_body is not None:
                session.cookies.clear()
            return response

        if self._session_override is not None:
            with self._injected_session_lock:
                return send()
        return send()

    def _reserve_source_attempt(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        *,
        source: str,
        host: str,
        request_id: str,
        attempt_no: int,
    ) -> None:
        """Persist one-use authorization immediately before a physical send.

        The local budget is reserved by the caller first. Gate failures and
        ambiguous commit results are converted to a non-retryable authorization
        error, so no transport or retry can reuse an uncertain reservation.
        """
        gate = self._source_attempt_gate
        if gate is None:  # pragma: no cover - guarded by the caller
            raise RequestAuthorizationError("source attempt gate is not configured")
        try:
            binding, canonical_model, operation = self._validated_source_attempt_scope(scopes)
            attempt = TransportAttempt(
                attempt_id=uuid.uuid4().hex,
                request_id=request_id,
                attempt_no=attempt_no,
                grant_id=binding.grant_id,
                expected_revision=binding.expected_revision,
                source=source,
                canonical_model=canonical_model,
                operation=operation.value,
                principal_scope=binding.principal_scope,
                host=host,
                run_key=binding.run_key,
            )
            reservation = gate.reserve_attempt(attempt)
        except Exception:
            raise RequestAuthorizationError(
                "source attempt gate did not authorize transport"
            ) from None
        if (
            type(reservation) is not AttemptReservation
            or reservation.status is not ReservationStatus.RESERVED_NEW
        ):
            raise RequestAuthorizationError("source attempt gate did not authorize transport")

    @staticmethod
    def _validate_source_attempt_scopes(
        scopes: tuple[RequestExecutionScope, ...],
    ) -> None:
        """Require one exact trusted source binding across all active scopes."""
        GovernedHttpClient._validated_source_attempt_scope(scopes)

    @staticmethod
    def _validated_source_attempt_scope(
        scopes: tuple[RequestExecutionScope, ...],
    ) -> tuple[SourceAttemptBinding, str, RequestOperation]:
        """Return the shared binding and identity after validating every scope."""
        if not scopes:
            raise RequestAuthorizationError(
                "source attempt gate requires consistent trusted request scopes"
            )
        first = scopes[0]
        binding = first.source_attempt_binding
        source = first.source
        canonical_model = first.canonical_model
        operation = first.operation
        if (
            type(binding) is not SourceAttemptBinding
            or type(source) is not str
            or not source
            or source != source.strip()
            or type(canonical_model) is not str
            or not canonical_model
            or canonical_model != canonical_model.strip()
            or type(operation) is not RequestOperation
        ):
            raise RequestAuthorizationError(
                "source attempt gate requires consistent trusted request scopes"
            )
        for scope in scopes:
            if (
                scope.source != source
                or scope.canonical_model != canonical_model
                or scope.operation is not operation
                or type(scope.source_attempt_binding) is not SourceAttemptBinding
                or scope.source_attempt_binding != binding
            ):
                raise RequestAuthorizationError(
                    "source attempt gate requires consistent trusted request scopes"
                )
        return binding, canonical_model, operation

    def _validate_attempt_gated_session(self, session: object) -> None:
        """Reject custom session or adapter code that could hide extra sends."""
        self._validate_bounded_session(session)
        if not isinstance(session, requests.Session) or type(session) is not requests.Session:
            raise RequestAuthorizationError(
                "source attempt gate requires the standard Requests transport"
            )
        if session.trust_env is not False:
            raise RequestAuthorizationError(
                "source attempt gate requires Requests environment trust to be disabled"
            )
        if (
            session.verify is not True
            or session.cert is not None
            or type(session.proxies) is not dict
            or session.proxies
            or self._config.proxies
        ):
            raise RequestAuthorizationError(
                "source attempt gate requires TLS verification, no proxies or client certificates"
            )
        for method_name in _BOUNDED_SESSION_METHODS:
            if method_name in session.__dict__:
                raise RequestAuthorizationError(
                    "source attempt gate rejects overridden Requests methods"
                )
        for adapter in session.adapters.values():
            if type(adapter) is not requests.adapters.HTTPAdapter or "send" in adapter.__dict__:
                raise RequestAuthorizationError(
                    "source attempt gate rejects custom transport adapters"
                )
            retries = getattr(adapter, "max_retries", None)
            if type(retries) is not Retry or any(
                getattr(retries, attribute, None) not in (None, 0, False)
                for attribute in ("total", "connect", "read", "redirect", "status", "other")
            ):
                raise RequestAdapterRetryError(
                    "source attempt gate requires Requests adapters with retries disabled"
                )

    def _get_session(self) -> requests.Session:
        """Return the injected session or a per-thread reusable session."""
        if self._session_override is not None:
            return self._session_override
        session = getattr(self._thread_local, "session", None)
        if session is None:
            session = requests.Session()
            self._thread_local.session = session
        return cast("requests.Session", session)

    def _get_form_session(self) -> requests.Session:
        """Return a form-only session with environment credential routing disabled."""
        if self._session_override is not None:
            return self._session_override
        session = getattr(self._thread_local, "form_session", None)
        if session is None:
            session = requests.Session()
            session.trust_env = False
            session.headers["Accept-Encoding"] = "identity"
            session.headers["Content-Type"] = "application/x-www-form-urlencoded"
            self._thread_local.form_session = session
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
        """Reject opaque authentication, response hooks, or adapter retries."""
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
                "governed requests reject unsupported session authentication"
            )
        if any(session.hooks.values()):
            raise RequestAuthorizationError("governed requests reject session response hooks")
        for adapter in session.adapters.values():
            retries = getattr(adapter, "max_retries", None)
            if retries is None or getattr(retries, "total", None) != 0:
                raise RequestAdapterRetryError(
                    "governed requests require adapters with retries disabled"
                )
            if any(
                getattr(retries, attribute, 0) not in (None, 0, False)
                for attribute in ("connect", "read", "redirect", "status", "other")
            ):
                raise RequestAdapterRetryError(
                    "governed requests require adapters with retries disabled"
                )

    def _validate_bounded_session(self, session: object) -> None:
        """Require the standard Requests hook and redirect-resolution pipeline."""
        if not isinstance(session, requests.Session):
            raise RequestAuthorizationError("bounded requests require a standard requests.Session")
        for method_name in _BOUNDED_SESSION_METHODS:
            actual_method = getattr(getattr(session, method_name), "__func__", None)
            expected_method = getattr(requests.Session, method_name)
            if actual_method is not expected_method:
                raise RequestAuthorizationError(
                    "bounded requests require standard requests.Session methods"
                )
        self._validate_scoped_session(session)

    def _validate_bounded_form_session(self, session: object) -> None:
        """Require a standard, direct session that cannot hide form credentials or retries."""
        if not isinstance(session, requests.Session) or type(session) is not requests.Session:
            raise RequestAuthorizationError(
                "bounded form POST requires the standard Requests session"
            )
        self._validate_bounded_session(session)
        if (
            session.trust_env is not False
            or session.verify is not True
            or session.cert is not None
            or type(session.proxies) is not dict
            or session.proxies
            or type(session.params) is not dict
            or session.params
            or self._config.proxies
            or session.auth is not None
            or type(session.cookies) is not requests.cookies.RequestsCookieJar
            or any(session.cookies)
        ):
            raise RequestAuthorizationError(
                "bounded form POST rejects environment routing and session credentials"
            )
        for header_map in (self._config.default_headers, session.headers):
            for name, value in header_map.items():
                if type(name) is not str or type(value) is not str:
                    raise RequestAuthorizationError(
                        "bounded form POST rejects malformed session headers"
                    )
                normalized = name.lower()
                if normalized in _UNSAFE_FORM_TRANSPORT_HEADERS:
                    raise RequestAuthorizationError(
                        "bounded form POST rejects session authentication or framing headers"
                    )
                if normalized not in _BOUNDED_FORM_SESSION_HEADERS:
                    raise RequestAuthorizationError(
                        "bounded form POST rejects unapproved session headers"
                    )
                if normalized == "accept-encoding" and value.strip().lower() != "identity":
                    raise RequestAuthorizationError(
                        "bounded form POST requires identity request encoding"
                    )
                if normalized == "content-type" and (
                    value.strip().lower() != "application/x-www-form-urlencoded"
                ):
                    raise RequestAuthorizationError(
                        "bounded form POST requires the form content type"
                    )
        for adapter in session.adapters.values():
            if type(adapter) is not requests.adapters.HTTPAdapter or "send" in adapter.__dict__:
                raise RequestAuthorizationError(
                    "bounded form POST rejects custom transport adapters"
                )
            retries = getattr(adapter, "max_retries", None)
            if type(retries) is not Retry or any(
                getattr(retries, attribute, None) not in (None, 0, False)
                for attribute in ("total", "connect", "read", "redirect", "status", "other")
            ):
                raise RequestAdapterRetryError(
                    "bounded form POST requires Requests adapters with retries disabled"
                )

    @contextmanager
    def _scoped_semaphore(
        self,
        host: str,
        scopes: tuple[RequestExecutionScope, ...],
        *,
        deadline: float | None = None,
    ) -> Iterator[None]:
        """Acquire this client's host gate while honoring scope cancellation."""
        semaphore = self._semaphore_for(host)
        while True:
            self._check_deadline(deadline)
            remaining = 0.05 if deadline is None else min(0.05, deadline - self._time())
            if remaining <= 0.0:
                raise _OverallTimeoutError
            if semaphore.acquire(timeout=remaining):
                break
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            self._check_deadline(deadline)
            yield
        finally:
            semaphore.release()

    @contextmanager
    def _request_semaphore(self, host: str, deadline: float | None) -> Iterator[None]:
        """Acquire a host gate, bounding a bounded request's wait by its deadline."""
        semaphore = self._semaphore_for(host)
        if deadline is None:
            semaphore.acquire()
        else:
            while True:
                remaining = deadline - self._time()
                if remaining <= 0.0:
                    raise _OverallTimeoutError
                if semaphore.acquire(timeout=min(0.05, remaining)):
                    break
        try:
            self._check_deadline(deadline)
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

    def _check_deadline(self, deadline: float | None) -> None:
        """Stop a bounded request when its cooperative monotonic budget expires."""
        if deadline is not None and self._time() >= deadline:
            raise _OverallTimeoutError

    def _remaining_timeout(
        self,
        timeout: float | tuple[float, float] | None,
        deadline: float | None,
    ) -> float | tuple[float, float] | None:
        """Clamp connect/read timeouts to the remaining total budget at send time."""
        if deadline is None:
            return timeout
        remaining = deadline - self._time()
        if remaining <= 0.0:
            raise _OverallTimeoutError
        if timeout is None:
            connect_timeout = self._config.connect_timeout
            read_timeout = self._config.read_timeout
        elif isinstance(timeout, tuple):
            connect_timeout, read_timeout = timeout
        else:
            connect_timeout = float(timeout)
            read_timeout = float(timeout)
        return min(float(connect_timeout), remaining), min(float(read_timeout), remaining)

    def _acquire_token(
        self,
        host: str,
        url: str,
        *,
        deadline: float | None = None,
    ) -> None:
        """Consume a rate-limit token, waiting up to the configured max.

        Raises:
            HttpFetchError: When the wait would exceed ``rate_max_wait``
                or the token was taken while waiting.
        """
        limit = self._config.rate_limit_per_host
        if limit is None:
            self._check_deadline(deadline)
            return
        with self._lock:
            bucket = self._buckets.setdefault(
                host, TokenBucket(limit, self._config.rate_burst, self._time)
            )
            if bucket.try_acquire(self._time()):
                self._check_deadline(deadline)
                return
            wait = bucket.wait_seconds(self._time())
        if wait > self._config.rate_max_wait:
            raise self._error(FailureCategory.RATE_LIMITED, url, host, attempts=0, retryable=False)
        if deadline is not None:
            remaining = deadline - self._time()
            if remaining <= 0.0 or wait >= remaining:
                raise _OverallTimeoutError
        if wait > 0.0:
            self._sleep(wait)
        self._check_deadline(deadline)
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
        return random.uniform(0.0, self._config.backoff_jitter)  # noqa: S311  # nosec B311  # retry backoff jitter only; no security value derives from this PRNG draw

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
