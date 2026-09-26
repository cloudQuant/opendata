"""Prove the C27 qfq judges can actually fail, and prove the A1 one could not.

C26 的教训是「一条不会失败的判据不是判据」。本轮把 A1 那条恒真判据换成了三条新的
（``scripts/codemod/qfq_chain_checks.py``，门禁与 AC-6 报告跑的就是这一份代码），
所以必须现场演示三件事：

* 旧判据（因子 := ``qfq_close/raw_close``，再断言 ``raw_close*factor == qfq_close``）
  对着**故意做坏**的 qfq 序列照样通过——因为它唯一的输入就是它要复现的那个量。
* 新判据对着同一批坏数据逐条失败，且每一点扰动都落到一条具体的差异行上。
* 失败方式各不相同：台阶判据看事件流（分红明细端点）、合成判据看另外三个价格字段与
  量/额、窗口自洽判据看两次录制是否还在说同一件事——覆盖面不重叠，才不是一条判据换了三个名字。
  反过来，一条**合法**的等比换锚三条都必须放过，抓到就是假阳性，同样让退出码变红。

不改动任何夹具：坏数据在这里就地构造，只喂给被测判据。

Usage (py313 env):
    python docs/evidence/C27/judge-falsification.py
"""

from __future__ import annotations

import sys
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.codemod import compare_with_upstream as comparator  # noqa: E402
from scripts.codemod import qfq_chain_checks as checks  # noqa: E402

#: 三条判据跑的夹具：sina 宽窗（118 天，内含 2024-06-19 除权日）。
RAW_CASE, QFQ_CASE = checks.WIDE_PAIR

#: 每种坏数据都应当被至少一条判据抓到；漏网的写进「未被抓到」里，会让退出码变红。
CORRUPTIONS = ("unrelated", "step_delayed", "wrong_height", "one_field")
EVENT_MUTATIONS = ("cash_10x", "cash_removed", "date_shifted")
WINDOW_MUTATIONS = ("overlap_close_off_by_a_cent", "overlap_factor_moved")
#: 合法扰动：整条等比换锚不改变任何相对形状，三条判据都必须放过它。
#: 抓到就等于把某一家的锚定常数当成了事实——所以「被抓到」同样让退出码变红。
WINDOW_TOLERATED = ("uniform_anchor",)


def _frame(case: str) -> pd.DataFrame:
    """One recorded frame, columns unified the way the judges expect them."""
    path = comparator.FIXTURES_DIR / case / "reference.csv.gz"
    return checks.normalize(comparator._read_reference_frame(path))


def _dividends() -> dict:
    """The recorded dividend detail, as the step judge reads it."""
    path = comparator.FIXTURES_DIR / "stock_action_dividend" / "reference.csv.gz"
    return checks.cash_dividends(comparator._read_reference_frame(path))


def _target_index(dates: list, dividends: dict) -> int:
    """Index of the in-window ex-date the corruptions are aimed at.

    Args:
        dates: Window trade dates, in order.
        dividends: ``ex_date -> cash`` as recorded.

    Returns:
        Position of the first recorded 实施 cash ex-date inside the window.

    Raises:
        RuntimeError: When the window holds no usable dividend row at all.
    """
    in_window = [day for day in sorted(dividends) if day in dates]
    if not in_window:
        raise RuntimeError("窗口里没有任何已实施的现金分红，坏数据无从构造")
    return dates.index(in_window[0])


def _window_diffs(
    narrow_raw: pd.DataFrame,
    narrow_qfq: pd.DataFrame,
    wide_raw: pd.DataFrame,
    wide_qfq: pd.DataFrame,
) -> list[str]:
    """The window-consistency judge's failure lines (its summary goes to the print)."""
    return checks.check_window_consistency(narrow_raw, narrow_qfq, wide_raw, wide_qfq)[1]


def _run(verdicts: dict[str, str], label: str, call: Callable[[], list[str]]) -> None:
    """Call one judge and record PASS/FAIL without letting a failure stop the tour.

    Args:
        verdicts: Collected ``label -> verdict`` rows, mutated in place.
        label: Row to print.
        call: Zero-arg callable returning the judge's difference lines.

    An ``ERROR`` means the scaffold broke, which must never be read as a judgement
    about the data — it fails the run.
    """
    try:
        diffs = call()
    except Exception as exc:
        verdicts[label] = "ERROR"
        print(f"  {label}: 脚手架异常 {type(exc).__name__}: {exc}")
        return
    if diffs:
        verdicts[label] = "FAIL"
        print(f"  {label}: FAIL <- {diffs[0][:150]}")
    else:
        verdicts[label] = "PASS"
        print(f"  {label}: PASS")


def old_a1_judge(raw_close: list[float], qfq_close: list[float]) -> bool:
    """The A1 leftover, verbatim in miniature: derive the factor, then reproduce it.

    Args:
        raw_close: Unadjusted closes.
        qfq_close: The "official" qfq closes being checked.

    Returns:
        ``True`` when every day satisfies ``|raw * (qfq/raw) - qfq| <= |qfq| * RTOL``.
    """
    for raw, official in zip(raw_close, qfq_close, strict=True):
        if abs(raw * (official / raw) - official) > abs(official) * comparator.RTOL:
            return False
    return True


