"""Wave 32 shard: offline witnesses for five more yfinance rows.

Same governed seam as the landed ``EquityQuote`` entry (:func:`..._SEAM`, kind
``governed_sdk_session``): the runner replaces ``governed_yfinance_session`` with a fake
``curl_cffi`` session and the installed yfinance 0.2.66 builds its own requests against it.
What differs per row is which legs the SDK asks that session for:

``EquityHistorical`` / ``EtfHistorical`` / ``IndexHistorical``
    ``Ticker.history(start, end)`` (via ``require_history_snapshot``) first runs
    ``_fetch_ticker_tz`` -- a ``v8/finance/chart/{symbol}?interval=1d&range=1d`` probe whose
    meta ``exchangeTimezoneName`` the price-history object is constructed with -- then the
    bar leg, which carries ``period1``/``period2`` (the exchange-local midnight epochs of
    the query's inclusive window, ``end`` shifted by the seam's one day) instead of
    ``range``. That is the request shape ``EquityQuote`` never showed: the landed row's
    legs were ``range``-keyed FastInfo windows, and only the sorted query string separates
    the tz probe from the bar leg here. Bars are stamped at exchange-local midnight of
    fixed weekday session dates (2026-09-01..2026-09-10) so the SDK's
    own ``set_df_tz`` keeps every declared session day and ``_history_rows`` keeps every
    row. Emptying the bar leg's ``chart.result`` is the tamper: PriceHistory takes its
    fail branch, logs the missing-price warning and hands back the empty frame, and
    ``_history_rows`` must refuse it through the typed
    ``YfinanceProviderError``/``YFINANCE_EMPTY_RESPONSE`` rather than publish bars the
    source never returned. The query end date sits in the far future so the bar leg always
    runs through the plain governed ``get``: a past end would route it through the
    process-global ``cache_get`` lru on the YfData singleton, and the counterfact's tampered
    second run would replay the control response without ever asking the fresh fake.

``EquityInfo``
    ``Ticker.get_info`` -> ``Info.info``: the five-module ``v10/finance/quoteSummary`` leg,
    the ``v7/finance/quote`` complementary leg (declared here as an honest empty
    ``quoteResponse`` -- 0.2.66 merges whichever leg carries rows), and the
    fundamentals-timeseries trailingPegRatio probe. Measured: emptying the summary's
    ``result`` does NOT refuse -- 0.2.66's complementary probe injects a null
    trailingPegRatio key that keeps the info mapping truthy and the product ships that as
    a valid snapshot -- so the tamper replaces the summary leg's object envelope with a
    bare array instead, which breaks the SDK's own merge inside ``_fetch_info`` and makes
    ``require_info`` refuse with the typed ``YFINANCE_REQUEST_FAILED``.

``EtfInfo``
    ``require_fund_sections`` drives ``FundsData``, one quoteSummary leg with
    ``modules=quoteType,summaryProfile,topHoldings,fundProfile``; every parsed section the
    nine documented properties read is carried in the fixture body. Tampering
    ``quoteSummary.result[0]`` to an empty object makes the SDK's own ``data["quoteType"]``
    lookup fail with the KeyError branch that raises ``YFDataException`` before any
    attribute is cached, and ``require_fund_sections`` re-raises it as
    ``YFINANCE_REQUEST_FAILED``.

``^GSPC``'s chart path is declared raw (the SDK interpolates the ticker into the URL and
the fake matches the effective URL string), and its bar leg carries no ``adjclose``
indicator -- an index publishes no adjusted series.
"""

from __future__ import annotations

from typing import Any, Final

import pandas as pd

_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_CRUMB: Final[str] = "fixture-crumb"
_TZ: Final[str] = "America/New_York"
_EVENTS: Final[str] = "events=div%2Csplits%2CcapitalGains"

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/market_iter2.py",
    "opendata/data/providers/yfinance/models/iter2.py",
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
    "opendata/data/providers/yfinance/models/stock_daily.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]

