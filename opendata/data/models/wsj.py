"""WSJ ETF-mover contracts: the three published mover tables share one row shape."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class WSJETFPerformance(ContractModel):
    """One ETF row from the WSJ Market Data mover tables."""

    symbol: str
    name: str
    last_price: float
    percent_change: float
    net_change: float
    volume: float
    date: date
    country: str
    mantissa: int
    type: str
    formatted_price: str
    formatted_volume: str
    formatted_price_change: str
    formatted_percent_change: str
    url: str


__all__ = ["WSJETFPerformance"]
