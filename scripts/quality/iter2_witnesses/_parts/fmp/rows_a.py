"""Wave 8 shard: the eight FMP market rows the base witness module leaves unwitnessed.

Every row here is a hand-written FMP Stable market adapter in
``opendata/data/providers/fmp/models/iter2_market.py``. Extraction still goes through
``fetch_array(...)``, so the offline seam is the engine transport the base module already uses
and the refusal type is still ``FMPProviderError`` (a ``ProviderEngineError`` subclass).

The faces differ from the base module for one reason, and it is measured, not copied. The
financial, macro and disclosure adapters call ``contract_shape_error``, which attributes the
contract column pydantic rejected. The market adapters do not: their guards
(``_required_text``, ``_number``, ``_integer``, ``_strict_bool``, ``_profile_range``) and
``_base_contract`` raise the bare typed code ``FMP_BAD_FIELD`` and keep the offending value out
of the error entirely. So each counterfact below declares ``FMPProviderError``/``FMP_BAD_FIELD``
with empty ``rejected``/``missing``/``invalid`` faces -- which is exactly the equality the runner
judges -- and the tamper names the cell it broke in ``tamper_reason`` instead.

The two ``row_model`` declarations this shard needed (``EquityActive``, ``EquityGainers``) were
added inside the provider package; the contract class each resolves to was verified with:

    $ PYTHONPATH=. python -c "from opendata.data.domains import contract_model; \
        print(contract_model('<domain>').__module__ + '.' + contract_model('<domain>').__name__)"
    fmp_equity_info        -> opendata.data.models.fmp_market.EquityInfo
    fmp_equity_peers       -> opendata.data.models.fmp_market.EquityPeers
    fmp_equity_active      -> opendata.data.models.fmp_market.EquityActive
    fmp_equity_gainers     -> opendata.data.models.fmp_market.EquityGainers
    fmp_equity_losers      -> opendata.data.models.fmp_market.EquityLosers
    fmp_market_snapshots   -> opendata.data.models.fmp_market.MarketSnapshot
    fmp_share_statistics   -> opendata.data.models.fmp_market.ShareStatistics
    fmp_price_performance  -> opendata.data.models.fmp_market.PricePerformance
"""

from __future__ import annotations

from typing import Any, Final

_FMP_SOURCE = "opendata/data/providers/fmp/models/_client.py"
_SOURCE_MARKET = ["opendata/data/providers/fmp/models/iter2_market.py", _FMP_SOURCE]
_CONTRACTS_MARKET = ["opendata/data/models/fmp_market.py", "opendata/data/domains.yaml"]

#: The market guards refuse with the bare typed code; no column attribution is published, so no
#: expected face is declared. See the module docstring.
_BAD_FIELD: Final = {"refusal_class": "FMPProviderError", "refusal_code": "FMP_BAD_FIELD"}


def _quote_row(
    symbol: str,
    name: str,
    price: float,
    change: float,
    percent: float,
    volume: int,
    exchange: str,
) -> dict[str, Any]:
    """One FMP quote-summary record as the movers routes publish it."""
    return {
        "symbol": symbol,
        "name": name,
        "price": price,
        "change": change,
        "changePercentage": percent,
        "volume": volume,
        "exchange": exchange,
    }


def _fmp_equity_active_document() -> list[dict[str, Any]]:
    return [
        _quote_row("NVDA", "NVIDIA Corp.", 174.32, 6.85, 4.09, 221_480_000, "NASDAQ"),
        _quote_row("TSLA", "Tesla, Inc.", 412.55, -9.34, -2.22, 118_940_000, "NASDAQ"),
        _quote_row("PLTR", "Palantir Technologies Inc.", 96.18, 3.12, 3.35, 96_310_000, "NASDAQ"),
    ]


def _fmp_equity_gainers_document() -> list[dict[str, Any]]:
    return [
        _quote_row("ACB", "Aurora Cannabis Inc.", 3.42, 0.71, 26.29, 51_204_000, "NYSE"),
        _quote_row("IAG", "Intellicheck, Inc.", 12.87, 1.95, 17.87, 8_412_000, "NASDAQ"),
        _quote_row("VSAB", "Eson Holdings Inc.", 1.05, 0.13, 14.13, 3_905_000, "NASDAQ"),
    ]


def _fmp_equity_losers_document() -> list[dict[str, Any]]:
    return [
        _quote_row("BYND", "Beyond Meat, Inc.", 6.13, -2.28, -27.11, 12_640_000, "NASDAQ"),
        _quote_row("RIOT", "Riot Platforms, Inc.", 9.44, -1.62, -14.66, 24_118_000, "NASDAQ"),
        _quote_row("UPST", "Upstart Holdings, Inc.", 41.07, -4.86, -10.58, 9_377_000, "NYSE"),
    ]


