"""Offline witness shard for the yfinance wave-32-b rows.

Lands ``CryptoHistorical``, ``CurrencyHistorical``, ``FuturesHistorical``, ``CompanyNews`` and
``HistoricalDividends`` against the same seam the landed ``EquityQuote`` row rides
(``kind=governed_sdk_session`` on ``_sdk.governed_yfinance_session``, the one-positional
``with ... as session:`` site at ``_sdk.py:78``). Nothing here edits the runner, the parent
witness module or the product: every leg below is the installed yfinance 0.2.66 SDK talking to
the fake session, and this repo's fetcher/normalizer running on the SDK's own parse.

What these rows ask the SDK for, measured with ``--phase direct`` refusals against an envelope
that answers only the cookie/crumb legs (this is the shape knowledge ``EquityQuote`` did not
carry, because FastInfo walks a different set of legs):

``CryptoHistorical`` / ``CurrencyHistorical`` / ``FuturesHistorical``
    All three are ``YfinanceHistoryFetcher`` subclasses, so one ``Ticker.history(...)`` leg pair:
    ``_sdk.require_history_frame`` builds ``{interval, prepost, actions, auto_adjust, start, end}``
    (``end`` shifted +1 day because the contract's dates are inclusive and the upstream's are
    exclusive) and the SDK first probes the schedule with
    ``/v8/finance/chart/<SYM>?crumb=..&interval=1d&range=1d`` -- that is where
    ``exchangeTimezoneName`` comes from, and a body without it is what produced the
    "possibly delisted; no timezone found" refusal -- then asks for the requested window with
    ``/v8/finance/chart/<SYM>?crumb=..&events=div%2Csplits%2CcapitalGains&includePrePost=False
    &interval=1d&period1=<epoch>&period2=<epoch>``. ``period1``/``period2`` are epochs the SDK
    derives from the query dates in the *meta* timezone, so the fixture needle stops at
    ``period1=`` and only the stable prefix is claimed. ``auto_adjust=False`` is why the bars the
    normalizer reads are the raw ones plus ``Adj Close``.

``CompanyNews``
    ``_sdk.require_news`` calls ``Ticker.get_news(count=.., tab="news")``, which is a **POST** to
    ``https://finance.yahoo.com/xhr/ncp?queryRef=latestNews&serviceKey=ncp_fin`` (the crumb is
    appended to the URL by ``YfData``, giving the doubled-``?`` URL the fixture needle carries),
    and the SDK reads the list out of ``body["data"]["tickerStream"]["stream"]``. Neither host nor
    verb matches the chart family, and the spec declares ``method: "POST"`` so the entry cannot be
    served to a GET.

``HistoricalDividends``
    ``_sdk.require_dividends_series`` reads ``Ticker.dividends``, which is
    ``PriceHistory.get_dividends(period="max")`` -> one
    ``/v8/finance/chart/KO?crumb=..&events=div%2Csplits%2CcapitalGains&includePrePost=True
    &interval=1d&period1=<99-years-back>&period2=<now>`` leg: both epochs are read off
    ``time.time()`` at call time, so again only the prefix up to ``period1=`` is claimed. The
    Series the row model needs is built by ``utils.parse_actions`` from
    ``chart.result[0].events.dividends``, whose per-event dicts must carry exactly ``date`` (epoch
    seconds) and ``amount`` -- extra keys become extra columns and ``safe_merge_dfs`` refuses the
    merge with "Expected 1 data col" (measured), which is why the fixture's events are two keys.

The bodies are hand-authored fixture bodies, not live captures: the price series is built from
the run's ``utcnow`` backwards so the SDK's own window trimming admits the bars, the query dates
are derived from the same run instant, and no claim is made that finance.yahoo.com published
these values.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Any, Final

import pandas as pd

#: One attribute, consumed as ``with attr(sdk) as session:`` at ``_sdk.py:78``.
_SEAM: Final[dict[str, Any]] = {
    "kind": "governed_sdk_session",
    "modules": ["opendata.data.providers.yfinance.models._sdk"],
    "attr": "governed_yfinance_session",
}

_CRUMB: Final[str] = "fixture-crumb"
_EVENTS: Final[str] = "events=div%2Csplits%2CcapitalGains"
_NEEDS_TZ: Final[str] = "America/New_York"

_SDK_SOURCES: Final[list[str]] = [
    "opendata/data/providers/yfinance/models/_sdk.py",
    "opendata/data/providers/yfinance/models/_session.py",
    "opendata/data/providers/yfinance/models/stock_daily.py",
]
_MARKET_ITER2: Final[str] = "opendata/data/providers/yfinance/models/market_iter2.py"
_ITER2: Final[str] = "opendata/data/providers/yfinance/models/iter2.py"
_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/yfinance_iter2.py",
    "opendata/data/domains.yaml",
]

#: The history window the query asks for, and the bar series the fixture answers it with, are
#: both read off the same run instant -- the same discipline ``EquityQuote``'s bodies use.
_TODAY = pd.Timestamp.utcnow().tz_convert("UTC").date()
_HISTORY_START: Final[str] = (_TODAY - timedelta(days=20)).isoformat()
_HISTORY_END: Final[str] = _TODAY.isoformat()


def _bars(count: int, base: float) -> tuple[list[int], list[float]]:
    """One deterministic synthetic daily series ending at the run's current time."""
    index = pd.date_range(end=pd.Timestamp.utcnow().tz_convert("UTC"), periods=count, freq="D")
    closes = [
        round(base * (1.0 + 0.02 * math.sin(i / 9.0)) + base * 0.0005 * i, 4) for i in range(count)
    ]
    return [int(ts.timestamp()) for ts in index], closes


