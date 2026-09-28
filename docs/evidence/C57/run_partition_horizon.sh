#!/usr/bin/env bash
# C57 live partition face: read-only measurement of the warehouse partition
# horizon the new executor was written for.
#
# Nothing here writes: the pass is planned, never applied. ``REORGANIZE`` is
# DDL against the production schema, so the apply half is proven by the e2e
# class in tests/test_partition_maintenance.py (its own ``_probe_*`` table);
# this archive answers the other question -- are the real tables partitioned
# at all, and how far does the yearly horizon actually lag?
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C57

{
  echo "partition-horizon.txt —— 真仓库分区上界只读面（计划半，未 apply）"
  echo "===================================================================="
  echo "round: C57"
  echo "ARCHIVE_ROUND=C57"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
  echo "command: bash docs/evidence/C57/run_partition_horizon.sh"
  echo "note: 只读面 = SELECT information_schema.PARTITIONS + SHOW CREATE TABLE +"
  echo "      SELECT COUNT(*) ... PARTITION (pmax)；三者都不改仓库，不执行 ALTER/REORGANIZE；"
  echo "      current_year / years_ahead 取的就是 executor 当场要用的那两个入参。"
  echo
  echo "===== 正文（未裁剪）====="
  echo
  python -u - <<'PY'
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import create_engine, text

from opendata.core.config import settings
from opendata.pipeline.alert_matrix import warehouse_tables
from opendata.pipeline.jobs import EXECUTABLE_KINDS
from opendata.pipeline.partitions import (
    FALLBACK_PARTITION,
    PartitionMaintainer,
    plan_yearly_partitions,
    reorganize_sql,
)
from opendata.pipeline.templates import PIPELINE_TEMPLATES, TemplateKind

row = next(t for t in PIPELINE_TEMPLATES if t.kind is TemplateKind.PARTITION_MAINTENANCE)
years_ahead = int(row.payload.get("years_ahead", 2))
current_year = date.today().year

print("A 接线（源码读数）")
print(f"   分派行 {row.name!r}: cron={row.cron} payload={dict(row.payload)}")
print(f"   kind 在 EXECUTABLE_KINDS 里 = {row.kind in EXECUTABLE_KINDS}"
      f"（不在则 register_builtin_jobs 会跳过这一行）")
print(f"   executor 当场入参: current_year={current_year} years_ahead={years_ahead}")

engine = create_engine(settings.data_database_url)
maintainer = PartitionMaintainer(engine)
tables = warehouse_tables()
partitioned = [t for t in tables if maintainer.is_partitioned(t)]

print(f"\nB 表普查（真仓库读数）")
print(f"   注册仓库表 {len(tables)} 张，其中已分区 {len(partitioned)} 张，"
      f"未分区 {len(tables) - len(partitioned)} 张（无 horizon 可维护）")

print("\nC 逐表年度上界")
with engine.connect() as conn:
    layout = {}
    for table in partitioned:
        create = conn.execute(text(f"SHOW CREATE TABLE `{table}`")).first()[1]
        m = re.search(r"PARTITION BY\s+(\w+(?:\s+COLUMNS)?)\s*\(\s*`?([a-zA-Z_]+)`?", create)
        layout[table] = (" ".join(m.group(1).split()), m.group(2)) if m else ("-", "-")
methods = sorted({method for method, _ in layout.values()})
expressions = sorted({expr for _, expr in layout.values()})
print(
    f"   分区方式 {methods} / 分区列 {expressions}"
    f"（判据要求 RANGE COLUMNS(trade_date) 年分区，这里读的是仓库自己的 SHOW CREATE TABLE）"
)
gaps: dict[str, list[tuple[str, date]]] = {}
for table in partitioned:
    state = maintainer.state(table)
    bounds = [p.upper_bound for p in state if p.upper_bound]
    last = max(bounds).isoformat() if bounds else "-"
    has_fallback = FALLBACK_PARTITION in [p.name for p in state]
    plan = plan_yearly_partitions(
        state, current_year=current_year, years_ahead=years_ahead
    )
    if plan:
        gaps[table] = plan
    print(
        f"   {table}: {len(state)} 分区  {layout[table][0]}({layout[table][1]})  "
        f"pmax={has_fallback}  最高年度上界={last}  缺={len(plan)}"
    )
    if plan:
        print(f"      plan -> {[(name, bound.isoformat()) for name, bound in plan]}")

print(
    f"\nD 汇总：待补的表 {len(gaps)} 张 / 年度分区 {sum(len(v) for v in gaps.values())} 个"
)
if gaps:
    sample = next(iter(gaps))
    statement = reorganize_sql(sample, gaps[sample])
    print(f"   样例语句（只打印，本轮不执行）for {sample}:")
    print(f"   {statement}")
else:
    print("   已分区表的年度上界全部满足 —— executor 当场 apply 数为 0")

keys_digest = "+".join(
    f"{method.replace(' ', '_')}({expr})" for method, expr in sorted(set(layout.values()))
) or "-"
print("\nE apply 代价面：``pmax`` 里现在有多少行（REORGANIZE 会重写这些行，决定这次 DDL 贵不贵）")
pmax_rows = 0
with engine.connect() as conn:
    for table in partitioned:
        try:
            count = conn.execute(text(f"SELECT COUNT(*) FROM `{table}` PARTITION (pmax)")).scalar()
            n = int(count or 0)
        except Exception as exc:
            n = -1
            print(f"   {table}: pmax 行数读取失败 -> {type(exc).__name__}")
            continue
        pmax_rows += n
        print(f"   {table}: pmax 行数 = {n}")
print(f"   合计 pmax 行数 = {pmax_rows}（0 ⇒ 补上界只动元数据，不重写数据行）")
print(
    f"\nFACTS tables={len(tables)} partitioned={len(partitioned)} "
    f"gap_tables={len(gaps)} gap_partitions={sum(len(v) for v in gaps.values())} "
    f"current_year={current_year} years_ahead={years_ahead} keys={keys_digest} "
    f"pmax_rows={pmax_rows} "
    f"dispatch_wired={'yes' if row.kind in EXECUTABLE_KINDS else 'no'}"
)
engine.dispose()
PY
  rc=$?
  echo
  echo "PARTITION_FACE_EXIT=$rc"
} | tee "$DIR/partition-horizon.txt"

exit 0