def _peer_row(symbol: str, name: str, price: float, market_cap: int) -> dict[str, Any]:
    """One peer listing as the stock-peers route publishes it."""
    return {"symbol": symbol, "companyName": name, "price": price, "mktCap": market_cap}


def _fmp_equity_peers_document() -> list[dict[str, Any]]:
    return [
        _peer_row("MSFT", "Microsoft Corporation", 427.34, 3_180_000_000_000),
        _peer_row("GOOGL", "Alphabet Inc.", 191.86, 2_320_000_000_000),
        _peer_row("META", "Meta Platforms, Inc.", 585.12, 1_480_000_000_000),
        _peer_row("AMZN", "Amazon.com, Inc.", 218.97, 2_310_000_000_000),
    ]


def _fmp_equity_info_document() -> list[dict[str, Any]]:
    """One company profile with the fields the public EquityInfo row publishes."""
    return [
        {
            "symbol": "AAPL",
            "companyName": "Apple Inc.",
            "cik": "0000320193",
            "cusip": "037833100",
            "isin": "US0378331005",
            "exchangeFullName": "NASDAQ Global Select Market",
            "description": (
                "Apple Inc. designs, manufactures and markets smartphones, personal computers, "
                "tablets, wearables and accessories, and sells a variety of related services."
            ),
            "website": "https://www.apple.com",
            "ceo": "Timothy D. Cook",
            "sector": "Technology",
            "industry": "Consumer Electronics",
            "address": "One Apple Park Way",
            "phone": "(408) 996-1010",
            "city": "Cupertino",
            "state": "CA",
            "zip": "95014",
            "country": "US",
            "fullTimeEmployees": 161_000,
            "image": "https://companieslogo.com/img/orig/AAPL.png",
            "currency": "USD",
            "marketCap": 3_765_000_000_000,
            "price": 252.11,
            "range": "169.21-260.10",
            "averageVolume": 51_432_900,
            "beta": 1.21,
            "isEtf": False,
            "isActivelyTrading": True,
            "isAdr": False,
            "isFund": False,
        }
    ]


def _snapshot_row(symbol: str, name: str, price: float) -> dict[str, Any]:
    """One batch-exchange quote with the session statistics the snapshot row keeps."""
    change = round(price - (price / 1.0132), 4)
    return {
        "exchange": "NASDAQ",
        "symbol": symbol,
        "name": name,
        "open": round(price - change * 0.4, 4),
        "dayHigh": round(price * 1.011, 4),
        "dayLow": round(price * 0.988, 4),
        "price": price,
        "volume": 41_285_000,
        "previousClose": round(price - change, 4),
        "change": change,
        "changePercentage": 1.32,
        "priceAvg50": round(price * 0.97, 4),
        "priceAvg200": round(price * 0.93, 4),
        "yearHigh": round(price * 1.22, 4),
        "yearLow": round(price * 0.74, 4),
        "marketCap": 1_842_000_000_000,
        "timestamp": 1_768_736_400,
    }


def _fmp_market_snapshots_document() -> list[dict[str, Any]]:
    return [
        _snapshot_row("AAPL", "Apple Inc.", 252.11),
        _snapshot_row("MSFT", "Microsoft Corporation", 427.34),
        _snapshot_row("NVDA", "NVIDIA Corp.", 174.32),
    ]


def _fmp_share_statistics_document() -> list[dict[str, Any]]:
    """One shares-float record; the route publishes a plain ISO date per symbol."""
    return [
        {
            "symbol": "AAPL",
            "date": "2026-01-16",
            "freeFloat": 0.9873,
            "floatShares": 15_142_000_000,
            "outstandingShares": 15_341_000_000,
            "source": "https://www.sec.gov/Archives/edgar/data/320193/000032019326000011/",
        }
    ]


def _fmp_price_performance_document() -> list[dict[str, Any]]:
    """One stock-price-change record, keyed exactly as FMP spells its horizons."""
    return [
        {
            "symbol": "AAPL",
            "1D": 1.32,
            "5D": 2.87,
            "1M": -3.41,
            "3M": 8.19,
            "6M": 14.02,
            "ytd": 4.55,
            "1Y": 29.84,
            "3Y": 74.19,
            "5Y": 118.62,
            "10Y": 341.07,
            "max": 412.55,
        }
    ]


