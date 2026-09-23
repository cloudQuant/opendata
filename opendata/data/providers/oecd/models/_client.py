"""OECD SDMX API access point (C1 P0).

The OECD SDMX endpoint is public (no key) and speaks CSV, so - like
fred/ecb/imf - this provider needs no SDK and no transport package: a
thin httpx call with a monkeypatch seam. Series keys are position-
strict eight-dimension dotted strings (verified live: a value in the
wrong slot is a 403, an aggregate that does not exist is a 404).

Clean-room note (design §1.3): only the OECD SDMX API's public interface
is referenced, no OpenBB code was consulted.
"""

from __future__ import annotations

import csv
import io
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from datetime import date

OECD_DEFAULT_BASE_URL = "https://sdmx.oecd.org/public/rest"


class OecdProviderError(RuntimeError):
    """Stable failures of the oecd provider adapter."""

    def __init__(self, code: str) -> None:
        """Store the stable failure code (and use it as the message).

        Args:
            code: One of the provider's stable codes, for example
                ``OECD_HTTP_ERROR``.
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
        series_key: Full position-strict key (for example
            ``GBR.M.HICP.CPI.PC.CP09.N.G1`` on the HICP dataflow).
        start: Inclusive start period.
        end: Inclusive end period.
        timeout: Optional request timeout override.

    Returns:
        Parsed CSV dicts; ``TIME_PERIOD``/``OBS_VALUE`` as published.

    Raises:
        OecdProviderError: Non-200 upstream status (``OECD_HTTP_ERROR``) or
            a body without the expected CSV columns
            (``OECD_BAD_RESPONSE``).
    """
    from opendata.core.config import get_settings

    flow = get_settings().oecd_cpi_flow or "OECD.SDD.TPS,DSD_PRICES@DF_PRICES_HICP"
    base_url = get_settings().oecd_api_base_url or OECD_DEFAULT_BASE_URL
    params = {"format": "csvfile"}
    if start is not None:
        params["startPeriod"] = start.isoformat()
    if end is not None:
        params["endPeriod"] = end.isoformat()
    status, text = _http_get(f"{base_url}/data/{flow}/{series_key}", params, timeout)
    if status != 200:
        raise OecdProviderError("OECD_HTTP_ERROR")
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and not {"REF_AREA", "TIME_PERIOD", "OBS_VALUE"} <= set(rows[0]):
        raise OecdProviderError("OECD_BAD_RESPONSE")
    return rows
