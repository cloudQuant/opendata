"""Typed Cboe daily index-history rows for the public volatility-index CSVs."""

from __future__ import annotations

from datetime import date

from pydantic import StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class CboeIndexHistorical(ContractModel):
    """One Cboe daily index row; source files may provide close only or OHLC."""

    symbol: StrictStr | None
    date: date
    open: StrictFloat | None
    high: StrictFloat | None
    low: StrictFloat | None
    close: StrictFloat | None
    volume: StrictInt | StrictFloat | None
    calls_volume: StrictFloat | None = None
    puts_volume: StrictFloat | None = None
    total_options_volume: StrictFloat | None = None
