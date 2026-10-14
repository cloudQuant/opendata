"""ODP v4 price-performance contracts backed by Finviz's public quote page."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import field_validator

from opendata.data.models.base import ContractModel

PartialPerformanceField = Literal[
    "one_day",
    "one_week",
    "one_month",
    "three_month",
    "six_month",
    "ytd",
    "one_year",
    "three_year",
    "five_year",
    "ten_year",
]


class FinvizPerformanceProvenance(ContractModel):
    """Source evidence retained alongside one normalized performance row."""

    requested_symbol: str
    returned_symbol: str
    source_url: str
    retrieved_at_utc: datetime
    quote_timestamp_raw: str | None = None
    quote_delay_notice: Literal["Stock quotes delayed by 1 minute."]
    source_labels: dict[PartialPerformanceField, str]
    raw_source_values: dict[str, str]
    return_basis: Literal["price-only; Finviz Perf excludes dividends"]
    dividends_included: Literal[False] = False

    @field_validator("retrieved_at_utc")
    @classmethod
    def _require_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("retrieved_at_utc must be timezone-aware UTC")
        return value.astimezone(timezone.utc)


class _FinvizPerformanceFields(ContractModel):
    """Complete nullable public ODP v4 PricePerformance data schema."""

    symbol: str | None = None
    one_day: float | None = None
    wtd: float | None = None
    one_week: float | None = None
    mtd: float | None = None
    one_month: float | None = None
    qtd: float | None = None
    three_month: float | None = None
    six_month: float | None = None
    ytd: float | None = None
    one_year: float | None = None
    two_year: float | None = None
    three_year: float | None = None
    four_year: float | None = None
    five_year: float | None = None
    ten_year: float | None = None
    max: float | None = None
    volatility_week: float | None = None
    volatility_month: float | None = None
    price: float | None = None
    volume: float | None = None
    average_volume: float | None = None
    relative_volume: float | None = None
    analyst_recommendation: float | None = None
    provenance: FinvizPerformanceProvenance
    partial_supported_fields: tuple[PartialPerformanceField, ...]


class FinvizPricePerformance(_FinvizPerformanceFields):
    """One equity's Finviz-mapped ODP v4 PricePerformance row."""


class FinvizEtfPricePerformance(_FinvizPerformanceFields):
    """One verified ETF's Finviz-mapped ODP v4 EtfPricePerformance row."""
