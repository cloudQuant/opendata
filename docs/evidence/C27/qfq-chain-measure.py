"""Measure the复权因子链 the fixtures can and cannot pin (AC-6 A1 leftover / AC-11).

``test_d10_qfq_synthesis_matches_official_series`` and ``_check_d10_qfq`` build the
factor as ``qfq_close / raw_close`` and then assert that ``apply_adjust`` reproduces
``qfq_close``. ``apply_adjust`` computes ``close * ratio`` (``opendata/data/adjust.py:66``),
so that assertion is true by construction: it can only fail if multiplication is
broken. It has been sitting behind a skip since A2.5, which is exactly why nobody
read it as circular. Before widening anything, this script measures what the
fixtures *can* say:

* A - 每家 vendor 内部：同一天的复权比例按四个价格字段分别算一遍（open/high/low/close）。
  复权是一个标量乘数 ⇒ 四个比值必须一致。这是可从夹具**独立**证伪的判据（不是
  ``apply_adjust`` 的自证），并把最大相对差留档。
* B - 跨 vendor：em 与 sina 的 ``f(t)=qfq_close/raw_close`` 各自都带一个自己的锚定常数
  （两家的前复权基准日不同），所以比值本身没有意义；有意义的是 **r(t)=f_em(t)/f_sina(t)
  是否为常数**。r 随日期变动 ⇒ 两家在某个除权日上对不上，即因子链事实分歧。
* C - 因子链是否在本窗口内**有台阶**：窗口里没有除权日的话，f(t) 逐日恒定，A/B 两判据
  都退化成「一个常数等于另一个常数」——这条直接决定夹具窗口该不该放宽到跨一个除权日。
* D - 录制帧的**标的同一性**：回放判据是「搬运层 == 上游在同一份转写上的返回」，
  所以两家都取错标的时回放照样 PASS（这是 AC-6 的结构性盲区）。D 段用转写之外的信息
  做对照：同一天 600519 的不复权收盘在 em/sina 两家必须相等（交易所口径的原始价格），
  对不上就说明有一家的 secid/symbol 解析拿错了标的。
* E - 台阶的**日期与高度**是否等于分红事件算出来的台阶：台阶日必须能在 ``stock_action_dividend``
  （另一个上游端点）里找到一条「实施」的除权除息日，且高度等于 ``P_prev/(P_prev-cash)``。
  反方向也查：窗口里出现了已实施现金分红却没有台阶 = 因子链漏事件。
  这条与 ``qfq_close/raw_close`` 的自证无关，是本轮用来替换 A1 那条恒真判据的候选。
* F - **窗口自洽**：本轮把录制窗口从 22 天放宽到跨一个除权日，所以必须先证明窄窗与宽窗
  说的是同一只票、同一段行情——同一端点在重叠日的不复权收盘要逐位相等；再证明
  ``f_宽/f_窄`` 只是个常数（锚定不随请求窗口移动）。两条都成立，放宽窗口才只是「把同一条
  链截得更长」；只要 q(t) 在重叠区里跳，任何跨窗口的因子比较都不成立。

Reads only the recorded fixtures (``tests/fixtures/upstream/``); no network at all.

Usage (py313 env):
    python docs/evidence/C27/qfq-chain-measure.py
"""

from __future__ import annotations

import gzip
import io
import statistics
import sys
from pathlib import Path
from typing import TYPE_CHECKING

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    import pandas as pd

FIXTURES = ROOT / "tests" / "fixtures" / "upstream"

#: ``(case, date column, price columns)`` for the four daily-line fixtures.
EM_RAW = ("stock_daily_raw", "日期", ("开盘", "收盘", "最高", "最低"))
EM_QFQ = ("stock_daily_qfq", "日期", ("开盘", "收盘", "最高", "最低"))
SINA_RAW = ("stock_daily_sina_raw", "date", ("open", "close", "high", "low"))
SINA_QFQ = ("stock_daily_sina_qfq", "date", ("open", "close", "high", "low"))
#: C27 recorded these two over the *widened* window. Only this pair shares dates
#: with the em pair that straddle 2024-06-19, so only this pair can carry section B.
SINA_RAW_WIDE = ("stock_daily_sina_raw_wide", "date", ("open", "close", "high", "low"))
SINA_QFQ_WIDE = ("stock_daily_sina_qfq_wide", "date", ("open", "close", "high", "low"))

