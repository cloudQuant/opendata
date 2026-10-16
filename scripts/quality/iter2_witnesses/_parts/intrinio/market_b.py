"""Wave 16 shard: intrinio's seven remaining frozen-row market rows.

These rows are the rest of the hand-written market slice that ``market.py`` left out: the company
profile, the market-cap series, the ETF holdings, the ETF price-performance join, both options
routes and the Zacks consensus. Every one of them already builds its rows out of a frozen class
under ``opendata/data/models/``, and the only provider-side change this wave ships is the seven
``row_model`` declarations that let the witness instrument see that fact
(``iter2_claims_probe_runner.py:400`` reads the attribute; ``:504`` requires the class to live under
``opendata.data.models.``). Each declaration was taken from the class the fetcher's own
``transform_data`` validates with. The domain-equality preflight below re-ran
``contract_model(capability.domain)`` against all seven and every pair is the same class;
that check is self-imposed, not inherited. The shipped guards assert the equality only for
federal_reserve's own fetchers (``tests/test_federal_reserve_iter2_models.py:398-399``) and
for the 21 declarative rows collected by ``tests/test_provider_model_contracts.py:577``, and
``--collect-only`` on that test
shows none of the seven intrinio identities in its population. It still matters, because
``provider_model_query._serialize_contract_row`` revalidates every served row against
``contract_model(domain)`` -- a look-alike contract passes its direct leg and refuses on the
service leg:

    ``opendata.data.models.*`` is the package root of every class name below:

    intrinio_equity_info              -> intrinio_market_clean.IntrinioEquityInfo
    intrinio_historical_market_cap    -> intrinio_market_clean.IntrinioHistoricalMarketCap
    intrinio_etf_holdings             -> intrinio_market_clean.IntrinioEtfHolding
    intrinio_options_chains           -> intrinio_market_clean.IntrinioOptionsChain
    intrinio_options_unusual          -> intrinio_market_clean.IntrinioOptionsUnusual
    intrinio_price_target_consensus   -> intrinio_market_clean.IntrinioPriceTargetConsensus
    intrinio_etf_price_performance    -> intrinio_snapshots_clean.IntrinioEtfPricePerformance

Like its sibling shard, every leg rides the ``client_seam`` over
``opendata.data.providers.intrinio.models.iter2_market_clean``, because ``_get_json`` reads
``get_shared_http_client()`` as a module global of that file (``iter2_market_clean.py:255``) -- the
snapshots fetcher inherits the same base, so ``EtfPricePerformance`` is patched through the same
module and not through ``iter2_snapshots_clean``. No ``http_transport`` is declared anywhere here.

Each counterfact breaks a cell that the shipped code refuses with its own typed error, which is
why no entry declares an ``expected_*`` face: ``INTRINIO_MARKET_SHAPE_INVALID`` is raised bare by
``_json_object``, by the pagination kernel, by ``_percent_fraction``, by the contract-code check and
by ``_industry_group_number``, and none of those guards attributes a column, so declaring one would
claim detail the refusal does not publish. Every refusal is ``IntrinioResponseShapeError``, which
``_typed_provider_refusal`` accepts because the class is declared in the provider package and
carries ``code`` and ``status`` (``status`` stays ``None``: each refusal below happens after the
governed client answered 200).

``market.py``'s closing note that FMP's ``EquityHistorical`` "publishes no frozen contract row" and
this slice's seven rows being unwitnessed were both true of the code as wave 8/9 shipped it; the
wave-16 ``row_model`` declarations supersede those sentences, and the matching fmp row is witnessed
in ``_parts/fmp/rows_f.py``.
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


def _paged(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Close every page but the last, the way the cursor kernel reads the chain (:281-315)."""
    last = len(pages) - 1
    for index, page in enumerate(pages):
        page["next_page"] = None if index == last else f"cursor-{index + 2}"
    return pages


def _single(needle: str, body: Any) -> dict[str, Any]:  # noqa: ANN401  # one endpoint's JSON object
    """One non-paginated endpoint answer."""
    return {"url_contains": needle, "body": body}


