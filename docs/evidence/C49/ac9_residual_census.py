#!/usr/bin/env python3
"""C49 现场读数：AC-9|03/|08/|09 三格各自量到什么（只读）.

判据原文（`验收文档.md` §2 里 AC-9 的三条未勾格）问的是：

- |03 反例用例：① 字段名不匹配时必须报错而非静默通过；② 单位未归一时必须产生 mismatch
- |08 单源域 dwd 直通模式可用（`layer=dwd` 查询不报错）
- |09 dwd 重算幂等

三条各自都有"名字相近的用例"存在，本轮量的是名字底下有没有那件事：

1. **判据与仪器面**：三格在台账里的现状、文档勾选、有没有条目级探针。
2. **反例面**（离线，纯 pandas）：七个场景逐个跑公开入口 `compare_source_frames` ——
   双侧同缺（None / 空串）、单侧缺、被 mapping 点名的列在帧里不存在、文本折成浮点、
   单位换算只在一侧声明，再加 tolerances 两侧不同时的胜负。
3. **见证用例面**（AST）：三格现有的见证用例各挂什么 marker、断言了哪些表达式，在
   `pytest -m "not e2e"`（门禁与探针用的选择式）下到底跑不跑。第一遍读数把
   `with pytest.raises(...)` 读成了"无 assert"（仪器只走 `ast.Assert`），那是仪器盲区
   不是用例为空；修法见 `_assert_text`，两遍读数的差别只在这正面。
4. **幂等面**（离线）：`merge_source_frames` 同输入跑两遍，逐列比输出，再换 `merged_at`
   跑第三遍看差在哪几列。
5. **直通查询面**（活库只读）：每个域有几条腿、有没有 dwd 表、表里几行，以及查询层是否
   读映射/权威面（读不到就说明它不会因为"只有一条腿"而改变行为）。
6. **同缺面**（活库只读）：两条腿重叠日逐字段数「两侧都空 / 只有一侧空 / 两侧都有值」，
   看读成"一致"的那部分里有多少其实是双侧无数据。
7. **直通查询执行面**（活库只读，|08 的判定面）：按 endpoint 自己的顺序（列表 → 建 SQL →
   执行）对 15 个域各走一遍 `layer=dwd`，用的是生产 `_key` / `build_data_select`，
   所以读数是"这条路径会停在哪一步"，不是"我手写的查询能不能跑"。
8. **真腿直通面**（活库只读，|08/|09 的生产侧）：`stock_action/ths` 的真 ods 腿经生产
   reader 取回 → `merge_source_frames` 单帧直通跑两遍比逐格 → 与 `dwd_stock_action`
   的实际行数对账。

安全边界：真库一段**只读**（只有 SELECT），引擎上出现任何非读面语句即以非零码退出；不打印
连接串（URL 含口令），不读取任何密钥值，不写任何一行数据。跑法见本轮 README。
"""

from __future__ import annotations

import ast
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from sqlalchemy import Engine

    from opendata.data.mapping import DomainMapping

#: Statements a read-only census run may legitimately send.
READ_FIRST_WORDS = {"SELECT", "SHOW", "SET", "BEGIN", "COMMIT", "ROLLBACK", "PRAGMA"}

#: Repository root (this file lives in docs/evidence/C49).
ROOT = Path(__file__).resolve().parents[3]

#: The three criterion ids this round is about.
ITEMS = ("AC-9|03", "AC-9|08", "AC-9|09")

#: The overlap day both stock_daily legs are populated on (C48's measurement).
OVERLAP_DAY = date(2026, 7, 3)

#: Contract numeric fields the shipped stock_daily mappings carry.
NUMERIC_FIELDS = ("open", "high", "low", "close", "volume", "amount")

KEY: tuple[str, ...] = ("symbol", "trade_date")
MERGED_AT = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
LATER_AT = datetime(2026, 9, 27, 18, 30, tzinfo=timezone.utc)

#: The date every live face asks the warehouse for.
AS_OF = date(2026, 9, 26)

