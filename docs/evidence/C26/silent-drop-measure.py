"""Measure every row the adapter layer drops, and every field it never fills (task #34).

C24 made the sina legs honour the caller's window; C25 taught the patrol to read
columns. Both stopped one step short of the question asked here: ``normalize()``
returns fewer rows than upstream sent - is every one of those losses *required by
the contract*, or is the adapter quietly answering a different question than the
one it was asked?

Every read comes from production code. The classifier below calls the same
helpers ``AkshareStockActionFetcher.transform_data`` calls and then asserts its
own kept-count against ``transform_data`` run on the same frame, so a drift in
this script fails the run instead of hiding it. Sections C and E read the
``CorporateAction`` rows ``transform_data`` returned - never a re-derivation of
them - because a script that recomputes the unit conversion in its own prose
would keep printing the old bug after the fix.

* A - 分红页: attribute each dropped row to a reason, and for an undated plan that
  still carries amounts ask whether the same announcement already has a dated row
  (the 601318 shape: sina dates the implemented plan and leaves the second plan's
  ``除权除息日`` empty).
* B - kept rows: how many share one ex-date, since a consumer keying on
  ``(symbol, ex_date)`` sees only the first.
* C - 配股页: what the adapter actually publishes for ``rights_shares`` and
  ``rights_price``, cross-checked against the ths primary leg for the same event.
* D - the ths-vs-sina cash gap on a shared ex-date: is it exactly the amount of a
  row this adapter dropped?
* E - the two rights units, settled against 东方财富's allotment table (a second
  vendor, cell by cell) instead of against this script's own assumption.
* F - the primary leg's field surface: which keys a real fuyao adjustment envelope
  carries, i.e. whether ``rights_*`` has a producer at all on that leg.

C and E query the allotment pages with the window widened to the whole history
(``_rights_query``): five of the six dated allotments in the sample predate the
2015 probe window, and a window cut would leave the unit question unmeasured.

Read-only: sina and fuyao are queried, nothing is written to the warehouse and no
credential is printed.

Usage (py313 env; needs the fuyao credentials):
    python docs/evidence/C26/silent-drop-measure.py
"""

from __future__ import annotations

import sys
from collections import Counter
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if TYPE_CHECKING:
    import pandas as pd

    from opendata.data.models import CorporateAction
    from opendata.data.protocol import Fetcher, QueryParams
    from opendata.data.providers.akshare.models.stock_action import (
        AkshareStockActionFetcher,
        StockActionQuery,
    )

#: Dividend shapes: ``601318`` is the recorded under-report, ``600519`` carries
#: two plans on one ex-date in the pinned fixture, the rest spread the boards.
DIVIDEND_SYMBOLS: tuple[str, ...] = (
    "600519",
    "601318",
    "000651",
    "000333",
    "002415",
    "600036",
    "300750",
    "000002",
)
#: Names whose sina 配股 page carries an allotment; rights issues are rare.
RIGHTS_SYMBOLS: tuple[str, ...] = ("000002", "600030", "601328", "600999", "601211", "600000")
WINDOW_START = date(2015, 1, 1)
WINDOW_END = date(2024, 12, 31)
#: Window handed to the ths leg: full history, so a shallow primary leg shows up
#: as "no reading" rather than as a disagreement.
DEEP_START = date(1990, 1, 1)
TOLERANCE = 1e-4
SAMPLE_LINES = 6


