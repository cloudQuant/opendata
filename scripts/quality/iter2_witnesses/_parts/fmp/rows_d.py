"""Wave 9 shard: the last eight FMP rows that publish a frozen contract row model.

The base witness module left sixteen FMP rows unwitnessed and said the reason was that the
market, ETF and disclosure adapters publish no ``row_model``. That reading is only true of eight
of the sixteen. This shard takes the eight that do --- four company calendars, the ordinary
single-symbol quote, the screener, and the two index routes --- and the eight that still do not
stay out, with their ``row_model=NONE`` recorded as a product gap.

Two offline seams appear here, and which one a row needs is decided by the shipped call site, not
by preference:

* ``_fetch_calendar_pages`` / ``_fetch_calendar_event_rows`` / ``EquityQuoteFetcher.extract_data``
  call ``fetch_array(endpoint, params, timeout)`` with no transport, so only the governed
  ``get_shared_http_client()`` can answer them. They declare a ``client_seam`` over
  ``opendata.data.providers.fmp.models._client`` and an envelope of ``{"responses": [...]}``.
  The calendars also settle the base module's pagination claim: the loop stops on an *empty*
  page, so the seam's ``sequence`` (``[rows, []]``) proves a bounded window in two requests
  instead of exhausting the page budget.
* ``_MarketFetcher.extract_data`` passes ``self.http_transport``, so the screener and both index
  routes are served by the engine transport seam exactly as the market shard already is, with a
  plain array document.

Refusal faces differ by family, and each entry declares the one its own shipped guard produces:
the calendar adapters call ``contract_shape_error``, which attributes the pydantic-refused cell
through the model's alias map, so their faces carry ``expected_invalid``/``expected_missing``
columns. The market guards (``_maybe_alias_text``, ``_flexible_source_date``, ``_required_text``)
and ``_history_record``/``EquityQuoteFetcher.transform_data`` raise bare typed codes with no
column attribution, so those entries declare only ``refusal_class``/``refusal_code`` and name the
broken cell in ``tamper_reason``.

Each entry's row class is the ``row_model`` the resolved fetcher publishes: the four calendars are
``fmp_iter2`` calendar events, ``EquityQuote`` is the canonical ``equity_price`` quote row, and
the screener and both index routes are ``fmp_market`` rows.
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"
_SOURCE_CALENDAR = ["opendata/data/providers/fmp/models/iter2.py", _FMP_SOURCE]
_CONTRACTS_CALENDAR = ["opendata/data/models/fmp_iter2.py", "opendata/data/domains.yaml"]
_SOURCE_MARKET = ["opendata/data/providers/fmp/models/iter2_market.py", _FMP_SOURCE]
_CONTRACTS_MARKET = ["opendata/data/models/fmp_market.py", "opendata/data/domains.yaml"]
_SOURCE_QUOTE = ["opendata/data/providers/fmp/models/equity_quote.py", _FMP_SOURCE]
_CONTRACTS_QUOTE = ["opendata/data/models/equity_price.py", "opendata/data/domains.yaml"]

_SEAM: Final = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.fmp.models._client"],
    "attr": "get_shared_http_client",
}
_WINDOW: Final = {"start_date": "2026-01-01", "end_date": "2026-03-31"}


def _paged(route: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Serve one calendar route as a first page and the empty tail page that ends the window."""
    return {"url_contains": f"/stable/{route}", "sequence": [rows, []], "status": 200}


def _single(route: str, body: Any) -> dict[str, Any]:  # noqa: ANN401  # fixture JSON body
    """Serve one non-paginated route."""
    return {"url_contains": f"/stable/{route}", "status": 200, "body": body}


def _dividend_row(
    symbol: str, date: str, record: str, payment: str, amount: float
) -> dict[str, Any]:
    """One dividends-calendar record, keyed as the stable route publishes it."""
    return {
        "symbol": symbol,
        "date": date,
        "recordDate": record,
        "paymentDate": payment,
        "declarationDate": "2025-12-18",
        "adjDividend": amount,
        "dividend": amount,
        "yield": round(amount / 19.75 * 100, 4),
        "frequency": "Quarterly",
    }


def _calendar_dividend_document() -> dict[str, Any]:
    """Two ex-dividend events inside the window, then the empty page that closes it."""
    return {
        "responses": [
            _paged(
                "dividends-calendar",
                [
                    _dividend_row("AAPL", "2026-01-09", "2026-01-09", "2026-02-13", 0.26),
                    _dividend_row("MSFT", "2026-02-12", "2026-02-12", "2026-03-11", 0.83),
                ],
            )
        ]
    }


