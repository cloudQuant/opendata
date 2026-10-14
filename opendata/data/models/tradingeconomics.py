"""Typed Economic Calendar contract for Trading Economics rows."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from opendata.data.models.base import ContractModel


class TradingEconomicsEconomicCalendar(ContractModel):
    """One Trading Economics economic calendar event."""

    date: dt.datetime
    country: str | None = None
    category: str | None = None
    event: str | None = None
    importance: str | None = None
    source: str | None = None
    currency: str | None = None
    unit: str | None = None
    consensus: str | float | None = None
    previous: str | float | None = None
    revised: str | float | None = None
    actual: str | float | None = None
    forecast: str | float | None = None
    reference: str | None = None
    reference_date: dt.date | None = None
    calendar_id: int
    date_span: Literal[0, 1] | None = None
    symbol: str | None = None
    ticker: str | None = None
    te_url: str | None = None
    source_url: str | None = None
    last_updated: dt.datetime | None = None
