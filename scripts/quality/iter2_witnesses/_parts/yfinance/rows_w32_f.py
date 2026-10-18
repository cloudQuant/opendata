"""Offline witnesses for the four yfinance *predefined screen* rows.

Every row here is one Yahoo saved screen: ``YfinanceEquityUndervaluedGrowthFetcher``,
``YfinanceEquityUndervaluedLargeCapsFetcher``, ``YfinanceEquityAggressiveSmallCapsFetcher``
and ``YfinanceGrowthTechEquitiesFetcher`` all subclass ``YfinanceScreenerFetcher``
(``market_iter2.py:1202``) and differ only in the class-level ``query_name`` / ``sort_field``
they hand to ``_sdk.require_screen(predefined=True, ...)``. So the four share one SDK call
family and one refusal site, and that family is reachable through the landed
``governed_sdk_session`` seam:

``require_screen`` runs inside ``_governed_sdk_call``, whose only HTTP-establishing point is
``with governed_yfinance_session(sdk) as session:`` (``_sdk.py:78``), and it then calls
``yfinance.screen(query_name, offset=..., count=..., sortField=..., sortAsc=..., session=...)``.
In the installed 0.2.66 SDK, ``screener/screener.py:138`` takes the ``offset is not None and
isinstance(query, str)`` branch -- this repo always passes ``offset`` (0 on the first page), so
the predefined name is looked up in ``PREDEFINED_SCREENER_QUERIES`` and the leg goes to Yahoo's
*custom* screener endpoint: one POST to ``https://query1.finance.yahoo.com/v1/finance/screener``
with the screen's EquityQuery serialized in the JSON body and the fixed query-string
``corsDomain/crumb/formatted/lang/region``. The rows therefore request the same URL with the
same parameters and are told apart only by the body, which the fake cannot inspect; each row
nonetheless gets its own fixture file, and the tamper of every row bites its own document.

Beyond ``EquityQuote`` the screen surface needs nothing new from the seam -- no chart legs, no
fundamentals timeseries, no quoteSummary. It needs the cookie bootstrap and the crumb the same
``YfData`` orchestration performs, then exactly one POST whose envelope the SDK unwraps as
``json()["finance"]["result"][0]`` and ``require_screen`` validates as
``{"quotes": [<mapping>, ...]}``. The bodies are **hand-authored fixture bodies, not live
captures**: the quote objects carry the metric fields each screen filters on, and no claim is
made that finance.yahoo.com published these rows on the run date.

Refusal site read honestly, and proven per row by the traceback tail of its tampered leg
(``_sdk.py:81 in wrapped`` -> the raise below): a document whose row array is absent or is not a
list is refused by ``_sdk.require_screen`` at ``_sdk.py:435``, and a document whose array carries
a non-Mapping element is refused by the same function's per-row arm at ``_sdk.py:437`` -- both
``YfinanceProviderError`` / ``YFINANCE_SCREEN_SHAPE_INVALID`` with ``status=None``, so
EquityUndervaluedGrowth and EquityUndervaluedLargeCaps refuse at ``:435`` and
EquityAggressiveSmallCaps and GrowthTechEquities at ``:437``. The same class and code appear as a
backstop in ``transform_data`` at ``market_iter2.py:1247``, but ``require_screen`` validates
before ``_fetch_screen_pages`` (``market_iter2.py:1189``) or ``transform_data`` ever see the
payload, so a document tamper cannot reach that guard first and no entry here claims it did. The
witness declares the code the shipped path actually raises and the site it actually raises from.
"""

from __future__ import annotations

from typing import Any, Final

#: The one attribute the screen legs consume as ``with attr(sdk) as session:`` at ``_sdk.py:78``.
_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_CRUMB: Final[str] = "fixture-crumb"

#: The effective URL 0.2.66 builds for a predefined screen: the custom screener endpoint plus
#: its sorted query string (the fake matches URL + encoded ``params``, and the screen's own
#: offset/count/sort live in the POST body, so this needle is the whole discriminator).
_SCREENER_NEEDLE: Final[str] = (
    "v1/finance/screener?corsDomain=finance.yahoo.com"
    f"&crumb={_CRUMB}&formatted=false&lang=en-US&region=US"
)

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/market_iter2.py",
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
    "opendata/data/providers/yfinance/models/stock_daily.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]

