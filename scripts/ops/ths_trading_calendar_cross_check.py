"""ths trading calendar against the bars an independent vendor landed (AC-10 / A4.7).

The ``trading_calendar`` leg is the producer side of the expectation predicate
in :mod:`opendata.pipeline.trading_calendar`, so ``verified=true`` needs a fact
that is not the calendar agreeing with itself. The fact used here is the price
series: *a day carrying bars is a day the market was open*, whatever any
calendar claims. Reading that back from the **akshare** chain while the
calendar comes from **fuyao** makes the two sides independent vendors, which is
what turns the check into evidence.

Four legs are measured, and the two kinds are kept apart on purpose:

* ``independent`` - rows this deployment holds from the other vendor
  (``ods_stock_daily_akshare`` and the ``source='akshare'`` slice of
  ``dwd_stock_daily``). A bar on a day the fuyao calendar closes fails the run.
* ``same-vendor`` - the fuyao/em ODS chain and ``source='ths'``. Agreement here
  is self-consistency, not corroboration, so these legs are reported and never
  decide the verdict (the C9 observation-only-board lesson).

A judged leg with too few dates inside the published window is reported as
*unverifiable*, not as a pass: an empty intersection proves nothing.

The calendar itself is re-read from the contract rows and checked for internal
shape (strictly ascending, one row per date, every row open, and
``prev_trade_date`` / ``next_trade_date`` matching the neighbouring rows),
because the adapter derives those two fields rather than reading them upstream.

Nothing is written; the warehouse is read only.

Usage (py313 env, fuyao credentials configured, warehouse reachable):
    python scripts/ops/ths_trading_calendar_cross_check.py
"""

from __future__ import annotations

import argparse
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine

    from opendata.data.models import TradingCalendar
    from opendata.data.protocol import Fetcher

#: 判定至少需要的「覆盖窗口内的独立 bar 天数」。少到这里就等于两侧没有交集，
#: 只能报不可判——空交集不是一个证据（C11a 的教训）。
MIN_JUDGABLE_DAYS = 20


class Leg(NamedTuple):
    """One reference population to read bar dates from."""

    label: str
    table: str
    column: str
    #: ``None`` reads the whole table; otherwise the ``source`` column is
    #: filtered, since ``dwd_stock_daily`` mixes both vendors' rows.
    source_filter: str | None
    #: Whether this population was published by a vendor other than fuyao.
    independent: bool


LEGS: tuple[Leg, ...] = (
    Leg("ods_stock_daily_akshare", "ods_stock_daily_akshare", "日期", None, True),
    Leg("dwd_stock_daily（source=akshare）", "dwd_stock_daily", "trade_date", "akshare", True),
    Leg("dwd_stock_daily（source=ths）", "dwd_stock_daily", "trade_date", "ths", False),
    Leg("ods_stock_daily_ths", "ods_stock_daily_ths", "trade_date", None, False),
)