def _entry(
    *,
    fixture: Any,  # noqa: ANN401  # fixture builder callable
    query: dict[str, Any],
    tamper: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Assemble one market-row witness entry from the shared bare-code refusal face."""
    return {
        "fixture_document": fixture,
        "query": query,
        "tamper": tamper,
        "tamper_reason": reason,
        "source_files": _SOURCE_MARKET,
        "contract_files": _CONTRACTS_MARKET,
        **_BAD_FIELD,
    }


MODELS: Final[dict[str, Any]] = {
    "EquityActive": _entry(
        fixture=_fmp_equity_active_document,
        query={"limit": 3},
        tamper={"op": "drop", "path": [0, "price"], "column": "price"},
        reason=(
            "the active-stock route answers with a live quote summary, and the contract declares "
            "price as a required float; a row that carries no price is not a quote, so the "
            "adapter must refuse with FMP_BAD_FIELD rather than publish a blank last trade"
        ),
    ),
    "EquityPeers": _entry(
        fixture=_fmp_equity_peers_document,
        query={"symbol": "AAPL"},
        tamper={"op": "set", "path": [0, "mktCap"], "value": "1.85e9", "column": "market_cap"},
        reason=(
            "stock-peers publishes mktCap as a JSON integer in reported currency; a scientific-"
            "notation string is a different cell than the integer the contract declares, and "
            "coercing it would let the adapter invent a market cap, so it must refuse with "
            "FMP_BAD_FIELD"
        ),
    ),
    "EquityInfo": _entry(
        fixture=_fmp_equity_info_document,
        query={"symbol": ["AAPL"]},
        tamper={"op": "set", "path": [0, "isEtf"], "value": "false", "column": "is_etf"},
        reason=(
            "the profile route states isEtf as a JSON boolean and the contract requires a strict "
            "bool; the text 'false' is truthy, so an adapter that accepted it would label an "
            "equity an ETF, and it must refuse with FMP_BAD_FIELD"
        ),
    ),
    "EquityGainers": _entry(
        fixture=_fmp_equity_gainers_document,
        query={"limit": 3, "sort": "desc"},
        tamper={
            "op": "set",
            "path": [0, "changePercentage"],
            "value": "+26.29%",
            "column": "percent_change",
        },
        reason=(
            "the movers routes rank rows by changePercentage, a JSON number in percentage "
            "points; a sign-and-unit formatted string is not that number, so the adapter must "
            "refuse with FMP_BAD_FIELD instead of ordering the board on a guessed value"
        ),
    ),
    "EquityLosers": _entry(
        fixture=_fmp_equity_losers_document,
        query={"limit": 3, "sort": "desc"},
        tamper={"op": "drop", "path": [0, "exchange"], "column": "exchange"},
        reason=(
            "each loser row is published under the venue that quotes it and the contract declares "
            "exchange as required text; without it the same ticker on two venues collapses into "
            "one unaddressable row, so the adapter must refuse with FMP_BAD_FIELD"
        ),
    ),
    "MarketSnapshots": _entry(
        fixture=_fmp_market_snapshots_document,
        query={"exchange": "NASDAQ"},
        tamper={"op": "set", "path": [0, "volume"], "value": "41.3M", "column": "volume"},
        reason=(
            "batch-exchange-quote publishes volume as a JSON integer share count, and the "
            "contract keeps it integral; a magnitude-suffixed string is not that integer, so the "
            "adapter must refuse with FMP_BAD_FIELD rather than store a rounded guess"
        ),
    ),
    "ShareStatistics": _entry(
        fixture=_fmp_share_statistics_document,
        query={"symbol": ["AAPL"]},
        tamper={
            "op": "set",
            "path": [0, "floatShares"],
            "value": "15.14B",
            "column": "float_shares",
        },
        reason=(
            "shares-float states floatShares as a JSON number of shares, the cell the natural key "
            "(symbol, date) is quoted against; a magnitude-suffixed string is not a share count, "
            "so the adapter must refuse with FMP_BAD_FIELD"
        ),
    ),
    "PricePerformance": _entry(
        fixture=_fmp_price_performance_document,
        query={"symbol": ["AAPL"]},
        tamper={"op": "set", "path": [0, "1M"], "value": "-3.41%", "column": "one_month"},
        reason=(
            "stock-price-change publishes every horizon as a percentage-point JSON number and "
            "the row's one_month is exactly that cell; a formatted string is a different unit "
            "than the contract declares, so the adapter must refuse with FMP_BAD_FIELD"
        ),
    ),
}
