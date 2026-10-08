"""CBOE index catalog and constituent-quote contracts.

These are provider-published shapes that no existing standard contract describes: the directory
carries calculation sessions and tick metadata, and the constituent rows are intraday quotes
rather than the ``IndexConstituent`` weight record. Each therefore gets its own contract family
and its own domain, the way ``SeriesCatalogItem`` and ``BlsCatalogItem`` do, and the declarative
declaration in ``opendata/data/providers/cboe/specs.py`` must name exactly these fields.
"""

from __future__ import annotations

from opendata.data.models.base import ContractModel


class CboeAvailableIndex(ContractModel):
    """One index in cboe's published US index directory."""

    symbol: str | None
    name: str | None
    exchange: str | None = None
    currency: str | None = None
    description: str | None = None
    data_delay: str | None = None
    open_time: str | None = None
    close_time: str | None = None
    time_zone: str | None = None
    tick_days: str | None = None
    tick_frequency: str | None = None
    tick_period: str | None = None


class CboeIndexConstituentQuote(ContractModel):
    """One constituent of a cboe European index with its intraday quote."""

    symbol: str | None
    name: str | None = None
    currency: str | None = None
    security_type: str | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    tick: str | None = None
    last_trade_time: str | None = None
    asset_type: str | None = None
