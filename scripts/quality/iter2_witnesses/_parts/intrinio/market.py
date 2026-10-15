"""Wave 8 shard: offline witnesses for intrinio's eight unwitnessed market rows.

These eight rows are the hand-written market slice of the provider
(``opendata/data/providers/intrinio/models/iter2_market_clean.py``, plus the snapshot row in
``iter2_snapshots_clean.py`` that reuses that slice's fetcher base). Every leg goes through the
shared base method ``_IntrinioMarketFetcher._get_json``, which calls ``get_shared_http_client()``
as a *module global of that file* (``iter2_market_clean.py:255``) at request time, so the seam
this shard declares names ``opendata.data.providers.intrinio.models.iter2_market_clean`` -- not
``opendata.data.http_client``, which is what the financials shard names because its fetchers reach
the client through ``_engine/http_json.py:825``. No ``http_transport`` is declared here: these
adapters own their headers, their page size and their cursor loop, so a
``FixedResponseTransport`` would be read by nothing and the leg would hit the socket breaker.

Because this slice owns its transport, its refusals are its own error classes, and the shard's
provider-local change is the two faces branch B requires of them (``code`` and ``status``, added
to ``IntrinioMarketError`` and each subclass, with ``status`` left ``None`` because every refusal
below is raised *after* the governed client answered 200) plus the ``row_model`` declarations,
which this slice had never published. Every refusal observed by the counterfact phase is
``IntrinioResponseShapeError`` with code ``INTRINIO_MARKET_SHAPE_INVALID``, and every ``tamper``
breaks one cell this slice's own code or its frozen contract declares, so no per-column
``expected_*`` face is declared: following the branch-B convention the base ``intrinio`` module
already uses for its hand-written rows, an invented attribution would claim detail the shipped
error does not publish.

Pagination is declared page by page. ``_get_pages`` loops until a page publishes
``next_page: null``, and the seam consumes one ``sequence`` item per matching call and fails
loudly if the source asks for a page beyond what is declared -- so ``EquityHistorical``,
``IndexHistorical`` and ``EtfSearch`` each declare every page their cursor chain actually walks
(two here), and the two-endpoint join in ``MarketSnapshots`` declares one terminal page per
endpoint.

Envelope ordering is most-specific needle first, exactly as the sibling ``financials`` shard had
to route ``/fundamentals`` detail ahead of ``companies``: ``EquityQuote`` answers with one quote
object carrying a nested ``security``, ``EtfInfo`` with one fund profile object, and ``EtfSearch``
with an ``etfs`` array, and their needles never overlap (``/securities/AAPL/quote``,
``/etfs/SPY``, ``/etfs/search``), so no entry can steal another's call.

Precondition (``row_model``), printed on the gate interpreter with
``python -c "from opendata.data.domains import contract_model; print(contract_model(d).__module__
+ '.' + contract_model(d).__name__)"`` and pasted next to each declaration in the provider files:

    intrinio_equity_quote       -> opendata.data.models.intrinio_market_clean.IntrinioEquityQuote
    intrinio_equity_historical  -> ...intrinio_market_clean.IntrinioEquityHistorical
    intrinio_etf_historical     -> ...intrinio_market_clean.IntrinioEtfHistorical
    intrinio_index_historical   -> ...intrinio_market_clean.IntrinioIndexHistorical
    intrinio_share_statistics   -> ...intrinio_market_clean.IntrinioShareStatistics
    intrinio_etf_info           -> ...intrinio_market_clean.IntrinioEtfInfo
    intrinio_etf_search         -> ...intrinio_market_clean.IntrinioEtfSearch
    intrinio_market_snapshots   -> opendata.data.models.intrinio_snapshots_clean.IntrinioMarketSnapshot

Each is the very class the fetcher validates its rows with, so no look-alike is involved; the
declarations are the whole provider-side change. ``MarketSnapshots`` is listed in the slice's
``GAP_MODELS`` only for the *historical* downloaded-file shape (``requested_date`` is refused by
``_validate_snapshot_query``); the current Nasdaq join this row serves is a documented JSON
contract and is witnessed here.
"""

from __future__ import annotations

from typing import Any, Final

