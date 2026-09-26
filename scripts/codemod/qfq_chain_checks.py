"""Falsifiable checks for the D10 qfq factor chain (AC-11 / AC-6).

The check this module replaces derived the factor as ``qfq_close / raw_close`` and
then asserted that ``raw * factor`` reproduces ``qfq_close``. ``apply_adjust``
multiplies by exactly that ratio (``opendata/data/adjust.py:66``), so the assertion
could only fail if multiplication itself were broken — it lived behind a skip for
five rounds precisely because nothing read it as circular.

Both checks here take their expectation from outside the qfq series:

* :func:`check_factor_steps` — where the chain jumps and by how much must equal the
  dividend detail from a *different* upstream endpoint (``stock_action_dividend``),
  and a missing jump fails as hard as an unexplained one.
* :func:`check_synthesis` — one scalar per day must also fit open/high/low, and must
  leave volume/amount untouched. Whether a second field fits is not implied by how the
  factor was defined on the close.

:func:`check_window_consistency` is the premise the widened fixtures rest on: the same
channel recorded over two windows has to describe one instrument and one anchor.

Tolerances are measured, not guessed — see ``STEP_FLOOR`` / ``ADJUST_TOLERANCE`` and
``docs/evidence/C27/qfq-chain-noise-floor.txt``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date

    import pandas as pd

#: ``(label, raw case, official qfq case)`` for every recorded daily-line pair off the
#: same instrument. Both windows straddle 600519's 2024-06-19 ex-date on purpose: over
#: a window with no ex-date the chain is one constant and both checks degenerate into
#: "a constant equals itself" (measured in docs/evidence/C27/qfq-chain-noise-floor.txt).
ADJUST_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("em", "stock_daily_raw", "stock_daily_qfq"),
    ("sina(宽)", "stock_daily_sina_raw_wide", "stock_daily_sina_qfq_wide"),
)

#: The same channel's short window, used as the control in :func:`check_window_consistency`.
NARROW_PAIR = ("stock_daily_sina_raw", "stock_daily_sina_qfq")
WIDE_PAIR = ("stock_daily_sina_raw_wide", "stock_daily_sina_qfq_wide")

#: Every kline fixture in this harness is 600519; the symbol only has to agree between
#: the bars and the factors handed to ``apply_adjust``.
SYMBOL = "600519"

#: em and sina record the same five numbers under different names.
COLUMN_ALIASES = {
    "日期": "trade_date",
    "date": "trade_date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
}

#: Fields other than the close that the same scalar factor must also fit.
OFFICIAL_PRICE_FIELDS = ("open", "high", "low")

#: Wire rounding, not modelling slack. Upstream quotes are two decimals, so
#: ``qfq/raw`` carries ~1e-6 of noise: measured adjacent-day jitter on a stepless
#: 22-day window is 1.4e-06 median / 5.5e-06 max, while the real step across
#: 2024-06-19 is 2.071e-02 — three orders apart. 1e-4 sits in that empty band.
STEP_FLOOR = 1e-4

#: Same reasoning for the field-by-field comparison: the largest four-field
#: disagreement measured on 118 days was 6.251e-06 and the dividend-implied step
#: matched to 3.117e-06, while a wrong dividend amount or a missed event moves
#: the chain by ~1e-2.
ADJUST_TOLERANCE = 1e-4


def normalize(frame: pd.DataFrame) -> pd.DataFrame:
    """Rename a recorded daily-line frame to the English column set.

    Args:
        frame: Reference frame as recorded (em Chinese columns or sina English ones).

    Returns:
        Frame carrying exactly ``trade_date/open/high/low/close/volume/amount``.
    """
    columns = ["trade_date", "open", "high", "low", "close", "volume", "amount"]
    return frame.rename(columns=COLUMN_ALIASES)[columns]


def dates_of(frame: pd.DataFrame) -> list[date]:
    """The recording's trade dates, in window order."""
    import pandas as pd

    return list(pd.to_datetime(frame["trade_date"]).dt.date)


def floats_of(frame: pd.DataFrame, column: str) -> list[float]:
    """One column as floats, in window order."""
    return [float(value) for value in frame[column]]


def factor_chain(raw: pd.DataFrame, qfq: pd.DataFrame) -> list[float]:
    """``qfq_close / raw_close`` per day, in window order."""
    return [
        adjusted / base
        for adjusted, base in zip(floats_of(qfq, "close"), floats_of(raw, "close"), strict=True)
    ]


