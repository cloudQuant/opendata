"""ths index constituents against the index publisher's own list (AC-10).

``index_constituent`` reached C1 as an akshare-only capability (the CSI
close-weight file). C9 adds the fuyao endpoint, whose response carries no
weight and no effective date, so this run is the proof for ``verified=true``:
membership is compared member-by-member against the publisher's own current
list, on five standard indices plus the second spelling of one of them.

What the run can and cannot judge, and why:

* the reference is ``index_stock_cons_csindex`` (中证指数公司官网当日清单) -
  a different vendor, and the party that decides who is in the index;
* the month-end weight file is **not** the reference. Measured on 2026-06-08
  windows: it agreed with the live list on 300/300, 50/50 and 500/500 members
  but differed on 5 of 50 for 科创50, purely because its ``日期`` is the last
  month-end while both live channels are today's;
* ``weight`` is reported, never judged: the ths endpoint does not publish it,
  so the contract rows carry ``None``. Comparing weights would mean borrowing
  the reference's own numbers, which proves nothing;
* a THS concept board is fetched as an observation-only leg - no independent
  publisher exists for 同花顺's own board membership.

Pass criteria: identical member sets (Jaccard 1.0000) and an as-of date within
one day of the publisher's. A missing or extra member fails unless it is
registered in ``KNOWN_DEVIATIONS`` with its attribution, and a registered
member that later reappears fails too - exceptions cannot accumulate.

Usage (py313 env; every leg calls two vendors live, so the run needs network).
The fuyao endpoint refuses bursts (measured: consecutive calls inside one
second come back ``ConnectError``), so the script paces itself:

    python scripts/ops/ths_index_constituent_cross_check.py
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

REPO_RELATIVE = Path(__file__).resolve().relative_to(ROOT)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from opendata.data.models import IndexConstituent
    from opendata.data.protocol import Fetcher

#: 秒。上游对连发直接断连（实测），留足间隔才把「跑不通」和「数据不对」分开。
PACE_SECONDS = 12.0
#: 断连后的重试间隔与次数（证据脚本要跑完，不要一断就废）。
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 5


class Leg(NamedTuple):
    """One comparison: the ths index code and the publisher's own code."""

    label: str
    ths_symbol: str
    #: ``None`` marks an observation-only leg (no independent publisher).
    reference_symbol: str | None
    #: ``standard`` judges membership; ``duplicate`` re-reads one index under
    #: its second code; ``board`` has no reference at all.
    kind: str = "standard"


#: Five standard indices covering 50/300/500/1000 members, chosen so that a
#: one-index bug (wrong suffix, dropped leading zero) cannot pass unnoticed.
STANDARD_LEGS: tuple[Leg, ...] = (
    Leg("沪深300", "000300.SH", "000300"),
    Leg("上证50", "000016.SH", "000016"),
    Leg("中证500", "000905.SH", "000905"),
    Leg("科创50", "000688.SH", "000688"),
    Leg("中证1000", "000852.SH", "000852"),
)

#: 同一指数的两种 thscode 写法：集合必须与彼此、也与参照侧完全一致。
DUPLICATE_LEGS: tuple[Leg, ...] = (Leg("沪深300 另一种写法", "399300.SZ", "000300", "duplicate"),)

#: 板块成分只观测（同花顺自定板块没有独立发布方可校）。
OBSERVATION_LEGS: tuple[Leg, ...] = (Leg("同花顺板块", "886042.TI", None, "board"),)

ALL_LEGS: tuple[Leg, ...] = STANDARD_LEGS + DUPLICATE_LEGS + OBSERVATION_LEGS


class LegResult(NamedTuple):
    """One leg's measurement, empty when the leg could not be run at all."""

    report: dict[str, Any] | None
    rows: list[IndexConstituent]
    ours_as_of: date | None
    ref_as_of: date | None


#: 快照日与发布方清单日的最大允许相差天数。两侧都是「当日观测」，允许 1 天只
#: 是为了跨过运行时刻恰好落在上海零点两侧的情况。
MAX_AS_OF_LAG_DAYS = 1
#: 成员码形状（A 股裸码 6 位数字）：不满足即为解析口径出错，而不是数据差异。
MEMBER_CODE_LENGTH = 6

#: 逐条登记的成员差异：``(指数代码, 成员代码, 方向)`` → 归因。方向为
#: ``ths_only``（我们多出）或 ``reference_only``（我们缺少）。
KNOWN_DEVIATIONS: Mapping[tuple[str, str, str], str] = {}


