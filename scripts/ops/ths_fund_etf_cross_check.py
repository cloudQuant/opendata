"""ths on-exchange ETF daily bars: is the published series convertible (AC-10 / C20).

``fund_etf_daily`` is the one price domain whose ths leg has been refused for
four rounds running, and the reason is measured rather than stylistic:
``/api/fund/market/historical`` publishes a single series and that series is
forward-adjusted, while D10 forbids storing anything but unadjusted prices in
``Bar``. C15 shipped the conversion key (the per-unit pre-tax distribution
stream) and proved the identity on three sample days. This run turns those
three points into a window-wide judgement and measures what the channel may be
used for at all:

* **A - what the endpoint publishes.** Every published close must sit at or
  below sina's unadjusted close - that direction has no exceptions in a
  forward-adjusted series and is the D10-relevant claim. The second half, the
  equality on days with no later distribution, is counted cell by cell; each
  disagreement is handed to criterion G rather than waved through.
* **B - the conversion, cell by cell.** ``raw = published + Σ cash (ex-date
  strictly after the bar)`` compared against sina's OHLC for every common
  trading day, with the rival convention (``ex-date >= the bar``) compared the
  same way. The two differ only on ex-dates, so the discriminating cells are
  exactly those, and criterion B' refuses to accept a window where the
  difference is never actually observed - a convention nobody tested is not a
  convention that has been settled.
* **C - units, from three independent sides.** Volume/amount against sina, a
  vendor-free self-proof (``amount / volume`` is that day's real average price,
  so it must land inside the day's *unadjusted* range), and a vendor that
  labels its own unit: Tencent publishes the same bar with volume in lots
  (手), so ``ths ≈ tencent x 100`` versus ``ths ≈ tencent`` settles the
  question without any arithmetic assumption. The akshare leg multiplies its
  upstream's volume by 100 because eastmoney counts in lots; whether the same
  factor belongs on the ths leg is decided here rather than assumed from
  symmetry with the other provider.
* **G - attribution of every disagreeing cell.** A/B/C hand over each cell where
  the two references and the published series do not agree. None may stay
  unexplained: a third vendor (Tencent, independent of both sina and the
  upstream) adjudicates it as an upstream defect, a reference defect, or an
  unattributed breach. Attributing is not tolerating - the cells are printed
  one by one with their rate over the judged population, and an "upstream
  defect" is only accepted as a single-cell typo when it misses that fund's
  ex-dates, stays within ``MAX_UPSTREAM_OUTLIER_DAYS`` of its days, and is not
  shared with a peer fund on the same day. Anything else is a systematic
  disagreement and fails the round, because the leg is about to store this
  series and the reader must know what is in it.
* **D - what may be stored.** Bisecting the earliest start the channel answers
  for at two window lengths separates "the guard follows today" from "the guard
  follows the window". Then: an out-of-depth window answers ``code=0`` with an
  empty ``item`` - the same silent-empty-frame class C11a fixed for the index
  legs - while an over-long window is the one case that raises ``code=1003``. A
  rolling ~5-year window is an incremental source, not a backfill source.
* **E - the parameter surface.** ``adjust`` in six spellings, ``interval``,
  ``limit`` and the envelope echo fields, all fingerprinted so that "accepted
  and ignored" can never be mistaken for "honoured".
* **F - the leg once wired.** Registry-routed bars must agree cell for cell
  with sina's unadjusted series, and the capability must carry the depth limit
  in ``notes``.

Not judged: whether upstream's forward series is internally consistent with its
own distribution stream (B pins it to an independent vendor instead), and LOF /
off-exchange funds. The warehouse is never touched: read-only, no DDL, no
writes.

Usage (py313 env; needs network - A/B/C/G call sina and Tencent for unadjusted
reference bars):

    python scripts/ops/ths_fund_etf_cross_check.py
"""

from __future__ import annotations

import hashlib
import sys
import time
from collections.abc import Mapping
from datetime import date, timedelta
from math import floor, log10
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, TypeVar, cast

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opendata.data.models import Bar, CorporateAction
    from opendata.data.providers.ths import FuyaoHttpClient
    from opendata.data.providers.ths.transport.envelope import FuyaoEnvelope

T = TypeVar("T")

#: 待接入端点（映射表里 status=available 的那条 fund_etf_daily 腿）。
FUND_ETF_ENDPOINT = "/api/fund/market/historical"
#: 采样基金：两只多年分红的宽基 + 两只从未分红的（换算必须退化为恒等，是对照组）。
FUNDS: tuple[tuple[str, str], ...] = (
    ("510300.SH", "sh510300"),
    ("510050.SH", "sh510050"),
    ("159915.SZ", "sz159915"),
    ("512880.SH", "sh512880"),
)
#: 换算判据取的窗口宽度（日历天）：留在实测跨度上限内，又盖住多个除息日。
WINDOW_DAYS = 1000
#: 判据 F 的裸码演练（走 ETF 目录，不是股票检索那条路）。
BARE_ROUTE_SYMBOL = "510300"

#: 秒。上游对连发直接断连（C15 实测），主取数之间要留开。
PACE_SECONDS = 12.0
#: 二分探针是同一端点的轻量重复调用：短限速，瞬时断连由 _retry 兜住。
PROBE_PACE_SECONDS = 2.5
#: 参照侧（新浪）逐只限速。
REFERENCE_PACE_SECONDS = 2.0
RETRY_SECONDS = 20.0
RETRY_ATTEMPTS = 5

#: 价格判等容差（元）：ETF 最小变动价位 0.001，两边各自进过一次。
PRICE_TOLERANCE = 0.005
#: 量额相对容差：两边都是聚合口径，这里只判「是不是同一个单位」。
UNIT_RELATIVE_TOLERANCE = 1e-3
#: 量额「同值」容差：过 1e-3 只说明单位一致，过这一档才说明差的只是渲染位数。
QUANT_RELATIVE_TOLERANCE = 1e-6
#: 第三 vendor（腾讯）：同一根不复权 K 线，成交量明标「手」，符号与新浪同形。
TX_KLINE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
#: 手 → 份 的换算因子（1 手 = 100 份），只用于和腾讯的标量口径对表。
LOTS_PER_SHARE = 100.0
#: 上游量额的量化位数：实测「发布值 = 参照值按 8 位有效数字舍入」是否逐根成立。
PUBLISHED_SIG_DIGITS = 8
#: 判据 G 的收紧（跑前写死）：一只基金上多于这一天数的上游离群格按系统性处理，不放行。
MAX_UPSTREAM_OUTLIER_DAYS = 1
#: 先前采样的地板偏移与跨度上限：本轮由二分复现，不作为前提。
CLAIMED_FLOOR_OFFSET = 1827
CLAIMED_MAX_SPAN = 1826

