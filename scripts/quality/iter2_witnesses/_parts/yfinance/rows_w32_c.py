"""Offline witness entries for four yfinance residue rows (shard w32_c).

Rows: ``KeyExecutives``, ``ShareStatistics``, ``OptionsChains``, ``PriceTargetConsensus``.
Each rides the same ``governed_sdk_session`` seam as the landed ``EquityQuote`` entry
(:mod:`iter2_witnesses.yfinance`): the patched attribute is consumed as
``with governed_yfinance_session(sdk) as session:`` at ``_sdk.py:78``, and the fake is a real
``curl_cffi.requests.Session`` subclass, so the installed yfinance 0.2.66 SDK does its own
crumb/cookie orchestration, URL construction and parsing against it while this repo's fetcher and
normalizer run on that parse.

What these four rows add beyond ``EquityQuote``'s ``FastInfo`` legs, read off the SDK source
installed in the runtime (never off a guess):

* ``require_info`` (KeyExecutives, ShareStatistics) is ``Quote.info``, which is **three** legs --
  ``v10/finance/quoteSummary/<symbol>`` with the five-module parameter set, the complementary
  ``v7/finance/quote?symbols=...`` block that ``_fetch_info`` merges with ``result.update(...)``,
  and the ``ws/fundamentals-timeseries`` leg ``_fetch_complementary`` asks for
  ``trailingPegRatio``. ``quoteSummary`` payloads arrive **module-nested**
  (``{"assetProfile": {"companyOfficers": [...]}}``) and the SDK flattens one level before the
  fetcher ever sees ``companyOfficers``, so the fixture has to nest them the way the wire does.
* ``require_options_chain`` is a **two-request** surface: ``Ticker.option_chain(date)`` first asks
  ``v7/finance/options/<symbol>`` to learn ``_expirations`` (a ``%Y-%m-%d`` -> epoch map), then asks
  ``v7/finance/options/<symbol>?date=<epoch>`` for that expiration's ``options[0]``. The fake
  matches on the effective URL (request URL plus encoded ``params``, and the crumb always arrives
  as a parameter), so the dated needle must be declared before the list needle.
* ``require_price_targets`` is ``Analysis.analyst_price_targets``, one
  ``v10/finance/quoteSummary/<symbol>?modules=financialData`` leg whose ``target*Price`` /
  ``currentPrice`` keys the SDK renames to ``low/high/mean/median/current``.

Every fixture body is hand-authored wire shape, not a live capture, and no claim is made that
finance.yahoo.com published these values.
"""

from __future__ import annotations

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

#: ``Ticker.option_chain(date)`` refuses an expiration it did not learn from the list leg, and the
#: row validator refuses a past date, so the snapshot is pinned to a forward Monday-shaped offset
#: that both the query and the two option envelopes agree on.
_EXPIRATION: Final[str] = str(
    (pd.Timestamp.utcnow().normalize() + pd.Timedelta(days=32)).date()
).replace("-", "-")
_EXPIRATION_EPOCH: Final[int] = int(pd.Timestamp(f"{_EXPIRATION} 00:00:00", tz="UTC").timestamp())

_BOOSTER = {"fc.yahoo.com": "", "getcrumb": "v1/test/getcrumb"}


def _bootstrap_legs() -> list[dict[str, Any]]:
    """The cookie and crumb handshakes every ``YfData`` request performs before the data leg."""
    return [
        {"url_contains": "fc.yahoo.com", "body": _BOOSTER["fc.yahoo.com"]},
        {"url_contains": _BOOSTER["getcrumb"], "body": _CRUMB},
    ]


def _quote_type_block() -> dict[str, Any]:
    """The ``quoteType`` module both ``info`` rows and the officer guard read."""
    return {
        "symbol": _SYMBOL,
        "exchange": "NMS",
        "quoteType": "EQUITY",
        "currency": "USD",
        "shortName": "Apple Inc.",
        "longName": "Apple Inc.",
    }


