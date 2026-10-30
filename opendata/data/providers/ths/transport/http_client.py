"""fuyao HTTP 客户端：超时、重试、退避、限流联动与响应体上限（A3.1）.

职责边界：本模块只管**传输与失败分类**，不做端点语义与字段映射（那些由
``opendata_fuyao`` 的端点模块与 provider 层承担）。所有失败统一为
:class:`~opendata_fuyao.errors.FuyaoError`，携带中文业务文案与处置建议。

安全：API Key 只进请求头；日志与异常信息一律脱敏。
"""

from __future__ import annotations

import json
import logging
import math
import random
import ssl
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx

from opendata.data.providers.ths.transport.envelope import FuyaoEnvelope, parse_envelope
from opendata.data.providers.ths.transport.errors import (
    FuyaoError,
    error_for_transport,
)
from opendata.data.providers.ths.transport.rate_limiter import (
    FuyaoRateLimiter,
    get_shared_concurrency_gate,
    get_shared_rate_limiter,
    shared_host_request,
)
from opendata.data.request_budget import (
    SOURCE_ATTEMPT_SCOPE_V1,
    SOURCE_ATTEMPT_SCOPE_V3,
    SOURCE_ATTEMPT_SCOPE_V4,
    RequestAuthorizationError,
    RequestBudgetError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestOperation,
    RequestRedirectError,
    SourceAttemptBinding,
    SourceAttemptScopeV3Pin,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    reserve_scoped_attempt,
    scoped_host_slot,
    validate_source_attempt_scope_v3,
    validate_source_attempt_scope_v4,
)
from opendata.data.request_summary import RawEvidenceError, request_summary_sha256
from opendata.data.source_attempt_gate import (
    AttemptReservation,
    ReservationStatus,
    SourceAttemptGate,
    SourceAttemptScopeV3,
    TransportAttempt,
)

if TYPE_CHECKING:
    from opendata.data.providers.ths.transport.credentials import FuyaoCredentials
    from opendata.data.raw_response_cache import CachedRawResponse, RawResponseCache

logger = logging.getLogger(__name__)
_SENSITIVE_PARAMETER_PARTS = ("api_key", "apikey", "token", "secret", "password", "credential")
_FUYAO_ERROR_CATEGORIES = frozenset(
    {
        "request",
        "auth",
        "permission",
        "empty",
        "not_ready",
        "unsupported",
        "rate_limited",
        "transient",
        "network",
        "timeout",
        "response_too_large",
        "envelope_invalid",
        "http",
        "unknown",
    }
)
_FUYAO_TRANSPORT_ERROR_KEYS = frozenset(
    {"network", "timeout", "response_too_large", "envelope_invalid", "http", "unknown"}
)
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

# These fingerprints assume this module loads during trusted process startup.
# They detect later class/instance replacement, but are not a sandbox against
# same-process malicious code or code that patches dependencies before import.
_HTTPX_CLIENT_BUILD_REQUEST = httpx.Client.build_request
_HTTPX_CLIENT_SEND = httpx.Client.send
_SOURCE_ATTEMPT_GATE_RESERVE_ONE_SHOT = SourceAttemptGate.reserve_one_shot_attempt
_SOURCE_ATTEMPT_GATE_RESERVE_ATTEMPT = SourceAttemptGate._reserve_attempt

_V4_ALLOWED_REQUEST_HEADER_VALUES = {
    "accept": "*/*",
    "accept-encoding": "identity",
    "connection": "keep-alive",
    "user-agent": f"python-httpx/{httpx.__version__}",
}
_V4_ALLOWED_REQUEST_HEADER_NAMES = frozenset(
    {"accept", "accept-encoding", "connection", "host", "user-agent", API_KEY_HEADER.lower()}
)


@dataclass(frozen=True, slots=True)
class _PreparedRequestV3Identity:
    """Validated route and query identity for one prepared HTTPX request."""

    request_scheme: str
    request_method: str
    request_host: str
    request_path: str
    request_summary_sha256: str


def _prepared_request_v3_identity(
    request: httpx.Request,
    pin: SourceAttemptScopeV3Pin,
) -> _PreparedRequestV3Identity:
    """Check a V3 pin against exact URL routing and its prepared GET query.

    The version 1 summary covers only scalar query parameters. The HTTP Host
    authority is checked against the URL and caller pin, but other headers,
    transport delivery, and provider receipt are not bound. Host/path/method
    pins are caller assertions carried by trusted job code, not a source-policy
    registry or proof of authorization.
    """
    pin.validate_integrity()
    request_url = request.url
    try:
        route_parts = urlsplit(str(request_url))
        port = route_parts.port
        request_path = request_url.raw_path.partition(b"?")[0].decode("ascii", errors="strict")
    except (AttributeError, UnicodeError, ValueError):
        raise RequestAuthorizationError("prepared request route is invalid") from None
    request_host = request_url.host
    if (
        request.method != pin.expected_method
        or route_parts.scheme != "https"
        or route_parts.hostname != request_host
        or request_host != pin.expected_https_host
        or port not in (None, 443)
        or route_parts.username is not None
        or route_parts.password is not None
        or request_url.fragment
        or request_path != pin.expected_path
    ):
        raise RequestAuthorizationError("prepared request route differs from its V3 pins")
    try:
        host_headers = request.headers.get_list("host", split_commas=False)
    except Exception:
        raise RequestAuthorizationError("prepared request Host header is invalid") from None
    expected_host_header = pin.expected_https_host
    allowed_host_headers = {expected_host_header, f"{expected_host_header}:443"}
    if (
        len(host_headers) != 1
        or type(host_headers[0]) is not str
        or host_headers[0] not in allowed_host_headers
    ):
        raise RequestAuthorizationError("prepared request Host header differs from its V3 pins")
    try:
        body = request.content
    except Exception:
        raise RequestAuthorizationError("prepared request body is unavailable") from None
    if pin.expected_method != "GET" or body:
        raise RequestAuthorizationError("V3 scope requires a bodyless prepared GET request")
    try:
        query_items = tuple(request.url.params.multi_items())
    except Exception as exc:
        raise RequestAuthorizationError("prepared request query is invalid") from exc
    query_keys = tuple(key for key, _value in query_items)
    if (
        len(query_keys) != len(set(query_keys))
        or set(query_keys) != set(pin.request_summary_allowed_fields)
        or any(type(key) is not str or type(value) is not str for key, value in query_items)
    ):
        raise RequestAuthorizationError("prepared request query differs from its V3 allowlist")
    try:
        digest = request_summary_sha256(
            dict(query_items),
            allowed_fields=pin.request_summary_allowed_fields,
        )
    except (RawEvidenceError, TypeError, ValueError):
        raise RequestAuthorizationError("prepared request query summary is invalid") from None
    if digest != pin.request_summary_sha256:
        raise RequestAuthorizationError("prepared request query differs from its V3 summary pin")
    return _PreparedRequestV3Identity(
        request_scheme=route_parts.scheme,
        request_method=request.method,
        request_host=request_host,
        request_path=request_path,
        request_summary_sha256=digest,
    )


