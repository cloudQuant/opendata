"""C34: does ``index_constituent``'s akshare leg answer the same question on *every* index?

C24 measured this pair on one probe symbol (300/300 members, the fallback
carrying an extra ``weight`` column) and refused to flip the ``verified`` bit,
because a single index settles nothing: a fallback that agrees on the symbol the
patrol happens to probe and disagrees on the next one is a second source with a
narrow overlap, not a fallback. This sweep walks the whole set of index codes
both sides can be asked about.

What the first run (2026-09-26) measured, over the 21 codes :data:`CASES` can ask
both sides about: 9 answerable codes agree symbol for symbol - every semiannual
family (沪深 300 / 上证 50 / 中证 500 / 中证 1000 / 上证 180 / 300 医药 / 800 能源 /
800 金地 / 上证能源) - and 4 do not, all of them 科创板 codes, each disagreeing by an
*equal* number of members in both directions (科创 50: 5 in / 5 out; 科创 100:
10 / 10) except 科创综指 (ths one row longer). The remaining 8 codes are
``NO_READING``: the ths index universe resolves no such code, while the
CSI weight file publishes it. That split is the finding this plane exists to keep
in view: the two legs' ``as_of`` do not name the same thing (观测日 vs 文件日), and on
the codes whose publisher reconstitutes in September the difference is visible *in
the membership*, not merely in a date column.

Three verdicts, same asymmetry as ``scripts/ops/akshare_fallback_cross_check.py``:

* ``PASS`` - the two member sets are equal, symbol by symbol;
* ``MISMATCH`` - they differ (the differing codes are printed), or the fallback
  is not self-consistent on the one numeric column it publishes;
* ``NO_READING`` - one side refused or answered zero rows. A refused code is
  evidence about the two catalogs, not about the data, so it never counts
  either way.

Weights are not comparable across the legs (ths publishes none), so the numeric
reading taken here is the fallback's own consistency: the CSI file's weights are
present per row and sum to 100 percent. The ``as_of`` difference is reported, not
judged - the 口径 was decided this round (see :func:`_report_as_of`) - and the
mismatch tally separates 等量换入换出 (the shape two lists from two dates make) from
unequal gaps, because only the latter would point at one side being wrong.

Read-only: csindex and fuyao are queried, nothing is written, no credential printed.

Usage (py313 env; needs the fuyao credentials for the primary legs):
    python docs/evidence/C34/index_constituent_generality_sweep.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import IndexConstituent
    from opendata.data.registry import ProviderRegistry

#: Verdict classes. ``NO_READING`` is the absence of a reading, not a failure.
VERDICT_OK = "PASS"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_NO_READING = "NO_READING"

#: Weights are a percent of the index; the CSI file's own claim, within rounding.
WEIGHT_SUM_TARGET = 100.0
WEIGHT_SUM_TOLERANCE = 0.5


@dataclass(frozen=True)
class Case:
    """One index code, and what the last review measured there.

    Attributes:
        symbol: The code as a caller spells it.
        expected: Verdict the previous sweep recorded.
        reason: Why that verdict, for the archive. Member counts are not pinned:
            an index reconstitution moves them, and a pin that reddens on the
            publisher's own schedule would cry wolf about the pair.
    """

    symbol: str
    expected: str
    reason: str


#: Codes both publishers can be asked about, and the verdict the first C34 run
#: measured on each (2026-09-26, 主源 as_of=观测日 2026-09-26 / 退路 as_of=文件日
#: 2026-08-31). The shape of that reading is the finding: every semiannual family
#: agrees symbol for symbol, and the four 科创板 codes do not - which is the two
#: ``as_of`` semantics showing up *in the membership*, not in a metadata column.
#: The ths-refused codes are kept too: they measure how far apart the two index
#: catalogs are, and a change in either direction is drift rather than silence.
CASES: tuple[Case, ...] = (
    Case("000300", VERDICT_OK, "沪深 300：C24 的探针符号，本轮复测"),
    Case("000016", VERDICT_OK, "上证 50"),
    Case("000905", VERDICT_OK, "中证 500"),
    Case("000852", VERDICT_OK, "中证 1000"),
    Case("000010", VERDICT_OK, "上证 180"),
    Case("000913", VERDICT_OK, "300 医药（19 只）：本轮最小的一条，小样本也要求逐只相同"),
    Case("000928", VERDICT_OK, "800 能源（24 只）"),
    Case("000934", VERDICT_OK, "800 金地"),
    Case("000032", VERDICT_OK, "上证能源"),
    Case("000688", VERDICT_MISMATCH, "科创 50：5 进 5 出，等量换仓"),
    Case("000689", VERDICT_MISMATCH, "科创材料：4 进 4 出，等量换仓"),
    Case("000698", VERDICT_MISMATCH, "科创 100：10 进 10 出，等量换仓"),
    Case("000680", VERDICT_MISMATCH, "科创综指：ths 多 1 只、退路少 1 只，不等量"),
    Case(
        "000988",
        VERDICT_NO_READING,
        "ths 指数目录解析不出该代码（THS_INDEX_SYMBOL_UNRESOLVED），CSI 权重文件有",
    ),
    Case("930050", VERDICT_NO_READING, "同上：CSI 权重文件有该指数，ths 目录没有"),
    Case("000963", VERDICT_NO_READING, "中证下游：ths 目录不认"),
    Case("000825", VERDICT_NO_READING, "央企红利：ths 目录不认"),
    Case("000922", VERDICT_NO_READING, "中证红利：ths 目录不认"),
    Case("000919", VERDICT_NO_READING, "300 价值：ths 目录不认"),
    Case("000949", VERDICT_NO_READING, "中证农业：ths 目录不认"),
    Case("000801", VERDICT_NO_READING, "资源 80：ths 目录不认"),
)


@dataclass(frozen=True)
class Reading:
    """One index's sweep result, with the numbers the verdict was decided from.

    Attributes:
        case: The swept code.
        status: One of the three verdict classes.
        summary: The deciding numbers, for the archive line.
        detail: Per-member disagreements, capped for readability by the caller.
        swapped: Members one side has and the other does not, as
            ``(仅 ths, 仅 akshare)``; ``(0, 0)`` when the two answered at all.
            Equal non-zero counts are the shape of a scheduled reconstitution -
            a list from two dates disagreeing - while an unequal gap is something
            else, and the two must not be reported as one class.
    """

    case: Case
    status: str
    summary: str
    detail: tuple[str, ...]
    swapped: tuple[int, int] = (0, 0)


@dataclass(frozen=True)
class Side:
    """One leg's answer for one index: rows, or the reason there are none."""

    rows: tuple[IndexConstituent, ...]
    error: str