# Hand-authored rows for each saved screen; the metric fields mirror what the screen filters on.
_UNDervalued_GROWTH_QUOTES: Final[list[dict[str, Any]]] = [
    {
        "symbol": "HELE",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 41.28,
        "trailingPE": 9.4,
        "pegRatio": 0.58,
        "epsGrowth": 32.6,
        "intradayMarketCap": 1754000000,
        "eodVolume": 214500,
        "dayVolume": 231800,
    },
    {
        "symbol": "CBU",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 57.11,
        "trailingPE": 11.2,
        "pegRatio": 0.74,
        "epsGrowth": 27.9,
        "intradayMarketCap": 3412000000,
        "eodVolume": 128400,
        "dayVolume": 140200,
    },
    {
        "symbol": "PSMT",
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 168.9,
        "trailingPE": 14.6,
        "pegRatio": 0.91,
        "epsGrowth": 25.4,
        "intradayMarketCap": 5990000000,
        "eodVolume": 96300,
        "dayVolume": 101700,
    },
]

_UNDervalued_Large_Caps_QUOTES: Final[list[dict[str, Any]]] = [
    {
        "symbol": "VOYA",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 57.62,
        "trailingPE": 8.1,
        "pegRatio": 0.66,
        "intradayMarketCap": 11420000000,
        "eodVolume": 1734000,
        "dayVolume": 1802000,
    },
    {
        "symbol": "KMX",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 106.34,
        "trailingPE": 12.7,
        "pegRatio": 0.83,
        "intradayMarketCap": 12980000000,
        "eodVolume": 921000,
        "dayVolume": 964000,
    },
    {
        "symbol": "PH",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 352.4,
        "trailingPE": 18.2,
        "pegRatio": 0.97,
        "intradayMarketCap": 16450000000,
        "eodVolume": 348000,
        "dayVolume": 361500,
    },
]

_Aggressive_Small_Caps_QUOTES: Final[list[dict[str, Any]]] = [
    {
        "symbol": "SMPL",
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 62.75,
        "epsGrowth": 11.4,
        "intradayMarketCap": 1318000000,
        "eodVolume": 268400,
        "dayVolume": 274100,
    },
    {
        "symbol": "CWH",
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 5.94,
        "epsGrowth": 8.2,
        "intradayMarketCap": 812000000,
        "eodVolume": 1402000,
        "dayVolume": 1516000,
    },
    {
        "symbol": "EVTC",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "regularMarketPrice": 30.06,
        "epsGrowth": 13.9,
        "intradayMarketCap": 1104000000,
        "eodVolume": 187300,
        "dayVolume": 195600,
    },
]

_Growth_Tech_QUOTES: Final[list[dict[str, Any]]] = [
    {
        "symbol": "ZS",
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "sector": "Technology",
        "quarterlyRevenueGrowth": 27.4,
        "epsGrowth": 41.2,
        "regularMarketPrice": 168.22,
        "eodVolume": 2841000,
        "dayVolume": 2913000,
    },
    {
        "symbol": "NET",
        "exchange": "NYQ",
        "quoteType": "EQUITY",
        "currency": "USD",
        "sector": "Technology",
        "quarterlyRevenueGrowth": 28.9,
        "epsGrowth": 33.5,
        "regularMarketPrice": 74.15,
        "eodVolume": 6420000,
        "dayVolume": 6604000,
    },
    {
        "symbol": "DDOG",
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "sector": "Technology",
        "quarterlyRevenueGrowth": 26.1,
        "epsGrowth": 29.8,
        "regularMarketPrice": 92.48,
        "eodVolume": 3115000,
        "dayVolume": 3208000,
    },
]


def _screen_document(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    """The three-leg envelope one predefined screen rides.

    Two legs are the ``YfData`` bootstrap the SDK performs before it can ask anything
    crumb-bearing (the cookie seed and ``v1/test/getcrumb``), the third is the screen itself.
    The screen body is wire-shaped -- ``{"finance": {"result": [{...}]}}`` -- because 0.2.66
    unwraps ``json()["finance"]["result"][0]`` itself; the row array this repo validates is the
    ``quotes`` key of that first result.
    """
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                "url_contains": _SCREENER_NEEDLE,
                "method": "POST",
                "body": {
                    "finance": {
                        "result": [{"total": len(quotes), "count": len(quotes), "quotes": quotes}],
                        "error": None,
                    }
                },
            },
        ]
    }


def _document_undervalued_growth() -> dict[str, Any]:
    return _screen_document([dict(row) for row in _UNDervalued_GROWTH_QUOTES])


