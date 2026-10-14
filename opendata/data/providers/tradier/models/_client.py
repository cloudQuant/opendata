"""Key-safe, bounded Tradier market-data transport."""

from __future__ import annotations

import os
from typing import Any

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

TRADIER_BASE_URL = "https://api.tradier.com/v1"
# S105 rationale retained: name of the required environment variable.
# B105 exception: this name is not a secret; os.environ.get below reads the secret.
TRADIER_TOKEN_ENV = "TRADIER_TOKEN"  # noqa: S105  # nosec B105
_DEFAULT_TIMEOUT_SECONDS = 30.0


class TradierProviderError(RuntimeError):
    """Sanitized provider error; messages never contain tokens or response bodies."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        requests_made: int = 0,
    ) -> None:
        """Create a stable error with machine-readable fields."""
        self.code = code
        self.status = status
        self.requests_made = requests_made
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


class TradierQueryLimitError(TradierProviderError):
    """A requested fan-out or response exceeds an explicit local bound."""

    def __init__(self, code: str, *, maximum: int, requests_made: int) -> None:
        """Create a bounded-operation refusal without returning partial rows."""
        self.maximum = maximum
        super().__init__(code, requests_made=requests_made)


def require_token() -> str:
    """Return the configured bearer token or fail before creating a transport request."""
    token = os.environ.get(TRADIER_TOKEN_ENV, "")
    if not token.strip():
        raise TradierProviderError("TRADIER_TOKEN_MISSING")
    return token.strip()


def fetch_json(
    path: str,
    params: dict[str, str],
    timeout: float | None,
) -> dict[str, Any]:
    """Fetch one JSON object through the shared governed HTTP client."""
    token = require_token()
    response = None
    try:
        response = get_shared_http_client().get(
            f"{TRADIER_BASE_URL}{path}",
            params=params,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            source="tradier",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise TradierProviderError(
            _status_error_code(exc.status),
            status=exc.status,
        ) from None
    except Exception:
        # Transport exceptions may retain prepared headers or URLs. Do not chain them.
        raise TradierProviderError("TRADIER_TRANSPORT_ERROR") from None

    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise TradierProviderError("TRADIER_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise TradierProviderError(_status_error_code(status), status=status)
    try:
        document: object = response.json()
    except Exception:
        raise TradierProviderError("TRADIER_BAD_JSON", status=status) from None
    if not isinstance(document, dict):
        raise TradierProviderError("TRADIER_BAD_SHAPE", status=status)
    if any(key in document for key in ("error", "errors", "fault", "Error Message")):
        raise TradierProviderError("TRADIER_API_ERROR", status=status)
    return document


def object_rows(
    document: dict[str, Any],
    container_key: str,
    rows_key: str,
    *,
    allow_missing_rows: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Normalize documented XML-to-JSON singleton objects and arrays to records."""
    container = document.get(container_key)
    if not isinstance(container, dict):
        raise TradierProviderError("TRADIER_BAD_SHAPE")
    if rows_key not in container:
        if allow_missing_rows:
            return ()
        raise TradierProviderError("TRADIER_BAD_SHAPE")
    value = container[rows_key]
    if value is None:
        return ()
    if isinstance(value, dict):
        return (value,)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise TradierProviderError("TRADIER_BAD_SHAPE")
    return tuple(value)


def _status_error_code(status: int | None) -> str:
    """Map HTTP status without retaining provider response text."""
    if status == 401:
        return "TRADIER_AUTH_ERROR"
    if status == 403:
        return "TRADIER_FORBIDDEN"
    if status == 404:
        return "TRADIER_NOT_FOUND"
    if status == 429:
        return "TRADIER_RATE_LIMITED"
    if status is not None and status >= 500:
        return "TRADIER_UPSTREAM_ERROR"
    return "TRADIER_HTTP_ERROR"
