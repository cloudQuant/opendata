#!/usr/bin/env python3
"""Rebuild stock-adjust factors from the registered THS ODS inputs.

The command is a guarded recovery tool. It reads the configured data
warehouse and defaults to a no-write dry run; ``--apply`` is required to
upsert into ``dwd_stock_adjust``. It never accepts arbitrary table names or
connection strings.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from opendata.pipeline.factor_builder import FactorBuildResult


def _parser() -> argparse.ArgumentParser:
    """Build the bounded rebuild command arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        help="inclusive factor trade-date write bound (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        help="inclusive factor trade-date write bound (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write rebuilt rows to the configured data warehouse (default: dry run)",
    )
    return parser


def rebuild(
    engine: Engine, *, start: date | None, end: date | None, apply: bool
) -> FactorBuildResult:
    """Run the factor builder for the registry's THS daily and action tables."""
    from opendata.data.domains import ods_table
    from opendata.pipeline.factor_builder import FactorBuilder

    builder = FactorBuilder(
        engine,
        daily_table=ods_table("stock_daily", "ths"),
        action_table=ods_table("stock_action", "ths"),
    )
    return builder.build(start=start, end=end, dry_run=not apply)


def main(argv: list[str] | None = None) -> int:
    """Execute a dry run or explicitly requested factor rebuild."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.start is not None and args.end is not None and args.start > args.end:
        parser.error("--start must be on or before --end")

    engine = None
    try:
        from sqlalchemy import create_engine

        from opendata.core.config import get_settings

        active_engine = create_engine(get_settings().data_database_url)
        engine = active_engine
        result = rebuild(active_engine, start=args.start, end=args.end, apply=args.apply)
    except Exception as exc:
        # SQL/driver exception text can contain connection details. Keep the
        # CLI failure visible without echoing a URL, credential, or query.
        print(f"factor rebuild failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()

    print(
        f"mode={'apply' if args.apply else 'dry-run'} "
        f"symbols={result.symbols} planned={result.rows_planned} "
        f"written={result.rows_written} events_outside_basis={result.events_outside_basis} "
        f"legacy_rows_preserved={result.legacy_rows_preserved}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
