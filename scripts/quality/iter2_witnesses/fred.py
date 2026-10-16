"""Offline witness configuration for the ``fred`` provider.

Witnesses run exclusively through the offline seam: the fetchers' ``http_transport``
attribute, served by ``FixedResponseTransport`` from ``opendata.data.providers._engine.testing``.
No leg touches the network and no ``OPENDATA_ALLOW_LIVE_E2E`` path is enabled.

FRED refuses shape failures through :class:`FredProviderError`, which reuses the engine's
typed refusal shape (code/status/url plus the attribution triples). The adapters here raise
it with a code only -- never an attributed column -- so no witness entry declares
``expected_missing`` / ``expected_rejected`` / ``expected_invalid``: each tamper breaks the
cell the named refusal code is declared for in the contract, and the empty attribution faces
are what the contract derives, not what a run showed.
"""

from __future__ import annotations

from typing import Any, Final

_OBSERVATION_PAGE_LIMIT = 100_000  # query.page_size default, echoed by a well-shaped page.
_MERGED_PAGE_LIMIT = 10_000  # min(page_size, max_records) for every merged survey model here.
_PROJECTION_PAGE_LIMIT = 100  # min(page_size, max_records) with both capped at 100 by the query.
#: ``FredRateQuery`` keeps ``page_size`` at 100_000 and ``max_records`` at 200_000, so the strict
#: pager asks every rate-extra leg for ``min(page_size, max_records)`` = 100_000 rows.
_RATE_EXTRA_PAGE_LIMIT = 100_000


def _fred_observation_page(
    *,
    series_id: str,
    values: list[tuple[str, str]],
    observation_start: str,
    observation_end: str,
) -> dict[str, Any]:
    """One complete FRED observations page exactly as the shared pagers' headers require.

    ``values`` carries ``(date, value-token)`` pairs; every page field the pagers' documented
    header check reads is echoed from the default query (``units`` ``lin``, ``output_type`` 1,
    ``order_by`` ``observation_date``, ``sort_order`` ``asc``, ``offset`` 0, ``limit`` the
    effective page size). ``series_id`` is echoed because every entry below asks for exactly
    one series per leg; a fan-out witness would have to drop it so one replayed document
    cannot claim a series the request did not name.
    """
    return {
        "realtime_start": "2026-01-06",
        "realtime_end": "2026-01-06",
        "observation_start": observation_start,
        "observation_end": observation_end,
        "limit": _OBSERVATION_PAGE_LIMIT,
        "offset": 0,
        "count": len(values),
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "units": "lin",
        "output_type": 1,
        "series_id": series_id,
        "observations": [
            {
                "date": day,
                "value": token,
                "realtime_start": "2026-01-06",
                "realtime_end": "2026-01-06",
            }
            for day, token in values
        ],
    }


