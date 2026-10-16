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


_SOURCE_REFUSAL_CLASS: Final[str] = "FredProviderError"

_CONFIG_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/fred/models/_client.py",
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
    },
}