def _company_profile() -> dict[str, Any]:
    """One ``/companies/<ticker>`` profile in the spellings the mapping filters on (:369-378)."""
    return {
        "id": "COMP12795",
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "legal_name": "Apple Inc.",
        "cik": "0000320193",
        "lei": "H6pj4983D219WV684A69",
        "stock_exchange": "XNGS",
        "sic": 3571,
        "ceo": "Timothy D. Cook",
        "company_url": "https://www.apple.com",
        "hq_address1": "One Apple Park Way",
        "hq_address_city": "Cupertino",
        "hq_state": "CA",
        "hq_country": "USA",
        "employees": 164000,
        "entity_legal_form": "Common Stock",
        "entity_status": "Active",
        "latest_filing_date": "2026-02-06",
        "sector": "Technology",
        "industry_category": "Information Technology",
        "industry_group": "Technology Hardware",
        "template": "us-gaap",
        "standardized_active": True,
        "first_fundamental_date": "1980-12-12",
        "last_fundamental_date": "2025-12-27",
        "first_stock_price_date": "1982-06-04",
        "last_stock_price_date": "2026-02-17",
    }


def _intrinio_equity_info_document() -> dict[str, Any]:
    return {"responses": [_single("/companies/AAPL", _company_profile())]}


def _market_cap_row(date: str, value: float) -> dict[str, Any]:
    """One ``historical_data[]`` market-cap observation as the endpoint publishes it."""
    return {"date": date, "value": value, "frequency": "daily", "change": 0.0}


def _intrinio_historical_market_cap_document() -> dict[str, Any]:
    """One terminal ``/historical_data/AAPL/marketcap`` page of two observations."""
    page = _paged(
        [
            {
                "historical_data": [
                    _market_cap_row("2026-02-17", 3_765_000_000_000.0),
                    _market_cap_row("2026-02-18", 3_812_450_000_000.0),
                ]
            }
        ]
    )[0]
    return {"responses": [_single("/historical_data/AAPL/marketcap", page)]}


def _holding_row(ticker: str, name: str, weighting: float, quantity: int, value: float) -> dict:
    """One ``holdings[]`` record; Intrinio documents ``weighting`` as a fraction, not a percent."""
    return {
        "ticker": ticker,
        "name": name,
        "type": "equity",
        "isin": f"US000{ticker[:2]}000000",
        "ric": f"{ticker}.O",
        "sedol": "B0{ticker[:2]}1Q2",
        "share_class_figi": f"BBG001{ticker[:2]}9XRY4",
        "quantity_held": quantity,
        "quantity_units": "shares",
        "quantity_per_share": round(quantity / 1_579_000_000, 8),
        "market_value_held": value,
        "weighting": weighting,
        "as_of_date": "2025-12-31",
    }


def _intrinio_etf_holdings_document() -> dict[str, Any]:
    """One terminal ``/etfs/SPY/holdings`` page with the two heaviest positions."""
    page = _paged(
        [
            {
                "holdings": [
                    _holding_row("AAPL", "Apple Inc.", 0.0648, 102_940_000, 24_530_000_000.0),
                    _holding_row("MSFT", "Microsoft Corp", 0.0431, 44_120_000, 16_320_000_000.0),
                ]
            }
        ]
    )[0]
    return {"responses": [_single("/etfs/SPY/holdings", page)]}


def _etf_profile() -> dict[str, Any]:
    """One ``/etfs/<ticker>`` fund profile; only its ticker is read by the performance join."""
    return {
        "id": "etf_28751",
        "ticker": "SPY",
        "name": "SPDR S&P 500 ETF Trust",
        "sponsor": "State Street Global Advisors",
        "domicile": "US",
        "inception_date": "1993-01-22",
        "exchange_mic": "XNGS",
        "is_live_listed": True,
    }


def _price_row(date: str, close: float, adjusted: float) -> dict[str, Any]:
    """One daily ``stock_prices[]`` row carrying both documented close series."""
    return {
        "date": date,
        "frequency": "daily",
        "open": round(close - 0.9, 2),
        "high": round(close + 1.1, 2),
        "low": round(close - 1.3, 2),
        "close": close,
        "adj_close": adjusted,
        "volume": 58_201_330,
    }