def steps(dates: list[date], factors: list[float], floor: float = STEP_FLOOR) -> dict[date, float]:
    """``ex_date -> relative move`` for every day the chain moved by at least ``floor``.

    Args:
        dates: Trade dates in window order.
        factors: The factor chain over the same days.
        floor: Smallest day-to-day relative move counted as an event (see ``STEP_FLOOR``).

    Returns:
        One entry per jumping day, keyed by that day, value = ``f(t)/f(t-1) - 1``.
    """
    return {
        dates[index]: factors[index] / factors[index - 1] - 1
        for index in range(1, len(factors))
        if abs(factors[index] / factors[index - 1] - 1) > floor
    }


def cash_dividends(frame: pd.DataFrame) -> dict[date, float]:
    """``ex_date -> cash per share`` for 实施 rows that are pure cash (no 送股/转增).

    Args:
        frame: The recorded ``stock_action_dividend`` reference frame.

    Returns:
        Map keyed by ex-date; rows this formula does not cover are left out.
    """
    import pandas as pd

    out: dict[date, float] = {}
    for row in frame.to_dict("records"):
        cash = float(row["派息"] or 0) / 10.0
        bonus = float(row["送股"] or 0) + float(row["转增"] or 0)
        if str(row["进度"]) == "实施" and bonus == 0.0 and cash:
            out[pd.to_datetime(str(row["除权除息日"])).date()] = cash
    return out


def check_factor_steps(
    raw: pd.DataFrame,
    qfq: pd.DataFrame,
    dividends: dict[date, float],
    tolerance: float = ADJUST_TOLERANCE,
) -> list[str]:
    """Do the chain's steps match the dividend record in date *and* height?

    Args:
        raw: Normalized unadjusted frame.
        qfq: Normalized official qfq frame for the same window.
        dividends: ``ex_date -> cash per share`` from :func:`cash_dividends`.
        tolerance: Relative slack on the step height (see ``ADJUST_TOLERANCE``).

    Returns:
        Failure lines; empty when the two endpoints agree.
    """
    dates = dates_of(raw)
    observed = steps(dates, factor_chain(raw, qfq))
    expected = {day: cash for day, cash in dividends.items() if day in dates}

    diffs: list[str] = []
    if not observed:
        diffs.append("no step inside the window: this fixture proves nothing about the chain")
    if set(observed) != set(expected):
        diffs.append(
            f"factor chain stepped on {sorted(observed)} but the dividend record has 实施 "
            f"cash ex-dates {sorted(expected)} inside the window"
        )

    closes = floats_of(raw, "close")
    for day, move in sorted(observed.items()):
        if day not in expected:
            continue
        index = dates.index(day)
        if index == 0:
            diffs.append(f"{day}: step on the first row, no previous close to check against")
            continue
        previous_close = closes[index - 1]
        # 前复权锚在序列末端，所以跨过纯现金除权日时 f(t) 抬高 P/(P-cash)。
        predicted = previous_close / (previous_close - expected[day])
        gap = abs((1.0 + move) / predicted - 1)
        if gap > tolerance:
            diffs.append(
                f"{day}: observed {1 + move:.10f} vs dividend-implied {predicted:.10f} "
                f"(previous close {previous_close}, cash {expected[day]}/share), "
                f"relative gap {gap:.3e}"
            )
    return diffs


