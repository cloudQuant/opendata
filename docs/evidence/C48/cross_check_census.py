#!/usr/bin/env python3
"""C48 现场读数：双源校对在生产调用面与真库上各量得出什么（只读）.

判据原文（AC-9 §2 三条）问的是「注入差异样本被**校对作业**捕获」「**告警治理**：白名
单容忍项生效、相同差异不重复告警、差异率突增才升级」「告警**双通道**：SMTP 邮件 + WS
``data.diff_alert`` 事件广播」。这三条在离线单元面上都跑过（``tests/test_cross_check*.py``
``test_alerts.py``），所以「判定函数存在」不是问题；问题是**谁在真机上叫它们跑**。本轮把
五件事分别量清：

1. **调度面**：四种 ``TemplateKind`` 里哪几种进了 ``EXECUTABLE_KINDS``，以及每条调度声明
   的 payload 键被哪个执行器读过——声明了却没人读的键就是假配置。
2. **生产调用面**：用 AST（不是 grep）扫 ``opendata/`` 与 ``scripts/``，把
   ``CrossCheckService``/``AlertPolicy`` 的**构造点及其关键字**、``diff_alert_message`` 与
   ``publish_diff_alert`` 的**引用点**原样列出来。判定与投递之间断在哪一层，只有列调用点
   才看得见。
3. **策略状态面**：``AlertPolicy`` 的三问各自依赖什么状态、那份状态在生产路径上的**生命周
   期**有多长——每次构造都新建的策略，去重集恒空，「不重复告警」就成了恒真式。
4. **真库面**：``dq_diff_report`` 在不在、列对不对、有几行；``dwd_stock_daily`` 的四条留痕列
   在不在、``_as_of``/``_diff_flag`` 读得出什么；一对 ods 腿各自的行数与日期跨度（=周末全
   量校对的真实工作面）。
5. **归因面**：``_diff_flag`` 普查里 86% 的行被标差异——用**生产 reader**（``ods_frame_reader``）
   读重叠日单日两条腿，逐字段量相对偏差与 close 比值分布，分清「复权口径常量差」与「噪声」。

安全边界：真库一段**只读**。``dq_diff_report`` 的写入由被测量方自己做，本脚本只 SELECT；
两条引擎上出现任何非读面语句（INSERT/UPDATE/DELETE/ALTER/CREATE/DROP）即以非零码退出。
不打印连接串（URL 含口令），不读取任何密钥值。跑法见 ``docs/evidence/C48/README.md``。
"""

from __future__ import annotations

import ast
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from sqlalchemy import Engine