def _chart(
    symbol: str,
    instrument_type: str,
    count: int,
    base: float,
    *,
    events: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The v8 chart envelope the SDK parses for one daily-interval leg."""
    stamps, closes = _bars(count, base)
    last = stamps[-1]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "symbol": symbol,
                        "exchangeName": "NMS",
                        "fullExchangeName": "NasdaqGS",
                        "exchangeTimezoneName": _NEEDS_TZ,
                        "exchangeTimezoneAbbreviation": "EST",
                        "instrumentType": instrument_type,
                        "regularMarketPrice": closes[-1],
                        "regularMarketVolume": 51000000,
                        "regularMarketTime": last,
                        "marketCap": 3700000000000,
                        "fiftyTwoWeekHigh": max(closes),
                        "fiftyTwoWeekLow": min(closes),
                        "previousClose": closes[-2],
                        "chartPreviousClose": closes[-2],
                        "firstTradeDate": stamps[0] - 86400 * 3650,
                        "dataGranularity": "1d",
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
                    "events": events if events is not None else {},
                    "indicators": {
                        "quote": [
                            {
                                "open": [round(c * 0.995, 4) for c in closes],
                                "high": [round(c * 1.01, 4) for c in closes],
                                "low": [round(c * 0.99, 4) for c in closes],
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


def _bootstrap() -> list[dict[str, Any]]:
    """The two crumb/cookie legs ``YfData`` runs before any crumbed chart or news leg."""
    return [
        {"url_contains": "fc.yahoo.com", "body": ""},
        {"url_contains": "v1/test/getcrumb", "body": _CRUMB},
    ]


def _history_entries(symbol: str, instrument_type: str, base: float) -> list[dict[str, Any]]:
    """The schedule-probe leg plus the requested-window leg, discriminated only by parameters."""
    return [
        {
            "url_contains": f"chart/{symbol}?crumb={_CRUMB}&interval=1d&range=1d",
            "body": _chart(symbol, instrument_type, 4, base),
        },
        {
            "url_contains": (
                f"chart/{symbol}?crumb={_CRUMB}&{_EVENTS}&includePrePost=False&interval=1d&period1="
            ),
            "body": _chart(symbol, instrument_type, 40, base),
        },
    ]


def _crypto_historical_document() -> dict[str, Any]:
    return {"responses": [*_bootstrap(), *_history_entries("BTC-USD", "CRYPTOCURRENCY", 64000.0)]}


def _currency_historical_document() -> dict[str, Any]:
    return {"responses": [*_bootstrap(), *_history_entries("EURUSD=X", "CURRENCY", 1.16)]}


def _futures_historical_document() -> dict[str, Any]:
    return {"responses": [*_bootstrap(), *_history_entries("ES=F", "FUTURE", 5800.0)]}


def _news_items() -> list[dict[str, Any]]:
    """Three ncp stream articles, each with the publish time the contract row is keyed on."""
    now = int(pd.Timestamp.utcnow().timestamp())
    return [
        {
            "id": f"fixture-news-{ordinal}",
            "contentType": {"originatingWebUrl": "https://finance.yahoo.com", "version": "v1"},
            "content": {
                "title": f"Fixture headline {ordinal}",
                "summary": "Hand-authored offline fixture body; never published by the source.",
                "providerPublishTime": now - 3600 * ordinal,
                "provider": {"displayName": "Fixture Wire"},
                "canonicalUrl": f"https://finance.yahoo.com/news/fixture-{ordinal}",
            },
            "ad": [],
        }
        for ordinal in range(3)
    ]


def _company_news_document() -> dict[str, Any]:
    return {
        "responses": [
            *_bootstrap(),
            {
                "url_contains": "xhr/ncp?queryRef=latestNews&serviceKey=ncp_fin",
                "method": "POST",
                "body": {"data": {"tickerStream": {"stream": _news_items()}}},
            },
        ]
    }


def _dividend_events(stamps: list[int]) -> dict[str, Any]:
    """Two ex-dividend records in the wire form ``utils.parse_actions`` merges.

    Exactly ``date`` (epoch seconds) and ``amount``: a third per-event key becomes a third column
    and the SDK's own ``safe_merge_dfs`` aborts the merge with "Expected 1 data col", which is the
    measured reason the first draft of this row refused.
    """
    return {
        "dividends": {
            str(stamps[-4]): {"date": stamps[-4], "amount": 0.46},
            str(stamps[-9]): {"date": stamps[-9], "amount": 0.45},
        }
    }


def _historical_dividends_document() -> dict[str, Any]:
    stamps, _ = _bars(20, 60.0)
    return {
        "responses": [
            *_bootstrap(),
            {
                "url_contains": f"chart/KO?crumb={_CRUMB}&interval=1d&range=1d",
                "body": _chart("KO", "EQUITY", 4, 60.0),
            },
            {
                "url_contains": (
                    f"chart/KO?crumb={_CRUMB}&{_EVENTS}&includePrePost=True&interval=1d&period1="
                ),
                "body": _chart("KO", "EQUITY", 20, 60.0, events=_dividend_events(stamps)),
            },
        ]
    }


_HISTORY_TAMPER_REASON = (
    "an empty chart result keeps every leg the SDK reads before the bars well-formed -- the crumb, "
    "the schedule probe that supplies exchangeTimezoneName, the URL, the parameters -- "
    "and only the daily series the window was asked for disappears: PriceHistory returns "
    "its empty frame, "
    "_history_rows sees a frame with no rows, and {fetcher} must refuse rather than publish a "
    "history window the source never returned bars for"
)

MODELS: Final[dict[str, Any]] = {
    "CryptoHistorical": {
        "fixture_document": _crypto_historical_document,
        "client_seam": _SEAM,
        "query": {
            "symbol": "BTC-USD",
            "start_date": _HISTORY_START,
            "end_date": _HISTORY_END,
            "interval": "1d",
        },
        "tamper": {"op": "set", "path": ["responses", 3, "body", "chart", "result"], "value": []},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _HISTORY_TAMPER_REASON.format(fetcher="YfinanceCryptoHistoricalFetcher"),
        "source_files": [_MARKET_ITER2, *_SDK_SOURCES],
        "contract_files": _CONTRACT_FILES,
    },
    "CurrencyHistorical": {
        "fixture_document": _currency_historical_document,
        "client_seam": _SEAM,
        "query": {
            # The public ODP pair form; the query class folds it into Yahoo's ``EURUSD=X`` before
            # the SDK ever builds a chart URL, so the fixture needle carries the source form.
            "symbol": "EURUSD",
            "start_date": _HISTORY_START,
            "end_date": _HISTORY_END,
            "interval": "1d",
        },
        "tamper": {"op": "set", "path": ["responses", 3, "body", "chart", "result"], "value": []},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _HISTORY_TAMPER_REASON.format(fetcher="YfinanceCurrencyHistoricalFetcher"),
        "source_files": [_MARKET_ITER2, *_SDK_SOURCES],
        "contract_files": _CONTRACT_FILES,
    },
    "FuturesHistorical": {
        "fixture_document": _futures_historical_document,
        "client_seam": _SEAM,
        # No ``expiration``: the query class refuses an expiry because yfinance documents ticker
        # history, not continuous-contract mapping, so the witnessed leg is the ES=F ticker series.
        "query": {
            "symbol": "ES=F",
            "start_date": _HISTORY_START,
            "end_date": _HISTORY_END,
            "interval": "1d",
        },
        "tamper": {"op": "set", "path": ["responses", 3, "body", "chart", "result"], "value": []},
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_RESPONSE",
        "tamper_reason": _HISTORY_TAMPER_REASON.format(fetcher="YfinanceFuturesHistoricalFetcher"),
        "source_files": [_MARKET_ITER2, *_SDK_SOURCES],
        "contract_files": _CONTRACT_FILES,
    },
    "CompanyNews": {
        "fixture_document": _company_news_document,
        "client_seam": _SEAM,
        "query": {"symbol": "AAPL", "limit": 3},
        "tamper": {
            "op": "drop",
            "path": [
                "responses",
                2,
                "body",
                "data",
                "tickerStream",
                "stream",
                0,
                "content",
                "providerPublishTime",
            ],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_NEWS_DATE_MISSING",
        "tamper_reason": (
            "the ncp stream still answers with a well-formed article list -- the POST URL, the "
            "crumb, the item count and every other field the SDK copies into raw_payload_json are "
            "untouched -- and only the first article's providerPublishTime is gone, so the "
            "observation the yfinance_company_news domain keys on published_at_utc has no "
            "timestamp to key on: YfinanceCompanyNewsFetcher must refuse the stream rather than "
            "publish an undated news row"
        ),
        "source_files": [_MARKET_ITER2, *_SDK_SOURCES],
        "contract_files": _CONTRACT_FILES,
    },
    "HistoricalDividends": {
        "fixture_document": _historical_dividends_document,
        "client_seam": _SEAM,
        "query": {"symbol": "KO"},
        "tamper": {
            "op": "set",
            "path": ["responses", 3, "body", "chart", "result", 0, "events", "dividends"],
            "value": {},
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EMPTY_OR_DUPLICATE_RESPONSE",
        "tamper_reason": (
            "the period=max actions leg still returns its full OHLCV series, so the SDK parses the "
            "chart, merges the (now empty) event map and hands back a price frame -- and only the "
            "Dividends column every record of this domain is built from is gone. "
            "Ticker.dividends becomes an empty Series, which is byte-for-byte what a symbol that "
            "never paid returns, so YfinanceHistoricalDividendsFetcher must refuse on "
            "YFINANCE_EMPTY_OR_DUPLICATE_RESPONSE rather than let an absent event map be read as a "
            "complete no-dividend history"
        ),
        "source_files": [_ITER2, *_SDK_SOURCES],
        "contract_files": _CONTRACT_FILES,
    },
}