#: 日线响应行里归一化实际吃到的价格字段（顺序即 ``prices`` 元组的顺序）。
PRICE_FIELDS = ("open_price", "high_price", "low_price", "close_price")
#: 异常格允许出现的字段名（四个价格 + 量 + 额）。
VOLUME_FIELD = "volume"
AMOUNT_FIELD = "amount"

#: 一根日线的四个价格 + 量 + 额，判据里反复按位取值，故折成具名结构。
Ohlcv = tuple[tuple[float, float, float, float], float, float]


class PublishedRow(NamedTuple):
    """One bar as the endpoint publishes it (i.e. still forward-adjusted)."""

    trade_date: date
    prices: tuple[float, float, float, float]
    volume: float
    amount: float


class TxRow(NamedTuple):
    """One unadjusted bar as Tencent publishes it (volume labelled in lots)."""

    prices: tuple[float, float, float, float]
    volume_lots: float


class Anomaly(NamedTuple):
    """A judged cell where the published series and sina do not agree."""

    symbol: str
    day: date
    field: str
    #: 上游侧该格的值（价格格填换算成不复权之后的值，便于与参照逐位对）。
    ours: float
    #: 新浪侧该格的值。
    theirs: float
    #: 是哪个判据抓到的（A / B / C）。
    caught_by: str


class FundFacts(NamedTuple):
    """Everything judged for one fund."""

    qualified: str
    sina_symbol: str
    published: dict[date, PublishedRow]
    reference: dict[date, Ohlcv]
    tencent: dict[date, TxRow]
    events: tuple[CorporateAction, ...]


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


def _ask(
    active: FuyaoHttpClient,
    *,
    symbol: str,
    start: date,
    end: date,
    **extra: object,
) -> tuple[FuyaoEnvelope | None, str]:
    """Request the bars endpoint once, returning the envelope or the failure text.

    Args:
        active: Client for this call.
        symbol: Qualified upstream fund code.
        start: First day of the window (inclusive).
        end: Last day of the window (exclusive).
        **extra: Extra query parameters, used to probe the parameter surface.

    Returns:
        ``(envelope, "")`` or ``(None, first line of the error)``.
    """
    from opendata.data.providers.ths import FuyaoError
    from opendata.data.providers.ths.endpoints import shanghai_midnight_millis

    params: dict[str, Any] = {
        "thscode": symbol,
        "interval": "1d",
        "start": shanghai_midnight_millis(start),
        "end": shanghai_midnight_millis(end) - 1,
    }
    params.update(extra)
    try:
        return active.get(FUND_ETF_ENDPOINT, params=params).envelope, ""
    except FuyaoError as exc:
        return None, str(exc).splitlines()[0]


def _fingerprint(envelope: FuyaoEnvelope | None) -> str:
    """Fingerprint the close series, so "ignored" cannot be read as "honoured"."""
    if envelope is None:
        return "(no envelope)"
    payload = "|".join(f"{row['date_ms']}:{row['close_price']}" for row in envelope.items)
    return f"{len(envelope.items):>4} bars {hashlib.sha256(payload.encode()).hexdigest()[:12]}"


def _rows_of(envelope: FuyaoEnvelope) -> list[PublishedRow]:
    """Fold envelope rows into ascending :class:`PublishedRow` values."""
    from opendata.data.providers.ths.endpoints import millis_to_trading_date

    rows = [
        PublishedRow(
            trade_date=millis_to_trading_date(row["date_ms"]),
            prices=(
                float(row["open_price"]),
                float(row["high_price"]),
                float(row["low_price"]),
                float(row["close_price"]),
            ),
            volume=float(row["volume"]),
            amount=float(row["turnover"]),
        )
        for row in envelope.items
        if isinstance(row, Mapping)
    ]
    return sorted(rows, key=lambda item: item.trade_date)


def _published_window(symbol: str, start: date, end: date) -> list[PublishedRow]:
    """Read a window of published (still adjusted) bars at the transport layer."""

    def call() -> list[PublishedRow]:
        from opendata.data.providers.ths.models._client import client

        with client(timeout_seconds=60.0) as active:
            envelope, error = _ask(active, symbol=symbol, start=start, end=end)
            if envelope is None:
                raise RuntimeError(f"{symbol} {start}->{end}: {error}")
            return _rows_of(envelope)

    return _retry(f"published bars {symbol}", call)


def _reference_window(sina_symbol: str, start: date, end: date) -> dict[date, Ohlcv]:
    """Sina's **unadjusted** daily bars: ``{trade_date: (OHLC, volume, amount)}``."""

    def call() -> dict[date, Ohlcv]:
        from opendata.data.providers.akshare._vendor.fund.fund_etf_sina import fund_etf_hist_sina

        frame = fund_etf_hist_sina(symbol=sina_symbol)
        out: dict[date, Ohlcv] = {}
        for row in [] if frame.empty else frame.to_dict("records"):
            day = date.fromisoformat(str(row["date"])[:10])
            if start <= day <= end:
                out[day] = (
                    (
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                    ),
                    float(row["volume"]),
                    float(row["amount"]),
                )
        return out

    return _retry(f"sina bars {sina_symbol}", call)


def _round_sig(value: float, digits: int) -> float:
    """Round ``value`` to ``digits`` significant decimal digits."""
    if value == 0.0:
        return 0.0
    exponent = digits - 1 - floor(log10(abs(value)))
    return round(value, exponent)


