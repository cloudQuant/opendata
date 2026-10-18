"""Wave-33 TMX shard: the GraphQL family, witnessed offline.

Every row here travels the same seam: the runner patches ``get_shared_http_client`` on the client
module (``models/client.py:8`` binds it at import time, so the shipped call site resolves it in its
own globals), one ``{"data": {root: ...}}`` body answers the leg, ``method`` is POST and the root
field is the field ``_OPERATIONS`` declares for that operation -- the operation's own name for
``getCompanyPriceHistory``, ``getEnhancedEarningsForDate`` and ``getCompanyInsidersActivities``, and
the aliases ``news`` and ``stockList`` where TMX publishes under them (``models/client.py:40-171``).

This shard first landed one row, ``EtfHistorical``, because four of the five were refused before the
seam was touched: ``client.graphql`` resolves the operation name in ``_OPERATIONS`` and raises
``TmxProviderError("TMX_OPERATION_NOT_ALLOWED")`` at the allow-list gate, and the operations that
serve ``CalendarEarnings``, ``CompanyNews``, ``EquityGainers`` and ``InsiderTrading``
(``getEnhancedEarningsForDate``, ``getNewsAndEvents``, ``getStockListSymbolsWithQuote``,
``getCompanyInsidersActivities``; OpenBB ``openbb_tmx/utils/gql.py:304``, ``:176``, ``:393``,
``:430``) were not declared.  Wave 33 declares them in ``models/client.py`` -- the allow-list exists
so an operation is declared, not so it can be bypassed -- and all five rows are witnessed here.

``EtfHistorical`` needs no new operation.  OpenBB binds the model to the price-history fetcher it
already ships -- ``openbb_tmx/__init__.py:58`` maps ``"EtfHistorical"`` to
``TmxEquityHistoricalFetcher`` -- and that fetcher posts ``getCompanyPriceHistory``, the same
operation this provider landed for ``EquityHistorical`` (``models/client.py:49-63`` holds the
selection, ``models/fetchers.py:256-270`` the request).  The eleven columns this row maps are that
selection's own names (``datetime``, ``openPrice``, ``high``, ``low``, ``closePrice``, ``volume``,
``vwap``, ``change``, ``numberOfTrade``, ``tradeValue``), the ``symbol`` is echoed from the request
as the landed history mapper does at ``models/fetchers.py:282``, and ``change_percent`` is published
null for the reason the landed mapper states at ``models/fetchers.py:292``: the scale of TMX's raw
``changePercent`` is not established by the UI selection, so nothing here invents one.  Twelve
contract columns, twelve produced fields.

Each tamper prices the mapper's own ruling, at the site its traceback ends in, and none of them is
an envelope, HTTP-status or ``list_rows`` refusal -- those stay excluded for the reasons the parent
module gives verbatim.  ``EtfHistorical`` over-delivers a bar the one-day window never asked for.
``CalendarEarnings`` gets a record that asserts the very date the mapper has to stamp, because
``getEnhancedEarningsForDate`` publishes no date column and ``report_date`` is this domain's
``time_field``.  ``CompanyNews`` loses the ``newsid`` that is the only thing the article ``url`` --
half the domain's natural key -- is built from, so the guard is what stands between the tamper and
a row keyed ``.../news/None``.  ``EquityGainers`` answers with a different ``stockListId`` than the
one requested, the echo ruling the dividend pager already makes on ``pageNumber``.
``InsiderTrading`` loses one period's ``buy`` array, which no count or shape check sees
and which without the guard would surface as a bare ``TypeError`` instead of a coded refusal.

The bodies are hand-authored fixture bodies, not live captures, on the same terms as the parent's:
they assert the selection's column names and the shapes the shipped mapper reads, not values
app-money.tmx.com published.  ``ZWB``, ``SHOP``, ``CNQ`` and ``TOP_PRICE_PERFORMER`` are identifiers
that pass ``_validate_symbol`` or the operation's own variable shape; nothing in this shard claims
TMX lists a fund, a company or a screen under them.
"""

from __future__ import annotations

from typing import Any, Final

#: The one attribute the transport resolves per request; ``models/client.py`` binds it at import
#: time, so patching it on that module is what the shipped call site reads on the next request.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.tmx.models.client"],
    "attr": "get_shared_http_client",
}