#: 「台阶」判据的阈值。上游价格是两位小数，所以 f(t)=qfq/raw 自带第 6~7 位的**舍入噪声**：
#: 本机实测（见 C 段打印）sina 窄窗 22 天的相邻日抖动中位数 1e-6 量级、最大也只有
#: 5.453e-06；而 sina 宽窗跨 2024-06-19 除权日的台阶是 2.071e-02 —— 高出三个数量级。
#: 早期版本按 round(v,10) 数「取值种类」判台阶，于是把噪声数成了「114 种取值 = 有台阶」，
#: 对不含除权日的窄窗也会给出假结论；1e-4 落在噪声与真实台阶正中间的空白带里。
STEP_TOLERANCE = 1e-4


def _load(case: str) -> pd.DataFrame | None:
    """Read one recorded reference frame, or None when it is not recorded.

    Args:
        case: Fixture directory name.

    Returns:
        The frame (all cells as text, as the harness stores them), or None.
    """
    import pandas as pd

    path = FIXTURES / case / "reference.csv.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return pd.read_csv(io.StringIO(handle.read()), dtype=str)


def _per_date_factors(
    raw: pd.DataFrame, adjusted: pd.DataFrame, spec_raw: tuple[str, tuple[str, ...]]
) -> dict[str, dict[str, float]]:
    """Compute ``adjusted / raw`` per date and per price field.

    Args:
        raw: The unadjusted frame.
        adjusted: The qfq frame for the same symbol/window.
        spec_raw: ``(date column, price columns)`` as recorded for that channel.

    Returns:
        ``date -> {field: ratio}``, keeping only dates present in both frames.
    """
    date_column, price_columns = spec_raw
    right = adjusted.set_index(date_column)
    factors: dict[str, dict[str, float]] = {}
    for row in raw.to_dict("records"):
        day = str(row[date_column])
        if day not in right.index:
            continue
        other = right.loc[day]
        entry: dict[str, float] = {}
        for field in price_columns:
            base = float(row[field])
            if base:
                entry[field] = float(other[field]) / base
        if entry:
            factors[day] = entry
    return factors


#: 台阶**高度**的对照阈值。台阶日期由 E 段和分红夹具对上之后，还要看高度：
#: 本机实测 predicted=1.0207134730 vs actual=1.0207102910，close 相对差 3.117e-06、
#: 四个字段最大 4.5e-07~3.1e-06（价格是两位小数，误差就来自这里）。取 1e-4：
#: 比实测噪声高一个数量级，比「分红金额取错/漏了一天」这类真错误（1e-2 量级）低两个数量级。
STEP_MATCH_TOLERANCE = 1e-4

#: 分红明细（数据中心端点），与日线是**两个不同的上游接口**——E 段用它来定量核对台阶高度。
DIVIDENDS = "stock_action_dividend"


def _dividend_rows() -> list[tuple[str, str, float]]:
    """Read the recorded dividend fixture as ``(ex_date, status, cash per share)``.

    Returns:
        One tuple per row; ``status`` is ``"ok"`` for a pure cash dividend that is
        already 实施 (so ``P/(P-cash)`` applies), ``"unusable"`` for a row this
        formula does not cover (送转 非零、进度不是「实施」、或没有现金), and
        ``"no-fixture"`` for every row when nothing is recorded.
    """
    frame = _load(DIVIDENDS)
    if frame is None:
        return []
    rows: list[tuple[str, str, float]] = []
    for row in frame.to_dict("records"):
        cash = float(row["派息"] or 0) / 10.0
        bonus = float(row["送股"] or 0) + float(row["转增"] or 0)
        usable = str(row["进度"]) == "实施" and bonus == 0.0 and cash != 0.0
        rows.append((str(row["除权除息日"]), "ok" if usable else "unusable", cash))
    return rows


