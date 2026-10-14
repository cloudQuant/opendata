"""Version-gated yfinance transport with per-send request-budget enforcement.

The supported runtime is deliberately narrow and every touchpoint is read
through the SDK module handed to the seam, so a reviewed generation can be
driven behind injected fixtures without a live import. Two reviewed yfinance
generations are governed: the 1.7.0 review pin and the 0.2.66 distribution
the project runtime carries (pyproject's extras comment records curl_cffi
0.13.0 as the exercised transport; the older 0.16.3 pin stays governed).
Each lower-level send is authorized against its actual HTTPS hostname and
consumes exactly one scoped attempt. SDK cache hits never reach this session
and therefore consume no transport attempt.
"""

from __future__ import annotations

import importlib
import math
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from typing import Any
from urllib.parse import urldefrag, urljoin, urlsplit

from opendata.data.providers.yfinance.models._budget import reserve_yfinance_send
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.request_budget import (
    RequestAdapterRetryError,
    RequestAuthorizationError,
    RequestExecutionDeadlineError,
    RequestExecutionScope,
    RequestRedirectError,
    authorize_request,
    check_scope_liveness,
    current_request_scopes,
    scoped_host_slot,
)

SUPPORTED_YFINANCE_VERSION = "1.7.0"
SUPPORTED_CURL_CFFI_VERSION = "0.16.3"
MAX_YFINANCE_REDIRECTS = 5

# Exact fail-closed generations this provider governs. The 1.7.0/0.16.3 pair
# is the original review pin; the 0.2.66 SDK generation carried by the
# project runtime over curl_cffi 0.13.0 is the pair pyproject's extras
# comment exercised, so both are governed through the same seam.
_GOVERNED_YFINANCE_VERSIONS = frozenset({SUPPORTED_YFINANCE_VERSION, "0.2.66"})
_GOVERNED_CURL_CFFI_VERSIONS = frozenset({SUPPORTED_CURL_CFFI_VERSION, "0.13.0"})

_SDK_CALL_LOCK = threading.RLock()
_SESSION: Any | None = None
_GOVERNED_SESSION_TYPE: type[Any] | None = None


class _GovernedRetryStrategy:
    """Declared zero-retry posture of the governed session transport.

    Older curl_cffi generations expose no ``retry`` knob on ``Session``;
    their transport performs exactly one send per call, which is what the
    budget gate verifies through this attribute.
    """

    def __init__(self, *, count: int = 0) -> None:
        self.count = count


def _check_runtime(sdk: Any) -> Any:  # noqa: ANN401 - optional SDK module
    """Require the reviewed SDK pair and a transport configuration we can govern."""
    if getattr(sdk, "__version__", None) not in _GOVERNED_YFINANCE_VERSIONS:
        raise YfinanceProviderError("YFINANCE_UNSUPPORTED_SDK_VERSION")
    try:
        curl_cffi = importlib.import_module("curl_cffi")
    except ImportError as exc:
        raise YfinanceProviderError("YFINANCE_CURL_CFFI_MISSING") from exc
    if getattr(curl_cffi, "__version__", None) not in _GOVERNED_CURL_CFFI_VERSIONS:
        raise YfinanceProviderError("YFINANCE_UNSUPPORTED_CURL_CFFI_VERSION")

    config = getattr(sdk, "config", None)
    network = getattr(config, "network", None)
    retries = getattr(network, "retries", None)
    if type(retries) is not int or retries != 0:
        raise YfinanceProviderError("YFINANCE_RETRIES_UNSUPPORTED")
    if getattr(network, "proxy", None) is not None:
        raise YfinanceProviderError("YFINANCE_PROXY_UNSUPPORTED")
    return curl_cffi


def _check_retry_strategy(session: Any) -> None:  # noqa: ANN401 - curl session type
    strategy = getattr(session, "retry", None)
    retry_count: object = getattr(strategy, "count", None)
    if type(retry_count) is not int or retry_count != 0:
        raise RequestAdapterRetryError("curl_cffi retries must be disabled")


def _ensure_yfdata_session(sdk: Any, session: Any, *, initialize: bool) -> None:  # noqa: ANN401
    """Verify yfinance's singleton points at the one governed process session."""
    try:
        data_module = sdk.data
        data_class = data_module.YfData
        instances = data_class._instances
        instance = instances.get(data_class)
    except (AttributeError, TypeError) as exc:
        raise YfinanceProviderError("YFINANCE_SESSION_SINGLETON_UNAVAILABLE") from exc
    if instance is None and initialize:
        try:
            instance = data_class(session=session)
        except Exception as exc:
            raise YfinanceProviderError("YFINANCE_SESSION_INITIALIZATION_FAILED") from exc
    if instance is None or getattr(instance, "_session", None) is not session:
        raise RequestAuthorizationError("yfinance replaced the governed process session")


