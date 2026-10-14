"""Bounded, credential-free access to the CFTC Socrata public datasets."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

CFTC_HOST = "publicreporting.cftc.gov"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0


class CFTCProviderError(RuntimeError):
    """Sanitized CFTC transport or response failure."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(code if status is None else f"{code} status={status}")


def get_json(
    url: str,
    *,
    params: dict[str, str],
    context: FetchContext,
) -> object:
    """GET one public CFTC page through the governed HTTP client."""
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != CFTC_HOST
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.query
        or parts.fragment
    ):
        raise CFTCProviderError("CFTC_URL_REJECTED")
    timeout = min(DEFAULT_TIMEOUT_SECONDS, context.timeout or DEFAULT_TIMEOUT_SECONDS)
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=timeout,
            source="cftc",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CFTCProviderError("CFTC_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise CFTCProviderError("CFTC_TRANSPORT_ERROR") from None
    status = response.status_code
    if not 200 <= status < 300:
        raise CFTCProviderError("CFTC_HTTP_ERROR", status=status)
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise CFTCProviderError("CFTC_RESPONSE_TOO_LARGE")
    try:
        document: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        raise CFTCProviderError("CFTC_BAD_JSON") from None
    return document


def array_rows(document: object) -> list[dict[str, Any]]:
    """Require Socrata's documented JSON array of rows."""
    if not isinstance(document, list) or any(not isinstance(row, dict) for row in document):
        raise CFTCProviderError("CFTC_BAD_RESPONSE_SHAPE")
    return document