def _dividend_on(ex_date: str) -> tuple[str, float]:
    """Look the recorded corporate action up for one ex-date.

    Args:
        ex_date: ISO date the chain stepped on.

    Returns:
        ``(status, cash per share)`` mirroring :func:`_dividend_rows`, with
        ``"missing"`` when the fixture has no row for that date at all.
    """
    rows = _dividend_rows()
    if not rows:
        return "missing", 0.0
    for day, status, cash in rows:
        if day == ex_date:
            return status, cash
    return "missing", 0.0


def _ok_dividend_map() -> dict[str, float]:
    """``ex_date -> cash per share`` for every usable row (see :func:`_dividend_rows`)."""
    return {day: cash for day, status, cash in _dividend_rows() if status == "ok"}


def _explain_steps(
    label: str,
    steps: dict[str, float],
    raw: pd.DataFrame | None,
    date_column: str,
    close_column: str,
) -> bool:
    """Section E for one vendor: does each step match the dividend record?

    Args:
        label: Vendor/channel name to print.
        steps: ``ex_date -> signed relative jump`` of ``f(t)``, from section C.
        raw: The unadjusted frame (needed for the previous close).
        date_column: Date column on that channel.
        close_column: Close column on that channel.

    Returns:
        ``True`` when something disagrees.
    """
    if raw is None:
        print(f"  {label}: 未判读，缺不复权夹具")
        return False
    previous = {str(row[date_column]): float(row[close_column]) for row in raw.to_dict("records")}
    days = sorted(previous)
    bad = False
    for ex_date, change in sorted(steps.items()):
        index = days.index(ex_date) if ex_date in days else -1
        if index <= 0:
            print(f"  {label} {ex_date}: 未判读，窗口里没有该日的前一个交易日")
            bad = True
            continue
        prev_close = previous[days[index - 1]]
        status, cash = _dividend_on(ex_date)
        if status == "missing":
            print(f"  {label} {ex_date}: 台阶无法解释——分红夹具里这一天没有任何行")
            bad = True
            continue
        if status == "unusable":
            print(f"  {label} {ex_date}: 未判读——有行但不是「纯现金且已实施」的方案（送转/进度）")
            bad = True
            continue
        predicted = prev_close / (prev_close - cash)
        actual = 1.0 + change
        gap = abs(actual / predicted - 1)
        flag = "OK" if gap <= STEP_MATCH_TOLERANCE else "不一致"
        print(
            f"  {label} {ex_date}: 前收 {prev_close:g} 每股现金 {cash:g} → "
            f"predicted={predicted:.10f} actual={actual:.10f} 相对差 {gap:.3e} {flag}"
        )
        bad = bad or gap > STEP_MATCH_TOLERANCE
    for ex_date, cash in sorted(_ok_dividend_map().items()):
        if ex_date in days and ex_date != days[0] and ex_date not in steps:
            print(
                f"  {label}: 漏台阶——{ex_date} 有已实施现金分红 {cash:g}/股，但 f(t) 在该日没有跳变"
            )
            bad = True
    if not bad and not steps:
        print(f"  {label}: 窗口内既没有台阶也没有应出现台阶的分红日——本节无判据可言")
    return bad


def _field_consistency(label: str, factors: dict[str, dict[str, float]]) -> None:
    """Print section A for one vendor: do the four price fields agree?

    Args:
        label: Vendor name to print.
        factors: ``date -> {field: ratio}`` from :func:`_per_date_factors`.
    """
    worst = 0.0
    worst_day = "-"
    for day, entry in sorted(factors.items()):
        if len(entry) < 2:
            continue
        spread = (max(entry.values()) - min(entry.values())) / abs(statistics.fmean(entry.values()))
        if spread > worst:
            worst, worst_day = spread, day
    print(f"  {label}: 可比日期 {len(factors)} 天，四字段比值最大相对差 {worst:.3e}（{worst_day}）")


