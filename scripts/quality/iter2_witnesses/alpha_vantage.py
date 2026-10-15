"""Offline witness configuration for the ``alpha_vantage`` provider.

Alpha Vantage's fetchers own their transport: every leg goes through
``opendata.data.providers.alpha_vantage.client.AlphaVantageClient``, which reads
``get_shared_http_client()`` as a module-level name at request time. The witness
therefore declares a ``client_seam`` over that one module and publishes the fixture
as a ``{"responses": [...]}`` envelope keyed by the official query endpoint, so the
metadata checks, the layout/numeric parsing, and the frozen ``AlphaVantage*`` row
validation that ship are exactly the code the phases exercise -- with the runner's
socket circuit breaker making "offline" enforceable rather than asserted.

The refusal face here is the provider's own: ``AlphaVantageError`` subclasses declare
a machine-readable ``code`` and a ``status`` face, and contract-shape refusals are
raised without an HTTP status, so the declared ``status`` stays ``None`` and no
column-attribution face is claimed.
"""

from __future__ import annotations

from typing import Any, Final

_QUERY_URL = "alphavantage.co/query"


def _equity_daily_envelope() -> dict[str, Any]:
    """Two unadjusted daily bars for AAPL in Alpha Vantage's documented layout."""
    return {
        "responses": [
            {
                "url_contains": _QUERY_URL,
                "status": 200,
                "content_type": "application/json",
                "body": {
                    "Meta Data": {
                        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
                        "2. Symbol": "AAPL",
                        "3. Last Refreshed": "2024-10-03",
                        "4. Output Size": "Full",
                        "5. Time Zone": "US/Eastern",
                    },
                    "Time Series (Daily)": {
                        "2024-10-01": {
                            "1. open": "229.98",
                            "2. high": "232.90",
                            "3. low": "228.96",
                            "4. close": "231.50",
                            "5. volume": "45120300",
                        },
                        "2024-10-02": {
                            "1. open": "231.50",
                            "2. high": "232.10",
                            "3. low": "229.40",
                            "4. close": "230.25",
                            "5. volume": "40110250",
                        },
                    },
                },
            }
        ]
    }


def _etf_daily_envelope() -> dict[str, Any]:
    """Two unadjusted daily bars for SPY in Alpha Vantage's documented layout."""
    return {
        "responses": [
            {
                "url_contains": _QUERY_URL,
                "status": 200,
                "content_type": "application/json",
                "body": {
                    "Meta Data": {
                        "1. Information": "Daily Prices (open, high, low, close) and Volumes",
                        "2. Symbol": "SPY",
                        "3. Last Refreshed": "2024-10-03",
                        "4. Output Size": "Full",
                        "5. Time Zone": "US/Eastern",
                    },
                    "Time Series (Daily)": {
                        "2024-10-01": {
                            "1. open": "569.12",
                            "2. high": "571.34",
                            "3. low": "568.10",
                            "4. close": "570.44",
                            "5. volume": "35210400",
                        },
                        "2024-10-02": {
                            "1. open": "570.44",
                            "2. high": "572.90",
                            "3. low": "569.80",
                            "4. close": "571.22",
                            "5. volume": "31880100",
                        },
                    },
                },
            }
        ]
    }


