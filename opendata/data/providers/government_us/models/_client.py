"""Bounded source transport helpers for public government endpoints."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.protocol import FetchContext  # noqa: TC001 - get_type_hints needs runtime name
from opendata.data.request_budget import RequestBudgetError

JSON_RESPONSE_LIMIT = 8 * 1024 * 1024
DOCUMENT_RESPONSE_LIMIT = 20 * 1024 * 1024
_DEFAULT_TIMEOUT = 20.0


class GovernmentUSProviderError(RuntimeError):
    """Sanitized source error; response bodies and query values are never kept."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


def _validate_url(url: str, hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.hostname.lower() not in hosts
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise GovernmentUSProviderError("GOV_US_URL_REJECTED")


def get_bytes(
    url: str,
    *,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
    context: FetchContext,
    source: str,
    allowed_hosts: frozenset[str],
    maximum_bytes: int = JSON_RESPONSE_LIMIT,
) -> tuple[bytes, str, dict[str, str]]:
    """Fetch one HTTPS response through the shared governed HTTP client."""
    _validate_url(url, allowed_hosts)
    timeout = (
        _DEFAULT_TIMEOUT if context.timeout is None else min(_DEFAULT_TIMEOUT, context.timeout)
    )
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            headers=headers,
            timeout=(min(5.0, timeout), timeout),
            source=source,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise GovernmentUSProviderError("GOV_US_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise GovernmentUSProviderError("GOV_US_TRANSPORT_ERROR") from None

    status = response.status_code
    if not 200 <= status < 300:
        raise GovernmentUSProviderError("GOV_US_HTTP_ERROR", status=status)
    body = response.content
    if len(body) > maximum_bytes:
        raise GovernmentUSProviderError("GOV_US_RESPONSE_TOO_LARGE")
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return body, content_type, dict(response.headers)


def get_json(
    url: str,
    *,
    params: dict[str, str] | None,
    headers: dict[str, str] | None,
    context: FetchContext,
    allowed_hosts: frozenset[str],
) -> tuple[object, dict[str, str]]:
    """Decode a bounded JSON response without exposing source error text."""
    body, content_type, response_headers = get_bytes(
        url,
        params=params,
        headers={"Accept": "application/json", **(headers or {})},
        context=context,
        source="government_us",
        allowed_hosts=allowed_hosts,
        maximum_bytes=JSON_RESPONSE_LIMIT,
    )
    if "html" in content_type or body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise GovernmentUSProviderError("GOV_US_HTML_RESPONSE")
    try:
        document: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise GovernmentUSProviderError("GOV_US_BAD_JSON") from None
    return document, response_headers


def array_rows(document: object) -> list[dict[str, Any]]:
    """Accept the documented array or FiscalData's JSON:API data array."""
    rows: object = document
    if isinstance(document, dict) and "data" in document:
        rows = document["data"]
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise GovernmentUSProviderError("GOV_US_BAD_SHAPE")
    return rows