#: The single host and route every TMX leg is allowed to name (``models/client.py:14``).
_GQL_ROUTE: Final = "app-money.tmx.com/graphql"

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/tmx/models/client.py",
    "opendata/data/providers/tmx/models/fetchers.py",
    "opendata/data/providers/tmx/models/fetchers_w33_graphql.py",
    "opendata/data/providers/tmx/provider.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/tmx.py",
    "opendata/data/domains.yaml",
]

#: The fund the history fixture queries and the two days the one-day window and its tamper
#: are built from.  Dates carry no time because ``TmxEtfHistorical`` accepts the source
#: ``datetime`` cell as either an ISO date or an ISO timestamp and a date is what the landed
#: history fixture publishes.
_ETF_SYMBOL: Final = "ZWB"
_HISTORY_DAY: Final = "2026-10-09"
_PRIOR_DAY: Final = "2026-10-08"

#: The symbols the four new legs query, the one date the earnings window is built from, and the
#: stock-list this row's default category resolves to.  Each is an identifier that passes
#: ``_validate_symbol`` or the operation's own variable shape; the bodies are hand-authored (see the
#: module docstring), so none of these values claims anything TMX published for it.
_SYMBOL: Final = "SHOP"
_INSIDER_SYMBOL: Final = "CNQ"
_EARNINGS_DAY: Final = _HISTORY_DAY
_STOCK_LIST_CATEGORY: Final = "price_performer"
_STOCK_LIST_ID: Final = "TOP_PRICE_PERFORMER"


def _history_row(day: str) -> dict[str, object]:
    """One ``getCompanyPriceHistory`` record, carrying exactly the eleven selected columns."""
    return {
        "datetime": day,
        "openPrice": 24.05,
        "closePrice": 24.28,
        "high": 24.41,
        "low": 23.98,
        "volume": 184_300,
        "tradeValue": 4_468_404.0,
        "numberOfTrade": 612,
        "change": 0.23,
        "changePercent": 0.00955,
        "vwap": 24.2105,
    }


def _etf_historical_document() -> dict[str, Any]:
    """One day of fund history under a one-day window, so the reply count equals the ``limit``.

    Same pair the landed ``EquityHistorical`` entry is witnessed with: a single-day window makes
    ``limit`` one, the control therefore carries exactly one bar, and the over-delivery guard in
    ``transform_data`` only has room to bite on the tamper.
    """
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {"data": {"getCompanyPriceHistory": [_history_row(_HISTORY_DAY)]}},
            }
        ]
    }


def _earnings_row(symbol: str, company: str) -> dict[str, object]:
    """One ``getEnhancedEarningsForDate`` record, carrying exactly the seven selected columns."""
    return {
        "symbol": symbol,
        "companyName": company,
        "announceTime": "BMO",
        "estimatedEps": 1.42,
        "actualEps": 1.51,
        "epsSurprisePercent": 0.0634,
        "epsSurpriseDollar": 0.09,
    }


def _news_row(newsid: str, headline: str) -> dict[str, object]:
    """One ``getNewsForSymbol`` record, carrying exactly the five selected columns."""
    return {
        "headline": headline,
        "datetime": "2026-10-09T16:30:00+00:00",
        "source": "TMX Newsroom",
        "newsid": newsid,
        "summary": f"{headline} (continued)",
    }


def _event_row(title: str) -> dict[str, object]:
    """One ``getUpComingEventsForSymbol`` record; the operation bundles it with the news."""
    return {"title": title, "date": "2026-10-15", "status": "UPCOMING", "type": "DIVIDEND"}


def _list_item(symbol: str, name: str, rank: int) -> dict[str, object]:
    """One ``stockList.listItems`` record, carrying exactly the eight selected columns."""
    return {
        "symbol": symbol,
        "longName": name,
        "rank": rank,
        "metric": 3.42,
        "price": 41.07,
        "priceChange": 1.36,
        "percentChange": 0.0342,
        "volume": 2_310_400,
    }


def _insider_record(name: str) -> dict[str, object]:
    """One insider transaction record, carrying exactly the five selected columns."""
    return {"name": name, "trades": 3, "shares": 30_000, "sharesHeld": 400_000, "tradeValue": 1.2e6}


def _insider_activity(period: str) -> dict[str, object]:
    """One ``insiderActivities`` period: the two direction arrays the mapper flattens."""
    return {
        "periodkey": period,
        "buy": [_insider_record("A. Buyer")],
        "sell": [_insider_record("A. Seller")],
    }


