"""fuyao（同花顺扶摇）错误分类与业务文案（FR-21 / A3.1）.

上游统一信封用 ``code`` 表达业务结果；本模块把错误码映射到**稳定分类**
（可重试性、是否本地前置拦截）并从 ``error_messages.yaml`` 取中文业务文案
与处置建议，供前端提示、执行记录与 WS 错误帧统一使用。

事实口径与消费者侧同花顺客户端一致（同生态 API 事实）：鉴权/权限不可重试、
限流等效 HTTP 429、5xxx 为暂时性错误可限次重试、1001~1004 应在本地拦截。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

#: 稳定分类：决定重试与处置策略。
ErrorCategory = Literal[
    "ok",
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
]

#: 上游业务错误码 → 稳定分类。
CODE_CATEGORIES: dict[int, ErrorCategory] = {
    1001: "request",
    1002: "request",
    1003: "request",
    1004: "request",
    2001: "auth",
    2003: "permission",
    3001: "empty",
    3002: "not_ready",
    3004: "unsupported",
    4001: "rate_limited",
    5001: "transient",
    5002: "transient",
    5003: "transient",
}

#: 本地前置拦截：这些码不应被发送到上游（请求构造错误）。
LOCAL_BLOCKING_CODES: frozenset[int] = frozenset({1001, 1002, 1003, 1004})

#: 与 HTTP 429 等效的业务码。
RATE_LIMIT_CODES: frozenset[int] = frozenset({4001})

#: 可限次重试的业务码（服务端/上游暂时性）。
TRANSIENT_CODES: frozenset[int] = frozenset({5001, 5002, 5003})

#: 错误文案表文件（随包分发）。
ERROR_MESSAGES_PATH = Path(__file__).parent / "error_messages.yaml"


class FuyaoError(RuntimeError):
    """fuyao 客户端稳定错误.

    Attributes:
        code: 稳定标识（``FUYAO_<CATEGORY>`` 或 ``FUYAO_<CODE>``）。
        category: 稳定分类，决定重试与处置。
        message: 中文业务文案（来自错误业务文案表）。
        advice: 中文处置建议（来自错误业务文案表）。
        upstream_code: 上游业务错误码（传输层错误为 ``None``）。
        request_id: 上游请求 ID（用于对账，非必需）。
        retryable: 是否可限次重试。
    """

    def __init__(
        self,
        code: str,
        *,
        category: ErrorCategory,
        message: str,
        advice: str,
        upstream_code: int | None = None,
        request_id: str | None = None,
        retryable: bool = False,
    ) -> None:
        """构造错误（稳定标识 + 分类 + 中文文案 + 处置建议）.

        Args:
            code: 稳定标识。
            category: 稳定分类。
            message: 中文业务文案。
            advice: 中文处置建议。
            upstream_code: 上游业务错误码。
            request_id: 上游请求 ID。
            retryable: 是否可限次重试。
        """
        self.code = code
        self.category = category
        self.message = message
        self.advice = advice
        self.upstream_code = upstream_code
        self.request_id = request_id
        self.retryable = retryable
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        """返回可写入执行记录/WS 错误帧的载荷.

        Returns:
            错误码、分类、中文文案、处置建议与上游请求 ID。
        """
        return {
            "code": self.code,
            "category": self.category,
            "message": self.message,
            "advice": self.advice,
            "upstream_code": self.upstream_code,
            "request_id": self.request_id,
            "retryable": self.retryable,
        }


class ErrorTableError(RuntimeError):
    """错误业务文案表缺失或格式非法（fail closed）."""


@dataclass(frozen=True)
class ErrorMessage:
    """错误业务文案表条目.

    Attributes:
        category: 稳定分类。
        message: 中文业务文案。
        advice: 中文处置建议。
    """

    category: str
    message: str
    advice: str


@lru_cache(maxsize=1)
def load_error_messages(path: str | None = None) -> dict[str, ErrorMessage]:
    """加载错误业务文案表（fail closed）.

    Args:
        path: 覆盖文件路径（测试用）；默认取随包文件。

    Returns:
        以错误码字符串或传输层分类为键的文案表。

    Raises:
        ErrorTableError: 文件不可读、结构非法或条目缺字段。
    """
    source = Path(path) if path else ERROR_MESSAGES_PATH
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ErrorTableError(f"error messages {source} are unreadable: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("messages"), dict):
        raise ErrorTableError(f"error messages {source} must declare a messages mapping")
    table = {
        str(key): _parse_message(key, entry, source) for key, entry in payload["messages"].items()
    }
    if not table:
        raise ErrorTableError(f"error messages {source} must not be empty")
    return table


def _parse_message(key: object, entry: object, source: Path) -> ErrorMessage:
    """解析单条文案（fail closed）.

    Args:
        key: 文案键（错误码或传输层分类）.
        entry: 原始条目.
        source: 来源文件（用于报错）.

    Returns:
        解析后的文案条目.

    Raises:
        ErrorTableError: 条目缺字段或类型非法.
    """
    if not isinstance(entry, dict):
        raise ErrorTableError(f"error message {key!r} in {source} is not a mapping")
    try:
        return ErrorMessage(
            category=str(entry["category"]),
            message=str(entry["message"]),
            advice=str(entry["advice"]),
        )
    except (KeyError, TypeError) as exc:
        raise ErrorTableError(f"error message {key!r} in {source} is malformed: {exc}") from exc


def _entry(key: str) -> ErrorMessage:
    table = load_error_messages()
    return table.get(key) or table["unknown"]


def category_for(upstream_code: int) -> ErrorCategory:
    """把上游业务错误码映射为稳定分类.

    Args:
        upstream_code: 上游 ``code`` 字段（非 0）。

    Returns:
        稳定分类；未登记的错误码为 ``"unknown"``。
    """
    return CODE_CATEGORIES.get(upstream_code, "unknown")


def is_retryable_category(category: ErrorCategory) -> bool:
    """判断分类是否可限次重试.

    Args:
        category: 稳定分类。

    Returns:
        限流与暂时性错误可重试；领域结果与鉴权类不可重试。
    """
    return category in {"rate_limited", "transient", "network", "timeout"}


def error_for_upstream_code(upstream_code: int, *, request_id: str | None = None) -> FuyaoError:
    """构造上游业务错误（含中文文案与处置建议）.

    Args:
        upstream_code: 上游非零 ``code``。
        request_id: 上游请求 ID。

    Returns:
        Stable error；``code`` 为 ``FUYAO_<分类大写>``。

    Raises:
        ValueError: ``upstream_code`` 为 0（成功不是错误）。
    """
    if upstream_code == 0:
        raise ValueError("upstream code 0 means success, not an error")
    category = category_for(upstream_code)
    entry = _entry(str(upstream_code))
    return FuyaoError(
        f"FUYAO_{category.upper()}",
        category=category,
        message=entry.message,
        advice=entry.advice,
        upstream_code=upstream_code,
        request_id=request_id,
        retryable=is_retryable_category(category),
    )


def error_for_transport(
    key: str, *, detail: str | None = None, request_id: str | None = None
) -> FuyaoError:
    """构造传输层错误（网络/超时/响应过大/信封非法/HTTP）.

    Args:
        key: 传输层分类键（见错误业务文案表）。
        detail: 附加细节（状态码等），只进 ``code`` 不进文案。
        request_id: 上游请求 ID（若有）。

    Returns:
        Stable error。
    """
    entry = _entry(key)
    category = entry.category
    suffix = f"_{detail}" if detail else ""
    return FuyaoError(
        f"FUYAO_{key.upper()}{suffix}",
        category=category,  # type: ignore[arg-type]  # 表内分类受测试守护
        message=entry.message,
        advice=entry.advice,
        request_id=request_id,
        retryable=is_retryable_category(category),  # type: ignore[arg-type]
    )
