"""Offline witness configuration for the ``yfinance`` provider.

yfinance's fetch legs never reach an engine transport or ``get_shared_http_client``: every SDK
call runs inside ``opendata.data.providers.yfinance.models._sdk._governed_sdk_call``, whose
HTTP-establishing point is ``with governed_yfinance_session(sdk) as session:`` at ``_sdk.py:78``
-- one positional argument consumed as a context manager. The landed ``shared_http_client`` seam
patches a 0-positional factory to ``lambda: client`` and cannot serve that shape without
swallowing the positional and silently passing the call through to the real client, which is
exactly the refusal that :mod:`iter2_claims_probe_runner` prices in
``docs/evidence/C128/plane1-residue-readiness-20261017/probes/yfinance_shard_entry_price.py``
arm F. This witness therefore declares the runner's ``governed_sdk_session`` kind: the patched
attribute is replaced by a one-positional factory that records the SDK object it was handed and
returns the fake session itself, and the fake is a real ``curl_cffi.requests.Session`` subclass
(the type ``yfinance.data.YfData._set_session`` demands) whose ``get``/``post`` answer the
fixture envelope. The installed SDK (0.2.66) does its own crumb/cookie orchestration, request
construction and chart parsing against that fake, and this repo's normalizer then maps the
result -- both under test, no socket open.

The seam stays enforceable on both arms, read off what the fetch actually did: the runner's
:func:`iter2_claims_probe_runner._seam_guard` refuses if the patched attribute was never
invoked (the fake records every hand-off) and if the fake session was never asked anything.

``EquityQuote`` rides ``require_fast_info``: the SDK's lazy ``FastInfo`` dict materializes
against a canned envelope -- one cookie bootstrap, one crumb, the 1h/5d intraday chart leg (the
SDK requests intraday bars to receive the exchange schedule), the 1d/1y daily chart leg, a
fundamentals-timeseries leg, and the quote/quoteSummary complementary legs. The bodies are
**hand-authored fixture bodies, not live captures**: the price series is built from the run
date forward so the SDK's own ``utcnow`` windows (year change, 50/200-day averages) admit the
bars, and no claim is made that finance.yahoo.com published these values.
"""

from __future__ import annotations

import math
from typing import Any, Final

import pandas as pd

#: One attribute, consumed as ``with attr(sdk) as session:`` at ``_sdk.py:78``.
_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_SYMBOL: Final[str] = "AAPL"
_CRUMB: Final[str] = "fixture-crumb"

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
    "opendata/data/providers/yfinance/models/stock_daily.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]


def _bars(count: int, freq: str, base: float) -> tuple[list[int], list[float]]:
    """One deterministic synthetic price series ending at the run's current time."""
    index = pd.date_range(end=pd.Timestamp.utcnow().tz_convert("UTC"), periods=count, freq=freq)
    closes = [round(base + 6.0 * math.sin(i / 9.0) + 0.05 * i, 2) for i in range(count)]
    return [int(ts.timestamp()) for ts in index], closes


def _chart_body(freq: str, count: int, granularity: str) -> dict[str, Any]:
    """The v8 chart envelope the SDK parses for one interval leg."""
    stamps, closes = _bars(count, freq, 250.0)
    last = stamps[-1]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": _SYMBOL,
                        "exchangeName": "NMS",
                        "fullExchangeName": "NasdaqGS",
                        "exchangeTimezoneName": "America/New_York",
                        "exchangeTimezoneAbbreviation": "EST",
                        "instrumentType": "EQUITY",
                        "regularMarketPrice": closes[-1],
                        "regularMarketVolume": 51000000,
                        "regularMarketTime": last,
                        "marketCap": 3700000000000,
                        "fiftyTwoWeekHigh": max(closes),
                        "fiftyTwoWeekLow": min(closes),
                        "previousClose": closes[-2],
                        "chartPreviousClose": closes[-2],
                        "firstTradeDate": stamps[0] - 86400 * 3650,
                        "dataGranularity": granularity,
                        "validRanges": [
                            "1d",
                            "5d",
                            "1mo",
                            "3mo",
                            "6mo",
                            "1y",
                            "2y",
                            "5y",
                            "10y",
                            "max",
                        ],
                        "validIntervals": ["1m", "5m", "60m", "1h", "1d"],
                        "currentTradingPeriod": {
                            "regular": {"start": last - 21600, "end": last},
                            "pre": {"start": last - 36000, "end": last - 21600},
                            "post": {"start": last, "end": last + 14400},
                        },
                        "tradingPeriods": {"pre": [], "post": []},
                    },
                    "timestamp": stamps,
                    "events": {},
                    "indicators": {
                        "quote": [
                            {
                                "open": [round(c - 0.5, 2) for c in closes],
                                "high": [round(c + 1.0, 2) for c in closes],
                                "low": [round(c - 1.0, 2) for c in closes],
                                "close": closes,
                                "volume": [50000000 + i for i in range(len(closes))],
                            }
                        ],
                        "adjclose": [{"adjclose": closes}],
                    },
                }
            ],
            "error": None,
        }
    }


