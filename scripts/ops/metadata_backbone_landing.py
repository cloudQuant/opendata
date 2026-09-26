"""C41: land the metadata backbone the batch reads (design §4.1, step zero).

``opendata.pipeline.jobs.landed_instruments`` and
``opendata.pipeline.trading_calendar.warehouse_calendar`` already read
``dwd_instrument`` / ``dwd_trading_calendar`` through their contracts, and
both degrade to "no backbone here" when the table is absent -- which is the
state of every warehouse in this deployment, because nothing wrote those two
tables. This script is that write, kept as an explicit operator action: a
production landing (and the DDL it needs) is a decision, not something an
import or a daily job should make on its own.

What it does, in order:

1. **read the two legs** through the registry, calling ``fetch`` and not
   ``extract_data`` -- the catalog's shape checks (empty page, duplicated
   ``thscode``, missing snapshot date) and the calendar's neighbour-date
   derivation and window slice both live in the normalize stage, so a leg
   read halfway would land rows that carry no ``prev_trade_date`` witness;
   the catalog is one call per asset class (the leg requires the filter, it
   does not answer a whole market in one page), the calendar one for the
   exchange it publishes;
2. **put every row through its contract** (:func:`refresh_metadata_backbone`):
   a row the contract refuses is printed with its reason and not landed. The
   date columns are the measured-weak ones (C14/C19: ``list_date`` came back
   hollow on 22 of 36 a-share reads), so a refusal here is a shape drift, not
   a missing value;
3. **land what was accepted** with the key-level dwd upsert -- unless
   ``--dry-run`` (the default), which stops after step 2 and reports what it
   *would* land.

A leg that does not answer fails the run before anything is written: a
half-landed backbone is worse than none, because the batch would then derive
its universe from whatever survived.

Usage (py313 env; needs the fuyao credentials and a warehouse):
    python scripts/ops/metadata_backbone_landing.py                 # dry run
    python scripts/ops/metadata_backbone_landing.py --write         # land it
    python scripts/ops/metadata_backbone_landing.py --asset-type a-share
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import pandas as pd

    from opendata.pipeline.runner import Window

#: The catalog pages the leg answers for; one request each (C14's contract).
DEFAULT_ASSET_TYPES = (
    "a-share",
    "a-share-index",
    "fund-etf",
    "fund-lof",
    "fund-otc",
    "fund-reits",
    "futures",
    "options",
    "forex",
)

#: The one exchange label the calendar leg publishes (C13's measurement).
DEFAULT_EXCHANGE = "CN-SSE"

#: Days of calendar the trailing-year leg is asked to cover.
DEFAULT_DAYS = 365


def _legs(
    source: str,
    asset_types: tuple[str, ...],
    exchange: str,
    window: Window,
    timeout: int,
) -> tuple[Callable[[], Sequence[object]], Callable[[], Sequence[object]]]:
    """Build the two fetch callables the backbone refresh asks.

    Args:
        source: Source label both legs route through.
        asset_types: Catalog pages to read, one request each.
        exchange: Calendar exchange label.
        window: The calendar slice to ask for. The label is the caller's
            intent only: the leg derives its neighbour dates across the
            *whole* published coverage and slices afterwards, so a row at
            ``window.start`` still names the trading day before the slice.
        timeout: Seconds per leg request.

    Returns:
        ``(fetch_instruments, fetch_calendar)`` - each runs its leg through
        the registry's full fetcher chain and hands back what it answered.
    """
    from opendata.data.protocol import FetchContext
    from opendata.pipeline.jobs import resolve_fetcher

    catalog = resolve_fetcher("instrument", source)
    calendar = resolve_fetcher("trading_calendar", source)
    ctx = FetchContext(timeout=timeout)

    def fetch_instruments() -> Sequence[object]:
        rows: list[object] = []
        for asset_type in asset_types:
            rows.extend(catalog.fetch(ctx=ctx, asset_type=asset_type))
        return rows

    def fetch_calendar() -> Sequence[object]:
        return calendar.fetch(
            ctx=ctx, exchange=exchange, start_date=window.start, end_date=window.end
        )

    return fetch_instruments, fetch_calendar


def main(argv: list[str] | None = None) -> int:
    """Read, contract-check and (on ``--write``) land the backbone."""
    from sqlalchemy import create_engine

    from opendata.core.config import settings
    from opendata.data.domains import dwd_table
    from opendata.pipeline.runner import Window
    from opendata.pipeline.templates import refresh_metadata_backbone

    parser = argparse.ArgumentParser(description="Land the metadata backbone (§4.1)")
    parser.add_argument("--source", default="ths", help="Source label of both legs (default ths)")
    parser.add_argument(
        "--asset-type",
        action="append",
        dest="asset_types",
        help="Catalog page to read (repeatable; default every published page)",
    )
    parser.add_argument("--exchange", default=DEFAULT_EXCHANGE, help="Calendar exchange label")
    parser.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="Calendar window length ending today"
    )
    parser.add_argument("--timeout", type=int, default=60, help="Seconds per leg request")
    parser.add_argument(
        "--write",
        action="store_true",
        help="Land the accepted rows (without this the run only reports)",
    )
    args = parser.parse_args(argv)
    asset_types = tuple(args.asset_types) if args.asset_types else DEFAULT_ASSET_TYPES
    today = date.today()
    window = Window(start=today - timedelta(days=max(args.days, 1) - 1), end=today)

    mode = "write" if args.write else "dry-run"
    print(f"# metadata backbone landing @ {today} (mode={mode})")
    print(f"#   source={args.source} asset_types={','.join(asset_types)} exchange={args.exchange}")
    print(f"#   window={window.start}..{window.end}")

    fetch_instruments, fetch_calendar = _legs(
        args.source, asset_types, args.exchange, window, args.timeout
    )
    engine = create_engine(settings.data_database_url) if args.write else None

    def land(domain: str, frame: pd.DataFrame, key: tuple[str, ...]) -> int:
        if engine is None:
            return len(frame)
        from opendata.pipeline.dwd_merge import DwdWriter

        return DwdWriter(engine).write(frame, table=dwd_table(domain), key=key)

    try:
        report = refresh_metadata_backbone(
            engine,
            source=args.source,
            fetch_instruments=fetch_instruments,
            fetch_calendar=fetch_calendar,
            window=window,
            land=land,
        )
    except Exception as exc:  # a leg that does not answer leaves no partial backbone
        print(f"# LEG_FAILED: {type(exc).__name__}: {exc}")
        return 2

    verb = "landed" if args.write else "would_land"
    refusals_by_domain = {
        domain: report.rejected.get(domain, ()) for domain in ("instrument", "trading_calendar")
    }
    for domain, refusals in refusals_by_domain.items():
        print(
            f"#   {dwd_table(domain)}: {verb} {report.landed[domain]} row(s), "
            f"refused {len(refusals)}"
        )
        for reason in refusals[:20]:
            print(f"#     reject {domain} {reason}")
        if len(refusals) > 20:
            print(f"#     ... {len(refusals) - 20} more refusal(s) not shown")
    if not args.write:
        print("# dry-run: nothing was written; pass --write to land these rows")
    refused = sum(len(reasons) for reasons in refusals_by_domain.values())
    landed = sum(report.landed.values())
    print(f"METADATA_BACKBONE {verb}={landed} refused={refused} mode={mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
