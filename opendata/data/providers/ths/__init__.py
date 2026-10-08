"""Lightweight public package interface for ths."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opendata.data.providers.ths.registration import FETCHERS as FETCHERS
    from opendata.data.providers.ths.registration import register as register
    from opendata.data.providers.ths.transport import API_KEY_HEADER as API_KEY_HEADER
    from opendata.data.providers.ths.transport import CODE_CATEGORIES as CODE_CATEGORIES
    from opendata.data.providers.ths.transport import (
        DEFAULT_FUYAO_API_BASE_URL as DEFAULT_FUYAO_API_BASE_URL,
    )
    from opendata.data.providers.ths.transport import DEFAULT_MAX_ATTEMPTS as DEFAULT_MAX_ATTEMPTS
    from opendata.data.providers.ths.transport import (
        DEFAULT_MAX_RESPONSE_BYTES as DEFAULT_MAX_RESPONSE_BYTES,
    )
    from opendata.data.providers.ths.transport import (
        DEFAULT_TIMEOUT_SECONDS as DEFAULT_TIMEOUT_SECONDS,
    )
    from opendata.data.providers.ths.transport import (
        FUYAO_API_BASE_URL_ENV as FUYAO_API_BASE_URL_ENV,
    )
    from opendata.data.providers.ths.transport import FUYAO_API_KEY_ENV as FUYAO_API_KEY_ENV
    from opendata.data.providers.ths.transport import LOCAL_BLOCKING_CODES as LOCAL_BLOCKING_CODES
    from opendata.data.providers.ths.transport import RATE_LIMIT_CODES as RATE_LIMIT_CODES
    from opendata.data.providers.ths.transport import SUCCESS_CODE as SUCCESS_CODE
    from opendata.data.providers.ths.transport import TRANSIENT_CODES as TRANSIENT_CODES
    from opendata.data.providers.ths.transport import ErrorMessage as ErrorMessage
    from opendata.data.providers.ths.transport import ErrorTableError as ErrorTableError
    from opendata.data.providers.ths.transport import FuyaoCredentials as FuyaoCredentials
    from opendata.data.providers.ths.transport import (
        FuyaoCredentialsError as FuyaoCredentialsError,
    )
    from opendata.data.providers.ths.transport import FuyaoEnvelope as FuyaoEnvelope
    from opendata.data.providers.ths.transport import FuyaoError as FuyaoError
    from opendata.data.providers.ths.transport import FuyaoHttpClient as FuyaoHttpClient
    from opendata.data.providers.ths.transport import FuyaoHttpResponse as FuyaoHttpResponse
    from opendata.data.providers.ths.transport import FuyaoRateLimiter as FuyaoRateLimiter
    from opendata.data.providers.ths.transport import category_for as category_for
    from opendata.data.providers.ths.transport import (
        error_for_transport as error_for_transport,
    )
    from opendata.data.providers.ths.transport import (
        error_for_upstream_code as error_for_upstream_code,
    )
    from opendata.data.providers.ths.transport import load_error_messages as load_error_messages
    from opendata.data.providers.ths.transport import parse_envelope as parse_envelope

_TRANSPORT_EXPORTS = (
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
)


def __getattr__(name: str) -> object:
    """Load compatibility exports only when explicitly requested."""
    if name == "register":
        from opendata.data.providers.ths.registration import register

        return register
    if name == "FETCHERS":
        from opendata.data.providers.ths.registration import FETCHERS

        return FETCHERS
    if name in _TRANSPORT_EXPORTS:
        from importlib import import_module

        transport = import_module(f"{__name__}.transport")
        value = getattr(transport, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["FETCHERS", "register", *_TRANSPORT_EXPORTS]