#: Statements a read-only census run may legitimately send.
READ_FIRST_WORDS = {"SELECT", "SHOW", "SET", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"}

#: Repository root (this file lives in docs/evidence/C48).
ROOT = Path(__file__).resolve().parents[3]

#: Production trees scanned for call sites (tests are reported separately).
PROD_TREES = ("opendata", "scripts")

#: Symbols whose construction/delivery face this round depends on.
WATCHED = (
    "CrossCheckService",
    "AlertPolicy",
    "diff_alert_message",
    "publish_diff_alert",
    "run_alert_matrix",
    "send_notification",
)


def section(title: str) -> None:
    """Print one readable section banner."""
    print(f"\n--- {title} ---")


def attach(engine: Engine) -> list[str]:
    """Capture every statement one engine executes, at the driver boundary."""
    from sqlalchemy import event

    sent: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        sent.append(statement)

    return sent


def report_statements(name: str, statements: list[str]) -> int:
    """Print the shape of what an engine actually sent; return the offender count."""
    words = Counter(str(s).strip().split(None, 1)[0].upper() for s in statements)
    print(f"{name}: {len(statements)} statements -> {dict(sorted(words.items()))}")
    offenders = [
        s for s in statements if str(s).strip().split(None, 1)[0].upper() not in READ_FIRST_WORDS
    ]
    for s in offenders:
        print(f"  NOT-READ-ONLY> {str(s)[:200]}")
    return len(offenders)


def criterion_items() -> None:
    """Print AC-9's item-level lines with their ledger keys and states."""
    sys.path.insert(0, str(ROOT / "scripts" / "quality"))
    from acceptance_ledger_check import DOC_PATH, load_ledger, parse_doc

    doc = (ROOT / DOC_PATH).read_text(encoding="utf-8")
    items, _rows_ = parse_doc(doc)
    entries = cast(
        "dict[str, Any]", load_ledger(ROOT / "docs/quality/acceptance-item-ledger.json")["items"]
    )
    ac9 = [item for item in items if item.group == "AC-9"]
    section("0. AC-9 条目级九条（判据原文 + 台账状态）")
    for item in ac9:
        entry = entries.get(item.key)
        state = str(entry.get("state", "<no state>")) if isinstance(entry, dict) else "<no entry>"
        tick = "x" if item.ticked else " "
        print(f"  [{tick}] L{item.line} {item.key} {state:<11} {item.text[:70]}")
    states = sorted(
        {
            str(e["state"])
            for e in entries.values()
            if isinstance(e, dict) and isinstance(e.get("state"), str)
        }
    )
    print(
        f"  AC-9 条目数={len(ac9)} 台账命中={sum(1 for i in ac9 if i.key in entries)}"
        f" 全局状态集={states}"
    )


def schedule_face() -> None:
    """Declared kinds vs executable kinds vs the payload keys anyone reads."""
    import inspect
    import re

    from opendata.pipeline.jobs import EXECUTABLE_KINDS, _execute_template
    from opendata.pipeline.templates import PIPELINE_TEMPLATES, TemplateKind

    section("1. 调度面：TemplateKind ↔ EXECUTABLE_KINDS ↔ schedules.yaml payload")
    print(f"  TemplateKind members      : {sorted(k.value for k in TemplateKind)}")
    print(f"  EXECUTABLE_KINDS          : {sorted(k.value for k in EXECUTABLE_KINDS)}")
    declared = {t.kind.value for t in PIPELINE_TEMPLATES}
    missing = sorted(declared - {k.value for k in EXECUTABLE_KINDS})
    print(f"  declared in schedules.yaml: {sorted(declared)}")
    print(f"  声明了却没有执行器的 kind : {missing}")
    read_keywords = sorted(
        set(
            re.findall(
                r"payload\.get\(\s*[\"']([a-z_]+)[\"']", inspect.getsource(_execute_template)
            )
        )
    )
    print(f"  _execute_template 读的 payload 键: {read_keywords}")
    for template in PIPELINE_TEMPLATES:
        keys = sorted(template.payload)
        executor = "有" if template.kind in EXECUTABLE_KINDS else "无"
        print(f"  {template.name} [{template.kind.value}] cron={template.cron!r} 执行器={executor}")
        print(f"      payload={keys}  note={template.note[:46]}")


def _walk(tree: ast.AST) -> list[ast.AST]:
    """Every node of a tree, flattened."""
    return list(ast.walk(tree))


def call_face() -> dict[str, list[str]]:
    """List every construction/reference of the watched symbols in production code."""
    section("2. 生产调用面：AST 扫描 opendata/ 与 scripts/（tests 单列）")
    found: dict[str, list[str]] = defaultdict(list)
    for tree_name in (*PROD_TREES, "tests"):
        hits: list[str] = []
        for path in sorted((ROOT / tree_name).rglob("*.py")):
            try:
                parsed = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError as exc:  # pragma: no cover - a broken file is a census error
                print(f"  PARSE-FAIL {path}: {exc}")
                continue
            for node in _walk(parsed):
                if isinstance(node, ast.Call):
                    name = _call_name(node.func)
                    if name in WATCHED:
                        keywords = sorted(k.arg or "" for k in node.keywords)
                        hits.append(f"{path}:{node.lineno} call {name}({','.join(keywords)})")
                elif isinstance(node, ast.Attribute) and node.attr in WATCHED:
                    hits.append(f"{path}:{node.lineno} attr {node.attr}")
        label = "production" if tree_name in PROD_TREES else "tests"
        print(f"  [{label}] {tree_name}/ -> {len(hits)} 引用")
        for line in hits:
            print(f"      {line}")
            found[label].append(line)
    return found


def _call_name(func: ast.expr) -> str:
    """The bare name of a called expression."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def policy_face() -> None:
    """What the three governance rules depend on, and for how long it lives."""
    from datetime import datetime, timezone

    from opendata.pipeline.alerts import AlertPolicy
    from opendata.pipeline.cross_check import DiffSummary

    section("3. 策略状态面：三问各自依赖的状态与其生命周期")
    policy = AlertPolicy()
    print(f"  AlertPolicy() 默认 whitelist={policy.whitelist!r} last_rate={policy.last_rate!r}")
    print(f"  AlertPolicy() 默认 alerted={policy.alerted!r}（进程内集合，不跨运行）")

    def summary(deviations: int, keys: int, *, field: str = "close") -> DiffSummary:
        return DiffSummary(
            domain="stock_daily",
            source_a="ths",
            source_b="akshare",
            checked_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
            batch_id="b1",
            compared_keys=keys,
            deviation_count=deviations,
            missing_count=0,
            per_field={field: deviations},
        )

    first = AlertPolicy().decide(summary(3, 100))
    second = AlertPolicy().decide(summary(3, 100))
    print(
        f"  同一差异、两个新建策略：alert#1={first.alert} alert#2={second.alert}（去重集不跨实例）"
    )
    shared = AlertPolicy()
    a = shared.decide(summary(3, 100))
    b = shared.decide(summary(3, 100))
    print(f"  同一差异、同一实例：alert#1={a.alert} alert#2={b.alert} reason={b.reason[:40]!r}")
    spike = AlertPolicy(last_rate=0.01)
    up = spike.decide(summary(30, 100))
    print(f"  差异率突增（last_rate=0.01 -> 0.30）：level={up.level}")
    wl = AlertPolicy(whitelist={("stock_daily", "close")})
    only = wl.decide(summary(3, 100))
    mixed = AlertPolicy(whitelist={("stock_daily", "close")}).decide(
        summary(3, 100, field="volume")
    )
    print(f"  白名单命中（全字段白）：alert={only.alert} reason={only.reason[:40]!r}")
    print(f"  白名单未覆盖字段：alert={mixed.alert}")


def mapping_face() -> None:
    """How wide the cross-check's reachable surface actually is."""
    from opendata.data.mapping import load_mapping, mapping_sources
    from opendata.data.providers import register_providers
    from opendata.data.registry import authority_baseline

    register_providers()
    section("4. 映射与权威面对账：校对作业在几个域上真的算得出差异")
    sources = sorted(mapping_sources())
    by_domain: dict[str, list[str]] = defaultdict(list)
    for source in sources:
        for domain in load_mapping(source).domains:
            by_domain[domain].append(source)
    pairs = {d: sorted(v) for d, v in by_domain.items() if len(v) >= 2}
    print(f"  有 domain mapping 的源: {sources}")
    print(f"  被 mapping 覆盖的域: {len(by_domain)}；两侧都有 mapping 的域: {len(pairs)}")
    print(f"    可对照域清单: {pairs}")
    authority = authority_baseline()
    print(f"  authority.json 声明权威序的域: {len(authority)}")
    for domain, entries in sorted(authority.items()):
        listed = list(entries)
        comparable = [s for s in listed if s in by_domain.get(domain, [])]
        if len(comparable) < 2:
            print(f"    {domain}: 权威序 {listed} 可对照侧只有 {comparable} ⇒ 校对 fail closed")
    print("  stock_daily 两条腿的 key 拼写（step-4 的 extra_diff_keys 能不能落进合并键空间）")
    from opendata.data.mapping import require_domain_mapping

    contract_key = ("symbol", "trade_date")
    for source in ("ths", "akshare"):
        mapping = require_domain_mapping(source, "stock_daily")
        same = tuple(mapping.source_key) == contract_key
        print(
            f"    {source}: contract_key={tuple(mapping.key)}"
            f" source_key={tuple(mapping.source_key)}"
            f" ods 拼写与合并键 {contract_key} 同形={same}"
        )


def _scalar(engine: Engine, sql: str, **params: object) -> Any:  # noqa: ANN401 - driver rows
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _rows(engine: Engine, sql: str, **params: object) -> list[tuple[Any, ...]]:
    from sqlalchemy import text

    with engine.connect() as conn:
        return [tuple(row) for row in conn.execute(text(sql), params).all()]


def warehouse_face(engine: Engine) -> None:
    """Read what the warehouse currently holds on the cross-check's three tables."""
    section("5. 真库读数（只读）：dq_diff_report / dwd_stock_daily / ods 两条腿")
    schema = str(engine.url.database)
    tables = {
        str(row[0])
        for row in _rows(
            engine,
            "SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()",
        )
    }
    print(f"  warehouse schema={schema} tables={len(tables)}")
    short = sorted(t for t in tables if len(t) < 3)
    if short:
        # The enumeration itself can be wrong: an unpacking slip here turns table
        # names into single characters and every later "does this table exist"
        # reads false. Fail loudly instead of reporting a missing table.
        print(f"  FAIL: table enumeration produced non-names {short}")
        raise SystemExit(1)
    cols: dict[str, list[str]] = {}
    for name, row in _rows(
        engine,
        "SELECT TABLE_NAME, GROUP_CONCAT(COLUMN_NAME ORDER BY ORDINAL_POSITION) "
        "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() GROUP BY TABLE_NAME",
    ):
        cols[str(name)] = [str(c) for c in str(row).split(",")]

    report = "dq_diff_report"
    if report in tables:
        wanted = (
            "domain",
            "source_a",
            "source_b",
            "biz_key",
            "field",
            "value_a",
            "value_b",
            "deviation",
            "verdict",
        )
        present = [c for c in wanted if c in cols[report]]
        print(
            f"  {report}: 存在，列 {len(cols[report])} 条；判据点名的 9 列在位 {len(present)}/9 "
            f"缺 {sorted(set(wanted) - set(present))}"
        )
        print(f"  {report}: rows={_scalar(engine, f'SELECT COUNT(*) FROM `{report}`')}")  # noqa: S608
        batches = _rows(
            engine,
            f"SELECT batch_id, domain, source_a, source_b, COUNT(*) FROM `{report}` "  # noqa: S608
            "GROUP BY batch_id, domain, source_a, source_b ORDER BY batch_id LIMIT 20",
        )
        print(f"  {report}: batch 普查 {len(batches)} 组")
        for row in batches:
            print(f"      {row}")
        step3 = [str(r[0]) for r in batches if str(r[0]).startswith("xcheck:")]
        print(
            f"  {report}: batch_id 形如 hook_batch_id() 产出的（xcheck: 前缀）{len(step3)} 组"
            f" / {len(batches)} 组 ⇒ 六步流水线 step-3 在真机上"
            f"{'写过' if step3 else '从未写过这一张表'}"
        )
        shape = _rows(
            engine,
            f"SELECT `field`, `verdict`, COUNT(*), SUM(value_a IS NULL), SUM(deviation IS NULL) "  # noqa: S608
            f"FROM `{report}` GROUP BY `field`, `verdict` ORDER BY 3 DESC LIMIT 12",
        )
        print(f"  {report}: field × verdict 形状（判据点名的字段级记录面）")
        for row in shape:
            print(
                f"      field={row[0]!r} verdict={row[1]!r} rows={int(row[2])}"
                f" value_a为空={int(row[3])} deviation为空={int(row[4])}"
            )
        sample = _rows(
            engine,
            f"SELECT `biz_key`, `field` FROM `{report}` ORDER BY `biz_key` LIMIT 3",  # noqa: S608
        )
        print(f"  {report}: biz_key 样例 {[(str(r[0]), str(r[1])) for r in sample]}")
    else:
        print(f"  {report}: 不存在（校对作业从未在真机写过一行）")

    dwd = "dwd_stock_daily"
    trace = ("source", "_merged_at", "_diff_flag", "_as_of")
    if dwd in tables:
        have = [c for c in trace if c in cols[dwd]]
        print(f"  {dwd}: rows={_scalar(engine, f'SELECT COUNT(*) FROM `{dwd}`')} 留痕列在位 {have}")  # noqa: S608
        flagged = _rows(
            engine,
            f"SELECT _diff_flag, COUNT(*) FROM `{dwd}` GROUP BY _diff_flag "  # noqa: S608
            "ORDER BY 2 DESC LIMIT 6",
        )
        print(f"  {dwd}: _diff_flag 普查 {[(int(a), int(b)) for a, b in flagged]}")
        by_source = _rows(
            engine,
            f"SELECT source, _diff_flag, COUNT(*) FROM `{dwd}` "  # noqa: S608
            "GROUP BY source, _diff_flag ORDER BY 3 DESC LIMIT 10",
        )
        print(f"  {dwd}: _diff_flag × source 普查")
        for row in by_source:
            print(f"      source={row[0]!r} _diff_flag={int(row[1])} rows={int(row[2])}")
        span = _rows(engine, f"SELECT MIN(_as_of), MAX(_as_of) FROM `{dwd}`")[0]  # noqa: S608
        print(f"  {dwd}: _as_of {span[0]} .. {span[1]}")

    for source in ("ths", "akshare"):
        table = f"ods_stock_daily_{source}"
        if table not in tables:
            print(f"  {table}: 不存在")
            continue
        date_col = next((c for c in cols[table] if c in ("trade_date", "日期", "date")), None)
        total = _scalar(engine, f"SELECT COUNT(*) FROM `{table}`")  # noqa: S608
        if date_col is None:
            print(f"  {table}: rows={total} 无可用的日期列名")
            continue
        span = _rows(
            engine,
            f"SELECT MIN(`{date_col}`), MAX(`{date_col}`) FROM `{table}`",  # noqa: S608
        )[0]
        print(f"  {table}: rows={total} {date_col} {span[0]} .. {span[1]}")


def _as_float(value: object) -> float | None:
    """Numeric value of one cell, or None when the cell is not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value))
    except ValueError:
        return None


def deviation_face(engine: Engine) -> None:
    """Attribute the ``_diff_flag`` census: what do the two legs disagree on?

    615,610 of 717,704 ths rows carry ``_diff_flag = 1`` (section 5), i.e. almost every
    key the two legs share - yet the one key readable on both legs at the akshare edge
    agreed to the cent. So either the flags are real, or the打标 is broken. Rather than
    re-implement the test, this section calls the merge's **own** decision functions
    (``_index`` / ``_value_columns`` / ``_disagreeing_keys``) on one representative
    overlap day read through the production reader, and reconciles that set against the
    ``_diff_flag`` dwd actually stores for the same day.
    """
    from datetime import date as date_cls

    import pandas as pd

    from opendata.pipeline.cross_check import _deviation
    from opendata.pipeline.dwd_merge import _disagreeing_keys, _index, _value_columns
    from opendata.pipeline.templates import ods_frame_reader

    section("6. 打标对账：合并自己的判定函数在这一天会怎么标，dwd 存的是什么")
    busiest = _rows(
        engine,
        "SELECT `日期`, COUNT(*) FROM `ods_stock_daily_akshare` "
        "GROUP BY `日期` ORDER BY COUNT(*) DESC LIMIT 3",
    )
    print(f"  akshare 腿最满的三天: {[(str(r[0]), int(r[1])) for r in busiest]}")
    day = date_cls.fromisoformat(str(busiest[0][0])[:10])
    key = ("symbol", "trade_date")
    authority = ["ths", "akshare"]
    frames = {
        source: ods_frame_reader(engine, "stock_daily", source)(day, day, set())
        for source in authority
    }
    for source, frame in frames.items():
        print(f"  {source}: {len(frame)} 行 @ {day} 列={sorted(frame.columns)}")
        for column in ("amount", "volume"):
            if column not in frame:
                continue
            values = pd.to_numeric(frame[column], errors="coerce")
            print(
                f"      {column}: 零值 {int((values == 0).sum())} 空值 {int(values.isna().sum())}"
                f" / {len(values)} 行  非零中位数 {values[values > 0].median()}"
            )
    indexed: dict[str, dict[tuple, dict]] = {}
    collided: dict[str, set[tuple]] = {}
    for source, frame in frames.items():
        rows, refused = _index(frame, key, source)
        indexed[source] = rows
        collided[source] = refused
    value_columns = _value_columns(frames, set(key), "stock_daily")
    disagreeing = _disagreeing_keys(indexed, authority, value_columns, key)
    shared = set(indexed["ths"]) & set(indexed["akshare"])
    print(
        f"  _disagreeing_keys 判为不一致 {len(disagreeing)} / 两腿同键 {len(shared)}"
        f" / ths 侧 {len(indexed['ths'])} 键 / 重键被拒 {len(collided['ths'])}"
    )
    for field in value_columns:
        pairs = [
            (ths_row.get(field), ak_row.get(field))
            for biz_key, ths_row in indexed["ths"].items()
            if (ak_row := indexed["akshare"].get(biz_key)) is not None
        ]
        if not pairs:
            continue
        numeric = [d for x, y in pairs if (d := _deviation(x, y)) is not None and d > 1e-4]
        text_only = [(x, y) for x, y in pairs if _deviation(x, y) is None and str(x) != str(y)]
        example = "-"
        if text_only:
            first, second = text_only[0]
            example = f"{first!r} vs {second!r} ({type(first).__name__}/{type(second).__name__})"
        print(
            f"    {field}: 同键可比 {len(pairs)}  数值判差 {len(numeric)}"
            f"  仅靠字符串判差 {len(text_only)}  例 {example}"
        )
        if not numeric:
            continue
        ratios_list: list[float] = []
        for x, y in pairs:
            value_a, value_b = _as_float(x), _as_float(y)
            if value_a is not None and value_b:
                ratios_list.append(value_a / value_b)
        ratios = pd.Series(ratios_list).round(3)
        top = dict(ratios.value_counts().head(3))
        print(
            f"        偏差幅度：中位 {pd.Series(numeric).median():.4f} 最大 {max(numeric):.4f}"
            f" | ths/ak 比值 top3（常量比值=单位或复权口径，散=噪声）{top}"
        )
    stored = _rows(
        engine,
        "SELECT symbol, trade_date FROM `dwd_stock_daily` "
        "WHERE trade_date = :day AND _diff_flag = 1",
        day=day,
    )
    stored_keys = {(str(r[0]), r[1]) for r in stored}
    as_text = {(str(k[0]), k[1]) for k in disagreeing}
    print(
        f"  dwd 该日 _diff_flag=1 存了 {len(stored_keys)} 行；本日判定函数给出 {len(as_text)} 键；"
        f"交集 {len(stored_keys & as_text)}"
    )
    print(
        "  读法：存的多而算的少 = 陈旧合并遗留；算的多而存的少 = 落库前被改；"
        "两者量级相当且交集覆盖判定集，才谈得上「打标正确」"
    )
    if not shared:
        print("  两腿当日无同键：数值面不可测（只能报空，不能报通过）")
        return


def main() -> int:
    """Run every reading and report the read-only verdict."""
    from opendata.core.config import settings
    from opendata.pipeline.jobs import warehouse_engine

    print(f"python={sys.version.split()[0]} cwd={Path.cwd()}")
    criterion_items()
    schedule_face()
    call_face()
    policy_face()
    mapping_face()

    engine = warehouse_engine()
    spy = attach(engine)
    warehouse_face(engine)
    deviation_face(engine)
    alive = _scalar(engine, "SELECT 1")
    offenders = report_statements("warehouse engine", spy)
    print(f"\nwarehouse engine alive (url never printed): probe={alive}")
    print(f"settings.data_database_url scheme={settings.data_database_url.split(':', 1)[0]}")
    if offenders:
        print(f"FAIL: {offenders} non-readonly statements issued")
        return 1
    print("OK: every face measured; no DDL, no writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
