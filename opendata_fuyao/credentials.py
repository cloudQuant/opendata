"""fuyao credential resolution (``X-api-key``).

同花顺扶摇凭据解析：

API Key 经环境变量 ``FUYAO_API_KEY`` 注入，绝不落库明文、不进日志、不进异常
消息。异常信息一律使用脱敏占位。
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

#: 凭据环境变量名。
FUYAO_API_KEY_ENV = "FUYAO_API_KEY"
#: 可选覆盖：fuyao API base URL。
FUYAO_API_BASE_URL_ENV = "FUYAO_API_BASE_URL"
#: 默认 base URL（消费者侧同花顺客户端一致）。
DEFAULT_FUYAO_API_BASE_URL = "https://fuyao.aicubes.cn"

_CREDENTIAL_MASK = "<redacted>"

if TYPE_CHECKING:
    from collections.abc import Mapping


class FuyaoCredentialsError(RuntimeError):
    """凭据解析失败（缺配置或非法），绝不携带 Key 明文."""

    def __init__(self, code: str, *, detail: str | None = None) -> None:
        """构造凭据错误（绝不携带 Key 明文）.

        Args:
            code: 稳定错误码.
            detail: 附加细节（不含凭据）.
        """
        self.code = code
        self.detail = detail
        super().__init__(code)


class FuyaoCredentials:
    """从环境变量解析的 fuyao API Key 持有者.

    Attributes:
        base_url: API 根地址（无尾斜杠）。
    """

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_FUYAO_API_BASE_URL) -> None:
        """构造凭据；缺失或非法一律失败关闭，绝不写入日志.

        Args:
            api_key: 上游 API Key。
            base_url: API 根地址。

        Raises:
            FuyaoCredentialsError: Key 为空或 base URL 非法。
        """
        if not isinstance(api_key, str) or not api_key.strip():
            raise FuyaoCredentialsError("FUYAO_CREDENTIAL_INVALID")
        if not isinstance(base_url, str) or not base_url.strip():
            raise FuyaoCredentialsError("FUYAO_BASE_URL_INVALID")
        normalized_base = base_url.strip().rstrip("/")
        if not normalized_base.lower().startswith(("http://", "https://")):
            raise FuyaoCredentialsError("FUYAO_BASE_URL_INVALID")
        self._api_key = api_key.strip()
        self.base_url = normalized_base

    @property
    def api_key(self) -> str:
        """返回 Key 用于构造请求头；调用方不得写入日志."""
        return self._api_key

    def __repr__(self) -> str:
        """返回脱敏表示（不含 Key 明文）."""
        return f"FuyaoCredentials(api_key={_CREDENTIAL_MASK})"

    def __str__(self) -> str:
        """返回脱敏字符串（不含 Key 明文）."""
        return f"FuyaoCredentials(api_key={_CREDENTIAL_MASK})"

    @classmethod
    def from_environment(
        cls, *, environment: Mapping[str, str] | None = None
    ) -> FuyaoCredentials | None:
        """从环境变量解析凭据；未配置返回 ``None``（上层失败关闭）.

        Args:
            environment: 覆盖环境映射（测试用）；默认读 ``os.environ``。

        Returns:
            凭据对象；未配置 ``FUYAO_API_KEY`` 时返回 ``None``。
        """
        source = os.environ if environment is None else environment
        raw_key = source.get(FUYAO_API_KEY_ENV, "").strip()
        if not raw_key:
            return None
        base_url = source.get(FUYAO_API_BASE_URL_ENV, DEFAULT_FUYAO_API_BASE_URL).strip()
        return cls(raw_key, base_url=base_url or DEFAULT_FUYAO_API_BASE_URL)


__all__ = [
    "DEFAULT_FUYAO_API_BASE_URL",
    "FUYAO_API_BASE_URL_ENV",
    "FUYAO_API_KEY_ENV",
    "FuyaoCredentials",
    "FuyaoCredentialsError",
]
