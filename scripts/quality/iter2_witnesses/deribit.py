"""Offline witness configuration for the ``deribit`` provider.

Deribit owns its request shape: all five fetchers go through
:func:`opendata.data.providers.deribit.client.rpc_call`, which posts one JSON-RPC envelope through
``get_shared_http_client().request("POST", ...)`` (the call at ``client.py:87`` in
:mod:`opendata.data.providers.deribit.client`). The engine's ``FixedResponseTransport`` is not the
seam for this provider -- no leg reads ``http_transport`` -- which is why these five rows sat in
``BLOCKED_ON_RUNNER_SURFACE`` until wave 29 widened the runner's fake governed client to answer
``request`` as well as ``get``, and to echo a JSON-RPC request id. ``client_seam`` therefore names
:mod:`opendata.data.providers.deribit.client` rather than :mod:`opendata.data.http_client`:
``client.py:12`` binds the factory name at import time, so the shipped call site resolves it in
*its own* module globals, and a witness that patched the owning module would leave the real client
in place while still reporting a clean run. Every entry also declares ``method: "POST"``, so the
widened surface cannot turn a POST-shaped fixture into a free GET.

Serving this seam faithfully costs one thing no other witnessed provider paid: ``_parse_reply``
refuses unless the reply echoes the client's own request id (``client.py:123``,
``type(response_id) is not int or response_id != request_id``), and the id comes from a
process-global counter (``client.py:31``, ``itertools.count(1)``). A fixture pinning that integer
could answer exactly one fetch generation per process -- enough for four of the five phases, but not
for ``counterfact``, whose untampered control run consumes the first ids, so the tampered run asked
2 (or 3-4) and all five models refused at the envelope with ``DERIBIT_BAD_ENVELOPE`` before the
tamper reached a parser. That reading is what moved this module to the faithful shape: a JSON-RPC
server answers the request it was given, so every envelope declares ``id: null`` plus
``echo_request_id`` and the seam fills that one key. What the fixture still proves about order is
carried by the *bodies*, not by integers: FuturesHistorical posts one chart call per symbol to the
same route, so its two-entry ``sequence`` is the only thing that can serve them in order, and a
third call exhausts it and refuses; the other four models address distinct routes.

The bodies are **hand-authored fixture bodies, not live captures**: the day is 2026-01-01 for the
candles and 2026-10-12 for the snapshot, and no claim is made that www.deribit.com published these
values. They carry the field names Deribit's public API documents and the shapes the shipped
parsers read, and they exist to prove the mapping and the gates.

Every refusal is the provider's own ``DeribitProviderError`` carrying a ``DERIBIT_*`` code. That
class carries ``code`` and ``status`` but no field attribution, so ``rejected``/``missing``/
``invalid`` stay empty in the observed face and this module declares no ``expected_*`` detail at
all: the counterfact gate is the code plus the cell the tamper broke, and each ``tamper_reason``
names that cell from the contract rather than from a captured run.

What this module does not cover, and why: the envelope refusals ``DERIBIT_BAD_ENVELOPE`` and
``DERIBIT_BAD_JSON`` (each entry carries one tamper and all five went to the parse/normalize layer,
where the clean-room mapping actually lives; an envelope refusal is reachable by dropping ``usOut``,
but it refuses the whole leg rather than one contract cell, and the request id is seam-owned, so it
cannot be broken from a fixture at all); the HTTP and budget refusals ``DERIBIT_HTTP_ERROR``,
``DERIBIT_RATE_LIMITED``, ``DERIBIT_ACCESS_ERROR``, ``DERIBIT_UPSTREAM_ERROR``,
``DERIBIT_TRANSPORT_ERROR``, ``DERIBIT_RESPONSE_TOO_LARGE`` (a seam that already answers 200 with
a small body cannot withhold the answer); ``DERIBIT_METHOD_NOT_ALLOWED`` (refused before the seam
is touched, so a fixture cannot observe it); ``DERIBIT_OPTION_CHAIN_LIMIT`` and
``DERIBIT_RESPONSE_ROW_LIMIT`` (both need more than 30 tickers / 10 000 candles than a fixture
that answers every call identically can carry); the ``status="no_data"`` chart branch (it returns
zero rows, which ``_run_direct`` refuses by design); and the duplicate-key refusals
``DERIBIT_DUPLICATE_INSTRUMENT`` / ``DERIBIT_DUPLICATE_SUMMARY`` / ``DERIBIT_DUPLICATE_OPTION``
(reachable only by publishing a second record, which is an ``add`` the tamper ops do not offer).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Final

#: The one attribute the transport resolves per request; ``client.py`` binds it at import time,
#: so patching it on that module is what the shipped call site reads on the next request.
_SEAM: Final[dict[str, Any]] = {
    "kind": "shared_http_client",
    "modules": ["opendata.data.providers.deribit.client"],
    "attr": "get_shared_http_client",
}

_SOURCE_FILES: Final[list[str]] = [
    "opendata/data/providers/deribit/client.py",
    "opendata/data/providers/deribit/_source.py",
]

_CONTRACT_FILES: Final[list[str]] = [
    "opendata/data/models/deribit.py",
    "opendata/data/domains.yaml",
]

#: One snapshot instant, shared as ``usOut`` by every envelope and as the ticker timestamp by the
#: option chain. The fetchers compare it against contract expirations, so it has to sit between the
#: directory's creation day and the nearest expiry.
_OBSERVED_AT: Final[datetime] = datetime(2026, 10, 12, 4, 30, 0, 123456, tzinfo=timezone.utc)
_CANDLE_AT: Final[datetime] = datetime(2026, 1, 1, tzinfo=timezone.utc)
_EXPIRY_AT: Final[datetime] = datetime(2026, 12, 25, tzinfo=timezone.utc)
_OPTION_EXPIRY_AT: Final[datetime] = datetime(2027, 6, 30, tzinfo=timezone.utc)
_WINDOW: Final[dict[str, str]] = {"start_date": "2026-01-01", "end_date": "2026-01-01"}

#: ``usOut`` is microseconds, so it is assembled from whole seconds rather than a float product.
_US_OUT: Final[int] = (
    int(_OBSERVED_AT.replace(microsecond=0).timestamp()) * 1_000_000 + _OBSERVED_AT.microsecond
)


def _ms(moment: datetime) -> int:
    """Millisecond epoch of an aware instant, the unit Deribit publishes timestamps in."""
    return int(moment.timestamp() * 1000)


def _envelope(result: object) -> dict[str, object]:
    """One JSON-RPC reply in Deribit's envelope shape, with the id left for the seam to echo.

    ``id`` is ``None`` rather than a pinned integer because the client refuses a reply that does not
    echo its own id; see the module docstring for why the seam, not the fixture, carries that key.
    """
    return {"jsonrpc": "2.0", "id": None, "result": result, "usOut": _US_OUT}


def _future_instrument(name: str) -> dict[str, object]:
    """Hand-author one ``public/get_instruments`` future record."""
    return {
        "kind": "future",
        "instrument_name": name,
        "instrument_id": 138583,
        "instrument_type": "reversed",
        "future_type": "reversed",
        "state": "open",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "counter_currency": "USD",
        "settlement_currency": "USD",
        "settlement_period": "month",
        "underlying_type": "crypto",
        "product_group": "BTC",
        "price_index": "btc_usd",
        "lot_size": 1,
        "min_trade_amount": 10.0,
        "tick_size": 2.5,
        "max_leverage": 50,
        "maker_commission": 0.0,
        "taker_commission": 0.0005,
        "is_active": True,
        "contract_size": 10.0,
        "tick_size_steps": [{"above_price": 100000.0, "tick_size": 5.0}],
        "creation_timestamp": 1_700_000_000_000,
        "expiration_timestamp": _ms(_EXPIRY_AT),
    }


def _option_instrument(name: str) -> dict[str, object]:
    """Hand-author one ``public/get_instruments`` option record, including its strike face."""
    return {
        "kind": "option",
        "instrument_name": name,
        "instrument_id": 240001,
        "option_type": "put",
        "strike": 100000.0,
        "index_id": 1000033,
        "base_currency": "BTC",
        "quote_currency": "USD",
        "counter_currency": "BTC",
        "settlement_currency": "BTC",
        "settlement_asset": "BTC",
        "creation_timestamp": 1_700_000_000_000,
        "expiration_timestamp": _ms(_OPTION_EXPIRY_AT),
        "expiration_date": _OPTION_EXPIRY_AT.strftime("%Y-%m-%d"),
        "activity_currency": "USD",
        "is_active": True,
        "state": "open",
    }


def _future_summary(name: str) -> dict[str, object]:
    """Hand-author one ``public/get_book_summary_by_currency`` future row."""
    return {
        "instrument_name": name,
        "kind": "future",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "volume": 20.5,
        "volume_usd": 2_100_000.0,
        "open_interest": 2000.0,
        "underlying_price": 100900.0,
        "bid_price": 101200.0,
        "ask_price": 101300.0,
        "mark_price": 101250.0,
        "last": 101240.0,
        "high": 103000.0,
        "low": 99000.0,
        "price_change": 2.5,
        "cost_percent": 0.15,
        "estimated_delivery_price": 101000.0,
        "creation_timestamp": 1_700_000_000_000,
    }


def _option_summary(name: str) -> dict[str, object]:
    """Hand-author one option summary row, whose mark is the chain row's implied volatility."""
    return {
        "instrument_name": name,
        "kind": "option",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "volume": 12.25,
        "open_interest": 480.0,
        "underlying_price": 100900.0,
        "bid_price": 3100.0,
        "ask_price": 3300.0,
        "mid_price": 3200.0,
        "mark_price": 3250.0,
        "last": 3240.0,
        "high": 3600.0,
        "low": 3000.0,
        "price_change": None,
        "mark_iv": 72.4,
        "interest_rate": 0.0,
        "underlying_index": "index_price",
        "creation_timestamp": 1_700_000_000_000,
    }


