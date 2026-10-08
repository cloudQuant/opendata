"""fuyao HTTP 客户端：超时、重试、退避、限流联动与响应体上限（A3.1）.

职责边界：本模块只管**传输与失败分类**，不做端点语义与字段映射（那些由
``opendata_fuyao`` 的端点模块与 provider 层承担）。所有失败统一为
:class:`~opendata_fuyao.errors.FuyaoError`，携带中文业务文案与处置建议。

安全：API Key 只进请求头；日志与异常信息一律脱敏。
"""

from __future__ import annotations

import logging
import random
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from opendata.data.providers.ths.transport.envelope import FuyaoEnvelope, parse_envelope
from opendata.data.providers.ths.transport.errors import FuyaoError, error_for_transport
from opendata.data.providers.ths.transport.rate_limiter import (
    FuyaoRateLimiter,
    get_shared_concurrency_gate,
    get_shared_rate_limiter,
    shared_host_request,
)
from opendata.data.request_budget import (
    RequestAuthorizationError,
    RequestBudgetError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestRedirectError,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    reserve_scoped_attempt,
    scoped_host_slot,
)

if TYPE_CHECKING:
    from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
    from opendata.data.raw_response_cache import CachedRawResponse, RawResponseCache

logger = logging.getLogger(__name__)
_SENSITIVE_PARAMETER_PARTS = ("api_key", "apikey", "token", "secret", "password", "credential")
_cache_config_warning_lock = threading.Lock()
_cache_config_warning_emitted = False

#: 鉴权头（同生态 API 事实：``X-api-key``）。
API_KEY_HEADER = "X-api-key"

#: 默认可重试次数（不含首次请求）。
DEFAULT_MAX_ATTEMPTS = 3

#: 默认退避基数（秒）与抖动上限（秒）。
DEFAULT_BACKOFF_BASE_SECONDS = 0.5
DEFAULT_BACKOFF_JITTER_SECONDS = 0.25

#: 默认响应体上限（与消费者侧同花顺客户端一致）。
DEFAULT_MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: 默认单请求超时（秒）。
DEFAULT_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class FuyaoHttpResponse:
    """一次成功请求的传输层结果.

    Attributes:
        status_code: HTTP 状态码。
        envelope: 解析后的信封。
        elapsed_seconds: 耗时。
    """

    status_code: int
    envelope: FuyaoEnvelope
    elapsed_seconds: float


@dataclass(frozen=True)
class _CacheRequestIdentity:
    """Prepared request state used only for raw-response cache partitioning."""

    endpoint: str
    headers: Mapping[str, str]