def _earnings_row(symbol: str, date: str, eps: float, revenue: float) -> dict[str, Any]:
    """One earnings-calendar record with the reported-versus-estimated pair intact."""
    return {
        "symbol": symbol,
        "date": date,
        "epsActual": eps,
        "epsEstimated": round(eps * 0.97, 4),
        "revenueActual": revenue,
        "revenueEstimated": round(revenue * 1.02, 4),
        "lastUpdated": "2026-01-30",
    }


def _calendar_earnings_document() -> dict[str, Any]:
    """Two reported earnings dates for the window, then the empty tail page."""
    return {
        "responses": [
            _paged(
                "earnings-calendar",
                [
                    _earnings_row("AAPL", "2026-01-29", 1.98, 143_800_000_000),
                    _earnings_row("MSFT", "2026-01-28", 3.23, 81_300_000_000),
                ],
            )
        ]
    }


def _splits_document() -> dict[str, Any]:
    """One declared split event, then the empty page that bounds the window."""
    return {
        "responses": [
            _paged(
                "splits-calendar",
                [
                    {
                        "symbol": "NVDA",
                        "date": "2026-02-04",
                        "numerator": 10,
                        "denominator": 1,
                        "splitType": "Forward Split",
                    }
                ],
            )
        ]
    }


def _ipos_document() -> dict[str, Any]:
    """One priced IPO from the non-paginated route, which is read exactly once.

    ``IpoCalendarEvent`` declares all ten cells required-nullable under ``extra="forbid"``, so the
    row must carry every key the route publishes even where FMP leaves a deal field blank.
    """
    return {
        "symbol": "ORBIT",
        "date": "2026-03-04",
        "daa": "2026-03-04T11:00:00.000Z",
        "company": "Orbit Robotics Inc.",
        "exchange": "NASDAQ",
        "actions": "Underwriting commitment",
        "shares": 12_500_000,
        "priceRange": "18-22",
        "marketCap": 890_000_000,
        "dealSize": 250_000_000,
    }


def _calendar_events_document() -> dict[str, Any]:
    """Four official company calendars under one shared request budget.

    ``_fetch_calendar_event_rows`` reads earnings, dividends and splits paginated and ``ipos``
    once, so three routes carry a two-page sequence and the fourth a single body.
    """
    return {
        "responses": [
            _paged(
                "earnings-calendar", [_earnings_row("AAPL", "2026-01-29", 1.98, 143_800_000_000)]
            ),
            _paged(
                "dividends-calendar",
                [_dividend_row("AAPL", "2026-01-09", "2026-01-09", "2026-02-13", 0.26)],
            ),
            _single("ipos-calendar", [_ipos_document()]),
            _paged(
                "splits-calendar",
                [
                    {
                        "symbol": "NVDA",
                        "date": "2026-02-04",
                        "numerator": 10,
                        "denominator": 1,
                        "splitType": "Forward Split",
                    }
                ],
            ),
        ]
    }


def _quote_document() -> dict[str, Any]:
    """One ordinary stable quote row, which the fetcher requires to be the only row."""
    return {
        "responses": [
            _single(
                "quote",
                [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 252.11,
                        "changePercentage": 1.32,
                        "change": 3.29,
                        "volume": 51_432_900,
                        "dayLow": 247.82,
                        "dayHigh": 253.01,
                        "yearLow": 169.21,
                        "yearHigh": 260.1,
                        "marketCap": 3_765_000_000_000,
                        "priceAvg50": 244.87,
                        "priceAvg200": 231.42,
                        "exchange": "NASDAQ",
                        "open": 249.14,
                        "previousClose": 248.82,
                        "timestamp": 1_768_736_400,
                    }
                ],
            )
        ]
    }


def _screener_row(symbol: str, name: str, market_cap: int, volume: int) -> dict[str, Any]:
    """One company-screener record as the stable route publishes it."""
    return {
        "symbol": symbol,
        "companyName": name,
        "marketCap": market_cap,
        "sector": "Technology",
        "industry": "Consumer Electronics",
        "beta": 1.21,
        "price": 252.11,
        "lastAnnualDividend": 1.04,
        "volume": volume,
        "exchange": "NASDAQ Global Select Market",
        "exchangeShortName": "NASDAQ",
        "country": "US",
        "isEtf": False,
        "isFund": False,
        "isActivelyTrading": True,
    }


