"""Offline tests for Deribit's public JSON-RPC adapters and output contracts."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from opendata.data.models.deribit import (
    DeribitFuturesCurve,
    DeribitFuturesHistorical,
    DeribitFuturesInfo,
    DeribitFuturesInstruments,
    DeribitOptionsChains,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.deribit import client
from opendata.data.providers.deribit.client import DeribitProviderError, RpcReply
from opendata.data.providers.deribit.models.fetchers import (
    DeribitFuturesCurveFetcher,
    DeribitFuturesHistoricalFetcher,
    DeribitFuturesInfoFetcher,
    DeribitFuturesInstrumentsFetcher,
    DeribitOptionsChainsFetcher,
)
from opendata.data.providers.deribit.provider import PROVIDER
from opendata.data.registry import ProviderRegistry
from opendata.data.request_budget import (
    GrantDecision,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services.provider_model_query import query_provider_model

OBSERVED_AT = datetime(2026, 10, 12, 4, 30, 0, 123456, tzinfo=timezone.utc)
HISTORY_TICK = 1_767_225_600_000


def _authorized_context(model: str) -> FetchContext:
    """Build finite offline-only permission for a fake Deribit transport."""
    profile = RequestBudgetProfile.BOUNDED_REQUEST
    grant = RequestGrant(
        source="deribit",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="offline-fixture-only-no-live-request",
        task_attempts=64,
        source_attempts=32,
        allowed_hosts={"www.deribit.com"},
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        profile=profile,
    )
    return FetchContext(
        timeout=5.0,
        request_budget=RequestBudget(
            task_attempts=64,
            source_attempts=32,
            grants=(grant,),
            profile=profile,
        ),
    )


def _fetch(fetcher: Fetcher[Any, Any], **query: object) -> FetchResult:
    """Run a fixture fetch with an exact local query grant."""
    model = fetcher.canonical_model
    assert model is not None
    return fetcher.fetch(ctx=_authorized_context(model), **query)


def _instrument(
    name: str,
    *,
    kind: str = "future",
    instrument_id: int = 138583,
    expiration: datetime = datetime(2026, 12, 25, tzinfo=timezone.utc),
    settlement_period: str = "month",
    base_currency: str = "BTC",
    contract_size: float = 10.0,
    instrument_type: str = "reversed",
) -> dict[str, object]:
    """Hand-author a public/get_instruments record in Deribit's field shape."""
    return {
        "kind": kind,
        "instrument_name": name,
        "instrument_id": instrument_id,
        "instrument_type": instrument_type,
        "future_type": "reversed" if kind == "future" else None,
        "state": "open",
        "base_currency": base_currency,
        "quote_currency": "USD",
        "counter_currency": "USD",
        "settlement_currency": "BTC",
        "settlement_period": settlement_period,
        "underlying_type": "crypto",
        "product_group": "BTC",
        "price_index": "btc_usd",
        "lot_size": 1,
        "min_trade_amount": 10.0,
        "tick_size": 2.5,
        "max_leverage": 50,
        "max_non_default_leverage": 25.0,
        "max_liquidation_commission": 0.0075,
        "maker_commission": 0.0,
        "taker_commission": 0.0005,
        "block_trade_commission": 0.00025,
        "block_trade_min_trade_amount": 200000.0,
        "block_trade_tick_size": 0.01,
        "is_active": True,
        "contract_size": contract_size,
        "index_id": 1000033,
        "tick_size_steps": [{"above_price": 100000.0, "tick_size": 5.0}],
        "base_currency_uuid": "base-btc",
        "quote_currency_uuid": "quote-usd",
        "creation_timestamp": 1_700_000_000_000,
        "expiration_timestamp": int(expiration.timestamp() * 1000),
    }