#: The single-source domain that has a real ods leg (C49's reading: stock_action/ths).
SINGLE_SOURCE_DOMAIN = "stock_action"
SINGLE_SOURCE_LEG = "ths"


class Witness(NamedTuple):
    """One witness test's reading: where it lives and what it can see."""

    path: str
    name: str
    klass: str
    markers: tuple[str, ...]
    class_markers: tuple[str, ...]
    asserts: tuple[str, ...]
    gate_runs: bool


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


def _scalar(engine: Engine, sql: str, **params: object) -> Any:  # noqa: ANN401 - driver row
    """Run one SELECT and return its first scalar."""
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def _count_rows(engine: Engine, name: str) -> int:
    """COUNT(*) for one table, with the dialect's own identifier quoting."""
    from sqlalchemy import Column, MetaData, Table, func, select
    from sqlalchemy.sql.elements import quoted_name

    unnamed = Table(quoted_name(name, True), MetaData(), Column("_row_marker"))
    stmt = select(func.count()).select_from(unnamed)
    with engine.connect() as conn:
        return int(conn.execute(stmt).scalar_one())


def _frame(**overrides: Any) -> Any:  # noqa: ANN401 - a pandas frame
    """A two-row contract frame with per-column overrides."""
    import pandas as pd

    base: dict[str, list[Any]] = {
        "symbol": ["600519", "000001"],
        "trade_date": [date(2024, 1, 2), date(2024, 1, 2)],
        "close": [1688.0, 9.5],
        "volume": [30000.0, 20000.0],
    }
    base.update(overrides)
    return pd.DataFrame(base)


def _mapping(
    columns: Mapping[str, str],
    tolerances: Mapping[str, float],
    *,
    volume_scale: float | None = 100.0,
) -> DomainMapping:
    """Build a stock_daily mapping; ``volume_scale=None`` means "no unit declared"."""
    from opendata.data.mapping import DomainMapping, FieldMapping

    fields = {}
    for name, source_column in columns.items():
        if name == "volume" and volume_scale is not None:
            fields[name] = FieldMapping(source_column, scale=volume_scale)
        else:
            fields[name] = FieldMapping(source_column)
    return DomainMapping(
        domain="stock_daily",
        key=KEY,
        fields=fields,
        # C53 made the three domain 口径 required: this census hand-builds both
        # legs of one stock_daily compare, so it states the shipped yaml's own
        # conventions. Equal on both sides, therefore the gate lets every case
        # through and the census readings below are unchanged by this edit.
        adjust="unadjusted",
        suspension="absent_row",
        denominator="key_union",
        tolerances=dict(tolerances),
    )


BASE_COLUMNS = {
    "symbol": "symbol",
    "trade_date": "trade_date",
    "close": "close",
    "volume": "volume",
}


def _compare(
    frame_a: Any,  # noqa: ANN401
    frame_b: Any,  # noqa: ANN401
    mapping_a: DomainMapping | None = None,
    mapping_b: DomainMapping | None = None,
) -> Any:  # noqa: ANN401
    """Call the public cross-check entry point."""
    from opendata.pipeline.cross_check import compare_source_frames

    default = _mapping(BASE_COLUMNS, {"close": 1e-4})
    return compare_source_frames(
        "stock_daily",
        frame_a,
        mapping_a or default,
        frame_b,
        mapping_b or default,
        source_a="akshare",
        source_b="ths",
        batch_id="c49-census",
        checked_at=MERGED_AT,
    )


def _deviation(a: object, b: object) -> Any:  # noqa: ANN401
    """Call the production deviation helper directly."""
    from opendata.pipeline.cross_check import _deviation as prod

    return prod(a, b)


def _render_deviation(value: Any) -> str:  # noqa: ANN401
    """Render a deviation reading, keeping None and NaN apart from a number."""
    if value is None:
        return "None"
    if isinstance(value, float) and math.isnan(value):
        return "nan"
    return str(value)