def _ths_fetcher() -> Fetcher[Any, Any]:
    """Resolve the routed ths fetcher exactly as a caller would."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry().resolve_domain("index_constituent", source="ths")


def _reference_members(symbol: str) -> tuple[set[str], date]:
    """Read 中证官网的当日成分股清单，返回裸码集合与其清单日期."""
    import opendata.data.providers.akshare._vendor as opendata_http

    frame = opendata_http.index_stock_cons_csindex(symbol=symbol)
    members = {str(code).strip().zfill(MEMBER_CODE_LENGTH) for code in frame["成分券代码"]}
    return members, max(frame["日期"])


def _ours_members(
    fetcher: Fetcher[Any, Any], symbol: str
) -> tuple[list[IndexConstituent], date | None]:
    """Fetch one index through the routing path, with the burst-limit backoff.

    Args:
        fetcher: The routed ``index_constituent`` ths fetcher.
        symbol: The index code as a caller would write it.

    Returns:
        The contract rows and their shared ``as_of``.

    Raises:
        Exception: The last error after the retries are used up.
    """
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            rows = cast("list[IndexConstituent]", list(fetcher.fetch(symbol=symbol)))
            return rows, rows[0].as_of if rows else None
        except Exception as exc:  # noqa: PERF203  # 上游断连是瞬时的：重试而不是放弃
            # 把「跑不通」和「数据不对」分开：只有重试用尽才算这条腿失败。
            last = exc
            print(f"!! {symbol} attempt {attempt + 1}: {type(exc).__name__}", file=sys.stderr)
            time.sleep(RETRY_SECONDS)
    raise RuntimeError(f"{symbol}: {RETRY_ATTEMPTS} fetch attempts failed") from last


def _plain_index_code(ths_symbol: str) -> str:
    """The stored index code: the thscode without its exchange suffix."""
    return ths_symbol.partition(".")[0]


def _compare(leg: Leg, rows: list[IndexConstituent], reference: set[str]) -> dict[str, Any]:
    """Measure one leg against the publisher's list.

    Args:
        leg: The leg being judged.
        rows: Contract rows from the routed ths fetcher.
        reference: The publisher's member codes for the same index.

    Returns:
        Counts, the Jaccard ratio, unregistered/registered/stale diffs, the
        shape and weight observations, and the failure flag.
    """
    ours = {row.symbol for row in rows}
    stored_index = {row.index_symbol for row in rows}
    index_code = _plain_index_code(leg.ths_symbol)
    union = ours | reference
    report: dict[str, Any] = {
        "ours": len(ours),
        "reference": len(reference),
        "jaccard": len(ours & reference) / len(union) if union else 0.0,
        "ths_only": sorted(ours - reference),
        "reference_only": sorted(reference - ours),
        "notes": [],
        "duplicate_rows": len(rows) - len(ours),
        "bad_shape": sorted(code for code in ours if not _is_plain_code(code)),
        "weight_none": all(row.weight is None for row in rows),
        "index_symbol": sorted(stored_index),
        "failed": False,
    }
    for direction in ("ths_only", "reference_only"):
        for code in report[direction]:
            registered = KNOWN_DEVIATIONS.get((index_code, code, direction))
            if registered is None:
                report["failed"] = True
            else:
                report["notes"].append(f"- {index_code} {code}（{direction}）：{registered}")
    for (registered_index, code, direction), attribution in KNOWN_DEVIATIONS.items():
        if registered_index != index_code:
            continue
        if code not in report[direction]:
            report["failed"] = True
            report["notes"].append(
                f"- {registered_index} {code}（{direction}）已回到一致，登记项须删除：{attribution}"
            )
    if report["index_symbol"] != [index_code]:
        report["notes"].append(
            f"- {leg.ths_symbol}: index_symbol 存的是 {report['index_symbol']}，应为 [{index_code}]"
        )
        report["failed"] = True
    if report["duplicate_rows"] or report["bad_shape"] or not report["weight_none"]:
        report["failed"] = True
    return report


def _is_plain_code(code: str) -> bool:
    """Whether a stored member code is the plain six-digit form."""
    return len(code) == MEMBER_CODE_LENGTH and code.isdigit()


def _observe(leg: Leg, rows: list[IndexConstituent]) -> dict[str, Any]:
    """Measure a board leg on shape only: 同花顺自定板块没有独立发布方可校.

    Args:
        leg: The observation-only leg.
        rows: Contract rows from the routed ths fetcher.

    Returns:
        The same report shape as :func:`_compare` with the set comparison
        blanked out: membership cannot be judged without a publisher.
    """
    ours = {row.symbol for row in rows}
    stored_index = sorted({row.index_symbol for row in rows})
    index_code = _plain_index_code(leg.ths_symbol)
    duplicate_rows = len(rows) - len(ours)
    bad_shape = sorted(code for code in ours if not _is_plain_code(code))
    weight_none = all(row.weight is None for row in rows)
    index_ok = stored_index == [index_code]
    return {
        "ours": len(ours),
        "reference": 0,
        "jaccard": float("nan"),
        "ths_only": [],
        "reference_only": [],
        "notes": []
        if index_ok
        else [f"- {leg.ths_symbol}: index_symbol 存的是 {stored_index}，应为 [{index_code}]"],
        "duplicate_rows": duplicate_rows,
        "bad_shape": bad_shape,
        "weight_none": weight_none,
        "index_symbol": stored_index,
        "failed": bool(duplicate_rows or bad_shape) or not (weight_none and index_ok),
    }


def _duplicate_deviation(
    leg: Leg, rows: list[IndexConstituent], observed: Mapping[str, set[str]]
) -> str | None:
    """Describe how a second spelling of an index disagrees with the standard leg.

    Args:
        leg: The duplicate leg, naming both spellings.
        rows: Its contract rows.
        observed: Member sets already seen, keyed by plain index code.

    Returns:
        ``None`` when the two spellings carry the identical list, else a
        description of the extra and missing members.
    """
    baseline = observed.get(str(leg.reference_symbol))
    if baseline is None:
        return f"{leg.label}: 标准腿 {leg.reference_symbol} 未先行观测，无法比对"
    ours = {row.symbol for row in rows}
    if ours == baseline:
        return None
    return (
        f"{leg.ths_symbol} 与标准腿 {leg.reference_symbol} 成员不一致："
        f"多出 {sorted(ours - baseline)}、缺少 {sorted(baseline - ours)}"
    )


def _lag_days(ours: date | None, theirs: date) -> int:
    """Absolute day gap between our observation date and the publisher's."""
    return abs((ours - theirs).days) if ours is not None else 999


