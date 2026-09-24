"""Trading-day expectation for batch windows and freshness lag (A4.7).

Two places ask the same question - "by which date should data exist?":
the incremental batch (its window end) and the freshness check (its
lag). Both used to answer "today", so a weekend run re-fetched a day
that never happened and every Saturday read as a two-day outage.

Two tiers answer here, and each one reports which tier spoke, because
the tiers are not equally trustworthy:

* ``warehouse-calendar`` - the open days landed in
  :data:`CALENDAR_TABLE`. Inside their coverage the stored rows decide,
  so holidays are correct; outside it the weekday rule takes over per
  day (the upstream calendar is a rolling trailing year, so coverage
  never reaches forward).
* ``weekday-rule`` - Monday..Friday. It knows nothing about holidays,
  but it never pretends otherwise, and it removes the larger error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqlalchemy import Engine

#: Warehouse table holding the landed :class:`TradingCalendar` contract.
CALENDAR_TABLE = "dwd_trading_calendar"

#: Provenance labels.
TIER_WAREHOUSE = "warehouse-calendar"
TIER_WEEKDAY = "weekday-rule"

#: How far :meth:`TradingCalendar.expected_data_date` walks back before it
#: calls the calendar broken (the longest real closure is a week).
MAX_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class TradingCalendar:
    """Known open days plus the rule that fills the gaps.

    Attributes:
        tier: Which tier produced this calendar.
        open_days: Dates the warehouse says the market was open.
    """

    tier: str = TIER_WEEKDAY
    open_days: frozenset[date] = field(default_factory=frozenset)

    @property
    def coverage(self) -> tuple[date, date] | None:
        """First and last known open day, None when nothing is known."""
        if not self.open_days:
            return None
        return min(self.open_days), max(self.open_days)

    def is_trading_day(self, day: date) -> bool:
        """Whether ``day`` should carry data.

        A known open day is open. A day inside coverage but absent from
        the landed rows is closed - that is what a holiday looks like in
        a table that only stores open days. Anything past coverage falls
        to the weekday rule, which is why "today" stays a trading day
        even though the upstream calendar never publishes it in advance.

        Args:
            day: The date to classify.

        Returns:
            True when data is expected for that date.
        """
        if day in self.open_days:
            return True
        span = self.coverage
        if span is not None and span[0] <= day <= span[1]:
            return False
        return day.weekday() < 5

    def expected_data_date(self, day: date) -> date:
        """Latest date on or before ``day`` for which data should exist.

        Args:
            day: The reference date (usually the run date).

        Returns:
            The expectation to compare ``MAX(<date column>)`` against.

        Raises:
            ValueError: If no trading day shows up within
                :data:`MAX_LOOKBACK_DAYS` - the landed rows are broken,
                and silently fetching a month-old window would hide it.
        """
        candidate = day
        for _ in range(MAX_LOOKBACK_DAYS):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        raise ValueError(
            f"no trading day within {MAX_LOOKBACK_DAYS} days before {day.isoformat()}: "
            f"{CALENDAR_TABLE} looks broken (tier {self.tier})"
        )

    def answered_from(self, day: date) -> str:
        """Which tier actually decided ``day`` (provenance for reports).

        Args:
            day: The date that was classified.

        Returns:
            :data:`TIER_WAREHOUSE` inside coverage, else
                :data:`TIER_WEEKDAY`.
        """
        span = self.coverage
        if span is not None and span[0] <= day <= span[1]:
            return TIER_WAREHOUSE
        return TIER_WEEKDAY


def weekday_calendar() -> TradingCalendar:
    """The no-data tier: Monday..Friday only, no holiday knowledge."""
    return TradingCalendar(tier=TIER_WEEKDAY)


def calendar_from_days(open_days: Iterable[date]) -> TradingCalendar:
    """Build a warehouse-tier calendar from landed open days.

    Args:
        open_days: Dates the market was open (non-open dates must not be
            in here; the tier infers closure from absence).

    Returns:
        A calendar, or :func:`weekday_calendar` when nothing was given.
    """
    days = frozenset(open_days)
    if not days:
        return weekday_calendar()
    return TradingCalendar(tier=TIER_WAREHOUSE, open_days=days)


def warehouse_calendar(engine: Engine, *, table: str = CALENDAR_TABLE) -> TradingCalendar:
    """Read the landed calendar, falling back to the weekday rule.

    An absent or unreadable table is the normal state before the calendar
    is landed, so it degrades rather than raising; the tier in the report
    says how much the answer is worth. The except is deliberately wide -
    a missing table, a missing column and a driver quirk all mean "no
    calendar here", as they do for the ods/dwd readers around it.

    Args:
        engine: Warehouse engine.
        table: Calendar table to read.

    Returns:
        The warehouse-tier calendar when it has rows, else the weekday tier.
    """
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT `date` FROM `{table}` WHERE `is_open` = 1 ORDER BY `date`")  # noqa: S608  # derived table
            ).all()
    except Exception as exc:  # a missing calendar table is a normal first-run state
        logger.warning(f"trading calendar unavailable from {table}: {exc!s}")
        return weekday_calendar()
    return calendar_from_days(day for row in rows if (day := _as_date(row[0])) is not None)


def resolve_calendar(engine: Engine | None = None) -> TradingCalendar:
    """Best available calendar: landed rows when present, weekday rule otherwise.

    Args:
        engine: Warehouse engine; None means no table to read.

    Returns:
        The calendar to derive expectations from.
    """
    if engine is None:
        return weekday_calendar()
    return warehouse_calendar(engine)


def _as_date(value: object) -> date | None:
    """Coerce a driver value to a date, None when unparsable.

    MySQL hands back ``date`` objects for a DATE column; SQLite hands
    back the ISO text, so both shapes are read here rather than forcing
    every test onto a MySQL container.
    """
    from datetime import datetime

    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


__all__ = [
    "CALENDAR_TABLE",
    "MAX_LOOKBACK_DAYS",
    "TIER_WAREHOUSE",
    "TIER_WEEKDAY",
    "TradingCalendar",
    "calendar_from_days",
    "resolve_calendar",
    "warehouse_calendar",
    "weekday_calendar",
]