def _new_session_class(curl_session_class: type[Any]) -> type[Any]:
    """Build a lazy subclass so importing this provider never imports curl_cffi."""

    def request(self: Any, method: str, url: str, *args: object, **kwargs: object) -> Any:  # noqa: ANN401
        return _manual_request(self, _governed_request_once, method, url, *args, **kwargs)

    return type(
        "YfinanceGovernedSession",
        (curl_session_class,),
        {
            "request": request,
            "retry": _GovernedRetryStrategy(count=0),
            "__module__": __name__,
        },
    )


def _send_once(session: Any, method: str, url: str, *args: object, **kwargs: object) -> Any:  # noqa: ANN401
    """Call the reviewed transport's own send path exactly once (offline-testable seam)."""
    base_class = type(session).__mro__[1]
    base_request = getattr(base_class, "request", None)
    if not callable(base_request):
        raise YfinanceProviderError("YFINANCE_TRANSPORT_SEND_UNAVAILABLE")
    return base_request(session, method, url, *args, **kwargs)


def _parse_target(url: object) -> tuple[str, tuple[str, int], str]:
    if not isinstance(url, str) or not url:
        raise RequestAuthorizationError("transport URL must be an absolute HTTPS URL")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise RequestAuthorizationError("transport URL is malformed") from exc
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise RequestAuthorizationError("transport requires a default-port HTTPS URL")
    host = hostname.lower().rstrip(".")
    if not host:
        raise RequestAuthorizationError("transport URL has no hostname")
    normalized = urldefrag(parsed.geturl()).url
    return host, (host, 443), normalized


def _scopes_or_reject() -> tuple[RequestExecutionScope, ...]:
    scopes = current_request_scopes()
    if not scopes:
        raise RequestAuthorizationError("yfinance transport requires an active request scope")
    check_scope_liveness(scopes)
    return scopes


def _curl_not_set(curl_session_module: Any) -> object:  # noqa: ANN401 - reviewed module
    """Return the transport's 'timeout not provided' sentinel for either generation."""
    for name in ("NOT_SET", "not_set"):
        sentinel = getattr(curl_session_module, name, None)
        if sentinel is not None:
            return sentinel
    return object()


def _remaining_timeout(
    session: Any,  # noqa: ANN401
    timeout: object,
    scopes: tuple[RequestExecutionScope, ...],
) -> object:
    deadlines = [scope.deadline for scope in scopes if scope.deadline is not None]
    if not deadlines:
        return timeout
    check_scope_liveness(scopes)
    remaining = min(float(deadline) - time.monotonic() for deadline in deadlines)
    if remaining <= 0:
        raise RequestExecutionDeadlineError("request execution deadline expired")

    curl_session_module = importlib.import_module("curl_cffi.requests.session")
    not_set = _curl_not_set(curl_session_module)
    effective = getattr(session, "timeout", None) if timeout is not_set else timeout
    if effective is None or effective is not_set:
        return remaining
    if isinstance(effective, tuple):
        if len(effective) != 2:
            raise RequestAuthorizationError(
                "curl timeout tuple must contain connect and read values"
            )
        values: tuple[object, ...] = effective
    else:
        values = (effective,)
    clamped: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RequestAuthorizationError("curl timeout must be a finite positive number")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise RequestAuthorizationError("curl timeout must be a finite positive number")
        clamped.append(min(number, remaining))
    return tuple(clamped) if isinstance(effective, tuple) else clamped[0]


def _governed_request_once(
    session: Any,  # noqa: ANN401
    method: str,
    url: str,
    *args: object,
    **kwargs: object,
) -> Any:  # noqa: ANN401
    sdk = getattr(session, "_yfinance_sdk", None)
    if sdk is None:
        raise RequestAuthorizationError("governed yfinance session has no SDK owner")
    with _SDK_CALL_LOCK:
        _check_runtime(sdk)
        _check_retry_strategy(session)
        scopes = _scopes_or_reject()
        host, _origin, _normalized = _parse_target(url)
        _ensure_yfdata_session(sdk, session, initialize=False)
        authorize_request(scopes, "yfinance", host)
        send_kwargs = dict(kwargs)
        send_kwargs["allow_redirects"] = False
        send_kwargs["max_redirects"] = 0
        with scoped_host_slot(scopes, "yfinance", host):
            check_scope_liveness(scopes)
            _ensure_yfdata_session(sdk, session, initialize=False)
            send_kwargs["timeout"] = _remaining_timeout(
                session, send_kwargs.get("timeout", _not_set_sentinel()), scopes
            )
            reserve_yfinance_send(scopes, host)
            return _send_once(session, method, url, *args, **send_kwargs)