def _calendar_earnings_document() -> dict[str, Any]:
    """Two companies on the one date the request asked for, under the operation's own root."""
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {
                    "data": {
                        "getEnhancedEarningsForDate": [
                            _earnings_row("SHOP", "Shopify Inc."),
                            _earnings_row("CNQ", "Canadian Natural Resources Limited"),
                        ]
                    }
                },
            }
        ]
    }


def _company_news_document() -> dict[str, Any]:
    """One page of articles plus the events section the same operation bundles in."""
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {
                    "data": {
                        "news": [
                            _news_row("2026-10488123", "Shopify beats quarterly estimates"),
                            _news_row("2026-10488098", "Shopify names new finance chief"),
                        ],
                        "events": [_event_row("Q3 2026 earnings release")],
                    }
                },
            }
        ]
    }


def _equity_gainers_document() -> dict[str, Any]:
    """The stock-list object TMX publishes, echoing the list id the request asked for."""
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {
                    "data": {
                        "stockList": {
                            "stockListId": _STOCK_LIST_ID,
                            "name": "Top Price Performers",
                            "description": "TMX top price performers",
                            "longDescription": "Ranked by daily percentage change",
                            "metricTitle": "Change (Day) %",
                            "listItems": [
                                _list_item("SHOP", "Shopify Inc.", 1),
                                _list_item("CNQ", "Canadian Natural Resources Limited", 2),
                            ],
                            "totalPriceChange": 34.51,
                            "totalPercentChange": 0.0281,
                            "createdAt": "2026-10-09T00:00:00+00:00",
                            "updatedAt": "2026-10-09T20:00:00+00:00",
                        }
                    }
                },
            }
        ]
    }


def _insider_trading_document() -> dict[str, Any]:
    """Two periods of insider activity plus the period totals the ``summary`` flag routes to."""
    return {
        "responses": [
            {
                "url_contains": _GQL_ROUTE,
                "method": "POST",
                "body": {
                    "data": {
                        "getCompanyInsidersActivities": {
                            "insiderActivities": [_insider_activity("3M"), _insider_activity("6M")],
                            "activitySummary": [
                                {
                                    "periodkey": "3M",
                                    "buyShares": 30_000,
                                    "soldShares": 30_000,
                                    "netActivity": 0,
                                    "totalShares": 60_000,
                                }
                            ],
                        }
                    }
                },
            }
        ]
    }