def _guard(fn: Callable[[], Any]) -> Any:  # noqa: ANN401
    """Run a scenario; return the exception text instead of aborting the census."""
    try:
        return fn()
    except Exception as exc:
        # Broad on purpose: the exception type IS the reading.
        return f"raised {type(exc).__name__}: {exc}"


def _report(name: str, outcome: Any) -> None:  # noqa: ANN401
    """Print one scenario's verdict line."""
    text = str(outcome)
    if text.startswith("raised "):
        print(f"  {name}: {text}")
        return
    print(
        f"  {name}: verdict={outcome.verdict.value}"
        f" 键并集={outcome.compared_keys} 判差={outcome.deviation_count}"
        f" 单边缺={outcome.missing_count} per_field={dict(sorted(outcome.per_field.items()))}"
    )


def _tolerance_source_line() -> str:
    """The production source line that decides whose tolerance applies.

    Printed rather than hard-coded: this face is a reading of the rule, and a
    label that repeats an old implementation would turn the census into an
    argument about a line of code that is no longer there.
    """
    source = (ROOT / "opendata" / "pipeline" / "cross_check.py").read_text(encoding="utf-8")
    lines = [
        line.strip()
        for line in source.splitlines()
        if "tolerances=" in line and not line.strip().startswith("def")
    ]
    return lines[0] if lines else "读不到 tolerances= 这一行"


def criterion_items() -> None:
    """面 1：三格判据原文、台账现状与有没有条目级仪器."""
    sys.path.insert(0, str(ROOT / "scripts" / "quality"))
    from acceptance_ledger_check import DOC_PATH, load_ledger, parse_doc

    doc = (ROOT / DOC_PATH).read_text(encoding="utf-8")
    items, _rows = parse_doc(doc)
    entries = cast(
        "dict[str, Any]", load_ledger(ROOT / "docs/quality/acceptance-item-ledger.json")["items"]
    )
    wanted = probe_registered()
    section("1. 三条判据原文 + 台账状态 + 探针注册面")
    for item in items:
        short = "|".join(item.key.split("|")[:2])
        if short not in ITEMS:
            continue
        entry = entries.get(item.key)
        state = str(entry.get("state", "<no state>")) if isinstance(entry, dict) else "<no entry>"
        tick = "x" if item.ticked else " "
        print(f"  [{tick}] L{item.line} {item.key} {state:<11}")
        print(f"        判据原文：{item.text}")
        print(f"        条目级探针：{'有' if short in wanted else '无'}")


def probe_registered() -> set[str]:
    """Which ``AC-N|NN`` ids the item-level probe instrument registers today."""
    source = (ROOT / "scripts/quality/acceptance_item_probe.py").read_text(encoding="utf-8")
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        parts = node.value.split("|")
        if len(parts) >= 2 and parts[0].startswith("AC-") and parts[1].isdigit():
            found.add(f"{parts[0]}|{parts[1]}")
    return found


