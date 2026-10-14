"""Governed public-source transport shared by Federal Reserve iteration-2 fetchers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.http_client import HttpFetchError, get_shared_http_client

if TYPE_CHECKING:
    from requests import Response

SOURCE = "federal_reserve"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class FederalReserveSourceError(ValueError):
    """Sanitized failure while reading or interpreting a public Fed source."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Create a stable source error without retaining upstream response content."""
        self.code = code
        self.status = status
        detail = "" if status is None else f" status={status}"
        super().__init__(code + detail)


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


def get_json(url: str, timeout: float | None = None) -> dict[str, object]:
    """Read one finite JSON object from an official endpoint."""
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