def _summary(
    name: str,
    *,
    kind: str = "future",
    base_currency: str = "BTC",
    quote_currency: str = "USD",
) -> dict[str, object]:
    """Hand-author an official summary response with quote/session fields."""
    record: dict[str, object] = {
        "instrument_name": name,
        "base_currency": base_currency,
        "quote_currency": quote_currency,
        "volume": 20.5,
        "open_interest": 2000.0,
        "underlying_price": 100900.0,
        "bid_price": 101200.0,
        "ask_price": 101300.0,
        "mid_price": 101250.0,
        "mark_price": 101250.0,
        "last": 101240.0,
        "high": 103000.0,
        "low": 99000.0,
        "price_change": 2.5,
        "creation_timestamp": 1_700_000_000_000,
    }
    if kind == "future":
        record.update(
            {
                "volume_usd": 2_100_000.0,
                "volume_notional": 2_050_000.0,
                "estimated_delivery_price": 101000.0,
            }
        )
    else:
        record.update(
            {
                "underlying_index": "index_price",
                "mark_iv": 72.4,
                "interest_rate": 0.0,
                "price_change": None,
            }
        )
    return record


def _ticker(name: str, *, timestamp: int = HISTORY_TICK) -> dict[str, object]:
    """Hand-author an official public/ticker future response."""
    return {
        "instrument_name": name,
        "timestamp": timestamp,
        "state": "open",
        "stats": {
            "high": 103000.0,
            "low": 99000.0,
            "price_change": 2.5,
            "volume": 20.5,
            "volume_usd": 2_100_000.0,
            "volume_notional": 2_050_000.0,
        },
        "best_bid_price": 101200.0,
        "best_bid_amount": 25.0,
        "best_ask_price": 101300.0,
        "best_ask_amount": 30.0,
        "current_funding": 0.0001,
        "funding_8h": 0.0002,
        "interest_value": 1.25,
        "last_price": 101240.0,
        "mark_price": 101250.0,
        "index_price": 101000.0,
        "estimated_delivery_price": 101000.0,
        "min_price": 98000.0,
        "max_price": 105000.0,
        "open_interest": 2000.0,
        "settlement_price": 100900.0,
    }


def _option_ticker(name: str) -> dict[str, object]:
    """Hand-author the option-only public/ticker fields and nested greeks."""
    timestamp = int(
        datetime(2026, 10, 12, 4, 30, 0, 123000, tzinfo=timezone.utc).timestamp() * 1000
    )
    record = _ticker(name, timestamp=timestamp)
    record.update(
        {
            "underlying_index": "index_price",
            "underlying_price": 100900.0,
            "index_price": 101000.0,
            "mark_iv": 72.4,
            "bid_iv": 70.0,
            "ask_iv": 73.0,
            "interest_rate": 0.0,
            "greeks": {
                "delta": 0.4,
                "gamma": 0.01,
                "theta": -0.02,
                "vega": 0.5,
                "rho": 0.03,
            },
        }
    )
    return record


def _chart(tick: int = HISTORY_TICK) -> dict[str, object]:
    """Hand-author one public/get_tradingview_chart_data record."""
    return {
        "status": "ok",
        "ticks": [tick],
        "open": [100000.0],
        "high": [100200.0],
        "low": [99900.0],
        "close": [100100.0],
        "volume": [5.25],
        "cost": [525000.0],
    }


def _rpc_script(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[RpcReply],
) -> list[dict[str, object]]:
    """Replace transport with ordered hand-authored replies and record calls."""
    calls: list[dict[str, object]] = []
    remaining = list(replies)

    def fake_rpc(
        method: str,
        params: dict[str, object],
        *,
        timeout: float | None = None,
    ) -> RpcReply:
        calls.append({"method": method, "params": params, "timeout": timeout})
        if not remaining:
            raise AssertionError("fetcher sent more RPC calls than the fixture declared")
        return remaining.pop(0)

    monkeypatch.setattr(client, "rpc_call", fake_rpc)
    return calls


def test_provider_binds_exactly_the_five_deribit_models() -> None:
    assert PROVIDER.source == "deribit"
    assert set(PROVIDER.fetcher_dict) == {
        "FuturesCurve",
        "FuturesHistorical",
        "FuturesInfo",
        "FuturesInstruments",
        "OptionsChains",
    }
    assert all(capability.verified is False for capability in PROVIDER.capabilities)


