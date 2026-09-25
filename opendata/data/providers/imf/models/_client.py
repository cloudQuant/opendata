"""IMF DataMapper access point (C1 P0).

The DataMapper API is public (no key) and speaks plain JSON, so - like
fred/ecb - this provider needs no SDK and no transport package: a thin
httpx call with a monkeypatch seam. The upstream ignores ``periods`` as
a query filter (verified live: a 2023-2025 request returns the full
1980-onwards history), so windowing is applied by the caller.

Clean-room note (design §1.3): only the DataMapper's public interface is
referenced, no OpenBB code was consulted.
"""

from __future__ import annotations

from typing import Any

import httpx

IMF_DEFAULT_BASE_URL = "https://www.imf.org/external/datamapper/api/v1"


class ImfProviderError(RuntimeError):
    """Stable failures of the imf provider adapter.

    The message carries the request that produced the failure, matching
    the convention of the governed client in
    :mod:`opendata.data.http_client`. Health patrol logs one line per
    failure, so a code alone ("IMF_HTTP_ERROR") cannot be attributed to
    an upstream without replaying the call by hand.
    """

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        """Store the stable failure code and the request behind it.

        Args:
            code: One of the provider's stable codes, for example
                ``IMF_HTTP_ERROR``.
            status: Upstream HTTP status, when a response was received.
            url: Requested URL, passed without its query string.
        """
        self.code = code
        self.status = status
        self.url = url
        detail = "" if status is None else f" status={status}"
        detail += "" if url is None else f" url={url}"
        super().__init__(code + detail)


def _http_get(url: str, timeout: float | None) -> tuple[int, str]:
    """Single GET returning ``(status, text)``; monkeypatched in tests."""
    response = httpx.get(url, timeout=timeout if timeout is not None else 30.0)
    return response.status_code, response.text


def fetch_indicator(indicator: str, country: str, *, timeout: float | None) -> dict[str, Any]:
    """Fetch one indicator's country series document.

    Args:
        indicator: DataMapper indicator code (for example ``PCPIPCH``).
        country: ISO3 country code (for example ``USA``).
        timeout: Optional request timeout override.

    Returns:
        The ``values.{indicator}.{country}`` mapping of ``{year: value}``.

    Raises:
        ImfProviderError: Non-200 upstream status (``IMF_HTTP_ERROR``) or a
            body without the expected document shape (``IMF_BAD_RESPONSE``).
    """
    import json

    from opendata.core.config import get_settings

    base_url = get_settings().imf_api_base_url or IMF_DEFAULT_BASE_URL
    url = f"{base_url}/{indicator}/{country}"
    status, text = _http_get(url, timeout)
    if status != 200:
        raise ImfProviderError("IMF_HTTP_ERROR", status=status, url=url)
    try:
        document = json.loads(text)
        series = document["values"][indicator][country]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ImfProviderError("IMF_BAD_RESPONSE", status=status, url=url) from exc
    if not isinstance(series, dict):
        raise ImfProviderError("IMF_BAD_RESPONSE", status=status, url=url)
    return series
