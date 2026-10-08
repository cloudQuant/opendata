#!/usr/bin/env python
"""Import timezone-aware minute bars from a local CSV file."""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from opendata.data.minute_archive import (  # noqa: E402
    MINUTE_DOMAIN_ASSET_CLASSES,
    MINUTE_PERIODS,
    RECORD_COLUMNS,
    MinuteArchiveIndexError,
    MinuteArchiveIntegrityError,
    ingest_minute_shard,
    validate_minute_identity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy import Engine


def _parser() -> argparse.ArgumentParser:
    """Build the explicit, provider-free import command parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Import local minute-bar CSV shards into the Parquet archive. "
            "Input rows must be sorted by symbol then timezone-aware timestamp."
        )
    )
    parser.add_argument("--input", required=True, type=Path, help="CSV file to import")
    parser.add_argument(
        "--domain",
        required=True,
        choices=sorted(MINUTE_DOMAIN_ASSET_CLASSES),
        help="Bar-capable domain",
    )
    parser.add_argument("--source", required=True, help="Explicit source identifier")
    parser.add_argument("--period", required=True, choices=MINUTE_PERIODS)
    parser.add_argument("--archive-root", type=Path, help="Override DATA_DIR/minute_archive")
    return parser


def import_csv(
    path: Path,
    *,
    engine: Engine,
    root: Path,
    domain: str,
    source: str,
    period: str,
) -> tuple[int, int]:
    """Stream sorted CSV records and publish one symbol/day shard at a time.

    Args:
        path: Input CSV with the exact canonical minute columns.
        engine: Isolated or production main-database metadata engine.
        root: Minute archive root.
        domain: Supported Bar domain.
        source: Explicit source identifier.
        period: Supported minute period.

    Returns:
        Number of committed shards and rows.

    Raises:
        ValueError: If the CSV contract or row ordering is invalid.
    """
    validate_minute_identity(domain, "validation-symbol", source, period)
    shard_identity: tuple[str, object] | None = None
    shard_records: list[dict[str, str]] = []
    committed_shards = 0
    imported_rows = 0
    previous_order: tuple[str, datetime] | None = None

    def flush() -> None:
        nonlocal committed_shards, imported_rows, shard_records
        if shard_identity is None or not shard_records:
            return
        symbol, _day = shard_identity
        batch_size = len(shard_records)
        ingest_minute_shard(
            engine,
            root,
            domain=domain,
            symbol=symbol,
            source=source,
            period=period,
            records=shard_records,
        )
        committed_shards += 1
        imported_rows += batch_size
        shard_records = []

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(RECORD_COLUMNS):
            raise ValueError(f"CSV header must be exactly: {','.join(RECORD_COLUMNS)}")
        for row_number, row in enumerate(reader, start=2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"CSV row {row_number} has a missing or extra value")
            symbol = row["symbol"]
            timestamp_text = row["timestamp"]
            try:
                timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"CSV row {row_number} timestamp is not ISO 8601") from exc
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError(f"CSV row {row_number} timestamp must include a timezone")
            order_key = (symbol, timestamp.astimezone(timezone.utc))
            if previous_order is not None and order_key < previous_order:
                raise ValueError("CSV rows must be sorted by symbol then timestamp")
            previous_order = order_key
            identity = (symbol, order_key[1].date())
            if shard_identity is not None and identity != shard_identity:
                flush()
            shard_identity = identity
            shard_records.append(row)
    flush()
    return committed_shards, imported_rows


def main(argv: Sequence[str] | None = None) -> int:
    """Run the local CSV importer without making upstream requests."""
    parser = _parser()
    args = parser.parse_args(argv)
    from opendata.data.minute_archive import get_minute_archive_engine, get_minute_archive_root

    engine = get_minute_archive_engine()
    root = args.archive_root or get_minute_archive_root()
    try:
        shards, rows = import_csv(
            args.input,
            engine=engine,
            root=root,
            domain=args.domain,
            source=args.source,
            period=args.period,
        )
    except (OSError, ValueError, MinuteArchiveIndexError, MinuteArchiveIntegrityError) as exc:
        print(f"minute archive import failed: {exc}", file=sys.stderr)
        return 2
    finally:
        engine.dispose()
    print(f"minute archive import finished: shards={shards}, rows={rows}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