def _validate_v3_target_url(url: str, pin: SourceAttemptScopeV3Pin) -> None:
    """Reject URL input normalization or route drift before HTTPX builds it."""
    pin.validate_integrity()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise RequestAuthorizationError("V3 target URL is invalid") from None
    if (
        parts.scheme != "https"
        or parts.hostname != pin.expected_https_host
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "?" in url
        or "#" in url
        or parts.path != pin.expected_path
    ):
        raise RequestAuthorizationError("V3 target URL differs from its exact route pins")


def _validate_v4_request_headers(
    request: httpx.Request,
    *,
    pin: SourceAttemptScopeV3Pin,
    api_key: str,
) -> None:
    """Allow only the fixed HTTPX defaults, pinned Host, and Fuyao API key."""
    try:
        headers = tuple(request.headers.multi_items())
    except Exception:
        raise RequestAuthorizationError("prepared one-shot request headers are invalid") from None
    grouped: dict[str, list[str]] = {}
    for name, value in headers:
        if type(name) is not str or type(value) is not str:
            raise RequestAuthorizationError("prepared one-shot request headers are invalid")
        normalized_name = name.lower()
        if normalized_name not in _V4_ALLOWED_REQUEST_HEADER_NAMES:
            raise RequestAuthorizationError("prepared one-shot request has an unapproved header")
        grouped.setdefault(normalized_name, []).append(value)
    if any(len(values) != 1 for values in grouped.values()):
        raise RequestAuthorizationError("prepared one-shot request has duplicate headers")

    required_values = {
        "accept-encoding": _V4_ALLOWED_REQUEST_HEADER_VALUES["accept-encoding"],
        API_KEY_HEADER.lower(): api_key,
    }
    host_values = {pin.expected_https_host, f"{pin.expected_https_host}:443"}
    host_headers = grouped.get("host", [])
    if len(host_headers) != 1 or host_headers[0] not in host_values:
        raise RequestAuthorizationError("prepared one-shot Host header differs from its pin")
    for name, expected in required_values.items():
        if grouped.get(name) != [expected]:
            raise RequestAuthorizationError(
                "prepared one-shot request credentials or encoding differ"
            )
    for name, expected in _V4_ALLOWED_REQUEST_HEADER_VALUES.items():
        if name in grouped and grouped[name] != [expected]:
            raise RequestAuthorizationError("prepared one-shot request has an unapproved header")


def _validate_v4_gate_fingerprint(
    gate: SourceAttemptGate | None,
    expected_gate: SourceAttemptGate | None,
) -> None:
    """Reject replaced gate instances or V4 reservation method implementations.

    The fingerprint detects later runtime swaps. It cannot authenticate code
    patched before this module loads or contain malicious code in this process.
    """
    if gate is None or gate is not expected_gate or type(gate) is not SourceAttemptGate:
        raise RequestAuthorizationError("one-shot source attempt gate identity changed")
    try:
        instance_attributes = object.__getattribute__(gate, "__dict__")
        bound_method = object.__getattribute__(gate, "reserve_one_shot_attempt")
        storage_method = object.__getattribute__(gate, "_reserve_attempt")
    except Exception:
        raise RequestAuthorizationError("one-shot source attempt gate method is invalid") from None
    gate_class = type(gate)
    class_method = gate_class.__dict__.get("reserve_one_shot_attempt")
    class_storage_method = gate_class.__dict__.get("_reserve_attempt")
    storage_method_overridden = "_reserve_attempt" in instance_attributes
    if (
        "reserve_one_shot_attempt" in instance_attributes
        or class_method is not _SOURCE_ATTEMPT_GATE_RESERVE_ONE_SHOT
        or getattr(bound_method, "__self__", None) is not gate
        or getattr(bound_method, "__func__", None) is not _SOURCE_ATTEMPT_GATE_RESERVE_ONE_SHOT
        or class_storage_method is not _SOURCE_ATTEMPT_GATE_RESERVE_ATTEMPT
        or storage_method_overridden
        or (
            not storage_method_overridden
            and getattr(storage_method, "__func__", None)
            is not _SOURCE_ATTEMPT_GATE_RESERVE_ATTEMPT
        )
        or "__getattribute__" in gate_class.__dict__
        or "__getattr__" in gate_class.__dict__
    ):
        raise RequestAuthorizationError("one-shot source attempt gate method identity changed")


@dataclass(frozen=True)
class FuyaoTransportAttemptTrace:
    """Local-only observation for one reserved THS attempt on a successful call.

    This record describes what this client reserved and observed. It is not an
    upstream receipt and cannot prove that the source received or processed a
    request. Identifiers are local to the client/gate; source request IDs stay
    in their own field and are never substituted for local IDs. The record is
    exposed only when a request returns a successful response. Terminal errors
    have no trace carrier, and gate reservations alone do not prove a send or
    its outcome; this type is not a complete audit trail.
    """

    transport_request_id: str
    attempt_id: str
    attempt_no: int
    reserved_at_us: int | None
    gate_status: str
    request_budget_reserved: bool
    http_status: int | None
    outcome: Literal["response_observed", "outcome_unknown"]
    failure_category: str | None = None
    source_request_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class FuyaoHttpResponse:
    """一次成功请求的传输层结果.

    Attributes:
        status_code: HTTP 状态码。
        envelope: 解析后的信封。
        elapsed_seconds: 耗时。
        raw_content: 本模块在体积上限内读取的实体 body bytes，而非线上 wire bytes。
            随成功结果返回；响应体不进入日志，只有显式启用的原始响应缓存会持久化它。
            底层传输可能暂存单个较大的 chunk，解析/复制也可能产生临时副本，因此该上限
            不是 wire 或 RSS 上限。
        attempt_trace: 仅随成功响应返回的本地 request/attempt 预留及观察记录，不是上游
            回执，也不是完整审计轨迹。终止失败从原异常中无法取得 trace；gate reservation
            只证明本地预留。不要对整个响应对象执行通用序列化，因为响应还包含原始实体和解析数据。
    """

    status_code: int
    envelope: FuyaoEnvelope
    elapsed_seconds: float
    raw_content: bytes = field(default=b"", repr=False, compare=False)
    attempt_trace: tuple[FuyaoTransportAttemptTrace, ...] = field(
        default=(), repr=False, compare=False
    )


