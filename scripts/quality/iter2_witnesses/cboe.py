"""Offline witness configuration for the cboe provider models.

Three of cboe's rows run through the declaration engine, so this module is the reference
witness for that population: refusals here are the engine's own attributed failures
(``missing=(column,)`` / ``rejected=(column,)``), not an adapter's bare code-only raise.

EquityQuote, EquitySearch and OptionsChains are the hand-written All Access adapters. Their
refusal is ``CboeAllAccessError``, a :class:`ProviderEngineError` that names the shape guard it
tripped and attributes no column, because these guards refuse a whole published record rather
than one declared field. IndexHistorical, EquityHistorical and EtfHistorical stay unwitnessed in
this wave: the first reads a CSV body the JSON fixture seam cannot carry, and the other two need
a different body per request (trading-day list, then one quote page per date), which a fixed
transport cannot answer.
"""

from __future__ import annotations

from typing import Any, Final


def _directory_record(symbol: str, name: str) -> dict[str, Any]:
    """One US index-directory record in the keys ``AVAILABLE_INDICES`` reads."""
    return {
        "index_symbol": symbol,
        "index_name": name,
        "currency": "USD",
        "description": f"{name} index",
        "mkt_data_delay": "15 min",
        "calc_start_time": "08:30",
        "calc_end_time": "21:00",
        "time_zone": "US/Eastern",
        "tick_days": "Monday,Tuesday,Wednesday,Thursday,Friday",
        "tick_frequency": "15 seconds",
        "tick_period": "Open to Close",
    }


def _cboe_available_indices_document() -> list[dict[str, Any]]:
    return [_directory_record("SPX", "S&P 500"), _directory_record("MDY", "S&P MidCap 400")]


def _cboe_index_search_document() -> list[dict[str, Any]]:
    return [_directory_record("SPX", "S&P 500"), _directory_record("OXY", "Occidental Petroleum")]


def _cboe_index_constituents_document() -> dict[str, Any]:
    return {
        "symbol": "EUDSTX",
        "name": "Cboe Europe Development Market Cap TSE Weighted Index",
        "currency": "EUR",
        "data": [
            {
                "symbol": "ADNO",
                "name": "Adidas AG",
                "currency": "EUR",
                "security_type": "Common Stock",
                "current_price": 10.5,
                "open": 10.1,
                "high": 10.7,
                "low": 10.0,
                "close": 10.4,
                "volume": 1_250_000,
                "prev_day_close": 10.2,
                "price_change": 0.3,
                "price_change_percent": 0.0294,
                "tick": "1 cent",
                "last_trade_time": "2026-01-02T09:30:00",
                "type": "equity",
            }
        ],
    }


def _cboe_equity_quote_document() -> list[dict[str, Any]]:
    """One All Access underlying-quote record for the one requested symbol.

    ``/market/underlying-quotes`` answers with an array keyed by ``symbol``; the adapter selects
    the record whose ``symbol`` equals the query and maps the ``underlying_*`` fields onto the
    ``CboeEquityQuote`` columns, keeping every field this route does not publish null.
    """
    return [
        {
            "symbol": "AAPL",
            "underlying_bid": 181.0,
            "underlying_bid_size": 100,
            "underlying_ask": 181.5,
            "underlying_ask_size": 200,
            "seq_no": 42,
            "underlying_last_trade_price": 181.25,
            "underlying_last_trade_size": 500,
            "underlying_open": 180.0,
            "underlying_high": 182.25,
            "underlying_low": 179.5,
            "underlying_close": 181.25,
            "underlying_volume": 45_000_000.0,
            "underlying_prev_day_close": 179.9,
            "iv30": 15.5,
            "iv30_change": 0.5,
            "iv30_change_percent": 3.2,
        }
    ]


def _cboe_equity_search_document() -> list[dict[str, Any]]:
    """Two records from the authenticated ``/reference/symbols`` collection."""
    return [
        {"name": "AAPL", "company_name": "Apple Inc."},
        {"name": "MSFT", "company_name": "Microsoft Corp."},
    ]


