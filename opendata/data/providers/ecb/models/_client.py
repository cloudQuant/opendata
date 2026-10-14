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

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.providers._engine.http_json import ProviderEngineError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from opendata.data.providers._engine.http_json import HttpResponse

ECB_DEFAULT_BASE_URL = "https://data-api.ecb.europa.eu"


class EcbProviderError(ProviderEngineError):
    """Stable, attributable failures of the ecb provider adapter.

    The message carries the request that produced the failure, matching
    the convention of the governed client in
    :mod:`opendata.data.http_client`. Health patrol logs one line per
    failure, so a code alone ("ECB_HTTP_ERROR") cannot be attributed to
    an upstream without replaying the call by hand.
    """


def seam_body(response: HttpResponse) -> bytes:
    """Return the raw response bytes the offline seam hands to the XML parsers.

    ``HttpResponse`` carries a parsed JSON ``document`` and an optional
    ``payload``; an SDMX GenericData body is neither JSON nor a byte string
    an in-memory transport can hand over. A witness therefore publishes the
    body losslessly as the list of its own source lines, and the seam
    rejoins them exactly once here so the production parser still reads the
    bytes a real ECB response would have carried.
    """
    if response.payload is not None:
        raw: bytes | str = response.payload
    elif isinstance(response.document, (list, tuple)):
        raw = "\n".join(str(line) for line in response.document)
    else:
        raw = response.document if isinstance(response.document, str) else ""
    return raw.encode("utf-8") if isinstance(raw, str) else raw


def seam_get(
    transport: Any,  # noqa: ANN401 - the seam is the engine's HttpGet callable
    url: str,
    params: dict[str, str],
    *,
    timeout: float | None,
    source: str,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Send one GET through the offline transport seam and return its response."""
    response: HttpResponse = transport(
        url,
        params,
        timeout=timeout if timeout is not None else 30.0,
        source=source,
        headers=dict(headers or {}),
    )
    return response


def _http_get(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    """Governed GET returning ``(status, text)``; monkeypatched in tests."""
    try:
        response = get_shared_http_client().get(
            url,
            params=params,
            timeout=timeout if timeout is not None else 30.0,
            source="ecb",
        )
    except HttpFetchError as exc:
        raise EcbProviderError("ECB_HTTP_ERROR", status=exc.status, url=exc.url) from exc
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
            (``ECB_BAD_RESPONSE``). Both carry the upstream status and
            the query-free URL that produced them.
    """
    from opendata.core.config import get_settings

    base_url = get_settings().ecb_api_base_url or ECB_DEFAULT_BASE_URL
    params = {"format": "csvdata"}
    if start is not None:
        params["startPeriod"] = start.isoformat()
    if end is not None:
        params["endPeriod"] = end.isoformat()
    url = f"{base_url}/service/data/{series_key}"
    status, text = _http_get(url, params, timeout)
    if status != 200:
        raise EcbProviderError("ECB_HTTP_ERROR", status=status, url=url)
    rows = list(csv.DictReader(io.StringIO(text)))
    if rows and not {"KEY", "TIME_PERIOD", "OBS_VALUE"} <= set(rows[0]):
        raise EcbProviderError("ECB_BAD_RESPONSE", status=status, url=url)
    return rows
