"""ths fund distributions against sina and against the ETF price identity (AC-10 / C15).

``fund_action`` exists because of a measurement, not a wish: the on-exchange ETF
daily endpoint (``/api/fund/market/historical``) publishes **one** series - the
envelope even echoes ``data.adjust`` back while the request has no such
parameter - and that series is forward-adjusted. D10 forbids storing an adjusted
series, which is why ``fund_etf_daily`` has stayed on the akshare chain since C6.
The conversion key is the distribution stream, so this leg publishes events and
judges them five ways:

* **A - contract shape (self-proof).** Per-unit positive cash, stock/rights
  fields left at zero, strictly descending unique ex-dates, qualified symbol -
  plus the two negatives the contract demands: an all-zero cash row must raise,
  and the all-null "never distributed" row must **not** become an event.
* **B - ex-date set, both directions (新浪, independent vendor).** sina's
  cumulative-dividend stream is a different pipeline over a different factor, so
  its step dates are an independent record of the same 14/18 distributions.
  Missing in either direction is a breach; a fund with no distributions must
  show no steps on either side.
* **C - per-event amount.** sina publishes the running total, so the amount of
  one distribution is the difference between two of its rows. Compared digit for
  digit with ``per_ten_cash_before_tax / 10``.
* **D - the upstream's own totals.** ``dividend_count`` and ``dividend_total``
  are fields the endpoint sends alongside the rows; the normalizer already
  refuses a response where they disagree with the rows, and this run shows what
  it is checking: the progress codes, and whether the pre-tax and post-tax
  amounts differ (they do not for these ETFs - measured, not assumed).
* **E - the price identity this leg is for.** For a bar on day ``d``, sina's
  unadjusted close minus fuyao's forward-adjusted close must equal the sum of
  the distributions whose ex-date is later than ``d``. That is the whole
  D10-conversion argument; if it holds, the event stream is sufficient to get
  the unadjusted series back from the published one.

What is deliberately **not** judged: whether upstream's pre-tax amounts are the
ones a holder actually receives (tax depends on the holder), and the ETF daily
leg itself - this round publishes the conversion key, it does not run the
conversion. The warehouse is never touched: read-only, no DDL, no writes.

Usage (py313 env; needs network - legs B/C/E call sina):

    python scripts/ops/ths_fund_action_cross_check.py
"""

from __future__ import annotations

import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opendata.data.models import CorporateAction

T = TypeVar("T")
#: 秒。上游对连发直接断连（实测），两次请求之间要留开。
PACE_SECONDS = 12.0
#: 断连后的重试间隔与次数（证据脚本要跑完，不要一断就废）。
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 5
#: 参照侧（新浪）逐只限速。
REFERENCE_PACE_SECONDS = 2.0

#: 采样基金：两只多年分红的宽基 ETF + 两只从未分红的（后者专门用来验「回空不是报错」）。
FUNDS: tuple[tuple[str, str], ...] = (
    ("510300.SH", "sh510300"),
    ("510050.SH", "sh510050"),
    ("159915.SZ", "sz159915"),
    ("512880.SH", "sh512880"),
)
#: 裸码解析演练用的一只（``resolve_fund_code`` 走 ETF 目录，和股票检索不是一条路）。
BARE_ROUTE_SYMBOL = "510300"
#: 价格恒等式只在有分红的基金上判（无分红基金的恒等式退化成了 0=0，没有信息量）。
PRICE_IDENTITY_FUNDS: tuple[str, ...] = ("510300.SH", "510050.SH")

#: 每份金额对照容差（元/份）：新浪的累计分红只给到三位小数。
CASH_TOLERANCE = 1e-4
#: 价格差判等容差（元）：ETF 的最小变动价位是 0.001，两边各自四舍五入一次。
PRICE_TOLERANCE = 0.005
#: 判 E 时围绕最近一个除息日取的窗口宽度。
PRICE_WINDOW_DAYS = 21


