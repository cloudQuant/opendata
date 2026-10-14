"""Typed Intrinio market rows backed by documented v2 JSON responses.

The contracts contain the OpenBB public row fields that this source can map
without deriving values from undocumented units or following opaque snapshot
download links.  Models deliberately reject unknown output keys.
"""

from __future__ import annotations

import datetime as _datetime
import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, StrictBool, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


def _finite_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a JSON number")
    if not math.isfinite(float(value)):
        raise ValueError("expected a finite JSON number")
    return value


FiniteFloat = Annotated[float, BeforeValidator(_finite_number)]
FiniteCount = Annotated[int | float, BeforeValidator(_finite_number)]


class IntrinioEquityInfo(ContractModel):
    """Typed company profile fields returned by Intrinio."""

    symbol: StrictStr
    name: StrictStr | None = None
    cik: StrictStr | None = None
    lei: StrictStr | None = None
    legal_name: StrictStr | None = None
    stock_exchange: StrictStr | None = None
    sic: StrictInt | None = None
    short_description: StrictStr | None = None
    long_description: StrictStr | None = None
    ceo: StrictStr | None = None
    company_url: StrictStr | None = None
    business_address: StrictStr | None = None
    mailing_address: StrictStr | None = None
    business_phone_no: StrictStr | None = None
    hq_address1: StrictStr | None = None
    hq_address2: StrictStr | None = None
    hq_address_city: StrictStr | None = None
    hq_address_postal_code: StrictStr | None = None
    hq_state: StrictStr | None = None
    hq_country: StrictStr | None = None
    inc_state: StrictStr | None = None
    inc_country: StrictStr | None = None
    employees: StrictInt | None = None
    entity_legal_form: StrictStr | None = None
    entity_status: StrictStr | None = None
    latest_filing_date: _datetime.date | None = None
    sector: StrictStr | None = None
    industry_category: StrictStr | None = None
    industry_group: StrictStr | None = None
    template: StrictStr | None = None
    standardized_active: StrictBool | None = None
    first_fundamental_date: _datetime.date | None = None
    last_fundamental_date: _datetime.date | None = None
    first_stock_price_date: _datetime.date | None = None
    last_stock_price_date: _datetime.date | None = None


class IntrinioEquityHistorical(ContractModel):
    """Typed daily OHLCV row for an equity security."""

    symbol: StrictStr
    date: _datetime.date
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    volume: FiniteCount | None = None
    vwap: FiniteFloat | None = None


class IntrinioEtfHistorical(IntrinioEquityHistorical):
    """Same documented security-price rows, queried for an ETF symbol."""


class IntrinioEtfInfo(ContractModel):
    """Typed profile fields for an exchange-traded fund."""

    symbol: StrictStr
    name: StrictStr | None = None
    issuer: StrictStr | None = None
    domicile: StrictStr | None = None
    description: StrictStr | None = None
    inception_date: _datetime.date | None = None
    fund_listing_date: _datetime.date | None = None
    data_change_date: _datetime.date | None = None
    etn_maturity_date: _datetime.date | None = None
    is_listed: StrictBool | None = None
    close_date: _datetime.date | None = None
    exchange: StrictStr | None = None
    isin: StrictStr | None = None
    ric: StrictStr | None = None
    sedol: StrictStr | None = None


class IntrinioHistoricalMarketCap(ContractModel):
    """Typed historical market-cap observation."""

    symbol: StrictStr
    date: _datetime.date
    market_cap: FiniteFloat


class IntrinioIndexHistorical(ContractModel):
    """Typed daily historical index level row."""

    symbol: StrictStr
    date: _datetime.date
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    volume: FiniteCount | None = None


class IntrinioSecurity(ContractModel):
    """Typed security metadata nested in an Intrinio quote."""

    id: StrictStr | None = None
    company_id: StrictStr | None = None
    exchange: StrictStr | None = None
    exchange_mic: StrictStr | None = None
    stock_exchange_id: StrictStr | None = None
    name: StrictStr | None = None
    code: StrictStr | None = None
    currency: StrictStr | None = None
    ticker: StrictStr | None = None
    composite_ticker: StrictStr | None = None
    figi: StrictStr | None = None
    composite_figi: StrictStr | None = None
    share_class_figi: StrictStr | None = None
    primary_listing: StrictBool | None = None


class IntrinioEquityQuote(ContractModel):
    """Typed latest equity quote with offset-aware update time."""

    symbol: StrictStr
    asset_type: StrictStr | None = None
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    bid: FiniteFloat | None = None
    bid_size: FiniteCount | None = None
    ask: FiniteFloat | None = None
    ask_size: FiniteCount | None = None
    last_price: FiniteFloat | None = None
    updated_on: datetime
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    volume: FiniteCount | None = None
    exchange_volume: FiniteCount | None = None
    prev_close: FiniteFloat | None = None
    change: FiniteFloat | None = None
    change_percent: FiniteFloat | None = None
    year_high: FiniteFloat | None = None
    year_low: FiniteFloat | None = None
    source: StrictStr | None = None
    security: IntrinioSecurity | None = None


class IntrinioEtfHolding(ContractModel):
    """Typed constituent row from an ETF holdings response."""

    symbol: StrictStr | None = None
    name: StrictStr | None = None
    security_type: StrictStr | None = None
    isin: StrictStr | None = None
    ric: StrictStr | None = None
    sedol: StrictStr | None = None
    share_class_figi: StrictStr | None = None
    maturity_date: _datetime.date | None = None
    contract_expiry_date: _datetime.date | None = None
    coupon: FiniteFloat | None = None
    balance: FiniteCount | None = None
    unit: StrictStr | None = None
    units_per_share: FiniteFloat | None = None
    face_value: FiniteFloat | None = None
    derivatives_value: FiniteFloat | None = None
    value: FiniteFloat | None = None
    weight: FiniteFloat | None = None
    updated: _datetime.date | None = None


