"""Key-safe, bounded transport for Congress.gov v3 and its official documents."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
)
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.protocol import FetchContext
    from opendata.data.providers._engine.http_json import HttpGet

API_ROOT = "https://api.congress.gov/v3"
API_HOSTS = frozenset({"api.congress.gov"})
DOCUMENT_HOSTS = frozenset({"www.congress.gov", "congress.gov"})
JSON_RESPONSE_LIMIT = 8 * 1024 * 1024
DOCUMENT_RESPONSE_LIMIT = 20 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 20.0


class CongressGovProviderError(ProviderEngineError):
    """Sanitized Congress.gov failures with a stable, attributable error code.

    Congress.gov refusals are attributable engine refusals: they carry the stable ``code``, the
    response ``status`` when one was observed, and the sanitized ``url`` of the request that
    produced them. Parsing here rejects a whole published record (a collection published without
    its pagination, a text version with no format links), so the column-detail faces stay empty
    by design and the code is the attribution.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
        rejected: Sequence[str] = (),
        missing: Sequence[str] = (),
        invalid: Sequence[str] = (),
    ) -> None:
        """Store only stable fields; never copy a body, header, or API key."""
        super().__init__(
            code,
            status=status,
            url=_safe_endpoint(url) if url is not None else None,
            rejected=rejected,
            missing=missing,
            invalid=invalid,
        )


class CongressGovLimitError(CongressGovProviderError):
    """A caller-selected response or document bound would be exceeded."""

    def __init__(self, code: str, *, maximum: int) -> None:
        self.maximum = maximum
        super().__init__(code)


def _safe_endpoint(url: str) -> str:
    """Drop any query and fragment from a URL that a failure message may repeat."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _seam_body(response: HttpResponse) -> bytes:
    """Return the body bytes the governed client would have handed back for one seam response.

    The governed client hands Congress.gov parsers ``content`` bytes; the repository's offline
    witness seam has no raw-text carrier, so it supplies the same document losslessly: as
    ``payload``, as the list of a text file's own source lines, or as the JSON document
    re-encoded to the bytes a JSON response would have had.
    """
    if isinstance(response.payload, bytes):
        return response.payload
    if isinstance(response.payload, str):
        return response.payload.encode("utf-8")
    document = response.document
    if isinstance(document, bytes):
        return document
    if isinstance(document, str):
        return document.encode("utf-8")
    if isinstance(document, list) and all(isinstance(line, str) for line in document):
        return "\n".join(document).encode("utf-8")
    return json.dumps(document).encode("utf-8")


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
    transport: HttpGet | None = None,
) -> dict[str, Any] | None:
    """Fetch one JSON API resource through the shared client or an offline witness seam.

    ``transport`` is the repository's own offline seam: a fetcher that receives one answers
    every request from it instead of the governed HTTP client, and no request leaves the process.
    """
    _validate_api_path(path)
    key = require_api_key()
    request_params = {**params, "format": "json", "api_key": key}
    timeout = (
        DEFAULT_TIMEOUT_SECONDS
        if context.timeout is None
        else min(DEFAULT_TIMEOUT_SECONDS, context.timeout)
    )
    if transport is not None:
        response = transport(
            f"{API_ROOT}{path}",
            request_params,
            timeout=timeout,
            source="congress_gov",
            headers={"Accept": "application/json"},
        )
        status = response.status
        if allow_not_found and status == 404:
            return None
        if type(status) is not int:
            raise CongressGovProviderError("CONGRESS_GOV_TRANSPORT_ERROR")
        body = _seam_body(response)
    else:
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

        status = cast("int", getattr(response, "status_code", None))
        if type(status) is not int:
            raise CongressGovProviderError("CONGRESS_GOV_TRANSPORT_ERROR")
        if allow_not_found and status == 404:
            return None
        body = getattr(response, "content", b"")
    if not 200 <= status < 300:
        raise CongressGovProviderError("CONGRESS_GOV_HTTP_ERROR", status=status)
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


def get_document(
    url: str, *, context: FetchContext, transport: HttpGet | None = None
) -> tuple[bytes, str]:
    """Download one bounded file published under an official Congress.gov host.

    With an offline witness seam installed the bytes come from that seam's response instead of the
    governed HTTP client; every bound and check below still applies.
    """
    _validate_document_url(url)
    timeout = (
        DEFAULT_TIMEOUT_SECONDS
        if context.timeout is None
        else min(DEFAULT_TIMEOUT_SECONDS, context.timeout)
    )
    headers = {"Accept": "application/pdf, application/xml, text/xml, text/html"}
    if transport is not None:
        response = transport(url, {}, timeout=timeout, source="congress_gov", headers=headers)
        status = response.status
        if type(status) is not int:
            raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TRANSPORT_ERROR")
        body = _seam_body(response)
    else:
        try:
            response = get_shared_http_client().get(
                url,
                headers=headers,
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

        status = cast("int", getattr(response, "status_code", None))
        if type(status) is not int:
            raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TRANSPORT_ERROR")
        body = cast("bytes", getattr(response, "content", None))
    if not 200 <= status < 300:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_HTTP_ERROR", status=status)
    if not isinstance(body, bytes):
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_BAD_BODY", status=status)
    if not body:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_EMPTY", status=status)
    if len(body) > DOCUMENT_RESPONSE_LIMIT:
        raise CongressGovProviderError("CONGRESS_GOV_DOCUMENT_TOO_LARGE", status=status)
    response_headers = getattr(response, "headers", {})
    content_type = "application/octet-stream"
    if isinstance(response_headers, Mapping):
        candidate = response_headers.get("Content-Type")
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