def _intrinio_etf_price_performance_document() -> dict[str, Any]:
    """The two-leg performance join: the fund profile, then one terminal daily-price page."""
    page = _paged(
        [
            {
                "security": {"id": "SEC31002", "ticker": "SPY", "code": "SPY"},
                "stock_prices": [
                    _price_row("2025-02-18", 500.24, 500.24),
                    _price_row("2025-08-15", 541.9, 541.9),
                    _price_row("2026-01-15", 592.04, 592.04),
                    _price_row("2026-02-17", 614.32, 614.32),
                    _price_row("2026-02-18", 618.86, 618.86),
                ],
            }
        ]
    )[0]
    return {
        "responses": [
            _single("/etfs/SPY", _etf_profile()),
            _single("/securities/SPY/prices", page),
        ]
    }


def _option_contract() -> dict[str, Any]:
    """One ``contracts[]`` row in the four nested objects the chain mapping reads (:829-924)."""
    return {
        "option": {
            "id": "OPT7742011",
            "code": "AAPL260220C00190000",
            "ticker": "AAPL",
            "strike": 190.0,
            "expiration": "2026-02-20",
            "type": "call",
            "root": "AAPL",
        },
        "price": {
            "last": 3.15,
            "last_size": 100,
            "last_timestamp": "2026-02-17T20:59:58Z",
            "bid": 3.05,
            "bid_size": 200,
            "bid_timestamp": "2026-02-17T20:59:57Z",
            "ask": 3.25,
            "ask_size": 300,
            "ask_timestamp": "2026-02-17T20:59:58Z",
            "volume": 1_842,
            "open_interest": 24_501,
            "mark": 3.15,
        },
        "stats": {
            "underlying_price": 189.71,
            "implied_volatility": 0.2431,
            "delta": 0.5512,
            "gamma": 0.0288,
            "theta": -0.0914,
            "vega": 0.1102,
            "rho": 0.0431,
        },
        "extended_price": {
            "trade_open": 3.4,
            "bid_open": 3.31,
            "ask_open": 3.49,
            "trade_high": 3.55,
            "bid_high": 3.44,
            "ask_high": 3.62,
            "trade_low": 3.02,
            "bid_low": 2.95,
            "ask_low": 3.1,
            "trade_close": 3.15,
            "bid_close": 3.05,
            "ask_close": 3.25,
        },
    }


def _intrinio_options_chains_document() -> dict[str, Any]:
    """One terminal ``/options/prices/by_ticker/AAPL/realtime`` page with one contract."""
    page = _paged([{"contracts": [_option_contract()]}])[0]
    return {"responses": [_single("/options/prices/by_ticker/AAPL/realtime", page)]}


def _unusual_trade() -> dict[str, Any]:
    """One ``trades[]`` record in the spellings the unusual-activity mapping reads (:763-780)."""
    return {
        "symbol": "AAPL",
        "contract": "AAPL260220C00190000",
        "timestamp": "2026-02-17T15:30:00Z",
        "type": "block",
        "sentiment": "bullish",
        "bid_at_execution": 3.05,
        "ask_at_execution": 3.25,
        "average_price": 3.15,
        "underlying_price_at_execution": 189.71,
        "total_size": 500,
        "total_value": 157_500,
    }


def _intrinio_options_unusual_document() -> dict[str, Any]:
    body = {"trades": [_unusual_trade()]}
    return {"responses": [_single("/options/unusual_activity/AAPL", body)]}


def _consensus_row() -> dict[str, Any]:
    """One ``target_price_consensuses[]`` record; the group number is a decimal string here."""
    return {
        "identifier": "AAPL",
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "high": 260.0,
        "low": 185.0,
        "mean": 224.35,
        "median": 225.0,
        "standard_deviation": 14.2,
        "total": 32,
        "most_recent_date": "2026-02-12",
        "raised": 4,
        "lowered": 1,
        "industry_group_number": "3512",
    }


def _intrinio_price_target_consensus_document() -> dict[str, Any]:
    page = _paged([{"target_price_consensuses": [_consensus_row()]}])[0]
    return {"responses": [_single("/zacks/target_price_consensuses", page)]}


