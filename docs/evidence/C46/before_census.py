#!/usr/bin/env python3
"""C46 先量：AC-18 两条判据的读数面，可复跑于任意一棵树（``--root``).

判据原文各问几件事，这里一问一读，全部来自现场而不是回忆：

* ``AC-18|01`` —— 各域最新数据日期与滞后天数**可查**（处理函数交出哪些键），
  缺失**触发告警**（谁调用判定函数、谁把告警投出去、调度是否真挂上 cron）。
* ``AC-18|02`` —— 目录按域组织，五个读数（覆盖标的数／时间范围／各源最近更新／
  新鲜度／质量标记）在载荷里给不给得出、页面读不读。

跑在 ``git archive HEAD`` 解出的树上就是「本轮动手前」，跑在工作树就是「动手后」，
两档读数之间差了什么即本轮做了什么——不靠叙述，靠同一份代码的两次运行。

只读：不写文件、不改配置、不连数仓（数仓侧的真值另见 ``warehouse_census.py``）。
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# 判据原文点名五个读数，每个读数允许的后端键名（任一命中即「载荷给得出」）。
READING_KEYS: dict[str, tuple[str, ...]] = {
    "覆盖标的数": ("symbols", "symbol_count"),
    "时间范围": ("start", "end", "time_range"),
    "各源最近更新": ("sources",),
    "新鲜度": ("latest", "lag_days", "status"),
    "质量标记": ("quality", "diff_flagged"),
}


def read(root: Path, rel: str) -> str:
    """Read one repo file from ``root`` as text."""
    return (root / rel).read_text(encoding="utf-8")


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def top_function(source: str, name: str) -> FunctionNode:
    """Find a module-level function node by name, so a route reads as written."""
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise LookupError(f"no module-level {name}() in this tree")


def dict_keys_of(func: FunctionNode) -> list[str]:
    """String keys built by dict literals inside ``func``, in first-seen order."""
    keys: list[str] = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Dict):
            continue
        named = (slot.value for slot in node.keys if isinstance(slot, ast.Constant))
        for value in named:
            if isinstance(value, str) and value not in keys:
                keys.append(value)
    return keys


def module_functions(source: str) -> dict[str, FunctionNode]:
    """Every module-level function in ``source``, keyed by name."""
    found: dict[str, FunctionNode] = {}
    for node in ast.parse(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            found[node.name] = node
    return found


def call_closure(source: str, entry: str) -> list[str]:
    """``entry`` plus every module-level function it reaches, transitively.

    The catalog row is assembled across a few helpers, so reading only
    ``data_catalog``'s own dict literals under-reports the payload -- a
    face that misses half the keys would be a face nobody could trust.
    Edges are *name* references rather than call sites because the
    coverage reader is handed to ``asyncio.to_thread``, and a callback is
    still part of what the route runs.
    """
    funcs = module_functions(source)
    seen: list[str] = [entry]
    queue = [entry]
    while queue:
        current = queue.pop()
        for node in ast.walk(funcs[current]):
            if not isinstance(node, ast.Name):
                continue
            if node.id in funcs and node.id not in seen:
                seen.append(node.id)
                queue.append(node.id)
    return seen


def section(title: str) -> None:
    """Print a section header."""
    print(f"\n--- {title} ---")


def catalogue_face(root: Path) -> list[str]:
    """AC-18|02：目录每域行的键面 ↔ 判据五个读数 ↔ 前端 CatalogEntry 字段面."""
    section("1. AC-18|02 目录每域行的键面（AST 读 data_catalog 实际构造的 dict）")
    body = read(root, "opendata/api/data_query.py")
    reached = call_closure(body, "data_catalog")
    every = sorted({k for name in reached for k in dict_keys_of(module_functions(body)[name])})
    print(f"  data_catalog 及其调用闭包（{reached}）构造出的键共 {len(every)} 个：{every}")
    missing: list[str] = []
    for label, hints in READING_KEYS.items():
        hit = [h for h in hints if h in every]
        if not hit:
            missing.append(label)
        print(f"  {label:8}：{'有（' + '/'.join(hit) + '）' if hit else '无'}")
    print(f"  判据五项：缺 {len(missing)} 项（{missing or '无'}）")

    ts = read(root, "frontend/src/api/catalog.ts")
    iface = re.search(r"interface CatalogEntry \{(.*?)\n\}", ts, re.S)
    front = re.findall(r"^\s*(\w+)\??:", iface.group(1), re.M) if iface else []
    print(f"  CatalogEntry 字段 {len(front)} 个：{front}")
    print(f"  前端声明而后端不发的字段：{sorted(set(front) - set(every)) or '无'}")
    print(f"  后端发而前端不读的字段：{sorted(set(every) - set(front)) or '无'}")
    return missing


def leg_face(root: Path) -> None:
    """每条源腿的 freshness 列取不取到，以及目录问的是哪张表."""
    section("2. 源腿 mapping 面：新鲜度列取不取到 + 目录问的是哪张表")
    sys.path.insert(0, str(root))
    from opendata.data.mapping import require_domain_mapping
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry
    from opendata.pipeline.freshness import freshness_field

    register_providers()
    try:
        from opendata.pipeline.alert_matrix import registered_legs

        legs = registered_legs()
        branch = "alert_matrix.registered_legs —— 含「注册了却一条腿都没有」的域"
    except ImportError:  # 动手前还没有 alert_matrix 这一模块
        grouped: dict[str, set[str]] = {}
        for cap in get_registry().capabilities():
            grouped.setdefault(cap.domain, set()).add(cap.source)
        legs = {domain: tuple(sorted(sources)) for domain, sources in grouped.items()}
        branch = "registry 能力腿 —— 只数有腿的域（动手前没有 registered_legs 可问）"
    total = sum(len(sources) for sources in legs.values())
    unmapped: list[str] = []
    mapped: list[str] = []
    for domain, sources in legs.items():
        field = freshness_field(domain)
        for source in sources:
            try:
                mapping = require_domain_mapping(source, domain)
            except LookupError:
                unmapped.append(f"{domain}/{source}: 无该域 mapping")
                continue
            if field not in mapping.fields:
                unmapped.append(f"{domain}/{source}: mapping 里没有新鲜度字段 {field}")
                continue
            column = mapping.fields[field].source_column
            mapped.append(f"{domain}/{source}: {field} ← 源列 {column}")
    with_legs = sum(1 for sources in legs.values() if sources)
    print(f"  域 {len(legs)} 个（其中有腿的 {with_legs} 个）、源腿 {total} 条")
    print(f"    读数分支：{branch}")
    print(f"    无字段映射 ⇒ ods_freshness 必 LookupError、量不出新鲜度：{len(unmapped)} 条")
    print(f"    映射取得到源列：{len(mapped)} 条")
    for line in unmapped[:8]:
        print(f"      未映射：{line}")
    for line in mapped[:4]:
        print(f"      已映射：{line}")

    body = read(root, "opendata/api/data_query.py")
    leg_builder = "_source_leg" if "_source_leg" in body else "data_catalog"
    asked = ast.get_source_segment(body, top_function(body, leg_builder)) or ""
    uses_ods = "ods_table(domain, source)" in asked
    print(
        f"  目录为「各源最近更新」查的表（{leg_builder}）："
        f"{'ods_table(domain, source)' if uses_ods else '只有 dwd_table —— 源列不在其上'}"
    )
    if not uses_ods:
        print(
            "    ⇒ 动手前：新鲜度按 dwd 表问、列名却按 mapping 的源列取，"
            "      两者不同源 ⇒ 每条腿只能读成 missing"
        )


def alert_face(root: Path) -> None:
    """AC-18|01：判定函数存在 ≠ 有人调用；调用 ≠ 投得出去."""
    section("3. AC-18|01 缺失触发告警：生产者 / 投递 / 通道")
    producers: list[str] = []
    delivered: list[str] = []
    tree = sorted((root / "opendata").rglob("*.py")) + sorted((root / "scripts").rglob("*.py"))
    for path in tree:
        text = path.read_text(encoding="utf-8")
        rel = str(path.relative_to(root))
        if re.search(r"\bevaluate_alerts\(|\bcollect_freshness\(|\brun_alert_matrix\(", text):
            producers.append(rel)
        if "broadcast" in text and "freshness" in text:
            delivered.append(rel)
    print(f"  生产新鲜度告警（调用判定/采集/矩阵）的非测试文件：{producers or '无（0 个）'}")
    print(f"  同时把 broadcast 与 freshness 写在一起的投递文件：{delivered or '无（0 个）'}")
    jobs = read(root, "opendata/pipeline/jobs.py")
    for name in ("_execute_template", "_execute_freshness"):
        try:
            body = ast.get_source_segment(jobs, top_function(jobs, name)) or ""
        except LookupError:
            print(f"    jobs.py::{name} 不存在")
            continue
        print(
            f"    jobs.py::{name} 存在，"
            f"引用 broadcast={'yes' if 'broadcast' in body else 'no'}，"
            f"引用 FRESHNESS={'yes' if 'FRESHNESS' in body else 'no'}"
        )


def schedule_face(root: Path) -> None:
    """调度声明的模板 kind ↔ 真能被执行器接住的 kind."""
    section("4. 调度面：schedules.yaml 声明的 kind ↔ jobs.py EXECUTABLE_KINDS")
    declared = re.findall(r"kind:\s*(\w+)", read(root, "opendata/pipeline/schedules.yaml"))
    jobs = read(root, "opendata/pipeline/jobs.py")
    block = re.search(r"EXECUTABLE_KINDS\s*=\s*frozenset\(\{(.*?)\}\)", jobs, re.S)
    executable = set(re.findall(r"TemplateKind\.(\w+)", block.group(1))) if block else set()
    print(f"  schedules.yaml 声明模板 kind {len(declared)} 次：{sorted(set(declared))}")
    print(f"  EXECUTABLE_KINDS = {sorted(executable)}")
    upper = {name.upper() for name in declared}
    print(f"  schedules.yaml 声明的 kind（大写归一后）：{sorted(upper)}")
    stranded = sorted(upper - executable)
    print(f"  声明了却没有执行器的 kind：{stranded or '无'}")
    print("    （这些 kind 在 register_builtin_jobs 里被 continue 掉，任务从未挂上 cron）")


def page_face(root: Path) -> None:
    """AC-18|03：目录页与「数据接口」页在路由上是不是一件事."""
    section("5. AC-18|03 合并面：路由标题 / 目录页是否读到函数级明细")
    router = read(root, "frontend/src/router/index.ts")
    titles = re.findall(r"title: '([^']+)'", router)
    data_titles = [title for title in titles if "数据接口" in title]
    print(f"  标题含「数据接口」的路由 {len(data_titles)} 条：{data_titles}")
    view = read(root, "frontend/src/views/DataCatalogView.vue")
    reaches_detail = "data/interfaces" in view
    print(f"  DataCatalogView 是否请求函数级明细：{'yes' if reaches_detail else 'no'}")
    components = re.findall(r"views/([A-Za-z]+\.vue)", router)
    print(f"  路由挂载的视图（前 8 个）：{components[:8]}")
    near = re.search(r"path: 'scripts'[\s\S]{0,200}?", router)
    excerpt = near.group(0).strip()[:110] if near else "未找到"
    print(f"  /scripts 路由附近文本：{excerpt}")


def main() -> int:
    """Run every face against one tree and print the readings."""
    parser = argparse.ArgumentParser(description="C46 AC-18 census")
    parser.add_argument("--root", default=".", type=Path, help="tree to measure")
    args = parser.parse_args()
    root = args.root.resolve()
    print("=" * 78)
    print(f"C46 读数  root={root}")
    print("=" * 78)
    catalogue_face(root)
    leg_face(root)
    alert_face(root)
    schedule_face(root)
    page_face(root)
    print("\n读数完毕（本脚本只读不写）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