def _table_row(
    leg: Leg, report: Mapping[str, Any] | None, ours_as_of: date | None, ref_as_of: date | None
) -> str:
    """Render one markdown table row (11 cells, matching the header in ``main``)."""
    if ours_as_of is None:
        window = "-"
    elif ref_as_of is None:
        window = str(ours_as_of)
    else:
        window = f"{ours_as_of} / {ref_as_of}"
    if report is None:
        cells = [leg.label, leg.ths_symbol, "-", window, *["-"] * 7]
        return "| " + " | ".join(cells) + " |"
    observed_only = leg.kind == "board"
    cells = [
        "无独立发布方" if observed_only else str(report["reference"]),
        window,
        str(report["ours"]),
        "-" if observed_only else f"{report['jaccard']:.4f}",
        "-" if observed_only else (", ".join(report["ths_only"]) or "-"),
        "-" if observed_only else (", ".join(report["reference_only"]) or "-"),
        str(report["duplicate_rows"]),
        "是" if report["weight_none"] else "否",
        "OBSERVE" if observed_only else ("PASS" if not report["failed"] else "FAIL"),
    ]
    return "| " + " | ".join([leg.label, leg.ths_symbol, *cells]) + " |"


def _measure_leg(leg: Leg, fetcher: Fetcher[Any, Any]) -> LegResult:
    """Fetch and measure one leg, turning a dead endpoint into a blank result.

    Args:
        leg: The leg to run (paced by :data:`PACE_SECONDS` first).
        fetcher: The routed ``index_constituent`` ths fetcher.

    Returns:
        An empty :class:`LegResult` when either vendor could not be reached,
        else the report, its rows and the two observation dates.
    """
    time.sleep(PACE_SECONDS)
    try:
        rows, ours_as_of = _ours_members(fetcher, leg.ths_symbol)
        if leg.kind == "board":
            return LegResult(_observe(leg, rows), rows, ours_as_of, None)
        reference, ref_as_of = _reference_members(str(leg.reference_symbol))
        return LegResult(_compare(leg, rows, reference), rows, ours_as_of, ref_as_of)
    except Exception as exc:  # 跑不通也记成 FAIL，不能让整轮证据作废
        print(f"!! {leg.ths_symbol}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return LegResult(None, [], None, None)


def _header(fetch_endpoint: str) -> list[str]:
    """Static preamble explaining what the run judges."""
    return [
        "AC-10 index_constituent (ths) vs the index publisher's own list",
        "",
        f"script: {REPO_RELATIVE}",
        "ours: get_registry().resolve_domain('index_constituent', source='ths').fetch(...)",
        f"          (the routing path; endpoint {fetch_endpoint})",
        "official: 中证指数官网当日成分股清单（index_stock_cons_csindex）",
        "          -- different vendor, and the party that decides membership",
        "hard criteria: identical member sets (jaccard == 1.0000);",
        f"          as-of within {MAX_AS_OF_LAG_DAYS} day of the publisher's date;",
        "          no duplicate rows; every stored member code plain six-digit;",
        "          index_symbol stored as the plain code on both sides;",
        "          weight None on every row (the endpoint publishes no weights).",
        "          A diff fails unless registered in KNOWN_DEVIATIONS, and a",
        "          registered diff that falls back into line fails too.",
        "",
    ]


def _run(
    legs: tuple[Leg, ...], fetcher: Fetcher[Any, Any], lines: list[str]
) -> tuple[list[str], list[str]]:
    """Run every leg, append table rows, and return the notes and failure strings.

    Args:
        legs: Legs in run order (a standard leg must precede its duplicate).
        fetcher: The routed ``index_constituent`` ths fetcher.
        lines: Output list to append markdown table rows to.

    Returns:
        ``(notes, failures)``: observation notes and failing legs.
    """
    notes: list[str] = []
    failures: list[str] = []
    observed: dict[str, set[str]] = {}
    for leg in legs:
        report, rows, ours_as_of, ref_as_of = _measure_leg(leg, fetcher)
        if report is None:
            failures.append(f"- {leg.label} {leg.ths_symbol}: fetch failed（详见 stderr）")
            lines.append(_table_row(leg, None, None, None))
            continue
        if ref_as_of is not None:
            lag = _lag_days(ours_as_of, ref_as_of)
            if lag > MAX_AS_OF_LAG_DAYS:
                report["failed"] = True
                failures.append(
                    f"- {leg.label}: as_of {ours_as_of} vs 发布方 {ref_as_of}（{lag} 天）"
                )
        if leg.kind == "duplicate":
            deviation = _duplicate_deviation(leg, rows, observed)
            if deviation is not None:
                report["failed"] = True
                failures.append(f"- {deviation}")
        if leg.kind != "board":
            observed[_plain_index_code(leg.ths_symbol)] = {row.symbol for row in rows}
        lines.append(_table_row(leg, report, ours_as_of, ref_as_of))
        notes.extend(report["notes"])
        if report["failed"]:
            failures.append(
                f"- {leg.label} {leg.ths_symbol}: ths_only={report['ths_only'][:8]} "
                f"reference_only={report['reference_only'][:8]} "
                f"duplicates={report['duplicate_rows']} bad_shape={report['bad_shape'][:5]} "
                f"index_symbol={report['index_symbol']} weight_none={report['weight_none']}"
            )
    return notes, failures


def main(argv: list[str] | None = None) -> int:
    """Run every leg, archive the report, and exit non-zero on failure."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/evidence/C9/ths-index-constituent-cross-check.txt")
    args = parser.parse_args(argv)

    from opendata.data.providers.ths.endpoints import INDEX_CONSTITUENTS_ENDPOINT
    from opendata.data.providers.ths.models.index_constituent import ThsIndexConstituentFetcher

    fetcher = _ths_fetcher()
    if not isinstance(fetcher, ThsIndexConstituentFetcher):  # 路由必须落在这一条腿上
        raise RuntimeError(f"index_constituent/ths resolves to {type(fetcher).__name__}")

    lines = _header(INDEX_CONSTITUENTS_ENDPOINT)
    lines.append(
        "| leg | thscode | 参照行数 | as_of 我们/发布方 | 成员数 | jaccard "
        "| ths 多出 | 参照多出 | 重复行 | weight 全空 | result |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    notes, failures = _run(ALL_LEGS, fetcher, lines)

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

    report = "\n".join(lines) + "\n"
    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report, end="")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
