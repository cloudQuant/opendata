"""EXPLAIN the default-window query against the running warehouse (AC-11|04).

The criterion says a data query without a date range must not fall into a
full-partition scan, and it names EXPLAIN as the proof. Unit tests can only
show that ``window_bounds`` fills a default in - whether MySQL then prunes the
yearly partitions is a property of the live table, so this script asks the
server: it builds the SELECT through the same code path the REST endpoint uses
(same bind parameters, same business-key columns) and reads the ``partitions``
column of the plan.

Read-only by construction: every statement issued is ``EXPLAIN`` or an
``information_schema`` select. The process exits non-zero unless every landed,
partitioned dwd table prunes, so "the window works" stays a measured claim.

Usage:
    python scripts/ops/window_partition_explain.py
    python scripts/ops/window_partition_explain.py --domain stock_daily
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import TypedDict

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import Engine, inspect, text  # noqa: E402

from opendata.data.domains import dwd_table  # noqa: E402
from opendata.pipeline.query import (  # noqa: E402
    DEFAULT_WINDOW_DAYS,
    DataQuery,
    build_data_select,
    resolve_time_field,
    window_bounds,
)


class DomainReading(TypedDict, total=False):
    """One domain's plan reading; the not-landed shape carries only ``domain``/``table``/``landed``.

    Typed rather than ``dict[str, object]`` because the caller has to join the partition names it
    reads back -- a bare ``object`` value is exactly the shape A2's mypy pass exists to catch.
    """

    domain: str
    table: str
    landed: bool
    time_field: str
    window: str
    sql: str
    params: dict[str, str]
    plan: list[dict[str, object]]
    partitions_total: int
    partitions_all: list[str]
    partitions_read: int
    partitions_used: list[str]
    pruned: bool


def registered_domains() -> list[str]:
    """Every domain the data dictionary declares, sorted."""
    from opendata.data.domains import load_domains

    return sorted(load_domains())


def table_columns(engine: Engine, table: str) -> list[str]:
    """Columns of a warehouse table, empty when it has not been migrated."""
    with engine.connect() as connection:
        try:
            return [column["name"] for column in inspect(connection).get_columns(table)]
        except Exception:  # a missing table is a normal answer for this instrument
            return []


def declared_partitions(engine: Engine, table: str) -> list[str]:
    """Partition names the server holds for a table, empty when it is not partitioned."""
    sql = (
        "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :table "
        "AND PARTITION_NAME IS NOT NULL ORDER BY PARTITION_DESCRIPTION"
    )
    with engine.connect() as connection:
        return [row[0] for row in connection.execute(text(sql), {"table": table})]


def plan_rows(engine: Engine, sql: str, params: dict[str, object]) -> list[dict[str, object]]:
    """Ask the server for the plan of one bound statement."""
    with engine.connect() as connection:
        result = connection.execute(text("EXPLAIN " + sql), params)
        return [dict(row) for row in result.mappings()]


def query_layer_digest() -> str:
    """Digest of the module that builds the SQL, so the archive says which query it planned.

    The archive is evidence about a SQL text; if that text moves, the plan in
    the archive is no longer the plan of the current query, and the probe says so.
    """
    from opendata.pipeline import query as query_module

    source = Path(query_module.__file__).with_suffix(".py")
    return hashlib.sha256(source.read_bytes()).hexdigest()[:12]


def measure_domain(engine: Engine, domain: str) -> DomainReading:
    """EXPLAIN one default-window (no ``start`` / no ``end``) query of one domain."""
    from opendata.api.data_query import _key  # the endpoint's own key derivation

    table = dwd_table(domain)
    columns = table_columns(engine, table)
    if not columns:
        return {"domain": domain, "table": table, "landed": False}
    partitions = declared_partitions(engine, table)
    query = DataQuery(domain=domain)
    start, end = window_bounds(query, today=date.today())
    sql, params = build_data_select(query, table=table, columns=columns, key=_key(domain))
    rows = plan_rows(engine, sql, params)
    read = sorted(
        {name for row in rows for name in str(row.get("partitions") or "").split(",") if name}
    )
    return {
        "domain": domain,
        "table": table,
        "landed": True,
        "time_field": resolve_time_field(domain),
        "window": f"{start}..{end}",
        "sql": sql,
        "params": {key: str(value) for key, value in params.items()},
        "plan": rows,
        "partitions_total": len(partitions),
        "partitions_all": partitions,
        "partitions_read": len(read),
        "partitions_used": read,
        "pruned": bool(partitions) and len(read) < len(partitions),
    }


def main() -> int:
    """EXPLAIN every domain asked for and fail unless each landed partitioned table prunes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domain",
        action="append",
        help="domain to EXPLAIN (repeatable); default is every registered domain",
    )
    args = parser.parse_args()

    from opendata.pipeline.jobs import warehouse_engine

    engine = warehouse_engine()
    domains = args.domain or registered_domains()
    print(f"dialect={engine.dialect.name} database={engine.url.database}")
    print(f"query_module_sha={query_layer_digest()}")
    print(f"domains_requested={len(domains)} default_window_days={DEFAULT_WINDOW_DAYS}")

    landed = 0
    pruned_yes = 0
    for domain in domains:
        reading = measure_domain(engine, domain)
        if not reading["landed"]:
            print(f"domain={domain} table={reading['table']} landed=no (skipped, not counted)")
            continue
        landed += 1
        pruned_yes += int(bool(reading["pruned"]))
        print(
            f"domain={domain} table={reading['table']} time_field={reading['time_field']} "
            f"window={reading['window']} partitions_total={reading['partitions_total']} "
            f"partitions_read={reading['partitions_read']} "
            f"read={','.join(reading['partitions_used'])} "
            f"pruned={'yes' if reading['pruned'] else 'NO'}"
        )
        print(f"  sql={reading['sql']}")
        print(f"  params={reading['params']}")
        print(f"  plan={json.dumps(reading['plan'], default=str, ensure_ascii=False)}")
    pruned_no = landed - pruned_yes
    print(f"landed_partitioned_tables={landed} pruned_yes={pruned_yes} pruned_no={pruned_no}")
    if landed == 0:
        print("explain_pruned=none (no landed dwd table to measure)")
        return 1
    if pruned_yes < landed:
        print("explain_pruned=partial")
        return 1
    print("explain_pruned=all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