@pytest.mark.parametrize(
    ("fetcher_type", "query"),
    (
        (
            DeribitFuturesHistoricalFetcher,
            {
                "symbol": "BTC-PERPETUAL",
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 1),
            },
        ),
        (DeribitFuturesInfoFetcher, {"symbol": "BTC-PERPETUAL"}),
        (DeribitFuturesInstrumentsFetcher, {"currency": "BTC"}),
        (DeribitFuturesCurveFetcher, {"symbol": "BTC"}),
        (DeribitOptionsChainsFetcher, {"symbol": "BTC"}),
    ),
)
def test_queries_accept_service_injected_source_and_global_market(
    fetcher_type: type[Fetcher[Any, Any]],
    query: dict[str, object],
) -> None:
    params = fetcher_type().transform_query(**query, source="deribit", market="global")
    assert params.source == "deribit"
    assert params.market == "global"


def test_historical_maps_official_cost_to_volume_notional_and_supports_symbol_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rpc_script(
        monkeypatch,
        [
            RpcReply(_chart(), OBSERVED_AT),
            RpcReply(_chart(), OBSERVED_AT),
        ],
    )
    rows = _fetch(
        DeribitFuturesHistoricalFetcher(),
        symbol=["BTC-PERPETUAL", "ETH-PERPETUAL"],
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 1),
        interval="1m",
    )

    assert [call["params"] for call in calls] == [
        {
            "instrument_name": "BTC-PERPETUAL",
            "start_timestamp": HISTORY_TICK,
            "end_timestamp": 1_767_311_999_999,
            "resolution": "1",
        },
        {
            "instrument_name": "ETH-PERPETUAL",
            "start_timestamp": HISTORY_TICK,
            "end_timestamp": 1_767_311_999_999,
            "resolution": "1",
        },
    ]
    assert [row.symbol for row in rows] == ["BTC-PERPETUAL", "ETH-PERPETUAL"]
    assert rows[0].date == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert rows[0].volume_notional == 525000.0
    assert rows[0].volume == 5.25
    assert rows[0].close == 100100.0


@pytest.mark.parametrize(
    "query",
    [
        {"symbol": "BTC-PERPETUAL", "start_date": date(2026, 1, 2), "end_date": date(2026, 1, 1)},
        {
            "symbol": "BTC-PERPETUAL",
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
            "interval": "2m",
        },
        {"symbol": "BTC-PERPETUAL", "start_date": date(2026, 1, 1)},
        {
            "symbol": "BTC-PERPETUAL?x=1",
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
        },
        {
            "symbol": "BTC-25DEC26",
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
            "expiration": "2027-01",
        },
        {
            "symbol": "BTC-PERPETUAL",
            "start_date": date(2020, 1, 1),
            "end_date": date(2026, 1, 1),
            "interval": "1m",
        },
    ],
)
def test_historical_invalid_parameters_are_refused_before_io(
    monkeypatch: pytest.MonkeyPatch,
    query: dict[str, object],
) -> None:
    calls = _rpc_script(monkeypatch, [])
    with pytest.raises(ValidationError):
        DeribitFuturesHistoricalFetcher().fetch(**query)
    assert calls == []


def test_historical_no_data_is_empty_but_bad_arrays_and_time_boundaries_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_data = {"status": "no_data", "ticks": [], "open": [], "close": []}
    _rpc_script(monkeypatch, [RpcReply(no_data, OBSERVED_AT)])
    assert (
        _fetch(
            DeribitFuturesHistoricalFetcher(),
            symbol="BTC-PERPETUAL",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 1),
        )
        == ()
    )

    malformed = _chart()
    malformed["high"] = []
    _rpc_script(monkeypatch, [RpcReply(malformed, OBSERVED_AT)])
    with pytest.raises(DeribitProviderError, match="DERIBIT_BAD_CHART_RESPONSE"):
        _fetch(
            DeribitFuturesHistoricalFetcher(),
            symbol="BTC-PERPETUAL",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 1),
        )

    _rpc_script(monkeypatch, [RpcReply(_chart(HISTORY_TICK - 60_000), OBSERVED_AT)])
    with pytest.raises(DeribitProviderError, match="DERIBIT_BAD_TIME_BOUNDARY"):
        _fetch(
            DeribitFuturesHistoricalFetcher(),
            symbol="BTC-PERPETUAL",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 1),
        )