def counterexample_face() -> None:
    """面 2（|03）：七个反例场景在公开入口上各自读出什么."""
    section("2. 反例面：compare_source_frames 对每种输入的实测反应")
    base = _frame()
    both_none = _frame(volume=[None, None])
    both_blank = _frame(volume=["", ""])
    cases: list[tuple[str, Callable[[], Any]]] = [
        ("A 两侧 volume 同为 None", lambda: _compare(both_none, both_none)),
        ("B 两侧 volume 同为空串", lambda: _compare(both_blank, both_blank)),
        ("C volume 一侧 None 一侧有值", lambda: _compare(both_none, base)),
        ("D volume 列在 A 帧里不存在", lambda: _compare(_frame().drop(columns=["volume"]), base)),
        (
            "E close 文本 vs 浮点（同值）",
            lambda: _compare(_frame(close=["1688.0", "9.5"]), base),
        ),
        (
            "F close 文本垃圾 vs 浮点",
            lambda: _compare(_frame(close=["-", "--"]), base),
        ),
        (
            "G 单位只在 a 腿声明（b 腿原始值是股）",
            lambda: _compare(
                base,
                _frame(volume=[3000000.0, 2000000.0]),
                mapping_b=_mapping(BASE_COLUMNS, {"close": 1e-4}, volume_scale=None),
            ),
        ),
    ]
    for name, fn in cases:
        _report(name, _guard(fn))

    print(f"  tolerances 两侧不同时谁生效（源码这一行：{_tolerance_source_line()}）")
    tight = _mapping(BASE_COLUMNS, {"close": 1e-4})
    loose = _mapping(BASE_COLUMNS, {"close": 1.0})
    none_ = _mapping(BASE_COLUMNS, {})
    near = _frame(close=[1688.4, 9.5])
    for label, mapping_a, mapping_b in (
        ("a 紧(1e-4) / b 松(1.0)", tight, loose),
        ("a 松(1.0) / b 紧(1e-4)", loose, tight),
        ("a 不声明 / b 紧(1e-4)", none_, tight),
        ("a 紧(1e-4) / b 不声明", tight, none_),
    ):
        pair = _guard(partial(_compare, near, base, mapping_a, mapping_b))
        _report(f"close 相差 0.4 @ {label}", pair)

    print("  _deviation 的分支读数（None/nan 都不记差异）")
    for a, b in ((None, None), ("", ""), ("abc", 1.0), (float("nan"), float("nan")), (1.0, 1.0)):
        left = _render_deviation(_deviation(a, b))
        print(f"    _deviation({a!r}, {b!r}) = {left}")


def _deco_text(source: str, node: ast.AST) -> tuple[str, ...]:
    """Render a function/class node's decorator sources."""
    decorators = getattr(node, "decorator_list", [])
    return tuple(
        (ast.get_source_segment(source, dec) or "").replace("\n", " ")[:80] for dec in decorators
    )


def _segment(source: str, node: ast.AST | None, width: int = 120) -> str:
    """Render one AST node back to its source, flattened to a single line."""
    if node is None:
        return ""
    return (ast.get_source_segment(source, node) or "").replace("\n", " ").strip()[:width]


def _assert_text(source: str, node: ast.AST) -> tuple[str, ...]:
    """Render every falsifiable construct inside a test node.

    ``assert`` statements *and* ``with pytest.raises(...)`` blocks: the second is a
    fail-closed witness, so counting only ``assert`` would read a real counterexample
    test as an empty one.
    """
    found: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Assert):
            message = f" ({_segment(source, sub.msg)})" if sub.msg is not None else ""
            found.append(f"assert {_segment(source, sub.test, 100)}{message}")
        elif isinstance(sub, ast.With):
            contexts = (_segment(source, item.context_expr) for item in sub.items)
            found.extend(f"with {text}" for text in contexts if "raises" in text)
    return tuple(found)