#: Fixed window. ``end`` sits in the far future ON PURPOSE: PriceHistory routes a whose
#: end is in the past through ``cache_get`` (an lru on the process-global YfData singleton),
#: so a past end would let the counterfact's tampered second run replay the control run's
#: in-memory response and never ask the fresh fake -- the empty guard would be unreachable
#: and the tamper would read as "did not bite". A future end forces the plain governed
#: ``get`` leg in every run, and the query's own window filter still keeps only the seven
#: declared session days.
_WINDOW_START: Final[str] = "2026-09-01"
_WINDOW_END: Final[str] = "2100-01-01"
_SESSION_DAYS: Final[list[str]] = [
    "2026-09-01",
    "2026-09-02",
    "2026-09-03",
    "2026-09-04",
    "2026-09-08",
    "2026-09-09",
    "2026-09-10",
]


def _period(date_str: str) -> int:
    """The exchange-local-midnight epoch the SDK puts into period1/period2 for one date."""
    return int(pd.Timestamp(date_str, tz=_TZ).timestamp())


def _stamps() -> list[int]:
    return [_period(day) for day in _SESSION_DAYS]


def _closes(base: float) -> list[float]:
    return [round(base + 1.5 * index + 0.25 * (-1) ** index, 2) for index in range(7)]


def _chart_envelope(
    symbol: str,
    instrument_type: str,
    base: float,
    *,
    with_adjclose: bool,
) -> dict[str, Any]:
    """One v8 chart answer carrying exactly the seven declared session days."""
    stamps = _stamps()
    closes = _closes(base)
    quote = {
        "open": [round(c - 0.4, 2) for c in closes],
        "high": [round(c + 0.9, 2) for c in closes],
        "low": [round(c - 0.9, 2) for c in closes],
        "close": closes,
        "volume": [40_000_000 + 1_000_000 * index for index in range(len(closes))],
    }
    indicators: dict[str, Any] = {"quote": [quote]}
    if with_adjclose:
        indicators["adjclose"] = [{"adjclose": closes}]
    else:
        quote["volume"] = [0] * len(closes)
    result: dict[str, Any] = {
        "meta": {
            "currency": "USD",
            "symbol": symbol,
            "exchangeName": "NMS" if instrument_type == "EQUITY" else "NGM",
            "fullExchangeName": "NasdaqGS" if instrument_type == "EQUITY" else "Nasdaq",
            "exchangeTimezoneName": _TZ,
            "exchangeTimezoneAbbreviation": "EDT",
            "instrumentType": instrument_type,
            "regularMarketPrice": closes[-1],
            "regularMarketVolume": 45_000_000,
            "regularMarketTime": stamps[-1] + 23400,
            "previousClose": closes[-2],
            "chartPreviousClose": closes[-2],
            "fiftyTwoWeekHigh": round(closes[-1] * 1.1, 2),
            "fiftyTwoWeekLow": round(closes[0] * 0.9, 2),
            "firstTradeDate": _period("2010-01-04"),
            "dataGranularity": "1d",
            "validRanges": ["1d", "5d", "1mo", "3mo", "6mo", "1y", "5y", "max"],
            "validIntervals": ["1m", "5m", "1h", "1d"],
            "currentTradingPeriod": {
                "regular": {"start": stamps[-1] + 14400, "end": stamps[-1] + 23400},
                "pre": {"start": stamps[-1] + 10800, "end": stamps[-1] + 14400},
                "post": {"start": stamps[-1] + 23400, "end": stamps[-1] + 28800},
            },
            "tradingPeriods": {"pre": [], "post": []},
        },
        "timestamp": stamps,
        "events": {},
        "indicators": indicators,
    }
    return {"chart": {"result": [result], "error": None}}