def _calendar_fetcher() -> Fetcher[Any, Any]:
    """Resolve the routed ths calendar fetcher exactly as a caller would."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry().resolve_domain("trading_calendar", source="ths")


def bar_dates(engine: Engine, leg: Leg) -> set[date]:
    """Read the distinct bar dates of one reference population.

    Args:
        engine: Warehouse engine (read-only use).
        leg: The population to read.

    Returns:
        Dates carrying at least one bar row, unparsable rows dropped.
    """
    from sqlalchemy import text

    from opendata.pipeline.trading_calendar import _as_date

    where = f"WHERE `{leg.column}` IS NOT NULL"
    params: dict[str, Any] = {}
    if leg.source_filter is not None:
        where += " AND `source` = :source"
        params["source"] = leg.source_filter
    sql = f"SELECT DISTINCT `{leg.column}` FROM `{leg.table}` {where}"  # noqa: S608  # 常量表名/列名
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).all()
    return {day for row in rows if (day := _as_date(row[0])) is not None}


def check_shape(rows: Sequence[TradingCalendar]) -> list[str]:
    """Judge the contract rows on the fields the adapter derives itself.

    Args:
        rows: Calendar rows from the routed fetcher, in returned order.

    Returns:
        One line per defect; empty when the rows are well formed.
    """
    from opendata.data.providers.ths.models.trading_calendar import DEFAULT_EXCHANGE

    defects: list[str] = []
    if not rows:
        return ["empty calendar"]
    dates = [row.date for row in rows]
    if any(b <= a for a, b in zip(dates, dates[1:], strict=False)):
        defects.append("dates are not strictly ascending")
    if len(set(dates)) != len(dates):
        defects.append(f"{len(dates) - len(set(dates))} duplicate date rows")
    if not all(row.is_open for row in rows):
        defects.append(f"{sum(1 for row in rows if not row.is_open)} rows claim is_open=false")
    exchanges = {row.exchange for row in rows}
    if exchanges != {DEFAULT_EXCHANGE}:
        defects.append(f"exchange labels: {sorted(exchanges)}")
    bad_neighbours = [
        row
        for index, row in enumerate(rows)
        if row.prev_trade_date != (dates[index - 1] if index else None)
        or row.next_trade_date != (dates[index + 1] if index + 1 < len(dates) else None)
    ]
    if bad_neighbours:
        first = bad_neighbours[0]
        defects.append(
            f"{len(bad_neighbours)} rows disagree with their neighbouring rows, e.g. "
            f"{first.date}: prev/next are {first.prev_trade_date}/{first.next_trade_date}"
        )
    return defects


class LegResult(NamedTuple):
    """One reference population measured against the published calendar."""

    leg: Leg
    dates: frozenset[date]
    inside: frozenset[date]
    on_closed: tuple[date, ...]
    open_without_bars: tuple[date, ...]

    @property
    def judgable(self) -> bool:
        """Whether enough dates fall inside coverage for a verdict to mean anything."""
        return len(self.inside) >= MIN_JUDGABLE_DAYS


def measure_leg(
    engine: Engine, leg: Leg, open_days: frozenset[date], window: tuple[date, date]
) -> LegResult:
    """Read one population's bar dates and line them up against the calendar.

    Args:
        engine: Warehouse engine (read-only use).
        leg: The population being judged.
        open_days: Calendar open days from the routed fetcher.
        window: First and last published calendar date (inclusive).

    Returns:
        The raw date set plus the two diff directions: bar dates the calendar
        closes, and calendar open days inside this population's own span that
        carry no bars at all (reported, never judged - a sparse backfill looks
        identical to a phantom open day from here).
    """
    dates = frozenset(bar_dates(engine, leg))
    start, end = window
    inside = frozenset(day for day in dates if start <= day <= end)
    span_start, span_end = (min(dates), max(dates)) if dates else (start, end)
    in_span = frozenset(day for day in open_days if span_start <= day <= span_end)
    return LegResult(
        leg=leg,
        dates=dates,
        inside=inside,
        on_closed=tuple(sorted(inside - open_days)),
        open_without_bars=tuple(sorted(in_span - dates)),
    )


def leg_failures(result: LegResult) -> list[str]:
    """Say what one leg proves; an independent leg that proves nothing fails.

    Args:
        result: The measured leg.

    Returns:
        Failure lines - empty for a same-vendor leg (self-consistency is not
        corroboration) and for an independent leg the calendar agrees with.
    """
    if not result.leg.independent:
        return []
    if not result.judgable:
        return [
            f"- {result.leg.label}: 日历覆盖窗口内只有 {len(result.inside)} 个日期，"
            f"不足 {MIN_JUDGABLE_DAYS}，判不了"
        ]
    if result.on_closed:
        return [
            f"- {result.leg.label}: {len(result.on_closed)} 个日历休市日有 bar，"
            f"例如 {list(result.on_closed[:5])}"
        ]
    return []


def _table_row(result: LegResult) -> str:
    """Render one markdown table row (8 cells, matching the header in ``main``)."""
    verdict = "OBSERVE"
    if result.leg.independent:
        verdict = "FAIL" if leg_failures(result) else "PASS"
    closed = ", ".join(str(day) for day in result.on_closed[:5]) or "-"
    return (
        f"| {result.leg.label} | {len(result.dates)} | {len(result.inside)} "
        f"| {len(result.dates) - len(result.inside)} | {len(result.on_closed)} | {closed} "
        f"| {len(result.open_without_bars)} | {verdict} |"
    )


def _header(endpoint: str, rows: Sequence[TradingCalendar]) -> list[str]:
    """Static preamble plus the two facts every judgement is read against."""
    first, last = rows[0].date, rows[-1].date
    return [
        "AC-10 trading_calendar (ths) vs the bars an independent vendor landed",
        "",
        f"script: {REPO_RELATIVE}",
        "ours: get_registry().resolve_domain('trading_calendar', source='ths').fetch()",
        f"          (the routing path; endpoint {endpoint})",
        "reference: 本库已落库的 bar 日期 —— 有 bar 的那天市场必然开市",
        "          (akshare 链为独立 vendor，判 PASS/FAIL；ths 链同 vendor，只观测)",
        "hard criteria: 独立 vendor 落在日历休市日的 bar 日期数 == 0；",
        f"          每条判定腿在日历覆盖窗口内至少 {MIN_JUDGABLE_DAYS} 个日期（否则算不可判）；",
        "          日历行严格递增、无重复、全为开市、prev/next 与相邻行一致。",
        "observe only: 「日历说开市但该区段无 bar」列在最后一栏，不计入 result——",
        "          搬运缺日与日历多报开市日在 bar 侧无法区分。",
        "",
        f"calendar      : {len(rows)} rows, coverage {first}..{last}, "
        f"{(date.today() - last).days} 天前为最后一个开市日",
    ]


def main(argv: list[str] | None = None) -> int:
    """Run the comparison, archive the report, and exit non-zero on failure.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        ``0`` when every judged leg corroborates the calendar, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="docs/evidence/C13/ths-trading-calendar-cross-check.txt",
        help="Where to archive the report",
    )
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine

    from opendata.core.config import settings
    from opendata.data.providers.ths.endpoints import CALENDAR_ENDPOINT
    from opendata.data.providers.ths.models.trading_calendar import ThsTradingCalendarFetcher

    fetcher = _calendar_fetcher()
    if not isinstance(fetcher, ThsTradingCalendarFetcher):  # 路由必须落在这一条腿上
        raise RuntimeError(f"trading_calendar/ths resolves to {type(fetcher).__name__}")
    rows = cast("list[TradingCalendar]", list(fetcher.fetch()))
    open_days = frozenset(row.date for row in rows)
    window = (rows[0].date, rows[-1].date)

    lines = _header(CALENDAR_ENDPOINT, rows)
    failures: list[str] = [f"- 日历契约行：{defect}" for defect in check_shape(rows)]

    lines.append("")
    lines.append(
        "| 参照数据集 | 全表日期数 | 窗口内 | 窗口外 | 休市日有bar | 明细 "
        "| 开市日无bar(自身区段) | result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    engine = create_engine(settings.data_database_url)
    results = [measure_leg(engine, leg, open_days, window) for leg in LEGS]
    for result in results:
        lines.append(_table_row(result))
        failures.extend(leg_failures(result))

    lines.append("")
    lines.append(
        "反向核对（只观测，不判定）：某日日历说开市而本库没有任何 bar，"
        "可能是搬运缺日，也可能是日历多报了一个开市日，"
        "单凭 bar 无法区分，故不计入 result。"
    )
    lines.append("")
    lines.append(f"result: {'FAIL' if failures else 'PASS'} ({len(failures)} failing checks)")
    if failures:
        lines.extend(failures)

    report_text = "\n".join(lines) + "\n"
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_text, encoding="utf-8")
    print(report_text, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
