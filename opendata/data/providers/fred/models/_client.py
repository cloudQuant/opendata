"""FRED web service access point (C1 P0).

FRED is a plain JSON REST service, so unlike fuyao/yfinance this provider
needs no SDK and no transport package: a thin httpx call lives here. The
API key comes from ``FRED_API_KEY`` (environment first, then settings)
and a missing key fails closed with a stable code (R2: the provider is
implemented before the key exists).

Clean-room note (design §1.3): only FRED's public interface is
referenced, no OpenBB code was consulted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from datetime import date

FRED_DEFAULT_BASE_URL = "https://api.stlouisfed.org"


class FredProviderError(RuntimeError):
    """Stable failures of the fred provider adapter."""

    def __init__(self, code: str) -> None:
        """Store the stable failure code (and use it as the message).

        Args:
            code: One of the provider's stable codes, for example
                ``FRED_API_KEY_MISSING``.
        """
        self.code = code
        super().__init__(code)


def require_api_key() -> str:
    """Return the FRED API key.

    Returns:
        The key from ``FRED_API_KEY`` or the app settings.

    Raises:
        FredProviderError: No key is configured (``FRED_API_KEY_MISSING``).
    """
    import os

    key = os.environ.get("FRED_API_KEY")
    if key and key.strip():
        return key.strip()
    from opendata.core.config import get_settings

    key = get_settings().fred_api_key
    if key and key.strip():
        return key.strip()
    raise FredProviderError("FRED_API_KEY_MISSING")


def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    """Single GET returning ``(status, text)``; monkeypatched in tests."""
    response = httpx.get(url, params=params, timeout=timeout if timeout is not None else 30.0)
    return response.status_code, response.text


def fetch_observations(
    series_id: str,
    *,
    start: date | None,
    end: date | None,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch one series' observations from the FRED web service.

    Args:
        series_id: Upstream series identifier (for example ``CPIAUCSL``).
        start: Inclusive observation start date.
        end: Inclusive observation end date.
        timeout: Optional request timeout override.

    Returns:
        The raw observation dicts (``date``/``value`` as published).

    Raises:
        FredProviderError: Key missing (``FRED_API_KEY_MISSING``), non-200
            upstream status (``FRED_HTTP_ERROR``), or a body that is not
            the expected JSON document (``FRED_BAD_RESPONSE``).
    """
    from opendata.core.config import get_settings

    base_url = get_settings().fred_api_base_url or FRED_DEFAULT_BASE_URL
    params = {
        "series_id": series_id.strip(),
        "api_key": require_api_key(),
        "file_type": "json",
    }
    if start is not None:
        params["observation_start"] = start.isoformat()
    if end is not None:
        params["observation_end"] = end.isoformat()
    status, text = _http_get(f"{base_url}/fred/series/observations", params, timeout)
    if status != 200:
        raise FredProviderError("FRED_HTTP_ERROR")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FredProviderError("FRED_BAD_RESPONSE") from exc
    observations = document.get("observations") if isinstance(document, dict) else None
    if not isinstance(observations, list):
        raise FredProviderError("FRED_BAD_RESPONSE")
    return observations
