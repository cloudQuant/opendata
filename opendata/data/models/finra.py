"""Typed FINRA output contracts for public OTC datasets."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class OTCAggregate(ContractModel):
    """One weekly OTC/ATS aggregate row."""

    update_date: date
    symbol: str
    share_quantity: float
    trade_quantity: float


class EquityShortInterest(ContractModel):
    """One consolidated OTC short-interest settlement row."""

    settlement_date: date
    symbol: str
    issue_name: str
    market_class: str
    current_short_position: float
    previous_short_position: float
    avg_daily_volume: float
    days_to_cover: float
    change: float
    change_pct: float