def locate_witness(path: str, name: str) -> Witness | None:
    """Find one test function's markers, class, assertions and gate visibility."""
    file = ROOT / path
    if not file.is_file():
        return None
    source = file.read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    funcs: list[Any] = [
        node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for cls in classes:
        for fn in cls.body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and fn.name == name:
                return _witness(source, path, name, cls, fn)
    for fn in funcs:
        if fn.name == name:
            return _witness(source, path, name, None, fn)
    return None


def _witness(source: str, path: str, name: str, cls: ast.ClassDef | None, fn: ast.AST) -> Witness:
    """Assemble one witness reading."""
    markers = _deco_text(source, fn)
    class_markers = _deco_text(source, cls) if cls is not None else ()
    flagged = "e2e" in " ".join((*markers, *class_markers))
    return Witness(
        path=path,
        name=name,
        klass=cls.name if cls is not None else "(模块级)",
        markers=markers,
        class_markers=class_markers,
        asserts=_assert_text(source, fn),
        gate_runs=not flagged,
    )


def witness_visibility_face() -> None:
    """面 3：三格的见证用例挂什么 marker、门禁选择式下跑不跑."""
    section("3. 见证用例面：用例存在 ≠ 门禁跑得到")
    for path, name in WITNESS_CASES:
        witness = locate_witness(path, name)
        if witness is None:
            print(f"  {path}::{name} —— 文件或用例不存在（这一条要显式说出来，不能当作没看见）")
            continue
        runs = "门禁跑" if witness.gate_runs else "门禁不跑（被 -m 'not e2e' 摘掉）"
        print(f"  {path}::{name}")
        print(f"    类={witness.klass} 用例 marker={witness.markers or '()'}")
        print(f"    类 marker={witness.class_markers or '()'}) {runs}")
        for text in witness.asserts:
            print(f"    assert> {text}")
        if not witness.asserts:
            print("    assert> （无 assert：这条用例不证明任何东西）")


WITNESS_CASES: tuple[tuple[str, str], ...] = (
    ("tests/test_cross_check.py", "test_unconverted_unit_produces_a_mismatch"),
    ("tests/test_cross_check.py", "test_structural_field_mismatch_fails_closed"),
    ("tests/test_cross_check.py", "test_duplicate_keys_fail_closed"),
    (
        "tests/test_cross_check.py",
        "test_a_mapped_column_the_frame_lacks_errors_rather_than_passing",
    ),
    ("tests/test_cross_check.py", "test_the_tighter_tolerance_decides_whichever_leg_declares_it"),
    ("tests/test_data_mapping.py", "test_normalize_frame_requires_mapped_columns"),
    ("tests/test_dwd_merge.py", "test_single_source_domain_is_passthrough"),
    (
        "tests/test_dwd_merge.py",
        "test_the_ambiguous_key_is_refused_and_the_rest_of_the_window_lands",
    ),
    ("tests/test_dwd_merge.py", "test_a_key_carried_three_times_is_still_refused_whole"),
    ("tests/test_dwd_merge.py", "test_the_service_reports_the_refusal_it_landed_around"),
    ("tests/test_dwd_merge.py", "test_passthrough_columns_are_exactly_the_dwd_table_columns"),
    ("tests/test_dwd_merge.py", "test_layer_dwd_query_builds_over_the_landed_columns"),
    ("tests/test_dwd_merge.py", "test_a_second_recompute_of_the_same_input_is_the_same_frame"),
    ("tests/test_dwd_merge.py", "test_the_input_dictionary_order_does_not_change_the_output"),
    ("tests/test_dwd_merge.py", "test_a_recompute_at_a_later_clock_moves_only_the_audit_stamp"),
    ("tests/test_dwd_merge.py", "test_landing_the_same_key_twice_is_one_row_written_in_place"),
    ("tests/test_dwd_merge.py", "test_merge_writes_rows_with_trace_columns_and_is_idempotent"),
    ("tests/test_ths_dwd_integration.py", "test_recompute_is_idempotent"),
)


def idempotence_face() -> None:
    """面 4（|09）：同输入合并两遍，输出逐列比；再换 merged_at 跑第三遍."""
    section("4. 幂等面：merge_source_frames 跑两遍到底差不差")
    from opendata.pipeline.dwd_merge import merge_source_frames

    frames: dict[str, Any] = {
        "ths": _frame(),
        "akshare": _frame(volume=[30100.0, 20000.0]),
    }
    first, stats_a = merge_source_frames(
        "stock_daily",
        frames,
        authority=("ths", "akshare"),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=MERGED_AT,
    )
    second, stats_b = merge_source_frames(
        "stock_daily",
        frames,
        authority=("ths", "akshare"),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=MERGED_AT,
    )
    print(f"  双源：stats 第一次={stats_a}")
    print(f"  双源：stats 第二次={stats_b}")
    print(f"  双源：两次输出逐格相等={first.equals(second)}")

    later, stats_c = merge_source_frames(
        "stock_daily",
        frames,
        authority=("ths", "akshare"),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=LATER_AT,
    )
    same = [c for c in first.columns if first[c].equals(later[c])]
    print(f"  换 merged_at 再跑：相同的列={same}")
    print(f"  换 merged_at 再跑：不同的列={[c for c in first.columns if c not in same]}")
    print(f"  换 merged_at 再跑：stats={stats_c}")

    single_frames: dict[str, Any] = {"ths": _frame()}
    single, stats_single = merge_source_frames(
        "stock_daily",
        single_frames,
        authority=("ths",),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=MERGED_AT,
    )
    again, _stats_again = merge_source_frames(
        "stock_daily",
        single_frames,
        authority=("ths",),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=MERGED_AT,
    )
    print(f"  单源直通：stats={stats_single} 直通帧两次相等={single.equals(again)}")
    print(f"  单源直通：输出列={list(single.columns)}")

    flipped, stats_flipped = merge_source_frames(
        "stock_daily",
        dict(reversed(list(frames.items()))),
        authority=("ths", "akshare"),
        key=KEY,
        as_of=date(2024, 1, 31),
        merged_at=MERGED_AT,
    )
    print(f"  帧字典顺序换一下（同一批输入）：与第一次相等={first.equals(flipped)}")
    print(f"  帧字典顺序换一下：stats={stats_flipped}")


def query_inspects_registry() -> bool:
    """Does the query layer read the mapping/registry at all (i.e. could it care about legs)."""
    source = (ROOT / "opendata/pipeline/query.py").read_text(encoding="utf-8")
    seen: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            seen.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Name):
            seen.add(node.id)
        elif isinstance(node, ast.Attribute):
            seen.add(node.attr)
    watched = ("authority_baseline", "load_mapping", "mapping_sources", "require_domain_mapping")
    hits = sorted(word for word in watched if word in seen)
    print(f"    query.py 里出现过的映射/权威符号：{hits or '无'}")
    return bool(hits)


