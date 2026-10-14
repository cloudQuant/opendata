"""Typed Deribit rows aligned with the public OpenBB Deribit output schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from opendata.data.models.base import ContractModel


class DeribitFuturesHistorical(ContractModel):
    """One Deribit OHLCV candle using the public historical output names."""

    date: datetime
    open: float | None
    high: float | None
    low: float | None
    close: float
    volume: float | None
    symbol: str | None
    volume_notional: float | None


class DeribitFuturesInfo(ContractModel):
    """Live quote and 24-hour session statistics for one future."""

    symbol: str
    timestamp: datetime | None
    state: str | None
    combo_state: str | None
    last_price: float | None
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None
    implied_bid: float | None
    implied_ask: float | None
    mark_price: float | None
    index_price: float | None
    settlement_price: float | None
    delivery_price: float | None
    estimated_delivery_price: float | None
    min_price: float | None
    max_price: float | None
    high: float | None
    low: float | None
    change_percent: float | None
    volume: float | None
    volume_notional: float | None
    volume_usd: float | None
    open_interest: float | None
    current_funding: float | None
    funding_8h: float | None
    interest_value: float | None
    interest_rate: float | None
    underlying_index: str | None
    underlying_price: float | None
    implied_volatility: float | None
    bid_iv: float | None
    ask_iv: float | None
    delta: float | None
    gamma: float | None
    vega: float | None
    theta: float | None
    rho: float | None


class DeribitTickSizeStep(ContractModel):
    """Price threshold and increment published in instrument metadata."""

    above_price: float
    tick_size: float


class DeribitFuturesInstruments(ContractModel):
    """One current or recently delivered future instrument definition."""

    symbol: str
    instrument_id: int
    kind: str | None
    instrument_type: str | None
    future_type: str | None
    state: str | None
    base_currency: str | None
    quote_currency: str | None
    counter_currency: str | None
    settlement_currency: str | None
    settlement_period: str | None
    underlying_type: str | None
    product_group: str | None
    price_index: str | None
    lot_size: int | None
    min_trade_amount: float | None
    tick_size: float | None
    max_leverage: int | None
    max_non_default_leverage: float | None
    max_liquidation_commission: float | None
    maker_commission: float | None
    taker_commission: float | None
    block_trade_commission: float | None
    block_trade_min_trade_amount: float | None
    block_trade_tick_size: float | None
    is_active: bool | None
    contract_size: float | None
    index_id: int | None
    tick_size_steps: list[DeribitTickSizeStep] | None
    base_currency_uuid: str | None
    quote_currency_uuid: str | None
    creation_timestamp: datetime | None
    expiration_timestamp: datetime | None


class DeribitFuturesCurve(ContractModel):
    """One dated future's mark-price observation in the current curve."""

    date: date | None
    expiration: str
    price: float | None
    symbol: str | None
    last_price: float | None
    hours_ago: int | None


class DeribitOptionsChains(ContractModel):
    """One option-chain contract with Deribit's current quote and risk fields."""

    underlying_symbol: str | None
    underlying_price: float | None
    contract_symbol: str
    eod_date: date | None
    expiration: date
    dte: int | None
    strike: float
    option_type: Literal["call", "put"]
    contract_size: float | int | None
    open_interest: float | int | None
    volume: float | int | None
    theoretical_price: float | None
    last_trade_price: float | None
    last_trade_size: float | int | None
    last_trade_time: datetime | None
    tick: str | None
    bid: float | None
    bid_size: float | int | None
    bid_time: datetime | None
    bid_exchange: str | None
    ask: float | None
    ask_size: float | int | None
    ask_time: datetime | None
    ask_exchange: str | None
    mark: float | None
    open: float | None
    open_bid: float | None
    open_ask: float | None
    high: float | None
    bid_high: float | None
    ask_high: float | None
    low: float | None
    bid_low: float | None
    ask_low: float | None
    close: float | None
    close_size: float | int | None
    close_time: datetime | None
    close_bid: float | None
    close_bid_size: float | int | None
    close_bid_time: datetime | None
    close_ask: float | None
    close_ask_size: float | int | None
    close_ask_time: datetime | None
    prev_close: float | None
    change: float | None
    change_percent: float | None
    implied_volatility: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None
    volume_notional: float | None
    bid_iv: float | None
    ask_iv: float | None
    interest_rate: float | None
    settlement_price: float | None
    min_price: float | None
    max_price: float | None
    underlying_spot_price: float | None
    is_inverse: bool | None
    timestamp: datetime