def _officers() -> list[dict[str, Any]]:
    """Two ``assetProfile.companyOfficers`` records in the field set the row model maps."""
    return [
        {
            "maxAge": 86400,
            "name": "Timothy D. Cook",
            "age": 63,
            "title": "Chief Executive Officer, Director",
            "yearBorn": 1961,
            "fiscalYear": 2025,
            "totalPay": {"raw": 16757564, "fmt": "16.76M"},
            "exercisedValue": {"raw": 0, "fmt": "0"},
            "unexercisedValue": {"raw": 0, "fmt": "0"},
        },
        {
            "maxAge": 86400,
            "name": "Kevan Parsia",
            "age": 52,
            "title": "Senior Vice President, Chief Financial Officer, Principal Accounting Officer",
            "yearBorn": 1972,
            "fiscalYear": 2025,
            "totalPay": {"raw": 5054934, "fmt": "5.05M"},
            "exercisedValue": {"raw": 1200000, "fmt": "1.2M"},
            "unexercisedValue": {"raw": 3400000, "fmt": "3.4M"},
        },
    ]


def _info_quote_summary_body(*, with_share_fields: bool) -> dict[str, Any]:
    """One module-nested ``quoteSummary`` envelope as ``Quote._fetch_info`` flattens it.

    ``with_share_fields=False`` keeps the share statistics solely on the complementary
    ``v7/finance/quote`` leg, which is what makes that leg the one the ``ShareStatistics`` claim
    is tampered on; the officer list always arrives nested under ``assetProfile``.
    """
    asset_profile: dict[str, Any] = {
        "address": {"city": "Cupertino", "state": "CA", "country": "United States"},
        "website": "https://www.apple.com",
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "longBusinessSummary": "Apple Inc. designs, manufactures and markets smartphones.",
        "numberOfEmployees": {"raw": 164000, "fmt": "164k"},
        "companyOfficers": _officers(),
    }
    result: dict[str, Any] = {
        "assetProfile": asset_profile,
        "quoteType": _quote_type_block(),
        "defaultKeyStatistics": {
            "sharesOutstanding": {"raw": 18650000000, "fmt": "18.65B"},
            "floatShares": {"raw": 18580000000, "fmt": "18.58B"},
            "heldPercentInstitutions": {"raw": 0.6125, "fmt": "61.25%"},
            "heldPercentInsiders": {"raw": 0.0782, "fmt": "7.82%"},
            "shortRatio": {"raw": 1.4, "fmt": "1.4"},
            "shortPercentOfFloat": {"raw": 0.0071, "fmt": "0.71%"},
        },
    }
    if with_share_fields:
        result["financialData"] = {"currentPrice": 246.38, "targetHighPrice": 300.0}
    return {"quoteSummary": {"result": [result], "error": None}}


def _quote_legs() -> list[dict[str, Any]]:
    """The complementary ``v7/finance/quote`` and ``fundamentals-timeseries`` legs of ``info``."""
    return [
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
                            "regularMarketPrice": 246.38,
                            "regularMarketVolume": 51000000,
                            "sharesOutstanding": 18650000000,
                            "impliedSharesOutstanding": 18650000000,
                            "floatShares": 18580000000,
                            "sharesShort": 105000000,
                            "sharesShortPriorMonth": 102000000,
                            "sharesShortPreviousMonthDate": 1751328000,
                            "sharesPercentSharesOut": 0.0056,
                            "heldPercentInsiders": 0.0782,
                            "heldPercentInstitutions": 0.6125,
                            "shortRatio": 1.4,
                            "shortPercentOfFloat": 0.0071,
                        }
                    ],
                    "error": None,
                }
            },
        },
        {
            "url_contains": "fundamentals-timeseries",
            "body": {"timeseries": {"result": [{}], "error": None}},
        },
    ]


def _info_document() -> dict[str, Any]:
    """The envelope ``require_info`` is answered with for the officer rows."""
    return {
        "responses": [
            *_bootstrap_legs(),
            {
                "url_contains": "v10/finance/quoteSummary",
                "body": _info_quote_summary_body(with_share_fields=True),
            },
            *_quote_legs(),
        ]
    }