def test_futures_info_uses_live_ticker_and_maps_session_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rpc_script(monkeypatch, [RpcReply(_ticker("SOL_USDC-PERPETUAL"), OBSERVED_AT)])
    rows = _fetch(DeribitFuturesInfoFetcher(), symbol="SOLUSDC")

    assert calls == [
        {
            "method": "public/ticker",
            "params": {"instrument_name": "SOL_USDC-PERPETUAL"},
            "timeout": 5.0,
        }
    ]
    assert len(rows) == 1
    assert rows[0].symbol == "SOL_USDC-PERPETUAL"
    assert rows[0].timestamp == datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert rows[0].bid == 101200.0
    assert rows[0].ask_size == 30.0
    assert rows[0].volume_notional == 2_050_000.0
    assert rows[0].high == 103000.0
    assert rows[0].mark_price == 101250.0


def test_futures_info_rejects_mismatched_ticker_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rpc_script(monkeypatch, [RpcReply(_ticker("ETH-PERPETUAL"), OBSERVED_AT)])
    with pytest.raises(DeribitProviderError, match="DERIBIT_INCONSISTENT_TICKER"):
        _fetch(DeribitFuturesInfoFetcher(), symbol="BTC-PERPETUAL")


def test_futures_instruments_preserve_documented_fields_and_require_instrument_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rpc_script(
        monkeypatch,
        [RpcReply([_instrument("BTC-25DEC26")], OBSERVED_AT)],
    )
    rows = _fetch(DeribitFuturesInstrumentsFetcher(), currency="BTC")

    assert calls[0]["params"] == {
        "currency": "BTC",
        "kind": "future",
        "expired": False,
        "extended": False,
    }
    assert len(rows) == 1
    assert rows[0].symbol == "BTC-25DEC26"
    assert rows[0].instrument_id == 138583
    assert rows[0].future_type == "reversed"
    assert rows[0].contract_size == 10.0
    assert rows[0].tick_size_steps[0].above_price == 100000.0
    assert rows[0].creation_timestamp == datetime(2023, 11, 14, 22, 13, 20, tzinfo=timezone.utc)

    missing_id = _instrument("BTC-25DEC26")
    missing_id.pop("instrument_id")
    _rpc_script(monkeypatch, [RpcReply([missing_id], OBSERVED_AT)])
    with pytest.raises(DeribitProviderError, match="DERIBIT_BAD_RESPONSE_SHAPE"):
        _fetch(DeribitFuturesInstrumentsFetcher(), currency="BTC")


def test_future_snapshot_invalid_parameters_fail_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _rpc_script(monkeypatch, [])
    with pytest.raises(ValidationError):
        DeribitFuturesInstrumentsFetcher().fetch(currency="DOGE")
    with pytest.raises(ValidationError):
        DeribitFuturesInstrumentsFetcher().fetch(currency="BTC", expired="false")
    with pytest.raises(ValidationError):
        DeribitFuturesInfoFetcher().fetch(symbol="BTC-PERPETUAL", start_date=date(2026, 1, 1))
    with pytest.raises(ValidationError):
        DeribitFuturesCurveFetcher().fetch(symbol="BTC", hours_ago="24")
    with pytest.raises(ValidationError):
        DeribitOptionsChainsFetcher().fetch(symbol="BTC", market="us")
    assert calls == []


