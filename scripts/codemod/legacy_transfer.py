#!/usr/bin/env python3
"""Legacy akshare_data transfer assessment & migration (AC-15 / C3).

The pre-migration warehouse (``akshare_data``, 1026 tables) holds data
the old pipeline captured. AC-15: tables with incremental value are
migrated into the ods layer (key conflicts keep the freshly-fetched
rows - the legacy data only fills gaps), the rest stay read-only until
their domain is ported.

Operations:

* ``--assess`` - enumerate the legacy tables, report row counts and
  which of them the P0 domain mappings cover;
* ``--migrate`` - stream one legacy table into its ods counterpart
  with ``INSERT IGNORE`` (new data wins, legacy fills gaps);
* ``--verify`` - compare row counts and a sampled digest;
* ``--drop`` - drop a legacy table after a verified migration
  (requires ``--yes``).

The column mappings live in :data:`MAPPINGS`; a table without a
mapping is not migratable, only assessable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

# The script runs from scripts/codemod; make the repo root importable
# so the settings module resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

#: Known legacy -> ods migrations. ``date_columns`` are cast with
#: STR_TO_DATE; ``_source`` is stamped per mapping (akshare lineage).
MAPPINGS: dict[str, dict[str, Any]] = {
    "STOCK_ZH_A_HIST": {
        "to": "ods_stock_daily_akshare",
        "source": "akshare",
        "date_columns": ["日期"],
        "columns": [
            "日期",
            "股票代码",
            "开盘",
            "收盘",
            "最高",
            "最低",
            "成交量",
            "成交额",
            "振幅",
            "涨跌幅",
            "涨跌额",
            "换手率",
        ],
    },
}


@dataclass(frozen=True)
class Assessment:
    """One legacy table's assessment.

    Attributes:
        table: Legacy table name.
        rows: Row count.
        mapping: Whether a migration mapping exists.
    """

    table: str
    rows: int
    mapping: bool


class TransferError(RuntimeError):
    """A migration or assessment step failed."""


def connect(url: str | None, database: str | None = None) -> Engine:
    """Create an engine, optionally pointing at a different database.

    Args:
        url: SQLAlchemy URL; defaults to the app's data warehouse.
        database: Database override (``akshare_data`` for legacy).

    Returns:
        The engine.
    """
    from opendata.core.config import settings

    base = url or settings.data_database_url
    if database is not None:
        base = base.rsplit("/", 1)[0] + "/" + database
    return create_engine(base)


def assess(engine: Engine) -> list[Assessment]:
    """Enumerate the legacy tables with their row counts.

    Args:
        engine: Engine of the legacy database.

    Returns:
        The assessments, sorted by name.
    """
    with engine.connect() as connection:
        rows = connection.execute(text("SHOW TABLES")).fetchall()
        tables = sorted(str(row[0]) for row in rows)
        assessments: list[Assessment] = []
        for table in tables:
            count = connection.execute(
                text(f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608  # from SHOW TABLES
            ).scalar()
            assessments.append(
                Assessment(table=table, rows=int(count or 0), mapping=table in MAPPINGS)
            )
        return assessments


def migrate(
    engine: Engine,
    table: str,
    *,
    dry_run: bool,
    limit: int | None = None,
) -> int:
    """Stream a legacy table into its ods counterpart.

    ``INSERT IGNORE`` keeps the freshly-fetched ods rows on key
    conflicts and fills only the gaps with legacy data (AC-15 "key 冲突
    以新抓为准").

    Args:
        engine: Ods warehouse engine (the legacy source is qualified
            as ``akshare_data.<table>`` from the same server).
        table: Legacy table name (must be in :data:`MAPPINGS`).
        dry_run: Print the statement without executing.
        limit: Optional row cap for a trial run.

    Returns:
        Number of rows affected (0 in dry-run mode).

    Raises:
        TransferError: The table has no mapping, or a column is absent.
    """
    mapping = MAPPINGS.get(table)
    if mapping is None:
        raise TransferError(f"{table}: no migration mapping; assessable only")
    batch = str(uuid.uuid4())
    select_columns = []
    for column in mapping["columns"]:
        if column in mapping["date_columns"]:
            # STR_TO_DATE, not CAST(NULLIF(...) AS DATE): the CAST form
            # returns NULL for valid ISO strings under the server's
            # utf8mb4_0900 collation (a server quirk), while
            # STR_TO_DATE parses them and NULLs the empty ones.
            select_columns.append(f"STR_TO_DATE(`{column}`, '%Y-%m-%d') AS `{column}`")
        else:
            select_columns.append(f"`{column}`")
    target = mapping["to"]
    sql = (
        f"INSERT IGNORE INTO `{target}` "  # noqa: S608  # registry-derived table
        f"({', '.join(f'`{c}`' for c in mapping['columns'])}, "
        f"`_source`, `_fetched_at`, `_batch_id`) "
        f"SELECT {', '.join(select_columns)}, "
        f"'{mapping['source']}', NOW(), :batch FROM `akshare_data`.`{table}`"
    )
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if dry_run:
        print(sql)
        return 0
    with engine.begin() as connection:
        result = connection.execute(text(sql), {"batch": batch})
        return int(result.rowcount)


def verify(engine: Engine, table: str) -> dict[str, object]:
    """Compare row counts and a sampled digest after migration.

    Args:
        engine: Ods warehouse engine.
        legacy_engine: Legacy warehouse engine.
        table: Legacy table name.

    Returns:
        Counts on both sides and whether the sample digest matches.

    Raises:
        TransferError: No mapping for the table.
    """
    mapping = MAPPINGS.get(table)
    if mapping is None:
        raise TransferError(f"{table}: no migration mapping")
    target = mapping["to"]
    key = mapping["columns"][1]  # symbol-like second column
    date_col = mapping["date_columns"][0]
    with engine.connect() as connection:
        migrated = connection.execute(
            text(f"SELECT COUNT(*) FROM `{target}` WHERE `_source` = :source"),  # noqa: S608  # registry-derived table
            {"source": mapping["source"]},
        ).scalar()
    with engine.connect() as connection:
        legacy = connection.execute(
            text(f"SELECT COUNT(*) FROM `akshare_data`.`{table}`")  # noqa: S608  # literal table
        ).scalar()
        sample_legacy = connection.execute(
            text(
                f"SELECT MD5(GROUP_CONCAT(CONCAT_WS('|', `{key}`, `{date_col}`) ORDER BY `{key}`, `{date_col}`)) "  # noqa: S608, E501  # literal table
                f"FROM (SELECT DISTINCT `{key}`, `{date_col}` FROM `akshare_data`.`{table}` "
                f"ORDER BY `{key}`, `{date_col}` LIMIT 1000) t"
            )
        ).scalar()
        sample_ods = connection.execute(
            text(
                f"SELECT MD5(GROUP_CONCAT(CONCAT_WS('|', `{key}`, `{date_col}`) ORDER BY `{key}`, `{date_col}`)) "  # noqa: S608, E501  # registry-derived table
                f"FROM (SELECT DISTINCT `{key}`, `{date_col}` FROM `{target}` "
                f"WHERE `_source` = :source ORDER BY `{key}`, `{date_col}` LIMIT 1000) t"
            ),
            {"source": mapping["source"]},
        ).scalar()
    return {
        "legacy_rows": int(legacy or 0),
        "migrated_rows": int(migrated or 0),
        "sample_matches": sample_legacy == sample_ods,
    }


def drop_table(engine: Engine, table: str) -> int:
    """Drop a legacy table (post-migration, explicit ``--yes``).

    Args:
        engine: Legacy warehouse engine.
        table: Table to drop.

    Returns:
        1 when dropped.
    """
    with engine.begin() as connection:
        connection.execute(text(f"DROP TABLE IF EXISTS `{table}`"))
    return 1


def _hash_report(text_value: str) -> str:
    """Stable short hash of a report body (for archiving)."""
    return hashlib.sha256(text_value.encode("utf-8")).hexdigest()[:12]


def main(argv: list[str] | None = None) -> int:
    """Run the requested operation.

    Args:
        argv: CLI arguments (None reads ``sys.argv``).

    Returns:
        Exit code.
    """
    parser = argparse.ArgumentParser(description="Legacy akshare_data transfer (AC-15)")
    parser.add_argument("--assess", action="store_true", help="assess the legacy tables")
    parser.add_argument("--migrate", metavar="TABLE", help="migrate one legacy table into ods")
    parser.add_argument("--verify", metavar="TABLE", help="verify one migrated table")
    parser.add_argument("--drop", metavar="TABLE", help="drop a legacy table (needs --yes)")
    parser.add_argument("--to", metavar="ODS_TABLE", help="reserved: mapping decides the target")
    parser.add_argument("--dry-run", action="store_true", help="print the SQL without running")
    parser.add_argument("--limit", type=int, help="row cap for a trial migration")
    parser.add_argument("--yes", action="store_true", help="confirm a destructive --drop")
    parser.add_argument("--legacy-url", help="legacy warehouse URL override")
    parser.add_argument("--out", help="write the JSON report here")
    args = parser.parse_args(argv)

    if not (args.assess or args.migrate or args.verify or args.drop):
        parser.error("one of --assess/--migrate/--verify/--drop is required")

    legacy_engine = connect(args.legacy_url, database="akshare_data")
    engine = connect(args.legacy_url)
    report: dict[str, object] = {}
    try:
        if args.assess:
            rows = assess(legacy_engine)
            report = {
                "total": len(rows),
                "migratable": [a.table for a in rows if a.mapping],
                "by_domain": {
                    "p0_mapped": len([a for a in rows if a.mapping]),
                    "assessable": len(rows),
                },
                "sample": [
                    {"table": a.table, "rows": a.rows, "mapping": a.mapping}
                    for a in rows
                    if a.mapping or a.rows > 0
                ][:50],
            }
        elif args.migrate:
            affected = migrate(
                engine,
                args.migrate,
                dry_run=args.dry_run,
                limit=args.limit,
            )
            report = {"table": args.migrate, "rows_affected": affected, "dry_run": args.dry_run}
        elif args.verify:
            report = {"table": args.verify, **verify(engine, args.verify)}
        elif args.drop:
            if not args.yes:
                print("--drop requires --yes (destructive)", file=sys.stderr)
                return 2
            report = {"dropped": args.drop, "count": drop_table(legacy_engine, args.drop)}
    finally:
        engine.dispose()
        legacy_engine.dispose()

    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        import pathlib

        pathlib.Path(args.out).write_text(output, encoding="utf-8")
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