def _share_statistics_document() -> dict[str, Any]:
    """The envelope of the share-statistics row, with every share field on one leg.

    ``Quote._fetch_info`` merges the module-nested ``quoteSummary`` block and the flat
    ``quoteResponse`` block into one mapping; ``YfinanceShareStatisticsFetcher._fetch_one`` then
    keeps only ``SHARE_FIELDS``. Carrying those fields solely on the complementary
    ``v7/finance/quote`` leg is what makes that leg the measured failure point of this claim: an
    empty ``quoteResponse`` leaves a well-populated company profile and no share statistics at all.
    """
    shares_result = _info_quote_summary_body(with_share_fields=True)["quoteSummary"]["result"][0]
    profile_block = shares_result["assetProfile"]
    return {
        "responses": [
            *_bootstrap_legs(),
            {
                "url_contains": "v10/finance/quoteSummary",
                "body": {
                    "quoteSummary": {
                        "result": [
                            {
                                "assetProfile": profile_block,
                                "quoteType": _quote_type_block(),
                            }
                        ],
                        "error": None,
                    }
                },
            },
            *_quote_legs(),
        ]
    }


def _option_record(salt: int, *, put: bool) -> dict[str, Any]:
    """One option contract in the ``optionChain`` wire shape the SDK reindexes into a frame."""
    strike = 245.0 + salt * 2.5
    return {
        "contractSymbol": f"{_SYMBOL}2612{18 + salt:02d}{'P' if put else 'C'}{int(strike * 1000)}",
        "lastTradeDate": _EXPIRATION_EPOCH - 86400 * (salt + 1),
        "strike": strike,
        "lastPrice": round(9.5 - salt * 0.75 + (1.25 if put else 0.0), 2),
        "bid": round(8.5 - salt * 0.75, 2),
        "ask": round(10.5 - salt * 0.75, 2),
        "change": 0.25,
        "percentChange": 2.5,
        "volume": 120 + salt,
        "openInterest": 5400 + salt * 17,
        "impliedVolatility": 0.27 + salt * 0.01,
        "inTheMoney": strike > 246.38 if put else strike < 246.38,
        "contractSize": "regular",
        "currency": "USD",
    }


def _option_chain_legs() -> list[dict[str, Any]]:
    """The two ``v7/finance/options`` legs: the expiration list first, then the dated snapshot.

    The dated needle is declared ahead of the list needle because the list needle is a substring
    of the dated request's effective URL, and the fake answers the first matching spec.
    """
    quote = {
        "language": "en_US",
        "region": "US",
        "quoteType": "EQUITY",
        "type": "equity",
        "symbol": _SYMBOL,
        "underlyingSymbol": _SYMBOL,
        "exchange": "NMS",
        "currency": "USD",
        "regularMarketPrice": 246.38,
        "regularMarketTime": _EXPIRATION_EPOCH - 86400 * 30,
    }
    options = {
        "expirationDate": _EXPIRATION_EPOCH,
        "strangle": 5000,
        "size": 100,
        "strikes": [245.0 + index * 2.5 for index in range(6)],
        "calls": [_option_record(index, put=False) for index in range(4)],
        "puts": [_option_record(index, put=True) for index in range(4)],
    }
    return [
        {
            "url_contains": f"v7/finance/options/{_SYMBOL}?date={_EXPIRATION_EPOCH}",
            "body": {
                "optionChain": {
                    "result": [
                        {
                            "expirationDates": [_EXPIRATION_EPOCH],
                            "quote": quote,
                            "options": [options],
                        }
                    ],
                    "error": None,
                }
            },
        },
        {
            "url_contains": "v7/finance/options/AAPL",
            "body": {
                "optionChain": {
                    "result": [{"expirationDates": [_EXPIRATION_EPOCH], "quote": quote}],
                    "error": None,
                }
            },
        },
    ]


def _options_document() -> dict[str, Any]:
    return {"responses": [*_bootstrap_legs(), *_option_chain_legs()]}


def _price_target_document() -> dict[str, Any]:
    """The single ``modules=financialData`` leg ``Analysis.analyst_price_targets`` renames."""
    return {
        "responses": [
            *_bootstrap_legs(),
            {
                "url_contains": (
                    f"v10/finance/quoteSummary/{_SYMBOL}?corsDomain=finance.yahoo.com"
                    f"&crumb={_CRUMB}&formatted=false&modules=financialData&symbol={_SYMBOL}"
                ),
                "body": {
                    "quoteSummary": {
                        "result": [
                            {
                                "financialData": {
                                    "currentPrice": 246.38,
                                    "targetLowPrice": 190.0,
                                    "targetHighPrice": 305.0,
                                    "targetMeanPrice": 258.4,
                                    "targetMedianPrice": 255.0,
                                    "recommendationMean": 1.8,
                                    "numberOfAnalystOpinions": 41,
                                }
                            }
                        ],
                        "error": None,
                    }
                },
            },
        ]
    }