def test_futures_curve_joins_current_mark_to_dated_monthly_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    perpetual = _instrument(
        "BTC-PERPETUAL",
        instrument_id=124972,
        expiration=datetime(3000, 1, 1, tzinfo=timezone.utc),
        settlement_period="perpetual",
    )
    calls = _rpc_script(
        monkeypatch,
        [
            RpcReply(
                [_instrument("BTC-25DEC26", instrument_id=138583), perpetual],
                OBSERVED_AT,
            ),
            RpcReply(
                [_summary("BTC-25DEC26"), _summary("BTC-PERPETUAL")],
                OBSERVED_AT,
            ),
        ],
    )

    rows = _fetch(DeribitFuturesCurveFetcher(), symbol="BTC", market="global")

    assert [call["method"] for call in calls] == [
        "public/get_instruments",
        "public/get_book_summary_by_currency",
    ]
    assert len(rows) == 1
    assert rows[0].symbol == "BTC-25DEC26"
    assert rows[0].date == OBSERVED_AT.date()
    assert rows[0].expiration == "2026-12"
    assert rows[0].price == 101250.0
    assert rows[0].last_price == 101240.0
    assert rows[0].hours_ago is None


def test_futures_curve_rejects_unmatched_summary_and_unsupported_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _rpc_script(
        monkeypatch,
        [
            RpcReply([_instrument("BTC-25DEC26")], OBSERVED_AT),
            RpcReply([_summary("BTC-26DEC26")], OBSERVED_AT),
        ],
    )
    with pytest.raises(DeribitProviderError, match="DERIBIT_INCONSISTENT_INSTRUMENTS"):
        _fetch(DeribitFuturesCurveFetcher(), symbol="BTC")

    calls = _rpc_script(monkeypatch, [])
    with pytest.raises(ValidationError):
        DeribitFuturesCurveFetcher().fetch(symbol="BTC", hours_ago=24)
    assert calls == []


def test_options_chain_joins_contract_directory_and_summary_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = "BTC-30JUN27-100000-C"
    instrument = _instrument(
        contract,
        kind="option",
        instrument_id=901234,
        expiration=datetime(2027, 6, 30, tzinfo=timezone.utc),
        contract_size=1.0,
    )
    option_summary = _summary(contract, kind="option")
    calls = _rpc_script(
        monkeypatch,
        [
            RpcReply([instrument], OBSERVED_AT),
            RpcReply([option_summary], OBSERVED_AT),
            RpcReply(_option_ticker(contract), OBSERVED_AT),
        ],
    )

    rows = _fetch(DeribitOptionsChainsFetcher(), symbol="BTC")

    assert [call["method"] for call in calls] == [
        "public/get_instruments",
        "public/get_book_summary_by_currency",
        "public/ticker",
    ]
    assert calls[0]["params"] == {
        "currency": "BTC",
        "kind": "option",
        "expired": False,
        "extended": False,
    }
    assert len(rows) == 1
    row = rows[0]
    assert row.contract_symbol == contract
    assert row.eod_date is None
    assert row.expiration == date(2027, 6, 30)
    assert row.dte == (date(2027, 6, 30) - OBSERVED_AT.date()).days
    assert row.underlying_symbol == "index_price"
    assert row.contract_size == 1.0
    assert row.bid == 101200.0
    assert row.ask == 101300.0
    assert row.mark == 101250.0
    assert row.implied_volatility == 72.4
    assert row.delta == 0.4
    assert row.volume_notional == 2_050_000.0
    assert row.timestamp == datetime(2026, 10, 12, 4, 30, 0, 123000, tzinfo=timezone.utc)


def test_options_chain_filters_and_rejects_missing_or_mismatched_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = "BTC_USDC-30JUN27-0d625-P"
    instrument = _instrument(
        contract,
        kind="option",
        instrument_id=901235,
        expiration=datetime(2027, 6, 30, tzinfo=timezone.utc),
        base_currency="BTC",
        contract_size=0.0001,
        instrument_type="linear",
    )
    option_summary = _summary(contract, kind="option", quote_currency="USDC")
    _rpc_script(
        monkeypatch,
        [
            RpcReply([instrument], OBSERVED_AT),
            RpcReply([option_summary], OBSERVED_AT),
            RpcReply(_option_ticker(contract), OBSERVED_AT),
        ],
    )
    rows = _fetch(
        DeribitOptionsChainsFetcher(),
        symbol="BTC_USDC",
        expiration_date=date(2027, 6, 30),
        option_type="put",
    )
    assert rows[0].strike == 0.625
    assert rows[0].option_type == "put"
    assert rows[0].is_inverse is False

    _rpc_script(
        monkeypatch,
        [
            RpcReply([instrument], OBSERVED_AT),
            RpcReply([_summary("BTC-30JUN27-100000-C", kind="option")], OBSERVED_AT),
        ],
    )
    with pytest.raises(DeribitProviderError, match="DERIBIT_INCONSISTENT_INSTRUMENTS"):
        _fetch(DeribitOptionsChainsFetcher(), symbol="BTC")

    calls = _rpc_script(monkeypatch, [])
    with pytest.raises(ValidationError):
        DeribitOptionsChainsFetcher().fetch(symbol="BTCUSDC")
    assert calls == []


