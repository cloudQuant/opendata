"""Trading-day expectation measured against two independent facts (A4.7 / AC-13).

The expectation predicate a batch window and a freshness check both use is
only as good as the calendar behind it, so this script checks it against:

* the fuyao calendar - one request, a rolling trailing year of open days,
  and the source the landed ``dwd_trading_calendar`` rows come from;
* the dates this deployment actually holds bars for - a day with bars is a
  day the market was open, whatever any calendar claims.

It then quantifies what the pre-A4.7 logic cost: with
``expected = date.today()`` every non-trading day read as a lag, so the
weekday-rule days the calendar closes are counted and shown as spans.
Nothing is written; the warehouse is read only.

Usage (py313 env, fuyao credentials configured, warehouse reachable):
    python scripts/ops/calendar_expectation_check.py --limit 500
"""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

#: The domain whose landed dates stand in for "the market was open".
CHECK_DOMAIN_TABLE = "dwd_stock_daily"


def fetch_upstream_open_days() -> tuple[frozenset[date], date, date]:
    """Pull the fuyao calendar and reduce it to open days.

    Returns:
        The open-day set plus the first and last date upstream published
        (its coverage; today is never inside it).

    Raises:
        RuntimeError: When the calendar comes back empty.
    """
    from opendata.data.providers.ths.models._client import client
    from opendata_fuyao.endpoints import fetch_trading_calendar

    with client() as fuyao:
        rows = fetch_trading_calendar(fuyao, exchange="CN-SSE")
    days = frozenset({row.date for row in rows})
    if not days:
        raise RuntimeError("fuyao returned an empty trading calendar")
    return days, min(days), max(days)


def landed_trade_dates(engine: Engine, *, since: date, limit: int) -> set[date]:
    """Read the distinct bar dates the warehouse actually holds.

    Args:
        engine: Warehouse engine (read-only use).
        since: Lower bound of the window.
        limit: Cap on distinct dates (guards a full scan on a big table).

    Returns:
        Dates carrying at least one bar row, unparsable rows dropped.
    """
    from sqlalchemy import text

    from opendata.pipeline.trading_calendar import _as_date

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"SELECT DISTINCT `trade_date` FROM `{CHECK_DOMAIN_TABLE}` "  # noqa: S608  # constant table name
                "WHERE `trade_date` >= :since ORDER BY `trade_date` LIMIT :limit"
            ),
            {"since": since, "limit": limit},
        ).all()
    return {day for row in rows if (day := _as_date(row[0])) is not None}


def weekday_overstatements(open_days: frozenset[date], coverage: tuple[date, date]) -> list[date]:
    """Weekdays inside coverage the real calendar says the market was closed.

    These are exactly the days the Mon..Fri tier reports as stale while
    nothing is missing, i.e. the false alarms A4.7 removes once the
    calendar is landed.

    Args:
        open_days: Fuyao open days.
        coverage: First and last published date (inclusive).

    Returns:
        Ascending dates a weekday rule would wrongly expect data for.
    """
    start, end = coverage
    missed: list[date] = []
    day = start
    while day <= end:
        if day.weekday() < 5 and day not in open_days:
            missed.append(day)
        day += timedelta(days=1)
    return missed


def build_report(
    open_days: frozenset[date],
    landed: set[date],
    *,
    coverage: tuple[date, date],
    holidays: Sequence[date],
    on: date,
) -> tuple[list[str], bool]:
    """Compose the printed report and the pass/fail verdict.

    Args:
        open_days: Calendar-backed open days.
        landed: Dates with bars in the warehouse.
        coverage: Calendar coverage window.
        holidays: Weekday holidays inside coverage.
        on: Reference date the expectation is computed for.

    Returns:
        Report lines plus whether the checks passed.
    """
    from opendata.pipeline.trading_calendar import calendar_from_days, weekday_calendar

    start, end = coverage
    landed_calendar = calendar_from_days(open_days)
    inside = {day for day in landed if start <= day <= end}
    bars_on_closed_days = sorted(inside - open_days)
    open_without_bars = sorted(open_days - inside)
    expected = landed_calendar.expected_data_date(on)
    lines = [
        f"calendar        : {len(open_days)} open days, coverage {start}..{end}",
        f"warehouse bars  : {len(inside)} distinct trade_date inside coverage "
        f"(from {CHECK_DOMAIN_TABLE})",
        f"agreement       : bars-on-closed-day={len(bars_on_closed_days)} "
        f"open-day-without-bars={len(open_without_bars)}",
        f"false-stale cost: {len(holidays)} weekday holidays a Mon..Fri rule reports as lagging",
        f"expectation @{on}: calendar tier -> {expected} "
        f"({landed_calendar.answered_from(expected)}); weekday tier -> "
        f"{weekday_calendar().expected_data_date(on)}",
    ]
    passed = True
    if bars_on_closed_days:
        lines.append(f"  FAIL bars exist on days the calendar closed: {bars_on_closed_days[:10]}")
        passed = False
    if start <= expected <= end:
        if expected not in open_days:
            lines.append(
                f"  FAIL expectation {expected} is closed inside coverage: predicate is wrong"
            )
            passed = False
    else:
        # Upstream publishes a rolling trailing year that excludes today, so an
        # answer outside coverage is the designed weekday fallback and there is
        # no independent fact to check it against.
        lines.append(
            f"  NOTE expectation {expected} is outside upstream coverage "
            f"({start}..{end}): answered by the weekday rule, unverifiable here"
        )
    if open_without_bars:
        lines.append(
            f"  NOTE open days with no bars yet (backfill gap, not a calendar fault): "
            f"{open_without_bars[:5]} ... {open_without_bars[-5:]}"
        )
    if holidays:
        lines.append(f"  old logic reported stale on: {', '.join(_group_spans(holidays))}")
    return lines, passed


def _group_spans(days: Sequence[date]) -> list[str]:
    """Render dates as ``start..end`` runs (holiday clusters, not noise)."""
    if not days:
        return []
    spans: list[str] = []
    run_start = previous = days[0]
    for day in days[1:]:
        if (day - previous).days > 3:
            spans.append(_span(run_start, previous))
            run_start = day
        previous = day
    spans.append(_span(run_start, previous))
    return spans


def _span(start: date, end: date) -> str:
    """One date or an inclusive range."""
    return start.isoformat() if start == end else f"{start}..{end}"


def main(argv: list[str] | None = None) -> int:
    """Run the comparison and print the report.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when the calendar and the landed dates agree, ``1`` otherwise.
    """
    from sqlalchemy import create_engine

    from opendata.core.config import settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=date.today().isoformat(), help="Reference date (defaults to today)"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Cap on distinct trade dates read (default 500)"
    )
    args = parser.parse_args(argv)
    reference = date.fromisoformat(args.date)

    print(f"# calendar expectation check @ {reference} ({REPO_RELATIVE})")
    open_days, first, last = fetch_upstream_open_days()
    coverage = (first, last)
    engine = create_engine(settings.data_database_url)
    landed = landed_trade_dates(engine, since=first, limit=args.limit)
    holidays = weekday_overstatements(open_days, coverage)
    lines, passed = build_report(
        open_days, landed, coverage=coverage, holidays=holidays, on=reference
    )
    for line in lines:
        print(line)
    print(f"verdict: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