MODELS: Final[dict[str, Any]] = {
    "EtfHistorical": {
        "fixture_document": _etf_historical_document,
        "client_seam": _SEAM,
        "query": {
            "symbol": _ETF_SYMBOL,
            "start_date": _HISTORY_DAY,
            "end_date": _HISTORY_DAY,
        },
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", "getCompanyPriceHistory"],
            "value": [_history_row(_HISTORY_DAY), _history_row(_PRIOR_DAY)],
        },
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROWS",
        "tamper_reason": (
            "the refusal is raised by EtfHistoricalFetcher.transform_data in "
            "opendata/data/providers/tmx/models/fetchers_w33_graphql.py, at the "
            "len(source_rows) > _history_limit(params) guard -- the site this leg's traceback "
            "actually ends in, not a helper the tamper never reaches. A one-day window is "
            "published to the source as limit 1, so the added bar is a session the caller never "
            "asked for: both records carry all eleven selected columns and each of them maps, so "
            "nothing about either row is wrong and only their count disagrees with the request. "
            "date is the required column TmxEtfHistorical files each row under and the "
            "time_field the tmx_etf_historical domain declares, and the mapper must refuse the "
            "over-long reply rather than return it, because a consumer that asked for one day and "
            "received two cannot tell an over-delivering source from a window it mis-stated -- the "
            "ruling this provider already makes for EquityHistorical and for CompanyFilings"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "CalendarEarnings": {
        "fixture_document": _calendar_earnings_document,
        "client_seam": _SEAM,
        "query": {"start_date": _EARNINGS_DAY, "end_date": _EARNINGS_DAY},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", "getEnhancedEarningsForDate", 0],
            "value": {**_earnings_row("SHOP", "Shopify Inc."), "report_date": _PRIOR_DAY},
        },
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROW",
        "tamper_reason": (
            "the refusal is raised by CalendarEarningsFetcher.transform_data in "
            "opendata/data/providers/tmx/models/fetchers_w33_graphql.py, at the "
            'if "report_date" in source_row guard -- the site this leg\'s traceback actually ends '
            "in. getEnhancedEarningsForDate selects seven columns and publishes no date, so the "
            "mapper stamps report_date from the one-date request: report_date is the time_field "
            "and the second natural_key column of tmx_calendar_earnings. The tamper adds "
            "report_date to "
            "the first record, which is the source asserting a date of its own; without the guard "
            "every other cell of that record still validates and the leg returns two rows, because "
            "projection puts the requested date in each one and the source's date is silently "
            "discarded. The refusal is about which date the reply asserts, and no list_rows, "
            "envelope or count check can see it: both records are well-formed and the reply is two "
            "rows for a two-company day"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "CompanyNews": {
        "fixture_document": _company_news_document,
        "client_seam": _SEAM,
        "query": {"symbol": _SYMBOL, "page": 1, "limit": 25},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", "news", 0, "newsid"],
            "value": None,
            "column": "newsid",
        },
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROW",
        "tamper_reason": (
            "the refusal is raised by CompanyNewsFetcher.transform_data in "
            "opendata/data/providers/tmx/models/fetchers_w33_graphql.py, at the "
            "isinstance(newsid, str) guard on the article id -- the site this leg's traceback "
            "ends in. "
            "tmx_company_news files its natural_key as url + date and the selection publishes no "
            "url: the mapper forms https://money.tmx.com/quote/<SYMBOL>/news/<newsid>, the address "
            "TMX's own UI uses (OpenBB openbb_tmx/models/company_news.py:113). Setting newsid to "
            "null is the tamper that shows the guard is load-bearing rather than decorative: "
            "format would happily render .../news/None, headline, datetime, source and summary all "
            "stay valid, and the leg would return two frozen contract rows whose keys name an "
            "article that does not exist. Nothing else on the path can catch it -- list_rows sees "
            "two well-formed dicts and the reply length equals the limit the request published"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "EquityGainers": {
        "fixture_document": _equity_gainers_document,
        "client_seam": _SEAM,
        "query": {"category": _STOCK_LIST_CATEGORY},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "data", "stockList", "stockListId"],
            "value": "TOP_VOLUME",
            "column": "stockListId",
        },
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROWS",
        "tamper_reason": (
            "the refusal is raised by EquityGainersFetcher.transform_data in "
            "opendata/data/providers/tmx/models/fetchers_w33_graphql.py, at the "
            'payload.get("stockListId") != _stock_list_id(params.category) echo guard -- the '
            "site "
            "this leg's traceback ends in. getStockListSymbolsWithQuote publishes the list id it "
            "answered under, and this row asks for TOP_PRICE_PERFORMER, so re-naming the reply "
            "TOP_VOLUME is a reply for the most-active list dressed as the top-movers screen. "
            "Every "
            "cell of both listItems stays present and typed, list_rows accepts the two records, "
            "the "
            "count is unbounded for this operation and the contract has no list column, so nothing "
            "but the echo separates the wrong-screen refusal from a served row: the shipped "
            "precedent is the dividend pager refusing a pageNumber that disagrees with the page it "
            "asked for (models/fetchers.py, HistoricalDividendsFetcher.extract_data)"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
    "InsiderTrading": {
        "fixture_document": _insider_trading_document,
        "client_seam": _SEAM,
        "query": {"symbol": _INSIDER_SYMBOL},
        "tamper": {
            "op": "drop",
            "path": [
                "responses",
                0,
                "body",
                "data",
                "getCompanyInsidersActivities",
                "insiderActivities",
                0,
                "buy",
            ],
        },
        "refusal_class": "TmxProviderError",
        "refusal_code": "TMX_BAD_ROW",
        "tamper_reason": (
            "the refusal is raised by InsiderTradingFetcher.transform_data in "
            "opendata/data/providers/tmx/models/fetchers_w33_graphql.py, at the "
            "if not isinstance(transactions, list) guard inside the buy/sell flatten -- the site "
            "this leg's traceback ends in. The operation publishes each period as one object with "
            "exactly two direction arrays, and the mapper takes the direction from the array a "
            "record sat in because no cell names it, so a period whose buy array is gone is a "
            "reply "
            "that cannot be read as transactions at all. Dropping it is the tamper that reaches "
            "that guard: the second period and the activitySummary both stay intact, so the count, "
            "list_rows and the envelope check all pass, and without the guard the flatten would "
            "iterate None and the caller would get a bare TypeError carrying neither a code nor a "
            "field attribution"
        ),
        "source_files": _SOURCE_FILES,
        "contract_files": _CONTRACT_FILES,
    },
}