def test_options_chain_refuses_to_truncate_when_ticker_budget_would_be_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = [f"BTC-30JUN27-{100000 + index}-C" for index in range(31)]
    instruments = [
        _instrument(
            name,
            kind="option",
            instrument_id=900000 + index,
            expiration=datetime(2027, 6, 30, tzinfo=timezone.utc),
            contract_size=1.0,
        )
        for index, name in enumerate(names)
    ]
    summaries = [_summary(name, kind="option") for name in names]
    calls = _rpc_script(
        monkeypatch,
        [RpcReply(instruments, OBSERVED_AT), RpcReply(summaries, OBSERVED_AT)],
    )
    with pytest.raises(DeribitProviderError, match="DERIBIT_OPTION_CHAIN_LIMIT"):
        _fetch(DeribitOptionsChainsFetcher(), symbol="BTC")
    assert [call["method"] for call in calls] == [
        "public/get_instruments",
        "public/get_book_summary_by_currency",
    ]


def _domain_spec(model: str) -> object:
    """Construct a complete test-only domain contract for service validation."""
    from opendata.data.domains import DomainSpec

    entries: dict[str, dict[str, object]] = {
        "FuturesHistorical": {
            "temporal_kind": "series",
            "time_field": "date",
            "natural_key": ("symbol", "date"),
            "filter_dims": ("symbol", "date", "close", "volume_notional"),
            "storage_mode": "upsert",
            "permissions": ("query", "store", "export"),
        },
        "FuturesInfo": {
            "temporal_kind": "snapshot",
            "time_field": "timestamp",
            "natural_key": ("symbol", "timestamp"),
            "filter_dims": ("symbol", "timestamp", "bid", "ask"),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
        "FuturesInstruments": {
            "temporal_kind": "snapshot",
            "time_field": None,
            "natural_key": ("symbol", "instrument_id"),
            "filter_dims": ("symbol", "instrument_id", "base_currency", "state"),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
        "FuturesCurve": {
            "temporal_kind": "snapshot",
            "time_field": "date",
            "natural_key": ("symbol", "date", "expiration"),
            "filter_dims": ("symbol", "date", "expiration", "price"),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
        "OptionsChains": {
            "temporal_kind": "snapshot",
            "time_field": "timestamp",
            "natural_key": ("contract_symbol", "timestamp"),
            "filter_dims": (
                "contract_symbol",
                "underlying_symbol",
                "expiration",
                "strike",
                "option_type",
            ),
            "storage_mode": "transient",
            "permissions": ("query",),
        },
    }
    contracts: dict[str, type[Any]] = {
        "FuturesHistorical": DeribitFuturesHistorical,
        "FuturesInfo": DeribitFuturesInfo,
        "FuturesInstruments": DeribitFuturesInstruments,
        "FuturesCurve": DeribitFuturesCurve,
        "OptionsChains": DeribitOptionsChains,
    }
    entry = entries[model]
    return DomainSpec(
        display_name=f"Deribit {model}",
        rest_path=f"derivatives/deribit/{_snake(model).replace('_', '-')}",
        contract=contracts[model].__name__,
        **entry,
    )


def _snake(name: str) -> str:
    """Convert a model label to its snake-case domain suffix."""
    return "".join(
        ("_" if char.isupper() and index else "") + char.lower() for index, char in enumerate(name)
    )


@pytest.mark.parametrize(
    ("model", "fetcher", "query", "replies", "methods", "checks"),
    (
        (
            "FuturesHistorical",
            DeribitFuturesHistoricalFetcher(),
            {
                "symbol": "BTC-PERPETUAL",
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 1),
            },
            [RpcReply(_chart(), OBSERVED_AT)],
            ["public/get_tradingview_chart_data"],
            {"symbol": "BTC-PERPETUAL", "volume_notional": 525000.0},
        ),
        (
            "FuturesInfo",
            DeribitFuturesInfoFetcher(),
            {"symbol": "BTC-PERPETUAL"},
            [RpcReply(_ticker("BTC-PERPETUAL"), OBSERVED_AT)],
            ["public/ticker"],
            {"symbol": "BTC-PERPETUAL", "bid": 101200.0},
        ),
        (
            "FuturesInstruments",
            DeribitFuturesInstrumentsFetcher(),
            {"currency": "BTC"},
            [RpcReply([_instrument("BTC-25DEC26")], OBSERVED_AT)],
            ["public/get_instruments"],
            {"symbol": "BTC-25DEC26", "instrument_id": 138583},
        ),
        (
            "FuturesCurve",
            DeribitFuturesCurveFetcher(),
            {"symbol": "BTC"},
            [
                RpcReply([_instrument("BTC-25DEC26")], OBSERVED_AT),
                RpcReply([_summary("BTC-25DEC26")], OBSERVED_AT),
            ],
            ["public/get_instruments", "public/get_book_summary_by_currency"],
            {"symbol": "BTC-25DEC26", "expiration": "2026-12", "price": 101250.0},
        ),
        (
            "OptionsChains",
            DeribitOptionsChainsFetcher(),
            {"symbol": "BTC"},
            [
                RpcReply(
                    [
                        _instrument(
                            "BTC-30JUN27-100000-C",
                            kind="option",
                            instrument_id=901234,
                            expiration=datetime(2027, 6, 30, tzinfo=timezone.utc),
                            contract_size=1.0,
                        )
                    ],
                    OBSERVED_AT,
                ),
                RpcReply([_summary("BTC-30JUN27-100000-C", kind="option")], OBSERVED_AT),
                RpcReply(_option_ticker("BTC-30JUN27-100000-C"), OBSERVED_AT),
            ],
            [
                "public/get_instruments",
                "public/get_book_summary_by_currency",
                "public/ticker",
            ],
            {"contract_symbol": "BTC-30JUN27-100000-C", "bid": 101200.0},
        ),
    ),
)
def test_query_service_injects_identity_executes_and_serializes_contract_rows(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    fetcher: Fetcher[Any, Any],
    query: dict[str, object],
    replies: list[RpcReply],
    methods: list[str],
    checks: dict[str, object],
) -> None:
    from opendata.data import domains

    registry = ProviderRegistry()
    registry.register_provider_models("deribit", (model,), fetcher)
    spec = _domain_spec(model)
    contract = {
        "FuturesHistorical": DeribitFuturesHistorical,
        "FuturesInfo": DeribitFuturesInfo,
        "FuturesInstruments": DeribitFuturesInstruments,
        "FuturesCurve": DeribitFuturesCurve,
        "OptionsChains": DeribitOptionsChains,
    }[model]
    monkeypatch.setattr(domains, "require_domain_semantics", lambda _domain: spec)
    monkeypatch.setattr(domains, "contract_model", lambda _domain: contract)

    captured: dict[str, object] = {}
    original_transform = fetcher.transform_query

    def capture_query(**kwargs: object) -> object:
        captured.update(kwargs)
        return original_transform(**kwargs)

    monkeypatch.setattr(fetcher, "transform_query", capture_query)
    calls = _rpc_script(monkeypatch, replies)
    response = asyncio.run(
        query_provider_model(
            registry=registry,
            source="deribit",
            model=model,
            query=query,
            principal=SimpleNamespace(
                allows_domain=lambda domain: domain == fetcher.capability.domain
            ),
            ctx=_authorized_context(model),
        )
    )

    assert captured["source"] == "deribit"
    assert captured["market"] == "global"
    assert [call["method"] for call in calls] == methods
    assert all("market" not in call["params"] and "source" not in call["params"] for call in calls)
    assert response["source"] == "deribit"
    assert response["model"] == model
    assert response["domain"] == fetcher.capability.domain
    rows = response["results"]
    assert isinstance(rows, list) and len(rows) == 1
    assert set(rows[0]) == set(contract.model_fields)
    assert all(rows[0][key] == value for key, value in checks.items())


def test_shared_transport_allows_ticker_and_classifies_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_record: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        content = b'{"jsonrpc":"2.0"}'

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "jsonrpc": "2.0",
                "id": 1,
                "result": _ticker("BTC-PERPETUAL"),
                "usOut": 1_797_067_800_123_456,
            }

    class FakeSharedClient:
        def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            request_record.update({"method": method, "url": url, **kwargs})
            return FakeResponse()

    monkeypatch.setattr(client, "get_shared_http_client", lambda: FakeSharedClient())
    monkeypatch.setattr(client, "_REQUEST_IDS", count(1))
    reply = client.rpc_call("public/ticker", {"instrument_name": "BTC-PERPETUAL"}, timeout=4.0)
    assert request_record["method"] == "POST"
    assert request_record["url"] == "https://www.deribit.com/api/v2/public/ticker"
    assert request_record["source"] == "deribit"
    assert request_record["timeout"] == 4.0
    assert request_record["json_body"] == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "public/ticker",
        "params": {"instrument_name": "BTC-PERPETUAL"},
    }
    assert isinstance(reply.result, dict)

    class RateLimitedResponse:
        status_code = 429
        content = b"rate limited"

    class RateLimitedClient:
        def request(self, _method: str, _url: str, **_kwargs: object) -> RateLimitedResponse:
            return RateLimitedResponse()

    monkeypatch.setattr(client, "get_shared_http_client", lambda: RateLimitedClient())
    with pytest.raises(DeribitProviderError) as rate_error:
        client.rpc_call("public/ticker", {"instrument_name": "BTC-PERPETUAL"})
    assert rate_error.value.code == "DERIBIT_RATE_LIMITED"
    assert rate_error.value.status == 429


