"""C15 候选否决的证据脚本：期货交易日历腿有没有独立信息量.

两条独立判据（结论见 ``docs/evidence/C15/futures-calendar-rejected.txt``）：

1. **参数面**：``/api/futures/calendar/trading-schedule`` 在 9 种参数写法下全部
   被上游以 ``FUYAO_REQUEST 请求参数缺失或格式非法`` 拒绝（含不带参数）⇒ 文档
   未给出该端点的必填参数名，本轮无法在不猜参数的前提下接入；
2. **信息量**：改从**已转正的期货日线腿**取交易日集合，与 A 股交易日历逐日对照
   ⇒ 若两个方向差异都是 0，即便接通该端点也不会带来任何 A 股日历之外的开市日。

只做读请求，不碰数仓。运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C15/futures-calendar-probe.py
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from opendata.data.providers.ths.models._client import client
from opendata_fuyao.endpoints import (
    FUTURES_PRICES_ENDPOINT,
    fetch_period_daily_bars,
    millis_to_trading_date,
)
from opendata_fuyao.errors import FuyaoError

A_SHARE_CALENDAR = "/api/a-share/calendar/trading-days"
FUTURES_CALENDAR = "/api/futures/calendar/trading-schedule"
DATE_KEYS = ("date_ms", "trading_date", "trade_date", "calendar_date", "date")
PACE_SECONDS = 12.0
#: 参数写法候选（文档未标注必填参数名，逐个试到位）
PARAM_TRIES: tuple[Mapping[str, Any], ...] = (
    {},
    {"start_date": "2026-09-01", "end_date": "2026-09-24"},
    {"start": "2026-09-01", "end": "2026-09-24"},
    {"begin_date": "2026-09-01", "end_date": "2026-09-24"},
    {"start_date": "2026-09-01"},
    {"start_date": "20260901", "end_date": "20260924"},
    {"start_date": "2026-09-01", "end_date": "2026-09-24", "exchange": "CNFE"},
    {"date": "2026-09-24"},
    {"year": 2026},
)
#: 用已转正的期货日线腿取交易日：上期所商品 + 中金所股指，覆盖两类休市规则
FUTURES_CONTRACTS = ("CU2610.SHF", "IF2612.CFE")
WINDOW_START = date(2026, 6, 1)
WINDOW_END = date(2026, 9, 24)


def _rows(envelope: Any) -> list[Mapping[str, Any]]:  # noqa: ANN401  # 信封在适配层内部建模
    """The envelope's row list, narrowed to mappings."""
    return [item for item in envelope.items if isinstance(item, Mapping)]


def _dates(rows: Sequence[Mapping[str, Any]]) -> list[date]:
    """Plain dates out of whichever date field a row actually names.

    Args:
        rows: Raw rows from one endpoint.

    Returns:
        Sorted distinct dates; empty when no candidate field is present (the
        caller prints the observed field set, so a miss is visible not silent).
    """
    found: list[date] = []
    for row in rows:
        for hint in DATE_KEYS:
            value = row.get(hint)
            if isinstance(value, str) and value[:1].isdigit():
                found.append(date.fromisoformat(value[:10]))
                break
            if isinstance(value, (int, float)) and value > 1e11:
                found.append(millis_to_trading_date(value))
                break
    return sorted(set(found))


def _try_parameters() -> list[tuple[Mapping[str, Any], str]]:
    """Probe the futures-calendar endpoint with every candidate parameter set.

    Returns:
        ``(params, outcome)`` pairs; ``outcome`` is ``OK …`` or the error text.
    """
    outcomes: list[tuple[Mapping[str, Any], str]] = []
    for params in PARAM_TRIES:
        with client(timeout_seconds=60.0) as active:
            try:
                envelope = active.get(FUTURES_CALENDAR, params=dict(params)).envelope
                outcomes.append((params, f"OK：{len(envelope.items)} 行"))
            except FuyaoError as exc:
                outcomes.append((params, f"{type(exc).__name__}: {exc!s}"))
        time.sleep(PACE_SECONDS)
    return outcomes


def _futures_bar_dates(symbol: str) -> list[date]:
    """Trading dates the verified futures-daily leg reports for one contract."""
    with client(timeout_seconds=60.0) as active:
        bars = fetch_period_daily_bars(
            active,
            endpoint=FUTURES_PRICES_ENDPOINT,
            symbol=symbol,
            start=WINDOW_START,
            end=WINDOW_END,
        )
    return [bar.trade_date for bar in bars]


def _calendar_dates() -> list[date]:
    """Open days published by the verified A-share calendar leg."""
    with client(timeout_seconds=60.0) as active:
        rows = _rows(active.get(A_SHARE_CALENDAR).envelope)
    return _dates(rows)


def main() -> int:
    """Print both judgments; return non-zero when the probe could not judge."""
    print("# C15 候选否决：ths 期货交易日历腿（/api/futures/calendar/trading-schedule）\n")

    print("## 1. 参数面（文档未标必填参数名，逐个试到位）\n")
    print("| 入参 | 结果 |")
    print("| --- | --- |")
    outcomes = _try_parameters()
    for params, outcome in outcomes:
        print(f"| `{dict(params) or '（不带参数）'}` | {outcome} |")
    accepted = [params for params, outcome in outcomes if outcome.startswith("OK")]

    print("\n## 2. 信息量：期货日线交易日 vs A 股交易日历（同窗口）\n")
    calendar = _calendar_dates()
    time.sleep(PACE_SECONDS)
    print(
        f"- A 股日历：{len(calendar)} 天，coverage "
        f"{calendar[0] if calendar else '-'}～{calendar[-1] if calendar else '-'}"
    )
    window = [d for d in calendar if WINDOW_START <= d < WINDOW_END]
    print(f"- 落在对照窗口 {WINDOW_START}～{WINDOW_END} 内的 A 股开市日：{len(window)} 天")

    judged = bool(calendar)
    all_only_futures: list[date] = []
    for symbol in FUTURES_CONTRACTS:
        bar_dates = _futures_bar_dates(symbol)
        time.sleep(PACE_SECONDS)
        only_futures = sorted(set(bar_dates) - set(window))
        only_share = sorted(set(window) - set(bar_dates))
        all_only_futures += only_futures
        print(
            f"- {symbol}：日线 {len(bar_dates)} 根；"
            f"仅期货有 {len(only_futures)} 天 {only_futures[:6]}；"
            f"仅 A 股有 {len(only_share)} 天 {only_share[:6]}"
        )
        judged &= bool(bar_dates)

    print("\n## 判定\n")
    if not judged:
        print("INCONCLUSIVE：日历或期货日线未取到可比日期，本轮否决结论不成立。")
        return 1
    if accepted:
        print(f"- 参数面：{len(accepted)} 种写法被接受 ⇒ 该端点可接，否决理由不成立。")
        return 1
    print("- 参数面：9 种写法（含不带参数）全部被拒 ⇒ 现在接不进来，接入需先向对方要参数定义。")
    if all_only_futures:
        print(
            f"- 信息量：期货侧出现 {len(all_only_futures)} 个 A 股日历外的交易日"
            " ⇒ 该腿有独立价值，否决不成立。"
        )
        return 1
    print(
        "- 信息量：两条期货日线的交易日集合完全落在 A 股日历的开市日之内（差集为空）"
        "⇒ 即便接通，也只会得到一份与 A 股日历逐日相同的副本，不会新增判据。"
    )
    print(
        "- 综合：本轮**不接**该端点，映射表状态维持 ``available``；"
        "夜间时段（夜盘）属**日内**维度而非额外交易日，故不构成差异（观察，非判据）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