MODELS: Final[dict[str, Any]] = {
    "KeyExecutives": {
        "fixture_document": _info_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": {
            "op": "drop",
            "path": [
                "responses",
                2,
                "body",
                "quoteSummary",
                "result",
                0,
                "assetProfile",
                "companyOfficers",
                0,
                "title",
            ],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_EXECUTIVES_REQUIRED_FIELD_MISSING",
        "tamper_reason": (
            "deleting one officer's title leaves the envelope well-formed everywhere else the "
            "product reads it -- the crumb, the quoteSummary module nesting the SDK flattens, the "
            "returned-symbol check, the list-of-mappings shape check all still pass -- and only "
            "the identity field YfinanceKeyExecutiveObservation maps through "
            "_executive_required_text disappears: the fetcher must refuse rather than publish a "
            "nameless executive drawn from the source's own officer list"
        ),
        "source_files": [
            "opendata/data/providers/yfinance/models/market_iter2.py",
            *_SOURCE_FILES,
        ],
        "contract_files": _CONTRACT_FILES,
    },
    "ShareStatistics": {
        "fixture_document": _share_statistics_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": {
            "op": "set",
            "path": ["responses", 3, "body", "quoteResponse", "result"],
            "value": [],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_SHARE_STATISTICS_MISSING",
        "tamper_reason": (
            "an empty quoteResponse result keeps the company profile the other leg publishes -- "
            "the SDK still flattens assetProfile and quoteType, require_info still gets a "
            "non-empty mapping and the symbol still matches -- while every field of "
            "ShareStatisticsFetcher.SHARE_FIELDS lives on that one complementary leg, so the "
            "share-count and short-interest snapshot the row exists to publish is gone and "
            "_fetch_one must refuse through its own stable code instead of emitting an "
            "observation whose payload carries no share statistics"
        ),
        "source_files": [
            "opendata/data/providers/yfinance/models/market_iter2.py",
            *_SOURCE_FILES,
        ],
        "contract_files": _CONTRACT_FILES,
    },
    "OptionsChains": {
        "fixture_document": _options_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL, "expiration_date": _EXPIRATION},
        "tamper": {
            "op": "set",
            "path": ["responses", 2, "body", "optionChain", "result", 0, "options"],
            "value": [],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_OPTIONS_SHAPE_INVALID",
        "tamper_reason": (
            "emptying the dated snapshot's options array leaves the expiration list leg, the "
            "crumb, the requested date's epoch and the underlying quote block intact, so the SDK "
            "still finds the expiration and returns a namedtuple -- but with calls and puts as "
            "None rather than frames: require_options_chain's DataFrame guard and "
            "YfinanceOptionsChainsFetcher.transform_data's contractSymbol/strike column guard are "
            "both downstream of that leg, and the row must refuse rather than publish an option "
            "chain the source never returned for the expiration it was asked about"
        ),
        "source_files": [
            "opendata/data/providers/yfinance/models/market_iter2.py",
            *_SOURCE_FILES,
        ],
        "contract_files": _CONTRACT_FILES,
    },
    "PriceTargetConsensus": {
        "fixture_document": _price_target_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL},
        "tamper": {
            "op": "drop",
            "path": [
                "responses",
                2,
                "body",
                "quoteSummary",
                "result",
                0,
                "financialData",
                "targetMedianPrice",
            ],
        },
        "refusal_class": "YfinanceProviderError",
        "refusal_code": "YFINANCE_PRICE_TARGET_SHAPE_INVALID",
        "tamper_reason": (
            "dropping targetMedianPrice leaves the financialData module intact at every layer "
            "before it -- the crumb, the modules=financialData request the SDK builds, the "
            "result[0]['financialData'] the Analysis scraper indexes, and the four surviving "
            "renamed keys current/low/high/mean -- and removes exactly the field the consensus "
            "row is about: require_price_targets' required-key guard sees a summary without "
            "'median' and YfinancePriceTargetConsensus.target_median cannot be drawn from a "
            "source that stopped publishing it"
        ),
        "source_files": [
            "opendata/data/providers/yfinance/models/market_iter2.py",
            *_SOURCE_FILES,
        ],
        "contract_files": _CONTRACT_FILES,
    },
}