_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.intrinio.models.iter2_market_clean"],
    "attr": "get_shared_http_client",
}
_MARKET_SOURCE: Final = "opendata/data/providers/intrinio/models/iter2_market_clean.py"
_SNAPSHOT_SOURCE: Final = "opendata/data/providers/intrinio/models/iter2_snapshots_clean.py"
_CLIENT_SOURCE: Final = "opendata/data/http_client.py"
_MARKET_CONTRACT: Final = "opendata/data/models/intrinio_market_clean.py"
_SNAPSHOT_CONTRACT: Final = "opendata/data/models/intrinio_snapshots_clean.py"
_DOMAINS: Final = "opendata/data/domains.yaml"
_REFUSAL_CLASS: Final = "IntrinioResponseShapeError"
_SHAPE_CODE: Final = "INTRINIO_MARKET_SHAPE_INVALID"


def _ohlcv(date: str, *, close: float, rows: int = 1) -> dict[str, Any]:
    """One ``stock_prices[]`` record in the keys the price rows read (:397-411)."""
    return {
        "date": date,
        "open": round(close - 0.85, 2),
        "high": round(close + 1.4, 2),
        "low": round(close - 1.55, 2),
        "close": close,
        "volume": 41_230_554 + rows,
    }


def _paged(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach the cursor chain the pagination kernel reads on every page (:295-315)."""
    last = len(pages) - 1
    for index, page in enumerate(pages):
        page["next_page"] = None if index == last else f"cursor-{index + 2}"
    return pages


def _quote_payload() -> dict[str, Any]:
    """One offset-aware ``/securities/AAPL/quote`` answer with its security metadata."""
    return {
        "symbol": "AAPL",
        "source": "iex",
        "last": 189.71,
        "open": 189.05,
        "high": 190.63,
        "low": 188.74,
        "previous_close": 188.85,
        "change": 0.86,
        "change_percent": 0.4554,
        "market_volume": 41_230_554,
        "exchange_volume": 39_880_012,
        "eod_fifty_two_week_high": 226.5554,
        "eod_fifty_two_week_low": 149.6446,
        "last_time": "2026-02-17T20:59:59Z",
        "security": {
            "id": "SEC16091",
            "company_id": "COMP12795",
            "exchange": "NASDAQ",
            "exchange_mic": "XNGS",
            "name": "Apple Inc Common Stock",
            "code": "AAPL",
            "currency": "USD",
            "ticker": "AAPL",
            "composite_ticker": "XNGS:AAPL",
            "figi": "BBG000B9XRY4",
            "composite_figi": "BBG000B9XRY4",
            "share_class_figi": "BBG001S5N8V8",
            "primary_listing": True,
        },
    }


def _intrinio_equity_quote_document() -> dict[str, Any]:
    return {"responses": [{"url_contains": "/securities/AAPL/quote", "body": _quote_payload()}]}


def _intrinio_equity_historical_document() -> dict[str, Any]:
    """Two cursor-walked ``/securities/AAPL/prices`` pages: two rows, then one."""
    pages = _paged(
        [
            {"stock_prices": [_ohlcv("2024-01-02", close=185.64), _ohlcv("2024-01-03", close=183.0)]},
            {"stock_prices": [_ohlcv("2024-01-04", close=181.94, rows=2)]},
        ]
    )
    return {"responses": [{"url_contains": "/securities/AAPL/prices", "sequence": pages}]}


def _intrinio_etf_historical_document() -> dict[str, Any]:
    """One terminal ``/securities/SPY/prices`` page: the price endpoint answers an ETF the same."""
    page = _paged([{"stock_prices": [_ohlcv("2024-01-02", close=461.99)]}])[0]
    return {"responses": [{"url_contains": "/securities/SPY/prices", "body": page}]}


def _index_level(date: str, value: float) -> dict[str, Any]:
    """One ``historical_data[]`` level observation for the index endpoint (:540-557)."""
    return {"date": date, "value": value}


def _intrinio_index_historical_document() -> dict[str, Any]:
    """Two ``/historical_data/level`` pages whose cursors are each handed out only once."""
    pages = _paged(
        [
            {"historical_data": [_index_level("2026-02-17", 6_143.2)]},
            {"historical_data": [_index_level("2026-02-18", 6_188.65)]},
        ]
    )
    return {"responses": [{"url_contains": "/historical_data/level", "sequence": pages}]}


def _etf_profile() -> dict[str, Any]:
    """One ``/etfs/<ticker>`` profile in the source spellings the mapping reads (:450-471)."""
    return {
        "id": "etf_28751",
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "sponsor": "State Street Global Advisors",
        "domicile": "US",
        "description": "Tracks the S&P 500 index.",
        "inception_date": "1993-01-22",
        "fund_listing_date": "1993-01-22",
        "data_change_date": "2026-02-13",
        "is_live_listed": True,
        "exchange_mic": "XNGS",
        "isin": "US78463X1092",
        "ric": "SPY",
        "sedol": "B12LQ2",
    }


def _intrinio_etf_info_document() -> dict[str, Any]:
    return {"responses": [{"url_contains": "/etfs/SPY", "body": _etf_profile()}]}


def _etf_hit(ticker: str, *, isin: str, sedol: str, intrinio_id: str) -> dict[str, Any]:
    """One ``etfs[]`` directory record for the search mapping (:709-725)."""
    return {
        "id": intrinio_id,
        "ticker": ticker,
        "name": f"{ticker} index fund",
        "exchange_mic": "XNGS",
        "figi_ticker": f"BBG000Lqq{ticker[-1]}",
        "ric": ticker,
        "isin": isin,
        "sedol": sedol,
    }


def _intrinio_etf_search_document() -> dict[str, Any]:
    """Two ``/etfs/search`` pages under the one needle that cannot steal the profile call."""
    pages = _paged(
        [
            {"etfs": [_etf_hit("VT", isin="US9220427424", sedol="B1FZ278", intrinio_id="etf_31512")]},
            {"etfs": [_etf_hit("VTI", isin="US9220428422", sedol="B1FZ280", intrinio_id="etf_31513")]},
        ]
    )
    return {"responses": [{"url_contains": "/etfs/search", "sequence": pages}]}


def _intrinio_share_statistics_document() -> dict[str, Any]:
    """One ``shares_outstanding`` cover-page record whose ticker answers to the request."""
    body = {
        "shares_outstanding": [
            {
                "trading_symbol": "AAPL",
                "company_id": "COMP12795",
                "end_date": "2026-01-30",
                "shares_outstanding": 14_875_000_000,
                "adj_shares_outstanding": 14_875_000_000,
            }
        ]
    }
    return {"responses": [{"url_contains": "/companies/AAPL/shares_outstanding", "body": body}]}


def _directory_row() -> dict[str, Any]:
    """One verified XNAS primary listing from ``/stock_exchanges/XNAS/securities``."""
    return {
        "id": "SEC16091",
        "ticker": "AAPL",
        "name": "Apple Inc Common Stock",
        "exchange_mic": "XNAS",
        "primary_listing": True,
    }


def _realtime_row() -> dict[str, Any]:
    """One delayed-SIP quote joined to the directory by security id (:544-628)."""
    return {
        "security": {"id": "SEC16091", "ticker": "AAPL"},
        "source": "delayed_sip",
        "last_price": 191.23,
        "last_size": 100,
        "eod_close_price": 190.1,
        "open_price": 190.55,
        "high_price": 192.0,
        "low_price": 189.8,
        "market_volume": 52_301_440,
        "exchange_volume": 44_109_770,
        "bid_price": 191.2,
        "bid_size": 200,
        "ask_price": 191.25,
        "ask_size": 300,
        "last_time": "2026-02-17T15:30:00Z",
        "bid_time": "2026-02-17T15:29:59Z",
        "ask_time": "2026-02-17T15:29:59Z",
        "updated_on": "2026-02-17T15:30:01Z",
    }


def _intrinio_market_snapshots_document() -> dict[str, Any]:
    """The two-leg join: the realtime prices leg first, then the XNAS directory leg."""
    return {
        "responses": [
            {
                "url_contains": "USCOMP/prices/realtime",
                "body": _paged([{"stock_prices": [_realtime_row()]}])[0],
            },
            {
                "url_contains": "XNAS/securities",
                "body": _paged([{"securities": [_directory_row()]}])[0],
            },
        ]
    }


MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityQuote": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_equity_quote_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "last_time"],
            "value": "2026-02-17",
            "column": "last_time",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "the quote row's updated_on is a required datetime that transform_data only publishes "
            "from last_time after checking parsed_time.utcoffset() is set, so a date-only stamp "
            "the source never anchored to a zone is refused by the adapter's own offset test "
            "rather than silently stamped at midnight UTC and sold as a quote timestamp"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EquityHistorical": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_equity_historical_document,
        "query": {
            "symbol": "AAPL",
            "interval": "1d",
            "start_date": "2024-01-01",
            "end_date": "2024-01-05",
        },
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "sequence", 0, "next_page"],
            "column": "next_page",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_get_pages tests 'next_page' not in payload before it reads the cursor, because a "
            "page that omits the pointer is indistinguishable from a page that ended the series, "
            "so a first page without next_page must be refused rather than quietly publishing "
            "two of the window's OHLCV days as if they were all of them"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EtfHistorical": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_etf_historical_document,
        "query": {"symbol": "SPY", "interval": "1d"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "stock_prices"],
            "column": "stock_prices",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "the securities price endpoint documents its OHLCV array under the stock_prices "
            "pointer and _get_pages refuses any payload whose rows pointer is not a list, so an "
            "answer without that key is a shape failure, not an empty ETF history that a caller "
            "would read as 'SPY never traded'"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "IndexHistorical": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_index_historical_document,
        "query": {
            "symbol": "^SPX",
            "interval": "1d",
            "start_date": "2026-02-16",
            "end_date": "2026-02-19",
        },
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 1, "next_page"],
            "value": "cursor-2",
            "column": "next_page",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_get_pages keeps every cursor it has already followed and raises the moment a page "
            "hands one out again, because a repeating cursor is the upstream telling it to walk "
            "the same window over; the second page re-offering 'cursor-2' must therefore be "
            "refused instead of looped until the 20-page ceiling truncates the level series"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "MarketSnapshots": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_market_snapshots_document,
        "query": {},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "stock_prices", 0, "source"],
            "value": "iex",
            "column": "source",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "the snapshot leg asks the realtime endpoint for source=delayed_sip and _snapshot_row "
            "verifies each row answers with that same source before publishing it, because the "
            "row's change and prev_close are only comparable inside one feed; a row labelled 'iex' "
            "in a delayed_sip join is refused rather than merged into the XNAS snapshot"
        ),
        "source_files": [_SNAPSHOT_SOURCE, _MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_SNAPSHOT_CONTRACT, _MARKET_CONTRACT, _DOMAINS],
    },
    "ShareStatistics": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_share_statistics_document,
        "query": {
            "symbol": "AAPL",
            "start_date": "2025-02-01",
            "end_date": "2026-02-01",
        },
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "shares_outstanding", 0, "trading_symbol"],
            "column": "trading_symbol",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "transform_data reads row['trading_symbol'] before it can decide whether the cover "
            "page belongs to the requested symbol and raises when it is not a nonempty string, "
            "because a share-count record with no trading symbol cannot be matched to a ticker at "
            "all: INTRINIO_MARKET_SHAPE_INVALID, not a silently skipped page"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EtfInfo": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_etf_info_document,
        "query": {"symbol": "SPY"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body"],
            "value": [{"ticker": "SPY", "name": "SPDR S&P 500 ETF Trust"}],
            "column": "body",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_get_json passes the decoded document through _json_object with 'Intrinio response "
            "must be a JSON object', because /etfs/<ticker> documents exactly one profile object, "
            "so an answer that arrives as an array is refused instead of being read as a profile "
            "whose every field is simply missing"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EtfSearch": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_etf_search_document,
        "query": {"query": "VT", "mode": "starts_with"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "sequence", 1, "next_page"],
            "value": "",
            "column": "next_page",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "the cursor contract is 'a nonempty string or null', and _get_pages raises on a "
            "whitespace-only string rather than treating it as terminal, because an empty cursor "
            "is what a broken upstream page looks like: the search must refuse instead of ending "
            "the directory walk early"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
}
