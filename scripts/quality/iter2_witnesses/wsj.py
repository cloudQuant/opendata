"""Offline witness configuration for the ``wsj`` ETF mover tables.

All three inventory rows run through the declaration engine over ``data.instruments``; a
document whose first instrument omits its ``ticker`` leaves the required, non-nullable
``symbol`` column unresolvable, so the engine's own refusal is the counterfact. The endpoint
requires no credential; the browser User-Agent is a declaration fact, not a secret.

Witnesses run exclusively through the offline seam (``http_transport``) served by
``FixedResponseTransport``. No leg touches the network.
"""

from __future__ import annotations

from typing import Any, Final


def _instrument(ticker: str, name: str, last: float, change: float, volume: int) -> dict[str, Any]:
    """One WSJ instrument in the JSON spellings the page publishes."""
    return {
        "bluegrassChannel": "/market-data/etf",
        "country": "US",
        "lastPrice": last,
        "mantissa": 2,
        "percentChange": change,
        "priceChange": round(change / 100 * last, 2),
        "name": name,
        "ticker": ticker,
        "timestamp": "2026-10-20T20:00:00.247+00:00",
        "type": "ETF",
        "volume": volume,
        "formattedPrice": f"{last:.2f}",
        "formattedPriceChange": f"{change / 100 * last:+.2f}",
        "formattedPercentChange": f"{change:+.2f}%",
        "formattedVolume": f"{volume / 1000:.1f}K",
        "url": f"https://www.wsj.com/market-data/quotes/etf/{ticker}",
    }


def _mover_document() -> dict[str, Any]:
    """A two-instrument answer from one mover table."""
    return {
        "id": '{"application":"WSJ","etfMover":"fixture","count":25}',
        "type": "mdc_etfmovers",
        "data": {
            "instruments": [
                _instrument("AAA", "Fixture ETF A", 10.5, 2.0, 12_000),
                _instrument("BBB", "Fixture ETF B", 20.25, -1.5, 8_000),
            ]
        },
    }


_TAMPER_REASON: Final = (
    "publishing a mover row without its 'ticker' leaves the required, non-nullable 'symbol' "
    "column unresolvable, so the engine must refuse with WSJ_SHAPE_INVALID attributing "
    "missing=('symbol',) rather than shipping an ETF row the caller cannot cite back"
)

CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.wsj.registration",
    "credential_env": {},
    "allowed_hosts": ["www.wsj.com"],
    "models": {
        model: {
            "fixture_document": _mover_document,
            "query": {},
            "tamper": {"op": "drop", "path": ["data", "instruments", 0, "ticker"], "column": "symbol"},
            "expected_missing": ["symbol"],
            "refusal_class": "ProviderEngineError",
            "refusal_code": "WSJ_SHAPE_INVALID",
            "tamper_reason": _TAMPER_REASON,
            "source_files": [
                "opendata/data/providers/wsj/models/etf_movers.py",
                "opendata/data/providers/wsj/specs.py",
                "opendata/data/providers/_engine/http_json.py",
            ],
            "contract_files": [
                "opendata/data/models/wsj.py",
                "opendata/data/domains.yaml",
            ],
        }
        for model in ("ETFGainers", "ETFLosers", "ETFActive")
    },
}
