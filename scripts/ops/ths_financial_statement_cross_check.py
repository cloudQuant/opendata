"""ths 财务报表 vs 新浪财务报表：跨 vendor 逐科目对照 (AC-10 / C10).

``financial_statement`` 此前只有搬运链（新浪）一条腿、登记为 ``verified=false``。
C10 接上 fuyao 的三张报表端点，本脚本就是 ``verified=true`` 的判据：**同一个披露
事实**由两家独立 vendor 各说一次，逐 (标的, 报告期末, 科目) 对到位。

判据是**分位相等**（``round(value, 2)``）而不是浮点阈值。理由：两侧发布的都是
「元、两位小数」的披露值，2026-09-25 实测最大相对残差 1.38e-16（float64 表示位，
如 272,699,660,092.25 vs 272,699,660,092.24997）；任何真实口径差都会至少差到
0.01 元，卡 1e-6 相对容差反而会把「差一分」放过去。披露日同为一等判据：它决定
时点查询是否可用。

科目映射表 (:data:`ITEM_MAP`) 是本轮真正的产出之一，**两处与官方文档的字段说明
相反**（见表内注释），都是逐财年实测反证出来的：``operating_income`` 对应
「营业收入」而非文档写的营业总收入、``interest_expenses`` 对应「利息支出」
而非利息费用。

参照侧的**报表形态差异按观测处理、不按分歧处理**：2026-09-25 实测新浪的平安
银行利润表没有「营业成本 / 销售费用 / 管理费用 / 研发费用 / 所得税费用 /
归属于母公司所有者的净利润」等一般企业科目（用的是「业务及管理费用」「减:所得税」），
招商银行资产负债表用「现金及存放中央银行款项」而非「货币资金」、用
「归属于母公司股东的权益」而非「所有者权益(或股东权益)合计」。这类「该标的没有
这一列」不是数据错，判成失败等于把别人的报表格式算成我们的 bug；但同一科目在
**所有**腿的参照侧都不存在，就只可能是映射表写错，故那一条做成硬失败（见
:func:`_mapping_misses`），否则判据集合会静默缩小到零。

跑法（py313 环境；每条腿都要两家真机取数，fuyao 拒绝连发，故自带节拍）::

    python scripts/ops/ths_financial_statement_cross_check.py
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

    from opendata.data.models import FinancialStatement
    from opendata.data.protocol import Fetcher

#: 秒。fuyao 对连发直接断连（C9 实测同端点家族），要把「跑不通」和「数据不对」分开。
PACE_SECONDS = 12.0
#: 断连后的重试间隔与次数。
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 5

#: 金额发布粒度（元，两位小数）：两侧四舍五入到分必须逐位相同。
CENTS = 2
#: 一条腿至少判到这么多单元格才算判过：只回一两行的响应即便全对也不构成证据。
MIN_JUDGED_CELLS = 12

#: ths 英文科目 → 新浪中文报表科目。三处歧义按实测定案，不照抄文档：
#:
#: * ``operating_income``：文档写「营业总收入」，实测其值与新浪「营业收入」列
#:   逐期相同（600519 五个财年、残差 0），与「营业总收入」相差 2%~3%。
#: * ``interest_expenses``：文档写「利息费用」，实测对应新浪「利息支出」
#:   （利润表两列都有、相差约 7 倍；选错不会报错，只会把另一个数当成披露值存下来）。
#: * ``holder_equity_total``：新浪列名带括号（所有者权益(或股东权益)合计）。
#:
#: 只映射**两类报表形态都有且口径唯一**的科目；未登记的科目不参与判据。
ITEM_MAP: Mapping[str, Mapping[str, str]] = {
    "income": {
        "operating_income": "营业收入",
        "operating_costs": "营业成本",
        "operating_expenses": "营业总成本",
        "sales_fee": "销售费用",
        "manage_fee": "管理费用",
        "research_and_development_expenses": "研发费用",
        "operating_profit": "营业利润",
        "interest_expenses": "利息支出",
        "profit_total": "利润总额",
        "income_tax_expense": "所得税费用",
        "net_profit": "净利润",
        "parent_holder_net_profit": "归属于母公司所有者的净利润",
        "basic_eps": "基本每股收益",
    },
    "balance": {
        "assets_total": "资产总计",
        "total_current_assets": "流动资产合计",
        "non_current_nets_total": "非流动资产合计",
        "cash": "货币资金",
        "accounts_receivable": "应收账款",
        "total_debt": "负债合计",
        "holder_equity_total": "所有者权益(或股东权益)合计",
    },
    "cashflow": {
        "act_cash_flow_net": "经营活动产生的现金流量净额",
        "invest_cash_flow_net": "投资活动产生的现金流量净额",
        "financing_cash_flow_net": "筹资活动产生的现金流量净额",
        "pay_fixed_assets_etc_cash": "购建固定资产、无形资产和其他长期资产所支付的现金",
        "pay_dividends_profits_interest_cash": "分配股利、利润或偿付利息所支付的现金",
        "cash_equivalents_net_addition": "现金及现金等价物净增加额",
    },
}

#: 新浪腿的 ``statement_type`` 词表（搬运链按披露原文的中文表名入库）。
SINA_STATEMENT_TYPES: Mapping[str, str] = {
    "income": "利润表",
    "balance": "资产负债表",
    "cashflow": "现金流量表",
}


class Leg(NamedTuple):
    """One comparison: one issuer, one statement, one reporting mode."""

    label: str
    ths_symbol: str
    reference_symbol: str
    statement_type: str
    period: str
    start_date: date | None = None
    end_date: date | None = None

    @property
    def code(self) -> str:
        """The plain issuer code that deviations are registered under."""
        return self.ths_symbol.partition(".")[0]

    @property
    def key(self) -> tuple[str, str, str, date | None, date | None]:
        """Identity of the observation that other legs must agree with."""
        return (self.code, self.statement_type, self.period, self.start_date, self.end_date)


#: 按缺陷选点，不是随机抽样：映射歧义集中在利润表、三张表各有独立科目表、银行是
#: 另一种报表形态、季报要验累计口径、科创板/创业板各覆盖一个。
LEGS: tuple[Leg, ...] = (
    Leg("贵州茅台 利润表 年报", "600519.SH", "600519", "income", "annual"),
    Leg("贵州茅台 资产负债表 年报", "600519.SH", "600519", "balance", "annual"),
    Leg("贵州茅台 现金流量表 年报", "600519.SH", "600519", "cashflow", "annual"),
    Leg("平安银行 利润表 年报", "000001.SZ", "000001", "income", "annual"),
    Leg("宁德时代 现金流量表 年报", "300750.SZ", "300750", "cashflow", "annual"),
    Leg("中芯国际 利润表 年报", "688981.SH", "688981", "income", "annual"),
    Leg(
        "招商银行 资产负债表 季报窗口",
        "600036.SH",
        "600036",
        "balance",
        "quarterly",
        date(2024, 1, 1),
        date(2025, 12, 31),
    ),
    Leg(
        "贵州茅台 利润表 季报窗口",
        "600519.SH",
        "600519",
        "income",
        "quarterly",
        date(2024, 3, 31),
        date(2025, 3, 31),
    ),
)


class AliasLeg(NamedTuple):
    """A second spelling of ``statement_type`` that must land on one leg's rows."""

    label: str
    ths_symbol: str
    spelled: str
    #: The standard leg this one is compared against (its English statement code).
    baseline: str

    @property
    def code(self) -> str:
        """The plain issuer code."""
        return self.ths_symbol.partition(".")[0]