def _factor_pair(
    label: str,
    raw_spec: tuple[str, str, tuple[str, ...]],
    qfq_spec: tuple[str, str, tuple[str, ...]],
) -> dict[str, dict[str, float]] | None:
    """Load one vendor's raw+qfq fixtures and return its per-date, per-field ratios.

    Args:
        label: Vendor/channel name to print when the pair is incomplete.
        raw_spec: ``(case, date column, price columns)`` for the unadjusted line.
        qfq_spec: The same for the official qfq line.

    Returns:
        ``date -> {field: ratio}``, or None when either fixture is still pending.
    """
    raw = _load(raw_spec[0])
    qfq = _load(qfq_spec[0])
    if raw is None or qfq is None:
        pending = [spec[0] for spec in (raw_spec, qfq_spec) if _load(spec[0]) is None]
        print(f"  {label}: 未判读，缺 {pending}")
        return None
    return _per_date_factors(raw, qfq, raw_spec[1:])


def _compare_raw_closes(label: str, em: pd.DataFrame, sina: pd.DataFrame) -> bool:
    """Section D: do the two vendors' *unadjusted* closes agree day by day?

    Args:
        label: Which sina window is being compared.
        em: The em unadjusted frame (Chinese columns).
        sina: The sina unadjusted frame (English columns).

    Returns:
        ``True`` when at least one shared date disagrees — i.e. the frames are not
        demonstrably about the same instrument.
    """
    em_close = {str(r["日期"]): float(r["收盘"]) for r in em.to_dict("records")}
    sina_close = {str(r["date"]): float(r["close"]) for r in sina.to_dict("records")}
    shared = sorted(set(em_close) & set(sina_close))
    if not shared:
        print(f"  {label}: 未判读（没有重叠交易日）")
        return False
    diffs = [(abs(em_close[day] - sina_close[day]), day) for day in shared]
    worst, worst_day = max(diffs)
    exact = sum(1 for value, _ in diffs if value == 0.0)
    only_em = len(set(em_close) - set(sina_close))
    only_sina = len(set(sina_close) - set(em_close))
    print(
        f"  {label}: 重叠 {len(shared)} 天（{shared[0]}..{shared[-1]}），"
        f"不复权收盘逐位相等 {exact}/{len(shared)} 天，最大绝对差 {worst:g}（{worst_day}）"
    )
    print(f"    仅在 em {only_em} 天 / 仅在 sina {only_sina} 天（日历与停牌语义差异，不判对错）")
    return worst > 0


def _compare_window_closes(
    narrow: pd.DataFrame | None,
    wide: pd.DataFrame | None,
    date_column: str,
    close_column: str,
) -> tuple[int, int, float, str]:
    """Section F: the same channel over two windows must agree on shared dates.

    Args:
        narrow: Frame recorded over the short window.
        wide: Frame recorded over the widened window.
        date_column: Date column on that channel.
        close_column: Close column on that channel.

    Returns:
        ``(shared, exact, worst_abs_diff, worst_day)``; ``shared`` is 0 when a frame
        is missing, which the caller prints as 未判读 rather than as agreement.
    """
    if narrow is None or wide is None:
        return 0, 0, 0.0, "-"
    left = {str(r[date_column]): float(r[close_column]) for r in narrow.to_dict("records")}
    right = {str(r[date_column]): float(r[close_column]) for r in wide.to_dict("records")}
    shared = sorted(set(left) & set(right))
    if not shared:
        return 0, 0, 0.0, "-"
    diffs = [(abs(left[day] - right[day]), day) for day in shared]
    worst, worst_day = max(diffs)
    return len(shared), sum(1 for value, _ in diffs if value == 0.0), worst, worst_day


