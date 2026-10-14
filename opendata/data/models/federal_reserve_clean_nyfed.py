"""Strict Federal Reserve model rows backed by New York Fed Markets APIs."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class SOFR(ContractModel):
    """SOFR rates and percentiles as fractions, with volume in billions USD."""

    date: date
    rate: float
    percentile_1: float | None = None
    percentile_25: float | None = None
    percentile_75: float | None = None
    percentile_99: float | None = None
    volume: float | None = None
    average_30d: float | None = None
    average_90d: float | None = None
    average_180d: float | None = None
    index: float | None = None


class PrimaryDealerFails(ContractModel):
    """One published dealer-financing fails series observation."""

    date: date
    symbol: str
    title: str
    value: int | float


class PrimaryDealerPositioning(ContractModel):
    """One published net-position time-series observation."""

    date: date
    symbol: str
    value: int
    name: str
    title: str


__all__ = ["PrimaryDealerFails", "PrimaryDealerPositioning", "SOFR"]