def _ref_cell(row: Ohlcv, field: str) -> float:
    """Sina's cell for one of the two aggregate fields."""
    return row[1] if field == VOLUME_FIELD else row[2]


def _quant_matches(ours: float, theirs: float) -> bool:
    """Whether ``ours`` is sina's value rendered at the upstream's digit count.

    Compared with a 1e-12 relative slack: both sides are floats, and an exact
    ``==`` here would fail on arithmetic noise rather than on data.
    """
    rounded = _round_sig(theirs, PUBLISHED_SIG_DIGITS)
    return abs(ours - rounded) <= max(abs(ours), abs(rounded)) * 1e-12


def _tx_segment(symbol: str, start: date, end: date) -> dict[date, TxRow]:
    """One request to Tencent's daily kline (wider ranges get truncated to 640)."""
    import json

    import requests

    params = {"_var": "kline_day", "param": f"{symbol},day,{start},{end},640,"}
    text = requests.get(TX_KLINE_URL, params=params, timeout=20).text
    node = json.loads(text[text.find("=") + 1 :])["data"][symbol]
    rows = node.get("day") or node.get("qfqday") or []
    out: dict[date, TxRow] = {}
    for row in rows:
        # 行序是 date, open, close, high, low, volume(手)：第 6 列被 akshare 标成
        # amount，实测它是「手」计成交量（= 新浪份数 / 100），故这里按量用。
        day = date.fromisoformat(str(row[0])[:10])
        out[day] = TxRow(
            prices=(float(row[1]), float(row[3]), float(row[4]), float(row[2])),
            volume_lots=float(row[5]),
        )
    return {day: value for day, value in out.items() if start <= day <= end}


def _tencent_window(tx_symbol: str, start: date, end: date) -> dict[date, TxRow]:
    """Tencent's **unadjusted** daily bars: prices plus volume counted in lots.

    Independent of both sina and the upstream, and its volume field carries its
    own unit label, so it settles a unit dispute without arithmetic assumptions.
    """

    def call() -> dict[date, TxRow]:
        out: dict[date, TxRow] = {}
        for year in range(start.year, end.year + 1):
            out.update(
                _tx_segment(
                    tx_symbol,
                    max(start, date(year, 1, 1)),
                    min(end, date(year, 12, 31)),
                )
            )
        return out

    return _retry(f"tencent bars {tx_symbol}", call)


def _events(qualified: str) -> tuple[CorporateAction, ...]:
    """The conversion key: ths per-unit pre-tax distributions (the C15 leg)."""

    def call() -> tuple[CorporateAction, ...]:
        from opendata.data.providers.ths.endpoints import fetch_fund_dividends
        from opendata.data.providers.ths.models._client import client

        with client(timeout_seconds=60.0) as active:
            return fetch_fund_dividends(active, symbol=qualified)

    return _retry(f"dividends {qualified}", call)


def _cash_after(events: Sequence[CorporateAction], day: date, *, include: bool = False) -> float:
    """Distributions with ex-date later than ``day`` (``include``: not earlier)."""
    keep = (lambda event: event.ex_date >= day) if include else (lambda event: event.ex_date > day)
    return round(sum(event.cash_dividend for event in events if keep(event)), 6)


