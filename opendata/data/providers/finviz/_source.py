"""Public Finviz HTML transport with conservative local pacing."""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.http_client import (
    GovernedHttpClient,
    HttpClientConfig,
    HttpFetchError,
)
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Mapping

BASE_URL = "https://finviz.com"
USER_AGENT = "opendata/0.1 (Finviz public-page adapter)"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class FinvizSourceError(RuntimeError):
    """A public-page transport or response failure with a stable code."""


class FinvizRateLimitError(FinvizSourceError):
    """Finviz declined a request with HTTP 429; the caller must not retry blindly."""


_HTTP_CLIENT = GovernedHttpClient(
    HttpClientConfig(
        connect_timeout=DEFAULT_TIMEOUT_SECONDS,
        read_timeout=DEFAULT_TIMEOUT_SECONDS,
        max_attempts=1,
        rate_limit_per_host=1.0,
        rate_burst=1,
        max_concurrency_per_host=1,
        rate_max_wait=30.0,
        default_headers={
            "Accept": "text/html,application/xhtml+xml",
            "User-Agent": USER_AGENT,
        },
    )
)


def request_html(
    path: str,
    params: Mapping[str, str],
    *,
    timeout: float | None = None,
) -> bytes:
    """Fetch one public Finviz HTML page; never sends credentials or follows custom hosts."""
    if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
        raise ValueError("Finviz path must be an absolute site-local path")
    try:
        response = _HTTP_CLIENT.get(
            f"{BASE_URL}{path}",
            params=params,
            timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
            source="finviz",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as error:
        if error.status == 429:
            raise FinvizRateLimitError("FINVIZ_HTTP_429") from None
        status = "UNKNOWN" if error.status is None else str(error.status)
        raise FinvizSourceError(f"FINVIZ_HTTP_{status}") from None
    except Exception:
        raise FinvizSourceError("FINVIZ_TRANSPORT_ERROR") from None
    content_type = (
        response.headers.get("Content-Type", "").split(";", maxsplit=1)[0].strip().casefold()
    )
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise FinvizSourceError("FINVIZ_UNEXPECTED_CONTENT_TYPE")
    body: bytes = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise FinvizSourceError("FINVIZ_RESPONSE_TOO_LARGE")
    if not body:
        raise FinvizSourceError("FINVIZ_EMPTY_RESPONSE")
    return body