class RawDividends(NamedTuple):
    """One dividends response: rows plus the endpoint's own totals."""

    rows: list[Mapping[str, Any]]
    declared_count: object
    declared_total: object
    adjust: object


class FundFacts(NamedTuple):
    """Everything judged for one fund."""

    qualified: str
    sina_symbol: str
    events: tuple[CorporateAction, ...]
    raw: RawDividends
    sina_cumulative: list[tuple[date, float]]
    transport_events: tuple[CorporateAction, ...]


def _retry(label: str, call: Callable[[], T]) -> T:
    """Run a network call with retries; give up loudly rather than silently.

    Args:
        label: What is being fetched (appears in the retry note).
        call: Zero-argument callable returning the fetched value.

    Returns:
        Whatever ``call()`` returns.

    Raises:
        Exception: The last error, once the attempts are used up.
    """
    last: Exception | None = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return call()
        except Exception as exc:  # noqa: PERF203  # 上游断连是瞬时的：重试而不是放弃
            last = exc
            print(f"!! {label} attempt {attempt + 1}: {type(exc).__name__}", file=sys.stderr)
            time.sleep(RETRY_SECONDS)
    raise last if last is not None else RuntimeError(f"{label}: no attempt ran")


def _raw_dividends(qualified: str) -> RawDividends:
    """Read the dividends endpoint through the transport layer (unnormalized).

    Args:
        qualified: ``510300.SH`` style code.

    Returns:
        The raw rows and the endpoint's own ``dividend_count`` / ``dividend_total``.
    """
    from opendata.data.providers.ths.models._client import client
    from opendata_fuyao.endpoints import FUND_DIVIDENDS_ENDPOINT

    def call() -> RawDividends:
        with client(timeout_seconds=60.0) as active:
            response = active.get(FUND_DIVIDENDS_ENDPOINT, params={"thscode": qualified})
            envelope = response.envelope
            return RawDividends(
                rows=[item for item in envelope.items if isinstance(item, Mapping)],
                declared_count=envelope.data.get("dividend_count"),
                declared_total=envelope.data.get("dividend_total"),
                adjust=envelope.data.get("adjust"),
            )

    return _retry(f"raw dividends {qualified}", call)


def _events_via_registry(symbol: str) -> tuple[CorporateAction, ...]:
    """Fetch one fund's distributions the way a caller does (routing + adapter).

    Args:
        symbol: Bare (``510300``) or qualified (``510300.SH``) code.

    Returns:
        Contract events, newest ex-date first.
    """
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return tuple(get_registry().resolve_domain("fund_action", source="ths").fetch(symbol=symbol))


def _events_via_transport(qualified: str) -> tuple[CorporateAction, ...]:
    """Normalize the same response at the transport layer (the adapter's input).

    Args:
        qualified: ``510300.SH`` style code.

    Returns:
        Events as ``normalize_fund_dividends`` publishes them.
    """
    from opendata.data.providers.ths.models._client import client
    from opendata_fuyao.endpoints import fetch_fund_dividends

    def call() -> tuple[CorporateAction, ...]:
        with client(timeout_seconds=60.0) as active:
            return fetch_fund_dividends(active, symbol=qualified)

    return _retry(f"transport dividends {qualified}", call)


def _sina_cumulative(sina_symbol: str) -> list[tuple[date, float]]:
    """Sina's cumulative-dividend stream for one on-exchange fund.

    Args:
        sina_symbol: ``sh510300`` style symbol.

    Returns:
        ``(date, cumulative amount)`` pairs in ascending order; empty when the
        vendor has no record for the fund.
    """
    from opendata_http.fund.fund_etf_sina import fund_etf_dividend_sina

    def call() -> list[tuple[date, float]]:
        frame = fund_etf_dividend_sina(symbol=sina_symbol)
        if frame.empty:
            return []
        return [(row["日期"], float(row["累计分红"])) for _, row in frame.iterrows()]

    return _retry(f"sina cumulative {sina_symbol}", call)