class IntrinioEtfSearch(ContractModel):
    """Typed ETF search result."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    figi_ticker: StrictStr | None = None
    ric: StrictStr | None = None
    isin: StrictStr | None = None
    sedol: StrictStr | None = None
    intrinio_id: StrictStr | None = None


class IntrinioOptionsUnusual(ContractModel):
    """Typed unusual-options trade row."""

    underlying_symbol: StrictStr | None = None
    contract_symbol: StrictStr
    trade_timestamp: datetime
    trade_type: Literal["block", "sweep", "large"]
    sentiment: Literal["bullish", "bearish", "neutral"]
    bid_at_execution: FiniteFloat
    ask_at_execution: FiniteFloat
    average_price: FiniteFloat
    underlying_price_at_execution: FiniteFloat | None = None
    total_size: StrictInt
    total_value: FiniteCount


class IntrinioOptionsChain(ContractModel):
    """Intrinio real-time-by-ticker row in the public OptionsChains shape."""

    underlying_symbol: StrictStr | None = None
    underlying_price: FiniteFloat | None = None
    contract_symbol: StrictStr
    eod_date: _datetime.date | None = None
    expiration: _datetime.date
    dte: StrictInt | None = None
    strike: FiniteFloat
    option_type: Literal["call", "put"]
    contract_size: FiniteCount | None = None
    open_interest: FiniteCount | None = None
    volume: FiniteCount | None = None
    theoretical_price: FiniteFloat | None = None
    last_trade_price: FiniteFloat | None = None
    last_trade_size: FiniteCount | None = None
    last_trade_time: datetime | None = None
    tick: StrictStr | None = None
    bid: FiniteFloat | None = None
    bid_size: FiniteCount | None = None
    bid_time: datetime | None = None
    bid_exchange: StrictStr | None = None
    ask: FiniteFloat | None = None
    ask_size: FiniteCount | None = None
    ask_time: datetime | None = None
    ask_exchange: StrictStr | None = None
    mark: FiniteFloat | None = None
    open: FiniteFloat | None = None
    open_bid: FiniteFloat | None = None
    open_ask: FiniteFloat | None = None
    high: FiniteFloat | None = None
    bid_high: FiniteFloat | None = None
    ask_high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    bid_low: FiniteFloat | None = None
    ask_low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    close_size: FiniteCount | None = None
    close_time: datetime | None = None
    close_bid: FiniteFloat | None = None
    close_bid_size: FiniteCount | None = None
    close_bid_time: datetime | None = None
    close_ask: FiniteFloat | None = None
    close_ask_size: FiniteCount | None = None
    close_ask_time: datetime | None = None
    prev_close: FiniteFloat | None = None
    change: FiniteFloat | None = None
    change_percent: FiniteFloat | None = None
    implied_volatility: FiniteFloat | None = None
    delta: FiniteFloat | None = None
    gamma: FiniteFloat | None = None
    theta: FiniteFloat | None = None
    vega: FiniteFloat | None = None
    rho: FiniteFloat | None = None


class IntrinioPriceTargetConsensus(ContractModel):
    """Typed analyst price-target consensus row."""

    symbol: StrictStr
    name: StrictStr | None = None
    target_high: FiniteFloat | None = None
    target_low: FiniteFloat | None = None
    target_consensus: FiniteFloat | None = None
    target_median: FiniteFloat | None = None
    standard_deviation: FiniteFloat | None = None
    total_analysts: StrictInt | None = None
    most_recent_date: _datetime.date | None = None
    raised: StrictInt | None = None
    lowered: StrictInt | None = None
    industry_group_number: StrictInt | None = None


class IntrinioShareStatistics(ContractModel):
    """Typed shares-outstanding and ownership statistics row."""

    symbol: StrictStr
    date: _datetime.date | None = None
    free_float: FiniteFloat | None = None
    float_shares: FiniteCount | None = None
    outstanding_shares: FiniteCount | None = None
    adjusted_outstanding_shares: FiniteFloat | None = None
    public_float: FiniteFloat | None = None
    implied_shares_outstanding: StrictInt | None = None
    short_interest: StrictInt | None = None
    short_percent_of_float: FiniteFloat | None = None
    days_to_cover: FiniteFloat | None = None
    short_interest_prev_month: StrictInt | None = None
    short_interest_prev_date: _datetime.date | None = None
    insider_ownership: FiniteFloat | None = None
    institution_ownership: FiniteFloat | None = None
    institution_float_ownership: FiniteFloat | None = None
    institutions_count: StrictInt | None = None


__all__ = [
    "IntrinioEquityHistorical",
    "IntrinioEquityInfo",
    "IntrinioEquityQuote",
    "IntrinioEtfHistorical",
    "IntrinioEtfHolding",
    "IntrinioEtfInfo",
    "IntrinioEtfSearch",
    "IntrinioHistoricalMarketCap",
    "IntrinioIndexHistorical",
    "IntrinioOptionsUnusual",
    "IntrinioOptionsChain",
    "IntrinioPriceTargetConsensus",
    "IntrinioSecurity",
    "IntrinioShareStatistics",
]