def legs_by_domain() -> dict[str, list[str]]:
    """Domain -> sources that map it, read from the shipped mapping files."""
    from opendata.data.mapping import load_mapping, mapping_sources

    by_domain: dict[str, list[str]] = defaultdict(list)
    for source in sorted(mapping_sources()):
        for domain in load_mapping(source).domains:
            by_domain[str(domain)].append(source)
    return dict(by_domain)


def single_source_face(engine: Engine) -> dict[str, list[str]]:
    """面 5（|08）：每个域的腿数、dwd 表在不在、表里几行."""
    section("5. 直通查询面：域 → 腿数 → dwd 表 → 行数")
    from opendata.data.domains import dwd_table
    from opendata.data.registry import authority_baseline

    by_domain = legs_by_domain()
    authority = cast("dict[str, Any]", authority_baseline())
    print(f"  查询层是否会因腿数而改变行为：{query_inspects_registry()}")
    domains = sorted(set(by_domain) | set(authority))
    print(f"  参与对账的域：{len(domains)}")
    for domain in domains:
        legs = sorted(by_domain.get(domain, []))
        order = list(authority.get(domain, []))
        table = dwd_table(domain)
        exists = bool(
            _scalar(
                engine,
                "SELECT COUNT(*) FROM information_schema.TABLES"
                " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t",
                t=table,
            )
        )
        rows = _count_rows(engine, table) if exists else None
        kind = "单源" if len(legs) == 1 else ("零腿" if not legs else f"{len(legs)} 腿")
        print(f"  {domain} [{kind}]: 腿={legs} 权威序={order} dwd 表={exists} 行数={rows}")
    landed_batches = _scalar(engine, "SELECT COUNT(*) FROM batch_watermark")
    print(f"  batch_watermark 记录数：{landed_batches}")
    return by_domain


