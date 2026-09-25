"""Widen the two C24 readings that a single probe cannot settle (task #31).

``scripts/ops/akshare_fallback_cross_check.py`` reuses the patrol probe for
every domain, which is the right discipline for asking "can the fallback take
over the question we actually ask" - and the wrong sample size to flip a
``verified`` bit on. Two domains read PASS there on one symbol:

* ``stock_action``: one issuer, two years, four ex-dates;
* ``index_constituent``: one index, one snapshot date.

This sweep asks the same question of six issuers over ten years and four CSI
indexes, because a fallback that agrees on the probe symbol and disagrees on
the next one is not a fallback - it is a second source with a narrow overlap.
Both scripts are read-only: fuyao, sina and csindex are queried, nothing is
written and no credential is printed.

Verdicts follow the cross-check's asymmetry: a fallback that drops a row or a
field the primary publishes has stopped answering (MISMATCH); one that carries
more is still answering (noted, not a failure). Different ``as_of`` dates are
reported so a membership diff can be attributed rather than assumed.

Usage (py313 env; needs the fuyao credentials):
    python docs/evidence/C24/fallback_generality_sweep.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.data.protocol import Fetcher, QueryParams

#: ``600519``/``000651`` distribute, ``601318`` and ``000333`` mix cash with
#: bonus capital, ``002415``/``600036`` add other boards and cadences.
ACTION_SYMBOLS: tuple[str, ...] = ("600519", "000651", "600036", "601318", "000333", "002415")
ACTION_START = date(2015, 1, 1)
ACTION_END = date(2024, 12, 31)
ACTION_FIELDS: tuple[str, ...] = (
    "cash_dividend",
    "stock_dividend",
    "rights_shares",
    "rights_price",
)

#: CSI indexes with a close-weight file on both sides: SSE50, CSI300, CSI500,
#: CSI1000 - different member counts and different rebalance histories.
CONSTITUENT_INDEXES: tuple[str, ...] = ("000300.SH", "000016.SH", "000905.SH", "000852.SH")

#: Relative tolerance on a payout, and absolute tolerance on a weight (percent).
TOLERANCE = 1e-4
WEIGHT_TOLERANCE = 5e-3

#: The window both legs get for the corporate-action section.
_WINDOW = {"start_date": ACTION_START, "end_date": ACTION_END}


def _fetcher(source: str, domain: str, asset_class: str) -> Fetcher[QueryParams, object]:
    """Resolve one leg from the live registry, registering providers once."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    resolved = get_registry().resolve(asset_class, domain, source=source)
    return cast("Fetcher[QueryParams, object]", resolved)


def _rows(source: str, domain: str, asset_class: str, **query: Any) -> list[Any]:  # noqa: ANN401
    """Fetch one request through one leg."""
    return list(_fetcher(source, domain, asset_class).fetch(**query))


def _gap(left: float, right: float) -> float:
    """Scale-aware distance between two readings."""
    return abs(left - right) / max(abs(left), abs(right), 1.0e-9)


def _index_by(rows: list[Any], key: str) -> dict[str, Any]:
    """Index rows by one field's string value, keeping the first row per key."""
    indexed: dict[str, Any] = {}
    for row in rows:
        indexed.setdefault(str(getattr(row, key)), row)
    return indexed


def _diff_lines(
    shared: list[str],
    ref: dict[str, Any],
    cand: dict[str, Any],
    fields: tuple[str, ...],
    tolerance: float,
) -> list[str]:
    """One line per shared key whose field values disagree past tolerance."""
    lines: list[str] = []
    for key in shared:
        for field in fields:
            left = float(getattr(ref[key], field))
            right = float(getattr(cand[key], field))
            gap = _gap(left, right)
            if gap > tolerance:
                lines.append(f"{key} {field}: ths={left} akshare={right} 相对差={gap:.8f}")
    return lines


def sweep_actions(symbol: str) -> tuple[str, list[str]]:
    """Compare both legs' corporate actions for one issuer.

    Args:
        symbol: Plain six-digit code.

    Returns:
        The summary line and one line per disagreement.
    """
    primary = _rows("ths", "stock_action", "equity", symbol=symbol, **_WINDOW)
    fallback = _rows("akshare", "stock_action", "equity", symbol=symbol, **_WINDOW)
    ref = _index_by(primary, "ex_date")
    cand = _index_by(fallback, "ex_date")
    shared = sorted(set(ref) & set(cand))
    only_primary = sorted(set(ref) - set(cand))
    only_fallback = sorted(set(cand) - set(ref))

    lines = _diff_lines(shared, ref, cand, ACTION_FIELDS, TOLERANCE)
    lines += [f"{day}: 仅 ths 有此事件" for day in only_primary]
    lines += [f"{day}: 仅 akshare 有此事件" for day in only_fallback]
    summary = (
        f"ths={len(ref)} akshare={len(cand)} 共同={len(shared)}"
        f" 仅 ths={len(only_primary)} 仅 akshare={len(only_fallback)} 不符={len(lines)}"
    )
    return summary, lines


