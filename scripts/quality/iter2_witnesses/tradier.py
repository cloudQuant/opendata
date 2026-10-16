"""Offline witness configuration for the ``tradier`` provider.

Tradier's fetchers own their transport: every leg goes through
``opendata.data.providers.tradier.models._client.fetch_json``, which reads
``get_shared_http_client()`` as a module-level name. The witness therefore declares a
``client_seam`` over that one module and publishes the fixture as a
``{"responses": [...]}`` envelope keyed by the endpoint path, so the parsing, the
``TradierProviderError`` refusals and the frozen Tradier row contracts that ship are exactly
the code the phases exercise -- with the runner's socket circuit breaker making
"offline" enforceable rather than asserted.

The historical pair is a two-endpoint chain, so the envelope carries one entry per endpoint path
and the fake client answers the first ``url_contains`` match: the type lookup and the candles it
gates are separate documents, and a witness that reused one document for both would never prove
that the lookup is what authorizes the domain label.

The refusal face here is the provider's own: ``TradierProviderError`` carries ``code`` and
``status``, and a shape/security-type refusal is raised without an HTTP status, so the
declared ``status`` stays ``None`` and no column-attribution face is claimed.
"""

from __future__ import annotations

from typing import Any, Final

_QUOTE_PATH = "/markets/quotes"
_LOOKUP_PATH = "/markets/lookup"
_HISTORY_PATH = "/markets/history"
_SEARCH_PATH = "/markets/search"
_CHAINS_PATH = "/markets/options/chains"


def _endpoint(url_contains: str, body: dict[str, Any]) -> dict[str, Any]:
    """One Tradier JSON answer in the envelope shape the fake client routes on."""
    return {
        "url_contains": url_contains,
        "status": 200,
        "content_type": "application/json",
        "body": body,
    }


def _history_envelope(
    symbol: str, security_type: str, days: list[dict[str, Any]]
) -> dict[str, Any]:
    """A type-authorized lookup answer plus the EOD candles it gates for one symbol."""
    return {
        "responses": [
            _endpoint(
                _LOOKUP_PATH,
                {
                    "securities": {
                        "security": {
                            "symbol": symbol,
                            "name": f"{symbol} listed security",
                            "type": security_type,
                            "exch": "NASDAQ",
                            "screensymbol": symbol,
                        }
                    }
                },
            ),
            _endpoint(
                _HISTORY_PATH,
                {"history": {"count": len(days), "symbol": symbol, "day": days}},
            ),
        ]
    }


def _bars() -> list[dict[str, Any]]:
    """Three consecutive EOD candles; Tradier publishes them as one ``day`` array."""
    return [
        {
            "date": "2026-01-05",
            "open": 227.2,
            "high": 230.1,
            "low": 226.4,
            "close": 229.8,
            "volume": 55_010_300,
        },
        {
            "date": "2026-01-06",
            "open": 229.8,
            "high": 232.9,
            "low": 228.7,
            "close": 231.5,
            "volume": 45_120_300,
        },
        {
            "date": "2026-01-07",
            "open": 231.5,
            "high": 233.4,
            "low": 230.2,
            "close": 232.6,
            "volume": 41_880_000,
        },
    ]


def _equity_history_envelope() -> dict[str, Any]:
    """Stock history: the lookup answers ``type="stock"``, so the history domain is authorized."""
    return _history_envelope("AAPL", "stock", _bars())


def _etf_history_envelope() -> dict[str, Any]:
    """ETF history: the same two-endpoint chain under the ETF lookup authorization."""
    return _history_envelope("SPY", "etf", _bars())


def _search_envelope() -> dict[str, Any]:
    """Two exchange listings for one partial search term -- Tradier's real multi-venue answer."""
    return {
        "responses": [
            _endpoint(
                _SEARCH_PATH,
                {
                    "securities": {
                        "security": [
                            {
                                "symbol": "AAPL",
                                "exchange": "NASDAQ",
                                "type": "stock",
                                "description": "Apple Inc. Common Stock",
                            },
                            {
                                "symbol": "AAPL",
                                "exchange": "OSL",
                                "type": "stock",
                                "description": "Apple Inc. Oslo Listed",
                            },
                        ]
                    }
                },
            )
        ]
    }


def _option_row(symbol: str, option_type: str, strike: float) -> dict[str, Any]:
    """One chain contract quote in the documented flat ``option`` record shape."""
    return {
        "symbol": symbol,
        "description": f"US AAPL {option_type} {strike}",
        "exch": "NASDAQ",
        "last": 4.35,
        "change": -0.15,
        "change_percentage": -3.33,
        "volume": 1_240,
        "open": 4.1,
        "high": 4.6,
        "low": 3.95,
        "close": 4.5,
        "bid": 4.2,
        "ask": 4.4,
        "trade_date": 1_767_657_600,
        "prevclose": 4.5,
        "open_interest": 12_400,
        "contract_size": 100,
        "expiration_date": "2026-01-16",
        "expiration_type": "standard",
        "option_type": option_type,
        "root_symbol": "AAPL",
        "underlying": "AAPL",
        "strike": strike,
        "lot_size": 100,
    }


def _chains_envelope() -> dict[str, Any]:
    """One expiry's chain: a call and a put, both for the requested underlying and date."""
    return {
        "responses": [
            _endpoint(
                _CHAINS_PATH,
                {
                    "options": {
                        "option": [
                            _option_row("AAPL260116C00150000", "call", 150.0),
                            _option_row("AAPL260116P00150000", "put", 150.0),
                        ]
                    }
                },
            )
        ]
    }


