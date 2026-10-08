#!/usr/bin/env python3
"""Explicitly backfill one ODS source/window into its DWD table."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

# Allow direct execution from a checkout without an editable installation.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from sqlalchemy import create_engine, pool  # noqa: E402

from opendata.core.config import settings  # noqa: E402
from opendata.pipeline.dwd_backfill import DwdBackfillError, backfill_dwd  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Sequence


def _date_arg(value: str) -> date:
    """Parse one required ISO calendar date."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}") from exc


def _parser() -> argparse.ArgumentParser:
    """Build the explicit-selection command interface."""
    parser = argparse.ArgumentParser(
        description=(
            "Backfill an explicitly selected registered ODS source into its existing DWD table. "
            "The command does not create tables or call providers."
        )
    )
    parser.add_argument("--domain", required=True, help="registered warehouse domain")
    parser.add_argument("--source", required=True, help="registered source leg; 'auto' is refused")
    parser.add_argument("--start", required=True, type=_date_arg, help="inclusive YYYY-MM-DD")
    parser.add_argument("--end", required=True, type=_date_arg, help="inclusive YYYY-MM-DD")
    parser.add_argument(
        "--page-size",
        type=int,
        default=2_000,
        help="maximum ODS rows held per page (default: 2000)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested backfill and print a JSON outcome."""
    args = _parser().parse_args(argv)
    engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
    try:
        stats = backfill_dwd(
            engine,
            domain=args.domain,
            source=args.source,
            start=args.start,
            end=args.end,
            page_size=args.page_size,
        )
    except DwdBackfillError as exc:
        print(
            json.dumps(
                {"status": "failed", "error": str(exc), "stats": exc.stats.as_dict()},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except (LookupError, ValueError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    print(json.dumps({"status": "complete", "stats": stats.as_dict()}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