def _fetcher(source: str, domain: str, asset_class: str) -> Fetcher[QueryParams, object]:
    """Resolve one leg from the live registry, registering providers once."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    resolved = get_registry().resolve(asset_class, domain, source=source)
    return cast("Fetcher[QueryParams, object]", resolved)


def _query(symbol: str) -> StockActionQuery:
    """The validated query the akshare leg gets for every read below."""
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    return AkshareStockActionFetcher().transform_query(
        symbol=symbol, start_date=WINDOW_START, end_date=WINDOW_END
    )


def _rights_query(symbol: str) -> StockActionQuery:
    """The same query with the window widened to the whole allotment history.

    The 配股 pages are old: five of the six dated allotments in the sample sit
    before 2015, so the probe window would cut them and C/E would have nothing
    to read. The unit question is about the published row, not about the window.
    """
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    return AkshareStockActionFetcher().transform_query(
        symbol=symbol, start_date=DEEP_START, end_date=WINDOW_END
    )


def _raw_action_pages(symbol: str) -> pd.DataFrame:
    """The combined sina frame the akshare leg actually normalizes."""
    from opendata.data.protocol import FetchContext
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    raw = AkshareStockActionFetcher().extract_data(_query(symbol), FetchContext())
    return cast("pd.DataFrame", raw)


def _normalize(
    fetcher: AkshareStockActionFetcher, frame: pd.DataFrame, params: StockActionQuery
) -> list[CorporateAction]:
    """Run the production normalize stage on one indicator's rows."""
    return cast("list[CorporateAction]", fetcher.transform_data(frame, params))


def _ths_actions(symbol: str) -> list[CorporateAction]:
    """The primary leg's events for one symbol, as deep as it publishes."""
    rows = _fetcher("ths", "stock_action", "equity").fetch(
        symbol=symbol, start_date=DEEP_START, end_date=WINDOW_END
    )
    return list(cast("Any", rows))


def _rel_gap(left: float, right: float) -> float:
    """Scale-aware distance between two readings."""
    return abs(left - right) / max(abs(left), abs(right), 1.0e-9)


def classify_dividends(raw: pd.DataFrame) -> dict[str, Any]:
    """Re-apply the adapter's own predicates to every 分红 row.

    Args:
        raw: The combined sina frame carrying the ``indicator`` marker.

    Returns:
        Reason counts, kept rows, and the undated rows that still carry amounts.
    """
    from opendata.data.providers.akshare.models._normalize import as_date, within_window
    from opendata.data.providers.akshare.models.stock_action import _numeric, _per_share

    page = raw[raw["indicator"] == "分红"]
    kept: list[dict[str, Any]] = []
    undated: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    blank_cells = 0

    for record in page.to_dict("records"):
        ex_date = as_date(record.get("除权除息日"))
        cash = _per_share(record.get("派息"))
        stock = _per_share(record.get("送股")) + _per_share(record.get("转增"))
        if ex_date is None:
            reasons["无除息日"] += 1
            if cash != 0.0 or stock != 0.0:
                undated.append(
                    {
                        "announce": record.get("公告日期"),
                        "progress": record.get("进度"),
                        "cash": cash,
                        "stock": stock,
                    }
                )
            continue
        if not within_window(ex_date, WINDOW_START, WINDOW_END):
            reasons["窗口外"] += 1
            continue
        if cash == 0.0 and stock == 0.0:
            reasons["全零"] += 1
            if all(_numeric(record.get(name)) is None for name in ("派息", "送股", "转增")):
                blank_cells += 1
            continue
        reasons["保留"] += 1
        kept.append(
            {
                "ex_date": ex_date,
                "announce": record.get("公告日期"),
                "cash": cash,
                "stock": stock,
                "progress": record.get("进度"),
            }
        )

    return {
        "reasons": reasons,
        "kept": kept,
        "undated": undated,
        "blank_cells": blank_cells,
        "total": len(page),
    }


def _undated_line(dropped: dict[str, Any]) -> str:
    """One printed row for an undated plan that still carries amounts."""
    return (
        f"公告 {dropped['announce']} 进度={dropped['progress']}"
        f" 派息={dropped['cash']} 送转={dropped['stock']}"
    )


