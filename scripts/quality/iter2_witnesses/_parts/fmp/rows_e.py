"""Wave 9 shard: the seven FMP rows that publish a frozen row only through ``record_model``.

The plane-1 witness instrument reads ``fetcher.row_model``
(``scripts/quality/iter2_claims_probe_runner.py:400``), and seven fmp fetchers declared only the
sibling name ``record_model``. Every one of them already builds its rows out of that same class
(``_base_contract(raw, params, CryptoSearch, values)``, ``self.record_model.model_validate(item)``),
and their witnessed siblings in the very same modules carry both names -- e.g.
``PriceTargetFetcher`` (``iter2_disclosures.py:687-688``) and ``IndexHistoricalFetcher``
(``iter2_market.py:848-849``) -- so this shard rides on seven one-line declarations added to the
provider package this round:

    CryptoHistorical      -> CryptoHistorical            iter2_market.py:790
    CurrencyHistorical    -> CurrencyHistorical          iter2_market.py:809
    CryptoSearch          -> CryptoSearch                iter2_market.py:882
    CurrencyPairs         -> CurrencyPair                iter2_market.py:935
    CurrencySnapshots     -> CurrencySnapshot            iter2_market.py:1052
    DiscoveryFilings      -> FmpDiscoveryFiling          iter2_disclosures.py:489
    EarningsCallTranscript -> FmpEarningsCallTranscript  iter2_disclosures.py:641

The declarations were verified against the shipped classes rather than assumed: with them in place,
``row_model is record_model`` for all seven, and the witness' own ``type(row) is row_model`` check
(``iter2_claims_probe_runner.py:271``) is what proves the produced rows really are that class.
``EquityHistorical`` is deliberately absent: ``EquityHistoricalFetcher``
(``opendata/data/providers/fmp/models/equity_historical.py:45``) is typed
``Fetcher[EquityHistoricalQuery, list[dict[str, Any]]]`` and declares neither name, so it publishes
no frozen contract row at all. That is a product gap, recorded as one, not a witness gap.

Two seams, exactly as in the sibling shards. Five entries ride the engine seam -- a
``_MarketFetcher`` subclass with ``http_transport`` pinned to a fixed response, so each
``fixture_document`` is the plain JSON array the route publishes. Three ride the governed-client
seam. ``CurrencySnapshotsFetcher.request_specs`` issues one ``quote`` request per counter currency
and the engine seam repeats a single body for every call, which would answer both counters with the
same row. ``_DisclosureFetcher`` has no transport to thread at all: ``_read_pages`` calls
``fetch_array(endpoint, request, timeout)`` with three arguments, so ``DiscoveryFilings`` and
``EarningsCallTranscript`` fall through to ``get_shared_http_client`` and their fixtures are
``{"responses": [...]}`` envelopes.

The refusal faces are measured, not copied. The market guards (``_required_text``, ``_number``,
``_integer``, ``_inverse``) and ``_base_contract`` raise the bare typed code and keep the offending
value out of the error, so those counterfacts declare ``FMPProviderError`` plus the code with empty
``rejected``/``missing``/``invalid`` faces -- which is the equality the runner judges -- and the
tamper names the broken cell in ``tamper_reason``. The two disclosure fetchers funnel the pydantic
error into a bare ``FMP_BAD_SHAPE`` (``iter2_disclosures.py:389-391``), and
``EarningsCallTranscriptFetcher.transform_data`` compares the parsed row against the request
(``iter2_disclosures.py:669-680``), so both also carry code-only faces.
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"
_SOURCE_MARKET = ["opendata/data/providers/fmp/models/iter2_market.py", _FMP_SOURCE]
_CONTRACTS_MARKET = ["opendata/data/models/fmp_market.py", "opendata/data/domains.yaml"]
_SOURCE_DISCLOSURE = ["opendata/data/providers/fmp/models/iter2_disclosures.py", _FMP_SOURCE]
_CONTRACTS_DISCLOSURE = ["opendata/data/models/fmp_disclosures.py", "opendata/data/domains.yaml"]

_SEAM: Final = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.fmp.models._client"],
    "attr": "get_shared_http_client",
}


def _envelope(route: str, body: list[dict[str, Any]]) -> dict[str, Any]:
    """Wrap one route's array body for the governed-client seam.

    ``_read_pages`` calls ``fetch_array(endpoint, request, timeout)`` without a transport, so a
    disclosure fetcher always falls back to ``get_shared_http_client`` and only the client seam can
    answer it (``iter2_disclosures.py:950-954``).
    """
    return {"responses": [{"url_contains": f"/stable/{route}", "status": 200, "body": body}]}


def _bar_row(symbol: str, day: str, close: float) -> dict[str, Any]:
    """One daily EOD bar as ``historical-price-eod/full`` publishes it."""
    open_ = round(close * 0.9946, 4)
    return {
        "symbol": symbol,
        "date": day,
        "open": open_,
        "high": round(close * 1.0071, 4),
        "low": round(open_ * 0.9881, 4),
        "close": close,
        "volume": 38_412_900,
        "vwap": round((open_ + close) / 2, 4),
        "change": round(close - open_, 4),
        "changePercent": round((close / open_ - 1) * 100, 4),
    }


def _crypto_historical_document() -> list[dict[str, Any]]:
    """Two crypto bars on consecutive days inside the requested window.

    ``CryptoHistorical`` keys on ``date`` alone, so the route answers one symbol per request and
    the row's own ``symbol`` cell is not part of the frozen row.
    """
    return [
        _bar_row("BTCUSD", "2026-01-15", 96_412.30),
        _bar_row("BTCUSD", "2026-01-16", 98_055.71),
    ]


def _currency_historical_document() -> list[dict[str, Any]]:
    """Two forex bars carrying the compact six-letter pair the route was asked for."""
    return [_bar_row("EURUSD", "2026-01-15", 1.0846), _bar_row("EURUSD", "2026-01-16", 1.0902)]


def _crypto_directory_row(
    symbol: str, name: str, ico_date: str, circulating: int, total: int
) -> dict[str, Any]:
    """One cryptocurrency-directory record; supply cells are JSON numbers here."""
    return {
        "symbol": symbol,
        "name": name,
        "exchange": "CRYPTOCURRENCY",
        "icoDate": ico_date,
        "circulatingSupply": circulating,
        "totalSupply": total,
    }


def _crypto_search_document() -> list[dict[str, Any]]:
    return [
        _crypto_directory_row("BTCUSD", "Bitcoin", "2009-01-03", 19_720_000, 21_000_000),
        _crypto_directory_row("ETHUSD", "Ethereum", "2015-07-30", 120_300_000, 120_300_000),
    ]


def _currency_pair_row(
    symbol: str, from_currency: str, to_currency: str, from_name: str, to_name: str
) -> dict[str, Any]:
    """One forex-directory record; the route spells its currency cells in camelCase."""
    return {
        "symbol": symbol,
        "fromCurrency": from_currency,
        "toCurrency": to_currency,
        "fromName": from_name,
        "toName": to_name,
    }


def _currency_pairs_document() -> list[dict[str, Any]]:
    return [
        _currency_pair_row("EURUSD", "EUR", "USD", "Euro", "US Dollar"),
        _currency_pair_row("GBPUSD", "GBP", "USD", "Pound Sterling", "US Dollar"),
    ]


def _fx_quote_row(symbol: str, price: float, previous: float) -> list[dict[str, Any]]:
    """One ``quote`` response for a single counter+base symbol."""
    return [
        {
            "symbol": symbol,
            "price": price,
            "open": round(previous, 6),
            "dayHigh": round(max(price, previous) * 1.0018, 6),
            "dayLow": round(min(price, previous) * 0.9982, 6),
            "previousClose": previous,
            "change": round(price - previous, 6),
            "changePercentage": round((price / previous - 1) * 100, 4),
            "priceAvg50": round(previous * 0.9987, 6),
            "priceAvg200": round(previous * 1.0043, 6),
            "yearHigh": round(price * 1.1294, 6),
            "yearLow": round(price * 0.8847, 6),
            "volume": 0,
            "timestamp": 1_768_736_400,
        }
    ]


def _currency_snapshots_document() -> dict[str, Any]:
    """Two counter-currency quotes under one route needle, served in request order.

    ``CurrencySnapshotsFetcher.request_specs`` issues ``quote?symbol=<counter><base>`` per counter,
    and every request matches the same ``url_contains``, so the fake client walks this sequence.
    """
    return {
        "responses": [
            {
                "url_contains": "/stable/quote",
                "sequence": [
                    _fx_quote_row("EURUSD", 1.0846, 1.0841),
                    _fx_quote_row("GBPUSD", 1.2639, 1.2612),
                ],
                "status": 200,
            }
        ]
    }


def _discovery_filing_row(
    symbol: str, cik: str, form_type: str, filing_date: str, accepted: str, financials: bool
) -> dict[str, Any]:
    """One market-wide discovery record, stamped with the two source datetimes."""
    return {
        "symbol": symbol,
        "cik": cik,
        "formType": form_type,
        "filingDate": filing_date,
        "acceptedDate": accepted,
        "link": f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{form_type}.htm",
        "finalLink": (
            f"https://www.sec.gov/Archives/edgar/data/{cik.lstrip('0')}/{form_type}-index.htm"
        ),
        "hasFinancials": financials,
    }


def _discovery_filings_document() -> dict[str, Any]:
    return _envelope(
        "sec-filings-financials",
        [
            _discovery_filing_row(
                "NVDA", "0001367529", "10-K", "2026-02-25 18:02:11", "2026-02-25 18:04:37", True
            ),
            _discovery_filing_row(
                "JPM", "0000006325", "8-K", "2026-03-10 07:15:02", "2026-03-10 07:16:44", False
            ),
        ],
    )


def _earnings_call_transcript_document() -> dict[str, Any]:
    """One quarterly transcript; the route answers with a single record for the request."""
    return _envelope(
        "earning-call-transcript",
        [
            {
                "symbol": "AAPL",
                "period": "Q1",
                "year": 2026,
                "date": "2026-01-29",
                "content": (
                    "Operator: Good day, and welcome to the Apple Inc. First Quarter 2026 Earnings "
                    "Call Conference. Our first question comes from the line of ... Thank you. "
                    "Welcome everyone to our first quarter fiscal 2026 call. "
                    "Today I will report on our results for a quarter that included the launch of "
                    "our latest product lineup, and we report revenue of $124.3B, up "
                    "8 percent year over year."
                ),
            }
        ],
    )


def _entry(
    *,
    fixture: Any,  # noqa: ANN401  # fixture builder callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    code: str,
    reason: str,
    source_files: list[str] = _SOURCE_MARKET,
    contract_files: list[str] = _CONTRACTS_MARKET,
    client_seam: bool = False,
) -> dict[str, Any]:
    """Assemble one entry over the bare typed refusal face the shipped guards publish."""
    entry: dict[str, Any] = {
        "refusal_class": "FMPProviderError",
        "refusal_code": code,
        "fixture_document": fixture,
        "query": query,
        "tamper": tamper,
        "tamper_reason": reason,
        "source_files": source_files,
        "contract_files": contract_files,
    }
    if client_seam:
        entry["client_seam"] = _SEAM
    return entry


MODELS: Final[dict[str, Any]] = {
    "CryptoHistorical": _entry(
        fixture=_crypto_historical_document,
        query={"symbol": "BTCUSD", "start_date": "2026-01-15", "end_date": "2026-01-16"},
        tamper={"op": "drop", "path": [0, "close"], "column": "close"},
        code="FMP_BAD_FIELD",
        reason=(
            "_history_record reads close with _number(..., required=True) because a daily bar "
            "without its last price is not a bar, and CryptoHistorical inherits _DailyBar with "
            "close declared required-nullable; an adapter that published the blank would let a "
            "downstream return series invent a settlement price, so it must refuse with "
            "FMP_BAD_FIELD"
        ),
    ),
    "CurrencyHistorical": _entry(
        fixture=_currency_historical_document,
        query={"symbol": "EUR-USD", "start_date": "2026-01-15", "end_date": "2026-01-16"},
        tamper={"op": "set", "path": [0, "symbol"], "value": "USDJPY", "column": "symbol"},
        code="FMP_UNREQUESTED_SYMBOL",
        reason=(
            "CurrencyHistoricalQuery accepts a hyphenated pair but FMP's historical route only "
            "answers compact six-letter symbols, so request_specs normalizes EUR-USD to EURUSD "
            "and _history_record refuses any row naming another pair rather than relabeling a "
            "USD/JPY bar as the requested EUR/USD one; the code is FMP_UNREQUESTED_SYMBOL"
        ),
    ),
    "CryptoSearch": _entry(
        fixture=_crypto_search_document,
        query={"limit": 5},
        tamper={"op": "set", "path": [0, "symbol"], "value": "   ", "column": "symbol"},
        code="FMP_BAD_FIELD",
        reason=(
            "cryptocurrency-list names every directory entry by symbol and CryptoSearch requires "
            "that text; _required_text treats whitespace-only as absent, so an adapter must "
            "refuse with FMP_BAD_FIELD instead of shipping an addressable-looking row that no "
            "later request can resolve"
        ),
    ),
    "CurrencyPairs": _entry(
        fixture=_currency_pairs_document,
        query={"limit": 5},
        tamper={"op": "drop", "path": [0, "toCurrency"], "column": "to_currency"},
        code="FMP_BAD_FIELD",
        reason=(
            "forex-list declares each leg separately and CurrencyPair keeps from_currency and "
            "to_currency as required text with no default; a row missing its quote currency "
            "cannot be oriented, so _required_text must refuse with FMP_BAD_FIELD rather than "
            "guess the counter currency from the composite symbol"
        ),
    ),
    "CurrencySnapshots": _entry(
        fixture=_currency_snapshots_document,
        query={
            "base": "USD",
            "counter_currencies": ["EUR", "GBP"],
            "quote_type": "indirect",
        },
        tamper={
            "op": "set",
            "path": ["responses", 0, "sequence", 1, 0, "price"],
            "value": 0,
            "column": "last_rate",
        },
        code="FMP_INVALID_FX_RATE",
        reason=(
            "under quote_type=indirect the adapter turns the source quote into the base "
            "perspective by dividing, and _inverse refuses any value at or below zero as "
            "FMP_INVALID_FX_RATE: a zero price is not a rate, and 1/0 would either crash or "
            "publish an infinite last_rate for the GBP counter leg the request asked for"
        ),
        client_seam=True,
    ),
    "DiscoveryFilings": _entry(
        fixture=_discovery_filings_document,
        query={"start_date": "2026-02-01", "end_date": "2026-03-31", "limit": 50},
        tamper={
            "op": "drop",
            "path": ["responses", 0, "body", 0, "filingDate"],
            "column": "filing_date",
        },
        code="FMP_BAD_SHAPE",
        reason=(
            "sec-filings-financials pages the market-wide window by filing date and "
            "FmpDiscoveryFiling keeps filing_date as a required datetime; "
            "_DisclosureFetcher.transform_data funnels the resulting pydantic error into the bare "
            "FMP_BAD_SHAPE, because a discovery row without the date it was filed cannot be "
            "placed in the window the query bounded"
        ),
        source_files=_SOURCE_DISCLOSURE,
        contract_files=_CONTRACTS_DISCLOSURE,
        client_seam=True,
    ),
    "EarningsCallTranscript": _entry(
        fixture=_earnings_call_transcript_document,
        query={"symbol": "AAPL", "year": 2026, "quarter": 1},
        tamper={
            "op": "set",
            "path": ["responses", 0, "body", 0, "period"],
            "value": "Q3",
            "column": "period",
        },
        code="FMP_WRONG_PERIOD",
        reason=(
            "earning-call-transcript is asked for one quarter by year and quarter, and "
            "EarningsCallTranscriptFetcher.transform_data compares the parsed row against both "
            "after the base validation; a transcript stamped Q3 for a Q1 request is a different "
            "document, so the adapter refuses with FMP_WRONG_PERIOD instead of mislabeling the "
            "text it returns"
        ),
        source_files=_SOURCE_DISCLOSURE,
        contract_files=_CONTRACTS_DISCLOSURE,
        client_seam=True,
    ),
}