def _screener_document() -> list[dict[str, Any]]:
    """Two screened companies from one page of the screener route."""
    return [
        _screener_row("AAPL", "Apple Inc.", 3_765_000_000_000, 51_432_900),
        _screener_row("MSFT", "Microsoft Corporation", 3_180_000_000_000, 24_813_400),
    ]


def _constituent_row(symbol: str, name: str, added: str, founded: int | str) -> dict[str, Any]:
    """One S&P 500 membership record with the profile cells the route states."""
    return {
        "symbol": symbol,
        "name": name,
        "sector": "Information Technology",
        "subSector": "Technology Hardware, Storage and Peripherals",
        "headQuarter": "Cupertino, California",
        "dateFirstAdded": added,
        "cik": "0000320193",
        "founded": founded,
    }


def _constituents_document() -> list[dict[str, Any]]:
    """Two current index members; ``founded`` is source text the contract may keep as a label."""
    return [
        _constituent_row("AAPL", "Apple Inc.", "1982-10-01", "1976-04-01"),
        _constituent_row("MSFT", "Microsoft Corporation", "1996-12-01", "1975-04-04"),
    ]


def _bar_row(symbol: str, date: str, close: float) -> dict[str, Any]:
    """One daily EOD bar from the full-history route."""
    open_ = round(close * 0.994, 4)
    return {
        "symbol": symbol,
        "date": date,
        "open": open_,
        "high": round(close * 1.006, 4),
        "low": round(open_ * 0.991, 4),
        "close": close,
        "volume": 48_102_300,
        "vwap": round((open_ + close) / 2, 4),
        "change": round(close - open_, 4),
        "changePercent": round((close / open_ - 1) * 100, 4),
    }


def _index_historical_document() -> list[dict[str, Any]]:
    """Two index-bars for the requested symbol inside the requested window."""
    return [_bar_row("SPY", "2026-01-15", 592.04), _bar_row("SPY", "2026-01-16", 595.17)]


def _calendar_entry(
    *,
    document: Any,  # noqa: ANN401  # fixture builder callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    refusal_code: str,
    reason: str,
    missing: list[str] | None = None,
    invalid: list[str] | None = None,
) -> dict[str, Any]:
    """Assemble one calendar-row witness over the governed-client seam."""
    entry: dict[str, Any] = {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "refusal_code": refusal_code,
        "fixture_document": document,
        "query": query,
        "tamper": tamper,
        "tamper_reason": reason,
        "source_files": _SOURCE_CALENDAR,
        "contract_files": _CONTRACTS_CALENDAR,
    }
    if missing is not None:
        entry["expected_missing"] = missing
    if invalid is not None:
        entry["expected_invalid"] = invalid
    return entry