def _sina_close(sina_symbol: str, start: date, end: date) -> dict[date, float]:
    """Sina's unadjusted daily closes for one fund inside a window.

    Args:
        sina_symbol: ``sh510300`` style symbol.
        start: First day to keep (inclusive).
        end: Last day to keep (inclusive).

    Returns:
        ``{trade_date: close}``.
    """
    from opendata_http.fund.fund_etf_sina import fund_etf_hist_sina

    def call() -> dict[date, float]:
        frame = fund_etf_hist_sina(symbol=sina_symbol)
        return {
            row["date"]: float(row["close"])
            for _, row in frame.iterrows()
            if start <= row["date"] <= end
        }

    return _retry(f"sina bars {sina_symbol}", call)


def _fuyao_qfq_close(qualified: str, start: date, end: date) -> dict[date, float]:
    """Fuyao's published ETF closes for one fund inside a window.

    The request has no ``adjust`` parameter at all - that is the defect this leg
    works around - so whatever comes back is the forward-adjusted series.

    Args:
        qualified: ``510300.SH`` style code.
        start: First day to request (inclusive).
        end: Last day to request (inclusive).

    Returns:
        ``{trade_date: close}``.
    """
    from opendata.data.providers.ths.models._client import client
    from opendata_fuyao.endpoints import millis_to_trading_date, shanghai_midnight_millis

    def call() -> dict[date, float]:
        with client(timeout_seconds=60.0) as active:
            response = active.get(
                "/api/fund/market/historical",
                params={
                    "thscode": qualified,
                    "start": shanghai_midnight_millis(start),
                    "end": shanghai_midnight_millis(end),
                },
            )
            return {
                millis_to_trading_date(item["date_ms"]): float(item["close_price"])
                for item in response.envelope.items
                if isinstance(item, Mapping)
            }

    return _retry(f"fuyao bars {qualified}", call)


def _published_cash(events: Sequence[CorporateAction]) -> dict[date, float]:
    """Index the published stream by ex-date."""
    return {event.ex_date: event.cash_dividend for event in events}


def _sina_steps(cumulative: Sequence[tuple[date, float]]) -> dict[date, float]:
    """Turn sina's running total into per-distribution amounts."""
    steps: dict[date, float] = {}
    previous = 0.0
    for day, total in cumulative:
        steps[day] = round(total - previous, 6)
        previous = total
    return {day: amount for day, amount in steps.items() if amount > 0.0}


def _subsequent_total(events: Sequence[CorporateAction], on_or_before: date) -> float:
    """The sum of the distributions whose ex-date is later than ``on_or_before``."""
    return round(sum(event.cash_dividend for event in events if event.ex_date > on_or_before), 6)


def _collect_facts() -> list[FundFacts]:
    """Fetch every leg once, in reading order, with pacing between calls."""
    facts: list[FundFacts] = []
    first = True
    for qualified, sina_symbol in FUNDS:
        if not first:
            time.sleep(PACE_SECONDS)
        first = False
        query = BARE_ROUTE_SYMBOL if qualified.startswith(BARE_ROUTE_SYMBOL) else qualified
        print(f"## 取数 {qualified}（注册表路由入参：{query}）\n")
        raw = _raw_dividends(qualified)
        time.sleep(PACE_SECONDS)
        events = _events_via_registry(query)
        time.sleep(PACE_SECONDS)
        transport_events = _events_via_transport(qualified)
        time.sleep(REFERENCE_PACE_SECONDS)
        sina_cumulative = _sina_cumulative(sina_symbol)
        facts.append(
            FundFacts(
                qualified=qualified,
                sina_symbol=sina_symbol,
                events=events,
                raw=raw,
                sina_cumulative=sina_cumulative,
                transport_events=transport_events,
            )
        )
    return facts


