"""ECB Data Portal access point (C1 P0).

The ECB SDMX web service is public (no key) and speaks CSV, so - like
fred - this provider needs no SDK and no transport package: a thin httpx
call with a monkeypatch seam. Window parameters are the SDMX camelCase
``startPeriod``/``endPeriod`` (verified live: the lowercase variant is
silently ignored and returns the full history).

Clean-room note (design §1.3): only the ECB Data Portal's public
interface is referenced, no OpenBB code was consulted.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from datetime import date

ECB_DEFAULT_BASE_URL = "https://data-api.ecb.europa.eu"


class EcbProviderError(RuntimeError):
    """Stable failures of the ecb provider adapter."""

    def __init__(self, code: str) -> None:
        """Store the stable failure code (and use it as the message).

        Args:
            code: One of the provider's stable codes, for example
                ``ECB_HTTP_ERROR``.
        """
        self.code = code
        super().__init__(code)


def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    """Single GET returning ``(status, text)``; monkeypatched in tests."""
    response = httpx.get(url, params=params, timeout=timeout if timeout is not None else 30.0)
    return response.status_code, response.text


def fetch_observations(
    series_key: str,
    *,
    start: date | None,
    end: date | None,
    timeout: float | None,
) -> list[dict[str, Any]]:
    """Fetch one series' observations as CSV rows.

    Args:
        series_key: Upstream series key (for example
            ``ICP/M.U2.N.000000.4.ANR``).
        start: Inclusive start period.
        end: Inclusive end period.
        timeout: Optional request timeout override.

    Returns:
        Parsed CSV dicts; ``TIME_PERIOD``/``OBS_VALUE`` as published and
        ``KEY`` carrying the dotted series identifier.

    Raises:
        EcbProviderError: Non-200 upstream status (``ECB_HTTP_ERROR``), a
            body without the expected CSV columns
            (``ECB_BAD_RESPONSE``).
    """
    from opendata.core.config import get_settings

    base_url = get_settings().ecb_api_base_url or ECB_DEFAULT_BASE_URL
    params = {"format": "csvdata"}
    if start is not None:
        params["startPeriod"] = start.isoformat()
    if end is not None:
        params["endPeriod"] = end.isoformat()
    status, text = _http_get(f"{base_url}/service/data/{series_key}", params, timeout)
    if status != 200:
        raise EcbProviderError("ECB_HTTP_ERROR")
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and not {"KEY", "TIME_PERIOD", "OBS_VALUE"} <= set(rows[0]):
        raise EcbProviderError("ECB_BAD_RESPONSE")
    return rows
