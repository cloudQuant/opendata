"""fuyao HTTP 客户端：超时、重试、退避、限流联动与响应体上限（A3.1）.

职责边界：本模块只管**传输与失败分类**，不做端点语义与字段映射（那些由
``opendata_fuyao`` 的端点模块与 provider 层承担）。所有失败统一为
:class:`~opendata_fuyao.errors.FuyaoError`，携带中文业务文案与处置建议。

安全：API Key 只进请求头；日志与异常信息一律脱敏。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from opendata_fuyao.envelope import FuyaoEnvelope, parse_envelope
from opendata_fuyao.errors import FuyaoError, error_for_transport
from opendata_fuyao.rate_limiter import FuyaoRateLimiter

if TYPE_CHECKING:
    from opendata_fuyao.credentials import FuyaoCredentials

logger = logging.getLogger(__name__)

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
        rate_limiter: FuyaoRateLimiter | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_jitter_seconds: float = DEFAULT_BACKOFF_JITTER_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """构造客户端.

        Args:
            credentials: 凭据。
            client: 注入的 httpx 客户端（测试用 MockTransport）；``None`` 时
                使用直连（不继承本地代理配置）。
            rate_limiter: 限流器；``None`` 时使用默认配置。
            timeout_seconds: 单请求超时。
            max_attempts: 最大尝试次数（含首次）。
            backoff_base_seconds: 指数退避基数。
            backoff_jitter_seconds: 退避抖动上限（随机）。
            max_response_bytes: 响应体上限。
            sleep: 退避等待实现（测试注入，避免真实休眠）。
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=timeout_seconds, trust_env=False)
        self._limiter = rate_limiter or FuyaoRateLimiter()
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_jitter_seconds = backoff_jitter_seconds
        self._max_response_bytes = max_response_bytes
        self._sleep = sleep

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

    def get(self, endpoint: str, *, params: Mapping[str, Any] | None = None) -> FuyaoHttpResponse:
        """调用 GET，带重试/退避/限流联动.

        Args:
            endpoint: 以 ``/`` 开头的端点路径。
            params: 查询参数。

        Returns:
            成功响应与解析后的信封。

        Raises:
            FuyaoError: 鉴权/权限/领域结果/限流/暂时性/传输失败，均含中文文案。
        """
        if not endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/'")
        last_error: FuyaoError | None = None
        for attempt in range(1, self._max_attempts + 1):
            self._limiter.wait_and_acquire(self._sleep)
            started = time.monotonic()
            try:
                response = self._client.get(
                    f"{self.credentials.base_url}{endpoint}",
                    params=dict(params or {}),
                    headers={API_KEY_HEADER: self.credentials.api_key},
                    timeout=self._timeout_seconds,
                )
            except httpx.TimeoutException:
                last_error = error_for_transport("timeout")
            except httpx.HTTPError as exc:
                last_error = error_for_transport("network", detail=exc.__class__.__name__)
            else:
                elapsed = time.monotonic() - started
                try:
                    return self._handle_response(response, elapsed)
                except FuyaoError as exc:
                    last_error = exc
                    if not exc.retryable:
                        raise
            if attempt >= self._max_attempts:
                break
            self._sleep(self._backoff_seconds(attempt))
            logger.warning(
                "fuyao %s attempt %s/%s failed: %s",
                endpoint,
                attempt,
                self._max_attempts,
                last_error.code if last_error else "UNKNOWN",
            )
        if last_error is None:  # pragma: no cover - 循环必然设置
            raise error_for_transport("unknown")
        raise last_error

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