def _corrupted(kind: str, raw: pd.DataFrame, qfq: pd.DataFrame, ex_index: int) -> pd.DataFrame:
    """Build a deliberately wrong official-qfq frame.

    Args:
        kind: Which defect to inject.
        raw: The unadjusted frame (read only).
        qfq: The recorded qfq frame (copied, never mutated in place).
        ex_index: Row the ex-date step sits on.

    Returns:
        A mutated copy of the recorded qfq frame.

    Raises:
        KeyError: On an unknown ``kind``, so a typo cannot silently no-op.
    """
    frame = qfq.copy()
    raw_close = checks.floats_of(raw, "close")
    factors = checks.factor_chain(raw, frame)

    if kind == "uniform":  # 整条等比缩放：只换前复权的锚定常数，不动台阶形状
        for column in ("open", "high", "low", "close"):
            frame[column] = frame[column].astype(float) * 0.5
    elif kind == "unrelated":  # 收盘与标的无关（模拟取错列 / 取错标的）
        frame["close"] = list(reversed(raw_close))
    elif kind == "step_delayed":  # 台阶推迟一个交易日：除权日算错一天
        moved = factors[:ex_index] + [factors[ex_index - 1]] + factors[ex_index + 1 :]
        frame["close"] = [r * f for r, f in zip(raw_close, moved, strict=True)]
    elif kind == "wrong_height":  # 台阶高度改成 1.5%：金额算错，日期仍对
        bumped = [
            f if index < ex_index else factors[ex_index - 1] * 1.015
            for index, f in enumerate(factors)
        ]
        frame["close"] = [r * f for r, f in zip(raw_close, bumped, strict=True)]
    elif kind == "one_field":  # 只有 high 多乘 1%：单字段口径错，收盘序列不动
        frame["high"] = frame["high"].astype(float) * 1.01
    else:
        raise KeyError(f"unknown corruption {kind!r}")
    return frame