def _not_set_sentinel() -> object:
    module = importlib.import_module("curl_cffi.requests.session")
    return _curl_not_set(module)


def _manual_request(
    session: Any,  # noqa: ANN401
    base_send: Any,  # noqa: ANN401 - governed single-send gateway
    method: str,
    url: str,
    *args: object,
    **kwargs: object,
) -> Any:  # noqa: ANN401
    sdk = getattr(session, "_yfinance_sdk", None)
    if sdk is None:
        raise RequestAuthorizationError("governed yfinance session has no SDK owner")
    with _SDK_CALL_LOCK:
        _check_runtime(sdk)
        _check_retry_strategy(session)
        arguments = dict(kwargs)
        follow_redirects = arguments.get("allow_redirects")
        if follow_redirects is None:
            follow_redirects = bool(getattr(session, "allow_redirects", True))
        raw_max_redirects = arguments.get("max_redirects")
        if raw_max_redirects is None:
            raw_max_redirects = getattr(session, "max_redirects", MAX_YFINANCE_REDIRECTS)
        if isinstance(raw_max_redirects, bool) or not isinstance(raw_max_redirects, int):
            raise RequestRedirectError("redirect limit must be an integer")
        if raw_max_redirects < 0:
            raise RequestRedirectError("unlimited redirects are not supported")
        max_redirects = min(raw_max_redirects, MAX_YFINANCE_REDIRECTS)

        current_method = str(method).upper()
        current_url = url
        visited: set[str] = set()
        host, _origin, normalized = _parse_target(current_url)
        visited.add(normalized)
        previous_host = host
        redirects = 0
        with ExitStack() as session_guard:
            clear_session_credentials = session_guard.enter_context(
                _session_credentials_guard(session)
            )
            while True:
                hop_host, _hop_origin, _hop_url = _parse_target(current_url)
                cross_host = hop_host != previous_host
                hop_arguments = dict(arguments)
                hop_arguments["allow_redirects"] = False
                hop_arguments["max_redirects"] = 0
                hop_arguments["params"] = None if redirects else hop_arguments.get("params")
                if cross_host:
                    _strip_request_credentials(hop_arguments)
                    clear_session_credentials()
                response = base_send(session, current_method, current_url, *args, **hop_arguments)

                status = getattr(response, "status_code", None)
                response_headers = getattr(response, "headers", {})
                location = _header_value(response_headers, "location")
                if not follow_redirects or status not in {301, 302, 303, 307, 308} or not location:
                    return response
                if redirects >= max_redirects:
                    _close_response(response)
                    raise RequestRedirectError("yfinance redirect limit exceeded")
                next_url = urljoin(current_url, location)
                _next_host, _next_origin, next_normalized = _parse_target(next_url)
                if next_normalized in visited:
                    _close_response(response)
                    raise RequestRedirectError("yfinance redirect loop detected")
                try:
                    current_method = _redirect_method(
                        current_method,
                        status,
                        hop_arguments,
                    )
                except RequestRedirectError:
                    _close_response(response)
                    raise
                arguments = hop_arguments
                visited.add(next_normalized)
                redirects += 1
                current_url = next_url
                previous_host = hop_host
                _close_response(response)


def _redirect_method(method: str, status: int, arguments: dict[str, object]) -> str:
    has_body = any(
        arguments.get(name) is not None
        for name in ("data", "content", "json", "files", "multipart")
    )
    if method in {"GET", "HEAD"}:
        if has_body:
            raise RequestRedirectError("GET or HEAD request body cannot be redirected")
        return method
    if status in {301, 302, 303}:
        for name in ("data", "content", "json", "files", "multipart"):
            arguments[name] = None
        headers = arguments.get("headers")
        if headers is not None:
            arguments["headers"] = _without_entity_headers(headers)
        return "HEAD" if method == "HEAD" else "GET"
    raise RequestRedirectError("unsafe method or body cannot be replayed across redirect")