def _historical_eps_envelope() -> dict[str, Any]:
    """Two quarterly earnings records in the documented EARNINGS response shape."""
    return {
        "responses": [
            {
                "url_contains": _QUERY_URL,
                "status": 200,
                "content_type": "application/json",
                "body": {
                    "symbol": "AAPL",
                    "annualEarnings": [],
                    "quarterlyEarnings": [
                        {
                            "fiscalDateEnding": "2024-06-30",
                            "reportedDate": "2024-08-01",
                            "reportedEPS": "1.40",
                            "estimatedEPS": "1.35",
                            "surprise": "0.05",
                            "surprisePercentage": "3.70",
                            "reportTime": "after-market",
                        },
                        {
                            "fiscalDateEnding": "2024-03-31",
                            "reportedDate": "2024-05-02",
                            "reportedEPS": "1.53",
                            "estimatedEPS": "1.42",
                            "surprise": "0.11",
                            "surprisePercentage": "7.75",
                            "reportTime": "after-market",
                        },
                    ],
                },
            }
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.alpha_vantage.provider",
    "credential_env": {"ALPHA_VANTAGE_API_KEY": "fixture-key-never-sent"},  # nosec B105  # offline placeholder, never sent
    "allowed_hosts": ["www.alphavantage.co"],
    "models": {
        "EquityHistorical": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.alpha_vantage.client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _equity_daily_envelope,
            "query": {"symbol": "AAPL", "interval": "1d", "adjustment": "unadjusted"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "Time Series (Daily)", "2024-10-01", "4. close"],
                "value": "not-a-number",
            },
            "refusal_class": "AlphaVantageResponseError",
            "refusal_code": "ALPHA_VANTAGE_BAD_RESPONSE",
            "tamper_reason": (
                "Alpha Vantage publishes OHLCV cells as numeric strings, so a daily bar whose "
                "'4. close' cell is the literal text 'not-a-number' must be refused by "
                "_parse_price_record as AlphaVantageResponseError instead of being coerced "
                "into an equity history row"
            ),
            "source_files": [
                "opendata/data/providers/alpha_vantage/client.py",
                "opendata/data/providers/alpha_vantage/models/_historical.py",
                "opendata/data/providers/alpha_vantage/models/equity_historical.py",
            ],
            "contract_files": [
                "opendata/data/models/alpha_vantage.py",
                "opendata/data/domains.yaml",
            ],
        },
        "HistoricalEps": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.alpha_vantage.client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _historical_eps_envelope,
            "query": {"symbol": "AAPL", "period": "quarter"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "quarterlyEarnings", 0, "fiscalDateEnding"],
                "value": "2024-13-99",
            },
            "refusal_class": "AlphaVantageResponseError",
            "refusal_code": "ALPHA_VANTAGE_BAD_RESPONSE",
            "tamper_reason": (
                "EARNINGS records are contracted to ISO fiscal dates, so a quarterly entry "
                "whose fiscalDateEnding is the impossible date '2024-13-99' must be refused by "
                "_parse_earnings_record as AlphaVantageResponseError instead of being emitted "
                "with an invented reporting period"
            ),
            "source_files": [
                "opendata/data/providers/alpha_vantage/client.py",
                "opendata/data/providers/alpha_vantage/models/historical_eps.py",
            ],
            "contract_files": [
                "opendata/data/models/alpha_vantage.py",
                "opendata/data/domains.yaml",
            ],
        },
        "EtfHistorical": {
            "client_seam": {
                "kind": "shared_http_client",
                "modules": ["opendata.data.providers.alpha_vantage.client"],
                "attr": "get_shared_http_client",
            },
            "fixture_document": _etf_daily_envelope,
            "query": {"symbol": "SPY", "interval": "1d", "adjustment": "unadjusted"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "Time Series (Daily)", "2024-10-01", "5. volume"],
                "value": "35210.40",
            },
            "refusal_class": "AlphaVantageResponseError",
            "refusal_code": "ALPHA_VANTAGE_BAD_RESPONSE",
            "tamper_reason": (
                "Alpha Vantage volumes are documented as non-negative integers, so a daily bar "
                "whose '5. volume' cell is the fractional string '35210.40' must be refused by "
                "_integer as AlphaVantageResponseError instead of truncating share counts into "
                "an ETF history row"
            ),
            "source_files": [
                "opendata/data/providers/alpha_vantage/client.py",
                "opendata/data/providers/alpha_vantage/models/_historical.py",
                "opendata/data/providers/alpha_vantage/models/etf_historical.py",
            ],
            "contract_files": [
                "opendata/data/models/alpha_vantage.py",
                "opendata/data/domains.yaml",
            ],
        },
    },
}