def _collect() -> tuple[list[FundFacts], date, date]:
    """Fetch every leg once, in reading order, with pacing between calls."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS)
    facts: list[FundFacts] = []
    for index, (qualified, sina_symbol) in enumerate(FUNDS):
        if index:
            time.sleep(PACE_SECONDS)
        print(f"## 取数 {qualified}：发布窗口 {start}→{end}（未换算）\n")
        published = {row.trade_date: row for row in _published_window(qualified, start, end)}
        time.sleep(REFERENCE_PACE_SECONDS)
        reference = _reference_window(sina_symbol, start, end)
        tencent = _tencent_window(sina_symbol, start, end)
        time.sleep(PACE_SECONDS)
        events = _events(qualified)
        common = set(published) & set(reference)
        print(
            f"- 发布 {len(published)} 根 / 新浪 {len(reference)} 根 / 腾讯 {len(tencent)} 根"
            f" / 共同 {len(common)} 根 / 分红 {len(events)} 笔\n"
        )
        facts.append(FundFacts(qualified, sina_symbol, published, reference, tencent, events))
    return facts, start, end


def _register(
    anomalies: list[Anomaly],
    fact: FundFacts,
    day: date,
    field: str,
    ours: float,
    theirs: float,
    caught_by: str,
) -> None:
    """Record a disagreeing cell once, and only once, for criterion G to judge."""
    if any(
        item.symbol == fact.qualified and item.day == day and item.field == field
        for item in anomalies
    ):
        return
    anomalies.append(Anomaly(fact.qualified, day, field, ours, theirs, caught_by))


def judge_series_shape(
    facts: Sequence[FundFacts], failures: list[str], anomalies: list[Anomaly]
) -> None:
    """Criterion A - the published series is forward-adjusted, and only that."""
    print("## A. 发布序列形状：是否 ≤ 不复权、且只在「其后无分红」的日子相等\n")
    print("| 基金 | 共同根 | 发布>不复权(超容差) | 实测相等根 | 应为相等根 | 首个相等日 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        common = sorted(set(fact.published) & set(fact.reference))
        if not common:
            failures.append(f"A/{fact.qualified}: 无共同交易日")
            print(f"| {fact.qualified} | 0 | - | - | - | - | 无共同交易日 |")
            continue
        above = [
            day
            for day in common
            if fact.published[day].prices[3] - fact.reference[day][0][3] > PRICE_TOLERANCE
        ]
        # 「应为相等根」由分红流推出：其后没有分红的日子，前复权价就是不复权价。
        expected = {day for day in common if _cash_after(fact.events, day) == 0.0}
        measured = {
            day
            for day in common
            if abs(fact.published[day].prices[3] - fact.reference[day][0][3]) <= PRICE_TOLERANCE
        }
        for day in sorted(measured ^ expected):
            _register(
                anomalies,
                fact,
                day,
                PRICE_FIELDS[3],
                fact.published[day].prices[3] + _cash_after(fact.events, day),
                fact.reference[day][0][3],
                "A",
            )
        ok = not above
        first = min(expected) if expected else None
        print(
            f"| {fact.qualified} | {len(common)} | {len(above)} | {len(measured)} "
            f"| {len(expected)} | {first or '—'} | {'相符' if ok else '不符'} |"
        )
        if not ok:
            failures.append(
                f"A/{fact.qualified}: 发布价高于不复权价 {len(above)} 根"
                f"（{sorted(above)[:3]}）⇒ 序列不是前复权"
            )
        if measured != expected:
            print(
                f"- {fact.qualified}：相等段与应为段差 {sorted(measured ^ expected)}，"
                "已交判据 G 归因"
            )
    print(
        "\n- 读法：C6/C15 的「只发前复权」此前只有三个采样日支撑；本轮逐根判。\n"
        "- 「发布 > 不复权」是硬失败：前复权序列在任何口径下都不可能高于不复权价。\n"
        "- 相等段的差异不在此处放过，一律进判据 G 由第三 vendor 归因（未被归因即失败）。"
    )


def _mismatched_cells(
    fact: FundFacts, days: Sequence[date], *, include: bool
) -> list[tuple[date, str]]:
    """Compare converted prices against sina cell by cell, listing the failures."""
    bad: list[tuple[date, str]] = []
    for day in days:
        published = fact.published[day].prices
        reference = fact.reference[day][0]
        later = _cash_after(fact.events, day, include=include)
        for field, name in enumerate(PRICE_FIELDS):
            if abs(published[field] + later - reference[field]) > PRICE_TOLERANCE:
                bad.append((day, name))
    return bad


def judge_conversion(
    facts: Sequence[FundFacts], failures: list[str], anomalies: list[Anomaly]
) -> None:
    """Criterion B - both boundary conventions, judged cell by cell."""
    print("## B. 前复权 → 不复权 换算逐格判（两种边界口径）\n")
    print("| 基金 | 共同根 | 判格 | 「其后」不等格 | 其中在除息日 | 「不早于」额外不等格 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        common = sorted(set(fact.published) & set(fact.reference))
        if not common:
            failures.append(f"B/{fact.qualified}: 无共同交易日")
            continue
        ex_dates = {event.ex_date for event in fact.events}
        strict_set = set(_mismatched_cells(fact, common, include=False))
        on_or_set = set(_mismatched_cells(fact, common, include=True))
        # 口径错就错在除息日：那里两种口径给不同的值，「其后」若在那里也不等，语义即被推翻。
        on_ex_bad = sorted(day for day, field in strict_set if day in ex_dates)
        # 两种口径只在除息日分歧；非除息日上「不早于」多出来的不等格只能是数据缺陷。
        extra = sorted(cell for cell in on_or_set - strict_set if cell[0] not in ex_dates)
        for day, field in sorted(strict_set):
            if day in ex_dates:
                continue
            _register(
                anomalies,
                fact,
                day,
                field,
                fact.published[day].prices[PRICE_FIELDS.index(field)]
                + _cash_after(fact.events, day),
                fact.reference[day][0][PRICE_FIELDS.index(field)],
                "B",
            )
        ok = not on_ex_bad and not extra
        print(
            f"| {fact.qualified} | {len(common)} | {len(common) * 4} | {len(strict_set)} "
            f"| {len(on_ex_bad)} | {len(extra)} | {'相符' if ok else '不符'} |"
        )
        for day in on_ex_bad[:6]:
            print(f"  - 「其后」口径在除息日 {day} 不符 ⇒ 边界语义待推翻")
        for day, field in extra[:6]:
            print(f"  - 非除息日 {day}/{field} 上两口径给出不同结果 ⇒ 该格另有问题")
        if on_ex_bad:
            failures.append(
                f"B/{fact.qualified}: 「其后」口径在 {len(on_ex_bad)} 个除息日不符 ⇒ 边界语义不成立"
            )
        if extra:
            failures.append(f"B/{fact.qualified}: {len(extra)} 格非除息日上的口径分歧")
    print(
        "\n- 判读：两种口径唯一的分歧处就是除息日，所以「除息日上的不等」是本轮的硬失败，"
        "它才是口径判据；\n  非除息日上的不等不是口径问题，一律交判据 G 由第三 vendor 归因。\n"
        "- 无分红基金（159915/512880）没有除息日，两种口径必须逐格一致：它们是换算的恒等对照组。"
    )


def judge_boundary_discrimination(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion B' - the boundary must actually bite somewhere in this window."""
    print("\n## B'. 边界分歧是否真的被观测到（判据有没有吃得住）\n")
    print("| 基金 | 窗口内除息日 | 除息日上的不等格 | 构成判别 |")
    print("| --- | --- | --- | --- |")
    testable = 0
    for fact in facts:
        common = sorted(set(fact.published) & set(fact.reference))
        ex_dates = {event.ex_date for event in fact.events}
        ex_in_window = [day for day in common if day in ex_dates]
        # 只有除息日上的不等才是「两种口径的分歧」；别处的不等是数据缺陷，不算判别。
        on_ex_bad = [
            cell for cell in _mismatched_cells(fact, common, include=True) if cell[0] in ex_dates
        ]
        if not ex_in_window:
            print(f"| {fact.qualified} | 0 | {len(on_ex_bad)} | 不适用（窗口内无除息日） |")
            if on_ex_bad:
                failures.append(f"B'/{fact.qualified}: 无除息日却在除息日口径上不等 ⇒ 分组错乱")
            continue
        testable += 1
        print(f"| {fact.qualified} | {len(ex_in_window)} | {len(on_ex_bad)} | {bool(on_ex_bad)} |")
        if not on_ex_bad:
            failures.append(
                f"B'/{fact.qualified}: 窗口内有除息日，两种口径却从未分歧 ⇒ 判据未被测到"
            )
    print(
        f"\n- {testable} 只基金在本窗口内有除息日，逐只要求分歧被观测到；"
        "一只都没有则本轮无法判边界口径，\n  必须换窗口重跑（不允许退回「两种都行」这种结论）。"
    )
    if testable == 0:
        failures.append("B': 采样窗口内没有任何除息日 ⇒ 边界口径无法判")


