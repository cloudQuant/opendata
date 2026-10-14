"""Governed public-source transport shared by Federal Reserve iteration-2 fetchers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import ProviderEngineError

if TYPE_CHECKING:
    from requests import Response

    from opendata.data.providers._engine.http_json import HttpGet

SOURCE = "federal_reserve"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class FederalReserveSourceError(ProviderEngineError, ValueError):
    """Sanitized failure while reading or interpreting a public Fed source.

    It carries the engine's attributable face (``code``, ``status``, ``url``, ``rejected``,
    ``missing``, ``invalid``) so a witness probe can judge a refusal by its typed detail, and it
    stays a ``ValueError`` because every published catch site and test of this provider's source
    errors reads it as one.
    """

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Create a stable source error without retaining upstream response content."""
        super().__init__(code, status=status)


def _get(url: str, timeout: float | None) -> Response:
    """Make one bounded GET through the governed shared HTTP client."""
    try:
        response = get_shared_http_client().get(
            url,
            timeout=timeout if timeout is not None else 30.0,
            source=SOURCE,
        )
    except HttpFetchError as exc:
        raise FederalReserveSourceError("FEDERAL_RESERVE_HTTP_ERROR", status=exc.status) from None
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise FederalReserveSourceError("FEDERAL_RESERVE_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise FederalReserveSourceError("FEDERAL_RESERVE_HTTP_ERROR", status=status)
    return response


def _document_through_seam(
    url: str, timeout: float | None, transport: HttpGet
) -> dict[str, object]:
    """Read one JSON object through the offline witness seam instead of the governed client.

    The seam is the engine's own :data:`~opendata.data.providers._engine.http_json.HttpGet`
    signature, so a witness hands back an :class:`HttpResponse` and no leg can reach a socket.
    The status bounds are the same checks :func:`_get` runs on a real response.
    """
    response = transport(
        url,
        {},
        timeout=timeout if timeout is not None else 30.0,
        source=SOURCE,
        headers=None,
    )
    status = getattr(response, "status", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise FederalReserveSourceError("FEDERAL_RESERVE_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise FederalReserveSourceError("FEDERAL_RESERVE_HTTP_ERROR", status=status)
    document = response.document
    if not isinstance(document, dict):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_JSON_SHAPE")
    return document


def get_json(
    url: str, timeout: float | None = None, *, transport: HttpGet | None = None
) -> dict[str, object]:
    """Read one finite JSON object from an official endpoint."""
    if transport is not None:
        return _document_through_seam(url, timeout, transport)
    response = _get(url, timeout)
    content = getattr(response, "content", None)
    if isinstance(content, bytes) and len(content) > MAX_RESPONSE_BYTES:
        raise FederalReserveSourceError("FEDERAL_RESERVE_RESPONSE_TOO_LARGE")
    try:
        document = response.json()
    except Exception:
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_JSON") from None
    if not isinstance(document, dict):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_JSON_SHAPE")
    return document


def get_bytes(url: str, timeout: float | None = None) -> bytes:
    """Read a bounded binary response, such as an official Excel workbook."""
    response = _get(url, timeout)
    body = getattr(response, "content", None)
    if not isinstance(body, bytes) or not body:
        raise FederalReserveSourceError("FEDERAL_RESERVE_EMPTY_RESPONSE")
    if len(body) > MAX_RESPONSE_BYTES:
        raise FederalReserveSourceError("FEDERAL_RESERVE_RESPONSE_TOO_LARGE")
    return body


def get_text(url: str, timeout: float | None = None) -> str:
    """Read a bounded UTF-8 source page such as the FOMC document index."""
    response = _get(url, timeout)
    body = getattr(response, "content", None)
    if isinstance(body, bytes) and len(body) > MAX_RESPONSE_BYTES:
        raise FederalReserveSourceError("FEDERAL_RESERVE_RESPONSE_TOO_LARGE")
    text = getattr(response, "text", None)
    if not isinstance(text, str) or not text:
        raise FederalReserveSourceError("FEDERAL_RESERVE_EMPTY_RESPONSE")
    if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise FederalReserveSourceError("FEDERAL_RESERVE_RESPONSE_TOO_LARGE")
    return text


__all__ = [
    "FederalReserveSourceError",
    "get_bytes",
    "get_json",
    "get_text",
]
