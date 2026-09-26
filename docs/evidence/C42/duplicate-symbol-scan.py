"""Measure whether the instrument leg ever publishes one ``symbol`` on two pages.

``refresh_metadata_backbone`` lands every accepted row with the key-level dwd
upsert on key ``(symbol,)``, and ``DwdWriter.write`` returns ``len(frame)``. If
two asset-type pages carried the same symbol, one row would win inside a single
write while the run reported both - so this scan asks the question the C42 guard
answers, against the live leg: how many rows does the catalog publish, how many
distinct keys is that, and do the colliding rows disagree with each other?

Read-only by construction: it routes ``registry.fetch`` (the full chain, as
``scripts/ops/metadata_backbone_landing.py`` does) and touches no engine, no DDL,
no INSERT. The credential is used by the client itself and nothing about it is
printed here - only whether each page answered, and with how many rows.

Usage (py313 env):
    python docs/evidence/C42/duplicate-symbol-scan.py
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import Instrument
    from opendata.data.protocol import FetchContext, Fetcher, QueryParams

SOURCE = "ths"
EXCHANGE = "CN-SSE"
DAYS = 40

#: The fields the batch reads (``jobs.drop_inactive_symbols``) plus the identity
#: ones a wrong-page landing would rewrite without anyone noticing.
WATCHED = ("exchange", "name", "status", "list_date", "delist_date", "board")

ModelT = TypeVar("ModelT", bound=BaseModel)


@dataclass(frozen=True)
class CatalogReading:
    """What the nine catalog pages answered, and what it adds up to.

    ``answered``/``failed`` are carried alongside the counts on purpose: without
    them a run where every page raised would print ``collided symbols = 0`` and
    exit 0, which reads as a verdict when it is only an absence of answer.
    """

    asked: int
    answered: int
    failed: tuple[str, ...]
    accepted: int
    distinct: int
    clashed: int
    disagreed: int
    drifted: int

    @property
    def lost_rows(self) -> int:
        """Rows the upsert would swallow under a key another row already holds."""
        return self.accepted - self.distinct


def _accepted(rows: Sequence[object], model: type[ModelT]) -> tuple[list[ModelT], int]:
    """Split a leg's answer into contract instances and the rest.

    C41 measured that these legs answer with contract objects already, so a row
    that is not one is a shape drift - counted here, refused by the refresh.
    """
    accepted = [row for row in rows if isinstance(row, model)]
    return accepted, len(rows) - len(accepted)


def _fetch_page(
    catalog: Fetcher[QueryParams, object], ctx: FetchContext, asset_type: str
) -> tuple[list[object], str | None]:
    """One catalog page: a leg that does not answer is reported, not raised."""
    try:
        return list(catalog.fetch(ctx=ctx, asset_type=asset_type)), None
    except Exception as exc:  # a failing page is a reading of this scan, not a crash
        return [], f"{type(exc).__name__}: {exc}"


def _scan_catalog() -> CatalogReading:
    """Fetch every published page and count the symbol keys across all of them."""
    from opendata.data.models import Instrument
    from opendata.data.protocol import FetchContext
    from opendata.pipeline.jobs import resolve_fetcher
    from scripts.ops.metadata_backbone_landing import DEFAULT_ASSET_TYPES

    catalog = resolve_fetcher("instrument", SOURCE)
    ctx = FetchContext(timeout=60)

    by_symbol: dict[str, list[tuple[str, Instrument]]] = defaultdict(list)
    accepted_rows = 0
    drifted = 0
    failed: list[str] = []
    for asset_type in DEFAULT_ASSET_TYPES:
        rows, failure = _fetch_page(catalog, ctx, asset_type)
        if failure is not None:
            print(f"  page {asset_type}: LEG_FAILED {failure}")
            failed.append(asset_type)
            continue
        rows_accepted, rows_drifted = _accepted(rows, Instrument)
        accepted_rows += len(rows_accepted)
        drifted += rows_drifted
        print(f"  page {asset_type}: rows={len(rows_accepted)} not_a_contract={rows_drifted}")
        for row in rows_accepted:
            by_symbol[row.symbol].append((asset_type, row))

    clashes = {symbol: hits for symbol, hits in by_symbol.items() if len(hits) > 1}
    disagreed = 0
    for symbol, hits in sorted(clashes.items())[:20]:
        values = {page: tuple(str(getattr(row, field)) for field in WATCHED) for page, row in hits}
        differs = len(set(values.values())) > 1
        disagreed += int(differs)
        print(f"  {symbol}: pages={sorted(values)} disagree={differs}")
        if differs:
            for page, value in sorted(values.items()):
                print(f"    {page}: {dict(zip(WATCHED, value, strict=True))}")

    return CatalogReading(
        asked=len(DEFAULT_ASSET_TYPES),
        answered=len(DEFAULT_ASSET_TYPES) - len(failed),
        failed=tuple(failed),
        accepted=accepted_rows,
        distinct=len(by_symbol),
        clashed=len(clashes),
        disagreed=disagreed,
        drifted=drifted,
    )


def _scan_calendar() -> tuple[str, int, int, str]:
    """The asked window, accepted rows, distinct (exchange, date) keys, returned span."""
    from opendata.data.models import TradingCalendar
    from opendata.data.protocol import FetchContext
    from opendata.pipeline.jobs import resolve_fetcher

    today = date.today()
    start = today - timedelta(days=DAYS - 1)
    calendar = resolve_fetcher("trading_calendar", SOURCE)
    rows = list(
        calendar.fetch(
            ctx=FetchContext(timeout=60),
            exchange=EXCHANGE,
            start_date=start,
            end_date=today,
        )
    )
    accepted, _ = _accepted(rows, TradingCalendar)
    dates = [row.date for row in accepted]
    span = f"{min(dates)} .. {max(dates)}" if dates else "empty"
    return (
        f"{start} .. {today}",
        len(accepted),
        len({(row.exchange, row.date) for row in accepted}),
        span,
    )


def main() -> int:
    """Print both legs' readings: rows published vs keys actually available."""
    print(f"# duplicate-symbol scan @ {date.today()} source={SOURCE} (read-only)")
    reading = _scan_catalog()
    print("\n  instrument leg, dwd key (symbol,):")
    print(f"    pages asked        = {reading.asked}")
    print(f"    pages answered     = {reading.answered}")
    print(f"    accepted rows      = {reading.accepted}")
    print(f"    distinct symbols   = {reading.distinct}")
    print(f"    collided symbols   = {reading.clashed} (the last row of the leg would win)")
    print(f"    rows an upsert swallows = {reading.lost_rows}")
    print(f"    collided and disagreeing = {reading.disagreed}")
    print(f"    rows the contract refused = {reading.drifted}")
    if reading.failed:
        print(
            f"\n  SCAN INCOMPLETE: {len(reading.failed)} page(s) did not answer "
            f"({', '.join(reading.failed)})."
        )
        print("  A zero collision count from an unanswered leg is not a verdict, so this exits 1.")
        return 1

    window, calendar_rows, calendar_keys, calendar_span = _scan_calendar()
    print("\n  calendar leg, dwd key (exchange, date):")
    print(f"    asked window       = {window}")
    print(f"    returned span      = {calendar_span}")
    print(f"    accepted rows      = {calendar_rows}")
    print(f"    distinct keys      = {calendar_keys}")
    print(f"    collided keys      = {calendar_rows - calendar_keys}")
    if calendar_rows == 0:
        print("\n  SCAN INCOMPLETE: the calendar leg answered with no row.")
        print("  A zero collision count from an empty calendar is not a verdict, so this exits 1.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
