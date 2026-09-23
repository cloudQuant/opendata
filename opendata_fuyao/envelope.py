"""fuyao 统一信封（``ApiResponse``）解析（A3.1）.

信封形态（同生态 API 事实）：

.. code-block:: json

    {"code": 0, "message": "success", "request_id": "...",
     "data": {"timestamp": 1716105600000, "item": []}}

``code`` 为 0 表示成功；非 0 一律按 :mod:`opendata_fuyao.errors` 的分类失败。
限流时上游可能同时返回 HTTP 429，客户端须同时检查 HTTP 状态码与信封 ``code``。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from opendata_fuyao.errors import error_for_transport, error_for_upstream_code

#: 成功时 ``code`` 的取值。
SUCCESS_CODE = 0


@dataclass(frozen=True)
class FuyaoEnvelope:
    """解析后的上游信封.

    Attributes:
        code: 上游业务码（0 为成功）。
        message: 上游原始 message（用于对账，不直接面向用户）。
        request_id: 上游请求 ID。
        items: ``data.item`` 业务数据行（元组，可能为空）。
        data_timestamp_ms: ``data.timestamp`` 毫秒戳（可能为 ``None``）。
    """

    code: int
    message: str
    request_id: str
    items: tuple[Any, ...]
    data_timestamp_ms: int | None


def parse_envelope(payload: Mapping[str, Any]) -> FuyaoEnvelope:
    """解析信封；非 0 业务码按稳定分类抛出.

    Args:
        payload: ``response.json()`` 结果（必须是映射）。

    Returns:
        成功信封。

    Raises:
        FuyaoError: 信封结构非法，或业务码非 0（含中文文案与处置建议）。
    """
    code = payload.get("code")
    if not isinstance(code, int) or isinstance(code, bool):
        raise error_for_transport("envelope_invalid", detail="code")
    request_id = payload.get("request_id")
    raw_message = payload.get("message")
    message = raw_message if isinstance(raw_message, str) else ""
    if code != SUCCESS_CODE:
        raise error_for_upstream_code(
            code, request_id=request_id if isinstance(request_id, str) else None
        )
    data = payload.get("data")
    items: tuple[Any, ...] = ()
    timestamp: int | None = None
    if data is not None:
        if not isinstance(data, Mapping):
            raise error_for_transport("envelope_invalid", detail="data")
        raw_items = data.get("item", ())
        if isinstance(raw_items, (list, tuple)):
            items = tuple(raw_items)
        elif raw_items is not None:
            raise error_for_transport("envelope_invalid", detail="item")
        raw_timestamp = data.get("timestamp")
        if isinstance(raw_timestamp, int) and not isinstance(raw_timestamp, bool):
            timestamp = raw_timestamp
    return FuyaoEnvelope(
        code=code,
        message=message,
        request_id=request_id if isinstance(request_id, str) else "",
        items=items,
        data_timestamp_ms=timestamp,
    )


__all__ = ["SUCCESS_CODE", "FuyaoEnvelope", "parse_envelope"]