def _equity_quote_document() -> dict[str, Any]:
    """The envelope the FastInfo legs are answered with, keyed on effective request URLs.

    The chart needles carry the SDK's own sorted query string (the runner's
    ``governed_sdk_session`` fake matches against the request URL plus its encoded ``params``),
    which is what separates the intraday schedule leg from the daily bar leg: both hit
    ``v8/finance/chart/AAPL`` and only the parameters differ.
    """
    return {
        "responses": [
            {"url_contains": "fc.yahoo.com", "body": ""},
            {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
            {
                "url_contains": "chart/AAPL?crumb=fixture-crumb&events=div%2Csplits%2CcapitalGains"
                "&includePrePost=True&interval=1h&range=5d",
                "body": _chart_body("h", 40, "60m"),
            },
            {
                "url_contains": "chart/AAPL?crumb=fixture-crumb&events=div%2Csplits%2CcapitalGains"
                "&includePrePost=False&interval=1d&range=1y",
                "body": _chart_body("B", 300, "1d"),
            },
            {
                "url_contains": "fundamentals-timeseries",
                "body": {"timeseries": {"result": [{}], "error": None}},
            },
            {
                "url_contains": "v10/finance/quoteSummary",
                "body": {"quoteSummary": {"result": [{}], "error": None}},
            },
            {
                "url_contains": "v7/finance/quote",
                "body": {
                    "quoteResponse": {
                        "result": [
                            {
                                "symbol": _SYMBOL,
                                "exchange": "NMS",
                                "quoteType": "EQUITY",
                                "currency": "USD",
                            }
                        ],
                        "error": None,
                    }
                },
            },
        ]
    }


def _yfinance_available_indices_document() -> dict[str, Any]:
    """Two entries from the local available-index reference table."""
    return {
        "sp500": {"name": "S&P 500 Index", "ticker": "^GSPC"},
        "nasdaq100": {"name": "NASDAQ 100", "ticker": "^NDX"},
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.yfinance.provider",
    "allowed_hosts": ["fc.yahoo.com", "query1.finance.yahoo.com", "query2.finance.yahoo.com"],
    "credential_env": {},
    "models": {
        "AvailableIndices": {
            "fixture_document": _yfinance_available_indices_document,
            "query": {},
            "tamper": {"op": "drop", "path": ["sp500", "ticker"], "column": "symbol"},
            "refusal_class": "ProviderEngineError",
            "refusal_code": "YFINANCE_SHAPE_INVALID",
            "tamper_reason": (
                "a reference entry without its 'ticker' leaves the required 'symbol' field "
                "unresolvable, so the directory must refuse with YFINANCE_SHAPE_INVALID rather "
                "than publish an index the caller cannot query"
            ),
            "source_files": [
                "opendata/data/providers/yfinance/models/available_indices.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/yfinance_available_indices.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquityQuote": {
            "fixture_document": _equity_quote_document,
            "client_seam": _SEAM,
            "query": {"symbol": _SYMBOL},
            "tamper": {
                "op": "set",
                "path": ["responses", 3, "body", "chart", "result"],
                "value": [],
            },
            "refusal_class": "YfinanceProviderError",
            "refusal_code": "YFINANCE_REQUEST_FAILED",
            "tamper_reason": (
                "an empty chart result keeps the envelope well-formed at every layer the SDK "
                "reads before prices -- the crumb, the URL, the meta block it stops parsing at "
                "the missing first result -- and only the daily bar series the FastInfo quote "
                "snapshot is computed from disappears: last_price and every averaged field of "
                "YfinanceEquityQuote derive from that series, and YfinanceEquityQuoteFetcher "
                "must refuse the snapshot through require_fast_info's stable code rather than "
                "publish a payload whose prices the source never returned"
            ),
            "source_files": [
                "opendata/data/providers/yfinance/models/market_iter2.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