MODELS: Final[dict[str, dict[str, Any]]] = {
    "EquityInfo": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_equity_info_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body"],
            "value": [{"ticker": "AAPL", "name": "Apple Inc."}],
            "column": "body",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_get_json funnels the decoded document through _json_object with 'Intrinio response "
            "must be a JSON object' before any mapping runs, because /companies/<ticker> documents "
            "one profile object, so an answer that arrives as an array must be refused instead of "
            "being read as a profile whose every field is merely absent"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "HistoricalMarketCap": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_historical_market_cap_document,
        "query": {
            "symbol": "AAPL",
            "interval": "day",
            "start_date": "2026-02-16",
            "end_date": "2026-02-19",
        },
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "historical_data"],
            "column": "historical_data",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_get_pages raises unless payload[rows_key] is a list before it ever reads the cursor, "
            "and this route documents its observations under the historical_data pointer, so a "
            "page that publishes no such pointer is a shape failure: answering it as an empty "
            "market-cap series would tell the caller AAPL has never had a market cap rather than "
            "that this page was malformed"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EtfHoldings": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_etf_holdings_document,
        "query": {"symbol": "SPY", "date": "2025-12-31"},
        "tamper": {
            "op": "set",
            "path": ["responses", 0, "body", "holdings", 0, "weighting"],
            "value": "6.48%",
            "column": "weighting",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_percent_fraction refuses any weighting that is not a finite JSON number before the "
            "mapping multiplies it by 100, because the endpoint documents a fraction; the percent-"
            "formatted string '6.48%' would either be dropped as a null weight or, if coerced, "
            "published as a 648% position, so the row must refuse with "
            "INTRINIO_MARKET_SHAPE_INVALID"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "EtfPricePerformance": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_etf_price_performance_document,
        "query": {"symbol": "SPY"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 1, "body", "stock_prices", 0, "adj_close"],
            "column": "adj_close",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "the row this route publishes is declared adjustment='splits_and_dividends' with "
            "calculation_method='adjusted_close_price_ratio_fraction_v1', and "
            "_adjusted_close_series walks the validated series and raises as soon as any day "
            "carries no adj_close, because the ratio would then be computed from one "
            "split-adjusted endpoint and one raw one; dropping the first day's adj_close must "
            "refuse instead of silently falling back to close"
        ),
        "source_files": [_SNAPSHOT_SOURCE, _MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_SNAPSHOT_CONTRACT, _MARKET_CONTRACT, _DOMAINS],
    },
    "OptionsChains": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_options_chains_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "contracts", 0, "option", "code"],
            "column": "code",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "transform_data checks option['code'] is a nonempty string before it maps anything "
            "else, because the OCC-style code is the contract_symbol the row is keyed by and the "
            "strike, expiry and type are all readable out of it; a contract object with no code "
            "cannot be addressed by a caller, so the chain must refuse with "
            "INTRINIO_MARKET_SHAPE_INVALID rather than publish an unnamed contract"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "OptionsUnusual": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_options_unusual_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "drop",
            "path": ["responses", 0, "body", "trades"],
            "column": "trades",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "extract_data reads payload['trades'] and raises 'unusual activity trades must be an "
            "array of objects' unless it is a list, because the delayed feed documents the block "
            "prints under that pointer; an answer without it is not a quiet day for the ticker, it "
            "is an unreadable payload, so INTRINIO_MARKET_SHAPE_INVALID is the only honest face"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
    "PriceTargetConsensus": {
        "client_seam": _SEAM,
        "fixture_document": _intrinio_price_target_consensus_document,
        "query": {"symbol": "AAPL"},
        "tamper": {
            "op": "set",
            "path": [
                "responses",
                0,
                "body",
                "target_price_consensuses",
                0,
                "industry_group_number",
            ],
            "value": "35.12",
            "column": "industry_group_number",
        },
        "refusal_class": _REFUSAL_CLASS,
        "refusal_code": _SHAPE_CODE,
        "tamper_reason": (
            "_industry_group_number accepts an int or a purely decimal string and refuses anything "
            "else, because the endpoint keys its industry groups with an integer id and OpenBB "
            "publishes it as int; '35.12' is neither, and truncating it to 35 would attribute the "
            "consensus to a group Intrinio never named, so the row must refuse with "
            "INTRINIO_MARKET_SHAPE_INVALID"
        ),
        "source_files": [_MARKET_SOURCE, _CLIENT_SOURCE],
        "contract_files": [_MARKET_CONTRACT, _DOMAINS],
    },
}