_REGISTRY: ProviderRegistry | None = None


def _registry() -> ProviderRegistry:
    """The process registry, with every provider registered once."""
    global _REGISTRY
    if _REGISTRY is None:
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        _REGISTRY = get_registry()
    return _REGISTRY


def _fetch(source: str, symbol: str) -> Side:
    """Ask one leg for one index, turning any failure into an attributable no-reading.

    Args:
        source: ``ths`` or ``akshare``.
        symbol: The index code as written in :data:`CASES`.

    Returns:
        The leg's rows, or the exception text that stands in for them.
    """
    fetcher = _registry().resolve("index", "index_constituent", source=source)
    try:
        rows = cast("Sequence[IndexConstituent]", fetcher.fetch(symbol=symbol))
    except Exception as exc:  # a refused code says nothing about the data
        return Side((), f"{type(exc).__name__}: {str(exc)[:120]}")
    return Side(tuple(rows), "")


def _dates(side: Side) -> str:
    """The distinct ``as_of`` values one leg published."""
    return "/".join(sorted({row.as_of.isoformat() for row in side.rows})) or "-"


def _weights(side: Side) -> tuple[int, float, float]:
    """How many rows carry a weight, and the sum and minimum of those weights."""
    values = [row.weight for row in side.rows if row.weight is not None]
    if not values:
        return 0, 0.0, 0.0
    return len(values), sum(values), min(values)


def sweep_one(case: Case) -> Reading:
    """Compare both legs' membership for one index code.

    Args:
        case: The code being swept.

    Returns:
        The verdict with the numbers that decided it.
    """
    primary = _fetch("ths", case.symbol)
    fallback = _fetch("akshare", case.symbol)
    if primary.error or fallback.error:
        parts = [
            f"{source} 未答：{side.error}"
            for source, side in (("ths", primary), ("akshare", fallback))
            if side.error
        ]
        return Reading(case, VERDICT_NO_READING, "；".join(parts), ())
    if not primary.rows or not fallback.rows:
        return Reading(
            case,
            VERDICT_NO_READING,
            f"空答案 ths={len(primary.rows)} akshare={len(fallback.rows)}",
            (),
        )

    ref = {row.symbol for row in primary.rows}
    cand = {row.symbol for row in fallback.rows}
    only_primary = sorted(ref - cand)
    only_fallback = sorted(cand - ref)
    counted, total, smallest = _weights(fallback)
    detail = [f"{member}: 仅 ths 认它是成分" for member in only_primary[:12]]
    detail += [f"{member}: 仅 akshare 认它是成分" for member in only_fallback[:12]]
    if counted != len(cand) or abs(total - WEIGHT_SUM_TARGET) > WEIGHT_SUM_TOLERANCE:
        detail.append(f"退路自身不自洽：权重 {counted}/{len(cand)} 行有值、合计 {total:.2f}")
    summary = (
        f"成员 ths={len(ref)} akshare={len(cand)} 共同={len(ref & cand)}"
        f" 仅 ths={len(only_primary)} 仅 akshare={len(only_fallback)}"
        f" | akshare 权重 {counted}/{len(cand)} 行有值，合计 {total:.2f}（最小 {smallest:.4f}）"
        f" | as_of ths={_dates(primary)} akshare={_dates(fallback)}"
    )
    status = VERDICT_MISMATCH if detail else VERDICT_OK
    return Reading(
        case,
        status,
        summary,
        tuple(detail),
        (len(only_primary), len(only_fallback)),
    )


