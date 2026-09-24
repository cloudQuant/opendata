"""Trading-day expectation tests (A4.7).

The warehouse is off-limits here: the warehouse tier is exercised against
an in-memory SQLite table and the dates are fixed literals, so a weekend
run of this suite cannot quietly change what "stale" means. 2026-09-25 is
a Friday, 09-26/27 the weekend; 2025-10-01..08 is the National Day
closure - a real holiday stretch, not a made-up one.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Boolean, Column, Date, MetaData, String, Table, create_engine, insert

from opendata.pipeline.trading_calendar import (
    CALENDAR_TABLE,
    TIER_WAREHOUSE,
    TIER_WEEKDAY,
    TradingCalendar,
    calendar_from_days,
    resolve_calendar,
    warehouse_calendar,
    weekday_calendar,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

FRIDAY = date(2026, 9, 25)
SATURDAY = date(2026, 9, 26)
SUNDAY = date(2026, 9, 27)
MONDAY = date(2026, 9, 28)
BEFORE_HOLIDAY = date(2025, 9, 30)
HOLIDAY_REOPEN = date(2025, 10, 9)
HOLIDAY_MID = date(2025, 10, 4)


def _calendar_engine(open_days: Sequence[date]) -> Engine:
    """A calendar table holding ``open_days`` plus one explicit closed row.

    The rows go in typed, but the reader selects through a plain
    statement, which is how the SQLite driver hands a DATE column back -
    ISO text where MySQL hands over a ``date``.
    """
    engine = create_engine("sqlite://")
    metadata = MetaData()
    calendar = Table(
        CALENDAR_TABLE,
        metadata,
        Column("exchange", String(16)),
        Column("date", Date),
        Column("is_open", Boolean),
    )
    metadata.create_all(engine)
    rows = [{"exchange": "CN-SSE", "date": day, "is_open": True} for day in open_days] + [
        {"exchange": "CN-SSE", "date": SATURDAY, "is_open": False}
    ]
    with engine.begin() as conn:
        conn.execute(insert(calendar), rows)
    return engine


class TestWeekdayTier:
    """The tier that knows nothing about holidays, and says so."""

    def test_weekend_expects_friday(self) -> None:
        calendar = weekday_calendar()

        assert calendar.expected_data_date(SATURDAY) == FRIDAY
        assert calendar.expected_data_date(SUNDAY) == FRIDAY
        assert calendar.expected_data_date(MONDAY) == MONDAY

    def test_tier_and_provenance_are_the_weekday_rule(self) -> None:
        calendar = weekday_calendar()

        assert calendar.coverage is None
        assert calendar.tier == TIER_WEEKDAY
        assert calendar.answered_from(FRIDAY) == TIER_WEEKDAY

    def test_no_landed_days_is_the_weekday_tier(self) -> None:
        assert calendar_from_days([]).tier == TIER_WEEKDAY


class TestWarehouseTier:
    """Inside coverage the landed rows decide, holidays included."""

    def test_a_holiday_is_not_a_lag(self) -> None:
        calendar = calendar_from_days([BEFORE_HOLIDAY, HOLIDAY_REOPEN])

        assert calendar.tier == TIER_WAREHOUSE
        assert calendar.is_trading_day(HOLIDAY_MID) is False
        assert calendar.expected_data_date(HOLIDAY_MID) == BEFORE_HOLIDAY
        assert calendar.answered_from(HOLIDAY_MID) == TIER_WAREHOUSE

    def test_days_beyond_coverage_still_count_as_open(self) -> None:
        """The upstream calendar ends at the last completed trading day."""
        calendar = calendar_from_days([FRIDAY])

        assert calendar.coverage == (FRIDAY, FRIDAY)
        assert calendar.is_trading_day(SATURDAY) is False
        assert calendar.is_trading_day(MONDAY) is True
        assert calendar.answered_from(MONDAY) == TIER_WEEKDAY

    def test_a_calendar_that_never_opens_fails_closed(self) -> None:
        """Thirty dead days means broken rows, not a month-long holiday."""
        calendar = TradingCalendar(
            tier=TIER_WAREHOUSE,
            open_days=frozenset({FRIDAY - timedelta(days=45), FRIDAY + timedelta(days=1)}),
        )

        with pytest.raises(ValueError, match="looks broken"):
            calendar.expected_data_date(FRIDAY)


class TestWarehouseRead:
    """Reading the table, and degrading when it is not there yet."""

    def test_landed_open_days_raise_the_tier(self) -> None:
        calendar = warehouse_calendar(_calendar_engine([BEFORE_HOLIDAY, FRIDAY]))

        assert calendar.tier == TIER_WAREHOUSE
        assert calendar.open_days == frozenset({BEFORE_HOLIDAY, FRIDAY})
        assert calendar.expected_data_date(SATURDAY) == FRIDAY

    def test_missing_table_degrades_to_the_weekday_rule(self) -> None:
        engine = create_engine("sqlite://")

        assert warehouse_calendar(engine).tier == TIER_WEEKDAY
        assert resolve_calendar(engine).tier == TIER_WEEKDAY

    def test_unreadable_engine_degrades_instead_of_failing_the_run(self) -> None:
        assert warehouse_calendar(object()).tier == TIER_WEEKDAY

    def test_no_engine_means_no_table_to_read(self) -> None:
        assert resolve_calendar(None).tier == TIER_WEEKDAY


class TestQueryExpectation:
    """The freshness endpoints measure lag against the same date."""

    async def test_warehouse_calendar_sets_the_expectation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opendata.api import data_query

        monkeypatch.setattr(
            data_query,
            "resolve_calendar",
            lambda engine: calendar_from_days([BEFORE_HOLIDAY, HOLIDAY_REOPEN]),
        )

        assert await data_query._expected_data_date(object(), on=HOLIDAY_MID) == BEFORE_HOLIDAY

    async def test_weekend_expectation_is_the_previous_friday(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from opendata.api import data_query

        monkeypatch.setattr(data_query, "resolve_calendar", lambda engine: weekday_calendar())

        assert await data_query._expected_data_date(object(), on=SATURDAY) == FRIDAY