class FuyaoHttpClient:
    """fuyao REST 客户端（同步）.

    Attributes:
        credentials: 凭据持有者。
    """

    def __init__(
        self,
        *,
        credentials: FuyaoCredentials,
        client: httpx.Client | None = None,
        mock_transport: httpx.MockTransport | None = None,
        rate_limiter: FuyaoRateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_jitter_seconds: float = DEFAULT_BACKOFF_JITTER_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
        raw_response_cache: RawResponseCache | None = None,
    ) -> None:
        """构造客户端.

        Args:
            credentials: 凭据。
            client: 注入的 httpx 客户端（测试用 MockTransport）；``None`` 时
                使用直连（不继承本地代理配置）。
            mock_transport: 仅供离线测试使用的精确 ``httpx.MockTransport``；
                与 ``client`` 互斥，并由本类创建受限客户端。
            rate_limiter: 限流器；``None`` 时使用默认配置。
            timeout_seconds: 单请求超时。
            max_attempts: 最大尝试次数（含首次）。
            backoff_base_seconds: 指数退避基数。
            backoff_jitter_seconds: 退避抖动上限（随机）。
            max_response_bytes: 响应体上限。
            sleep: 退避等待实现（测试注入，避免真实休眠）。
            raw_response_cache: 显式缓存注入；缺省时仅在应用设置启用后使用。
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if client is not None and mock_transport is not None:
            raise ValueError("client and mock_transport are mutually exclusive")
        if mock_transport is not None and type(mock_transport) is not httpx.MockTransport:
            raise ValueError("mock_transport must be an exact httpx.MockTransport")
        self.credentials = credentials
        self._owns_client = client is None
        if client is not None:
            self._client = client
            self._scoped_transport_verified = False
        else:
            transport = (
                mock_transport
                if mock_transport is not None
                else httpx.HTTPTransport(retries=0, trust_env=False)
            )
            self._client = httpx.Client(
                transport=transport,
                timeout=timeout_seconds,
                trust_env=False,
                auth=None,
                follow_redirects=False,
            )
            self._scoped_transport_verified = True
        host = urlsplit(credentials.base_url).hostname or "unknown"
        self._limiter = rate_limiter or (
            get_shared_rate_limiter(host) if self._owns_client else FuyaoRateLimiter()
        )
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_jitter_seconds = backoff_jitter_seconds
        self._max_response_bytes = max_response_bytes
        self._sleep = sleep
        self._concurrency_host = host
        self._raw_response_cache_override = raw_response_cache
        self._request_lock = threading.RLock()

    def close(self) -> None:
        """关闭自有的 httpx 客户端."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> FuyaoHttpClient:
        """进入上下文管理器."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """退出上下文管理器并关闭客户端."""
        self.close()

    def get(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
        cache_context: Mapping[str, Any] | None = None,
    ) -> FuyaoHttpResponse:
        """调用 GET，带重试/退避/限流联动.

        Args:
            endpoint: 以 ``/`` 开头的端点路径。
            params: 查询参数。
            cache_context: 可选的数据解释维度，仅用于区分原始响应缓存键。

        Returns:
            成功响应与解析后的信封。

        Raises:
            FuyaoError: 鉴权/权限/领域结果/限流/暂时性/传输失败，均含中文文案。
        """
        if not endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        # Client cookies and default query/header state are part of cache
        # identity. Keep preparation and its matching request in one critical
        # section so another call cannot change that state between the two.
        scopes = current_request_scopes()
        if scopes:
            while not self._request_lock.acquire(timeout=0.05):
                check_scope_liveness(scopes)
            try:
                check_scope_liveness(scopes)
                url = f"{self.credentials.base_url}{endpoint}"
                host = self._scoped_host(url)
                authorize_request(scopes, "ths", host)
                self._validate_scoped_client()
                return self._get_locked(
                    endpoint,
                    params=params,
                    cache_context=cache_context,
                    scopes=scopes,
                )
            finally:
                self._request_lock.release()
        with self._request_lock:
            return self._get_locked(
                endpoint,
                params=params,
                cache_context=cache_context,
                scopes=(),
            )

    def _get_locked(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None,
        cache_context: Mapping[str, Any] | None,
        scopes: tuple[RequestExecutionScope, ...],
    ) -> FuyaoHttpResponse:
        """Run one GET while holding the client-state lock."""
        last_error: FuyaoError | None = None
        request_id = uuid.uuid4().hex
        url = f"{self.credentials.base_url}{endpoint}"
        raw_cache = self._raw_response_cache_override
        if raw_cache is None:
            raw_cache = _get_configured_raw_response_cache()
        cache_headers = {API_KEY_HEADER: self.credentials.api_key}
        cache_identity = (
            self._prepare_cache_identity(url, params, cache_headers)
            if raw_cache is not None
            else None
        )
        if raw_cache is not None and cache_identity is not None:
            if scopes:
                check_scope_liveness(scopes)
                authorize_request(scopes, "ths", self._scoped_host(url))
            cached = raw_cache.get(
                source="ths",
                endpoint=cache_identity.endpoint,
                headers=cache_identity.headers,
                context=cache_context,
            )
            if cached is not None:
                started = time.monotonic()
                try:
                    if scopes:
                        check_scope_liveness(scopes)
                        if 300 <= cached.status_code < 400:
                            raise RequestRedirectError("scoped redirects are not allowed")
                    response = _restore_cached_httpx_response(url, cached)
                    result = self._handle_response(response, time.monotonic() - started)
                    if scopes:
                        check_scope_liveness(scopes)
                except RequestBudgetError:
                    raise
                except Exception as exc:
                    raw_cache.invalidate(
                        source="ths",
                        endpoint=url,
                        params=params,
                        headers=cache_headers,
                        context=cache_context,
                    )
                    logger.warning(
                        "raw response cache entry rejected",
                        extra={
                            "event": "raw_response_cache_warning",
                            "reason": "cached_response_invalid",
                            "error_type": exc.__class__.__name__,
                        },
                    )
                else:
                    self._log_request_event(
                        url=url,
                        params=params,
                        request_id=request_id,
                        attempt=0,
                        started=started,
                        status=cached.status_code,
                        failure_category=None,
                        cache_hit=True,
                    )
                    return result
        for attempt in range(1, self._max_attempts + 1):
            started = time.monotonic()
            try:
                self._limiter.wait_and_acquire(
                    self._scoped_sleep(scopes) if scopes else self._sleep
                )
                if scopes:
                    check_scope_liveness(scopes)
            except FuyaoError as exc:
                self._log_request_event(
                    url=url,
                    params=params,
                    request_id=request_id,
                    attempt=attempt,
                    started=started,
                    status=None,
                    failure_category=exc.category,
                )
                raise
            response_status: int | None = None
            request_event_logged = False
            try:
                if scopes:
                    request_host = self._scoped_host(url)
                    check_scope_liveness(scopes)
                    authorize_request(scopes, "ths", request_host)
                    self._validate_scoped_client()
                    timeout = self._scoped_timeout(scopes)
                    prepared_request = self._client.build_request(
                        "GET",
                        url,
                        params=dict(params or {}),
                        headers={API_KEY_HEADER: self.credentials.api_key},
                        timeout=timeout,
                    )
                    check_scope_liveness(scopes)
                    authorize_request(scopes, "ths", request_host)
                    self._validate_scoped_client()
                    with (
                        self._scoped_shared_host_request(scopes, request_host),
                        scoped_host_slot(scopes, "ths", request_host),
                    ):
                        check_scope_liveness(scopes)
                        authorize_request(scopes, "ths", request_host)
                        self._validate_scoped_client()
                        timeout = self._scoped_timeout(scopes)
                        prepared_request.extensions["timeout"] = httpx.Timeout(timeout).as_dict()
                        reserve_scoped_attempt(scopes, "ths", request_host)
                        response = self._client.send(prepared_request, follow_redirects=False)
                        check_scope_liveness(scopes)
                else:
                    with shared_host_request(self._concurrency_host):
                        response = self._client.get(
                            url,
                            params=dict(params or {}),
                            headers={API_KEY_HEADER: self.credentials.api_key},
                            timeout=self._timeout_seconds,
                        )
            except RequestBudgetError:
                raise
            except httpx.TimeoutException:
                last_error = error_for_transport("timeout")
            except httpx.HTTPError as exc:
                last_error = error_for_transport("network", detail=exc.__class__.__name__)
            except FuyaoError as exc:
                last_error = exc
            else:
                response_status = response.status_code
                if scopes and 300 <= response_status < 400:
                    raise RequestRedirectError("scoped redirects are not allowed")
                elapsed = time.monotonic() - started
                try:
                    result = self._handle_response(response, elapsed)
                except FuyaoError as exc:
                    last_error = exc
                    self._log_request_event(
                        url=url,
                        params=params,
                        request_id=request_id,
                        attempt=attempt,
                        started=started,
                        status=response.status_code,
                        failure_category=exc.category,
                    )
                    request_event_logged = True
                    if not exc.retryable:
                        raise
                else:
                    if (
                        raw_cache is not None
                        and cache_identity is not None
                        and 200 <= response.status_code < 300
                    ):
                        raw_cache.put(
                            method="GET",
                            source="ths",
                            endpoint=cache_identity.endpoint,
                            headers=cache_identity.headers,
                            context=cache_context,
                            status_code=response.status_code,
                            content=response.content,
                            encoding=response.encoding,
                        )
                    self._log_request_event(
                        url=url,
                        params=params,
                        request_id=request_id,
                        attempt=attempt,
                        started=started,
                        status=response.status_code,
                        failure_category=None,
                    )
                    return result
            if not request_event_logged:
                self._log_request_event(
                    url=url,
                    params=params,
                    request_id=request_id,
                    attempt=attempt,
                    started=started,
                    status=response_status,
                    failure_category=(last_error.category if last_error is not None else "unknown"),
                )
            if attempt >= self._max_attempts:
                break
            if scopes:
                self._scoped_sleep(scopes)(self._backoff_seconds(attempt))
            else:
                self._sleep(self._backoff_seconds(attempt))
        if last_error is None:  # pragma: no cover - 循环必然设置
            raise error_for_transport("unknown")
        raise last_error

    @staticmethod
    def _scoped_host(url: str) -> str:
        """Validate the exact HTTPS host shape required by a scoped grant."""
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError as exc:
            raise RequestAuthorizationError("scoped request URL is invalid") from exc
        if (
            parts.scheme.lower() != "https"
            or parts.hostname is None
            or parts.username is not None
            or parts.password is not None
            or port not in (None, 443)
        ):
            raise RequestAuthorizationError("scoped requests require an exact HTTPS host")
        return parts.hostname.lower()

    def _validate_scoped_client(self) -> None:
        """Fail closed on opaque clients or callbacks that can add sends."""
        if not self._scoped_transport_verified:
            raise RequestAuthorizationError("scoped request transport is not verified")
        if self._client.is_closed:
            raise RequestAuthorizationError("scoped request client is closed")
        if self._client.auth is not None:
            raise RequestAuthorizationError("scoped client authentication is not allowed")
        hooks = self._client.event_hooks
        if any(hooks.values()):
            raise RequestAuthorizationError("scoped client event hooks are not allowed")

    @staticmethod
    @contextmanager
    def _scoped_shared_host_request(
        scopes: tuple[RequestExecutionScope, ...], host: str
    ) -> Iterator[None]:
        """Acquire the legacy process-wide host gate with liveness polling."""
        gate = get_shared_concurrency_gate(host)
        while not gate.acquire(timeout=0.05):
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            yield
        finally:
            gate.release()

    def _scoped_sleep(self, scopes: tuple[RequestExecutionScope, ...]) -> Callable[[float], None]:
        """Wait in short increments so cancellation/deadlines stop later sends."""

        def sleep_with_checks(seconds: float) -> None:
            remaining = max(0.0, seconds)
            check_scope_liveness(scopes)
            while remaining > 0:
                interval = min(0.05, remaining)
                self._sleep(interval)
                remaining -= interval
                check_scope_liveness(scopes)

        return sleep_with_checks

    def _scoped_timeout(self, scopes: tuple[RequestExecutionScope, ...]) -> float:
        """Cap transport timeout at the earliest enclosing scope deadline."""
        check_scope_liveness(scopes)
        deadlines = [scope.deadline for scope in scopes if scope.deadline is not None]
        if not deadlines:
            return self._timeout_seconds
        remaining = min(deadline - time.monotonic() for deadline in deadlines)
        if remaining <= 0:
            raise RequestExecutionDeadlineError("request execution deadline expired")
        return min(self._timeout_seconds, remaining)

    def _prepare_cache_identity(
        self,
        url: str,
        params: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> _CacheRequestIdentity | None:
        """Fingerprint httpx's merged headers, cookies and query parameters."""
        if type(self._client) is not httpx.Client:
            return None
        if self._client.auth is not None:
            # Arbitrary auth flows can add credentials only at send time.
            return None
        if self._client.trust_env or (
            not self._scoped_transport_verified and getattr(self._client, "_mounts", None)
        ):
            # Proxy/environment routing is outside the prepared request identity.
            return None
        try:
            request = self._client.build_request(
                "GET", url, params=dict(params or {}), headers=dict(headers)
            )
        except Exception:
            return None
        return _CacheRequestIdentity(endpoint=str(request.url), headers=request.headers)

    def _log_request_event(
        self,
        *,
        url: str,
        params: Mapping[str, Any] | None,
        request_id: str,
        attempt: int,
        started: float,
        status: int | None,
        failure_category: str | None,
        cache_hit: bool = False,
    ) -> None:
        """Emit one structured transport event without credentials or values."""
        parts = urlsplit(url)
        host = parts.hostname or "unknown"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            port = None
        netloc = host if port is None else f"{host}:{port}"
        endpoint = f"{parts.scheme}://{netloc}{parts.path or '/'}"
        keys = tuple(sorted(str(key) for key in (params or {})))
        redacted_keys = tuple(
            key
            for key in keys
            if any(part in key.lower().replace("-", "_") for part in _SENSITIVE_PARAMETER_PARTS)
        )
        extra = {
            "event": "governed_http_request",
            "source": "ths",
            "endpoint": endpoint,
            "parameter_summary": {"keys": keys, "redacted_keys": redacted_keys},
            "elapsed_seconds": max(0.0, time.monotonic() - started),
            "request_id": request_id,
            "attempt": attempt,
            "status": status,
            "failure_category": failure_category,
        }
        if cache_hit:
            extra["cache_hit"] = True
        logger.info(
            "fuyao HTTP request",
            extra=extra,
        )

    def _handle_response(self, response: httpx.Response, elapsed: float) -> FuyaoHttpResponse:
        """校验状态码/响应体大小并解析信封."""
        content = response.content
        if len(content) > self._max_response_bytes:
            raise error_for_transport("response_too_large", detail=str(self._max_response_bytes))
        if response.status_code == 429:
            # 429 与业务码 4001 等效：记录冷却并按可重试分类抛出。
            self._limiter.record_rate_limit(retry_after_seconds=_retry_after(response))
            raise error_for_transport("rate_limited", detail="429")
        if response.status_code in {200, 201}:
            try:
                payload = response.json()
            except ValueError:
                raise error_for_transport("envelope_invalid", detail="json") from None
            if not isinstance(payload, Mapping):
                raise error_for_transport("envelope_invalid", detail="not_mapping")
            return FuyaoHttpResponse(
                status_code=response.status_code,
                envelope=parse_envelope(payload),
                elapsed_seconds=elapsed,
            )
        raise error_for_transport("http", detail=str(response.status_code))

    def _backoff_seconds(self, attempt: int) -> float:
        """指数退避 + 抖动（第 ``attempt`` 次失败后的等待）."""
        # 2.0 ** n：mypy 对 int ** 非常量指数推断为 Any，用浮点底数保持类型明确。
        base = self._backoff_base_seconds * (2.0 ** (attempt - 1))
        jitter: float = random.random() * self._backoff_jitter_seconds  # noqa: S311  # nosec B311
        return base + jitter


