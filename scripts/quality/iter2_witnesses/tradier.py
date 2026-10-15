"""Offline witness configuration for the ``tradier`` provider.

Tradier's fetchers own their transport: every leg goes through
``opendata.data.providers.tradier.models._client.fetch_json``, which reads
``get_shared_http_client()`` as a module-level name. The witness therefore declares a
``client_seam`` over that one module and publishes the fixture as a
``{"responses": [...]}`` envelope keyed by the endpoint path, so the parsing, the
``TradierProviderError`` refusals and the frozen ``TradierEquityQuote`` validation that ship
are exactly the code the phases exercise -- with the runner's socket circuit breaker making
"offline" enforceable rather than asserted.

The refusal face here is the provider's own: ``TradierProviderError`` carries ``code`` and
``status``, and a shape/security-type refusal is raised without an HTTP status, so the
declared ``status`` stays ``None`` and no column-attribution face is claimed.
"""

from __future__ import annotations

from typing import Any, Final

_QUOTE_PATH = "/markets/quotes"


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
    },
}
