"""Governed EIA public-file and API transport helpers."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_ALLOWED_HOSTS = frozenset({"api.eia.gov", "ir.eia.gov", "www.eia.gov"})


class EIAProviderError(RuntimeError):
    """Sanitized EIA transport, credential, or schema failure."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


def get_bytes(
    url: str,
    *,
    params: dict[str, str] | None,
    context: FetchContext,
) -> tuple[bytes, str]:
    """GET one official EIA response using the shared governed client."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in _ALLOWED_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise EIAProviderError("EIA_URL_REJECTED")
    timeout = min(DEFAULT_TIMEOUT_SECONDS, context.timeout or DEFAULT_TIMEOUT_SECONDS)
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=timeout,
            source="eia",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise EIAProviderError("EIA_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise EIAProviderError("EIA_TRANSPORT_ERROR") from None
    status = response.status_code
    if not 200 <= status < 300:
        raise EIAProviderError("EIA_HTTP_ERROR", status=status)
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise EIAProviderError("EIA_RESPONSE_TOO_LARGE")
    return body, response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def get_json(url: str, *, params: dict[str, str], context: FetchContext) -> object:
    """Decode an EIA API v2 JSON response and reject error-shaped success bodies."""
    body, _content_type = get_bytes(url, params=params, context=context)
    try:
        document: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        raise EIAProviderError("EIA_BAD_JSON") from None
    if not isinstance(document, dict):
        raise EIAProviderError("EIA_BAD_RESPONSE_SHAPE")
    if document.get("error"):
        raise EIAProviderError("EIA_API_ERROR")
    response = document.get("response")
    if not isinstance(response, dict):
        raise EIAProviderError("EIA_BAD_RESPONSE_SHAPE")
    return response


def api_key() -> str:
    """Read only the documented EIA_API_KEY environment variable."""
    value = os.environ.get("EIA_API_KEY")
    if not value or not value.strip():
        raise EIAProviderError("EIA_API_KEY_MISSING")
    return value.strip()