def judge_shape(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion A - the published stream satisfies the contract, both ways round."""
    from opendata_fuyao import FuyaoError, parse_envelope
    from opendata_fuyao.endpoints import normalize_fund_dividends

    print("## A. 契约形状自证（含两条必须挂的反例）\n")
    print("| 基金 | 事件数 | 每份现金区间 | 降序唯一 | 符号与非现金字段 | 搬运层一致 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        cashes = [event.cash_dividend for event in fact.events]
        dates = [event.ex_date for event in fact.events]
        descending = dates == sorted(set(dates), reverse=True)
        shape = all(
            event.symbol == fact.qualified
            and event.stock_dividend == 0.0
            and event.rights_shares == 0.0
            and event.rights_price == 0.0
            for event in fact.events
        )
        agrees = [(event.ex_date, event.cash_dividend) for event in fact.transport_events] == list(
            zip(dates, cashes, strict=True)
        )
        ok = all(cash > 0.0 for cash in cashes) and descending and shape and agrees
        span = "-" if not cashes else f"{min(cashes):.3f}～{max(cashes):.3f}"
        print(
            f"| {fact.qualified} | {len(fact.events)} | {span} | {descending} | {shape} "
            f"| {agrees} | {'合格' if ok else '不合格'} |"
        )
        if not ok:
            failures.append(f"A/{fact.qualified}: 契约形状不成立")

    # 反例一：全零现金行必须挂（契约明文：all-zero row 无效，须在 normalize 里过滤掉）
    zero_row = {
        "per_ten_cash_before_tax": 0.0,
        "per_ten_cash_after_tax": 0.0,
        "progress": "2",
        "ex_dividend_date_ms": 1_768_752_000_000,
    }
    # 反例二：全 null 占位行不得成为事件（实测 159915.SZ / 512880.SH 发的就是它）
    placeholder = dict.fromkeys(zero_row)
    payload = {
        "code": 0,
        "message": "success",
        "request_id": "req-a",
        "data": {"timestamp": None, "item": [zero_row, placeholder]},
    }
    try:
        normalize_fund_dividends(parse_envelope(payload), symbol="510300.SH")
        breaches = "全零行未报错"
    except FuyaoError as exc:
        breaches = "" if "fund_dividend_cash" in str(exc) else f"报错码不符：{exc}"
    only_placeholder = {
        "code": 0,
        "message": "success",
        "request_id": "req-b",
        "data": {"timestamp": None, "dividend_count": 0, "item": [placeholder]},
    }
    empty_ok = normalize_fund_dividends(parse_envelope(only_placeholder), symbol="159915.SZ") == ()
    zero_verdict = breaches or "已拦（fund_dividend_cash）"
    placeholder_verdict = "归一化为空事件流" if empty_ok else "未过滤，仍在发事件"
    print(f"\n- 反例：全零现金行 → {zero_verdict}；全 null 占位行 → {placeholder_verdict}")
    if breaches or not empty_ok:
        failures.append(f"A/反例: {breaches or '占位行未被过滤'}")


def judge_dates(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion B - the ex-date sets agree with sina in both directions."""
    print("\n## B. 除息日集合双向对照（新浪累计分红的阶跃日）\n")
    print("| 基金 | 上游笔数 | 新浪笔数 | 仅上游有 | 仅新浪有 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        ours = set(_published_cash(fact.events))
        theirs = set(_sina_steps(fact.sina_cumulative))
        only_ours = sorted(ours - theirs)
        only_theirs = sorted(theirs - ours)
        ok = not only_ours and not only_theirs
        print(
            f"| {fact.qualified} | {len(ours)} | {len(theirs)} | {len(only_ours)} "
            f"| {len(only_theirs)} | {'一致' if ok else '不符'} |"
        )
        for day in only_ours:
            print(f"  - 仅上游有：{day}")
        for day in only_theirs:
            print(f"  - 仅新浪有：{day}")
        if not ok:
            failures.append(f"B/{fact.qualified}: 除息日集合双向不等")
    print(
        "\n- 反向也判：新浪有阶跃而上游无事件，同样算不符——无分红基金"
        f"（{', '.join(f.qualified for f in facts if not f.events)}）两侧笔数都必须为 0。"
    )


def judge_amounts(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion C - per-distribution amounts, digit for digit."""
    print("\n## C. 逐笔每份现金对照（新浪 Δ累计分红）\n")
    print("| 基金 | 除息日 | 上游每份 | 新浪每份 | 差额 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- |")
    total_compared = 0
    for fact in facts:
        ours = _published_cash(fact.events)
        theirs = _sina_steps(fact.sina_cumulative)
        common = sorted(set(ours) & set(theirs))
        for day in common:
            diff = ours[day] - theirs[day]
            ok = abs(diff) <= CASH_TOLERANCE
            total_compared += 1
            print(
                f"| {fact.qualified} | {day} | {ours[day]:.6f} | {theirs[day]:.6f} "
                f"| {diff:+.6f} | {'相符' if ok else '不符'} |"
            )
            if not ok:
                failures.append(f"C/{fact.qualified}/{day}: {ours[day]} != {theirs[day]}")
    print(
        f"\n- 可比笔数 {total_compared}（容差 {CASH_TOLERANCE} 元/份："
        "新浪只发布三位小数的累计值）。"
    )


def judge_self_checks(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion D - what the endpoint's own totals and codes actually say."""
    from opendata_fuyao.endpoints import IMPLEMENTED_PROGRESS

    print("\n## D. 上游自带自校验（``dividend_count`` / ``dividend_total`` / ``progress``）\n")
    print(
        "| 基金 | 行数 | 占位 | 声明 | 归一 | 声明合计 | 逐笔合计 | progress | 税前=税后 | 判定 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        rows = fact.raw.rows
        placeholders = sum(1 for row in rows if all(value is None for value in row.values()))
        ours = [event.cash_dividend for event in fact.events]
        summed = round(sum(ours), 6)
        declared_total = fact.raw.declared_total
        total_ok = (
            declared_total is None
            or not isinstance(declared_total, (int, float))
            or abs(float(declared_total) - summed) <= 1e-6
        )
        count_ok = fact.raw.declared_count is None or fact.raw.declared_count == len(ours)
        codes = sorted(
            {str(row.get("progress")) for row in rows if not all(v is None for v in row.values())}
        )
        unknown = [code for code in codes if code not in IMPLEMENTED_PROGRESS]
        same_tax = all(
            row.get("per_ten_cash_before_tax") == row.get("per_ten_cash_after_tax")
            for row in rows
            if row.get("per_ten_cash_before_tax") is not None
        )
        ok = count_ok and total_ok and (not codes or not unknown)
        print(
            f"| {fact.qualified} | {len(rows)} | {placeholders} | {fact.raw.declared_count} "
            f"| {len(ours)} | {declared_total} | {summed} | {','.join(codes) or '-'} "
            f"| {same_tax} | {'相符' if ok else '不符'} |"
        )
        if not count_ok:
            failures.append(f"D/{fact.qualified}: dividend_count 与逐笔不符")
        if not total_ok:
            failures.append(f"D/{fact.qualified}: dividend_total 与逐笔不符")
        if unknown:
            failures.append(f"D/{fact.qualified}: 未知 progress 码 {unknown}")
    print(
        "\n- 口径：契约落**税前**每份现金（``per_ten_cash_before_tax / 10``）。上表最后两列是实测，"
        "不是假设——这些 ETF 的税前与税后逐笔相等，故税后值不另设字段、也不混用。"
    )
    print(
        "- 归一化对不上即失败关闭：``dividend_count`` 与逐笔不等、"
        "或合计超出 1e-6 容差，都不发事件流。"
    )


def judge_price_identity(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion E - unadjusted minus forward-adjusted equals the later distributions."""
    print("\n## E. 价格恒等式：新浪不复权收盘 − 上游前复权收盘 = 该日之后分红之和\n")
    print("| 基金 | 交易日 | 不复权 | 前复权 | 实测差 | 应为（Σ其后分红） | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    by_symbol = {fact.qualified: fact for fact in facts}
    for qualified in PRICE_IDENTITY_FUNDS:
        fact = by_symbol[qualified]
        if not fact.events:
            failures.append(f"E/{qualified}: 无分红事件，恒等式无法判")
            continue
        last_ex = max(event.ex_date for event in fact.events)
        start, end = (
            last_ex - timedelta(days=PRICE_WINDOW_DAYS),
            last_ex + timedelta(days=PRICE_WINDOW_DAYS),
        )
        raw_bars = _sina_close(fact.sina_symbol, start, end)
        time.sleep(REFERENCE_PACE_SECONDS)
        qfq_bars = _fuyao_qfq_close(qualified, start, end)
        days = sorted(set(raw_bars) & set(qfq_bars))
        if not days:
            failures.append(f"E/{qualified}: 两侧在窗口 {start}～{end} 内无共同交易日")
            continue
        # 除息日前后各取一根，外加窗口末日：三点足以显出「分红落地即差值变小」
        sample = [days[0], days[-1]]
        around = [day for day in days if day >= last_ex]
        if around and around[0] not in sample:
            sample.append(around[0])
        for day in sorted(set(sample)):
            gap = round(raw_bars[day] - qfq_bars[day], 6)
            expected = _subsequent_total(fact.events, day)
            ok = abs(gap - expected) <= PRICE_TOLERANCE
            print(
                f"| {qualified} | {day} | {raw_bars[day]:.3f} | {qfq_bars[day]:.3f} "
                f"| {gap:.3f} | {expected:.3f} | {'相符' if ok else '不符'} |"
            )
            if not ok:
                failures.append(f"E/{qualified}/{day}: 差 {gap} != Σ其后分红 {expected}")
        print(
            f"| {qualified} | 窗口 {start}～{end} | 共同交易日 {len(days)} 根 "
            f"| - | - | - | 采样 {len(set(sample))} 根 |"
        )
    print(f"\n- 判据容差 {PRICE_TOLERANCE} 元：ETF 最小变动价位 0.001，两边收盘价各自进过一次。")
    print(
        "- 意义：恒等式成立即说明**分红事件流足以把发布的前复权序列换回不复权序列**，"
        "这正是 D10 入库前必须做的那一步；本腿发的是换算键，不是价格。"
    )


def _route_label(qualified: str) -> str:
    """How this fund's query reached the adapter: bare-code lookup or direct."""
    if qualified.startswith(BARE_ROUTE_SYMBOL):
        return f"{qualified}（裸码经 ETF 目录解析）"
    return f"{qualified}（合格码直传）"


def main() -> int:
    """Run every leg, print the evidence report, and judge the five criteria.

    Returns:
        ``0`` when all judged criteria hold, ``1`` otherwise.
    """
    print("# C15 ths 基金分红（fund_action）跨 vendor 对照（全程只读：不建表、不写仓）\n")

    facts = _collect_facts()
    print("\n- 路由入参：" + "、".join(_route_label(fact.qualified) for fact in facts))

    failures: list[str] = []
    judge_shape(facts, failures)
    judge_dates(facts, failures)
    judge_amounts(facts, failures)
    judge_self_checks(facts, failures)
    judge_price_identity(facts, failures)

    print("\n## 判定")
    if failures:
        print(f"FAIL（{len(failures)} 项）")
        for line in failures:
            print(f"- {line}")
        return 1
    print("PASS：A/B/C/D/E 五条判据全部成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
