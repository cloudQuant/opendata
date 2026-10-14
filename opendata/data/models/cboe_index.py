"""CBOE index catalog and constituent-quote contracts.

These are provider-published shapes that no existing standard contract describes: the directory
carries calculation sessions and tick metadata, and the constituent rows are intraday quotes
rather than the ``IndexConstituent`` weight record. Each therefore gets its own contract family
and its own domain, the way ``SeriesCatalogItem`` and ``BlsCatalogItem`` do, and the declarative
declaration in ``opendata/data/providers/cboe/specs.py`` must name exactly these fields.

The search model publishes 11 of the directory's 12 columns, omitting ``exchange``, so it cannot
share ``CboeAvailableIndex`` and gets its own contract class.
"""

from __future__ import annotations

import re
from datetime import datetime, time

from pydantic import field_validator

from opendata.data.models.base import ContractModel

_DELAY_MINUTES = re.compile(r"\s*(\d+)(?:\s+min)?\s*\Z", re.IGNORECASE)


class _CboeDirectoryIndex(ContractModel):
    """Shared published fields and source-value parsing for Cboe's index directory."""

    symbol: str
    name: str | None
    currency: str | None = None
    description: str | None = None
    data_delay: int | None = None
    open_time: time | None = None
    close_time: time | None = None
    time_zone: str | None = None
    tick_days: str | None = None
    tick_frequency: str | None = None
    tick_period: str | None = None

    @field_validator("data_delay", mode="before")
    @classmethod
    def _parse_data_delay(cls, value: object) -> object:
        """Parse a minute count while retaining an explicit zero and null."""
        if value is None:
            return value
        if isinstance(value, int) and not isinstance(value, bool):
            if value < 0:
                raise ValueError("data_delay cannot be negative")
            return value
        if not isinstance(value, str):
            raise ValueError("data_delay must be an integer or a minute-count string")
        match = _DELAY_MINUTES.fullmatch(value)
        if match is None:
            raise ValueError("data_delay must be an integer or the count followed by 'min'")
        return int(match.group(1))

    @field_validator("open_time", "close_time", mode="before")
    @classmethod
    def _parse_index_clock(cls, value: object) -> object:
        """Parse a local wall-clock value; the row's timezone remains a separate field."""
        if value is None:
            return value
        if isinstance(value, time):
            if value.tzinfo is not None:
                raise ValueError("index session time must not carry a timezone offset")
            return value
        if not isinstance(value, str):
            raise ValueError("index session time must be an ISO local time string")
        parsed = time.fromisoformat(value)
        if parsed.tzinfo is not None:
            raise ValueError("index session time must not carry a timezone offset")
        return parsed


class CboeAvailableIndex(_CboeDirectoryIndex):
    """One index in cboe's published US index directory."""

    exchange: str | None = None


class CboeSearchedIndex(_CboeDirectoryIndex):
    """One index in cboe's published US index directory, as its search model publishes it."""

    name: str


class CboeIndexConstituentQuote(ContractModel):
    """One constituent of a cboe European index with its intraday quote."""

    symbol: str
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
    last_trade_time: datetime | None = None
    asset_type: str | None = None

    @field_validator("last_trade_time", mode="before")
    @classmethod
    def _parse_last_trade_time(cls, value: object) -> object:
        """Parse an ISO timestamp and preserve an explicit null."""
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError("last_trade_time must be an ISO datetime string")
        return datetime.fromisoformat(value)


class CboeEquitySearchListing(ContractModel):
    """One company in cboe's US options directory, as its CSV download publishes it."""

    symbol: str | None
    name: str | None = None
    dpm_name: str | None = None
    post_station: str | None = None