def check_synthesis(
    raw: pd.DataFrame,
    qfq: pd.DataFrame,
    tolerance: float = ADJUST_TOLERANCE,
) -> list[str]:
    """Does one scalar factor per day also fit open/high/low and leave size alone?

    Args:
        raw: Normalized unadjusted frame.
        qfq: Normalized official qfq frame for the same window.
        tolerance: Relative slack per price field (see ``ADJUST_TOLERANCE``).

    Returns:
        Failure lines; empty when the synthesized series matches the official one.
    """
    from opendata.data.adjust import apply_adjust
    from opendata.data.models import AdjustFactor, Bar

    dates = dates_of(raw)
    if dates != dates_of(qfq):
        return ["raw and qfq windows disagree day by day"]
    factors = factor_chain(raw, qfq)
    if not steps(dates, factors):
        return ["no step inside the window: the three unchecked fields are still untested"]

    bars = [
        Bar(
            symbol=SYMBOL,
            trade_date=trade_date,
            open=open_value,
            high=high,
            low=low,
            close=close,
            volume=volume,
            amount=amount,
        )
        for trade_date, open_value, high, low, close, volume, amount in zip(
            dates,
            floats_of(raw, "open"),
            floats_of(raw, "high"),
            floats_of(raw, "low"),
            floats_of(raw, "close"),
            floats_of(raw, "volume"),
            floats_of(raw, "amount"),
            strict=True,
        )
    ]
    adjust_factors = [
        AdjustFactor(symbol=SYMBOL, trade_date=trade_date, qfq_factor=factor, hfq_factor=1.0)
        for trade_date, factor in zip(dates, factors, strict=True)
    ]

    synthesized = apply_adjust(bars, adjust_factors, "qfq")
    official = {column: floats_of(qfq, column) for column in OFFICIAL_PRICE_FIELDS}
    raw_volume, raw_amount = floats_of(raw, "volume"), floats_of(raw, "amount")

    diffs: list[str] = []
    for index, bar in enumerate(synthesized):
        for column, values in official.items():
            got, want = getattr(bar, column), values[index]
            if abs(got - want) > abs(want) * tolerance:
                diffs.append(f"{dates[index]} {column}: synthesized {got}, official qfq {want}")
        if bar.volume != raw_volume[index]:
            diffs.append(f"{dates[index]} volume adjusted: {bar.volume} != raw {raw_volume[index]}")
        if bar.amount != raw_amount[index]:
            diffs.append(f"{dates[index]} amount adjusted: {bar.amount} != raw {raw_amount[index]}")
    return diffs


def check_window_consistency(
    narrow_raw: pd.DataFrame,
    narrow_qfq: pd.DataFrame,
    wide_raw: pd.DataFrame,
    wide_qfq: pd.DataFrame,
    tolerance: float = ADJUST_TOLERANCE,
) -> tuple[str, list[str]]:
    """Do two recordings of the same channel over different windows tell one story?

    C27 widened a fixture window on purpose to straddle an ex-date. That only buys
    anything if the two frames describe the same instrument (raw closes agree on the
    overlap) and the qfq anchor does not move with the requested range (the ratio of
    the two chains is constant). Otherwise a chain computed over one window is not a
    restriction of the chain over another, and no window may be mixed with another.

    Args:
        narrow_raw: Normalized unadjusted frame over the short window.
        narrow_qfq: Normalized official qfq frame over the short window.
        wide_raw: Normalized unadjusted frame over the widened window.
        wide_qfq: Normalized official qfq frame over the widened window.
        tolerance: Relative drift allowed on the anchor ratio (see ``ADJUST_TOLERANCE``).

    Returns:
        ``(summary, diffs)`` — the summary always prints, the diffs are the failed claims.
    """
    import statistics

    narrow_close = dict(zip(dates_of(narrow_raw), floats_of(narrow_raw, "close"), strict=True))
    wide_close = dict(zip(dates_of(wide_raw), floats_of(wide_raw, "close"), strict=True))
    shared = sorted(set(narrow_close) & set(wide_close))
    if len(shared) < 3:
        return f"重叠 {len(shared)} 天（不足以判读）", ["two windows share no usable overlap"]

    mismatched_days = [day for day in shared if narrow_close[day] != wide_close[day]]
    diffs = [
        f"{day}: raw close {narrow_close[day]} != {wide_close[day]}" for day in mismatched_days
    ]

    narrow_factor = dict(
        zip(dates_of(narrow_raw), factor_chain(narrow_raw, narrow_qfq), strict=True)
    )
    wide_factor = dict(zip(dates_of(wide_raw), factor_chain(wide_raw, wide_qfq), strict=True))
    ratios = {day: wide_factor[day] / narrow_factor[day] for day in shared}
    anchor = statistics.median(ratios.values())
    drift = max(abs(value / anchor - 1) for value in ratios.values())
    if drift > tolerance:
        worst_day = max(ratios, key=lambda day: abs(ratios[day] / anchor - 1))
        diffs.append(
            f"anchor moves with the requested window: f_宽/f_窄 deviates by {drift:.3e} "
            f"(worst {worst_day}), so chains from different windows cannot be mixed"
        )
    return (
        f"重叠 {len(shared)} 天（{shared[0]}..{shared[-1]}），"
        f"不复权收盘不相等 {len(mismatched_days)}/{len(shared)} 天，"
        f"锚定比 f_宽/f_窄 中位数={anchor:.6f} 最大相对偏离 {drift:.3e}",
        diffs,
    )