def _future_ticker(name: str) -> dict[str, object]:
    """Hand-author one ``public/ticker`` future response with its 24-hour stats block."""
    return {
        "instrument_name": name,
        "timestamp": _ms(_OBSERVED_AT),
        "state": "open",
        "best_bid_price": 101200.0,
        "best_bid_amount": 25.0,
        "best_ask_price": 101300.0,
        "best_ask_amount": 30.0,
        "last_price": 101240.0,
        "mark_price": 101250.0,
        "index_price": 101000.0,
        "estimated_delivery_price": 101000.0,
        "min_price": 98000.0,
        "max_price": 105000.0,
        "open_interest": 2000.0,
        "settlement_price": 100900.0,
        "current_funding": 0.0001,
        "funding_8h": 0.0002,
        "interest_value": 1.25,
        "stats": {
            "high": 103000.0,
            "low": 99000.0,
            "price_change": 2.5,
            "volume": 20.5,
            "volume_usd": 2_100_000.0,
            "volume_notional": 2_050_000.0,
        },
    }


def _option_ticker(name: str) -> dict[str, object]:
    """Hand-author one option ticker response, adding the iv block and nested greeks."""
    return {
        "instrument_name": name,
        "timestamp": _ms(_OBSERVED_AT),
        "state": "open",
        "best_bid_price": 3100.0,
        "best_ask_price": 3300.0,
        "last_price": 3240.0,
        "mark_price": 3250.0,
        "index_price": 101000.0,
        "underlying_price": 100900.0,
        "underlying_index": "index_price",
        "open_interest": 480.0,
        "min_price": 0.1,
        "max_price": 9999.0,
        "mark_iv": 72.4,
        "bid_iv": 70.0,
        "ask_iv": 73.0,
        "interest_rate": 0.0,
        "greeks": {"delta": 0.4, "gamma": 0.01, "theta": -0.02, "vega": 0.5, "rho": 0.03},
        "stats": {
            "high": 3600.0,
            "low": 3000.0,
            "price_change": -1.1,
            "volume": 12.25,
            "volume_usd": 40_000.0,
        },
    }