def _cboe_options_chains_document() -> list[dict[str, Any]]:
    """One synchronized option-and-underlying snapshot with two listed contracts."""
    return [
        {
            "symbol": "AAPL",
            "underlying_last_trade_price": 181.25,
            "options": [
                {
                    "option": "AAPL260116C00185000",
                    "expiry": "2026-01-16",
                    "strike": 185.0,
                    "option_type": "C",
                    "open_interest": 1_200,
                    "option_volume": 340,
                    "cboe_theo": 3.15,
                    "option_last_trade_price": 3.2,
                    "option_bid": 3.1,
                    "option_bid_size": 40,
                    "option_ask": 3.3,
                    "option_ask_size": 60,
                    "option_mid": 3.2,
                    "option_open": 3.05,
                    "option_high": 3.4,
                    "option_low": 2.95,
                    "option_close": 3.2,
                    "option_prev_day_close": 3.0,
                    "iv": 0.21,
                    "delta": 0.42,
                    "gamma": 0.05,
                    "theta": -0.02,
                    "vega": 0.11,
                    "rho": 0.03,
                },
                {
                    "option": "AAPL260116P00170000",
                    "expiry": "2026-01-16",
                    "strike": 170.0,
                    "option_type": "P",
                    "open_interest": 800,
                    "option_volume": 120,
                    "cboe_theo": 1.05,
                    "option_last_trade_price": 1.1,
                    "option_bid": 1.0,
                    "option_bid_size": 20,
                    "option_ask": 1.2,
                    "option_ask_size": 30,
                    "option_mid": 1.1,
                    "option_open": 1.02,
                    "option_high": 1.15,
                    "option_low": 0.98,
                    "option_close": 1.1,
                    "option_prev_day_close": 1.04,
                    "iv": 0.24,
                    "delta": -0.28,
                    "gamma": 0.04,
                    "theta": -0.01,
                    "vega": 0.09,
                    "rho": -0.02,
                },
            ],
        }
    ]


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.cboe.registration",
    "credential_env": {"CBOE_LIVEVOL_ACCESS_TOKEN": "fixture-access-token-never-sent"},  # nosec B105  # offline placeholder, never sent
    "allowed_hosts": ["cdn.cboe.com", "api.livevol.com"],
    "models": {
        "AvailableIndices": {
            "fixture_document": _cboe_available_indices_document,
            "query": {},
            "tamper": {
                "op": "drop",
                "path": [0, "index_symbol"],
                "column": "symbol",
            },
            "expected_missing": ["symbol"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "CBOE_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a directory record without 'index_symbol' makes the required "
                "'symbol' column unresolvable, so the engine must refuse with "
                "CBOE_SHAPE_INVALID attributing missing=('symbol',)"
            ),
            "source_files": [
                "opendata/data/providers/cboe/specs.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/cboe_index.py",
                "opendata/data/domains.yaml",
            ],
        },
        "IndexSearch": {
            "fixture_document": _cboe_index_search_document,
            "query": {"query": "SPX", "is_symbol": True},
            "tamper": {
                "op": "drop",
                "path": [0, "index_name"],
                "column": "name",
            },
            "expected_missing": ["name"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "CBOE_SHAPE_INVALID",
            "tamper_reason": (
                "dropping 'index_name' from the only record the symbol filter keeps makes the "
                "required 'name' column unresolvable, so the engine must refuse with "
                "CBOE_SHAPE_INVALID attributing missing=('name',) rather than publishing a "
                "nameless search hit"
            ),
            "source_files": [
                "opendata/data/providers/cboe/specs.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/cboe_index.py",
                "opendata/data/domains.yaml",
            ],
        },
        "IndexConstituents": {
            "fixture_document": _cboe_index_constituents_document,
            "query": {"symbol": "EUDSTX"},
            "tamper": {
                "op": "set",
                "path": ["data", 0, "volume"],
                "value": True,
                "column": "volume",
            },
            "expected_rejected": ["volume"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "CBOE_SHAPE_INVALID",
            "tamper_reason": (
                "publishing a boolean where the declaration says an int kind is the numeric "
                "guard's own refusal: pydantic would silently widen True to 1 share, so the "
                "engine must refuse with CBOE_SHAPE_INVALID attributing rejected=('volume',)"
            ),
            "source_files": [
                "opendata/data/providers/cboe/specs.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/cboe_index.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquityQuote": {
            "fixture_document": _cboe_equity_quote_document,
            "query": {"symbol": "AAPL", "date": "2026-01-02"},
            "tamper": {
                "op": "set",
                "path": [0, "underlying_bid_size"],
                "value": True,
                "column": "bid_size",
            },
            "refusal_class": "CboeAllAccessError",
            "refusal_code": "CBOE_LIVEVOL_BAD_INTEGER_FIELD",
            "tamper_reason": (
                "publishing a boolean where the quote route documents an integer board size is "
                "the adapter's own integer guard: pydantic would widen True to a 1-share bid, so "
                "the adapter must refuse with CBOE_LIVEVOL_BAD_INTEGER_FIELD rather than build a "
                "CboeEquityQuote whose bid_size is a flag"
            ),
            "source_files": [
                "opendata/data/providers/cboe/models/all_access.py",
                "opendata/data/providers/cboe/models/_all_access_client.py",
            ],
            "contract_files": [
                "opendata/data/models/congress_cboe_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquitySearch": {
            "fixture_document": _cboe_equity_search_document,
            "query": {"query": "apple", "is_symbol": False},
            "tamper": {
                "op": "drop",
                "path": [0, "company_name"],
                "column": "name",
            },
            "refusal_class": "CboeAllAccessError",
            "refusal_code": "CBOE_LIVEVOL_BAD_SYMBOL_ROW",
            "tamper_reason": (
                "the symbols reference publishes a company name for every listed symbol, and the "
                "search row's 'name' column is read from it; a record without 'company_name' is "
                "not a directory row, so the adapter must refuse with "
                "CBOE_LIVEVOL_BAD_SYMBOL_ROW rather than emit a nameless listing"
            ),
            "source_files": [
                "opendata/data/providers/cboe/models/all_access.py",
                "opendata/data/providers/cboe/models/_all_access_client.py",
            ],
            "contract_files": [
                "opendata/data/models/cboe_index.py",
                "opendata/data/domains.yaml",
            ],
        },
        "OptionsChains": {
            "fixture_document": _cboe_options_chains_document,
            "query": {"symbol": "AAPL", "date": "2026-01-02"},
            "tamper": {
                "op": "set",
                "path": [0, "options", 0, "option_type"],
                "value": "X",
                "column": "option_type",
            },
            "refusal_class": "CboeAllAccessError",
            "refusal_code": "CBOE_LIVEVOL_BAD_OPTION_TYPE",
            "tamper_reason": (
                "the option-and-underlying route enumerates a contract as 'C' or 'P' and the "
                "contract column narrows that to call/put; a published 'X' is neither, so the "
                "adapter must refuse with CBOE_LIVEVOL_BAD_OPTION_TYPE instead of guessing which "
                "side of the chain the contract is on"
            ),
            "source_files": [
                "opendata/data/providers/cboe/models/all_access.py",
                "opendata/data/providers/cboe/models/_all_access_client.py",
            ],
            "contract_files": [
                "opendata/data/models/congress_cboe_iter2.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