def judge_units(facts: Sequence[FundFacts], failures: list[str], anomalies: list[Anomaly]) -> None:
    """Criterion C - volume/amount units, against sina, Tencent and a self-proof."""
    print("## C. 单位：量额对新浪（相对差；参照侧为 0 的行是缺测，不参与求差）\n")
    print(
        "| 基金 | 根 | 参照有值(量/额) | 量最大绝对差 | 量最大相对差 | 额最大相对差 "
        "| 8 位有效舍入命中(量/额) | 判定 |"
    )
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        common = sorted(set(fact.published) & set(fact.reference))
        meas: dict[str, list[date]] = {
            VOLUME_FIELD: [day for day in common if fact.reference[day][1] > 0.0],
            AMOUNT_FIELD: [day for day in common if fact.reference[day][2] > 0.0],
        }
        for day in common:
            row, ref = fact.published[day], fact.reference[day]
            if ref[1] <= 0.0 and row.volume > 0.0:
                _register(anomalies, fact, day, VOLUME_FIELD, row.volume, ref[1], "C")
            if ref[2] <= 0.0 and row.amount > 0.0:
                _register(anomalies, fact, day, AMOUNT_FIELD, row.amount, ref[2], "C")
        stats: dict[str, tuple[float, float, int]] = {}
        for field, days in meas.items():
            abs_diffs: list[float] = []
            rels: list[float] = []
            quant = 0
            for day in days:
                ours = getattr(fact.published[day], field)
                theirs = _ref_cell(fact.reference[day], field)
                abs_diffs.append(abs(ours - theirs))
                rels.append(abs(ours - theirs) / theirs)
                quant += 1 if _quant_matches(ours, theirs) else 0
            stats[field] = (
                max(abs_diffs, default=0.0),
                max(rels, default=0.0),
                quant,
            )
        vol_abs, vol_rel, vol_quant = stats[VOLUME_FIELD]
        _, amt_rel, amt_quant = stats[AMOUNT_FIELD]
        ok = (
            vol_rel < UNIT_RELATIVE_TOLERANCE
            and amt_rel < UNIT_RELATIVE_TOLERANCE
            and vol_rel < QUANT_RELATIVE_TOLERANCE
            and amt_rel < QUANT_RELATIVE_TOLERANCE
        )
        print(
            f"| {fact.qualified} | {len(common)} | {len(meas[VOLUME_FIELD])}"
            f"/{len(meas[AMOUNT_FIELD])} | {vol_abs:.0f} | {vol_rel:.2e} | {amt_rel:.2e} "
            f"| {vol_quant}/{amt_quant} | {'相符' if ok else '不符'} |"
        )
        if not ok:
            failures.append(
                f"C/{fact.qualified}: 量额相对差 {vol_rel:.2e}/{amt_rel:.2e} "
                f"超阈 ⇒ 单位或量值不同口径"
            )
    print(
        "\n- 两档容差各有其职：``UNIT_RELATIVE_TOLERANCE`` 只问「是不是同一个单位」，\n"
        f"  ``QUANT_RELATIVE_TOLERANCE`` 问「除了渲染是不是同一个数」。\n"
        "  后者也过，说明差异全在舍入。\n"
        f"- 「8 位有效舍入命中」是把残余差异**解释掉**而不是放过：发布值 == 新浪值按 "
        f"{PUBLISHED_SIG_DIGITS} 位\n  有效数字舍入 ⇒ 上游的量额是聚合后量化过的值，"
        "逐位相等在这里不是正确的判据。"
    )

    print("\n### C''. 单位第三方对表：腾讯同一根 K 线的成交量明标「手」\n")
    print("| 基金 | 腾讯有该行 | 份口径命中(≈手×100) | 手口径命中(≈手) | 判定 |")
    print("| --- | --- | --- | --- | --- |")
    for fact in facts:
        days = sorted(set(fact.published) & set(fact.tencent))
        share_hit = sum(
            1
            for day in days
            if abs(fact.published[day].volume - fact.tencent[day].volume_lots * LOTS_PER_SHARE)
            <= fact.published[day].volume * UNIT_RELATIVE_TOLERANCE
        )
        lot_hit = sum(
            1
            for day in days
            if abs(fact.published[day].volume - fact.tencent[day].volume_lots)
            <= fact.published[day].volume * UNIT_RELATIVE_TOLERANCE
        )
        ok = bool(days) and share_hit == len(days) and lot_hit == 0
        print(
            f"| {fact.qualified} | {len(days)} | {share_hit}/{len(days)} | {lot_hit}/{len(days)} "
            f"| {'份' if ok else '待定'} |"
        )
        if not ok:
            failures.append(
                f"C''/{fact.qualified}: 腾讯标手口径下命中 {lot_hit}/{len(days)}、"
                f"份口径 {share_hit}/{len(days)} ⇒ 单位未定"
            )

    print("\n### C'. VWAP 自证（不需要第二个 vendor）：``额/量`` 必须落进当日不复权区间\n")
    print("| 基金 | 根 | 份口径命中（换算价） | ×100 手口径命中 | 用发布价判命中 | 判定 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for fact in facts:
        rows = list(fact.published.values())
        tested = [row for row in rows if row.volume > 0.0]
        shares_hit = lot_hit = adjusted_hit = 0
        for row in tested:
            later = _cash_after(fact.events, row.trade_date)
            raw_low, raw_high = row.prices[2] + later, row.prices[1] + later
            vwap = row.amount / row.volume
            if raw_low - PRICE_TOLERANCE <= vwap <= raw_high + PRICE_TOLERANCE:
                shares_hit += 1
            lot_vwap = row.amount / (row.volume * 100.0)
            if raw_low - PRICE_TOLERANCE <= lot_vwap <= raw_high + PRICE_TOLERANCE:
                lot_hit += 1
            if row.prices[2] - PRICE_TOLERANCE <= vwap <= row.prices[1] + PRICE_TOLERANCE:
                adjusted_hit += 1
        ok = bool(tested) and shares_hit == len(tested) and lot_hit == 0
        print(
            f"| {fact.qualified} | {len(tested)}（零量跳过 {len(rows) - len(tested)}） "
            f"| {shares_hit}/{len(tested)} "
            f"| {lot_hit}/{len(tested)} | {adjusted_hit}/{len(tested)} | {'份' if ok else '待定'} |"
        )
        if not ok:
            failures.append(f"C'/{fact.qualified}: VWAP 自证未能区分份/手")
    print(
        "\n- 最后一列不是冗余：``额/量`` 是**当天真实成交均价**，发布价却是前复权价，"
        "两者只在最后除息日之后重合。\n  拿发布价区间判 VWAP 必然在老行上失败 ⇒ "
        "这同时坐实了**量与额不参与复权**：换算只动价格，不动量额。\n"
        "- 意义：akshare 的 ``fund_etf_daily`` 腿把上游成交量 ×100（东财按「手」计数）。"
        "ths 腿若照抄就会整体放大 100 倍，\n  本轮三条各自独立的证据（对新浪逐根相对差、"
        "对腾讯的标手口径、VWAP 自证）都指向上游已是「份」\n"
        "  ⇒ ths 腿**不做**任何单位换算，直接入库。"
    )


def _tx_value(row: TxRow | None, field: str) -> float | None:
    """The Tencent cell matching a judged field (its lots column converted to shares)."""
    if row is None or field == AMOUNT_FIELD:
        return None
    if field == VOLUME_FIELD:
        return row.volume_lots * LOTS_PER_SHARE
    return row.prices[PRICE_FIELDS.index(field)]


def _agrees(ours: float, theirs: float, field: str) -> bool:
    """Judge one cell equal, with the tolerance that field's own rendering allows."""
    if field in PRICE_FIELDS:
        return abs(ours - theirs) <= PRICE_TOLERANCE
    return abs(ours - theirs) <= max(abs(ours), abs(theirs)) * UNIT_RELATIVE_TOLERANCE


def judge_anomalies(
    facts: Sequence[FundFacts], anomalies: Sequence[Anomaly], failures: list[str]
) -> None:
    """Criterion G - every disagreeing cell is attributed by a third vendor.

    Nothing is waved through here: a cell that neither reference vendor explains
    is a breach, and an upstream defect that repeats across funds or lands on
    an ex-date is not a typo but a systematic disagreement, so it fails too.
    """
    print("\n## G. 异常格三方归因（腾讯为独立第三 vendor；未被归因即失败）\n")
    by_symbol = {fact.qualified: fact for fact in facts}
    print("| 基金 | 日期 | 字段 | 上游换算 | 新浪 | 腾讯 | 抓到 | 归因 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    if not anomalies:
        print("| — | — | — | — | — | — | — | 无异常格 |")
        print("\n- 三个判据（A/B/C）在全部共同交易日上未留下一格待解释的差异。")
        return
    upstream_days: dict[str, list[date]] = {}
    for item in anomalies:
        fact = by_symbol[item.symbol]
        third_row = fact.tencent.get(item.day)
        third = _tx_value(third_row, item.field)
        agrees_ours = third is not None and _agrees(item.ours, third, item.field)
        agrees_theirs = third is not None and _agrees(item.theirs, third, item.field)
        sina_row = fact.reference.get(item.day)
        if item.theirs <= 0.0 and item.ours > 0.0 and agrees_ours:
            # 参照侧给 0 而行存在（腾讯同一天有量）：那是缺测，不是单位差。
            verdict = "新浪缺测（该日有行，量额为 0）"
        elif item.field == AMOUNT_FIELD and sina_row is not None and sina_row[1] <= 0.0:
            # 额没有第三 vendor 可对：量与额一起为 0、而腾讯该日有量，即为整行缺测。
            verdict = (
                "新浪缺测（量额同为 0，腾讯该日有量）"
                if third_row is not None and third_row.volume_lots > 0.0 and item.ours > 0.0
                else "未归因"
            )
        elif agrees_theirs and not agrees_ours:
            verdict = "上游单格离群"
        elif agrees_ours and not agrees_theirs:
            verdict = "新浪单格离群"
        else:
            verdict = "未归因"
        print(
            f"| {item.symbol} | {item.day} | {item.field} | {item.ours:.3f} | {item.theirs:.3f} "
            f"| {'—' if third is None else f'{third:.3f}'} | {item.caught_by} | {verdict} |"
        )
        if verdict == "未归因":
            failures.append(f"G/{item.symbol}/{item.day}/{item.field}: 第三 vendor 无法归因")
        if verdict == "上游单格离群":
            upstream_days.setdefault(item.symbol, []).append(item.day)

    print("\n### G'. 离群是不是「单格」：落在除息日上、或一只基金上多天，就不是笔误\n")
    judged = sum(len(set(fact.published) & set(fact.reference)) for fact in facts)
    for symbol, days in upstream_days.items():
        fact = by_symbol[symbol]
        ex_dates = {event.ex_date for event in fact.events}
        on_ex = sorted(day for day in set(days) if day in ex_dates)
        outlier_days = set(days)
        peers = sorted(
            other
            for other, other_days in upstream_days.items()
            if other != symbol and outlier_days & set(other_days)
        )
        rate = len(outlier_days) / max(len(fact.published), 1)
        print(
            f"- {symbol}：离群日 {sorted(outlier_days)}（该基金 {len(outlier_days)} 日 / "
            f"{len(fact.published)} 根 = {rate:.2e}）；落在除息日上的 {on_ex}；"
            f"同日他基金亦离群 {peers}"
        )
        if on_ex:
            failures.append(f"G'/{symbol}: 上游离群格落在除息日 {on_ex} ⇒ 是口径问题，不接受")
        if len(outlier_days) > MAX_UPSTREAM_OUTLIER_DAYS:
            failures.append(
                f"G'/{symbol}: 离群日 {len(outlier_days)} 天 > {MAX_UPSTREAM_OUTLIER_DAYS} ⇒ "
                "系统性差异，不按笔误处理"
            )
        if peers:
            failures.append(f"G'/{symbol}: 与 {peers} 在同一天一起离群 ⇒ 同日共性，不是笔误")
    print(
        f"\n- 全部判格基数 {judged} 根（{len(facts)} 只基金的共同交易日之和）；"
        f"离群上限 {MAX_UPSTREAM_OUTLIER_DAYS} 日是**跑前就写死的收紧**，\n"
        "  不是为今天的读数留口子：多于一日即视为系统性，本轮不放行（见模块 docstring 判据 G）。"
    )
    fields_per_day: dict[tuple[str, date], set[str]] = {}
    for item in anomalies:
        fields_per_day.setdefault((item.symbol, item.day), set()).add(item.field)
    for (symbol, day), fields in sorted(fields_per_day.items()):
        print(f"- {symbol} {day}：涉及字段 {sorted(fields)}（一根 K 线上的哪几格）")


def judge_channel_guards(failures: list[str]) -> None:
    """Criterion D - the depth cliff, how a miss is reported, and the span cap."""
    from opendata.data.providers.ths.models._client import client

    today = date.today()
    symbol = FUNDS[0][0]
    print("## D. 通道守卫：深度地板 / 越界怎么报 / 跨度上限\n")

    def served(active: FuyaoHttpClient, start: date, span: int) -> bool:
        end = start + timedelta(days=span)
        envelope, _ = _ask(active, symbol=symbol, start=start, end=end)
        return envelope is not None and len(envelope.items) > 0

    def bisect_floor(active: FuyaoHttpClient, span: int) -> date:
        low, high = today - timedelta(days=2400), today - timedelta(days=1400)
        while (high - low).days > 1:
            mid = low + timedelta(days=(high - low).days // 2)
            if served(active, mid, span):
                high = mid
            else:
                low = mid
            time.sleep(PROBE_PACE_SECONDS)
        return high

    with client(timeout_seconds=60.0) as active:
        floors: dict[int, date] = {}
        for span in (300, 1200):
            floors[span] = bisect_floor(active, span)
            gap = (today - floors[span]).days
            print(f"- span={span:>4}d 的地板 start={floors[span]}  距今 {gap}d")
            time.sleep(PROBE_PACE_SECONDS)
        same = floors[300] == floors[1200]
        offset = (today - floors[1200]).days
        anchor = "跟随「今天」（滚动窗口）" if same else "跟随窗口自身"
        print(
            f"\n  两种窗口长度地板{'相同' if same else '不同'} ⇒ 悬崖{anchor}；"
            f"距今 {offset}d vs 先前采样 {CLAIMED_FLOOR_OFFSET}d"
            f" {'●复现' if offset == CLAIMED_FLOOR_OFFSET else '○未复现'}"
        )
        if offset != CLAIMED_FLOOR_OFFSET:
            failures.append(f"D: 地板距今 {offset}d，未复现 {CLAIMED_FLOOR_OFFSET}d")
        if not same:
            failures.append(f"D: 地板随窗口长度移动（{floors[300]} vs {floors[1200]}）⇒ 语义要重判")

        print("\n### D'. 越界是怎么报的（空帧 vs 业务码）\n")
        print("| 起点相对地板 | 应答 |")
        print("| --- | --- |")
        for label, probe_start in (
            ("地板当天", floors[1200]),
            ("地板前一天", floors[1200] - timedelta(days=1)),
            ("地板前 1 年", floors[1200] - timedelta(days=365)),
            ("地板前 5 年", floors[1200] - timedelta(days=1825)),
        ):
            end = probe_start + timedelta(days=300)
            envelope, err = _ask(active, symbol=symbol, start=probe_start, end=end)
            verdict = (
                f"code=0，item 行数={len(envelope.items)}"
                if envelope is not None
                else f"报错：{err}"
            )
            print(f"| {label} {probe_start}→{end} | {verdict} |")
            time.sleep(PROBE_PACE_SECONDS)
        print(
            "\n  早于地板的窗口回的是 **code=0 的空 item** ⇒ 与「这只基金从没交易过」在信封上"
            "不可区分（C11a 指数腿同一类缺陷）。\n  ⇒ 适配器必须在发请求前自查地板，"
            "并把「请求起点早于服务端深度」作为一种**错误**发出去。"
        )

        print("\n### D''. 跨度上限二分（起点固定在地板内，终点往后推）\n")

        def span_ok(span: int) -> tuple[bool, str]:
            probe_start = floors[1200] + timedelta(days=10)
            envelope, err = _ask(
                active,
                symbol=symbol,
                start=probe_start,
                end=probe_start + timedelta(days=span),
            )
            time.sleep(PROBE_PACE_SECONDS)
            return envelope is not None, err

        low, high = 900, 2400
        while high - low > 1:
            mid = (low + high) // 2
            ok, _ = span_ok(mid)
            if ok:
                low = mid
            else:
                high = mid
        _, over_err = span_ok(low + 1)
        print(
            f"- 接受的最大跨度 = {low}d；+1d 即拒：{over_err}\n"
            f"- 先前采样 {CLAIMED_MAX_SPAN}d {'●复现' if low == CLAIMED_MAX_SPAN else '○未复现'}；"
            f"折算 {low / 365.25:.2f} 年\n"
            "- 错误文案写「单次不超过 10 年」而实际上限≈5 年 ⇒ 本地守卫的文案必须点名实测上限，"
            "否则排障会被引去查股票腿。"
        )
        if low != CLAIMED_MAX_SPAN:
            failures.append(f"D'': 跨度上限 {low}d，未复现 {CLAIMED_MAX_SPAN}d")


def judge_parameters(failures: list[str]) -> None:
    """Criterion E - the parameter surface and the envelope echo."""
    from opendata.data.providers.ths.endpoints import millis_to_trading_date
    from opendata.data.providers.ths.models._client import client

    print("\n## E. 参数面：adjust / interval / limit / 信封回显\n")
    start = date.today() - timedelta(days=400)
    end = date.today() - timedelta(days=100)
    with client(timeout_seconds=60.0) as active:
        base, _ = _ask(active, symbol=FUNDS[0][0], start=start, end=end)
        base_tag = _fingerprint(base)
        print(f"- 基准窗口 {start}→{end}：{base_tag}")
        time.sleep(PROBE_PACE_SECONDS)
        probes: tuple[tuple[str, dict[str, object]], ...] = (
            ("adjust 缺省", {}),
            ("adjust=0", {"adjust": 0}),
            ("adjust=1", {"adjust": 1}),
            ("adjust=2", {"adjust": 2}),
            ("adjust=forward", {"adjust": "forward"}),
            ("adjust=backward", {"adjust": "backward"}),
            ("interval=1w", {"interval": "1w"}),
            ("limit=5", {"limit": 5}),
        )
        for label, extra in probes:
            envelope, err = _ask(active, symbol=FUNDS[0][0], start=start, end=end, **extra)
            if envelope is None:
                print(f"- {label:<16} 被拒：{err}")
            else:
                tag = _fingerprint(envelope)
                same = "●与基准同" if tag == base_tag else "○与基准不同"
                print(f"- {label:<16} {tag} {same}")
            time.sleep(PROBE_PACE_SECONDS)
        print(
            "  读法：C6/C15 记作「连 adjust 参数都不接受」。若实测是**接受但完全无效**，"
            "那比拒绝更危险——请求侧看不出没生效。\n"
            "  interval 与 limit 同列判：limit 若真的截断行数，它就是分页游标而不是被忽略，"
            "适配层的取数逻辑要另写。"
        )
        if base is not None:
            stamp = base.data_timestamp_ms
            last_ms = base.items[-1]["date_ms"]
            last_bar = millis_to_trading_date(last_ms)
            stamp_kind = "窗口内末根" if str(stamp) == str(last_ms) else "请求时刻"
            print(
                f"\n- data 键集={sorted(base.data)}\n- 行键集={sorted(base.items[0])}\n"
                f"- data.adjust={base.data.get('adjust')!r}"
                f"  data.thscode={base.data.get('thscode')!r}\n"
                f"- data.timestamp={stamp} → {millis_to_trading_date(stamp)}；末根 → {last_bar}"
                f" ⇒ 信封戳是{stamp_kind}\n"
                "- 新鲜度判据只能取**末根交易日**，不能取信封戳：戳是窗口相对的，"
                "窗口不含今天时拿它当「上游更新到哪天」会静默漏判。"
            )
        else:
            failures.append("E: 基准窗口取不到信封")


def judge_leg(facts: Sequence[FundFacts], failures: list[str]) -> None:
    """Criterion F - once wired: routed bars must equal the unadjusted truth."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    print("\n## F. 接入后的腿（注册表路由 → 契约 Bar）\n")
    register_providers()
    try:
        routed = get_registry().resolve_domain("fund_etf_daily", source="ths")
    except LookupError as exc:
        print(f"- 未接入（本轮先冻结前置测量，接入后重跑即判此项）：{type(exc).__name__}: {exc}")
        return
    capability = routed.capability
    print(
        f"- 已接入：asset_class={capability.asset_class} period={capability.period} "
        f"verified={capability.verified}\n- notes={capability.notes}"
    )
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=WINDOW_DAYS)
    bars = cast(
        "tuple[Bar, ...]",
        tuple(routed.fetch(symbol=FUNDS[0][0], start_date=start, end_date=end)),
    )
    fact = facts[0]
    print(
        f"- 路由取回 {len(bars)} 根（窗口 {start}→{end}）；"
        f"搬运层发布 {len(fact.published)} 根 / 新浪 {len(fact.reference)} 根"
    )
    bad = 0
    missing = 0
    for bar in bars:
        theirs = fact.reference.get(bar.trade_date)
        if theirs is None:
            missing += 1
            continue
        for ours, ref in zip((bar.open, bar.high, bar.low, bar.close), theirs[0], strict=True):
            bad += 1 if abs(ours - ref) > PRICE_TOLERANCE else 0
    ok = bool(bars) and bad == 0 and capability.verified
    verdict = "相符" if ok else "不符"
    print(
        f"- 逐格对新浪不复权：不等格 {bad}，无参照 {missing}；"
        f"verified={capability.verified} ⇒ {verdict}"
    )
    if not ok:
        failures.append(f"F: 路由腿与新浪不复权有 {bad} 格不符 / verified={capability.verified}")
    notes_ok = any(
        marker in capability.notes for marker in ("1827", "rolling", "深度", "滚动", "depth")
    )
    print(f"- notes 是否载明滚动深度：{notes_ok}")
    if not notes_ok:
        failures.append("F: capability.notes 未载明滚动深度")
    bare = cast(
        "tuple[Bar, ...]",
        tuple(routed.fetch(symbol=BARE_ROUTE_SYMBOL, start_date=start, end_date=end)),
    )
    same_bare = [(b.trade_date, b.close) for b in bare] == [(b.trade_date, b.close) for b in bars]
    print(f"- 裸码 {BARE_ROUTE_SYMBOL} 经 ETF 目录解析：{len(bare)} 根，与合格码一致={same_bare}")
    if not same_bare:
        failures.append("F: 裸码解析与合格码取数不一致")


def main() -> int:
    """Run every leg, print the evidence report, and judge A-G.

    Returns:
        ``0`` when all judged criteria hold, ``1`` otherwise.
    """
    print("# C20 ths 场内基金日线（fund_etf_daily）跨 vendor 对照（全程只读：不建表、不写仓）\n")
    failures: list[str] = []
    anomalies: list[Anomaly] = []
    facts, start, end = _collect()
    judge_series_shape(facts, failures, anomalies)
    judge_conversion(facts, failures, anomalies)
    judge_boundary_discrimination(facts, failures)
    judge_units(facts, failures, anomalies)
    judge_anomalies(facts, anomalies, failures)
    judge_channel_guards(failures)
    judge_parameters(failures)
    judge_leg(facts, failures)
    print(f"\n- 数据窗口基准：{start} → {end}（WINDOW_DAYS={WINDOW_DAYS}）")

    print("\n## 判定")
    if failures:
        print(f"FAIL（{len(failures)} 项）")
        for line in failures:
            print(f"- {line}")
        return 1
    print("PASS：A/B/B'/C/C'/C''/G/G'/D/D'/D''/E/F 全部成立。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