@dataclass(frozen=True)
class FuyaoRawExchangeResponse:
    """One bounded Fuyao HTTP exchange without envelope interpretation.

    The body is the complete HTTP entity body observed within the configured
    response cap. It is not the wire representation or a process-memory cap.
    Response headers are intentionally omitted so callers cannot accidentally
    persist credentials or other transport metadata with the body. A source can
    still echo request credentials inside the entity body; callers must check
    for credential reflection before persisting or serializing this response.
    """

    status_code: int
    body: bytes = field(repr=False, compare=False)
    elapsed_seconds: float
    attempt_trace: tuple[FuyaoTransportAttemptTrace, ...] = field(
        default=(), repr=False, compare=False
    )


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
        source_attempt_gate: SourceAttemptGate | None = None,
    ) -> None:
        """构造客户端.

        Args:
            credentials: 凭据。
            client: 注入的标准 httpx.Client（测试用 MockTransport）。非 scoped 调用会拒绝
                auth、mounts 和 response hooks 并关闭重定向。注入 transport 仍是可信边界，
                不得隐藏重试，也不得在 ``send(stream=True)`` 返回前读取或缓存完整 body。
                ``None`` 时使用直连。
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
            source_attempt_gate: 显式的 SQLite 持久发送门禁。它只接受来自可信
                ``RequestExecutionScope`` 的绑定，不创建授权或默认数据库。
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        if client is not None and mock_transport is not None:
            raise ValueError("client and mock_transport are mutually exclusive")
        if mock_transport is not None and type(mock_transport) is not httpx.MockTransport:
            raise ValueError("mock_transport must be an exact httpx.MockTransport")
        if source_attempt_gate is not None and type(source_attempt_gate) is not SourceAttemptGate:
            raise TypeError("source_attempt_gate must be a SourceAttemptGate or None")
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
        self._scoped_transport = (
            self._client._transport if self._scoped_transport_verified else None
        )
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
        self._source_attempt_gate = source_attempt_gate
        self._source_attempt_gate_identity = source_attempt_gate
        if source_attempt_gate is not None:
            self._validate_v4_gate_integrity()
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
        self._validate_source_attempt_mode(scopes)
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

    def get_raw_exchange(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> FuyaoRawExchangeResponse:
        """Return one bounded GET response without parsing or caching its body.

        This opt-in transport seam makes exactly one ``Client.send`` call,
        never follows redirects, and returns the HTTP status and complete
        entity body for every status code. The existing :meth:`get` behavior
        remains unchanged. Active request scopes still authorize the host,
        reserve one request-budget attempt, and use the optional durable source
        attempt gate. The Fuyao host limiter is consumed once for every call.
        The body is returned unchanged and may contain an echoed API key, so a
        caller must check for credential reflection before persisting or
        serializing it.

        Args:
            endpoint: A path beginning with ``/``; query and fragment syntax
                must be supplied through ``params``.
            params: Optional request query parameters.

        Returns:
            The observed status code and bounded response entity body.

        Raises:
            ValueError: If ``endpoint`` is not a path-only absolute URL part.
            RequestBudgetError: If no active scope exists or it does not authorize a send.
            FuyaoError: If local rate governance, timeout, transport, or the
                configured body cap prevents observing the complete response.
                No partial or synthetic body is returned on these failures.
        """
        if not endpoint.startswith("/") or "?" in endpoint or "#" in endpoint:
            raise ValueError("endpoint must be a path beginning with '/' and no query or fragment")
        scopes = current_request_scopes()
        if not scopes:
            raise RequestAuthorizationError("raw exchange requires an active request scope")
        self._validate_source_attempt_mode(scopes)
        while not self._request_lock.acquire(timeout=0.05):
            check_scope_liveness(scopes)
        try:
            check_scope_liveness(scopes)
            url = f"{self.credentials.base_url}{endpoint}"
            host = self._scoped_host(url)
            authorize_request(scopes, "ths", host)
            self._validate_scoped_client()
            self._validate_raw_exchange_client()
            return self._raw_exchange_locked(endpoint, params=params, scopes=scopes)
        finally:
            self._request_lock.release()

    def _raw_exchange_locked(
        self,
        endpoint: str,
        *,
        params: Mapping[str, Any] | None,
        scopes: tuple[RequestExecutionScope, ...],
    ) -> FuyaoRawExchangeResponse:
        """Execute one transport attempt while holding the client-state lock."""
        self._validate_source_attempt_mode(scopes)
        if not scopes:
            raise RequestAuthorizationError("raw exchange requires an active request scope")
        url = f"{self.credentials.base_url}{endpoint}"
        if self._source_attempt_scope_version(scopes) == SOURCE_ATTEMPT_SCOPE_V4:
            _binding, _pin, locked_scope, _model, _operation = validate_source_attempt_scope_v4(
                scopes
            )
            request_id = locked_scope.attempt.request_id
        else:
            request_id = uuid.uuid4().hex
        started = time.monotonic()
        status: int | None = None
        gate_reservation: tuple[str, AttemptReservation] | None = None
        send_started = False
        response: httpx.Response | None = None
        body: bytes | None = None

        try:
            scope_version = self._source_attempt_scope_version(scopes)
            if scope_version == SOURCE_ATTEMPT_SCOPE_V3:
                _binding, pin, _canonical_model, _operation = validate_source_attempt_scope_v3(
                    scopes
                )
                _validate_v3_target_url(url, pin)
            elif scope_version == SOURCE_ATTEMPT_SCOPE_V4:
                _binding, pin, _locked_scope, _model, _operation = validate_source_attempt_scope_v4(
                    scopes
                )
                _validate_v3_target_url(url, pin)
            self._validate_raw_exchange_client()
            self._limiter.wait_and_acquire(self._scoped_sleep(scopes))
            check_scope_liveness(scopes)

            request_host = self._scoped_host(url)
            check_scope_liveness(scopes)
            authorize_request(scopes, "ths", request_host)
            self._validate_scoped_client()
            self._validate_raw_exchange_client()
            if self._source_attempt_scope_version(scopes) in (
                SOURCE_ATTEMPT_SCOPE_V3,
                SOURCE_ATTEMPT_SCOPE_V4,
            ):
                self._validate_v3_httpx_methods()
            request = self._client.build_request(
                "GET",
                url,
                params=dict(params or {}),
                headers={
                    API_KEY_HEADER: self.credentials.api_key,
                    "Accept-Encoding": "identity",
                },
                timeout=self._scoped_timeout(scopes),
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
                self._validate_raw_exchange_client()
                request.extensions["timeout"] = httpx.Timeout(
                    self._scoped_timeout(scopes)
                ).as_dict()
                self._validate_v3_prepared_request(scopes, request)
                reserve_scoped_attempt(scopes, "ths", request_host)
                if self._source_attempt_gate is not None:
                    scope_version = self._source_attempt_scope_version(scopes)
                    if scope_version == SOURCE_ATTEMPT_SCOPE_V4:
                        gate_reservation = self._reserve_one_shot_source_attempt(
                            scopes,
                            host=request_host,
                            prepared_request=request,
                        )
                    elif scope_version == SOURCE_ATTEMPT_SCOPE_V3:
                        gate_reservation = self._reserve_source_attempt(
                            scopes,
                            host=request_host,
                            request_id=request_id,
                            attempt_no=1,
                            prepared_request=request,
                        )
                    else:
                        gate_reservation = self._reserve_source_attempt(
                            scopes,
                            host=request_host,
                            request_id=request_id,
                            attempt_no=1,
                        )
                    check_scope_liveness(scopes)
                    authorize_request(scopes, "ths", request_host)
                    self._validate_scoped_client(require_attempt_gate=True)
                    self._validate_raw_exchange_client()
                    request.extensions["timeout"] = httpx.Timeout(
                        self._scoped_timeout(scopes)
                    ).as_dict()
                    self._validate_v3_prepared_request(scopes, request)
                send_started = True
                response = self._client.send(
                    request,
                    follow_redirects=False,
                    stream=True,
                )
                with _close_response(response):
                    status = response.status_code
                    if status == 429:
                        self._limiter.record_rate_limit(retry_after_seconds=_retry_after(response))
                    check_scope_liveness(scopes)
                    body = self._read_bounded_response(response, scopes, include_all_statuses=True)
                    check_scope_liveness(scopes)
        except RequestBudgetError:
            raise
        except httpx.TimeoutException:
            failure = error_for_transport("timeout")
            failure.retryable = False
            self._log_request_event(
                url=url,
                params=params,
                request_id=request_id,
                attempt=1,
                started=started,
                status=status,
                failure_category=failure.category,
            )
            raise failure from None
        except httpx.HTTPError:
            failure = error_for_transport("network")
            failure.retryable = False
            self._log_request_event(
                url=url,
                params=params,
                request_id=request_id,
                attempt=1,
                started=started,
                status=status,
                failure_category=failure.category,
            )
            raise failure from None
        except FuyaoError as exc:
            category = exc.category if exc.category in _FUYAO_ERROR_CATEGORIES else "unknown"
            if category in _FUYAO_TRANSPORT_ERROR_KEYS:
                failure = error_for_transport(category)
            else:
                safe_template = error_for_transport("unknown")
                failure = FuyaoError(
                    f"FUYAO_{category.upper()}",
                    category=category,
                    message=safe_template.message,
                    advice=safe_template.advice,
                    retryable=exc.retryable is True,
                )
            failure.retryable = exc.retryable is True
            self._log_request_event(
                url=url,
                params=params,
                request_id=request_id,
                attempt=1,
                started=started,
                status=status,
                failure_category=failure.category,
            )
            raise failure from None
        except Exception:
            # Do not propagate arbitrary injected transport messages: they can
            # contain request URLs, credentials, response fragments, or body data.
            failure = error_for_transport("network")
            failure.retryable = False
            self._log_request_event(
                url=url,
                params=params,
                request_id=request_id,
                attempt=1,
                started=started,
                status=status,
                failure_category=failure.category,
            )
            raise failure from None

        if response is None or status is None or body is None:
            raise error_for_transport("unknown")
        elapsed = time.monotonic() - started
        trace: tuple[FuyaoTransportAttemptTrace, ...] = ()
        if gate_reservation is not None and send_started:
            attempt_id, reservation = gate_reservation
            trace = (
                _make_attempt_trace(
                    transport_request_id=request_id,
                    attempt_id=attempt_id,
                    attempt_no=1,
                    reservation=reservation,
                    http_status=status,
                    failure_category=None,
                    source_request_id=None,
                ),
            )
        self._log_request_event(
            url=url,
            params=params,
            request_id=request_id,
            attempt=1,
            started=started,
            status=status,
            failure_category=None,
        )
        return FuyaoRawExchangeResponse(
            status_code=status,
            body=body,
            elapsed_seconds=elapsed,
            attempt_trace=trace,
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
        self._validate_source_attempt_mode(scopes)
        last_error = error_for_transport("unknown")
        if self._source_attempt_scope_version(scopes) == SOURCE_ATTEMPT_SCOPE_V4:
            _binding, _pin, locked_scope, _model, _operation = validate_source_attempt_scope_v4(
                scopes
            )
            request_id = locked_scope.attempt.request_id
        else:
            request_id = uuid.uuid4().hex
        url = f"{self.credentials.base_url}{endpoint}"
        scope_version = self._source_attempt_scope_version(scopes)
        if scope_version == SOURCE_ATTEMPT_SCOPE_V3:
            _binding, pin, _canonical_model, _operation = validate_source_attempt_scope_v3(scopes)
            _validate_v3_target_url(url, pin)
        elif scope_version == SOURCE_ATTEMPT_SCOPE_V4:
            _binding, pin, _locked_scope, _canonical_model, _operation = (
                validate_source_attempt_scope_v4(scopes)
            )
            _validate_v3_target_url(url, pin)
        gate_enabled = self._source_attempt_gate is not None
        raw_cache = None if gate_enabled else self._raw_response_cache_override
        if raw_cache is None and not gate_enabled:
            raw_cache = _get_configured_raw_response_cache()
        cache_headers = {
            API_KEY_HEADER: self.credentials.api_key,
            "Accept-Encoding": "identity",
        }
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
                    cached_response = _restore_cached_httpx_response(url, cached)
                    result = self._handle_response(cached_response, time.monotonic() - started)
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
        attempt_traces: list[FuyaoTransportAttemptTrace] = []

        def record_attempt_trace(
            reservation: tuple[str, AttemptReservation] | None,
            *,
            send_started: bool,
            already_recorded: bool,
            attempt_no: int,
            http_status: int | None,
            failure_category: str | None,
            source_request_id: object = None,
        ) -> bool:
            """Append one local gated attempt observation and report if recorded."""
            if reservation is None or not send_started or already_recorded:
                return already_recorded
            attempt_id, gate_reservation = reservation
            attempt_traces.append(
                _make_attempt_trace(
                    transport_request_id=request_id,
                    attempt_id=attempt_id,
                    attempt_no=attempt_no,
                    reservation=gate_reservation,
                    http_status=http_status,
                    failure_category=failure_category,
                    source_request_id=source_request_id,
                )
            )
            return True

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
            gate_reservation: tuple[str, AttemptReservation] | None = None
            send_started = False
            trace_recorded = False
            response: httpx.Response | None = None
            response_content = b""
            rate_limit_recorded = False

            try:
                if scopes:
                    request_host = self._scoped_host(url)
                    check_scope_liveness(scopes)
                    authorize_request(scopes, "ths", request_host)
                    self._validate_scoped_client()
                    timeout = self._scoped_timeout(scopes)
                    if self._source_attempt_scope_version(scopes) in (
                        SOURCE_ATTEMPT_SCOPE_V3,
                        SOURCE_ATTEMPT_SCOPE_V4,
                    ):
                        self._validate_v3_httpx_methods()
                    prepared_request = self._client.build_request(
                        "GET",
                        url,
                        params=dict(params or {}),
                        headers={
                            API_KEY_HEADER: self.credentials.api_key,
                            "Accept-Encoding": "identity",
                        },
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
                        self._validate_v3_prepared_request(scopes, prepared_request)
                        reserve_scoped_attempt(scopes, "ths", request_host)
                        if gate_enabled:
                            scope_version = self._source_attempt_scope_version(scopes)
                            if scope_version == SOURCE_ATTEMPT_SCOPE_V4:
                                gate_reservation = self._reserve_one_shot_source_attempt(
                                    scopes,
                                    host=request_host,
                                    prepared_request=prepared_request,
                                )
                            elif scope_version == SOURCE_ATTEMPT_SCOPE_V3:
                                gate_reservation = self._reserve_source_attempt(
                                    scopes,
                                    host=request_host,
                                    request_id=request_id,
                                    attempt_no=attempt,
                                    prepared_request=prepared_request,
                                )
                            else:
                                gate_reservation = self._reserve_source_attempt(
                                    scopes,
                                    host=request_host,
                                    request_id=request_id,
                                    attempt_no=attempt,
                                )
                            check_scope_liveness(scopes)
                            authorize_request(scopes, "ths", request_host)
                            # Recheck the complete client configuration after
                            # the durable reservation, immediately before send.
                            # The reservation boundary may invoke storage code;
                            # mutable httpx state must not change in that gap.
                            self._validate_scoped_client(require_attempt_gate=gate_enabled)
                            timeout = self._scoped_timeout(scopes)
                            prepared_request.extensions["timeout"] = httpx.Timeout(
                                timeout
                            ).as_dict()
                            self._validate_v3_prepared_request(scopes, prepared_request)
                        send_started = True
                        response = self._client.send(
                            prepared_request,
                            follow_redirects=False,
                            stream=True,
                        )
                        with _close_response(response):
                            response_status = response.status_code
                            if response_status == 429:
                                self._limiter.record_rate_limit(
                                    retry_after_seconds=_retry_after(response)
                                )
                                rate_limit_recorded = True
                            check_scope_liveness(scopes)
                            if 300 <= response_status < 400:
                                raise RequestRedirectError("scoped redirects are not allowed")
                            response_content = self._read_bounded_response(response, scopes)
                            check_scope_liveness(scopes)
                else:
                    with shared_host_request(self._concurrency_host):
                        self._validate_unscoped_client()
                        prepared_request = self._client.build_request(
                            "GET",
                            url,
                            params=dict(params or {}),
                            headers={
                                API_KEY_HEADER: self.credentials.api_key,
                                "Accept-Encoding": "identity",
                            },
                            timeout=self._timeout_seconds,
                        )
                        send_started = True
                        response = self._client.send(
                            prepared_request,
                            follow_redirects=False,
                            stream=True,
                        )
                        with _close_response(response):
                            response_status = response.status_code
                            if response_status == 429:
                                self._limiter.record_rate_limit(
                                    retry_after_seconds=_retry_after(response)
                                )
                                rate_limit_recorded = True
                            response_content = self._read_bounded_response(response, ())
            except RequestBudgetError:
                raise
            except httpx.TimeoutException:
                last_error = error_for_transport("timeout")
                if response is not None:
                    # Headers arrived, so replay could duplicate an upstream action.
                    last_error.retryable = False
            except httpx.HTTPError as exc:
                last_error = error_for_transport("network", detail=exc.__class__.__name__)
                if response is not None:
                    # A body-read failure leaves the response outcome ambiguous.
                    last_error.retryable = False
            except FuyaoError as exc:
                last_error = exc
            else:
                response_status = response.status_code
                elapsed = time.monotonic() - started
                try:
                    result = self._handle_response(
                        response,
                        elapsed,
                        content=response_content,
                        rate_limit_recorded=rate_limit_recorded,
                    )
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
                    trace_recorded = record_attempt_trace(
                        gate_reservation,
                        send_started=send_started,
                        already_recorded=trace_recorded,
                        attempt_no=attempt,
                        http_status=response_status,
                        failure_category=exc.category,
                        source_request_id=exc.request_id,
                    )
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
                            content=result.raw_content,
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
                    trace_recorded = record_attempt_trace(
                        gate_reservation,
                        send_started=send_started,
                        already_recorded=trace_recorded,
                        attempt_no=attempt,
                        http_status=response_status,
                        failure_category=None,
                        source_request_id=result.envelope.request_id,
                    )
                    if attempt_traces:
                        result = FuyaoHttpResponse(
                            status_code=result.status_code,
                            envelope=result.envelope,
                            elapsed_seconds=result.elapsed_seconds,
                            raw_content=result.raw_content,
                            attempt_trace=tuple(attempt_traces),
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
                    failure_category=last_error.category,
                )
            record_attempt_trace(
                gate_reservation,
                send_started=send_started,
                already_recorded=trace_recorded,
                attempt_no=attempt,
                http_status=response_status,
                failure_category=last_error.category,
                source_request_id=last_error.request_id,
            )
            if not last_error.retryable:
                raise last_error
            if attempt >= self._max_attempts:
                break
            if scopes:
                self._scoped_sleep(scopes)(self._backoff_seconds(attempt))
            else:
                self._sleep(self._backoff_seconds(attempt))
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

    def _validate_scoped_client(self, *, require_attempt_gate: bool = False) -> None:
        """Fail closed on opaque clients or callbacks that can add sends."""
        if not self._scoped_transport_verified:
            raise RequestAuthorizationError("scoped request transport is not verified")
        if type(self._client) is not httpx.Client:
            raise RequestAuthorizationError("scoped request client type is not verified")
        if self._client.is_closed:
            raise RequestAuthorizationError("scoped request client is closed")
        if self._client._transport is not self._scoped_transport:
            raise RequestAuthorizationError("scoped request transport has changed")
        if self._client.auth is not None:
            raise RequestAuthorizationError("scoped client authentication is not allowed")
        if self._client.trust_env or self._client.follow_redirects or self._client._mounts:
            raise RequestAuthorizationError("scoped client routing is not allowed")
        hooks = self._client.event_hooks
        if any(hooks.values()):
            raise RequestAuthorizationError("scoped client event hooks are not allowed")
        if self._source_attempt_gate is not None or require_attempt_gate:
            self._validate_attempt_gated_client()

    def _validate_v3_httpx_methods(self) -> None:
        """Reject changed HTTPX request/send methods for explicit V3 scopes.

        Original descriptors were captured when this module loaded. This catches
        later class- and instance-level replacement; it assumes trusted process
        startup and does not protect against code that patched HTTPX before import.
        """
        if (
            httpx.Client.build_request is not _HTTPX_CLIENT_BUILD_REQUEST
            or httpx.Client.send is not _HTTPX_CLIENT_SEND
        ):
            raise RequestAuthorizationError("source attempt V3 HTTPX methods are not verified")
        namespace = getattr(self._client, "__dict__", {})
        if any(name in namespace for name in ("build_request", "send")):
            raise RequestAuthorizationError(
                "source attempt V3 client methods cannot be overridden on the instance"
            )
        if any(
            getattr(getattr(self._client, name, None), "__func__", None) is not expected
            for name, expected in (
                ("build_request", _HTTPX_CLIENT_BUILD_REQUEST),
                ("send", _HTTPX_CLIENT_SEND),
            )
        ):
            raise RequestAuthorizationError("source attempt V3 client methods are not verified")

    def _validate_unscoped_client(self) -> None:
        """Reject HTTPX features that can add hidden sends or body reads."""
        if type(self._client) is not httpx.Client:
            raise RequestAuthorizationError("bounded request client type is not verified")
        if self._client.is_closed:
            raise RequestAuthorizationError("bounded request client is closed")
        if self._client.auth is not None:
            raise RequestAuthorizationError("bounded client authentication is not allowed")
        if self._client._mounts:
            raise RequestAuthorizationError("bounded client routing is not allowed")
        if any(self._client.event_hooks.values()):
            raise RequestAuthorizationError("response hooks are not allowed for bounded transport")

    def _validate_raw_exchange_client(self) -> None:
        """Fail closed unless this client can make one direct, verified send."""
        self._validate_unscoped_client()
        if self._client.trust_env:
            raise RequestAuthorizationError(
                "raw exchange client environment routing is not allowed"
            )
        transport = self._client._transport
        if type(transport) is httpx.MockTransport:
            # Exact MockTransport is the explicit offline testing seam.
            return
        if type(transport) is not httpx.HTTPTransport:
            raise RequestAuthorizationError("raw exchange transport is not verified")
        pool = getattr(transport, "_pool", None)
        ssl_context = getattr(pool, "_ssl_context", None)
        if (
            pool is None
            or getattr(pool, "_retries", None) != 0
            or getattr(pool, "_proxy", None) is not None
            or ssl_context is None
            or getattr(ssl_context, "verify_mode", None) != ssl.CERT_REQUIRED
            or getattr(ssl_context, "check_hostname", None) is not True
        ):
            raise RequestAuthorizationError("raw exchange direct TLS transport is not verified")

    def _validate_attempt_gated_client(self) -> None:
        """Verify the gate-bound client cannot proxy, retry, or skip TLS checks."""
        transport = self._scoped_transport
        if transport is None or self._client._transport is not transport:
            raise RequestAuthorizationError("source attempt transport is not verified")
        if type(transport) is httpx.MockTransport:
            # MockTransport is an explicit offline-only constructor seam.
            return
        if type(transport) is not httpx.HTTPTransport:
            raise RequestAuthorizationError("source attempt transport is not verified")
        pool = getattr(transport, "_pool", None)
        ssl_context = getattr(pool, "_ssl_context", None)
        if (
            pool is None
            or getattr(pool, "_retries", None) != 0
            or getattr(pool, "_proxy", None) is not None
            or ssl_context is None
            or getattr(ssl_context, "verify_mode", None) != ssl.CERT_REQUIRED
            or getattr(ssl_context, "check_hostname", None) is not True
        ):
            raise RequestAuthorizationError("source attempt TLS transport is not verified")

    def _validate_source_attempt_mode(self, scopes: tuple[RequestExecutionScope, ...]) -> None:
        """Require the explicitly selected gate version and its complete binding."""
        scope_version = self._source_attempt_scope_version(scopes)
        if self._source_attempt_gate is None:
            if scope_version in (SOURCE_ATTEMPT_SCOPE_V3, SOURCE_ATTEMPT_SCOPE_V4) or any(
                scope.source_attempt_binding is not None for scope in scopes
            ):
                raise RequestAuthorizationError(
                    "source attempt binding requires a configured durable gate"
                )
            return
        if scope_version == SOURCE_ATTEMPT_SCOPE_V4:
            self._validate_v3_httpx_methods()
            self._validate_v4_gate_integrity()
            binding, _pin, locked_scope, source_model, operation = validate_source_attempt_scope_v4(
                scopes
            )
            self._validate_v4_transport_limits()
            if (
                scopes[0].source != "ths"
                or not source_model
                or locked_scope.attempt.attempt_no != 1
                or locked_scope.attempt.request_id == ""
                or locked_scope.attempt.attempt_id == ""
                or locked_scope.attempt.operation != operation.value
                or locked_scope.attempt.grant_id != binding.grant_id
            ):
                raise RequestAuthorizationError(
                    "source attempt V4 requires one exact pre-locked attempt"
                )
            return
        if scope_version == SOURCE_ATTEMPT_SCOPE_V3:
            self._validate_v3_httpx_methods()
            binding, _pin, source_model, _operation = validate_source_attempt_scope_v3(scopes)
            if scopes[0].source != "ths" or not source_model or binding.run_key is None:
                raise RequestAuthorizationError("source attempt V3 scope is invalid for THS")
            return
        self._validated_source_attempt_scope(scopes)

    @staticmethod
    def _source_attempt_scope_version(
        scopes: tuple[RequestExecutionScope, ...],
    ) -> int:
        """Return one exact version discriminator or reject an ambiguous stack."""
        if not scopes:
            return SOURCE_ATTEMPT_SCOPE_V1
        versions: list[int] = []
        for scope in scopes:
            value = getattr(scope, "source_attempt_scope_version", SOURCE_ATTEMPT_SCOPE_V1)
            if type(value) is not int or value not in (
                SOURCE_ATTEMPT_SCOPE_V1,
                SOURCE_ATTEMPT_SCOPE_V3,
                SOURCE_ATTEMPT_SCOPE_V4,
            ):
                raise RequestAuthorizationError("source attempt scope version is invalid")
            versions.append(value)
        if any(version != versions[0] for version in versions):
            raise RequestAuthorizationError("source attempt scope versions are inconsistent")
        version = versions[0]
        if version == SOURCE_ATTEMPT_SCOPE_V1 and any(
            getattr(scope, "source_attempt_scope_v3_pin", None) is not None
            or getattr(scope, "source_attempt_one_shot_scope_v4", None) is not None
            for scope in scopes
        ):
            raise RequestAuthorizationError("V3 pin cannot be used with the legacy gate version")
        if version == SOURCE_ATTEMPT_SCOPE_V3:
            if any(
                getattr(scope, "source_attempt_one_shot_scope_v4", None) is not None
                for scope in scopes
            ):
                raise RequestAuthorizationError("V4 scope cannot be used with the V3 gate version")
            validate_source_attempt_scope_v3(scopes)
        elif version == SOURCE_ATTEMPT_SCOPE_V4:
            validate_source_attempt_scope_v4(scopes)
        return version

    def _validate_v3_prepared_request(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        request: httpx.Request,
    ) -> None:
        """Recheck V3/V4 pins against the same request immediately before send."""
        version = self._source_attempt_scope_version(scopes)
        if version not in (SOURCE_ATTEMPT_SCOPE_V3, SOURCE_ATTEMPT_SCOPE_V4):
            return
        self._validate_v3_httpx_methods()
        if version == SOURCE_ATTEMPT_SCOPE_V3:
            _binding, pin, _canonical_model, _operation = validate_source_attempt_scope_v3(scopes)
        else:
            _binding, pin, _locked_scope, _canonical_model, _operation = (
                validate_source_attempt_scope_v4(scopes)
            )
            self._validate_v4_gate_integrity()
            self._validate_v4_transport_limits()
            _validate_v4_request_headers(request, pin=pin, api_key=self.credentials.api_key)
        _prepared_request_v3_identity(request, pin)

    def _validate_v4_gate_integrity(self) -> None:
        """Check the pinned gate and its storage entrypoint for this transport."""
        _validate_v4_gate_fingerprint(
            self._source_attempt_gate,
            self._source_attempt_gate_identity,
        )

    def _validate_v4_transport_limits(self) -> None:
        """Keep a V4 request to one bounded attempt with a finite timeout."""
        if (
            type(self._max_attempts) is not int
            or self._max_attempts != 1
            or type(self._timeout_seconds) not in (int, float)
            or not math.isfinite(self._timeout_seconds)
            or self._timeout_seconds <= 0
        ):
            raise RequestAuthorizationError(
                "one-shot transport requires max_attempts=1 and a finite positive timeout"
            )

    @staticmethod
    def _validated_source_attempt_scope(
        scopes: tuple[RequestExecutionScope, ...],
    ) -> tuple[SourceAttemptBinding, str, RequestOperation]:
        """Return one exact binding after checking the entire trusted scope stack."""
        if not scopes:
            raise RequestAuthorizationError(
                "source attempt gate requires consistent trusted request scopes"
            )
        first = scopes[0]
        binding = first.source_attempt_binding
        canonical_model = first.canonical_model
        operation = first.operation
        if (
            type(first.source) is not str
            or first.source != "ths"
            or type(canonical_model) is not str
            or not canonical_model
            or canonical_model != canonical_model.strip()
            or type(operation) is not RequestOperation
            or type(binding) is not SourceAttemptBinding
        ):
            raise RequestAuthorizationError(
                "source attempt gate requires consistent trusted request scopes"
            )
        for scope in scopes:
            if (
                scope.source != first.source
                or scope.canonical_model != canonical_model
                or scope.operation is not operation
                or type(scope.source_attempt_binding) is not SourceAttemptBinding
                or scope.source_attempt_binding != binding
            ):
                raise RequestAuthorizationError(
                    "source attempt gate requires consistent trusted request scopes"
                )
        return binding, canonical_model, operation

    def _reserve_source_attempt(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        *,
        host: str,
        request_id: str,
        attempt_no: int,
        prepared_request: httpx.Request | None = None,
    ) -> tuple[str, AttemptReservation]:
        """Persist and return the local one-use reservation immediately before send."""
        gate = self._source_attempt_gate
        if gate is None:
            raise RequestAuthorizationError("source attempt gate is not configured")
        scope_version = self._source_attempt_scope_version(scopes)
        if scope_version == SOURCE_ATTEMPT_SCOPE_V4:
            raise RequestAuthorizationError(
                "V4 one-shot requests require the dedicated one-shot gate method"
            )
        if scope_version == SOURCE_ATTEMPT_SCOPE_V3:
            try:
                if prepared_request is None:
                    raise RequestAuthorizationError(
                        "source attempt V3 requires the prepared request"
                    )
                binding, pin, canonical_model, operation = validate_source_attempt_scope_v3(scopes)
                request_identity = _prepared_request_v3_identity(prepared_request, pin)
                if host != request_identity.request_host:
                    raise RequestAuthorizationError(
                        "V3 prepared host differs from the reserved attempt host"
                    )
                attempt = TransportAttempt(
                    attempt_id=uuid.uuid4().hex,
                    request_id=request_id,
                    attempt_no=attempt_no,
                    grant_id=binding.grant_id,
                    expected_revision=binding.expected_revision,
                    source="ths",
                    canonical_model=canonical_model,
                    operation=operation.value,
                    principal_scope=binding.principal_scope,
                    host=request_identity.request_host,
                    run_key=binding.run_key,
                    expected_rights_evidence_reference=(binding.expected_rights_evidence_reference),
                )
                v3_scope = SourceAttemptScopeV3(
                    attempt=attempt,
                    request_scheme=request_identity.request_scheme,
                    request_method=request_identity.request_method,
                    request_host=request_identity.request_host,
                    request_path=request_identity.request_path,
                    capture_intent_id=pin.capture_intent_id,
                    request_summary_sha256=request_identity.request_summary_sha256,
                    source_policy_id=pin.source_policy_id,
                    source_policy_revision=pin.source_policy_revision,
                    source_policy_sha256=pin.source_policy_sha256,
                    budget_binding_sha256=pin.budget_binding_sha256,
                    budget_quota_key=pin.budget_quota_key,
                    budget_policy_revision=pin.budget_policy_revision,
                    budget_policy_sha256=pin.budget_policy_sha256,
                )
                reservation = gate.reserve_attempt_v3(v3_scope)
            except Exception:
                raise RequestAuthorizationError(
                    "source attempt V3 gate did not authorize transport"
                ) from None
            if (
                type(reservation) is not AttemptReservation
                or reservation.status is not ReservationStatus.RESERVED_NEW
            ):
                raise RequestAuthorizationError(
                    "source attempt V3 gate did not authorize transport"
                )
            return attempt.attempt_id, reservation
        try:
            binding, canonical_model, operation = self._validated_source_attempt_scope(scopes)
            attempt = TransportAttempt(
                attempt_id=uuid.uuid4().hex,
                request_id=request_id,
                attempt_no=attempt_no,
                grant_id=binding.grant_id,
                expected_revision=binding.expected_revision,
                source="ths",
                canonical_model=canonical_model,
                operation=operation.value,
                principal_scope=binding.principal_scope,
                host=host,
                run_key=binding.run_key,
                expected_rights_evidence_reference=(binding.expected_rights_evidence_reference),
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
        return attempt.attempt_id, reservation

    def _reserve_one_shot_source_attempt(
        self,
        scopes: tuple[RequestExecutionScope, ...],
        *,
        host: str,
        prepared_request: httpx.Request | None,
    ) -> tuple[str, AttemptReservation]:
        """Consume the exact locked V4 identity; never create replacement IDs."""
        gate = self._source_attempt_gate
        self._validate_v4_gate_integrity()
        try:
            if prepared_request is None:
                raise RequestAuthorizationError(
                    "one-shot source attempt requires the prepared request"
                )
            binding, pin, locked_scope, _canonical_model, _operation = (
                validate_source_attempt_scope_v4(scopes)
            )
            identity = _prepared_request_v3_identity(prepared_request, pin)
            attempt = locked_scope.attempt
            if (
                host != identity.request_host
                or identity.request_scheme != locked_scope.request_scheme
                or identity.request_method != locked_scope.request_method
                or identity.request_host != locked_scope.request_host
                or identity.request_path != locked_scope.request_path
                or identity.request_summary_sha256 != locked_scope.request_summary_sha256
                or attempt.attempt_no != 1
                or attempt.source != "ths"
                or attempt.host != host
                or attempt.grant_id != binding.grant_id
                or attempt.expected_revision != binding.expected_revision
                or attempt.principal_scope != binding.principal_scope
                or attempt.run_key != binding.run_key
                or attempt.expected_rights_evidence_reference
                != binding.expected_rights_evidence_reference
            ):
                raise RequestAuthorizationError("prepared request differs from locked V4 scope")
            self._validate_v4_gate_integrity()
            reservation = _SOURCE_ATTEMPT_GATE_RESERVE_ONE_SHOT(gate, locked_scope)
            self._validate_v4_gate_integrity()
        except Exception:
            raise RequestAuthorizationError(
                "one-shot source attempt gate did not authorize transport"
            ) from None
        if (
            type(reservation) is not AttemptReservation
            or reservation.status is not ReservationStatus.RESERVED_NEW
        ):
            raise RequestAuthorizationError(
                "one-shot source attempt gate did not authorize transport"
            )
        return locked_scope.attempt.attempt_id, reservation

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

    def _read_bounded_response(
        self,
        response: httpx.Response,
        scopes: tuple[RequestExecutionScope, ...],
        *,
        include_all_statuses: bool = False,
    ) -> bytes:
        """Read an identity-encoded body while enforcing its entity cap.

        Liveness is checked around every raw chunk yielded by HTTPX. Socket reads
        remain subject to the configured inactivity timeout; a slow trickle can
        cross a scope's total deadline before the next yielded chunk, so this is
        not a hard wall-clock deadline guarantee. The bytearray accumulated here
        never exceeds the configured cap. HTTPX or an injected transport may
        buffer a larger transport chunk before yielding it, and conversion to
        the returned immutable bytes may create a temporary copy; this is not a
        physical wire-byte or process-RSS limit. An injected non-scoped client
        and its transport are trusted capabilities and must not eagerly consume
        the body inside ``send``. No later iterator chunk is requested after
        the first over-limit yield.
        """
        if not include_all_statuses and response.status_code not in {200, 201}:
            return b""

        content_lengths = response.headers.get_list("content-length", split_commas=True)
        transfer_encodings = response.headers.get_list("transfer-encoding", split_commas=True)
        if (
            len(content_lengths) > 1
            or len(transfer_encodings) > 1
            or (content_lengths and transfer_encodings)
            or (transfer_encodings and transfer_encodings[0].casefold() != "chunked")
        ):
            raise error_for_transport("envelope_invalid", detail="headers")

        expected_length: int | None = None
        if content_lengths:
            raw_length = content_lengths[0]
            if (
                not raw_length
                or len(raw_length) > 20
                or not raw_length.isascii()
                or not raw_length.isdecimal()
            ):
                raise error_for_transport("envelope_invalid", detail="content_length")
            expected_length = int(raw_length)
            if expected_length > self._max_response_bytes:
                raise error_for_transport(
                    "response_too_large", detail=str(self._max_response_bytes)
                )

        content_encodings = response.headers.get_list("content-encoding", split_commas=True)
        if content_encodings and (
            len(content_encodings) != 1 or content_encodings[0].casefold() != "identity"
        ):
            raise error_for_transport("envelope_invalid", detail="content_encoding")

        # HTTPX's standard MockTransport ``Response(content=...)`` constructor
        # eagerly reads its ByteStream, leaving ``is_stream_consumed`` true but
        # retaining the bounded test/injected body on ``response.content``.
        # Accept that already-buffered shape without restarting the stream. A
        # real HTTP response still follows the incremental raw-stream path
        # below; an already-consumed, uncached stream remains a hard failure.
        if response.is_stream_consumed:
            if scopes:
                check_scope_liveness(scopes)
            try:
                buffered_content = response.content
            except httpx.ResponseNotRead:
                stream_error = error_for_transport("network", detail="response_body_unavailable")
                stream_error.retryable = False
                raise stream_error from None
            if len(buffered_content) > self._max_response_bytes:
                raise error_for_transport(
                    "response_too_large", detail=str(self._max_response_bytes)
                )
            if expected_length is not None and len(buffered_content) != expected_length:
                raise error_for_transport("envelope_invalid", detail="content_length")
            if scopes:
                check_scope_liveness(scopes)
            return buffered_content

        content = bytearray()
        # Leave chunking to the transport. Passing ``chunk_size`` makes HTTPX's
        # byte chunker coalesce multiple source chunks before yielding, delaying
        # scope checks and potentially consuming more body data before we can
        # enforce the cap.
        iterator = iter(response.iter_raw())
        while True:
            if scopes:
                check_scope_liveness(scopes)
            try:
                chunk = next(iterator)
            except StopIteration:
                break
            except httpx.TimeoutException:
                raise
            except httpx.HTTPError:
                raise
            except Exception as exc:
                stream_error = error_for_transport("network", detail=exc.__class__.__name__)
                stream_error.retryable = False
                raise stream_error from None
            if scopes:
                check_scope_liveness(scopes)
            if len(content) + len(chunk) > self._max_response_bytes:
                raise error_for_transport(
                    "response_too_large", detail=str(self._max_response_bytes)
                )
            content.extend(chunk)

        if expected_length is not None and len(content) != expected_length:
            raise error_for_transport("envelope_invalid", detail="content_length")
        return bytes(content)

    def _handle_response(
        self,
        response: httpx.Response,
        elapsed: float,
        *,
        content: bytes | None = None,
        rate_limit_recorded: bool = False,
    ) -> FuyaoHttpResponse:
        """Validate HTTP status and parse the Fuyao envelope from bounded bytes."""
        if content is None:
            content = response.content
        if len(content) > self._max_response_bytes:
            raise error_for_transport("response_too_large", detail=str(self._max_response_bytes))
        if response.status_code == 429:
            # 429 与业务码 4001 等效：记录冷却并按可重试分类抛出。
            if not rate_limit_recorded:
                self._limiter.record_rate_limit(retry_after_seconds=_retry_after(response))
            raise error_for_transport("rate_limited", detail="429")
        if response.status_code in {200, 201}:
            try:
                payload = json.loads(content)
            except ValueError:
                raise error_for_transport("envelope_invalid", detail="json") from None
            if not isinstance(payload, Mapping):
                raise error_for_transport("envelope_invalid", detail="not_mapping")
            return FuyaoHttpResponse(
                status_code=response.status_code,
                envelope=parse_envelope(payload),
                elapsed_seconds=elapsed,
                raw_content=content,
            )
        raise error_for_transport("http", detail=str(response.status_code))

    def _backoff_seconds(self, attempt: int) -> float:
        """指数退避 + 抖动（第 ``attempt`` 次失败后的等待）."""
        # 2.0 ** n：mypy 对 int ** 非常量指数推断为 Any，用浮点底数保持类型明确。
        base = self._backoff_base_seconds * (2.0 ** (attempt - 1))
        jitter: float = random.random() * self._backoff_jitter_seconds  # noqa: S311  # nosec B311  # jitter term of the exponential retry backoff; no security value uses this draw
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


@contextmanager
def _close_response(response: httpx.Response) -> Iterator[None]:
    """Close a streamed response without replacing the primary read failure."""
    primary_error: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            response.close()
        except Exception as exc:
            if primary_error is not None:
                logger.warning(
                    "fuyao response close failed after a request error",
                    extra={"error_type": exc.__class__.__name__},
                )
            else:
                category = "timeout" if isinstance(exc, httpx.TimeoutException) else "network"
                close_error = error_for_transport(category, detail=exc.__class__.__name__)
                close_error.retryable = False
                raise close_error from None


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
    "FuyaoRawExchangeResponse",
    "FuyaoHttpResponse",
    "FuyaoTransportAttemptTrace",
]


def _make_attempt_trace(
    *,
    transport_request_id: str,
    attempt_id: str,
    attempt_no: int,
    reservation: AttemptReservation,
    http_status: int | None,
    failure_category: str | None,
    source_request_id: object,
) -> FuyaoTransportAttemptTrace:
    """Build the public allowlist projection of a local attempt reservation."""
    return FuyaoTransportAttemptTrace(
        transport_request_id=transport_request_id,
        attempt_id=attempt_id,
        attempt_no=attempt_no,
        reserved_at_us=reservation.reserved_at_us,
        gate_status=reservation.status.value,
        request_budget_reserved=True,
        http_status=http_status,
        outcome="response_observed" if http_status is not None else "outcome_unknown",
        failure_category=failure_category,
        source_request_id=_safe_source_request_id(source_request_id),
    )


def _safe_source_request_id(value: object) -> str | None:
    """Keep a bounded printable source ID distinct from local transport IDs."""
    if (
        type(value) is not str
        or not value
        or len(value) > 256
        or not value.isascii()
        or not value.isprintable()
    ):
        return None
    return value