#: 中文披露名与英文契约名必须落进同一批行：合并键不能因写法分裂。
ALIAS_LEGS: tuple[AliasLeg, ...] = (
    AliasLeg("贵州茅台 利润表 中文写法", "600519.SH", "利润表", "income"),
)


class LegResult(NamedTuple):
    """One leg's measurement, empty when the leg could not be run at all."""

    report: dict[str, Any] | None
    signature: tuple[str, ...]
    key: tuple[str, str, str, date | None, date | None] | None


#: 逐条登记的差异：``(标的裸码, 报表, 报告期末, 判据项)`` → 归因。判据项取值有四类：
#: ths 科目名（值不符）、``科目名#announce_date``（披露日不符）、
#: ``科目名#missing``（新浪该期无此列）、``#period``（ths 多出该报告期）。
#: 登记后一旦重新落回一致即失败：例外不能只增不减。
KNOWN_DEVIATIONS: Mapping[tuple[str, str, date, str], str] = {}


def _fetchers() -> tuple[Fetcher[Any, Any], Fetcher[Any, Any]]:
    """Resolve both providers of the domain through the real routing path."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    registry = get_registry()
    return (
        registry.resolve_domain("financial_statement", source="ths"),
        registry.resolve_domain("financial_statement", source="akshare"),
    )


def _retry(label: str, call: Callable[[], list[FinancialStatement]]) -> list[FinancialStatement]:
    """Run one fetch with the burst-limit backoff.

    Args:
        label: What to name on stderr (the leg and the side).
        call: The routed fetch, already bound to its query keywords.

    Returns:
        The contract rows.

    Raises:
        RuntimeError: The last error after the retries are used up.
    """
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return call()
        except Exception as exc:  # noqa: PERF203  # 上游断连是瞬时的：重试而不是放弃
            last = exc
            print(f"!! {label} attempt {attempt + 1}: {type(exc).__name__}", file=sys.stderr)
            time.sleep(RETRY_SECONDS)
    raise RuntimeError(f"{label}: {RETRY_ATTEMPTS} fetch attempts failed") from last


def _cell(row: FinancialStatement) -> tuple[str, date]:
    """The published fact of one row: its cent value and its disclosure date."""
    return (f"{round(row.value, CENTS):.2f}", row.announce_date)


def _signature(rows: Iterable[FinancialStatement]) -> tuple[str, ...]:
    """Judging signature: one line per row with its cent value and disclosure date."""
    lines: list[str] = []
    for row in rows:
        value, announced = _cell(row)
        lines.append(f"{row.report_period} {row.item} {value} {announced}")
    return tuple(sorted(lines))


def _reference_index(
    rows: list[FinancialStatement],
) -> tuple[dict[tuple[date, str], FinancialStatement], list[str], int]:
    """Index the sina leg by (报告期末, 中文科目) and report its duplicate keys.

    Args:
        rows: Contract rows from the routed akshare/sina fetcher.

    Returns:
        ``(index, conflicts, period_count)``: the lookup, the keys where one
        issuer and one line item carry two different figures (the merge key
        would not be unique, so nothing can be judged), and the report periods.
    """
    index: dict[tuple[date, str], FinancialStatement] = {}
    conflicts: list[str] = []
    for row in rows:
        key = (row.report_period, row.item)
        previous = index.get(key)
        if previous is not None and _cell(previous) != _cell(row):
            conflicts.append(f"{row.report_period} {row.item}")
        index[key] = row
    return index, sorted(set(conflicts)), len({row.report_period for row in rows})


class Diffs(NamedTuple):
    """Structured outcome of one leg: every diff carries its registration key."""

    judged: int
    ours_periods: int
    ref_periods: int
    cells: list[tuple[date, str, str]]
    #: 参照方**对该标的根本不发布这一列**（报表形态差异，不是数据分歧）：银行
    #: 利润表没有「营业成本/销售费用/研发费用/所得税费用/归属于母公司所有者的净
    #: 利润」，资产负债表用「现金及存放中央银行款项」而非「货币资金」、用
    #: 「归属于母公司股东的权益」而非「所有者权益(或股东权益)合计」。逐条打印、不判定。
    uncomparable: list[tuple[str, str]]
    conflicts: list[str]


def _measure(leg: Leg, ours: list[FinancialStatement], ref_rows: list[FinancialStatement]) -> Diffs:
    """Compare both providers cell by cell for one leg.

    Args:
        leg: The leg being judged.
        ours: Rows from the routed ths fetcher.
        ref_rows: Rows from the routed sina fetcher (its full disclosed history).

    Returns:
        The judged cell count, the items this issuer cannot be compared on,
        and every diff that a registration could cover.
    """
    index, conflicts, ref_periods = _reference_index(ref_rows)
    item_map = ITEM_MAP[leg.statement_type]
    ref_columns = {row.item for row in ref_rows}
    uncomparable = [
        (english, chinese)
        for english, chinese in sorted(item_map.items())
        if chinese not in ref_columns
    ]
    seen_periods = {row.report_period for row in ref_rows}
    cells: list[tuple[date, str, str]] = []
    judged = 0
    for row in ours:
        chinese = item_map.get(row.item)
        if chinese is None:
            continue  # 未登记科目：不判，也不因为响应里多给了列就失败
        if chinese not in ref_columns:
            continue  # 该标的没有这一列：形态差异，已记入 uncomparable
        if row.report_period not in seen_periods:
            cells.append(
                (
                    row.report_period,
                    "#period",
                    f"{row.report_period} {leg.statement_type}: ths 独有报告期",
                )
            )
            continue
        reference = index.get((row.report_period, chinese))
        if reference is None:
            cells.append(
                (
                    row.report_period,
                    f"{row.item}#missing",
                    f"{row.report_period} {row.item}: 新浪该期无「{chinese}」列",
                )
            )
            continue
        judged += 1
        ours_cell = _cell(row)
        ref_cell = _cell(reference)
        if ours_cell[0] != ref_cell[0]:
            cells.append(
                (
                    row.report_period,
                    row.item,
                    f"{row.report_period} {row.item}: ths {ours_cell[0]} vs 新浪 {ref_cell[0]}",
                )
            )
        if ours_cell[1] != ref_cell[1]:
            cells.append(
                (
                    row.report_period,
                    f"{row.item}#announce_date",
                    f"{row.report_period} {row.item}: 披露日 {ours_cell[1]} vs {ref_cell[1]}",
                )
            )
    return Diffs(
        judged=judged,
        ours_periods=len({row.report_period for row in ours}),
        ref_periods=ref_periods,
        cells=sorted(set(cells)),
        uncomparable=uncomparable,
        conflicts=conflicts,
    )


def _report(leg: Leg, diffs: Diffs) -> dict[str, Any]:
    """Turn one leg's diffs into the archived report, applying the registry.

    Args:
        leg: The leg being judged.
        diffs: The structured comparison result.

    Returns:
        Counts, the diff texts grouped by class, the items this issuer cannot
        be compared on, notes, and the failure flag.
    """
    notes: list[str] = []
    failed = False
    seen: set[tuple[date, str]] = set()
    grouped: dict[str, list[str]] = {"value": [], "date": [], "missing": [], "period": []}
    for period, judgement, text in diffs.cells:
        seen.add((period, judgement))
        if judgement == "#period":
            grouped["period"].append(text)
        elif judgement.endswith("#missing"):
            grouped["missing"].append(text)
        elif judgement.endswith("#announce_date"):
            grouped["date"].append(text)
        else:
            grouped["value"].append(text)
        attribution = KNOWN_DEVIATIONS.get((leg.code, leg.statement_type, period, judgement))
        if attribution is None:
            failed = True
            notes.append(f"- FAIL {text}（未登记归因）")
        else:
            notes.append(f"- 已登记 {text}：{attribution}")
    for (code, statement, period, judgement), attribution in KNOWN_DEVIATIONS.items():
        if (code, statement) != (leg.code, leg.statement_type) or (period, judgement) in seen:
            continue
        failed = True
        notes.append(
            f"- {code} {statement} {period} {judgement} 已回到一致，登记项须删除：{attribution}"
        )
    if diffs.uncomparable:
        notes.append(
            f"- {leg.code} {leg.statement_type} 不可比科目（参照方未发布该列，"
            f"形态差异而非数据分歧）：{', '.join(e for e, _ in diffs.uncomparable)}"
        )
    if diffs.conflicts:
        failed = True
        notes.append(f"- FAIL 新浪侧同键多值（合并键不唯一）：{diffs.conflicts[:6]}")
    if diffs.judged < MIN_JUDGED_CELLS:
        failed = True
        notes.append(f"- FAIL 判据单元格 {diffs.judged} < {MIN_JUDGED_CELLS}：样本过小不构成证据")
    return {
        "ours_periods": diffs.ours_periods,
        "ref_periods": diffs.ref_periods,
        "judged": diffs.judged,
        "diffs": grouped,
        "uncomparable": list(diffs.uncomparable),
        "notes": notes,
        "failed": failed,
    }


def _mapping_misses(
    reports: Mapping[str, list[list[tuple[str, str]]]],
) -> list[str]:
    """Flag mapped line items that **no** leg's reference page publishes at all.

    An item missing on one issuer is a disclosure-form difference (banks have
    no 营业成本); an item missing on every issuer tested means the mapping
    table itself names a column that does not exist, which would silently
    shrink the judged set to nothing.

    Args:
        reports: Per statement type, one list of uncomparable pairs per leg.

    Returns:
        Failure lines, empty when every mapped item was comparable somewhere.
    """
    misses: list[str] = []
    for statement, per_leg in reports.items():
        if not per_leg:
            continue
        common = set(per_leg[0])
        for uncomparable in per_leg[1:]:
            common &= set(uncomparable)
        for english, chinese in sorted(common):
            misses.append(
                f"- FAIL 科目映射写错：{statement} 的 {english} → 「{chinese}」"
                f"在 {len(per_leg)} 条腿的参照侧都不存在（没有任何腿判到它）"
            )
    return misses


def _run_leg(leg: Leg, ths: Fetcher[Any, Any], sina: Fetcher[Any, Any]) -> LegResult:
    """Fetch both providers for one leg and measure them against each other.

    Args:
        leg: The leg to run (paced by :data:`PACE_SECONDS` first).
        ths: The routed ths fetcher.
        sina: The routed akshare/sina fetcher.

    Returns:
        An empty :class:`LegResult` when either provider could not be reached,
        else the report, its judging signature and its key.
    """
    time.sleep(PACE_SECONDS)
    try:
        ours = _retry(
            f"{leg.label} ths",
            lambda: cast(
                "list[FinancialStatement]",
                list(
                    ths.fetch(
                        symbol=leg.ths_symbol,
                        statement_type=leg.statement_type,
                        period=leg.period,
                        start_date=leg.start_date,
                        end_date=leg.end_date,
                    )
                ),
            ),
        )
        ref = _retry(
            f"{leg.label} 新浪",
            lambda: cast(
                "list[FinancialStatement]",
                list(
                    sina.fetch(
                        symbol=leg.reference_symbol,
                        statement_type=SINA_STATEMENT_TYPES[leg.statement_type],
                    )
                ),
            ),
        )
    except Exception as exc:  # 跑不通也记成 FAIL，不能让整轮证据作废
        print(f"!! {leg.label}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return LegResult(None, (), None)
    diffs = _measure(leg, ours, ref)
    return LegResult(_report(leg, diffs), _signature(ours), leg.key)


def _statement_cell(leg: Leg | AliasLeg) -> str:
    """The statement/mode description shown for a leg in the table."""
    if isinstance(leg, AliasLeg):
        return f"{leg.spelled}（≡{leg.baseline}）"
    window = "" if leg.start_date is None else f" {leg.start_date}~{leg.end_date}"
    return f"{SINA_STATEMENT_TYPES[leg.statement_type]}/{leg.period}{window}"


def _table_row(leg: Leg | AliasLeg, report: Mapping[str, Any] | None, result: str) -> str:
    """Render one markdown table row (11 cells, matching the header in ``main``)."""
    if report is None:
        measures = ["-"] * 7
    else:
        measures = [
            str(report["ours_periods"]),
            str(report["ref_periods"]),
            str(report["judged"]),
            str(len(report["diffs"]["value"])),
            str(len(report["diffs"]["date"])),
            str(len(report["diffs"]["period"]) + len(report["diffs"]["missing"])),
            str(len(report["uncomparable"])),
        ]
    return (
        "| "
        + " | ".join([leg.label, leg.ths_symbol, _statement_cell(leg), *measures, result])
        + " |"
    )


def _alias_deviation(
    alias: AliasLeg,
    rows: list[FinancialStatement],
    observed: Mapping[tuple[str, str, str, date | None, date | None], tuple[str, ...]],
) -> str | None:
    """Describe how a second ``statement_type`` spelling disagrees.

    Args:
        alias: The alias leg, naming its baseline statement code.
        rows: Its contract rows.
        observed: Signatures already measured, keyed by leg identity.

    Returns:
        ``None`` when both spellings carry the identical series, else a
        description of the disagreement.
    """
    baseline_key: tuple[str, str, str, date | None, date | None] = (
        alias.code,
        alias.baseline,
        "annual",
        None,
        None,
    )
    baseline = observed.get(baseline_key)
    if baseline is None:
        return f"{alias.label}: 标准腿 {baseline_key[1]} 未先行观测，无法比对"
    signature = _signature(rows)
    if signature == baseline:
        return None
    extra = sorted(set(signature) - set(baseline))
    short = sorted(set(baseline) - set(signature))
    return (
        f"{alias.ths_symbol} {alias.spelled} 与标准腿 {alias.baseline} 不一致："
        f"多出 {extra[:3]}、缺少 {short[:3]}"
    )


def _run_alias(
    alias: AliasLeg,
    ths: Fetcher[Any, Any],
    observed: Mapping[tuple[str, str, str, date | None, date | None], tuple[str, ...]],
) -> tuple[str, list[str], list[str]]:
    """Fetch one 中文写法 leg and judge it against its standard leg.

    Args:
        alias: The spelling leg to run (paced by :data:`PACE_SECONDS` first).
        ths: The routed ths fetcher.
        observed: Signatures of the standard legs already measured.

    Returns:
        ``(table row, notes, failures)`` for this leg.
    """
    time.sleep(PACE_SECONDS)
    try:
        rows = _retry(
            f"{alias.label} ths",
            lambda: cast(
                "list[FinancialStatement]",
                list(ths.fetch(symbol=alias.ths_symbol, statement_type=alias.spelled)),
            ),
        )
    except Exception as exc:  # 跑不通也记成 FAIL，不能让整轮证据作废
        print(f"!! {alias.label}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return (
            _table_row(alias, None, "FAIL"),
            [],
            [f"- {alias.label}: fetch failed（详见 stderr）"],
        )
    deviation = _alias_deviation(alias, rows, observed)
    if deviation is not None:
        return _table_row(alias, None, "FAIL"), [], [f"- {deviation}"]
    cells = len(_signature(rows))
    note = f"- {alias.label}: 与标准腿逐单元格一致（{cells} 格）"
    return _table_row(alias, None, "PASS"), [note], []


def _header() -> list[str]:
    """Static preamble explaining what the run judges."""
    from opendata.data.providers.ths.endpoints import (
        BALANCE_SHEETS_ENDPOINT,
        CASH_FLOW_STATEMENTS_ENDPOINT,
        INCOME_STATEMENTS_ENDPOINT,
    )

    judged = (
        f"at least {MIN_JUDGED_CELLS} judged cells per leg (a near-empty response proves nothing);"
    )
    return [
        "AC-10 financial_statement (ths) vs 新浪财务报表（akshare 搬运链）",
        "",
        f"script: {REPO_RELATIVE}",
        "ours: get_registry().resolve_domain('financial_statement', source='ths').fetch(...)",
        (
            "          (the routing path; endpoints "
            f"{INCOME_STATEMENTS_ENDPOINT}, "
            f"{BALANCE_SHEETS_ENDPOINT}, "
            f"{CASH_FLOW_STATEMENTS_ENDPOINT})"
        ),
        "reference: get_registry().resolve_domain('financial_statement', source='akshare')",
        "          -- 新浪三张报表（搬运链）：不同的 vendor、不同的抓取链",
        "hard criteria: every mapped line item agrees to the cent --",
        f"          round(value, {CENTS}) identical, not a relative tolerance, because both",
        "          sides publish 元/两位小数 (measured max relative residual 1.38e-16);",
        "          announce_date identical per period (point-in-time queries depend on it);",
        f"          {judged}",
        "          no (symbol, report_period, item) key carrying two different figures;",
        "          no report period on our side that the reference never disclosed;",
        "          判据只覆盖**两侧对该标的都发布了这一列**的格子：某标的不发布某科目",
        "          是报表形态差异（实测：银行利润表无营业成本/销售费用/研发费用/所得税费用，",
        "          资产负债表用「现金及存放中央银行款项」而非「货币资金」），逐条打印为",
        "          不可比科目、不判定；但一个科目若在**所有**腿的参照侧都不存在即映射写错，",
        "          必然失败（否则判据集合会静默缩小）；",
        "          中文 statement_type 写法与英文名落到同一批行。",
        "          A diff fails unless registered in KNOWN_DEVIATIONS with its",
        "          attribution, and a registered diff that falls back into line fails too.",
        "",
    ]


def main(argv: list[str] | None = None) -> int:
    """Run every leg, archive the report, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(description="ths 财务报表跨 vendor 对照（AC-10 / C10）")
    parser.add_argument(
        "--out", default="docs/evidence/C10/ths-financial-statement-cross-check.txt"
    )
    args = parser.parse_args(argv)

    from opendata.data.providers.akshare.models.financial_statement import (
        AkshareFinancialStatementFetcher,
    )
    from opendata.data.providers.ths.models.financial_statement import (
        ThsFinancialStatementFetcher,
    )

    ths, sina = _fetchers()
    if not isinstance(ths, ThsFinancialStatementFetcher):  # 路由必须落在这一条腿上
        raise RuntimeError(f"financial_statement/ths resolves to {type(ths).__name__}")
    if not isinstance(sina, AkshareFinancialStatementFetcher):
        raise RuntimeError(f"financial_statement/akshare resolves to {type(sina).__name__}")

    lines = _header()
    lines.append(
        "| leg | thscode | 报表/口径 | 我们期数 | 新浪期数 | 判据单元格 | 值不符 | 披露日不符 "
        "| 缺期缺列 | 不可比科目 | result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")

    notes: list[str] = []
    failures: list[str] = []
    observed: dict[tuple[str, str, str, date | None, date | None], tuple[str, ...]] = {}
    uncomparable_by_statement: dict[str, list[list[tuple[str, str]]]] = {}
    for leg in LEGS:
        report, signature, key = _run_leg(leg, ths, sina)
        if report is None or key is None:
            failures.append(f"- {leg.label} {leg.ths_symbol}: fetch failed（详见 stderr）")
            lines.append(_table_row(leg, None, "FAIL"))
            continue
        observed[key] = signature
        uncomparable_by_statement.setdefault(leg.statement_type, []).append(report["uncomparable"])
        lines.append(_table_row(leg, report, "FAIL" if report["failed"] else "PASS"))
        notes.extend(report["notes"])
        if report["failed"]:
            detail = ", ".join(
                f"{kind}={report['diffs'][kind][:3]}"
                for kind in ("value", "date", "missing", "period")
                if report["diffs"][kind]
            )
            failures.append(
                f"- {leg.label} {leg.ths_symbol}: judged={report['judged']} "
                f"ours_periods={report['ours_periods']} {detail}"
            )

    for alias in ALIAS_LEGS:
        row, alias_notes, alias_failures = _run_alias(alias, ths, observed)
        lines.append(row)
        notes.extend(alias_notes)
        failures.extend(alias_failures)

    mapping_misses = _mapping_misses(uncomparable_by_statement)
    failures.extend(mapping_misses)

    lines.append("")
    if notes:
        lines.append("## 观测备注（含已登记的跨 vendor 修订）")
        lines.append("")
        lines.extend(dict.fromkeys(notes))
        lines.append("")
    lines.append(f"result: {'FAIL' if failures else 'PASS'} ({len(failures)} failing legs)")
    if failures:
        lines.append("")
        lines.extend(failures)

    report_text = "\n".join(lines) + "\n"
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_text, encoding="utf-8")
    print(report_text, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
