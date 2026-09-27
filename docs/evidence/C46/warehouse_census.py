#!/usr/bin/env python3
"""C46 数仓侧读数：AC-18 的新鲜度在**真数仓**上量得出什么（只读）.

判据说「各域最新数据日期与滞后天数可查」，离线面（SQLite 夹具）证明的是接口形状；
这一档证明的是**现场**：数仓里真有哪些表、日历给出的期望日是哪一天、每条腿读出来是
fresh / stale / missing 还是根本量不出（无字段映射）。

只读：全程只有 ``SELECT`` 与 ``information_schema`` 查询，没有 DDL、没有写入、
不打印连接串（URL 里含口令）。跑法见 ``docs/evidence/C46/README.md``。
"""

from __future__ import annotations

import argparse
import re
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def safe_ident(name: str, kind: str) -> str:
    """Return ``name`` when it is a plain identifier, else refuse to build SQL.

    The table names come from the registry rather than from a request, but a
    reading script that interpolates an unvalidated name into SQL is a habit
    worth refusing to form.
    """
    if not IDENTIFIER.match(name):
        raise ValueError(f"refusing to use {kind} as an identifier: {name!r}")
    return name


def table_census(engine: Engine) -> dict[str, list[str]]:
    """Warehouse table names grouped by layer, straight off information_schema."""
    from sqlalchemy import text

    grouped: dict[str, list[str]] = {"dwd": [], "ods": [], "other": []}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() ORDER BY table_name"
            )
        ).all()
    for row in rows:
        name = str(row[0])
        bucket = "dwd" if name.startswith("dwd_") else "ods" if name.startswith("ods_") else "other"
        grouped[bucket].append(name)
    return grouped


def row_count(engine: Engine, name: str) -> int:
    """Exact ``COUNT(*)`` of one table (the warehouse is small enough to count)."""
    from sqlalchemy import func, select, table

    with engine.connect() as conn:
        stmt = select(func.count()).select_from(table(safe_ident(name, "table")))
        value = conn.execute(stmt).scalar_one()
    return int(value)


def freshness_reading(engine: Engine, expected: date) -> tuple[dict[str, int], list[str]]:
    """Run the alert matrix's collection against the warehouse, read-only.

    Returns:
        A status tally plus the per-leg lines, so the reading can be re-checked
        by hand rather than only counted.
    """
    from opendata.pipeline.alert_matrix import collect_freshness, registered_legs

    declared = sum(len(sources) for sources in registered_legs().values())
    reports, scope = collect_freshness(engine, expected=expected)
    print(f"    注册源腿 {declared} 条（本进程 register_providers 后），参与读数 {len(reports)} 条")
    tally: dict[str, int] = {
        "fresh": 0,
        "stale": 0,
        "missing": 0,
        "unmapped_legs": scope.unmapped_legs,
        "domains": scope.domains,
        "source_legs": scope.source_legs,
    }
    lines: list[str] = []
    for report in reports:
        tally[report.status] = tally.get(report.status, 0) + 1
        lines.append(
            f"    {report.domain:<22} {str(report.source or '-'):<9} "
            f"{report.field:<11} latest={report.latest or '-'} "
            f"lag={report.lag_days if report.lag_days is not None else '-'} {report.status}"
        )
    return tally, lines


def main() -> int:
    """Print the warehouse-side readings for AC-18."""
    parser = argparse.ArgumentParser(description="C46 warehouse census")
    parser.add_argument("--today", type=date.fromisoformat, help="day to ask the calendar about")
    args = parser.parse_args()

    from opendata.api.data_query import get_warehouse_engine
    from opendata.data.providers import register_providers
    from opendata.pipeline.trading_calendar import resolve_calendar

    # An empty registry would silently read as "no source leg is stale", so the
    # legs are counted here and printed next to the readings.
    register_providers()
    engine = get_warehouse_engine()
    today = args.today or date.today()
    print("=" * 78)
    print(f"C46 数仓读数  dialect={engine.dialect.name}  database={engine.url.database}")
    print("=" * 78)

    grouped = table_census(engine)
    print("\n--- 表面（information_schema.tables）")
    n_dwd, n_ods, n_other = len(grouped["dwd"]), len(grouped["ods"]), len(grouped["other"])
    print(f"    dwd {n_dwd} 张 / ods {n_ods} 张 / 其他 {n_other} 张")
    for name in grouped["dwd"]:
        print(f"    {name}: {row_count(engine, name)} 行")
    print(f"    ods 表清单：{grouped['ods'] or '（无）'}")

    calendar = resolve_calendar(engine)
    coverage = calendar.coverage
    expected = calendar.expected_data_date(today)
    print(
        f"\n--- 日历面：覆盖 {coverage[0] if coverage else '-'}..{coverage[1] if coverage else '-'}"
        f"，today={today} ⇒ expected_data_date={expected}"
    )

    tally, lines = freshness_reading(engine, expected)
    print(
        f"\n--- 新鲜度读数：域 {tally['domains']} 个 / 有报告的源腿 {tally['source_legs']} 条 / "
        f"无映射量不出 {tally['unmapped_legs']} 条"
    )
    print(f"    状态合计：fresh={tally['fresh']} stale={tally['stale']} missing={tally['missing']}")
    for line in lines:
        print(line)
    print("\n读数完毕（本脚本只读不写）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
