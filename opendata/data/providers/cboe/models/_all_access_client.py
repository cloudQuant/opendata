"""Bounded client for the documented Cboe LiveVol All Access JSON API."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import HttpGet, ProviderEngineError
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opendata.data.protocol import FetchContext

CBOE_ALL_ACCESS_BASE_URL = "https://api.livevol.com/v1/delayed/allaccess"
# B105 exception: this is an environment-variable name; os.environ.get below reads the secret.
CBOE_ACCESS_TOKEN_ENV = "CBOE_LIVEVOL_ACCESS_TOKEN"  # noqa: S105  # nosec B105
JSON_RESPONSE_LIMIT = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
_ALLOWED_PATHS = frozenset(
    {
        "/market/underlying-quotes",
        "/market/option-and-underlying-quotes",
        "/reference/symbols",
        "/reference/trading-days",
    }
)


class CboeAllAccessError(ProviderEngineError):
    """Sanitized failure from the authenticated Cboe All Access API.

    It is a ``ProviderEngineError`` so an All Access refusal carries the engine's typed attribution
    face (``code``/``status``/``url``/``rejected``/``missing``/``invalid``) that every other cboe
    model's refusal already publishes; this route simply names no column, because the All Access
    guards refuse a whole record shape rather than one declared column.
    """

    def __init__(self, code: str, *, status: int | None = None) -> None:
        super().__init__(code, status=status)


def require_access_token() -> str:
    """Read an externally provisioned bearer token without initiating OAuth."""
    token = os.environ.get(CBOE_ACCESS_TOKEN_ENV, "").strip()
    if not token:
        raise CboeAllAccessError("CBOE_LIVEVOL_ACCESS_TOKEN_MISSING")
    return token


def get_json(
    path: str,
    *,
    params: Mapping[str, str],
    context: FetchContext,
    transport: HttpGet | None = None,
) -> object:
    """Fetch one documented API path through the governed client, or an injected offline seam.

    ``transport`` is the witness seam the provider's handwritten adapters publish as
    ``http_transport``: it answers with the already-decoded JSON document, the same face the
    engine's own governed GET returns. Every guard after the byte boundary -- status mapping,
    the API-error envelope check, the dict/list shape requirement -- runs unchanged on what the
    seam published, so an injected transport narrows no rule.
    """
    if path not in _ALLOWED_PATHS:
        raise CboeAllAccessError("CBOE_LIVEVOL_PATH_REJECTED")
    token = require_access_token()
    timeout = (
        DEFAULT_TIMEOUT_SECONDS
        if context.timeout is None
        else min(DEFAULT_TIMEOUT_SECONDS, context.timeout)
    )
    url = f"{CBOE_ALL_ACCESS_BASE_URL}{path}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    if transport is not None:
        response = transport(
            url,
            dict(params),
            timeout=min(5.0, timeout),
            source="cboe",
            headers=headers,
        )
        if isinstance(response.status, bool) or not isinstance(response.status, int):
            raise CboeAllAccessError("CBOE_LIVEVOL_TRANSPORT_ERROR")
        if not 200 <= response.status < 300:
            raise CboeAllAccessError(_status_code(response.status), status=response.status)
        payload = response.document
    else:
        payload = _live_json(url, params, headers, timeout)

    if isinstance(payload, dict) and any(key in payload for key in ("error", "errors", "fault")):
        raise CboeAllAccessError("CBOE_LIVEVOL_API_ERROR")
    if not isinstance(payload, (dict, list)):
        raise CboeAllAccessError("CBOE_LIVEVOL_BAD_SHAPE")
    return payload


def _live_json(
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout: float,
) -> object:
    """Read one All Access body through the governed shared HTTP client."""
    try:
        response = get_shared_http_client().get(
            url,
            params=dict(params),
            headers=dict(headers),
            timeout=(min(5.0, timeout), timeout),
            source="cboe",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CboeAllAccessError(_status_code(exc.status), status=exc.status) from None
    except Exception:
        raise CboeAllAccessError("CBOE_LIVEVOL_TRANSPORT_ERROR") from None

    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise CboeAllAccessError("CBOE_LIVEVOL_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise CboeAllAccessError(_status_code(status), status=status)
    body = getattr(response, "content", None)
    if not isinstance(body, bytes):
        raise CboeAllAccessError("CBOE_LIVEVOL_BAD_BODY", status=status)
    if len(body) > JSON_RESPONSE_LIMIT:
        raise CboeAllAccessError("CBOE_LIVEVOL_RESPONSE_TOO_LARGE", status=status)
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise CboeAllAccessError("CBOE_LIVEVOL_BAD_JSON", status=status) from None


def require_rows(
    payload: object,
    *,
    code: str = "CBOE_LIVEVOL_BAD_SHAPE",
) -> tuple[dict[str, Any], ...]:
    """Require a JSON array of objects and normalize it to an immutable tuple."""
    if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
        raise CboeAllAccessError(code)
    return tuple(payload)


def _status_code(status: int | None) -> str:
    if status == 401:
        return "CBOE_LIVEVOL_AUTH_ERROR"
    if status == 403:
        return "CBOE_LIVEVOL_FORBIDDEN"
    if status == 404:
        return "CBOE_LIVEVOL_NOT_FOUND"
    if status == 429:
        return "CBOE_LIVEVOL_RATE_LIMITED"
    if status is not None and status >= 500:
        return "CBOE_LIVEVOL_UPSTREAM_ERROR"
    return "CBOE_LIVEVOL_HTTP_ERROR"
