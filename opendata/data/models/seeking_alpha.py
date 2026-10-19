"""Typed Seeking Alpha earnings-calendar rows for the public JSON:API calendar route.

The column list is the declared output contract of the reference checkout's
``openbb_seeking_alpha/models/calendar_earnings.py`` (``SACalendarEarningsData``, 4 own annotations
over the 5-field ``CalendarEarningsData`` base), kept under the contract's own names. The route is
``https://seekingalpha.com/api/v3/earnings_calendar/tickers`` (captured unauthenticated on
2026-10-19 at ``docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/sources/`` with
``HTTP/2 200``; page 1 holds 50 records and page 2 holds 6 for the same date, and
``50 + 6 == meta.total_count == 56``).

Two declared columns have no cell in that envelope -- ``eps_previous`` and ``eps_consensus`` --
because the calendar route publishes only the schedule row for a ticker, not consensus. They are
declared nullable here and the fetcher never fills them, so a later implementer cannot land a value
the transport does not return.
"""

from __future__ import annotations

from datetime import date

from pydantic import StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class SeekingAlphaCalendarEarnings(ContractModel):
    """One scheduled earnings report; columns the calendar does not carry stay null."""

    report_date: date
    symbol: StrictStr
    name: StrictStr | None = None
    eps_previous: StrictFloat | None = None
    eps_consensus: StrictFloat | None = None
    market_cap: StrictFloat | None = None
    reporting_time: StrictStr | None = None
    exchange: StrictStr | None = None
    sector_id: StrictInt | None = None


__all__ = ["SeekingAlphaCalendarEarnings"]
