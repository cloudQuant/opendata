"""Read-only pre-handover comparison of the AFI and OpenData control planes.

Stage 4 of the port copies ``ak_*`` rows into the bare ``data_*`` tables. Before
any copy is authorised, this tool measures what is actually on each side so the
owner can approve a counted hand-over instead of a blind one. It never writes:
both engines are opened read-only and only ``SELECT`` aggregates are issued.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy import table as sa_table

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

SCHEMA_DIFF = (
    Path(__file__).resolve().parents[2] / "docs" / "afi-data-management-port" / "schema-diff.json"
)


def _count(engine: Engine, table: str) -> int | None:
    """Return the row count of one table, or None when the table is absent."""
    with engine.connect() as conn:
        if not inspect(conn).has_table(table):
            return None
        stmt = select(func.count()).select_from(sa_table(table))
        return int(conn.execute(stmt).scalar() or 0)


def compare_pair(
    source: Engine, target: Engine, afi_table: str, opendata_table: str
) -> dict[str, Any]:
    """Compare one table pair by row count; missing tables are reported, not guessed."""
    src = _count(source, afi_table)
    dst = _count(target, opendata_table)
    if src is None or dst is None:
        verdict = "missing_table"
    elif src == dst:
        verdict = "count_match"
    elif dst == 0:
        verdict = "target_empty"
    else:
        verdict = "count_differs"
    return {
        "afi_table": afi_table,
        "opendata_table": opendata_table,
        "source_rows": src,
        "target_rows": dst,
        "verdict": verdict,
    }


def main(argv: list[str] | None = None) -> int:
    """Print the per-pair comparison as JSON; exit 0 only when every pair matches."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-url", required=True, help="SQLAlchemy URL of the AFI database")
    parser.add_argument(
        "--target-url", required=True, help="SQLAlchemy URL of the OpenData database"
    )
    parser.add_argument("--out", type=Path, default=None, help="optional JSON report path")
    args = parser.parse_args(argv)

    pairs = json.loads(SCHEMA_DIFF.read_text(encoding="utf-8"))["tables"]
    source = create_engine(args.source_url)
    target = create_engine(args.target_url)
    try:
        rows = [compare_pair(source, target, p["afi_table"], p["opendata_table"]) for p in pairs]
    finally:
        source.dispose()
        target.dispose()

    report = {"pairs": rows, "all_match": all(r["verdict"] == "count_match" for r in rows)}
    rendered = json.dumps(report, indent=2, ensure_ascii=False)
    print(rendered)
    if args.out:
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["all_match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
