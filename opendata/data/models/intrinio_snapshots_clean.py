"""Typed Intrinio snapshot/performance projections with explicit source semantics."""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictStr

from opendata.data.models.base import ContractModel


def _finite_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a JSON number")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValueError("expected a finite JSON number")
    return value


FiniteFloat = Annotated[float, BeforeValidator(_finite_number)]
FiniteCount = Annotated[int | float, BeforeValidator(_finite_number), Field(ge=0)]


class IntrinioEtfPricePerformance(ContractModel):
    """ETF performance computed from documented daily security prices.

    ``*_return`` fields use decimal fractions computed as ``end / start - 1``. OpenBB V4's
    standardization guidance specifies decimal points for percent values; Intrinio ETF-statistics
    values are not copied because their source units and some annualization semantics differ.
    """

    symbol: StrictStr
    one_day: FiniteFloat | None = None
    wtd: FiniteFloat | None = None
    one_week: FiniteFloat | None = None
    mtd: FiniteFloat | None = None
    one_month: FiniteFloat | None = None
    qtd: FiniteFloat | None = None
    three_month: FiniteFloat | None = None
    six_month: FiniteFloat | None = None
    ytd: FiniteFloat | None = None
    one_year: FiniteFloat | None = None
    two_year: FiniteFloat | None = None
    three_year: FiniteFloat | None = None
    four_year: FiniteFloat | None = None
    five_year: FiniteFloat | None = None
    ten_year: FiniteFloat | None = None
    max: FiniteFloat | None = None
    price: FiniteFloat = Field(gt=0)
    volume: FiniteCount | None = None
    updated: date
    return_type: Literal["trailing", "calendar"]
    adjustment: Literal["splits_and_dividends"]
    calculation_scale: Literal["end_price_over_start_price_minus_one"]
    calculation_method: Literal["adjusted_close_price_ratio_fraction_v1"]


class IntrinioMarketSnapshot(ContractModel):
    """One current Nasdaq primary-listing quote joined by Intrinio security ID."""

    exchange: StrictStr
    symbol: StrictStr
    name: StrictStr | None = None
    open: FiniteFloat | None = Field(default=None, gt=0)
    high: FiniteFloat | None = Field(default=None, gt=0)
    low: FiniteFloat | None = Field(default=None, gt=0)
    close: FiniteFloat | None = Field(default=None, gt=0)
    volume: FiniteCount | None = None
    prev_close: FiniteFloat | None = Field(default=None, gt=0)
    change: FiniteFloat | None = None
    change_percent: FiniteFloat | None = None
    last_price: FiniteFloat | None = Field(default=None, gt=0)
    last_size: FiniteCount | None = None
    last_trade_timestamp: datetime | None = None
    bid_price: FiniteFloat | None = Field(default=None, gt=0)
    bid_size: FiniteCount | None = None
    last_bid_timestamp: datetime | None = None
    ask_price: FiniteFloat | None = Field(default=None, gt=0)
    ask_size: FiniteCount | None = None
    last_ask_timestamp: datetime | None = None
    source_updated_on: StrictStr | None = None
    source_last_time: StrictStr | None = None
    source_bid_time: StrictStr | None = None
    source_ask_time: StrictStr | None = None
    source_market_volume: FiniteCount | None = None
    source_exchange_volume: FiniteCount | None = None
    source_listing_exchange_mic: Literal["XNAS"] = "XNAS"
    source_price_feed: Literal["delayed_sip"] = "delayed_sip"


SNAPSHOT_GAPS: dict[str, str] = {
    "MarketSnapshots.date": (
        "Intrinio's public security snapshot endpoint documents only expiring file URLs and "
        "part metadata; its downloadable row schema is not documented. Historical date queries "
        "are therefore unsupported."
    ),
    "OptionsSnapshots": (
        "Intrinio's public options snapshot endpoint documents snapshot timestamps, expiring "
        "file URLs, part numbers, and byte sizes, but no downloadable row schema/auth contract. "
        "No option rows are emitted from metadata."
    ),
    "EtfPricePerformance.provider_statistics": (
        "Intrinio ETF statistics return fields are documented as numeric source values without "
        "a unit basis, and some multi-year trailing fields are explicitly annualized. They are "
        "not copied into this projection. OpenBB V4 standardization requires decimal-point "
        "representation for percent values; the adapter computes returns as end/start - 1."
    ),
    "EtfPricePerformance.calendar_semantics": (
        "OpenBB V4 describes return_type only as a trailing or calendar window. Intrinio's "
        "separate ETF stats endpoint defines calendar_year_N as the N-years-ago calendar-year "
        "return; this supports the candidate mapping to distinct completed years, but exact V4 "
        "field parity remains unverified."
    ),
    "EtfPricePerformance.splits_only": (
        "Intrinio defines split_ratio only as the split ratio (and a legacy CSV describes it as "
        "the factor by which shares were divided); neither document states new/old versus "
        "old/new orientation or the exact price multiplier. The adapter rejects splits_only "
        "before I/O."
    ),
}