def _header_value(headers: object, name: str) -> str | None:
    if isinstance(headers, Mapping):
        for key, value in headers.items():
            if str(key).lower() == name:
                return value if isinstance(value, str) else None
    return None


def _header_pairs(headers: object) -> list[tuple[object, object]] | None:
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    if isinstance(headers, (list, tuple)):
        pairs: list[tuple[object, object]] = []
        for item in headers:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                return None
            pairs.append((item[0], item[1]))
        return pairs
    return None


def _without_sensitive_headers(headers: object) -> object:
    pairs = _header_pairs(headers)
    if pairs is None:
        raise RequestRedirectError("unsupported header shape on cross-host redirect")
    sensitive = {
        "authorization",
        "cookie",
        "proxy-authorization",
        "host",
        ":authority",
        "x-api-key",
        "x-auth-token",
    }
    return {key: value for key, value in pairs if str(key).lower() not in sensitive}


def _without_entity_headers(headers: object) -> object:
    pairs = _header_pairs(headers)
    if pairs is None:
        raise RequestRedirectError("unsupported header shape on method-changing redirect")
    entity = {"content-length", "content-type", "transfer-encoding"}
    return {key: value for key, value in pairs if str(key).lower() not in entity}


def _strip_request_credentials(arguments: dict[str, object]) -> None:
    arguments["headers"] = _without_sensitive_headers(arguments.get("headers"))
    for name in ("cookies", "auth", "proxy_auth", "referer"):
        arguments[name] = None


def _clear_session_credentials(session: Any) -> None:  # noqa: ANN401
    session.headers = _without_sensitive_headers(session.headers)
    session._cookies = type(session._cookies)()
    session.auth = None
    session.proxy_auth = None


@contextmanager
def _session_credentials_guard(
    session: Any,  # noqa: ANN401
) -> Iterator[Callable[[], None]]:
    old_headers = session.headers
    old_cookies = session._cookies
    old_auth = session.auth
    old_proxy_auth = session.proxy_auth
    cleared = False

    def clear() -> None:
        nonlocal cleared
        cleared = True
        try:
            _clear_session_credentials(session)
        except Exception as exc:
            raise RequestRedirectError(
                "unable to isolate yfinance credentials across hosts"
            ) from exc

    try:
        yield clear
    finally:
        if cleared:
            session.headers = old_headers
            session._cookies = old_cookies
            session.auth = old_auth
            session.proxy_auth = old_proxy_auth


def _close_response(response: Any) -> None:  # noqa: ANN401
    close = getattr(response, "close", None)
    if callable(close):
        close()


@contextmanager
def governed_yfinance_session(sdk: Any) -> Iterator[Any]:  # noqa: ANN401
    """Lock SDK construction/call and supply the one reviewed process session.

    The SDK must pass the exact-version gate before any Ticker/SDK operation.
    """
    global _SESSION, _GOVERNED_SESSION_TYPE
    with _SDK_CALL_LOCK:
        _check_runtime(sdk)
        request_api = importlib.import_module("curl_cffi.requests")
        if _GOVERNED_SESSION_TYPE is None:
            _GOVERNED_SESSION_TYPE = _new_session_class(request_api.Session)
        if _SESSION is None:
            _SESSION = _GOVERNED_SESSION_TYPE(
                impersonate="chrome",
                trust_env=False,
                allow_redirects=True,
                max_redirects=MAX_YFINANCE_REDIRECTS,
            )
        if not isinstance(_SESSION, request_api.Session):
            raise YfinanceProviderError("YFINANCE_SESSION_TYPE_INVALID")
        _SESSION._yfinance_sdk = sdk
        _check_retry_strategy(_SESSION)
        if getattr(_SESSION, "cache", None) is not None:
            raise YfinanceProviderError("YFINANCE_SESSION_CACHE_UNSUPPORTED")
        _ensure_yfdata_session(sdk, _SESSION, initialize=True)
        yield _SESSION


def governed_sdk_lock() -> threading.RLock:
    """Return the reentrant process lock for source-owned offline diagnostics."""
    return _SDK_CALL_LOCK


__all__ = [
    "MAX_YFINANCE_REDIRECTS",
    "SUPPORTED_CURL_CFFI_VERSION",
    "SUPPORTED_YFINANCE_VERSION",
    "_SDK_CALL_LOCK",
    "_SESSION",
    "_check_runtime",
    "_send_once",
    "governed_sdk_lock",
    "governed_yfinance_session",
]
