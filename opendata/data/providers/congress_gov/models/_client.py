"""Key-safe, bounded transport for Congress.gov v3 and its official documents."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext

API_ROOT = "https://api.congress.gov/v3"
API_HOSTS = frozenset({"api.congress.gov"})
DOCUMENT_HOSTS = frozenset({"www.congress.gov", "congress.gov"})
JSON_RESPONSE_LIMIT = 8 * 1024 * 1024
DOCUMENT_RESPONSE_LIMIT = 20 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0


class CongressGovProviderError(RuntimeError):
    """Sanitized Congress.gov failures with a stable error code."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        detail = "" if status is None else f" status={status}"
        super().__init__(code + detail)


class CongressGovLimitError(CongressGovProviderError):
    """A caller-selected response or document bound would be exceeded."""

    def __init__(self, code: str, *, maximum: int) -> None:
        self.maximum = maximum
        super().__init__(code)


def require_api_key() -> str:
    """Return the configured public API key without reading any live configuration file."""
    key = os.environ.get("CONGRESS_GOV_API_KEY", "").strip()
    if not key:
        raise CongressGovProviderError("CONGRESS_GOV_API_KEY_MISSING")
    return key


def get_json(
    path: str,
    *,
    params: dict[str, str],
    context: FetchContext,
    allow_not_found: bool = False,
) -> dict[str, Any] | None:
    """Fetch one JSON API resource through the shared governed HTTP client."""
    _validate_api_path(path)
    key = require_api_key()
    request_params = {**params, "format": "json", "api_key": key}
    timeout = (
        DEFAULT_TIMEOUT_SECONDS
        if context.timeout is None
        else min(DEFAULT_TIMEOUT_SECONDS, context.timeout)
    )
    try:
        response = get_shared_http_client().get(
            f"{API_ROOT}{path}",
            params=request_params,
            headers={"Accept": "application/json"},
            timeout=(min(5.0, timeout), timeout),
            source="congress_gov",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        if allow_not_found and exc.status == 404:
            return None
        raise CongressGovProviderError("CONGRESS_GOV_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise CongressGovProviderError("CONGRESS_GOV_TRANSPORT_ERROR") from None

    status = getattr(response, "status_code", None)
    if type(status) is not int:
        raise CongressGovProviderError("CONGRESS_GOV_TRANSPORT_ERROR")
    if allow_not_found and status == 404:
        return None
    if not 200 <= status < 300:
        raise CongressGovProviderError("CONGRESS_GOV_HTTP_ERROR", status=status)
    body = getattr(response, "content", b"")
    if not isinstance(body, bytes):
        raise CongressGovProviderError("CONGRESS_GOV_TRANSPORT_ERROR", status=status)
    if len(body) > JSON_RESPONSE_LIMIT:
        raise CongressGovProviderError("CONGRESS_GOV_RESPONSE_TOO_LARGE", status=status)
    try:
        document: object = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        raise CongressGovProviderError("CONGRESS_GOV_BAD_JSON", status=status) from None
    if not isinstance(document, dict):
        raise CongressGovProviderError("CONGRESS_GOV_BAD_SHAPE", status=status)
    if any(key in document for key in ("error", "errors", "fault")):
        raise CongressGovProviderError("CONGRESS_GOV_API_ERROR", status=status)
    return document


def get_document(url: str, *, context: FetchContext) -> tuple[bytes, str]:
    """Download one bounded file published under an official Congress.gov host."""
    _validate_document_url(url)
    timeout = (
        DEFAULT_TIMEOUT_SECONDS
        if context.timeout is None
        else min(DEFAULT_TIMEOUT_SECONDS, context.timeout)
    )
    try:
        response = get_shared_http_client().get(
            url,
            headers={"Accept": "application/pdf, application/xml, text/xml, text/html"},
            timeout=(min(5.0, timeout), timeout),
            source="congress_gov",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise CongressGovProviderError(
            "CONGRESS_GOV_DOCUMENT_HTTP_ERROR", status=exc.status
        ) from None
    except Exception:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TRANSPORT_ERROR") from None

    status = getattr(response, "status_code", None)
    if type(status) is not int:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_HTTP_ERROR", status=status)
    body = getattr(response, "content", None)
    if not isinstance(body, bytes):
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_BAD_BODY", status=status)
    if not body:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_EMPTY", status=status)
    if len(body) > DOCUMENT_RESPONSE_LIMIT:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TOO_LARGE", status=status)
    headers = getattr(response, "headers", {})
    content_type = "application/octet-stream"
    if isinstance(headers, Mapping):
        candidate = headers.get("Content-Type")
        if isinstance(candidate, str) and candidate.strip():
            content_type = candidate.split(";", 1)[0].strip().lower()
    return body, content_type


def _validate_api_path(path: str) -> None:
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or "?" in path
        or "#" in path
        or ".." in path.split("/")
        or any(ord(character) < 33 or ord(character) == 127 for character in path)
    ):
        raise CongressGovProviderError("CONGRESS_GOV_PATH_REJECTED")


def _validate_document_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except (TypeError, ValueError):
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_URL_REJECTED") from None
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        parts.scheme != "https"
        or parts.hostname is None
        or parts.hostname.lower() not in DOCUMENT_HOSTS
        or parts.username is not None
        or parts.password is not None
        or port not in (None, 443)
        or parts.fragment
        or set(query).difference({"format"})
        or any(
            value.lower() not in {"pdf", "xml", "html", "htm", "txt"}
            for values in query.values()
            for value in values
        )
        or len(parts.path) > 2048
    ):
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_URL_REJECTED")