MODELS: Final[dict[str, Any]] = {
    "CalendarDividend": _calendar_entry(
        document=_calendar_dividend_document,
        query={**_WINDOW, "max_requests": 4},
        tamper={
            "op": "set",
            "path": ["responses", 0, "sequence", 0, 0, "yield"],
            "value": "1.32%",
            "column": "yield_percent",
        },
        refusal_code="FMP_BAD_SHAPE",
        invalid=["yield_percent"],
        reason=(
            "dividends-calendar states the yield as a JSON percentage-point number under the wire "
            "key 'yield', which the contract publishes as yield_percent; the percent-formatted "
            "string is not that number, so _validate_record must refuse with FMP_BAD_SHAPE and "
            "contract_shape_error must name the published column, not the wire key"
        ),
    ),
    "CalendarEarnings": _calendar_entry(
        document=_calendar_earnings_document,
        query={**_WINDOW, "max_requests": 4},
        tamper={
            "op": "drop",
            "path": ["responses", 0, "sequence", 0, 0, "symbol"],
            "column": "symbol",
        },
        refusal_code="FMP_BAD_SHAPE",
        missing=["symbol"],
        reason=(
            "the earnings calendar is an event board keyed by ticker, and symbol is the one "
            "required cell of EarningsCalendarEvent; a record without it cannot be attributed to "
            "any issuer, so the adapter must refuse with FMP_BAD_SHAPE attributing "
            "missing=('symbol',) rather than publish an ownerless report date"
        ),
    ),
    "CalendarSplits": _calendar_entry(
        document=_splits_document,
        query={**_WINDOW, "max_requests": 4},
        tamper={
            "op": "set",
            "path": ["responses", 0, "sequence", 0, 0, "numerator"],
            "value": 10.5,
            "column": "numerator",
        },
        refusal_code="FMP_BAD_SHAPE",
        invalid=["numerator"],
        reason=(
            "a split ratio is stated as an integral share count, and SplitsCalendarEvent declares "
            "numerator as a plain int; a fractional 10.5 is a ratio the contract cannot publish, "
            "pydantic will not truncate it, so the row must refuse with FMP_BAD_SHAPE attributing "
            "invalid=('numerator',)"
        ),
    ),
    "CalendarEvents": _calendar_entry(
        document=_calendar_events_document,
        query={**_WINDOW, "max_requests": 16},
        tamper={
            "op": "set",
            "path": ["responses", 3, "sequence", 0, 0, "date"],
            "value": "2025-12-31",
            "column": "date",
        },
        refusal_code="FMP_OUT_OF_WINDOW_ROW",
        reason=(
            "the aggregate route reads four official calendars under one window, and every source "
            "row is checked by _ensure_date_in_window before it is projected; a splits event dated "
            "the day before the requested start would otherwise publish an event the caller never "
            "asked for, so the fetcher refuses with FMP_OUT_OF_WINDOW_ROW and keeps the bare code "
            "because no contract column was refused"
        ),
    ),
    "EquityQuote": {
        "client_seam": _SEAM,
        "refusal_class": "FMPProviderError",
        "refusal_code": "FMP_BAD_SHAPE",
        "fixture_document": _quote_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", 0, "timestamp"],
            "value": "1768736400",
            "column": "provider_timestamp",
        },
        "tamper_reason": (
            "the stable quote route stamps its row with a JSON integer epoch and EquityQuote keeps "
            "provider_timestamp as StrictInt because the adapter refuses to guess its unit; the "
            "text '1768736400' is not that integer, so transform_data funnels the validation error "
            "into the bare FMP_BAD_SHAPE without echoing the value"
        ),
        "source_files": _SOURCE_QUOTE,
        "contract_files": _CONTRACTS_QUOTE,
    },
    "EquityScreener": {
        "refusal_class": "FMPProviderError",
        "refusal_code": "FMP_AMBIGUOUS_FIELD",
        "fixture_document": _screener_document,
        "query": {"limit": 3, "max_pages": 1, "sector": "Technology"},
        "tamper": {
            "op": "set",
            "path": [0, "name"],
            "value": "Apple Computer Inc.",
            "column": "name",
        },
        "tamper_reason": (
            "company-screener names an issuer with companyName while other market routes use name, "
            "and _maybe_alias_text reads them as aliases of one cell: it may agree or be absent, "
            "never disagree. A row stating both spellings lets the adapter pick a name silently, "
            "so it refuses with the bare FMP_AMBIGUOUS_FIELD"
        ),
        "source_files": _SOURCE_MARKET,
        "contract_files": _CONTRACTS_MARKET,
    },
    "IndexConstituents": {
        "refusal_class": "FMPProviderError",
        "refusal_code": "FMP_BAD_FIELD",
        "fixture_document": _constituents_document,
        "query": {"symbol": "sp500", "historical": False},
        "tamper": {
            "op": "set",
            "path": [0, "founded"],
            "value": 1976,
            "column": "founded",
        },
        "tamper_reason": (
            "the membership route states a founding period as text and _flexible_source_date keeps "
            "an unparseable label verbatim so the contract can publish what the source said; a "
            "bare JSON integer is neither an ISO date nor a label, so the guard refuses "
            "with FMP_BAD_FIELD instead of inventing April 1, 1976"
        ),
        "source_files": _SOURCE_MARKET,
        "contract_files": _CONTRACTS_MARKET,
    },
    "IndexHistorical": {
        "refusal_class": "FMPProviderError",
        "refusal_code": "FMP_UNREQUESTED_SYMBOL",
        "fixture_document": _index_historical_document,
        "query": {"symbol": "SPY", "start_date": "2026-01-15", "end_date": "2026-01-16"},
        "tamper": {
            "op": "set",
            "path": [0, "symbol"],
            "value": "IVV",
            "column": "symbol",
        },
        "tamper_reason": (
            "historical-price-eod/full answers one symbol per request and _history_record checks "
            "each bar against the requested symbol set before the window check; a bar labelled IVV "
            "would be filed under the SPY series the caller asked for, so the adapter refuses with "
            "the bare FMP_UNREQUESTED_SYMBOL"
        ),
        "source_files": _SOURCE_MARKET,
        "contract_files": _CONTRACTS_MARKET,
    },
}
