"""Wave 8 shard A of the ``fred`` offline witness configuration.

This file publishes ``MODELS`` for eight fred inventory rows only; ``iter2_claims_fixtures``
merges it into ``scripts/quality/iter2_witnesses/fred.py``'s ``CONFIG["models"]`` (a duplicated
key fails closed). Nothing here touches the twelve already-green base rows.

Seams, per row (never a socket, never ``OPENDATA_ALLOW_LIVE_E2E``):

* Branch A - ``ConsumerPriceIndex`` and ``MortgageIndices`` run on the engine seam: both
  fetchers descend from ``_FixedMacroFetcher``, which declares
  ``http_transport: ClassVar[HttpGet | None]`` (``iter2_macro.py:880``) and installs it on the
  inner ``FredSeriesFetcher`` per call (``iter2_macro.py:930``), so the fixture document is the
  raw FRED observations page the generic pager (``models/_paged.py``) replays.
* Branch B - ``BondIndices``, ``CommercialPaper``, ``YieldCurve``, ``CommoditySpotPrices`` and
  ``FederalFundsRate`` reach the wire through the governed client: the four rate rows build
  ``FixedObservationPagerSpec(get_http_client=get_shared_http_client)`` per request
  (``iter2_rates_extra.py:597``), ``FederalFundsRateFetcher`` calls a fresh
  ``FredSeriesFetcher()`` (``federal_funds.py:125``) whose pager defers to
  ``_client._http_get``, and ``NonFarmPayrollsFetcher`` calls ``_fred_json``
  (``iter2_auxiliary.py:67``), which is the same ``_client._http_get``. Each therefore declares
  ``client_seam`` naming the module whose ``get_shared_http_client`` attribute is read at request
  time, and the fixture carrier is the ``{"responses": [...]}`` envelope.

Fan-out: the fake client matches on ``url_contains`` only (FRED carries ``series_id`` as a query
parameter, never in the URL path), so a multi-series row declares ONE entry whose ``sequence``
holds exactly one page per series id, in ``_series_ids`` order. Each page echoes its own
``series_id``, which ``_validate_page_header`` (``_fixed_observation_pages.py:438``) compares to
the series that leg requested - so a replayed document cannot silently claim another series. The
two Branch A rows replay a single document across legs and therefore omit ``series_id``.

Refusals: every fred adapter refuses with ``FredProviderError``, a ``ProviderEngineError``
subclass raised with a code only (``_client.py:29``), so no entry declares an ``expected_*`` face.
Each tamper instead writes prose into the one source token that feeds the row's required float
contract column - ``value`` for ``FredConsumerPriceIndex``/``FredBondIndices``/
``FredNonFarmPayroll``, ``rate`` for ``FredMortgageIndex``/``FredCommercialPaper``/
``FredYieldCurve``/``FredFederalFundsRate``, ``price`` for ``FredCommoditySpotPrices`` - which the
frozen contracts declare as a non-null ``float``. The ``fred_yield_curve`` domain keeps the
documented ``float`` + ``'.'`` null-sentinel ruling (``FredYieldCurve.rate`` is
``float | None`` and ``_normalize`` only ever stores a parsed non-null float), so the yield-curve
tamper is a genuinely malformed cell rather than a missing one, and the sentinel stays untouched.

Upstream hazard check: none of these eight rows selects ``TEDRATE`` or the ICE BofA OAS series -
the series maps used here are ``_BOND_INDEX_SERIES`` (six BAMLC* effective-yield legs),
``_CP_CATEGORIES`` x ``_CP_MATURITY`` (twenty-four RIFSPP* legs), ``_REAL_CURVE`` (five DFII*
legs), ``_COMMODITY_UNITS`` (twelve EIA-distributed spot legs) and ``_FED_FUNDS_SERIES`` (eleven
EFFR/OBFR-family legs); every one of those ids is a still-published history, so no row here has
to be reported as an empty-join GAP.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Final

#: Page header ``limit`` the generic pager demands: ``min(page_size, max_records)`` of the
#: transport query it builds. ``_MacroQuery`` caps ``max_records`` at 10_000 (iter2_macro.py:463)
#: and ``FederalFundsRateQuery`` defaults both bounds to 10_000 (federal_funds.py:69-70).
_MACRO_PAGE_LIMIT: Final = 10_000
_FED_FUNDS_PAGE_LIMIT: Final = 10_000
#: ``FredRateQuery`` keeps the observations pager defaults: page_size 100_000, max_records
#: 200_000 (iter2_rates_extra.py:244-246).
_RATE_PAGE_LIMIT: Final = 100_000

_REALTIME_START: Final = "2026-01-06"
_REALTIME_END: Final = "2026-01-06"
_OBSERVATIONS_URL: Final = "/fred/series/observations"
_RELEASE_TABLES_URL: Final = "/fred/release/tables"

#: ``MortgageIndicesFetcher.primary_indices`` (iter2_macro.py:1439) - the seven legs the default
#: ``index=["primary"]`` selector fans out to, in declaration order.
_MORTGAGE_PRIMARY_SERIES: Final[tuple[str, ...]] = (
    "OBMMIC30YF",
    "OBMMIC30YFNA",
    "OBMMIJUMBO30YF",
    "OBMMIFHA30YF",
    "OBMMIVA30YF",
    "OBMMIUSDA30YF",
    "OBMMIC15YF",
)
#: ``_BOND_INDEX_SERIES`` keys (iter2_rates_extra.py:149) in the order ``_series_ids`` returns.
_BOND_INDEX_SERIES: Final[tuple[str, ...]] = (
    "BAMLC1A0C13YEY",
    "BAMLC2A0C35YEY",
    "BAMLC3A0C57YEY",
    "BAMLC4A0C710YEY",
    "BAMLC7A0C1015YEY",
    "BAMLC8A0C15PYEY",
)
#: ``_COMMODITY_UNITS`` series ids (iter2_rates_extra.py:100) in the order ``_series_ids`` returns.
_COMMODITY_SERIES: Final[tuple[str, ...]] = (
    "DCOILWTICO",
    "DCOILBRENTEU",
    "DHHNGSP",
    "DJFUELUSGULF",
    "DPROPANEMBTX",
    "DHOILNYH",
    "DDFUELUSGULF",
    "DDFUELNYH",
    "DDFUELLA",
    "DGASNYH",
    "DGASUSGULF",
    "DRGASLA",
)
#: ``_CP_CATEGORIES`` x ``_CP_MATURITY`` (iter2_rates_extra.py:123-136) flattened by
#: ``CommercialPaperFetcher._series_specs``: category outer, maturity inner. Each id is paired
#: with the two tokens its maturity is quoted at, so the sequence order is derived from the same
#: product the fetcher walks rather than from slicing an identifier of variable category length.
_CP_MATURITY_TOKENS: Final[dict[str, tuple[str, str]]] = {
    "01": ("4.30", "4.28"),
    "07": ("4.26", "4.24"),
    "15": ("4.22", "4.20"),
    "30": ("4.16", "4.14"),
    "60": ("4.10", "4.08"),
    "90": ("4.04", "4.02"),
}
_CP_SERIES: Final[dict[str, tuple[str, str]]] = {
    f"RIFSPP{category}{maturity}NB": tokens
    for category in ("AAAD", "FAAD", "NAAD", "NA2P2D")
    for maturity, tokens in _CP_MATURITY_TOKENS.items()
}
#: ``_REAL_CURVE`` series ids (iter2_rates_extra.py:190) for ``yield_curve_type="real"``.
_REAL_CURVE_SERIES: Final[tuple[str, ...]] = ("DFII5", "DFII7", "DFII10", "DFII20", "DFII30")
#: ``_FED_FUNDS_SERIES`` values (federal_funds.py:39) in the order ``extract_data`` loops.
_FED_FUNDS_SERIES: Final[tuple[str, ...]] = (
    "EFFR",
    "DFEDTARU",
    "DFEDTARL",
    "EFFR1",
    "EFFR25",
    "EFFR75",
    "EFFR99",
    "EFFRVOL",
    "EFFRL",
    "EFFRH",
    "EFFRSD",
)

#: ``YieldCurveFetcher._date_selector`` (iter2_rates_extra.py:1644) bounds the search window at
#: ``target - 400 days``; the page header must echo exactly that window.
_YIELD_CURVE_TARGET: Final = date(2026, 1, 5)
_YIELD_CURVE_LOOKBACK: Final = 400
_YIELD_CURVE_START: Final = _YIELD_CURVE_TARGET - timedelta(days=_YIELD_CURVE_LOOKBACK)

_SOURCE_REFUSAL_CLASS: Final[str] = "FredProviderError"
_CONFIG_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/fred/models/_client.py",
]


def _observation(day: str, token: str) -> dict[str, str]:
    return {
        "date": day,
        "value": token,
        "realtime_start": _REALTIME_START,
        "realtime_end": _REALTIME_END,
    }


def _observations_page(
    *,
    series_id: str | None,
    values: list[tuple[str, str]],
    observation_start: str,
    observation_end: str,
    limit: int,
    units: str = "lin",
) -> dict[str, Any]:
    """One complete FRED observations page, shaped for both fred pagers.

    Every header field ``_REQUIRED_PAGE_FIELDS`` (``_fixed_observation_pages.py:33``) demands is
    echoed from the default transport query (``units`` from ``transform or "lin"``,
    ``output_type`` 1, ``order_by`` ``observation_date``, ``sort_order`` ``asc``, ``offset`` 0,
    ``file_type`` ``json``), so the fixed pager's documented header check passes for the leg that
    requested ``series_id``. ``series_id`` is omitted only where one document legitimately replays
    several legs.
    """
    page: dict[str, Any] = {
        "realtime_start": _REALTIME_START,
        "realtime_end": _REALTIME_END,
        "observation_start": observation_start,
        "observation_end": observation_end,
        "limit": limit,
        "offset": 0,
        "count": len(values),
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "units": units,
        "output_type": 1,
        "observations": [_observation(day, token) for day, token in values],
    }
    if series_id is not None:
        page["series_id"] = series_id
    return page


def _fanout_pages(
    series_ids: tuple[str, ...],
    *,
    limit: int,
    values_for: Any,  # noqa: ANN401 - Callable[[str], list[tuple[str, str]]]
    units: str = "lin",
    observation_start: str = "2026-01-02",
    observation_end: str = "2026-01-05",
) -> list[dict[str, Any]]:
    """One complete page per requested series id, in ``_series_ids`` order."""
    return [
        _observations_page(
            series_id=series_id,
            values=values_for(series_id),
            observation_start=observation_start,
            observation_end=observation_end,
            limit=limit,
            units=units,
        )
        for series_id in series_ids
    ]


def _seam_envelope(needle: str, pages: list[Any] | list[dict[str, Any]]) -> dict[str, Any]:
    """The Branch B carrier: one url-matched entry serving every leg in order."""
    return {"responses": [{"url_contains": needle, "sequence": pages}]}


def _fred_consumer_price_index_document() -> dict[str, Any]:
    """Two CPIAUCSL year-over-year observations (the one series CPI's query maps)."""
    return _observations_page(
        series_id=None,
        values=[("2025-11-01", "2.7"), ("2025-12-01", "2.9")],
        observation_start="2025-11-01",
        observation_end="2025-12-01",
        limit=_MACRO_PAGE_LIMIT,
        units="pc1",  # ConsumerPriceIndexFetcher.source_transform maps "yoy" -> "pc1".
    )


def _fred_mortgage_indices_document() -> dict[str, Any]:
    """One page replayed across the seven ``primary`` OBMMI legs.

    ``series_id`` is deliberately absent: the engine transport hands the identical document to
    every leg, and only ``_paged.py`` validates it (it never reads ``series_id``), so echoing a
    single series here would misattribute six legs.
    """
    return _observations_page(
        series_id=None,
        values=[("2026-01-02", "6.62"), ("2026-01-05", "6.58")],
        observation_start="2026-01-02",
        observation_end="2026-01-05",
        limit=_MACRO_PAGE_LIMIT,
    )


def _fred_bond_indices_document() -> dict[str, Any]:
    """Six ICE BofA US corporate effective-yield pages, one per ``_BOND_INDEX_SERIES`` leg."""
    by_series = {
        "BAMLC1A0C13YEY": ("4.11", "4.08"),
        "BAMLC2A0C35YEY": ("4.26", "4.22"),
        "BAMLC3A0C57YEY": ("4.45", "4.40"),
        "BAMLC4A0C710YEY": ("4.72", "4.67"),
        "BAMLC7A0C1015YEY": ("5.03", "4.98"),
        "BAMLC8A0C15PYEY": ("5.41", "5.35"),
    }
    return _seam_envelope(
        _OBSERVATIONS_URL,
        _fanout_pages(
            _BOND_INDEX_SERIES,
            limit=_RATE_PAGE_LIMIT,
            values_for=lambda sid: [
                ("2026-01-02", by_series[sid][0]),
                ("2026-01-05", by_series[sid][1]),
            ],
        ),
    )


def _fred_commercial_paper_document() -> dict[str, Any]:
    """Twenty-four RIFSPP commercial-paper pages (4 categories x 6 maturities)."""
    return _seam_envelope(
        _OBSERVATIONS_URL,
        _fanout_pages(
            tuple(_CP_SERIES),
            limit=_RATE_PAGE_LIMIT,
            values_for=lambda sid: [
                ("2026-01-02", _CP_SERIES[sid][0]),
                ("2026-01-05", _CP_SERIES[sid][1]),
            ],
        ),
    )


def _fred_commodity_spot_prices_document() -> dict[str, Any]:
    """Twelve EIA-distributed FRED spot-price pages, one per ``_COMMODITY_UNITS`` leg."""
    by_series = {
        "DCOILWTICO": ("61.18", "60.94"),
        "DCOILBRENTEU": ("65.32", "65.07"),
        "DHHNGSP": ("3.41", "3.36"),
        "DJFUELUSGULF": ("2.41", "2.39"),
        "DPROPANEMBTX": ("1.86", "1.85"),
        "DHOILNYH": ("2.55", "2.53"),
        "DDFUELUSGULF": ("2.62", "2.60"),
        "DDFUELNYH": ("2.71", "2.69"),
        "DDFUELLA": ("2.76", "2.74"),
        "DGASNYH": ("2.38", "2.36"),
        "DGASUSGULF": ("2.24", "2.22"),
        "DRGASLA": ("2.31", "2.29"),
    }
    return _seam_envelope(
        _OBSERVATIONS_URL,
        _fanout_pages(
            _COMMODITY_SERIES,
            limit=_RATE_PAGE_LIMIT,
            values_for=lambda sid: [
                ("2026-01-02", by_series[sid][0]),
                ("2026-01-05", by_series[sid][1]),
            ],
        ),
    )


def _fred_yield_curve_document() -> dict[str, Any]:
    """Five TIPS real-curve pages sharing both observation dates.

    ``YieldCurveFetcher._normalize`` keeps only dates on which *every* maturity of the curve is
    numeric, so the legs must share their calendar; the window dates are the bounded
    ``[target - 400 days, target]`` selector the fetcher itself computes, because
    ``_validate_page_header`` refuses a page whose ``observation_start``/``observation_end`` do not
    match the leg query. Every token here is numeric - the ``'.'`` null sentinel is the documented
    way a maturity is absent, and this witness never needs it.
    """
    by_series = {
        "DFII5": ("1.28", "1.25"),
        "DFII7": ("1.46", "1.43"),
        "DFII10": ("1.71", "1.68"),
        "DFII20": ("2.10", "2.06"),
        "DFII30": ("2.36", "2.32"),
    }
    return _seam_envelope(
        _OBSERVATIONS_URL,
        _fanout_pages(
            _REAL_CURVE_SERIES,
            limit=_RATE_PAGE_LIMIT,
            values_for=lambda sid: [
                ("2026-01-02", by_series[sid][0]),
                (_YIELD_CURVE_TARGET.isoformat(), by_series[sid][1]),
            ],
            observation_start=_YIELD_CURVE_START.isoformat(),
            observation_end=_YIELD_CURVE_TARGET.isoformat(),
        ),
    )


def _fred_federal_funds_document() -> dict[str, Any]:
    """Eleven EFFR-family pages joined on the same two dates (federal_funds.py:39)."""
    by_series = {
        "EFFR": ("3.65", "3.63"),
        "DFEDTARU": ("3.75", "3.75"),
        "DFEDTARL": ("3.50", "3.50"),
        "EFFR1": ("3.56", "3.54"),
        "EFFR25": ("3.62", "3.60"),
        "EFFR75": ("3.70", "3.68"),
        "EFFR99": ("3.82", "3.80"),
        "EFFRVOL": ("44.9", "45.3"),
        "EFFRL": ("3.55", "3.53"),
        "EFFRH": ("3.85", "3.83"),
        "EFFRSD": ("0.05", "0.04"),
    }
    return _seam_envelope(
        _OBSERVATIONS_URL,
        _fanout_pages(
            _FED_FUNDS_SERIES,
            limit=_FED_FUNDS_PAGE_LIMIT,
            values_for=lambda sid: [
                ("2026-01-02", by_series[sid][0]),
                ("2026-01-05", by_series[sid][1]),
            ],
        ),
    )


def _fred_nonfarm_payrolls_document() -> dict[str, Any]:
    """Release-50 table tree for the ``employees_nsa`` element (5645, iter2_macro.py:112-115).

    The documented ``/fred/release/tables`` shape is a ``release_id`` plus an ``elements`` map of
    tree nodes; ``FredReleaseTableFetcher.transform_data`` walks the nodes and only the
    ``type == "series"`` leaves reach ``FredNonFarmPayroll``, where ``value`` is a required float.
    """
    payload: dict[str, Any] = {
        "release_id": 50,
        "elements": {
            "5646": {
                "element_id": 5646,
                "parent_id": 5645,
                "line": 1,
                "type": "series",
                "name": "Total, All Employees",
                "level": 2,
                "series_id": "CES0000000001",
                "children": [],
                "date": "2025-11-01",
                "value": "1586356.0",
            },
            "5647": {
                "element_id": 5647,
                "parent_id": 5645,
                "line": 2,
                "type": "series",
                "name": "Total, All Employees",
                "level": 2,
                "series_id": "CES0000000001",
                "children": [],
                "date": "2025-12-01",
                "value": "1587021.0",
            },
        },
    }
    return {"responses": [{"url_contains": _RELEASE_TABLES_URL, "body": payload}]}


MODELS: Final[dict[str, dict[str, Any]]] = {
    "ConsumerPriceIndex": {
        "fixture_document": _fred_consumer_price_index_document,
        "query": {},
        "tamper": {"op": "set", "path": ["observations", 1, "value"], "value": "not-a-rate"},
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "publishing prose in the 'value' token breaks the required float 'value' column of "
            "FredConsumerPriceIndex (FRED text is the only source for it, and '.' is the only "
            "missing marker), so _FixedMacroFetcher._validate_raw must refuse with "
            "FRED_BAD_OBSERVATION instead of coercing the cell"
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
    "NonFarmPayrolls": {
        "fixture_document": _fred_nonfarm_payrolls_document,
        "query": {"category": "employees_nsa"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "elements", "5646", "value"],
            "value": "not-a-payroll",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in a release-table series node's 'value' breaks the required float 'value' "
            "column of FredNonFarmPayroll, which is read only through _parse_value's numeric "
            "text or '.' rule, so the release-table walk must refuse with FRED_BAD_OBSERVATION "
            "instead of publishing an invented payroll level"
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
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models._client"],
            "attr": "get_shared_http_client",
        },
    },
    "BondIndices": {
        "fixture_document": _fred_bond_indices_document,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 3, "observations", 0, "value"],
            "value": "not-a-rate",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in the BAMLC4A0C710YEY page's 'value' token breaks the required float 'value' "
            "column of FredBondIndices (_number accepts ASCII numeric text or the '.' sentinel "
            "only), so the bond-index normalizer must refuse the whole six-leg fan-out with "
            "FRED_BAD_OBSERVATION instead of coercing the cell"
        ),
        "source_files": [
            *_CONFIG_SOURCE_FILES,
            "opendata/data/providers/fred/models/iter2_rates_extra.py",
            "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_rates_extra.py",
            "opendata/data/domains.yaml",
        ],
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models.iter2_rates_extra"],
            "attr": "get_shared_http_client",
        },
    },
    "CommercialPaper": {
        "fixture_document": _fred_commercial_paper_document,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 10, "observations", 0, "value"],
            "value": "not-a-rate",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in the eleventh commercial-paper leg's 'value' token breaks the required float "
            "'rate' column of FredCommercialPaper, whose only source is FRED's ASCII numeric text "
            "with '.' as the missing marker, so the 24-series fan-out must refuse with "
            "FRED_BAD_OBSERVATION instead of coercing the cell"
        ),
        "source_files": [
            *_CONFIG_SOURCE_FILES,
            "opendata/data/providers/fred/models/iter2_rates_extra.py",
            "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_rates_extra.py",
            "opendata/data/domains.yaml",
        ],
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models.iter2_rates_extra"],
            "attr": "get_shared_http_client",
        },
    },
    "MortgageIndices": {
        "fixture_document": _fred_mortgage_indices_document,
        "query": {},
        "tamper": {"op": "set", "path": ["observations", 0, "value"], "value": "not-a-rate"},
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in a replayed 'value' token breaks the required float 'rate' column of "
            "FredMortgageIndex, which _parse_number reads only from FRED's numeric text (the "
            "'.' sentinel meaning missing, never a guessable rate), so every OBMMI leg of the "
            "primary fan-out must refuse with FRED_BAD_OBSERVATION"
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
    "YieldCurve": {
        "fixture_document": _fred_yield_curve_document,
        "query": {"yield_curve_type": "real", "date": _YIELD_CURVE_TARGET.isoformat()},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 2, "observations", 1, "value"],
            "value": "not-a-rate",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "the real-curve FredYieldCurve 'rate' column keeps the documented float plus '.' "
            "null-sentinel ruling, so a genuinely malformed DFII10 token - prose where numeric "
            "text is required - is what _number must reject: the curve normalizer has to refuse "
            "with FRED_BAD_OBSERVATION rather than drop a maturity or coerce the cell"
        ),
        "source_files": [
            *_CONFIG_SOURCE_FILES,
            "opendata/data/providers/fred/models/iter2_rates_extra.py",
            "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_rates_extra.py",
            "opendata/data/domains.yaml",
        ],
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models.iter2_rates_extra"],
            "attr": "get_shared_http_client",
        },
    },
    "CommoditySpotPrices": {
        "fixture_document": _fred_commodity_spot_prices_document,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 5, "observations", 0, "value"],
            "value": "not-a-price",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in the DHOILNYH leg's 'value' token breaks the required float 'price' column "
            "of FredCommoditySpotPrices, whose documented source rule is ASCII numeric text with "
            "'.' meaning no price observation, so the twelve-commodity fan-out must refuse with "
            "FRED_BAD_OBSERVATION instead of coercing the cell"
        ),
        "source_files": [
            *_CONFIG_SOURCE_FILES,
            "opendata/data/providers/fred/models/iter2_rates_extra.py",
            "opendata/data/providers/fred/models/_fixed_observation_pages.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_rates_extra.py",
            "opendata/data/domains.yaml",
        ],
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models.iter2_rates_extra"],
            "attr": "get_shared_http_client",
        },
    },
    "FederalFundsRate": {
        "fixture_document": _fred_federal_funds_document,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 0, "observations", 0, "value"],
            "value": "not-a-rate",
        },
        "refusal_class": _SOURCE_REFUSAL_CLASS,
        "refusal_code": "FRED_BAD_OBSERVATION",
        "tamper_reason": (
            "prose in the EFFR page's 'value' token breaks the required float 'rate' column of "
            "FredFederalFundsRate - the one joined field the contract forbids null for, since "
            "_parse_value accepts only ASCII numeric text or the '.' missing sentinel - so the "
            "eleven-series join must refuse with FRED_BAD_OBSERVATION instead of publishing a "
            "funds row without its rate"
        ),
        "source_files": [
            *_CONFIG_SOURCE_FILES,
            "opendata/data/providers/fred/models/federal_funds.py",
            "opendata/data/providers/fred/models/series.py",
            "opendata/data/providers/fred/models/_paged.py",
        ],
        "contract_files": [
            "opendata/data/models/fred_iter2.py",
            "opendata/data/domains.yaml",
        ],
        "client_seam": {
            "kind": "shared_http_client",
            "modules": ["opendata.data.providers.fred.models._client"],
            "attr": "get_shared_http_client",
        },
    },
}