def _document_undervalued_large_caps() -> dict[str, Any]:
    return _screen_document([dict(row) for row in _UNDervalued_Large_Caps_QUOTES])


def _document_aggressive_small_caps() -> dict[str, Any]:
    return _screen_document([dict(row) for row in _Aggressive_Small_Caps_QUOTES])


def _document_growth_tech_equities() -> dict[str, Any]:
    return _screen_document([dict(row) for row in _Growth_Tech_QUOTES])


_ROW_PATH: Final[list[Any]] = ["responses", 2, "body", "finance", "result", 0]

MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityUndervaluedGrowth": {
        "fixture_document": _document_undervalued_growth,
        "client_seam": _SEAM,
        "query": {"limit": 3},
        "tamper": {"op": "drop", "path": [*_ROW_PATH, "quotes"]},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SCREEN_SHAPE_INVALID",
        "tamper_reason": (
            "the undervalued-growth screen is answered by Yahoo's screener endpoint, and the "
            "only thing this repo maps rows from is the row array inside "
            "finance.result[0].quotes -- dropping that key keeps the envelope valid at every "
            "layer the SDK reads (status, the result list, total/count) while the rows "
            "YfinanceEquityUndervaluedGrowthFetcher publishes disappear, so the refusal is the "
            "arm that actually sees it: require_screen's own list check at _sdk.py:435 raises "
            "YFINANCE_SCREEN_SHAPE_INVALID on the page the SDK just unwrapped, before "
            "_fetch_screen_pages or transform_data can turn an absent array into an empty list "
            "the source never said was empty"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "EquityUndervaluedLargeCaps": {
        "fixture_document": _document_undervalued_large_caps,
        "client_seam": _SEAM,
        "query": {"limit": 3},
        "tamper": {"op": "set", "path": [*_ROW_PATH, "quotes"], "value": {}},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SCREEN_SHAPE_INVALID",
        "tamper_reason": (
            "a screen result whose rows arrive as a symbol-keyed object rather than the "
            "ordered array the endpoint documented is exactly the upstream re-shape this "
            "provider refuses: the object is a Mapping and JSON-valid, so nothing before the "
            "row-array check notices, and YfinanceEquityUndervaluedLargeCapsFetcher would "
            "otherwise iterate dict keys as quotes and lose every row's ordinal; the list check "
            "that fires is require_screen's at _sdk.py:435, measured on this row's tampered leg, "
            "raising YFINANCE_SCREEN_SHAPE_INVALID and not the same-code transform_data backstop "
            "at market_iter2.py:1247 that a document tamper never reaches"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "EquityAggressiveSmallCaps": {
        "fixture_document": _document_aggressive_small_caps,
        "client_seam": _SEAM,
        "query": {"limit": None},
        "tamper": {"op": "set", "path": [*_ROW_PATH, "quotes", 0], "value": "SMPL"},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SCREEN_SHAPE_INVALID",
        "tamper_reason": (
            "this screen defaults to every source row, so one row of the returned array is the "
            "smallest real defect the fetch can meet: a row the endpoint published as a bare "
            "ticker string instead of a quote object still leaves the array length, total and "
            "count intact, and it survives require_screen's list check at _sdk.py:435 -- the "
            "refusal measured on this row is the very next arm, the per-row Mapping check at "
            "_sdk.py:437, which raises YFINANCE_SCREEN_SHAPE_INVALID because _screen_rows would "
            "otherwise have no Mapping to keep as raw_payload_json and no symbol to read"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "GrowthTechEquities": {
        "fixture_document": _document_growth_tech_equities,
        "client_seam": _SEAM,
        "query": {"limit": 3},
        "tamper": {"op": "set", "path": [*_ROW_PATH, "quotes", 1], "value": None},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SCREEN_SHAPE_INVALID",
        "tamper_reason": (
            "the growth-technology screen filters on sector and revenue-growth fields a quote "
            "object carries, and a null row is what the endpoint returns when one quote fails "
            "server-side enrichment: the array stays a list of the documented length, so "
            "require_screen's list check at _sdk.py:435 passes and only its per-row Mapping arm "
            "at _sdk.py:437 can catch it -- measured on this row's tampered leg -- and it refuses "
            "with YFINANCE_SCREEN_SHAPE_INVALID instead of letting a null row through to symbol "
            "extraction or a silent gap in the ordinal sequence"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
}