def _chart_result(base_price: float) -> dict[str, object]:
    """Hand-author one ``public/get_tradingview_chart_data`` result: one candle, one instrument."""
    return {
        "status": "ok",
        "ticks": [_ms(_CANDLE_AT)],
        "open": [base_price],
        "high": [base_price + 200.0],
        "low": [base_price - 100.0],
        "close": [base_price + 100.0],
        "volume": [5.25],
        "cost": [round((base_price + 100.0) * 5.25, 2)],
    }


def _post(needle: str, result: object) -> dict[str, object]:
    """One seam entry: answer POST to this route with an envelope whose id the seam echoes."""
    return {
        "url_contains": f"public/{needle}",
        "method": "POST",
        "echo_request_id": True,
        "body": _envelope(result),
    }


def _futures_historical_document() -> dict[str, Any]:
    """Two chart replies, one per requested instrument, served in the order the loop asks.

    Both symbols post to the same route, so the payload is the only thing that distinguishes them
    and the seam cannot match on URL; the ``sequence`` is what carries the measured two-call order,
    and a third call to the same route exhausts it and refuses.
    """
    return {
        "responses": [
            {
                "url_contains": "public/get_tradingview_chart_data",
                "method": "POST",
                "echo_request_id": True,
                "sequence": [_envelope(_chart_result(100000.0)), _envelope(_chart_result(3900.0))],
            }
        ]
    }