def _report_as_of(readings: Sequence[Reading]) -> None:
    """Print the date column, which is reported and not judged.

    The 口径 was decided this round: ``as_of`` names the snapshot the *source*
    publishes (design §4.1: one row per ``(index, constituent, as_of)``, so a
    date range reconstructs membership at any past rebalance). The CSI weight
    file carries that date; the fuyao endpoint publishes no list date at all, and
    its ``as_of`` is the observation day. Both legs keep publishing what they
    actually have - the gap is therefore a property of the pair to record, not a
    disagreement about membership, and no judge here can close it by asserting.

    Args:
        readings: The sweep's per-index readings.
    """
    print("\n===== as_of 口径（报告，不判定）=====")
    print("  本轮拍定：as_of = 该源所发布清单的所属快照日（契约 docstring 已写明）")
    print("  ths 端点不发布清单日期，其 as_of 是观测日（C9 起如此，本轮未改）")
    for reading in readings:
        if reading.status == VERDICT_NO_READING:
            continue
        print(f"  {reading.case.symbol}: {reading.summary.split('| as_of ')[-1]}")


def main() -> int:
    """Sweep every code, pin the archived verdicts, and print the flip reading.

    Returns:
        0 when every reading matches :data:`CASES`; 1 on drift, because a
        silently stale verdict table is an over-claim.
    """
    print("===== C34 index_constituent 跨指数加宽复核（真机、只读、不写数仓、不打印密钥）=====")
    print(
        f"python={sys.version.split()[0]}  指数代码 {len(CASES)} 个"
        f"  权重合计容差={WEIGHT_SUM_TOLERANCE}"
    )
    readings = [sweep_one(case) for case in CASES]
    drift = 0
    print("\n===== 逐指数读数 =====")
    for reading in readings:
        agrees = reading.status == reading.case.expected
        if not agrees:
            drift += 1
        marker = "" if agrees else f"；归档判定={reading.case.expected}"
        if reading.status == VERDICT_NO_READING:
            marker += f"（归档理由：{reading.case.reason}）"
        print(f"  [{reading.status:10s}] {reading.case.symbol}: {reading.summary}{marker}")
        for line in reading.detail[:12]:
            print(f"                 {line}")
        if len(reading.detail) > 12:
            print(f"                 …另有 {len(reading.detail) - 12} 条同类不符")

    _report_as_of(readings)

    judged = [r for r in readings if r.status != VERDICT_NO_READING]
    agreed = [r for r in judged if r.status == VERDICT_OK]
    swapped = [
        r for r in judged if r.status == VERDICT_MISMATCH and r.swapped[0] == r.swapped[1] > 0
    ]
    uneven = [r for r in judged if r.status == VERDICT_MISMATCH and r not in swapped]
    print("\n===== 转正判据（由上面的读数算出，不是写死的）=====")
    print(f"  可判读指数 {len(judged)} 个：成员集合逐只相同 {len(agreed)} 个")
    print(
        f"  等量换入换出 {len(swapped)} 个（{', '.join(r.case.symbol for r in swapped) or '无'}）"
        "：两腿各自清单所属日期不同的典型形状"
    )
    print(
        f"  其它不符 {len(uneven)} 个（{', '.join(r.case.symbol for r in uneven) or '无'}）"
        "：不是换仓形状，需单独归因"
    )
    print(f"  不可判读 {len(readings) - len(judged)} 个（一侧未答，不参与任何方向的结论）")
    decision = (
        "支持把 akshare 的 index_constituent 腿翻 verified"
        if judged and len(agreed) == len(judged)
        else "存在不符，保持 verified=false"
    )
    print(f"  ⇒ {decision}")
    print("\n===== 与归档判据的一致性 =====")
    if drift:
        print(f"  DRIFT：{drift} 个代码的判定与 CASES 归档不一致")
        return 1
    print(f"  {len(CASES)} 个代码的判定与 CASES 归档逐条相同")
    return 0


if __name__ == "__main__":
    sys.exit(main())