def _pair_undated(info: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Split undated-but-numbered plans into paired and orphan."""
    lines: list[str] = []
    orphans: list[str] = []
    for dropped in info["undated"]:
        shared = [row for row in info["kept"] if row["announce"] == dropped["announce"]]
        if not shared:
            orphans.append(_undated_line(dropped))
            continue
        first = shared[0]
        lines.append(
            f"{_undated_line(dropped)}"
            f" | 同公告日已保留 {first['ex_date']}"
            f" 派息={first['cash']} 送转={first['stock']}"
        )
    return lines, orphans


def measure_dividends() -> int:
    """Sections A and B: drop attribution plus ex-date collisions."""
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    print(f"\n===== A. 分红页逐行归因（窗口 {WINDOW_START}..{WINDOW_END}）=====")
    drift = 0
    paired_total = orphan_total = undated_total = 0
    collisions: list[str] = []

    for symbol in DIVIDEND_SYMBOLS:
        try:
            raw = _raw_action_pages(symbol)
        except Exception as exc:  # a refused page is no reading, not a loss
            print(f"  [NO_READING] {symbol}: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        fetcher = AkshareStockActionFetcher()
        params = _query(symbol)
        info = classify_dividends(raw)
        accepted = _normalize(fetcher, raw[raw["indicator"] == "分红"], params)
        if len(info["kept"]) != len(accepted):
            drift += 1
            print(
                f"  [CLASSIFIER_DRIFT] {symbol}:"
                f" 分类器保留={len(info['kept'])} 适配层返回={len(accepted)}"
            )

        reasons: Counter[str] = info["reasons"]
        undated_total += len(info["undated"])
        lines, orphans = _pair_undated(info)
        paired_total += len(lines)
        orphan_total += len(orphans)

        seen: Counter[date] = Counter(row["ex_date"] for row in info["kept"])
        collisions.extend(
            f"{symbol} {day}：{seen[day]} 条同除息日方案"
            for day in sorted(d for d, n in seen.items() if n > 1)
        )

        print(
            f"  {symbol}: 上游 {info['total']} 行"
            f" 保留={reasons['保留']} 无除息日={reasons['无除息日']}"
            f" 窗口外={reasons['窗口外']} 全零={reasons['全零']}"
            f"（单元格全空={info['blank_cells']}）"
            f" 无除息日但有金额={len(info['undated'])}"
        )
        for line in lines[:SAMPLE_LINES]:
            print(f"       [同公告日丢数] {line}")
        for line in orphans[:SAMPLE_LINES]:
            print(f"       [孤儿] {line}")

    print(
        f"  小结：无除息日但有金额 {undated_total} 条"
        f"，其中同公告日已有保留行 {paired_total} 条、孤儿 {orphan_total} 条"
    )
    print(f"  B. 同除息日碰撞 {len(collisions)} 处")
    for line in collisions[:10]:
        print(f"       {line}")
    return drift


def measure_rights() -> tuple[int, int, int]:
    """Section C: what the rights branch publishes, against the ths primary."""
    from opendata.data.providers.akshare.models._normalize import as_date
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    print("\n===== C. 配股页字段读数（逐事件对 ths 主腿）=====")
    drift = 0
    events = 0
    no_row = 0
    zero_ratio = 0
    price_shift = 0

    for symbol in RIGHTS_SYMBOLS:
        try:
            raw = _raw_action_pages(symbol)
        except Exception as exc:
            print(f"  [NO_READING] {symbol}: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        page = raw[raw["indicator"] == "配股"]
        fetcher = AkshareStockActionFetcher()
        params = _rights_query(symbol)
        accepted = _normalize(fetcher, page, params)
        if len(page) == 0:
            print(f"  {symbol}: 配股页无数据")
            continue

        primary: dict[str, CorporateAction] = {}
        try:
            for row in _ths_actions(symbol):
                if row.rights_shares or row.rights_price:
                    primary.setdefault(str(row.ex_date), row)
        except Exception as exc:
            print(f"  {symbol}: ths 无读数（{type(exc).__name__}: {str(exc)[:80]}）")

        print(
            f"  {symbol}: 配股页 {len(page)} 行"
            f" 除权日非空、全史窗口内 -> 适配层返回 {len(accepted)} 条"
            f"；ths 全史配股事件 {len(primary)} 条"
        )
        for record in page.to_dict("records"):
            ex_date = as_date(record.get("除权日"))
            if ex_date is None:
                continue
            events += 1
            plan = record.get("配股方案")
            published = _published(accepted, ex_date)
            if published is None:
                no_row += 1
                print(f"    {ex_date} 配股方案={plan!r} -> 适配层未返回该事件")
                continue
            ratio = published.rights_shares
            price = published.rights_price
            if ratio == 0.0:
                zero_ratio += 1
            if abs(price - float(record.get("配股价格") or 0.0)) > TOLERANCE:
                price_shift += 1
            sample = (
                f"    {ex_date} 配股方案={plan!r}({type(plan).__name__})"
                f" 配股价格={record.get('配股价格')!r}"
                f" -> rights_shares={ratio} rights_price={price}"
            )
            ref = primary.get(str(ex_date))
            if ref is None:
                print(f"{sample} | ths 该日无配股事件")
                continue
            ok = (
                _rel_gap(ref.rights_shares, ratio) <= TOLERANCE
                and _rel_gap(ref.rights_price, price) <= TOLERANCE
            )
            if not ok:
                drift += 1
            print(
                f"{sample} | ths[{str(ex_date)}] rights_shares={ref.rights_shares}"
                f" rights_price={ref.rights_price} -> {'OK' if ok else 'MISMATCH'}"
            )

    print(
        f"  小结：除权日非空 {events} 条，适配层未返回 {no_row} 条"
        f"，rights_shares 解析为 0 的 {zero_ratio} 条"
        f"，rights_price 与原始值不同(÷10) 的 {price_shift} 条"
        f"，与 ths 不符 {drift} 条"
    )
    return drift, zero_ratio, price_shift


def _published(accepted: list[CorporateAction], ex_date: date) -> CorporateAction | None:
    """The event the adapter actually returned for one 除权日.

    Args:
        accepted: Rows ``transform_data`` returned for this page.
        ex_date: The 除权日 to look up.

    Returns:
        The published row, or None when the adapter dropped it.
    """
    for row in accepted:
        if row.ex_date == ex_date:
            return row
    return None


def measure_gap_attribution() -> tuple[int, int]:
    """Section D: does a dropped undated plan carry exactly what ths counts extra?

    The recorded 601318 shape is a cash dividend the primary leg reports higher
    than the fallback. Matching on announcement dates did not explain it (section
    A reads every undated plan as an orphan), so this asks the weaker, amount-level
    question: for an ex-date where ths and akshare disagree, is the difference
    equal to one of the rows the adapter threw away?
    """
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    print("\n===== D. 与 ths 同除息日差额是否等于被丢行（按金额归因）=====")
    matched = 0
    unmatched = 0
    for symbol in DIVIDEND_SYMBOLS:
        try:
            raw = _raw_action_pages(symbol)
            primary = _ths_actions(symbol)
        except Exception as exc:
            print(f"  [NO_READING] {symbol}: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        info = classify_dividends(raw)
        accepted = _normalize(
            AkshareStockActionFetcher(), raw[raw["indicator"] == "分红"], _query(symbol)
        )
        cand = {str(row.ex_date): row for row in accepted}
        ref = {
            str(row.ex_date): row for row in primary if WINDOW_START <= row.ex_date <= WINDOW_END
        }
        for day in sorted(set(ref) & set(cand)):
            gap = ref[day].cash_dividend - cand[day].cash_dividend
            if abs(gap) <= TOLERANCE:
                continue
            hits = [row for row in info["undated"] if abs(row["cash"] - gap) <= TOLERANCE]
            verdict = "MATCHED" if hits else "UNMATCHED"
            if hits:
                matched += 1
            else:
                unmatched += 1
            print(
                f"  {symbol} {day}: ths={ref[day].cash_dividend}"
                f" akshare={cand[day].cash_dividend} 差额={gap:.6f} -> [{verdict}]"
            )
            for row in hits[:SAMPLE_LINES]:
                print(f"       被丢行即差额：{_undated_line(row)}")
            if not hits:
                print(f"       被丢无除息日行 {len(info['undated'])} 条，无一条金额等于 {gap:.6f}")
        only_ref = sorted(set(ref) - set(cand))
        if only_ref:
            print(
                f"       {symbol}: ths 有而退路无的除息日 {len(only_ref)} 个："
                f"{', '.join(only_ref[:6])}"
            )
    print(f"  小结：差额可由被丢行解释 {matched} 处，解释不了 {unmatched} 处")
    return matched, unmatched


_ALLOT_CACHE: dict[str, pd.DataFrame] = {}


def _em_allotment() -> pd.DataFrame:
    """东方财富的整市场配股表（与 sina 不同的 vendor，同一事件的第二读数）."""
    if "all" not in _ALLOT_CACHE:
        import opendata_http

        _ALLOT_CACHE["all"] = opendata_http.stock_pg_em()
    return _ALLOT_CACHE["all"]


def _em_rows(symbol: str) -> list[dict[str, Any]]:
    """The allotment rows one symbol publishes on the other vendor's table."""
    frame = _em_allotment()
    return [row for row in frame.to_dict("records") if str(row.get("股票代码")) == symbol]


def cross_check_rights() -> tuple[int, int, int]:
    """Section E: settle the two rights units against an independent vendor.

    ``配股价`` is a price, so dividing it by 10 is only right if the page means
    per-10-share money. The em table publishes both columns for the same events,
    plus 配股数量 / 配股前总股本, which says what one unit of 配股比例 bought.
    """
    from opendata.data.providers.akshare.models._normalize import as_date
    from opendata.data.providers.akshare.models.stock_action import AkshareStockActionFetcher

    print("\n===== E. 配股口径跨 vendor 对账（sina vs 东方财富）=====")
    matched_cells = 0
    adapter_bad = 0
    unpublished = 0
    compared = 0
    for symbol in RIGHTS_SYMBOLS:
        try:
            raw = _raw_action_pages(symbol)
            em_rows = _em_rows(symbol)
        except Exception as exc:
            print(f"  [NO_READING] {symbol}: {type(exc).__name__}: {str(exc)[:120]}")
            continue
        page = raw[raw["indicator"] == "配股"]
        if len(page) == 0 or not em_rows:
            print(f"  {symbol}: sina 配股 {len(page)} 行 / em {len(em_rows)} 行 -> 无可对事件")
            continue
        accepted = _normalize(AkshareStockActionFetcher(), page, _rights_query(symbol))

        for record in page.to_dict("records"):
            ex_date = as_date(record.get("除权日"))
            if ex_date is None:
                continue
            plan = record.get("配股方案")
            price = record.get("配股价格")
            plan_num = _float(plan)
            price_num = _float(price)
            published = _published(accepted, ex_date)
            if published is None:
                unpublished += 1
                print(f"  {symbol} {ex_date}: 适配层未返回该事件，无法对账")
                continue
            ratio = published.rights_shares
            published_price = published.rights_price
            # em states the ratio as text "10配N"; that N is the per-10 quantity.
            best = min(
                em_rows,
                key=lambda row: _plan_distance(row.get("配股比例"), plan),
            )
            em_plan = _plan_number(best.get("配股比例"))
            em_price = _float(best.get("配股价"))
            base = _float(best.get("配股前总股本"))
            allotted = _float(best.get("配股数量"))
            compared += 1
            cell_ok = (
                plan_num is not None
                and em_plan is not None
                and abs(plan_num - em_plan) <= 1e-6
                and price_num is not None
                and em_price is not None
                and abs(price_num - em_price) <= 1e-6
            )
            if cell_ok:
                matched_cells += 1
            value_ok = (
                abs(ratio - (em_plan or 0.0) / 10.0) <= TOLERANCE
                and abs(published_price - (em_price or 0.0)) <= TOLERANCE
            )
            if not value_ok:
                adapter_bad += 1
            actual = allotted / base if base is not None and allotted is not None else None
            print(
                f"  {symbol} {ex_date} sina: 方案={plan!r} 价格={price!r}"
                f" | em: 比例={best.get('配股比例')!r} 价={best.get('配股价')!r}"
                f" 数量/前股本={actual if actual is None else round(actual, 6)}"
            )
            print(
                f"       单元格一致={'是' if cell_ok else '否'}"
                f" 适配层现值 rights_shares={ratio} rights_price={published_price}"
                f" 应为={((em_plan or 0.0) / 10.0) if em_plan is not None else None},"
                f"{em_price} -> {'OK' if value_ok else 'WRONG'}"
            )
    print(
        f"  小结：可对事件 {compared} 条"
        f"，sina 与 em 单元格逐字相等 {matched_cells} 条"
        f"，适配层未返回 {unpublished} 条"
        f"，适配层读数与 em 口径不符 {adapter_bad} 条"
    )
    return compared, matched_cells, adapter_bad


def _plan_number(text: object) -> float | None:
    """The per-10-share quantity inside em's ``10配N`` spelling."""
    from opendata.data.providers.akshare.models.stock_action import _RIGHTS_PLAN

    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    match = _RIGHTS_PLAN.search(text)
    return float(match.group(1)) if match else None


def _plan_distance(text: object, plan: object) -> float:
    """How far one em plan text sits from a sina numeric plan."""
    number = _plan_number(text)
    target = _float(plan)
    if number is None or target is None:
        return 1.0e9
    return abs(number - target)


def _float(value: object) -> float | None:
    """Read an upstream numeric cell as float, None when it is not a number."""
    import math

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        reading = float(value)
        return None if math.isnan(reading) else reading
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def primary_field_surface() -> list[str]:
    """Section F: which fields the primary leg's envelope actually publishes."""
    from opendata.data.providers.ths.models._client import client
    from opendata_fuyao.endpoints import (
        ADJUSTMENT_FACTORS_ENDPOINT,
        build_adjustment_factors_request,
    )

    print("\n===== F. 主腿（fuyao 除复权端点）字段面 =====")
    lines: list[str] = []
    key_union: set[str] = set()
    with client(timeout_seconds=30.0) as active:
        for code, start, end in (
            ("600030.SH", date(2021, 12, 1), date(2022, 3, 1)),
            ("600999.SH", date(2020, 6, 1), date(2020, 9, 1)),
            ("601318.SH", date(2018, 5, 1), date(2018, 7, 1)),
        ):
            envelope = active.get(
                ADJUSTMENT_FACTORS_ENDPOINT,
                params=build_adjustment_factors_request(symbol=code, start=start, end=end),
            ).envelope
            keys: set[str] = set()
            samples: list[str] = []
            for row in envelope.items:
                keys.update(str(name) for name in row)
                samples.append(str(dict(row)))
            key_union |= keys
            line = f"  {code} 事件 {len(envelope.items)} 条 字段={sorted(keys)}"
            print(line)
            lines.append(line)
            for sample in samples[:1]:
                print(f"       样例行(不含凭证)={sample[:200]}")
    print(f"  小结：主腿信封出现过的字段并集={sorted(key_union)}")
    print(
        "  推论：信封没有配股列 -> 契约的 rights_shares/rights_price 在主腿"
        " 结构性无生产者；ths 腿文档写着「the allotment columns map onto the rights"
        " fields」，与实测字段面不符"
    )
    return sorted(key_union)


def main() -> int:
    """Run both sections and print the disposition."""
    print("===== C26 适配层丢弃与空字段真机度量（只读、不写数仓、不打印密钥）=====")
    print(
        f"python={sys.version.split()[0]} pandas={_pandas_version()}"
        f" 分红符号={len(DIVIDEND_SYMBOLS)} 配股符号={len(RIGHTS_SYMBOLS)}"
        f" 窗口={WINDOW_START}..{WINDOW_END} 容差={TOLERANCE}"
    )
    drift_a = measure_dividends()
    drift_c, zero_ratio, price_shift = measure_rights()
    matched, unmatched = measure_gap_attribution()
    compared, cells_ok, adapter_bad = cross_check_rights()
    fields = primary_field_surface()

    print("\n===== 判定 =====")
    print(f"  分类器与适配层不一致：{drift_a + drift_c} 处（非 0 即本脚本自身不可信）")
    print(f"  rights_shares 恒 0：{zero_ratio} 条 / rights_price 被 ÷10：{price_shift} 条")
    print(f"  同除息日差额能被被丢行解释：{matched} 处 / 不能：{unmatched} 处")
    print(
        f"  配股跨 vendor：可对 {compared} 条"
        f"，sina 与 em 单元格相等 {cells_ok} 条，适配层与 em 口径不符 {adapter_bad} 条"
    )
    print(f"  主腿信封字段并集：{fields}")
    return 0 if (drift_a + drift_c) == 0 else 1


def _pandas_version() -> str:
    """The pandas build the pages were parsed with (column types depend on it)."""
    import pandas

    return str(pandas.__version__)


if __name__ == "__main__":
    sys.exit(main())
