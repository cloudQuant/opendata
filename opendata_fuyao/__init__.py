"""fuyao transport layer (A3.1).

（同花顺扶摇）客户端传输层：

只做**传输与失败分类**：凭据解析、统一信封、错误业务文案表、限流退避与
HTTP 客户端。端点语义（``/api/meta/*``、``/api/a-share/prices``、除复权事件、
market-dumps）在 A3.2/A3.3 落地；provider 化在 A3.4（FR-7 形态）。
"""

from opendata_fuyao.credentials import (
    DEFAULT_FUYAO_API_BASE_URL,
    FUYAO_API_BASE_URL_ENV,
    FUYAO_API_KEY_ENV,
    FuyaoCredentials,
    FuyaoCredentialsError,
)
from opendata_fuyao.envelope import SUCCESS_CODE, FuyaoEnvelope, parse_envelope
from opendata_fuyao.errors import (
    CODE_CATEGORIES,
    LOCAL_BLOCKING_CODES,
    RATE_LIMIT_CODES,
    TRANSIENT_CODES,
    ErrorMessage,
    ErrorTableError,
    FuyaoError,
    category_for,
    error_for_transport,
    error_for_upstream_code,
    load_error_messages,
)
from opendata_fuyao.http_client import (
    API_KEY_HEADER,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_MAX_RESPONSE_BYTES,
    DEFAULT_TIMEOUT_SECONDS,
    FuyaoHttpClient,
    FuyaoHttpResponse,
)
from opendata_fuyao.rate_limiter import FuyaoRateLimiter

__all__ = [
    "API_KEY_HEADER",
    "CODE_CATEGORIES",
    "DEFAULT_FUYAO_API_BASE_URL",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_RESPONSE_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "FUYAO_API_BASE_URL_ENV",
    "FUYAO_API_KEY_ENV",
    "LOCAL_BLOCKING_CODES",
    "RATE_LIMIT_CODES",
    "SUCCESS_CODE",
    "TRANSIENT_CODES",
    "ErrorMessage",
    "ErrorTableError",
    "FuyaoCredentials",
    "FuyaoCredentialsError",
    "FuyaoEnvelope",
    "FuyaoError",
    "FuyaoHttpClient",
    "FuyaoHttpResponse",
    "FuyaoRateLimiter",
    "category_for",
    "error_for_transport",
    "error_for_upstream_code",
    "load_error_messages",
    "parse_envelope",
]