def _is_empty(value: object) -> bool:
    """Is this a cell the comparator has nothing to compare?"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return isinstance(value, float) and math.isnan(value)


def _unparseable(value: object) -> bool:
    """Would ``to_numeric(errors='coerce')`` turn this into NaN?"""
    import pandas as pd

    if value is None or isinstance(value, (int, float)):
        return False
    try:
        pd.to_numeric(str(value), errors="raise")
    except Exception:
        # Broad on purpose: counting the coercion surface IS the reading.
        return True
    return False


def absence_face(engine: Engine) -> None:
    """面 6（|03 的洞）：重叠日逐字段数双侧同缺，用生产 reader + 生产 mapping."""
    section("6. 同缺面：两条腿逐字段的空值形状")
    from opendata.data.mapping import load_mapping, require_domain_mapping
    from opendata.pipeline.templates import ods_frame_reader

    maps = {source: require_domain_mapping(source, "stock_daily") for source in ("ths", "akshare")}
    legs = {
        source: ods_frame_reader(engine, "stock_daily", source)(OVERLAP_DAY, OVERLAP_DAY, set())
        for source in ("ths", "akshare")
    }
    for source, leg in legs.items():
        print(f"  {source} 腿：{len(leg)} 行，列={list(leg.columns)[:10]}")
    index = {
        source: {
            tuple(row[field] for field in maps[source].key): row for row in leg.to_dict("records")
        }
        for source, leg in legs.items()
    }
    shared = sorted(
        set(index["ths"]) & set(index["akshare"]),
        key=lambda item: tuple(str(part) for part in item),
    )
    print(f"  同键可比 {len(shared)} 对（{OVERLAP_DAY}）")
    for field in NUMERIC_FIELDS:
        both_empty = one_empty = both_value = garbage = 0
        for biz_key in shared:
            value_a = index["ths"][biz_key].get(field)
            value_b = index["akshare"][biz_key].get(field)
            empty_a, empty_b = _is_empty(value_a), _is_empty(value_b)
            if empty_a and empty_b:
                both_empty += 1
                continue
            if empty_a or empty_b:
                one_empty += 1
                continue
            both_value += 1
            garbage += _unparseable(value_a) + _unparseable(value_b)
        declared = {
            source: (
                field in maps[source].fields,
                getattr(maps[source].fields.get(field), "scale", None),
            )
            for source in ("ths", "akshare")
        }
        tolerance = {
            source: load_mapping(source).domains["stock_daily"].tolerance(field)
            for source in ("ths", "akshare")
        }
        print(
            f"    {field}: 两侧都空={both_empty} 只一侧空={one_empty}"
            f" 两侧有值={both_value} 归一不出={garbage}"
            f" 声明(在字段表里, scale)={declared} tolerance={tolerance}"
        )


def passthrough_query_face(engine: Engine, by_domain: dict[str, list[str]]) -> None:
    """面 7（|08 的判定面）：每个域用生产查询函数走一遍 ``layer=dwd`` 查询.

    The endpoint's own order is reproduced (columns first, then build, then
    execute) so the reading says where a single-source domain would stop
    working, not where a hand-written query would.
    """
    section("7. 直通查询执行面：生产 _inspect_columns/_key/build_data_select + 真机 SELECT")
    from opendata.api.data_query import _inspect_columns, _key
    from opendata.data.domains import dwd_table, load_domains
    from opendata.pipeline.query import DataQuery, build_data_select, resolve_time_field

    domains = sorted(load_domains())
    print(f"  注册表里的域：{len(domains)}；有 mapping 腿的域：{len(by_domain)}")
    print(f"  只在注册表、没有任何腿的域：{sorted(set(domains) - set(by_domain))}")
    for domain in domains:
        legs = sorted(by_domain.get(domain, []))
        kind = "单源" if len(legs) == 1 else ("零腿" if not legs else f"{len(legs)} 腿")
        table = dwd_table(domain)
        columns = [str(item) for item in _inspect_columns(engine, table)]
        if not columns:
            exists = bool(
                _scalar(
                    engine,
                    "SELECT COUNT(*) FROM information_schema.TABLES"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t",
                    t=table,
                )
            )
            print(
                f"  {domain} [{kind}] {table}: 生产 _inspect_columns 得到 0 列"
                f"（表真的存在={exists}）→ endpoint 在这一步 404，建 SQL 走不到"
            )
            continue
        key = _guard(partial(_key, domain))
        if isinstance(key, str):
            print(f"  {domain} [{kind}] {table}: 生产 _key 失败 {key}")
            continue
        built = _guard(
            partial(
                build_data_select,
                DataQuery(domain=domain, layer="dwd", page_size=1),
                table=table,
                columns=columns,
                key=key,
                today=AS_OF,
            )
        )
        if isinstance(built, str):
            print(f"  {domain} [{kind}] {table}: 建 SQL 失败 {built}")
            continue
        sql, params = built
        executed = _guard(partial(_execute_row_count, engine, sql, params))
        time_field = _guard(partial(resolve_time_field, domain))
        print(
            f"  {domain} [{kind}] {table}: 列={len(columns)} key={key}"
            f" 时间字段={time_field} 执行={executed}"
        )


def _execute_row_count(engine: Engine, sql: str, params: dict[str, object]) -> int:
    """Run one built SELECT and return how many rows it handed back."""
    from sqlalchemy import text

    with engine.connect() as connection:
        return len(connection.execute(text(sql), params).fetchall())


def real_leg_passthrough(engine: Engine) -> None:
    """面 8（|08/|09 的生产侧）：真 ods 腿 → 生产 reader → 直通 merge 跑两遍 → 与 dwd 对账."""
    section("8. 真腿直通面：ods 腿 → merge_source_frames → dwd 落地行数")
    from opendata.data.domains import dwd_table
    from opendata.data.mapping import require_domain_mapping
    from opendata.pipeline.dwd_merge import merge_source_frames
    from opendata.pipeline.templates import ods_frame_reader

    domain, source = SINGLE_SOURCE_DOMAIN, SINGLE_SOURCE_LEG
    mapping = _guard(partial(require_domain_mapping, source, domain))
    if isinstance(mapping, str):
        print(f"  {domain}/{source} 生产映射取不到：{mapping}")
        return
    read_leg = ods_frame_reader(engine, domain, source)
    read = _guard(partial(read_leg, date(2000, 1, 1), AS_OF, set()))
    if isinstance(read, str):
        print(f"  {domain}/{source} 生产 reader 失败：{read}")
        return
    print(f"  {domain}/{source} 生产 reader 产出：{len(read)} 行，key={mapping.key}")
    print(f"  reader 列：{list(read.columns)}")

    def _merge() -> Any:  # noqa: ANN401 - a merged frame
        return merge_source_frames(
            domain,
            {source: read},
            authority=(source,),
            key=mapping.key,
            as_of=AS_OF,
            merged_at=MERGED_AT,
        )

    first = _guard(_merge)
    if isinstance(first, str):
        print(f"  直通 merge 失败：{first}")
        return
    merged, stats = first
    again, stats_again = cast("tuple[Any, Any]", _guard(_merge))
    print(
        f"  直通 merge：rows={stats.rows} passthrough={stats.passthrough}"
        f" degraded={stats.degraded_rows} diff_flagged={stats.diff_flagged}"
    )
    print(f"  直通 merge 跑两遍逐格相等={merged.equals(again)} stats 相等={stats == stats_again}")
    print(f"  输出列={list(merged.columns)}")
    print(f"  dwd 表 {dwd_table(domain)} 实际行数={_count_rows(engine, dwd_table(domain))}")


def main() -> int:
    """Run every face and report the read-only verdict."""
    from opendata.core.config import settings
    from opendata.pipeline.jobs import warehouse_engine

    print(f"python={sys.version.split()[0]} cwd={Path.cwd()}")
    criterion_items()
    counterexample_face()
    witness_visibility_face()
    idempotence_face()

    engine = warehouse_engine()
    spy = attach(engine)
    by_domain = single_source_face(engine)
    passthrough_query_face(engine, by_domain)
    absence_face(engine)
    real_leg_passthrough(engine)
    print(f"\nwarehouse engine alive (url never printed): probe={_scalar(engine, 'SELECT 1')}")
    print(f"settings.data_database_url scheme={settings.data_database_url.split(':', 1)[0]}")
    offenders = report_statements("warehouse engine", spy)
    if offenders:
        print(f"FAIL: {offenders} non-readonly statements issued")
        return 1
    print("OK: every face measured; no DDL, no writes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