def _fred_sofr_document() -> dict[str, Any]:
    """Two SOFR observations in the keys the fixed SOFR pager reads."""
    return _fred_observation_page(
        series_id="SOFR",
        values=[("2026-01-02", "3.65"), ("2026-01-05", "3.62")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_sonia_document() -> dict[str, Any]:
    """Two SONIA rate observations in the keys the fixed SONIA pager reads."""
    return _fred_observation_page(
        series_id="IUDSOIA",
        values=[("2026-01-02", "4.53"), ("2026-01-05", "4.55")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_iorb_document() -> dict[str, Any]:
    """Two IORB observations in the keys the fixed IORB pager reads."""
    return _fred_observation_page(
        series_id="IORB",
        values=[("2026-01-02", "4.90"), ("2026-01-05", "4.90")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_merged_page(
    *,
    values: list[tuple[str, str]],
    observation_start: str,
    observation_end: str,
    series_id: str | None = None,
) -> dict[str, Any]:
    """One complete observations page replayed to every series leg of a merged fetch.

    The survey models below fan out to one request per selected series and the shared
    ``FixedResponseTransport`` answers each leg with this same document, so the page publishes
    no ``series_id`` echo: an echoed identity could only have been true for one of the legs.
    The observation rows the adapters produce carry the series identity the *request* named
    (``_series_id``), which is what the merged contract row is keyed by. ``series_id`` is only
    ever passed for a one-leg fetch. Every model here caps at ``max_records`` 10000, so the
    echoed ``limit`` is the effective page size ``min(page_size, max_records)`` the shared
    pager compares against, not the 100000 the query object carries.
    """
    document: dict[str, Any] = {
        "realtime_start": "2026-01-06",
        "realtime_end": "2026-01-06",
        "observation_start": observation_start,
        "observation_end": observation_end,
        "limit": _MERGED_PAGE_LIMIT,
        "offset": 0,
        "count": len(values),
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "units": "lin",
        "output_type": 1,
        "observations": [
            {
                "date": day,
                "value": token,
                "realtime_start": "2026-01-06",
                "realtime_end": "2026-01-06",
            }
            for day, token in values
        ],
    }
    if series_id is not None:
        document["series_id"] = series_id
    return document


def _fred_michigan_document() -> dict[str, Any]:
    """Two monthly observations for the sentiment and inflation-expectation legs."""
    return _fred_merged_page(
        values=[("2025-12-01", "54.7"), ("2026-01-01", "52.6")],
        observation_start="2025-12-01",
        observation_end="2026-01-01",
    )


def _fred_manufacturing_ny_document() -> dict[str, Any]:
    """Two monthly observations for one Empire State survey topic's four legs."""
    return _fred_merged_page(
        values=[("2025-12-01", "3.9"), ("2026-01-01", "6.1")],
        observation_start="2025-12-01",
        observation_end="2026-01-01",
    )


def _fred_manufacturing_texas_document() -> dict[str, Any]:
    """Two monthly observations for one Dallas Fed survey topic's four legs."""
    return _fred_merged_page(
        values=[("2025-12-01", "10.4"), ("2026-01-01", "8.2")],
        observation_start="2025-12-01",
        observation_end="2026-01-01",
    )


def _fred_chicago_document() -> dict[str, Any]:
    """Two monthly observations for the nine Chicago survey legs."""
    return _fred_merged_page(
        values=[("2025-12-01", "-0.3"), ("2026-01-01", "0.8")],
        observation_start="2025-12-01",
        observation_end="2026-01-01",
    )


def _fred_retail_prices_document() -> dict[str, Any]:
    """Two monthly BLS average-price observations for one item-and-area series leg."""
    return _fred_merged_page(
        values=[("2025-12-01", "2.145"), ("2026-01-01", "2.204")],
        observation_start="2025-12-01",
        observation_end="2026-01-01",
        series_id="APU0000711211",
    )


def _fred_series_document() -> dict[str, Any]:
    """One CPIAUCSL observation in the keys the bounded generic pager reads."""
    return _fred_observation_page(
        series_id="CPIAUCSL",
        values=[("2025-12-01", "315.600")],
        observation_start="2025-12-01",
        observation_end="2025-12-01",
    )


def _fred_ameribor_document() -> dict[str, Any]:
    """Two overnight-AMERIBOR observations for the fixed-series rate pager."""
    return _fred_observation_page(
        series_id="AMERIBOR",
        values=[("2026-01-02", "4.32"), ("2026-01-05", "4.31")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_discount_window_document() -> dict[str, Any]:
    """Two DPCREDIT observations with the documented real-time interval."""
    return _fred_observation_page(
        series_id="DPCREDIT",
        values=[("2026-01-02", "4.75"), ("2026-01-05", "4.75")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_ecb_document() -> dict[str, Any]:
    """Two ECB marginal-lending observations."""
    return _fred_observation_page(
        series_id="ECBMLFR",
        values=[("2026-01-02", "2.40"), ("2026-01-05", "2.40")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_tips_document() -> dict[str, Any]:
    """Two DFII10 inflation-indexed yield observations."""
    return _fred_observation_page(
        series_id="DFII10",
        values=[("2026-01-02", "2.10"), ("2026-01-05", "2.08")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_treasury_constant_maturity_document() -> dict[str, Any]:
    """Two DGS3MO constant-maturity yield observations."""
    return _fred_observation_page(
        series_id="DGS3MO",
        values=[("2026-01-02", "4.01"), ("2026-01-05", "3.99")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_selected_treasury_bill_document() -> dict[str, Any]:
    """Two DTB3 discount-bill observations."""
    return _fred_observation_page(
        series_id="DTB3",
        values=[("2026-01-02", "3.85"), ("2026-01-05", "3.83")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_selected_treasury_constant_maturity_document() -> dict[str, Any]:
    """Two DGS10 constant-maturity yield observations."""
    return _fred_observation_page(
        series_id="DGS10",
        values=[("2026-01-02", "4.58"), ("2026-01-05", "4.55")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
    )


def _fred_search_document() -> dict[str, Any]:
    """One catalog record under the array the series-search pager reads."""
    return {
        "realtime_start": "2026-01-06",
        "realtime_end": "2026-01-06",
        "observation_start": "1999-01-01",
        "observation_end": "2025-12-01",
        "search_text": "sofr",
        "limit": 1_000,  # query.page_size default for the catalog search.
        "offset": 0,
        "count": 1,
        "seriess": [
            {
                "id": "SOFR",
                "title": "Secured Overnight Financing Rate",
                "frequency": "Daily",
                "units": "Percent",
                "seasonal_adjustment": "Not Seasonally Adjusted",
                "last_updated": "2026-01-05T16:30:00-0500",
                "observation_start": "1999-01-01",
                "observation_end": "2025-12-01",
                "popularity": 94,
                "notes": "Fixture catalog note.",
            }
        ],
    }


def _seam_envelope(url_contains: str, body: dict[str, Any]) -> dict[str, Any]:
    """One JSON answer in the envelope the runner's fake shared client routes on.

    FRED's release-table and auxiliary legs reach the network through
    ``_client._http_get`` without a ``transport=``, so they read
    ``get_shared_http_client()`` and are witnessed by patching that name in
    ``opendata.data.providers.fred.models._client`` -- the engine ``http_transport`` slot is
    simply never consulted on these paths.
    """
    return {
        "responses": [
            {
                "url_contains": url_contains,
                "status": 200,
                "content_type": "application/json",
                "body": body,
            }
        ]
    }


def _release_tables_payload(
    *,
    release_id: int,
    series_element_id: int,
    series_id: str,
    series_name: str,
    value: str,
    observation_date: str,
    container_element_id: int,
    container_name: str,
    line: int,
) -> dict[str, Any]:
    """A two-level release tree: one container element plus the series element under it.

    ``elements`` is FRED's keyed mapping, and the container carries no observation, so a
    witness that shipped only the series node would never exercise the ``element_type`` filter
    that separates the tree walk from the row the contract publishes. The series object is
    embedded in the container's ``children`` as the same object the mapping keys because the
    flattener visits both faces and refuses two payloads that share an ``element_id`` but
    disagree in content -- a stub child would be charged as a conflicting duplicate instead of
    being deduplicated.
    """
    series_node: dict[str, Any] = {
        "element_id": series_element_id,
        "parent_id": container_element_id,
        "type": "series",
        "name": series_name,
        "level": 1,
        "line": line,
        "series_id": series_id,
        "date": observation_date,
        "value": value,
        "children": [],
    }
    return {
        "release_id": release_id,
        "elements": {
            str(container_element_id): {
                "element_id": container_element_id,
                "type": "container",
                "name": container_name,
                "level": 0,
                "children": [series_node],
            },
            str(series_element_id): series_node,
        },
    }


def _fred_pce_document() -> dict[str, Any]:
    """The BEA PCE release (54) element 3220 published for one observation month."""
    return _seam_envelope(
        "/fred/release/tables",
        _release_tables_payload(
            release_id=54,
            container_element_id=3219,
            container_name="Personal consumption expenditures",
            series_element_id=3220,
            series_id="PCE",
            series_name="Personal consumption expenditures",
            value="13128.134",
            observation_date="2025-12-01",
            line=1,
        ),
    )


def _fred_release_table_document() -> dict[str, Any]:
    """One release tree as the generic table fetcher receives it: root, container, series.

    The payload-level root is what exercises the ``element_id``/``children`` columns of
    ``FredReleaseTable``: the flattener visits the root, the keyed mapping and the container's
    child list, so a fixture that published only leaves would leave the tree walk untested.
    """
    payload = _release_tables_payload(
        release_id=54,
        container_element_id=3219,
        container_name="Personal consumption expenditures",
        series_element_id=3220,
        series_id="PCE",
        series_name="Personal consumption expenditures",
        value="13128.134",
        observation_date="2025-12-01",
        line=1,
    )
    payload["element_id"] = 3218
    payload["type"] = "container"
    payload["name"] = "Bureau of Economic Analysis release 54"
    payload["level"] = 0
    return _seam_envelope("/fred/release/tables", payload)


def _fred_calendar_document() -> dict[str, Any]:
    """Two official publication dates on the page the calendar pager requires.

    The window carries no ``release_id``, so only ``/fred/releases/dates`` is read and the
    event name must come from the row itself rather than from a second metadata call.
    """
    return _seam_envelope(
        "/fred/releases/dates",
        {
            "realtime_start": "2026-01-06",
            "realtime_end": "2026-01-06",
            "order_by": "release_date",
            "sort_order": "desc",
            "limit": 1000,
            "offset": 0,
            "count": 2,
            "release_dates": [
                {
                    "release_id": 54,
                    "date": "2025-12-01",
                    "release_name": "Personal Income and Its Disposition",
                },
                {
                    "release_id": 10,
                    "date": "2025-12-10",
                    "release_name": "Consumer Price Index",
                },
            ],
        },
    )


def _fred_regional_document() -> dict[str, Any]:
    """A GeoFRED regional cross-section under the ``meta.data`` shape the series path reads."""
    return _seam_envelope(
        "/geofred/series/data",
        {
            "meta": {
                "series_id": "ALB251",
                "date": "2025-01-01",
                "data": [
                    {
                        "region": "Alabama",
                        "code": "01000",
                        "series_id": "ALB251",
                        "value": "2510.5",
                    },
                    {
                        "region": "Alaska",
                        "code": "02000",
                        "series_id": "AKB251",
                        "value": "1704.9",
                    },
                ],
            }
        },
    )


def _fred_bop_document() -> dict[str, Any]:
    """Two calendar-quarter BEA readings on the page every BOP leg asks for.

    The five mapped ``IEABC*`` legs share this one page, so the observations publish no
    ``series_id`` echo: each leg is labelled by its own request, and the join keys on the
    quarter start.
    """
    return _seam_envelope(
        "/fred/series/observations",
        {
            "realtime_start": "2026-01-06",
            "realtime_end": "2026-01-06",
            "limit": _OBSERVATION_PAGE_LIMIT,
            "offset": 0,
            "count": 2,
            "order_by": "observation_date",
            "sort_order": "asc",
            "units": "lin",
            "output_type": 1,
            "observations": [
                {
                    "date": "2025-07-01",
                    "value": "-243500",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
                {
                    "date": "2025-10-01",
                    "value": "-285100",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
            ],
        },
    )


def _fred_projections_document() -> dict[str, Any]:
    """Two SEP projection observations on the 100-row page the projection query caps each leg at.

    ``page_size`` and ``max_records`` are both capped at 100 by ``ProjectionsQuery``, so the
    pager's ``bounded_page_size`` -- and therefore the response's ``limit`` echo -- is 100. The
    two readings carry different real-time intervals because FRED's page identity is
    ``(date, realtime_start, realtime_end)``: a revision of an older SEP vintage is a separate
    row, which is what lets the join's own date-key gate be the one that bites.
    """
    return _seam_envelope(
        "/fred/series/observations",
        {
            "realtime_start": "2025-12-10",
            "realtime_end": "2025-12-10",
            "limit": _PROJECTION_PAGE_LIMIT,
            "offset": 0,
            "count": 2,
            "order_by": "observation_date",
            "sort_order": "asc",
            "units": "lin",
            "output_type": 1,
            "observations": [
                {
                    "date": "2025-12-31",
                    "value": "3.875",
                    "realtime_start": "2025-12-10",
                    "realtime_end": "2025-12-10",
                },
                {
                    "date": "2026-12-31",
                    "value": "3.625",
                    "realtime_start": "2025-09-17",
                    "realtime_end": "2025-09-17",
                },
            ],
        },
    )


def _fred_obfr_document() -> dict[str, Any]:
    """Two overnight funding days with the distribution columns the OBFR join publishes.

    Every one of the six mapped legs reads this page, so the rate column is present for both
    dates: the join keys a row on ``OBFR`` alone and only then attaches the percentiles. The
    ``limit`` echo is 10_000 because ``OvernightBankFundingRateQuery`` caps both ``page_size``
    and ``max_records`` there, not at FRED's own 100_000 page ceiling.
    """
    return _seam_envelope(
        "/fred/series/observations",
        {
            "realtime_start": "2026-01-06",
            "realtime_end": "2026-01-06",
            "limit": _MERGED_PAGE_LIMIT,
            "offset": 0,
            "count": 2,
            "order_by": "observation_date",
            "sort_order": "asc",
            "units": "lin",
            "output_type": 1,
            "observations": [
                {
                    "date": "2026-01-05",
                    "value": "3.615",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
                {
                    "date": "2026-01-06",
                    "value": "3.625",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
            ],
        },
    )


def _fred_estr_document() -> dict[str, Any]:
    """Two ESTR sessions shaped for the governed strict pager the rate-extra family uses.

    ``EuroShortTermRateFetcher`` builds a ``FixedObservationPagerSpec`` per leg, so the page must
    echo all thirteen documented header fields -- ``file_type`` and ``output_type`` included --
    and the ``limit`` it was actually asked for: ``min(page_size, max_records)`` is 100_000 for
    ``FredRateQuery``, not the 100-row default of the ordinary series query. The seven legs share
    this one page, so it publishes no ``series_id`` echo, and each value is an integral token
    because ``transactions`` and ``number_of_banks`` are read by an integer-only converter that
    accepts a whole number or the '.' sentinel; the five float columns therefore read coarsely.
    """
    return _seam_envelope(
        "/fred/series/observations",
        {
            "realtime_start": "2026-01-06",
            "realtime_end": "2026-01-06",
            "observation_start": "2025-12-04",
            "observation_end": "2025-12-05",
            "limit": _RATE_EXTRA_PAGE_LIMIT,
            "offset": 0,
            "count": 2,
            "order_by": "observation_date",
            "sort_order": "asc",
            "units": "lin",
            "output_type": 1,
            "file_type": "json",
            "observations": [
                {
                    "date": "2025-12-04",
                    "value": "3",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
                {
                    "date": "2025-12-05",
                    "value": "4",
                    "realtime_start": "2026-01-06",
                    "realtime_end": "2026-01-06",
                },
            ],
        },
    )


_HQM_SPOT_KEYS: Final[tuple[str, ...]] = tuple(
    str(index // 2) if index % 2 == 0 else f"{index // 2}.5" for index in range(1, 201)
)
_HQM_PAR_KEYS: Final[tuple[str, ...]] = ("2", "5", "10", "30")
_HQM_OBSERVATION_DATE: Final[str] = "2025-12-01"


def _hqm_series_entry(series_id: str, title: str, value: str) -> dict[str, Any]:
    """One release series as FRED's v2 endpoint publishes it, with a single monthly reading.

    All seven documented metadata fields are text because ``_hqm_release_rows`` requires each of
    them to be a ``str``; the identity it keys on is the title's ``<tenor>-Year`` token plus its
    ``Spot Rate``/``Par Yield`` suffix, so the tenor never has to be declared separately.
    """
    return {
        "series_id": series_id,
        "title": title,
        "frequency": "Monthly",
        "units": "Percent",
        "seasonal_adjustment": "Not Seasonally Adjusted",
        "last_updated": "2026-01-05 09:00:00",
        "copyright_id": "Copyright, ICE Data Indices, LLC, used with permission.",
        "notes": "Reviewed HQM release inventory witness series.",
        "observations": [{"date": _HQM_OBSERVATION_DATE, "value": value}],
    }


def _fred_hqm_release_document() -> dict[str, Any]:
    """The complete official HQM inventory -- 200 spot plus 4 par tenors -- on one cursor page.

    ``_hqm_release_rows`` refuses any release that does not carry the whole
    0.5-to-100-year spot grid and the 2/5/10/30-year par grid, whatever the query selected, so
    both rows below witness the same 204-series page. ``has_more`` is the string ``"false"`` with
    no ``next_cursor``, which is what ends the cursor walk after a single request. The tenor keys
    are re-derived here rather than imported from the product so that a grid change in the code
    under test breaks this witness instead of being copied into it.
    """
    spot = [
        _hqm_series_entry(
            f"BAMLC0A0CM{position:03d}SY",
            f"ICE BofA US Corporate AA Effective {key}-Year Spot Rate",
            f"{3.80 + 0.01 * position:.2f}",
        )
        for position, key in enumerate(_HQM_SPOT_KEYS, start=1)
    ]
    par = [
        _hqm_series_entry(
            f"BAMLC0A0CMP{key}SY",
            f"ICE BofA US Corporate AA {key}-Year Par Yield",
            f"{3.70 + 0.05 * position:.2f}",
        )
        for position, key in enumerate(_HQM_PAR_KEYS, start=1)
    ]
    return _seam_envelope(
        "/fred/v2/release/observations",
        {
            "has_more": "false",
            "release": {
                "release_id": 402,
                "name": "Corporate Bond Yield Curve",
                "url": "https://fred.stlouisfed.org/release?rid=402",
                "sources": ["ICE Data Indices, LLC"],
            },
            "series": [*spot, *par],
        },
    )


_SOURCE_REFUSAL_CLASS: Final[str] = "FredProviderError"

_CONFIG_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/fred/models/_client.py",
]

#: The one module whose ``get_shared_http_client`` the legs below actually read: every
#: release-table and auxiliary request goes through ``_client._http_get`` with ``transport``
#: left at its default, so the shared client -- not the engine slot -- is the offline entry.
_FRED_CLIENT_MODULE: Final[str] = "opendata.data.providers.fred.models._client"

#: The seam declaration shared by every leg below: the fetchers reach the network through
#: ``_client._http_get`` with ``transport`` left at its default, so the module global
#: ``get_shared_http_client`` is the only attribute a witness may substitute.
_FRED_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": [_FRED_CLIENT_MODULE],
    "attr": "get_shared_http_client",
}

#: Every auxiliary and fan-out row below serializes through the same frozen contract module.
_ITER2_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/fred_iter2.py",
    "opendata/data/domains.yaml",
]

#: The observations-pager modules a delegating fetcher reaches once it builds a bare
#: ``FredSeriesFetcher()`` per source series.
_PAGED_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/fred/models/series.py",
    "opendata/data/providers/fred/models/_paged.py",
]

#: The rate-extra family builds its own ``FixedObservationPagerSpec`` per leg and imports
#: ``get_shared_http_client`` into this module's namespace, so the pager resolves that module
#: global at fetch time -- it is the attribute a witness of these rows may substitute.
_RATE_EXTRA_MODULE: Final[str] = "opendata.data.providers.fred.models.iter2_rates_extra"

_RATE_EXTRA_CLIENT_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": [_RATE_EXTRA_MODULE],
    "attr": "get_shared_http_client",
}

#: The module that assembles the rate-extra rows and reads the seam above.
_RATE_EXTRA_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/fred/models/iter2_rates_extra.py",
]

#: The governed strict pager, reached only by the observation-paged legs of that family.
_STRICT_PAGER_SOURCE_FILE: Final[str] = (
    "opendata/data/providers/fred/models/_fixed_observation_pages.py"
)

#: The frozen wide/long row contracts of the rate-extra family.
_RATE_EXTRA_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/fred_rates_extra.py",
    "opendata/data/domains.yaml",
]

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.fred.registration",
    "credential_env": {"FRED_API_KEY": "fixture-key-never-sent"},
    "allowed_hosts": ["api.stlouisfed.org"],
    "models": {
        "SOFR": {
            "fixture_document": _fred_sofr_document,
            "query": {"series_id": "SOFR"},
            "tamper": {
                "op": "set",
                "path": ["observations", 1, "value"],
                "value": "not-a-rate",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing a non-numeric 'value' token breaks the documented source-token "
                "float conversion the SOFR row keeps alongside the native unit, so the "
                "adapter must refuse the whole page with FRED_BAD_OBSERVATION surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of coercing the cell"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/sofr.py",
                "opendata/data/providers/fred/models/_sofr_client.py",
                "opendata/data/providers/fred/models/_fixed_observation_pages.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_sofr.py",
                "opendata/data/domains.yaml",
            ],
        },
        "SONIA": {
            "fixture_document": _fred_sonia_document,
            "query": {"parameter": "rate"},
            "tamper": {
                "op": "set",
                "path": ["observations", 0, "value"],
                "value": "not-a-rate",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing a non-numeric 'value' token breaks the documented source-token "
                "float conversion the SONIA row keeps with its parameter-bound native unit, "
                "so the adapter must refuse with FRED_BAD_OBSERVATION surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of coercing the cell"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/sonia.py",
                "opendata/data/providers/fred/models/_sonia_client.py",
                "opendata/data/providers/fred/models/_fixed_observation_pages.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_sonia.py",
                "opendata/data/domains.yaml",
            ],
        },
        "IORB": {
            "fixture_document": _fred_iorb_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": ["observations", 0, "realtime_end"],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_RESPONSE",
            "tamper_reason": (
                "dropping 'realtime_end' removes the closing half of the documented revision "
                "interval every observation row must carry, so the shared FRED pager must "
                "refuse the page with FRED_BAD_RESPONSE surfaced as a ProviderEngineError-"
                "shaped FredProviderError rather than publishing an open-ended observation"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iorb.py",
                "opendata/data/providers/fred/models/_iorb_client.py",
                "opendata/data/providers/fred/models/_fixed_observation_pages.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iorb.py",
                "opendata/data/domains.yaml",
            ],
        },
        "FredSeries": {
            "fixture_document": _fred_series_document,
            "query": {"series_id": "CPIAUCSL"},
            "tamper": {
                "op": "set",
                "path": ["observations", 0, "value"],
                "value": "",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing an empty value token breaks the documented FRED wire rule that "
                "only the '.' sentinel means missing, so the generic observations adapter "
                "must refuse with FRED_BAD_OBSERVATION surfaced as a ProviderEngineError-"
                "shaped FredProviderError instead of inventing a zero"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/economic_series.py",
                "opendata/data/domains.yaml",
            ],
        },
        "FredSearch": {
            "fixture_document": _fred_search_document,
            "query": {"search_text": "sofr"},
            "tamper": {
                "op": "drop",
                "path": ["seriess", 0, "title"],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_SERIES",
            "tamper_reason": (
                "dropping the required 'title' makes the strict catalog row contract refuse "
                "the record; the search adapter must surface FRED_BAD_SERIES as a "
                "ProviderEngineError-shaped FredProviderError rather than publishing a "
                "nameless catalog entry"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/search.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/economic_series.py",
                "opendata/data/domains.yaml",
            ],
        },
        "Ameribor": {
            "fixture_document": _fred_ameribor_document,
            "query": {"maturity": "overnight"},
            "tamper": {
                "op": "set",
                "path": ["observations", 0, "value"],
                "value": "not-a-rate",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing a non-numeric 'value' token breaks the documented AMERIBOR rate "
                "column, whose contract type is a finite float read from FRED's text token "
                "('.' being the only missing marker), so the fixed-series adapter must refuse "
                "with FRED_BAD_OBSERVATION surfaced as a ProviderEngineError-shaped "
                "FredProviderError instead of coercing the cell"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/fixed_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "DiscountWindowPrimaryCreditRate": {
            "fixture_document": _fred_discount_window_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": ["observations", 0, "realtime_end"],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "dropping 'realtime_end' removes the closing half of the revision interval the "
                "discount-window contract reads for every observation (it bounds the vintage "
                "the row keeps privately), so the adapter must refuse with FRED_BAD_OBSERVATION "
                "surfaced as a ProviderEngineError-shaped FredProviderError rather than "
                "publishing an open-ended observation"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/fixed_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EuropeanCentralBankInterestRates": {
            "fixture_document": _fred_ecb_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": ["count"],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_PAGINATION_ERROR",
            "expected_status": 200,
            "tamper_reason": (
                "dropping the page-header 'count' removes the documented result-size the shared "
                "FRED pager must know before it can bound records and pages, so the request "
                "must refuse with FRED_PAGINATION_ERROR surfaced as a ProviderEngineError-shaped "
                "FredProviderError instead of returning an unbounded ECB policy-rate page"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/fixed_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "TipsYields": {
            "fixture_document": _fred_tips_document,
            "query": {"maturity": "10"},
            "tamper": {
                "op": "set",
                "path": ["observations", 0, "date"],
                "value": "2026-1-05",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing a non-canonical 'date' breaks the TIPS contract column, which is a "
                "date read only from FRED's canonical YYYY-MM-DD text, so the adapter must "
                "refuse with FRED_BAD_OBSERVATION surfaced as a ProviderEngineError-shaped "
                "FredProviderError instead of guessing a calendar cell"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/fixed_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "TreasuryConstantMaturity": {
            "fixture_document": _fred_treasury_constant_maturity_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["observations", 1, "value"],
                "value": "not-a-rate",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "publishing a non-numeric 'value' token breaks the documented 'rate' contract "
                "column of the Treasury constant-maturity row (a finite float, or null only for "
                "FRED's '.' sentinel), so the adapter must refuse with FRED_BAD_OBSERVATION "
                "surfaced as a ProviderEngineError-shaped FredProviderError instead of coercing"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/treasury_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "SelectedTreasuryBill": {
            "fixture_document": _fred_selected_treasury_bill_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": ["observations", 0, "value"],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "dropping the 'value' cell removes the only source of the selected-bill "
                "contract's 'rate' column, so the adapter must refuse with FRED_BAD_OBSERVATION "
                "surfaced as a ProviderEngineError-shaped FredProviderError rather than "
                "publishing a bill row whose rate was invented"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/treasury_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "SelectedTreasuryConstantMaturity": {
            "fixture_document": _fred_selected_treasury_constant_maturity_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["offset"],
                "value": 7,
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_PAGINATION_ERROR",
            "expected_status": 200,
            "tamper_reason": (
                "echoing 'offset' 7 for the page requested at offset 0 breaks the documented "
                "page-header contract every Treasury yield row depends on for gap-free paging, "
                "so the shared pager must refuse with FRED_PAGINATION_ERROR surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of stitching a hole into "
                "the curve"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/treasury_rates.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "UniversityOfMichigan": {
            "fixture_document": _fred_michigan_document,
            "query": {},
            "tamper": {"op": "set", "path": ["observations", 0, "value"], "value": "not-a-rate"},
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the contract's consumer_sentiment and inflation_expectation columns are floats "
                "read straight from FRED's text value token, so a page whose token is not a "
                "number must be refused with FRED_BAD_OBSERVATION surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of guessing a sentiment "
                "index for the survey month"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "ManufacturingOutlookNY": {
            "fixture_document": _fred_manufacturing_ny_document,
            "query": {"topic": "new_orders"},
            "tamper": {"op": "set", "path": ["observations", 0, "date"], "value": "2026-1-01"},
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the NY survey row is keyed by its survey month, which FRED publishes only as a "
                "canonical YYYY-MM-DD date, so a non-canonical 'date' cell must be refused with "
                "FRED_BAD_OBSERVATION surfaced as a ProviderEngineError-shaped FredProviderError "
                "instead of inventing which diffusion reading belongs to which month"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "ManufacturingOutlookTexas": {
            "fixture_document": _fred_manufacturing_texas_document,
            "query": {"topic": "business_activity"},
            "tamper": {"op": "drop", "path": ["count"]},
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_PAGINATION_ERROR",
            "expected_status": 200,
            "tamper_reason": (
                "dropping the page-header 'count' removes the result size the shared FRED pager "
                "must know before it can bound the four series legs of one Texas survey topic, so "
                "the request must refuse with FRED_PAGINATION_ERROR surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of returning an unbounded "
                "survey page"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "SurveyOfEconomicConditionsChicago": {
            "fixture_document": _fred_chicago_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["observations", 1, "realtime_end"],
                "value": "2025-11-30",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "every Chicago survey reading is published inside a real-time interval whose start "
                "precedes its end, and the contract keeps that vintage per series, so an "
                "observation whose realtime_end precedes its realtime_start must be refused with "
                "FRED_BAD_OBSERVATION surfaced as a ProviderEngineError-shaped FredProviderError "
                "instead of silently reversing the survey's vintage"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "RetailPrices": {
            "fixture_document": _fred_retail_prices_document,
            "query": {"item": "bananas", "region": "all_city"},
            "tamper": {"op": "set", "path": ["observations", 0, "value"], "value": ""},
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the retail contract's value column is the BLS average price read from FRED's text "
                "token, and FRED's documented missing reading is a single dot rather than an empty "
                "cell, so an empty value must be refused with FRED_BAD_OBSERVATION surfaced as a "
                "ProviderEngineError-shaped FredProviderError instead of publishing a price of "
                "zero for the item and area"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/iter2_macro_remaining.py",
                "opendata/data/providers/fred/models/series.py",
                "opendata/data/providers/fred/models/_paged.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "PersonalConsumptionExpenditures": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_pce_document,
            "query": {"category": "pce_dollars", "date": ["2025-12-01"]},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "elements", "3219", "type"],
                "value": "series",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the PCE row is contracted to carry the observation itself, so "
                "release_contract_values refuses any series element whose date, symbol, value or "
                "name is absent; re-typing the observation-less BEA container element 3219 as a "
                "series must therefore surface FRED_BAD_OBSERVATION as a "
                "ProviderEngineError-shaped "
                "FredProviderError instead of publishing a headline node of the release tree as if "
                "it were a PCE reading"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_macro.py",
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
            ],
            "contract_files": [
                "opendata/data/models/fred_macro_iter2.py",
                "opendata/data/models/fred_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "FredReleaseTable": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_release_table_document,
            "query": {"release_id": "54", "date": ["2025-12-01"]},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "elements", "3220", "series_id"],
                "value": "P CE",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "a release-table element that names a FRED series publishes that name under the "
                "identifier grammar the flattener enforces, so re-writing element 3220's "
                "series_id with a space must surface FRED_BAD_OBSERVATION as a "
                "ProviderEngineError-shaped FredProviderError instead of storing a symbol no "
                "source series can be replayed against"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "EconomicCalendar": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_calendar_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "release_dates", 1, "date"],
                "value": "2025-12-1",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the calendar row is the official publication date, and the adapter parses only "
                "FRED's canonical zero-padded YYYY-MM-DD text, so unpadded 2025-12-1 must "
                "surface FRED_BAD_OBSERVATION as a ProviderEngineError-shaped FredProviderError "
                "instead of publishing an event date the frozen contract cannot round-trip"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "FredRegional": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_regional_document,
            "query": {"symbol": "ALB251"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "meta", "data", 0, "code"],
                "value": "",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "GeoFRED identifies a region by its exact source code, and the row's region "
                "column is only attributable through it, so an empty code must surface "
                "FRED_BAD_OBSERVATION as a ProviderEngineError-shaped FredProviderError instead "
                "of publishing a regional reading whose geography cannot be named"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "BalanceOfPayments": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_bop_document,
            "query": {"frequency": "quarterly"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "observations", 0, "date"],
                "value": "2025-08-15",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the quarterly BEA transactions join keys each row on a calendar-quarter start, "
                "so a mid-quarter observation date must surface FRED_BAD_OBSERVATION as a "
                "ProviderEngineError-shaped FredProviderError instead of labelling a reading "
                "with a period no quarterly release publishes"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
                *_PAGED_SOURCE_FILES,
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "PROJECTIONS": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_projections_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "observations", 1, "date"],
                "value": "2025-12-31",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the seven projection legs align on one observation date per row, and FRED "
                "keeps a revision as a separate reading by its real-time interval, so two "
                "vintages of 2025-12-31 must surface FRED_BAD_OBSERVATION as a "
                "ProviderEngineError-shaped FredProviderError instead of collapsing two SEP "
                "vintages into one projection row"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/iter2_auxiliary.py",
                *_PAGED_SOURCE_FILES,
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "OvernightBankFundingRate": {
            "client_seam": _FRED_CLIENT_SEAM,
            "fixture_document": _fred_obfr_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "observations", 0, "value"],
                "value": ".",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the overnight funding row is keyed by its headline rate, and FRED's documented "
                "missing reading is the dot sentinel, so a dot for OBFR itself must surface "
                "FRED_BAD_OBSERVATION as a ProviderEngineError-shaped FredProviderError instead "
                "of publishing a date whose rate column is absent while its percentiles remain"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                "opendata/data/providers/fred/models/federal_funds.py",
                *_PAGED_SOURCE_FILES,
            ],
            "contract_files": _ITER2_CONTRACT_FILES,
        },
        "EuroShortTermRate": {
            "client_seam": _RATE_EXTRA_CLIENT_SEAM,
            "fixture_document": _fred_estr_document,
            "query": {},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "limit"],
                "value": 100,
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_PAGINATION_ERROR",
            "expected_status": 200,
            "tamper_reason": (
                "every ESTR leg asks the governed pager for min(page_size, max_records) = 100_000 "
                "rows and the pager compares its own request against the page's 'limit' echo, so a "
                "page that answers 100 must surface FRED_PAGINATION_ERROR as a "
                "ProviderEngineError-shaped FredProviderError instead of stitching a 99_900-row "
                "hole into the seven-column wide series"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                *_RATE_EXTRA_SOURCE_FILES,
                _STRICT_PAGER_SOURCE_FILE,
            ],
            "contract_files": _RATE_EXTRA_CONTRACT_FILES,
        },
        "SpotRate": {
            "client_seam": _RATE_EXTRA_CLIENT_SEAM,
            "fixture_document": _fred_hqm_release_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": ["responses", 0, "body", "series", 0],
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "the reviewed contract reads the whole official HQM table -- 200 spot tenors from "
                "0.5 to 100 years plus the 2/5/10/30-year par points -- before it cuts out the "
                "curve point a row reports, so a release missing its 0.5-Year spot series must "
                "surface FRED_BAD_OBSERVATION as a ProviderEngineError-shaped FredProviderError "
                "instead of publishing a spot rate read from a truncated table"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                *_RATE_EXTRA_SOURCE_FILES,
            ],
            "contract_files": _RATE_EXTRA_CONTRACT_FILES,
        },
        "HighQualityMarketCorporateBond": {
            "client_seam": _RATE_EXTRA_CLIENT_SEAM,
            "fixture_document": _fred_hqm_release_document,
            "query": {"yield_curve": "par"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "release", "sources"],
                "value": "ICE Data Indices, LLC",
            },
            "refusal_class": _SOURCE_REFUSAL_CLASS,
            "refusal_code": "FRED_BAD_OBSERVATION",
            "tamper_reason": (
                "every HQM curve row is published inside one release whose identity (id, name, url "
                "and source list) the pager fingerprints before it reads a tenor, so replacing the "
                "release's sources array with a bare string must surface FRED_BAD_OBSERVATION as a "
                "ProviderEngineError-shaped FredProviderError instead of attributing a par curve "
                "to a release whose provenance cannot be checked"
            ),
            "source_files": [
                *_CONFIG_SOURCE_FILES,
                *_RATE_EXTRA_SOURCE_FILES,
            ],
            "contract_files": _RATE_EXTRA_CONTRACT_FILES,
        },
    },
}