def _bootstrap() -> list[dict[str, Any]]:
    """The session crumb/cookie legs every governed yfinance request bootstraps through."""
    return [
        {"url_contains": "fc.yahoo.com", "body": ""},
        {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
    ]


def _history_needles(symbol: str) -> tuple[str, str]:
    """The two chart needles one history fetch produces, in the order the SDK asks for them."""
    tz_needle = f"chart/{symbol}?crumb={_CRUMB}&interval=1d&range=1d"
    period1 = _period(_WINDOW_START)
    period2 = _period((pd.Timestamp(_WINDOW_END) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"))
    bar_needle = (
        f"chart/{symbol}?crumb={_CRUMB}&{_EVENTS}&includePrePost=False&interval=1d"
        f"&period1={period1}&period2={period2}"
    )
    return tz_needle, bar_needle


def _history_document(
    symbol: str, instrument_type: str, base: float, with_adjclose: bool
) -> dict[str, Any]:
    tz_needle, bar_needle = _history_needles(symbol)
    envelope = _chart_envelope(symbol, instrument_type, base, with_adjclose=with_adjclose)
    return {
        "responses": [
            *_bootstrap(),
            {"url_contains": tz_needle, "body": envelope},
            {"url_contains": bar_needle, "body": envelope},
        ]
    }


def _history_query(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "start_date": _WINDOW_START,
        "end_date": _WINDOW_END,
        "interval": "1d",
    }


def _equity_info_document() -> dict[str, Any]:
    """Company snapshot legs: five-module quoteSummary carries every field the row reads."""
    summary = {
        "quoteSummary": {
            "result": [
                {
                    "quoteType": {
                        "symbol": "AAPL",
                        "exchange": "NMS",
                        "quoteType": "EQUITY",
                        "currency": "USD",
                        "longName": "Apple Inc.",
                    },
                    "assetProfile": {
                        "address": {"city": "Cupertino", "state": "CA"},
                        "longBusinessSummary": "Designs and sells smartphones and computers.",
                        "employees": 164000,
                    },
                    "summaryDetail": {
                        "previousClose": 246.61,
                        "dayHigh": {"raw": 247.9, "fmt": "247.90"},
                        "fiftyTwoWeekHigh": {"raw": 256.72, "fmt": "256.72"},
                    },
                    "defaultKeyStatistics": {"trailingEps": {"raw": 6.58, "fmt": "6.58"}},
                    "financialData": {"totalRevenue": {"raw": 391000000000, "fmt": "391B"}},
                }
            ],
            "error": None,
        }
    }
    return {
        "responses": [
            *_bootstrap(),
            {"url_contains": "v10/finance/quoteSummary", "body": summary},
            {
                "url_contains": "v7/finance/quote",
                "body": {"quoteResponse": {"result": [], "error": None}},
            },
            {
                "url_contains": "fundamentals-timeseries",
                "body": {"timeseries": {"result": [{}], "error": None}},
            },
        ]
    }


def _etf_info_document() -> dict[str, Any]:
    """The one FundsData quoteSummary leg, carrying every section the nine properties parse."""
    raw = lambda value: {"raw": value, "fmt": str(value)}  # noqa: E731
    summary = {
        "quoteSummary": {
            "result": [
                {
                    "quoteType": {"quoteType": "ETF"},
                    "summaryProfile": {"longBusinessSummary": "Tracks the S&P 500 index."},
                    "topHoldings": {
                        "cashPosition": raw(0.011),
                        "stockPosition": raw(0.981),
                        "bondPosition": raw(0.0),
                        "preferredPosition": raw(0.0),
                        "convertiblePosition": raw(0.0),
                        "otherPosition": raw(0.008),
                        "holdings": [
                            {
                                "symbol": "AAPL",
                                "holdingName": "Apple Inc.",
                                "holdingPercent": 0.071,
                            },
                            {
                                "symbol": "MSFT",
                                "holdingName": "Microsoft Corporation",
                                "holdingPercent": 0.064,
                            },
                        ],
                        "equityHoldings": {
                            "priceToEarnings": raw(25.4),
                            "priceToBook": raw(9.8),
                            "priceToSales": raw(6.1),
                            "priceToCashflow": raw(14.7),
                            "medianMarketCap": raw(284_000_000_000),
                            "threeYearEarningsGrowth": raw(0.11),
                        },
                        "bondHoldings": {
                            "duration": raw(0.02),
                            "maturity": raw(0.03),
                            "creditQuality": raw(0.0),
                        },
                        "bondRatings": [],
                        "sectorWeightings": [
                            {"realestate": 0.024},
                            {"consumer_cyclical": 0.104},
                            {"basic_materials": 0.028},
                            {"consumer_defensive": 0.068},
                            {"technology": 0.313},
                            {"communication_services": 0.089},
                            {"energy": 0.034},
                            {"utilities": 0.025},
                            {"financial": 0.129},
                            {"healthcare": 0.111},
                            {"industrials": 0.075},
                        ],
                    },
                    "fundProfile": {
                        "categoryName": "Equity-US-Large",
                        "family": "SPDR",
                        "legalType": "ETF",
                        "feesExpensesInvestment": {
                            "annualReportExpenseRatio": raw(0.0009),
                            "annualHoldingsTurnover": raw(3.5),
                            "totalNetAssets": raw(700_000_000_000),
                        },
                        "feesExpensesInvestmentCat": {
                            "annualReportExpenseRatio": raw(0.0045),
                            "annualHoldingsTurnover": raw(24.0),
                            "totalNetAssets": raw(12_000_000_000_000),
                        },
                    },
                }
            ],
            "error": None,
        }
    }
    return {
        "responses": [
            *_bootstrap(),
            {"url_contains": "v10/finance/quoteSummary", "body": summary},
        ]
    }


_HISTORY_TAMPER_REASON = (
    "an empty chart result keeps the envelope well-formed for the crumb, the tz probe and "
    "the URL the SDK built from the query window, and only the bar series the contract rows "
    "are computed from disappears: PriceHistory takes its fail branch and returns the empty "
    "frame, and _history_rows must refuse it with "
    "YfinanceProviderError/YFINANCE_EMPTY_RESPONSE rather than publish bars the source never "
    "returned"
)


def _history_entry(
    symbol: str,
    instrument_type: str,
    base: float,
    with_adjclose: bool,
) -> dict[str, Any]:
    def document() -> dict[str, Any]:
        return _history_document(symbol, instrument_type, base, with_adjclose)

    return {
        "fixture_document": document,
        "client_seam": _SEAM,
        "query": _history_query(symbol),
        "tamper": {
            "op": "set",
            "path": ["responses", 3, "body", "chart", "result"],
            "value": [],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _HISTORY_TAMPER_REASON,
        "source_files": list(_SOURCE_FILES),
        "contract_files": list(_CONTRACT_FILES),
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityInfo": {
        "fixture_document": _equity_info_document,
        "client_seam": _SEAM,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 2, "body"],
            "value": [],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_REQUEST_FAILED",
        "tamper_reason": (
            "a quoteSummary leg that answers with a bare array instead of the object "
            "envelope keeps every URL, cookie and crumb leg intact, but the SDK's own "
            "envelope merge inside _fetch_info fails on the missing mapping surface and "
            "require_info must translate that parse-time break through its typed "
            "YfinanceProviderError/YFINANCE_REQUEST_FAILED rather than publish the "
            "complementary-probe-only snapshot the empty-result variant would leave behind "
            "(that variant is measured not to refuse: 0.2.66's trailingPegRatio probe "
            "injects a null key that keeps the mapping truthy, so the product genuinely "
            "accepts it)"
        ),
        "source_files": list(_SOURCE_FILES),
        "contract_files": list(_CONTRACT_FILES),
    },
    "EquityHistorical": _history_entry("AAPL", "EQUITY", 240.0, True),
    "EtfHistorical": _history_entry("SPY", "ETF", 560.0, True),
    "EtfInfo": {
        "fixture_document": _etf_info_document,
        "client_seam": _SEAM,
        "query": {"symbol": "SPY"},
        "tamper": {
            "op": "set",
            "path": ["responses", 2, "body", "quoteSummary", "result", 0],
            "value": {},
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_REQUEST_FAILED",
        "tamper_reason": (
            "a result object with none of the sections FundsData parses still matches the "
            "crumb and the URL, but the SDK's own data['quoteType'] lookup takes the KeyError "
            "branch that raises YFDataException('No Fund data found.') before any section is "
            "cached, and require_fund_sections must surface that transport-shaped refusal "
            "through its declared stable code rather than publish a payload of empty sections"
        ),
        "source_files": list(_SOURCE_FILES),
        "contract_files": list(_CONTRACT_FILES),
    },
    "IndexHistorical": _history_entry("^GSPC", "INDEX", 6600.0, False),
}