def _restore_cached_httpx_response(url: str, cached: CachedRawResponse) -> httpx.Response:
    """Recreate the JSON response surface while keeping the request query-free."""
    content_type = "application/json"
    if cached.encoding is not None:
        content_type = f"{content_type}; charset={cached.encoding}"
    return httpx.Response(
        cached.status_code,
        content=cached.content,
        headers={"content-type": content_type},
        request=httpx.Request("GET", url),
    )


def _get_configured_raw_response_cache() -> RawResponseCache | None:
    """Load application cache configuration lazily for optional web installs."""
    global _cache_config_warning_emitted
    try:
        from opendata.data.raw_response_cache import get_configured_raw_response_cache

        return get_configured_raw_response_cache()
    except Exception as exc:
        with _cache_config_warning_lock:
            if not _cache_config_warning_emitted:
                logger.warning(
                    "raw response cache configuration unavailable",
                    extra={
                        "event": "raw_response_cache_warning",
                        "reason": "optional_configuration_unavailable",
                        "error_type": exc.__class__.__name__,
                    },
                )
                _cache_config_warning_emitted = True
        return None


def _retry_after(response: httpx.Response) -> float | None:
    """解析 ``Retry-After`` 头（秒）."""
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


__all__ = [
    "API_KEY_HEADER",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "FuyaoHttpClient",
    "FuyaoHttpResponse",
]
