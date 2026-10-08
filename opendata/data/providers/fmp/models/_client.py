"""Governed, key-safe transport for the offline FMP model prototype."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any, cast

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from requests import Response

FMP_DEFAULT_BASE_URL = "https://financialmodelingprep.com/stable"
FMP_HISTORICAL_RESPONSE_LIMIT = 5_000
_DEFAULT_TIMEOUT_SECONDS = 30.0


class FMPProviderError(RuntimeError):
    """Stable FMP errors that never include request parameters or response bodies."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        partial: bool = False,
        requests_made: int = 0,
    ) -> None:
        """Create a sanitized error with machine-readable status fields."""
        self.code = code
        self.status = status
        self.partial = partial
        self.requests_made = requests_made
        detail = "" if status is None else f" status={status}"
        super().__init__(code + detail)


class FMPQueryLimitError(FMPProviderError):
    """A response cannot be proven complete within the caller's declared budget."""

    def __init__(
        self,
        code: str,
        *,
        limit_kind: str,
        maximum: int,
        requests_made: int,
    ) -> None:
        """Create an explicit incomplete-result error."""
        self.limit_kind = limit_kind
        self.maximum = maximum
        super().__init__(
            code,
            partial=requests_made > 0,
            requests_made=requests_made,
        )


def require_api_key() -> str:
    """Read only the explicit FMP_API_KEY environment variable."""
    key = os.environ.get("FMP_API_KEY", "")
    if not key.strip():
        raise FMPProviderError("FMP_API_KEY_MISSING")
    return key.strip()


def fetch_array(
    endpoint: str,
    params: Mapping[str, str],
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch one JSON object array through the governed shared HTTP client."""
    api_key = require_api_key()
    response = _get_response(endpoint, params, api_key, timeout)
    status = response.status_code
    content_type = response.headers.get("Content-Type", "")
    body = response.text

    if _looks_like_html(body, content_type):
        raise FMPProviderError("FMP_HTML_RESPONSE", status=status)

    invalid_json_code: str | None = None
    try:
        document: object = json.loads(body)
    except json.JSONDecodeError as exc:
        invalid_json_code = (
            "FMP_TRUNCATED_RESPONSE" if _looks_truncated(body, exc) else "FMP_BAD_RESPONSE"
        )
    if invalid_json_code is not None:
        raise FMPProviderError(invalid_json_code, status=status)

    if isinstance(document, dict) and _is_api_error_document(document):
        raise FMPProviderError("FMP_API_ERROR", status=status)
    if not isinstance(document, list) or any(not isinstance(row, dict) for row in document):
        raise FMPProviderError("FMP_BAD_SHAPE", status=status)
    return cast("list[dict[str, Any]]", document)


def _get_response(
    endpoint: str,
    params: Mapping[str, str],
    api_key: str,
    timeout: float | None,
) -> Response:
    """Issue a key-bearing request and replace all transport errors safely."""
    url = f"{FMP_DEFAULT_BASE_URL}/{endpoint.lstrip('/')}"
    response: Response | None = None
    failure_code: str | None = None
    failure_status: int | None = None
    request_params = {**params, "apikey": api_key}
    try:
        response = get_shared_http_client().get(
            url,
            params=request_params,
            timeout=_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            source="fmp",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        failure_status = exc.status
        failure_code = _http_error_code(exc.status)
    except Exception:
        # The transport exception can contain the prepared URL and API key.
        # Do not chain or retain it in the public provider error.
        failure_code = "FMP_TRANSPORT_ERROR"

    if failure_code is not None:
        raise FMPProviderError(failure_code, status=failure_status)
    if response is None:  # pragma: no cover - invariant guard
        raise FMPProviderError("FMP_TRANSPORT_ERROR")
    if not 200 <= response.status_code < 300:
        raise FMPProviderError(
            _http_error_code(response.status_code),
            status=response.status_code,
        )
    return response


def _http_error_code(status: int | None) -> str:
    """Map transport status codes into stable FMP error categories."""
    if status == 401:
        return "FMP_AUTH_ERROR"
    if status == 403:
        return "FMP_FORBIDDEN"
    if status == 429:
        return "FMP_RATE_LIMITED"
    if status is not None and status >= 500:
        return "FMP_UPSTREAM_ERROR"
    return "FMP_HTTP_ERROR"


def _looks_like_html(body: str, content_type: str) -> bool:
    """Identify an HTML gateway page before reporting a JSON parse error."""
    stripped = body.lstrip().lower()
    return "text/html" in content_type.lower() or stripped.startswith(("<!doctype html", "<html"))


def _looks_truncated(body: str, error: json.JSONDecodeError) -> bool:
    """Use only parser position to distinguish an incomplete tail from bad JSON."""
    stripped = body.rstrip()
    return (
        not stripped
        or error.msg.startswith("Unterminated")
        or error.pos >= max(0, len(stripped) - 1)
    )


def _is_api_error_document(document: dict[str, Any]) -> bool:
    """Recognize common JSON error envelopes without exposing their text."""
    return any(key in document for key in ("Error Message", "error", "Error", "message"))