def test_transport_applies_instrument_pacing_only_to_directory_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter_calls: list[str] = []
    request_methods: list[str] = []
    monkeypatch.setattr(
        client, "_respect_instrument_rate_limit", lambda: limiter_calls.append("hit")
    )

    class FakeResponse:
        status_code = 200
        content = b"{}"

        def __init__(self, document: dict[str, object]) -> None:
            self._document = document

        def json(self) -> dict[str, object]:
            return self._document

    class FakeSharedClient:
        def request(self, _method: str, _url: str, **kwargs: object) -> FakeResponse:
            payload = kwargs["json_body"]
            assert isinstance(payload, dict)
            request_methods.append(payload["method"])
            return FakeResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": [],
                    "usOut": 1_797_067_800_123_456,
                }
            )

    monkeypatch.setattr(client, "get_shared_http_client", lambda: FakeSharedClient())
    monkeypatch.setattr(client, "_REQUEST_IDS", count(1))
    client.rpc_call("public/get_instruments", {"currency": "BTC", "kind": "future"})
    client.rpc_call("public/ticker", {"instrument_name": "BTC-PERPETUAL"})
    client.rpc_call("public/get_book_summary_by_currency", {"currency": "BTC", "kind": "option"})
    assert request_methods == [
        "public/get_instruments",
        "public/ticker",
        "public/get_book_summary_by_currency",
    ]
    assert limiter_calls == ["hit"]


def test_transport_rejects_bad_rpc_envelopes_and_unlisted_methods() -> None:
    with pytest.raises(DeribitProviderError, match="DERIBIT_BAD_ENVELOPE"):
        client._parse_reply(
            {"jsonrpc": "2.0", "id": 2, "result": [], "usOut": 1_797_067_800_123_456},
            request_id=1,
            status=200,
        )
    with pytest.raises(DeribitProviderError, match="DERIBIT_METHOD_NOT_ALLOWED"):
        client.rpc_call("public/private_method", {})
