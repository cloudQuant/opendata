#!/usr/bin/env bash
# C58 live ods face. C57 measured the partition horizon through
# ``alert_matrix.warehouse_tables()`` and read "20 registered tables"; that
# number is a lazy registry read, and in a bare process the registry holds no
# capabilities at all, so the 33 ``ods_*`` legs were never in the census. This
# round takes the same reading with ``register_providers()`` first -- the way
# the pipeline resolves a fetcher (jobs.py:204) -- so the face answers both
# questions: what is the real table set, and what does it look like inside the
# warehouse.
#
# Nothing here writes. Every statement is a SELECT against information_schema,
# SHOW CREATE TABLE, or COUNT(*) ... PARTITION (pmax); no ALTER/REORGANIZE, no
# INSERT, no DROP. The apply half stays gated on an explicit confirmation.
set -uo pipefail

export PATH="$HOME/opt/anaconda3/envs/py313/bin:$PATH"
export PYTHONPATH=.

cd "$(git rev-parse --show-toplevel)"
DIR=docs/evidence/C58

{
  echo "ods-face.txt —— ods 落库面 + 分区面（真仓库只读，注册表按 register_providers 后取值）"
  echo "===================================================================="
  echo "round: C58"
  echo "ARCHIVE_ROUND=C58"
  echo "date: $(date '+%Y-%m-%d %H:%M %z')"
  echo "branch: $(git rev-parse --abbrev-ref HEAD)   head: $(git rev-parse --short HEAD)（+ 本轮未提交的改动）"
  echo "python: $(python -V 2>&1)"
  echo "command: bash docs/evidence/C58/run_ods_face.sh"
  echo "note: 三类语句全是只读（information_schema SELECT / SHOW CREATE TABLE / COUNT(*) PARTITION），"
  echo "      不执行 ALTER/REORGANIZE/INSERT/DROP；表名集合取法与 executor 一致："
  echo "      register_providers() -> alert_matrix.warehouse_tables()。"
  echo
  echo "===== 正文（未裁剪）====="
  echo
  python -u - <<'PY'
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import create_engine, inspect, text

from opendata.core.config import settings
from opendata.data.domains import load_domains
from opendata.data.providers import register_providers
from opendata.pipeline.alert_matrix import registered_legs, warehouse_tables
from opendata.pipeline.partitions import (
    FALLBACK_PARTITION,
    PartitionMaintainer,
    plan_yearly_partitions,
)

new_caps = register_providers()
legs = registered_legs()
tables = warehouse_tables()
ods = [t for t in tables if t.startswith("ods_")]
current_year = date.today().year
years_ahead = 2

print("A 注册面（先 register_providers，再读 warehouse_tables）")
print(f"   本次新注册能力 {len(new_caps)} 条（已注册则 0；幂等）")
print(f"   注册域 {len(legs)} 个，其中有至少一条 (domain, source) 腿的 {sum(1 for s in legs.values() if s)} 个")
print(f"   注册仓库表 {len(tables)} 张 = dwd {len(tables) - len(ods)} + ods {len(ods)}")
print(
    "   对照 C57 那一次读数（未调 register_providers 的裸进程）：表集合退化成 20 张 dwd，"
    "33 条 ods 腿整个不在 —— 本文件与 C57 档案的差值就是这条"
)

engine = create_engine(settings.data_database_url)
inspector = inspect(engine)
live = set(inspector.get_table_names())
maintainer = PartitionMaintainer(engine)

print("\nB 真仓库对账（opendata_data 当场有什么）")
missing = [t for t in tables if t not in live]
extra = sorted(t for t in live if t.startswith(("ods_", "dwd_")) and t not in set(tables))
warehouse_live = [t for t in live if t.startswith(("ods_", "dwd_"))]
print(f"   仓库里 ods_/dwd_ 前缀的真实表 {len(warehouse_live)} 张；注册表里尚未建出的 {len(missing)} 张")
print(f"   未注册却存在的 ods_/dwd_ 表 {len(extra)} 张 {extra if extra else ''}")

print("\nC ods 表命名与形状（判据：ods_<domain>_<source> + 源原始列 + 三元组 + 业务 key 主键）")
known_domains = set(load_domains())
TRIO = ("`_source`", "`_fetched_at`", "`_batch_id`")
name_re = re.compile(r"^ods_(?P<domain>[a-z_]+)_(?P<source>[a-z]+)$")
live_ods = sorted(t for t in live if t.startswith("ods_"))
trio_ok = named_ok = key_pk = 0
autokey = 0
for table in live_ods:
    create = engine.connect().execute(text(f"SHOW CREATE TABLE `{table}`")).first()[1]
    has_trio = all(fragment in create for fragment in TRIO)
    match = name_re.match(table)
    domain = "-"
    if match:
        domain = match.group("domain")
        for candidate in sorted(known_domains, key=len, reverse=True):
            if domain.startswith(candidate):
                domain = candidate
                break
    pk = list(inspector.get_pk_constraint(table).get("constrained_columns") or [])
    auto = "AUTO_INCREMENT" in create
    trio_ok += has_trio
    named_ok += bool(match) and domain in known_domains
    key_pk += bool(pk)
    autokey += auto
    print(
        f"   {table}: 域 {domain if domain in known_domains else '?'} / 三元组 {'全' if has_trio else '缺'}"
        f" / 主键 {pk} / 自增 id {'有' if auto else '无'}"
    )
print(f"   汇总：命名合规 {named_ok}/{len(live_ods)}，三元组齐全 {trio_ok}/{len(live_ods)}，"
      f"业务 key 主键 {key_pk}/{len(live_ods)}，带 AUTO_INCREMENT {autokey}/{len(live_ods)}")

print("\nD 分区面（注册集合内、当场已分区的表）")
partitioned = [t for t in tables if maintainer.is_partitioned(t)]
gaps: dict[str, list[tuple[str, date]]] = {}
pmax_rows_total = 0
layout = {}
for table in partitioned:
    state = maintainer.state(table)
    names = [p.name for p in state]
    bounds = [p.upper_bound for p in state if p.upper_bound]
    last = max(bounds).isoformat() if bounds else "-"
    create = engine.connect().execute(text(f"SHOW CREATE TABLE `{table}`")).first()[1]
    found = re.search(
        r"PARTITION BY\s+(\w+(?:\s+COLUMNS)?)\s*\(\s*`?([^\s()`]+)`?", create
    )
    kind, column = (" ".join(found.group(1).split()), found.group(2)) if found else ("-", "-")
    layout[table] = (kind, column)
    plan = plan_yearly_partitions(
        state, current_year=current_year, years_ahead=years_ahead
    )
    if plan:
        gaps[table] = plan
    with engine.connect() as conn:
        count = int(
            conn.execute(
                text(f"SELECT COUNT(*) FROM `{table}` PARTITION ({FALLBACK_PARTITION})")
            ).scalar()
            or 0
        )
    pmax_rows_total += count
    print(
        f"   {table}: {len(state)} 分区 pmax={FALLBACK_PARTITION in names} "
        f"{kind}({column}) 最高上界={last} 缺={len(plan)} pmax 行数={count}"
    )
    if plan:
        print(f"      plan -> {[(n, b.isoformat()) for n, b in plan]}")
unpartitioned_live = [t for t in live_ods if t not in partitioned]
print(
    f"   仓库里存在但未分区的 ods 表 {len(unpartitioned_live)} 张："
    f"{unpartitioned_live if unpartitioned_live else '（无）'}"
)
print(
    f"\nE 汇总：待补表 {len(gaps)} 张 / 年度分区 {sum(len(v) for v in gaps.values())} 个 / "
    f"pmax 现有行数合计 {pmax_rows_total}（0 ⇒ 补上界只动元数据；>0 ⇒ REORGANIZE 会重写这些行）"
)

print("\nF 两套 alembic 环境各自的版本表（AC-8|02：仓库 DDL 独立、启动不建表）")
control = create_engine(settings.database_url_sync)
with control.connect() as conn:
    schema = conn.execute(text("SELECT DATABASE()")).scalar()
ctrl_tables = set(inspect(control).get_table_names())
data_tables = set(inspect(engine).get_table_names())
for label, holder, table_name in (
    ("应用（控制库）", ctrl_tables, "alembic_version"),
    ("仓库（数据库）", data_tables, "alembic_version_data"),
):
    if table_name not in holder:
        print(f"   {label}: 版本表 {table_name} 不存在")
        continue
    engine_used = control if holder is ctrl_tables else engine
    with engine_used.connect() as conn:
        rows = [
            r[0]
            for r in conn.execute(text(f"SELECT version_num FROM {table_name} ORDER BY 1"))
        ]
    print(f"   {label}: {table_name}（schema={schema if engine_used is control else 'opendata_data'}）"
          f" 当前版本 {rows}")
print(
    f"   控制库里的 ods_/dwd_ 表 {sum(1 for t in ctrl_tables if t.startswith(('ods_', 'dwd_')))} 张"
    "（>0 则启动建表摸到了仓库层）"
)
control.dispose()
engine.dispose()

print(
    f"\nFACTS registered={len(tables)} ods_registered={len(ods)} legs={sum(1 for s in legs.values() if s)} "
    f"live_ods={len(live_ods)} ods_named={named_ok} ods_trio={trio_ok} ods_key_pk={key_pk} "
    f"ods_autokey={autokey} partitioned={len(partitioned)} gap_tables={len(gaps)} "
    f"gap_partitions={sum(len(v) for v in gaps.values())} pmax_rows={pmax_rows_total} "
    f"current_year={current_year} years_ahead={years_ahead} ctrl_warehouse_tables="
    f"{sum(1 for t in ctrl_tables if t.startswith(('ods_', 'dwd_')))}"
)
PY
  rc=$?
  echo
  echo "ODS_FACE_EXIT=$rc"
} | tee "$DIR/ods-face.txt"

exit 0
