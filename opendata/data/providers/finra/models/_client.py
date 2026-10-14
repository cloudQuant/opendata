"""Bounded OAuth and JSON transport for FINRA's public Query API."""

from __future__ import annotations

import base64
import json
import os
import re
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

FIP_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
API_BASE = "https://api.finra.org"
FIP_HOSTS = frozenset({"ews.fip.finra.org"})
API_HOSTS = frozenset({"api.finra.org"})
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PAGE_SIZE = 5_000
MAX_RESULT_ROWS = 50_000
_DEFAULT_TIMEOUT = 20.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,8192}$")


class FinraProviderError(RuntimeError):
    """Stable sanitized provider error that never retains credentials or bodies."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


def _validate_url(url: str, allowed_hosts: frozenset[str]) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.hostname.lower() not in allowed_hosts
        or parts.username is not None
        or parts.password is not None
        or parts.port not in (None, 443)
        or parts.fragment
    ):
        raise FinraProviderError("FINRA_URL_REJECTED")


def _request(
    method: str,
    url: str,
    *,
    context: FetchContext,
    allowed_hosts: frozenset[str],
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    json_body: dict[str, object] | None = None,
) -> tuple[bytes, dict[str, str]]:
    _validate_url(url, allowed_hosts)
    timeout = (
        _DEFAULT_TIMEOUT if context.timeout is None else min(_DEFAULT_TIMEOUT, context.timeout)
    )
    try:
        response = get_shared_http_client().request(
            method,
            url,
            params=params,
            headers=headers,
            timeout=(min(5.0, timeout), timeout),
            source="finra",
            json_body=json_body,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise FinraProviderError("FINRA_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise FinraProviderError("FINRA_TRANSPORT_ERROR") from None

    status = response.status_code
    if not 200 <= status < 300:
        raise FinraProviderError("FINRA_HTTP_ERROR", status=status)
    body = response.content
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise FinraProviderError("FINRA_RESPONSE_TOO_LARGE_OR_INVALID")
    return body, dict(response.headers)


def _parse_json(body: bytes, *, error: str) -> object:
    if body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise FinraProviderError("FINRA_HTML_RESPONSE")
    try:
        return json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise FinraProviderError(error) from None


def access_token(context: FetchContext) -> str:
    """Exchange the official FINRA client credential for one short-lived token."""
    client_id = os.environ.get("FINRA_API_CLIENT_ID", "")
    client_secret = os.environ.get("FINRA_API_CLIENT_SECRET", "")
    if (
        not client_id
        or not client_secret
        or len(client_id) > 512
        or len(client_secret) > 512
        or any(ord(char) < 32 or ord(char) == 127 for char in client_id + client_secret)
    ):
        raise FinraProviderError("FINRA_CREDENTIALS_MISSING_OR_INVALID")

    pair = f"{client_id}:{client_secret}".encode()
    authorization = base64.b64encode(pair).decode("ascii")
    body, _ = _request(
        "POST",
        FIP_URL,
        params={"grant_type": "client_credentials"},
        headers={"Accept": "application/json", "Authorization": f"Basic {authorization}"},
        context=context,
        allowed_hosts=FIP_HOSTS,
    )
    document = _parse_json(body, error="FINRA_TOKEN_BAD_JSON")
    if not isinstance(document, dict):
        raise FinraProviderError("FINRA_TOKEN_BAD_SHAPE")
    token = document.get("access_token")
    token_type = document.get("token_type")
    expires_raw: object = document.get("expires_in")
    if not isinstance(expires_raw, (str, int, float)):
        raise FinraProviderError("FINRA_TOKEN_BAD_SHAPE")
    try:
        expires_in = int(expires_raw)
    except (TypeError, ValueError):
        raise FinraProviderError("FINRA_TOKEN_BAD_SHAPE") from None
    if (
        not isinstance(token, str)
        or _TOKEN_RE.fullmatch(token) is None
        or not isinstance(token_type, str)
        or token_type.casefold() != "bearer"
        or not 1 <= expires_in <= 172_800
    ):
        raise FinraProviderError("FINRA_TOKEN_BAD_SHAPE")
    return token


def get_json(
    url: str,
    *,
    params: dict[str, str] | None,
    token: str,
    context: FetchContext,
) -> tuple[object, dict[str, str]]:
    """Fetch one bounded official FINRA JSON metadata document."""
    body, response_headers = _request(
        "GET",
        url,
        params=params,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Data-API-Version": "1",
        },
        context=context,
        allowed_hosts=API_HOSTS,
    )
    content_type = _header(response_headers, "Content-Type").casefold()
    if "html" in content_type:
        raise FinraProviderError("FINRA_HTML_RESPONSE")
    return _parse_json(body, error="FINRA_BAD_JSON"), response_headers


def post_rows(
    url: str,
    *,
    token: str,
    fields: tuple[str, ...],
    compare_filters: list[dict[str, str]],
    context: FetchContext,
) -> list[dict[str, Any]]:
    """Read all rows in a capped offset window or refuse an incomplete page set."""
    rows: list[dict[str, Any]] = []
    expected_total: int | None = None
    offset = 0
    while offset < MAX_RESULT_ROWS:
        payload: dict[str, object] = {
            "fields": list(fields),
            "compareFilters": compare_filters,
            "limit": MAX_PAGE_SIZE,
            "offset": offset,
            "async": False,
        }
        body, response_headers = _request(
            "POST",
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Data-API-Version": "1",
            },
            json_body=payload,
            context=context,
            allowed_hosts=API_HOSTS,
        )
        document = _parse_json(body, error="FINRA_BAD_JSON")
        if not isinstance(document, list) or any(not isinstance(row, dict) for row in document):
            raise FinraProviderError("FINRA_BAD_ROWS_SHAPE")
        page: list[dict[str, Any]] = document
        if len(page) > MAX_PAGE_SIZE:
            raise FinraProviderError("FINRA_BAD_PAGE_SIZE")
        header_total = _header(response_headers, "Record-Total")
        if not header_total:
            raise FinraProviderError("FINRA_RECORD_TOTAL_MISSING")
        try:
            page_total = int(header_total)
        except ValueError:
            raise FinraProviderError("FINRA_BAD_RECORD_TOTAL") from None
        if page_total < 0 or page_total > 500_000:
            raise FinraProviderError("FINRA_BAD_RECORD_TOTAL")
        if expected_total is not None and page_total != expected_total:
            raise FinraProviderError("FINRA_PAGINATION_TOTAL_CHANGED")
        expected_total = page_total
        if expected_total > MAX_RESULT_ROWS:
            raise FinraProviderError("FINRA_RESULT_LIMIT_EXCEEDED")
        rows.extend(page)
        offset += len(page)
        if expected_total is not None:
            if offset >= expected_total:
                break
            if not page:
                raise FinraProviderError("FINRA_PAGINATION_INCOMPLETE")
        elif len(page) < MAX_PAGE_SIZE or not page:
            break
    else:
        raise FinraProviderError("FINRA_RESULT_LIMIT_EXCEEDED")

    if expected_total is not None and len(rows) != expected_total:
        raise FinraProviderError("FINRA_PAGINATION_INCOMPLETE")
    if expected_total is None and len(rows) >= MAX_RESULT_ROWS:
        raise FinraProviderError("FINRA_RESULT_LIMIT_EXCEEDED")
    return rows


def _header(headers: dict[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value.strip()
    return ""


def array_rows(document: object) -> list[dict[str, Any]]:
    """Require a JSON array of records for the documented FINRA query endpoints."""
    if not isinstance(document, list) or any(not isinstance(row, dict) for row in document):
        raise FinraProviderError("FINRA_BAD_ROWS_SHAPE")
    return document