def _anchor_ratio_across_windows(
    narrow: dict[str, dict[str, float]] | None,
    wide: dict[str, dict[str, float]] | None,
) -> None:
    """Print section F's factor half: is the qfq anchor window-independent?

    C27 widened the recorded window specifically to straddle an ex-date, which only
    means what the reader thinks it means if the two windows describe the same
    series. If ``f_wide/f_narrow`` is constant on the shared days, the two frames
    differ only by the anchor each request happened to use — harmless, and section
    E stays valid within one frame. If it jumps inside the overlap, then a factor
    chain computed over one window is **not** a restriction of the chain over
    another, and no window may be mixed with another.

    Args:
        narrow: ``date -> {field: ratio}`` for the short window.
        wide: The same for the widened window.
    """
    if narrow is None or wide is None:
        print("  未判读：窄窗或宽窗因子链没有录全")
        return
    shared = sorted(set(narrow) & set(wide))
    if len(shared) < 3:
        print(f"  未判读：重叠只有 {len(shared)} 天")
        return
    ratios = {day: wide[day]["close"] / narrow[day]["close"] for day in shared}
    anchor = statistics.median(ratios.values())
    jumps = [
        day
        for index, day in enumerate(shared[1:], start=1)
        if abs(ratios[day] / ratios[shared[index - 1]] - 1) > STEP_TOLERANCE
    ]
    worst = max(abs(value / anchor - 1) for value in ratios.values())
    print(
        f"  重叠 {len(shared)} 天（{shared[0]}..{shared[-1]}），"
        f"q(t)=f_宽(t)/f_窄(t) 中位数={anchor:.6f}，最大相对偏离 {worst:.3e}"
    )
    print(f"  q(t) 跳变日：{jumps or '无'}")
    if jumps:
        print("    -> 锚定随请求窗口移动：不同窗口录到的复权链不能混用")
    else:
        print("    -> 锚定与窗口无关（只差一个常数），放宽窗口只是把同一条链截得更长")