def _futures_info_document() -> dict[str, Any]:
    """The live ticker of the one instrument the query canonicalized to."""
    return {"responses": [_post("ticker", _future_ticker("SOL_USDC-PERPETUAL"))]}


def _futures_instruments_document() -> dict[str, Any]:
    """The future directory for the requested settlement currency."""
    return {"responses": [_post("get_instruments", [_future_instrument("BTC-25DEC26")])]}


def _futures_curve_document() -> dict[str, Any]:
    """Directory then summaries, the order ``extract_data`` asks for them."""
    return {
        "responses": [
            _post("get_instruments", [_future_instrument("BTC-25DEC26")]),
            _post("get_book_summary_by_currency", [_future_summary("BTC-25DEC26")]),
        ]
    }


def _options_chains_document() -> dict[str, Any]:
    """Directory, summaries and the one bounded per-contract ticker the chain selects."""
    return {
        "responses": [
            _post("get_instruments", [_option_instrument("BTC-30JUN27-100000-P")]),
            _post("get_book_summary_by_currency", [_option_summary("BTC-30JUN27-100000-P")]),
            _post("ticker", _option_ticker("BTC-30JUN27-100000-P")),
        ]
    }


CONFIG: Final[dict[str, Any]] = {
    "registration_module": "opendata.data.providers.deribit.provider",
    "allowed_hosts": ["www.deribit.com"],
    "credential_env": {},
    "models": {
        "FuturesHistorical": {
            "fixture_document": _futures_historical_document,
            "client_seam": _SEAM,
            "query": {
                "symbol": ["BTC-PERPETUAL", "ETH-PERPETUAL"],
                "start_date": "2026-01-01",
                "end_date": "2026-01-01",
                "interval": "1m",
            },
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "sequence", 1, "result", "close", 0],
                "value": "4000.0",
            },
            "refusal_class": "DeribitProviderError",
            "refusal_code": "DERIBIT_BAD_CHART_RESPONSE",
            "tamper_reason": (
                "a close array that carries a numeric string keeps the tick, the open/high/low and "
                "the volume arrays exactly as long, so the length guards pass and only the reader "
                "of close notices: close is a required column of DeribitFuturesHistorical and the "
                "candle fetcher must refuse the chart rather than coerce the cell to a float it "
                "never parsed or publish a bar with no close. The break is in the second of the "
                "two chart replies, so the first instrument's candle is served whole and the "
                "refusal is the fetcher's, not a fixture that never worked"
            ),
            "source_files": [
                "opendata/data/providers/deribit/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "FuturesInfo": {
            "fixture_document": _futures_info_document,
            "client_seam": _SEAM,
            "query": {"symbol": "SOLUSDC"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "result", "instrument_name"],
                "value": "DOGE_USDC-PERPETUAL",
            },
            "refusal_class": "DeribitProviderError",
            "refusal_code": "DERIBIT_INCONSISTENT_TICKER",
            "tamper_reason": (
                "the reply still carries a well-formed timestamp and a complete stats block, so "
                "nothing but the name disagrees: symbol is the contract column of "
                "DeribitFuturesInfo that the caller asked for as SOL_USDC-PERPETUAL, and the "
                "ticker fetcher must refuse a quote about another instrument rather than rename "
                "someone else's book onto the requested future"
            ),
            "source_files": [
                "opendata/data/providers/deribit/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "FuturesInstruments": {
            "fixture_document": _futures_instruments_document,
            "client_seam": _SEAM,
            "query": {"currency": "BTC"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "result", 0, "instrument_id"],
                "value": "138583",
            },
            "refusal_class": "DeribitProviderError",
            "refusal_code": "DERIBIT_BAD_RESPONSE_SHAPE",
            "tamper_reason": (
                "quoting the identifier leaves every other cell of the directory record "
                "well-formed, and instrument_id is the contract column of "
                "DeribitFuturesInstruments that Deribit documents as an integer: the directory "
                "fetcher must refuse the page rather than publish a contract whose id is a string "
                "or silently re-derive it from the instrument name"
            ),
            "source_files": [
                "opendata/data/providers/deribit/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "FuturesCurve": {
            "fixture_document": _futures_curve_document,
            "client_seam": _SEAM,
            "query": {"symbol": "BTC", "market": "global"},
            "tamper": {
                "op": "set",
                "path": ["responses", 1, "body", "result", 0, "instrument_name"],
                "value": "BTC-31DEC99",
            },
            "refusal_class": "DeribitProviderError",
            "refusal_code": "DERIBIT_INCONSISTENT_INSTRUMENTS",
            "tamper_reason": (
                "the summary keeps a mark, a last and a volume, and its new name is a "
                "well-formed dated future that simply is not in the directory the same query "
                "just returned: symbol is the join key between the two replies and a required "
                "column of DeribitFuturesCurve, so the curve fetcher must refuse the snapshot "
                "rather than publish a dated future it has no definition for"
            ),
            "source_files": [
                "opendata/data/providers/deribit/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
        "OptionsChains": {
            "fixture_document": _options_chains_document,
            "client_seam": _SEAM,
            "query": {"symbol": "BTC"},
            "tamper": {
                "op": "set",
                "path": ["responses", 0, "body", "result", 0, "kind"],
                "value": "future",
            },
            "refusal_class": "DeribitProviderError",
            "refusal_code": "DERIBIT_BAD_INSTRUMENT_KIND",
            "tamper_reason": (
                "the record is asked for as kind=option and keeps its strike, option_type and "
                "expiry cells, so only the kind cell contradicts the request and the contract: "
                "DeribitOptionsChains rows are option contracts by definition, and the chain "
                "fetcher must refuse a directory that mixed a future into the option chain rather "
                "than join its summary and ticker onto it"
            ),
            "source_files": [
                "opendata/data/providers/deribit/models/fetchers.py",
                *_SOURCE_FILES,
            ],
            "contract_files": _CONTRACT_FILES,
        },
    },
}
