"""Trading-day expectation for batch windows and freshness lag (A4.7).

Two places ask the same question - "by which date should data exist?":
the incremental batch (its window end) and the freshness check (its
lag). Both used to answer "today", so a weekend run re-fetched a day
that never happened and every Saturday read as a two-day outage.

Two tiers answer here, and each one reports which tier spoke, because
the tiers are not equally trustworthy:

* ``warehouse-calendar`` - the rows landed in :data:`CALENDAR_TABLE`,
  read back through the :class:`opendata.data.models.TradingCalendar`
  contract (design §4.1). Inside their coverage the stored rows decide,
  so holidays are correct; outside it the weekday rule takes over per
  day (the upstream calendar is a rolling trailing year, so coverage
  never reaches forward).
* ``weekday-rule`` - Monday..Friday. It knows nothing about holidays,
  but it never pretends otherwise, and it removes the larger error.

What the warehouse tier hands back is a *projection* of that contract
(:class:`CalendarView`), not a second model of the same thing: the
questions the pipeline asks are per-day, so the rows are collapsed into
open days, closed days, and the ``prev_trade_date`` the contract itself
published. Those published previous-trade-dates are kept as independent
witnesses - a table whose ``is_open`` flags and ``prev_trade_date``
chain disagree is internally broken, and picking one of the two quietly
would hide it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import text

from opendata.data.models import TradingCalendar as CalendarContract

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from sqlalchemy import Engine

#: Warehouse table holding the landed :class:`CalendarContract` rows.
CALENDAR_TABLE = "dwd_trading_calendar"

#: Provenance labels.
TIER_WAREHOUSE = "warehouse-calendar"
TIER_WEEKDAY = "weekday-rule"

#: How far :meth:`CalendarView.expected_data_date` walks back before it
#: calls the calendar broken (the longest real closure is a week).
MAX_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class CalendarView:
    """The landed calendar projected onto the questions the pipeline asks.

    Attributes:
        tier: Which tier produced this calendar.
        open_days: Dates the warehouse says the market was open.
        closed_days: Dates the warehouse says the market was closed with
            an explicit row (``is_open = False``); a date inside coverage
            with no row at all counts as closed too.
        prev_trade_dates: For each closed date that carries one, the
            previous trade date published by the contract row itself -
            the witness :meth:`expected_data_date` is checked against.
    """

    tier: str = TIER_WEEKDAY
    open_days: frozenset[date] = field(default_factory=frozenset)
    closed_days: frozenset[date] = field(default_factory=frozenset)
    prev_trade_dates: dict[date, date] = field(default_factory=dict)

    @property
    def coverage(self) -> tuple[date, date] | None:
        """First and last date the landed rows speak for, None when empty."""
        known = self.open_days | self.closed_days
        if not known:
            return None
        return min(known), max(known)

    def is_trading_day(self, day: date) -> bool:
        """Whether ``day`` should carry data.

        A known open day is open. A day inside coverage but absent from
        the landed rows is closed - that is what a holiday looks like in
        a table that only stores open days, and an explicit closed row
        says the same thing. Anything past coverage falls to the weekday
        rule, which is why "today" stays a trading day even though the
        upstream calendar never publishes it in advance.

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

        The answer is walked back from the open/closed sets, then checked
        against what the contract row for ``day`` itself publishes.

        Args:
            day: The reference date (usually the run date).

        Returns:
            The expectation to compare ``MAX(<date column>)`` against.

        Raises:
            ValueError: If no trading day shows up within
                :data:`MAX_LOOKBACK_DAYS` - the landed rows are broken,
                and silently fetching a month-old window would hide it.
                Also if the landed rows disagree with themselves, which
                is the same kind of breakage.
        """
        candidate = day
        for _ in range(MAX_LOOKBACK_DAYS):
            if self.is_trading_day(candidate):
                return self._with_witness(day, candidate)
            candidate -= timedelta(days=1)
        raise ValueError(
            f"no trading day within {MAX_LOOKBACK_DAYS} days before {day.isoformat()}: "
            f"{CALENDAR_TABLE} looks broken (tier {self.tier})"
        )

    def _with_witness(self, day: date, expected: date) -> date:
        """Return ``expected`` unless the contract row for ``day`` names another.

        Args:
            day: The date ``expected`` was derived for.
            expected: The date the open/closed walk-back produced.

        Returns:
            ``expected`` when the published previous trade date agrees
            (or there is none to compare).

        Raises:
            ValueError: When the two answers differ - ``is_open`` flags
                and ``prev_trade_date`` cannot both be trusted, and a
                batch window built on a coin flip is not worth running.
        """
        witness = self.prev_trade_dates.get(day)
        if witness is not None and witness != expected:
            raise ValueError(
                f"{CALENDAR_TABLE} disagrees with itself on {day.isoformat()}: "
                f"prev_trade_date says {witness.isoformat()}, "
                f"the open/closed rows say {expected.isoformat()}"
            )
        return expected

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


def weekday_calendar() -> CalendarView:
    """The no-data tier: Monday..Friday only, no holiday knowledge."""
    return CalendarView(tier=TIER_WEEKDAY)


def calendar_from_days(open_days: Iterable[date]) -> CalendarView:
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
    return CalendarView(tier=TIER_WAREHOUSE, open_days=days)


def calendar_from_contracts(rows: Sequence[CalendarContract]) -> CalendarView:
    """Project validated contract rows onto the calendar the pipeline asks.

    Args:
        rows: Rows of :data:`CALENDAR_TABLE`, each already an
            :class:`opendata.data.models.TradingCalendar`.

    Returns:
        The warehouse-tier view, or :func:`weekday_calendar` when the
        table holds nothing (no rows is "not landed yet", which is not
        the same as "landed and empty").
    """
    if not rows:
        return weekday_calendar()
    return CalendarView(
        tier=TIER_WAREHOUSE,
        open_days=frozenset(row.date for row in rows if row.is_open),
        closed_days=frozenset(row.date for row in rows if not row.is_open),
        prev_trade_dates={
            row.date: row.prev_trade_date
            for row in rows
            if not row.is_open and row.prev_trade_date is not None
        },
    )


def warehouse_calendar(engine: Engine, *, table: str = CALENDAR_TABLE) -> CalendarView:
    """Read the landed calendar through its contract, else the weekday rule.

    An absent or unreadable table is the normal state before the calendar
    is landed, so it degrades rather than raising; the tier in the report
    says how much the answer is worth. The rows are read whole and handed
    to the contract before use - a table that does not carry the fields
    design §4.1 fixes (or carries rows the contract rejects) is not the
    calendar this pipeline can reason about, and that too degrades with a
    warning instead of guessing from whatever columns happen to exist.
    The except is deliberately wide: a missing table, a missing column
    and a driver quirk all mean "no calendar here", as they do for the
    ods/dwd readers around it.

    Args:
        engine: Warehouse engine.
        table: Calendar table to read.

    Returns:
        The warehouse-tier calendar when it has contract rows, else the
        weekday tier.
    """
    try:
        import pandas as pd

        with engine.connect() as connection:
            rows = connection.execute(
                text(f"SELECT * FROM `{table}` ORDER BY `date`")  # noqa: S608  # nosec B608  # tbl default=CALENDAR_TABLE literal
            ).mappings()
            records = CalendarContract.from_frame(pd.DataFrame(list(rows)))
    except Exception as exc:  # an unlanded or non-contract table is a normal state
        logger.warning(f"trading calendar contract unavailable from {table}: {exc!s}")
        return weekday_calendar()
    return calendar_from_contracts(records)


def resolve_calendar(engine: Engine | None = None) -> CalendarView:
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
    "CalendarView",
    "calendar_from_contracts",
    "calendar_from_days",
    "resolve_calendar",
    "warehouse_calendar",
    "weekday_calendar",
]