def main() -> int:
    """Run A/B/C over the recorded fixtures and print what they can say.

    Returns:
        ``0`` when the em pair and at least one sina pair are recorded; ``1`` while
        any of them is still pending, because then nothing below has been measured.
    """
    print("== A. 每家 vendor 内部：四价格字段算出的复权比例是否一致 ==")
    em_factors = _factor_pair("em  ", EM_RAW, EM_QFQ)
    sina_narrow = _factor_pair("sina(窄窗，原夹具)", SINA_RAW, SINA_QFQ)
    sina_wide = _factor_pair("sina(宽窗，跨 2024-06-19)", SINA_RAW_WIDE, SINA_QFQ_WIDE)
    for label, factors in (
        ("em  ", em_factors),
        ("sina(窄)", sina_narrow),
        ("sina(宽)", sina_wide),
    ):
        if factors:
            _field_consistency(label, factors)

    print()
    print("== B. 跨 vendor：f_em(t)/f_sina(t) 是否为常数（锚定日不同只抬常数，不该抬形状） ==")
    sina_for_b, b_note = (sina_wide, "宽窗") if sina_wide else (sina_narrow, "窄窗（回退）")
    if em_factors is None or sina_for_b is None:
        print("  未判读：em 或 sina 因子链尚未录全，B 段不做任何结论")
    else:
        shared = sorted(set(em_factors) & set(sina_for_b))
        if not shared:
            print(f"  未判读：两家{b_note}窗口没有重叠交易日")
        else:
            ratios = {day: em_factors[day]["close"] / sina_for_b[day]["close"] for day in shared}
            anchor = statistics.median(ratios.values())
            worst = max(abs(value / anchor - 1) for value in ratios.values())
            print(
                f"  {b_note}：重叠 {len(shared)} 天（{shared[0]}..{shared[-1]}），"
                f"anchor(中位数)={anchor:.6f}，最大相对偏离 {worst:.3e}"
            )
            steps = [
                day
                for index, day in enumerate(shared[1:], start=1)
                if abs(ratios[day] / ratios[shared[index - 1]] - 1) > STEP_TOLERANCE
            ]
            print(f"  r(t) 发生跳变的交易日：{steps or '无（整窗一个常数）'}")
            if not steps:
                print("    -> 注意：没有台阶就意味着 B 段还没有真正证伪过任何东西")

    print()
    print("== C. 因子链在本窗口内有没有台阶（没有除权日的窗口证不了因子链） ==")
    has_step = False
    chain_steps: dict[str, dict[str, float]] = {}
    specs: dict[str, tuple[str, str, tuple[str, ...]]] = {}
    for label, factors, raw_spec in (
        ("em", em_factors, EM_RAW),
        ("sina(窄)", sina_narrow, SINA_RAW),
        ("sina(宽)", sina_wide, SINA_RAW_WIDE),
    ):
        if factors is None:
            print(f"  {label}: 未录全，跳过")
            continue
        closes = {day: entry["close"] for day, entry in sorted(factors.items()) if "close" in entry}
        if not closes:
            print(f"  {label}: 无数据")
            continue
        values = list(closes.values())
        jumps = {
            day: values[i] / values[i - 1] - 1 for i, day in enumerate(list(closes)[1:], start=1)
        }
        if not jumps:
            print(f"  {label}: 只有 1 天，无台阶可言")
            continue
        jitter_steps = {day: value for day, value in jumps.items() if abs(value) > STEP_TOLERANCE}
        chain_steps[label] = jitter_steps
        specs[label] = raw_spec
        print(
            f"  {label}: {len(values)} 天，相邻日相对抖动 中位数(噪声地板)="
            f"{statistics.median([abs(v) for v in jumps.values()]):.3e} / "
            f"最大={max(abs(v) for v in jumps.values()):.3e}，"
            f"首末相对变化 {abs(values[-1] / values[0] - 1):.3e}，"
            f"取值 {len({round(v, 10) for v in values})} 种"
        )
        print(f"    台阶日（抖动 > {STEP_TOLERANCE:.0e}）：{jitter_steps or '无'}")
        verdict = "窗口内含除权日，因子链有台阶" if jitter_steps else "整窗恒定：A/B 判据在此退化"
        print(f"    -> {verdict}（台阶判据阈值 {STEP_TOLERANCE:.0e}）")
        has_step = has_step or bool(jitter_steps)

    print()
    print("== D. 标的同一性：em 与 sina 的**不复权**收盘在重叠交易日上是否逐位相等 ==")
    em_raw_frame = _load(EM_RAW[0])
    if em_raw_frame is None:
        print("  未判读：em 不复权夹具还没有录到")
    else:
        for spec, note in ((SINA_RAW, "窄窗"), (SINA_RAW_WIDE, "宽窗")):
            sina_frame = _load(spec[0])
            if sina_frame is None:
                print(f"  sina({note}): 未判读，缺 {spec[0]}")
                continue
            disagree = _compare_raw_closes(f"sina({note})", em_raw_frame, sina_frame)
            if disagree:
                print("    -> 两家原始收盘对不上：至少一帧取错了标的（回放判据看不见这件事）")

    print()
    print("== E. 台阶的日期与高度是否等于分红事件算出来的台阶（跨端点，可定量证伪） ==")
    explained_any = False
    for vendor, vendor_steps in chain_steps.items():
        vendor_spec = specs[vendor]
        close_column = "收盘" if vendor_spec[1] == "日期" else "close"
        bad = _explain_steps(
            vendor, vendor_steps, _load(vendor_spec[0]), vendor_spec[1], close_column
        )
        explained_any = explained_any or bool(vendor_steps)
        if bad:
            print(f"    -> {vendor}: 上述不符项成立，则因子链与事件流两家有一家在说谎")
    if not explained_any:
        print("  未判读：还没有任何一条链出现台阶（窗口不含除权日，或夹具未录全）")

    print()
    print("== F. 同一通道两个窗口（sina 窄窗 vs 宽窗）：原始收盘与锚定是否自洽 ==")
    n_shared, n_exact, worst_diff, worst_day = _compare_window_closes(
        _load(SINA_RAW[0]), _load(SINA_RAW_WIDE[0]), SINA_RAW[1], "close"
    )
    if n_shared:
        print(
            f"  不复权收盘：重叠 {n_shared} 天，逐位相等 {n_exact}/{n_shared} 天，"
            f"最大绝对差 {worst_diff:g}（{worst_day}）"
        )
        if worst_diff:
            print("    -> 同一个上游端点、同一天给出两个不复权收盘价：搬运层的窗口参数在改数据")
    else:
        print("  未判读：窄窗或宽窗的不复权夹具没录到，或两窗没有重叠交易日")
    _anchor_ratio_across_windows(sina_narrow, sina_wide)

    if em_factors is None or (sina_narrow is None and sina_wide is None):
        print("QFQ_CHAIN_EXIT=1")
        return 1
    print(f"QFQ_CHAIN_EXIT=0  (至少一条链有台阶={has_step})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