def _quote_envelope() -> dict[str, Any]:
    """One stock quote record in Tradier's own XML-to-JSON shape."""
    return {
        "responses": [
            {
                "url_contains": _QUOTE_PATH,
                "status": 200,
                "content_type": "application/json",
                "body": {
                    "quotes": {
                        "quote": {
                            "symbol": "AAPL",
                            "description": "Apple Inc.",
                            "exch": "NASDAQ",
                            "type": "stock",
                            "last": 231.5,
                            "change": 1.25,
                            "volume": 45_120_300,
                            "open": 230.1,
                            "high": 232.9,
                            "low": 229.4,
                            "close": 230.25,
                            "bid": 231.45,
                            "ask": 231.55,
                            "changepercentage": 0.55,
                            "change_percentage": 0.55,
                            "average_volume": 52_000_000,
                            "last_volume": 1_024,
                            "trade_date": 1_728_691_200,
                            "prevclose": 229.0,
                            "week_52_high": 260.1,
                            "week_52_low": 169.21,
                            "bidsize": 10,
                            "bidexch": "NASDAQ",
                            "bid_date": 1_728_691_200,
                            "asksize": 12,
                            "askexch": "NASDAQ",
                            "ask_date": 1_728_691_200,
                            "lot_size": 1,
                        }
                    }
                },
            }
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.tradier.registration",
    "credential_env": {"TRADIER_TOKEN": "fixture-token-never-sent"},  # nosec B105  # offline placeholder, never sent
    "allowed_hosts": ["api.tradier.com"],
    "models": {
        "EquityQuote": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.tradier.models._client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _quote_envelope,
            "query": {"symbol": "AAPL"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "quotes", "quote", "type"],
                "value": "etf",
            },
            "refusal_class": "TradierProviderError",
            "refusal_code": "TRADIER_WRONG_SECURITY_TYPE",
            "tamper_reason": (
                "EquityQuote is contracted to the stock security type, so a quote payload that "
                "answers 'AAPL' with type='etf' must be refused by transform_data as "
                "TRADIER_WRONG_SECURITY_TYPE instead of being emitted as a stock quote"
            ),
            "source_files": [
                "opendata/data/providers/tradier/models/_client.py",
                "opendata/data/providers/tradier/models/equity_quote.py",
                "opendata/data/providers/tradier/models/_queries.py",
            ],
            "contract_files": [
                "opendata/data/models/tradier.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquityHistorical": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.tradier.models._client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _equity_history_envelope,
            "query": {"symbol": "AAPL", "start_date": "2026-01-05", "end_date": "2026-01-09"},
            "tamper": {
                "op": "set",
                "path": ["responses", 1, "body", "history", "symbol"],
                "value": "MSFT",
            },
            "refusal_class": "TradierProviderError",
            "refusal_code": "TRADIER_HISTORY_SYMBOL_MISMATCH",
            "tamper_reason": (
                "extract_data honors only history whose payload names the symbol it requested, so "
                "a /markets/history answer that carries symbol='MSFT' under an AAPL request must "
                "be refused instead of labeling MSFT candles as AAPL bars"
            ),
            "source_files": [
                "opendata/data/providers/tradier/models/_client.py",
                "opendata/data/providers/tradier/models/equity_historical.py",
                "opendata/data/providers/tradier/models/_queries.py",
            ],
            "contract_files": [
                "opendata/data/models/tradier.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EtfHistorical": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.tradier.models._client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _etf_history_envelope,
            "query": {"symbol": "SPY", "start_date": "2026-01-05", "end_date": "2026-01-09"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "securities", "security", "type"],
                "value": "stock",
            },
            "refusal_class": "TradierProviderError",
            "refusal_code": "TRADIER_WRONG_SECURITY_TYPE",
            "tamper_reason": (
                "EtfHistorical is reached only after the lookup answers type='etf' for the exact "
                "symbol, so a lookup that reports the same symbol as a stock must be refused "
                "before any history request is made rather than published as an ETF bar"
            ),
            "source_files": [
                "opendata/data/providers/tradier/models/_client.py",
                "opendata/data/providers/tradier/models/etf_historical.py",
                "opendata/data/providers/tradier/models/_queries.py",
            ],
            "contract_files": [
                "opendata/data/models/tradier.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EquitySearch": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.tradier.models._client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _search_envelope,
            "query": {"query": "apple"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "securities", "security", 1, "exchange"],
                "value": "NASDAQ",
            },
            "refusal_class": "TradierProviderError",
            "refusal_code": "TRADIER_DUPLICATE_SEARCH_RESULT",
            "tamper_reason": (
                "a search result is identified by (symbol, exchange), so two listings that both "
                "name AAPL on NASDAQ are the same catalog row twice and transform_data must "
                "refuse the whole page instead of emitting a duplicated security"
            ),
            "source_files": [
                "opendata/data/providers/tradier/models/_client.py",
                "opendata/data/providers/tradier/models/equity_search.py",
            ],
            "contract_files": [
                "opendata/data/models/tradier.py",
                "opendata/data/domains.yaml",
            ],
        },
        "OptionsChains": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.tradier.models._client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _chains_envelope,
            "query": {"symbol": "AAPL", "expiration": "2026-01-16"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "options", "option", 1, "expiration_date"],
                "value": "2026-02-20",
            },
            "refusal_class": "TradierProviderError",
            "refusal_code": "TRADIER_WRONG_EXPIRATION",
            "tamper_reason": (
                "the chain was requested for one expiration, so a contract row carrying a "
                "different expiration_date than the query would mislabel the expiry of a whole "
                "page and must be refused by transform_data as TRADIER_WRONG_EXPIRATION"
            ),
            "source_files": [
                "opendata/data/providers/tradier/models/_client.py",
                "opendata/data/providers/tradier/models/options_chains.py",
                "opendata/data/providers/tradier/models/_queries.py",
            ],
            "contract_files": [
                "opendata/data/models/tradier.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