def _window_corrupted(
    kind: str,
    overlap_index: int,
    wide_raw: pd.DataFrame,
    wide_qfq: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Break the wide pair so it no longer matches the narrow recording.

    Args:
        kind: Which defect to inject.
        overlap_index: Row of the wide frame that the narrow frame also covers.
        wide_raw: The wide unadjusted frame (copied).
        wide_qfq: The wide official qfq frame (copied).

    Returns:
        The mutated ``(raw, qfq)`` pair for the judge's wide-window slots.

    Raises:
        KeyError: On an unknown ``kind``.
    """
    raw, qfq = wide_raw.copy(), wide_qfq.copy()
    if kind == "overlap_close_off_by_a_cent":
        # 重叠日的不复权收盘差一分钱：两次录制说的不是同一只票（或有一个窗口取错了行）
        closes = checks.floats_of(raw, "close")
        raw["close"] = [
            value + 0.01 if index == overlap_index else value for index, value in enumerate(closes)
        ]
        return raw, qfq
    if kind == "overlap_factor_moved":
        # 重叠日的复权序列整体被挪了 0.5%：不复权收盘仍然一致，但两个窗口的链形状对不上
        for column in ("open", "high", "low", "close"):
            values = checks.floats_of(qfq, column)
            qfq[column] = [
                value * 1.005 if index == overlap_index else value
                for index, value in enumerate(values)
            ]
        return raw, qfq
    if kind == "uniform_anchor":
        # 整条等比平移：同一条链换个锚定常数，判据**故意**放过（见 读法）
        for column in ("open", "high", "low", "close"):
            qfq[column] = qfq[column].astype(float) * 1.001
        return raw, qfq
    raise KeyError(f"unknown window corruption {kind!r}")


def _shifted(items: dict, dates: list) -> dict:
    """Move every in-window ex-date one trading day later (an off-by-one event feed).

    Args:
        items: ``ex_date -> cash`` as recorded.
        dates: Window trade dates, in order.

    Returns:
        A copy with the in-window dates pushed forward by one row.
    """
    out = dict(items)
    for day, cash in items.items():
        if day in dates and day != dates[-1]:
            del out[day]
            out[dates[dates.index(day) + 1]] = cash
    return out


def main() -> int:
    """Run the tour and report whether every judge behaved as claimed.

    Returns:
        ``0`` when the old judge survived all five close-side corruptions, every
        corruption, event mutation and window mutation is caught by some new judge,
        the anchor shift the judges deliberately ignore stays uncaught, the untouched
        control passes, and nothing in the scaffold errored.
    """
    raw, qfq = _frame(RAW_CASE), _frame(QFQ_CASE)
    dates = checks.dates_of(raw)
    raw_close = checks.floats_of(raw, "close")
    dividends = _dividends()
    ex_index = _target_index(dates, dividends)
    narrow_raw, narrow_qfq = _frame(checks.NARROW_PAIR[0]), _frame(checks.NARROW_PAIR[1])
    narrow_dates = checks.dates_of(narrow_raw)
    overlap_index = next(
        index for index, day in enumerate(dates) if day == narrow_dates[len(narrow_dates) // 2]
    )
    print(
        f"宽窗 {dates[0]}..{dates[-1]}（{len(dates)} 天），靶心除权日 {dates[ex_index]}；"
        f"窄窗 {len(narrow_dates)} 天，重叠行 {dates[overlap_index]}"
    )

    print()
    print("== 1. 旧判据（A1 恒真）对着坏数据的表现 ==")
    survived = 0
    for kind in ("uniform", *CORRUPTIONS):
        bad = checks.floats_of(_corrupted(kind, raw, qfq, ex_index), "close")
        hit = old_a1_judge(raw_close, bad)
        survived += int(hit)
        print(f"  qfq 改成 {kind:<12} -> 旧判据 {'仍然 PASS（恒真）' if hit else 'FAIL'}")

    print()
    print("== 2. 三条新判据对着同一批坏数据的表现 ==")
    verdicts: dict[str, str] = {}
    for kind in ("uniform", *CORRUPTIONS):
        bad = _corrupted(kind, raw, qfq, ex_index)
        _run(
            verdicts,
            f"{kind:<12} 台阶判据",
            partial(checks.check_factor_steps, raw, bad, dividends),
        )
        _run(verdicts, f"{kind:<12} 合成判据", partial(checks.check_synthesis, raw, bad))
        _run(
            verdicts,
            f"{kind:<12} 窗口自洽",
            partial(_window_diffs, narrow_raw, narrow_qfq, raw, bad),
        )

    print()
    print("== 3. 事件流侧的扰动（换掉判据的另一路输入，而不是价格数据） ==")
    mutations: dict[str, Callable[[dict], dict]] = {
        "cash_10x": lambda items: {day: cash / 10 for day, cash in items.items()},
        "cash_removed": lambda _: {},
        "date_shifted": lambda items: _shifted(items, dates),
    }
    for kind, mutate in mutations.items():
        mutated = mutate(dividends)
        _run(
            verdicts,
            f"{kind:<12} 台阶判据",
            partial(checks.check_factor_steps, raw, qfq, mutated),
        )

    print()
    print("== 4. 窗口侧的扰动（两次录制之间的一致性，只有这一条判据看得见） ==")
    for kind in (*WINDOW_MUTATIONS, *WINDOW_TOLERATED):
        broken_raw, broken_qfq = _window_corrupted(kind, overlap_index, raw, qfq)
        _run(
            verdicts,
            f"{kind:<26} 窗口自洽",
            partial(_window_diffs, narrow_raw, narrow_qfq, broken_raw, broken_qfq),
        )

    print()
    print("== 5. 对照组：什么都不改 ==")
    _run(verdicts, "untouched    台阶判据", partial(checks.check_factor_steps, raw, qfq, dividends))
    _run(verdicts, "untouched    合成判据", partial(checks.check_synthesis, raw, qfq))
    _run(
        verdicts,
        "untouched    窗口自洽",
        partial(_window_diffs, narrow_raw, narrow_qfq, raw, qfq),
    )

    print()
    print("== 读法 ==")
    print(f"  旧判据在 {len(CORRUPTIONS) + 1} 种坏收盘数据上 PASS 了 {survived} 次。")
    print("    它挂在 skip 上半年没人发现，就因为这个。")
    for label, verdict in verdicts.items():
        print(f"  {label}: {verdict}")
    print(
        "  uniform 只换前复权的锚定常数，台阶/合成/窗口三条判据的相对形状都不该变，所以都不该抓到；"
    )
    print("    被抓到反而说明某条判据把这一家的锚定当成了事实。")
    print("  one_field 只错 high：收盘台阶判据与窗口自洽天然看不见，由合成判据兜住——覆盖面不重叠。")
    print("  step_delayed/wrong_height 的扰动在 6 月，重叠区只到 1 月，窗口自洽判据管不着——")
    print("    那正是台阶判据的活：三条判据各管一段，没有一条是另一条的换名。")
    print("  窗口侧两条只有窗口自洽判据看得见：两次录制各自对自己的转写都是 PASS，回放看不出来。")

    errors = sorted(label for label, verdict in verdicts.items() if verdict == "ERROR")
    missed = [
        kind
        for kind in (*CORRUPTIONS, *EVENT_MUTATIONS, *WINDOW_MUTATIONS)
        if not any(
            label.startswith(kind) and verdict == "FAIL" for label, verdict in verdicts.items()
        )
    ]
    false_positives = [
        kind
        for kind in WINDOW_TOLERATED
        if any(label.startswith(kind) and verdict == "FAIL" for label, verdict in verdicts.items())
    ]
    control = tuple(verdict for label, verdict in verdicts.items() if label.startswith("untouched"))
    ok = (
        survived == len(CORRUPTIONS) + 1
        and control == ("PASS", "PASS", "PASS")
        and not errors
        and not missed
        and not false_positives
    )
    print(f"  未被任何新判据抓到的坏数据 = {missed or '无'}")
    print(f"  被误抓的合法扰动（等比换锚）= {false_positives or '无'}")
    print(f"  脚手架异常 = {errors or '无'}")
    print(f"JUDGE_FALSIFIED_EXIT={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