def _weight_diff_lines(
    shared: list[str], ref: dict[str, float | None], cand: dict[str, float | None]
) -> list[str]:
    """One line per member whose published weights disagree past tolerance."""
    lines: list[str] = []
    for member in shared:
        left, right = ref[member], cand[member]
        if left is None or right is None:
            continue
        delta = abs(left - right)
        if delta > WEIGHT_TOLERANCE:
            lines.append(f"{member} 权重: ths={left} akshare={right} 绝对差={delta:.4f}")
    return lines


def _as_of(rows: list[Any]) -> str:
    """The snapshot date one leg published (``-`` when it published nothing)."""
    return str(rows[0].as_of) if rows else "-"


def sweep_constituents(index: str) -> tuple[str, list[str]]:
    """Compare both legs' membership and weights for one CSI index.

    Args:
        index: Suffixed index code as the patrol names it.

    Returns:
        The summary line and one line per disagreement.
    """
    primary = _rows("ths", "index_constituent", "index", symbol=index)
    fallback = _rows("akshare", "index_constituent", "index", symbol=index)
    ref = {str(row.symbol): row.weight for row in primary}
    cand = {str(row.symbol): row.weight for row in fallback}
    shared = sorted(set(ref) & set(cand))
    only_primary = sorted(set(ref) - set(cand))
    only_fallback = sorted(set(cand) - set(ref))

    lines = _weight_diff_lines(shared, ref, cand)
    lines += [f"{member}: 仅 ths 认它是成分" for member in only_primary]
    lines += [f"{member}: 仅 akshare 认它是成分" for member in only_fallback]
    summary = (
        f"as_of ths={_as_of(primary)} akshare={_as_of(fallback)}"
        f" ths={len(ref)} akshare={len(cand)} 共同={len(shared)}"
        f" 仅 ths={len(only_primary)} 仅 akshare={len(only_fallback)} 不符={len(lines)}"
    )
    return summary, lines


def _walk(
    title: str,
    cases: tuple[str, ...],
    sweep: Callable[[str], tuple[str, list[str]]],
    unit: str,
) -> int:
    """Run one section of the sweep and report it.

    Args:
        title: Section heading.
        cases: The symbols walked.
        sweep: The comparison for one symbol.
        unit: Noun used in the tally line.

    Returns:
        The number of symbols whose two legs disagreed.
    """
    print(f"\n===== {title} =====")
    disagreements = 0
    for case in cases:
        try:
            summary, lines = sweep(case)
        except Exception as exc:  # a refused symbol is no reading, not a disagreement
            print(f"  [NO_READING] {case}: {type(exc).__name__}: {str(exc)[:150]}")
            continue
        status = "MISMATCH" if lines else "PASS"
        if lines:
            disagreements += 1
        print(f"  [{status:8s}] {case}: {summary}")
        for line in lines[:12]:
            print(f"             {line}")
        if len(lines) > 12:
            print(f"             …另有 {len(lines) - 12} 条同类不符")
    print(f"  小结：{unit} {len(cases)} 个，其中两侧不一致 {disagreements} 个")
    return disagreements


def main() -> int:
    """Walk both sections and print the flip decision each way."""
    print("===== C24 转正候选的加宽复核（真机、只读、不写数仓、不打印密钥）=====")
    print(
        f"python={sys.version.split()[0]}  事件字段相对容差={TOLERANCE}"
        f" 权重绝对容差={WEIGHT_TOLERANCE}"
    )

    action_bad = _walk(
        f"stock_action 跨符号（{ACTION_START}..{ACTION_END}，每股口径）",
        ACTION_SYMBOLS,
        sweep_actions,
        "发行主体",
    )
    constituent_bad = _walk(
        "index_constituent 跨指数（成员集合 + 权重）",
        CONSTITUENT_INDEXES,
        sweep_constituents,
        "指数",
    )

    print("\n===== 转正判定 =====")
    sections = (
        ("stock_action", action_bad, len(ACTION_SYMBOLS), "符号"),
        ("index_constituent", constituent_bad, len(CONSTITUENT_INDEXES), "指数"),
    )
    for domain, bad, total, unit in sections:
        decision = "跨样本一致，可考虑转正" if not bad else "跨样本不符，保持 verified=false"
        print(f"  {domain}: {decision}（不符 {bad}/{total} {unit}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
