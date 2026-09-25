"""C19: how often does one and the same ths catalog page disagree with itself.

The shape probe (``catalog-date-field-shape.py``) caught A-share ``list_date``
fully filled and fully hollow 51 seconds apart, and both pages carried the same
snapshot instant *at the one-second resolution that probe printed*. Reading the
label at millisecond resolution is what turns the explanation around: the two
faces are two different instants, and each fill state belongs to exactly one of
them. That rules out both explanations C18 was stuck between - no contract
change (the key is always present and the schema is identical) and no "wait for
the end-of-day backfill" (it flipped inside a minute). What is left is an
upstream fleet that answers some requests from a catalog whose date join is
loaded and others from one where it is not.

The questions are therefore factual rather than rhetorical, and each has an
answer that changes what downstream may assert:

* does the fill state track the snapshot instant (a per-copy load state) or the
  asset type (a permanent gap for that type)? Measured: one of each, and the
  two are separable - ``a-share`` tracks the instant in 36 of 36 reads (14
  full, 22 hollow), while ``fund-etf`` is hollow in 24 of 24, ``a-share-index``
  full in 24 of 24 and ``futures`` stable at 877/1,142 in 24 of 24, all of them
  under either instant. So the instant is not one fleet-wide snapshot: the four
  types read inside one round each carry their own.
* is the page ever in a third state - some rows dated, some not, neither
  "full" nor "hollow"? ``fill_state`` classifies per read (``全值`` / ``全空`` /
  ``散值``) so a scattered page cannot hide inside a binary counter. Measured:
  yes, and it is a permanent one - ``futures`` answers 散值 every time because
  the 265 undated rows are its synthetic ``7777``/``8888``/``9999`` series.
* do all of one round's pages come from the same instance? Measured: no -
  adjacent requests carry different instants, so the label identifies the
  build that answered, not the caller's connection.
* is the search route correlated with the list route at all? Measured: no.

Read-only against the live endpoint; nothing is written to the warehouse.

Run (stdout redirected to the sibling ``.txt``)::

    python docs/evidence/C19/catalog-flip-rate.py --reads 12 --pause 5
    python docs/evidence/C19/catalog-flip-rate.py --reads 12 --pause 3 \
        --asset-types a-share,fund-etf,a-share-index,futures
"""

from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from opendata.data.providers.ths.models._client import client
from opendata_fuyao import endpoints as e

if TYPE_CHECKING:
    from opendata_fuyao import FuyaoHttpClient

#: Probe instrument; C14 archived its listing date as 2001-08-27.
SEARCH_QUERY = "600519"

#: Default sample face, matching the archived run of this same command.
DEFAULT_ASSET_TYPES = ("a-share",)

#: Rows a page may leave undated and still count as "the dates are there".
#: C14 measured 9/5,578 new listings with no date yet; 20 is that with slack.
#: Anything above this and below "every row hollow" is the third state, the one
#: that must never be silently bucketed with either of the two known faces.
NULL_TOLERANCE = 20

SHANGHAI = ZoneInfo("Asia/Shanghai")


def stamp_of(millis: int | None) -> str:
    """Render an envelope snapshot instant, milliseconds included."""
    if millis is None:
        return "无标签"
    return datetime.fromtimestamp(millis / 1000, SHANGHAI).strftime("%H:%M:%S.%f")[:-3]


def fill_state(rows: int, valued: int) -> str:
    """Classify one page's date column into the three states it can be in.

    Args:
        rows: Rows on the page.
        valued: Rows whose ``list_date`` is neither missing nor null.

    Returns:
        ``全空`` (no date anywhere), ``全值`` (within :data:`NULL_TOLERANCE`),
        or ``散值`` - a partially dated page, which is neither of the two
        states C19 measured and would be invisible to a boolean counter.
    """
    if valued == 0:
        return "全空"
    if rows - valued <= NULL_TOLERANCE:
        return "全值"
    return "散值"


def read_page(active: FuyaoHttpClient, asset_type: str) -> tuple[int, int, str, str]:
    """Read one full catalog page for one asset type.

    Returns:
        ``(rows, valued, snapshot, request_id)`` where ``valued`` counts rows
        whose ``list_date`` is neither missing nor null.
    """
    listed = active.get(
        e.TICKERS_LIST_ENDPOINT,
        params=e.build_tickers_list_request(
            limit=e.MAX_LIST_LIMIT, offset=0, asset_type=asset_type
        ),
    )
    rows = list(e._items(listed.envelope, context="ticker"))
    valued = sum(1 for row in rows if row.get("list_date"))
    return (
        len(rows),
        valued,
        stamp_of(listed.envelope.data_timestamp_ms),
        listed.envelope.request_id,
    )


def read_search(active: FuyaoHttpClient) -> str:
    """Read the search route's answer for the probe instrument."""
    searched = active.get(
        e.TICKERS_SEARCH_ENDPOINT,
        params=e.build_tickers_search_request(query=SEARCH_QUERY, limit=1),
    )
    hits = list(e._items(searched.envelope, context="ticker"))
    return str(hits[0].get("list_date")) if hits else "无命中"


def main() -> int:
    """Sample the live catalog and report the fill distribution per face."""
    parser = argparse.ArgumentParser(description="Sample the ths catalog pages repeatedly.")
    parser.add_argument("--reads", type=int, default=12, help="How many rounds to sample.")
    parser.add_argument("--pause", type=float, default=5.0, help="Seconds between rounds.")
    parser.add_argument(
        "--asset-types",
        default=",".join(DEFAULT_ASSET_TYPES),
        help="Comma separated asset types sampled side by side.",
    )
    args = parser.parse_args()
    asset_types = tuple(part.strip() for part in args.asset_types.split(",") if part.strip())

    fills: dict[str, Counter[str]] = {name: Counter() for name in asset_types}
    stamps: dict[str, Counter[str]] = {name: Counter() for name in asset_types}
    pairs: Counter[str] = Counter()
    search_hits: Counter[str] = Counter()
    label = ",".join(asset_types)
    print(
        f"[1] 逐轮读目录单页（limit={e.MAX_LIST_LIMIT}，asset_type={label}）+ search {SEARCH_QUERY}"
    )
    for read in range(1, args.reads + 1):
        with client(timeout_seconds=60) as active:
            seen = {name: read_page(active, name) for name in asset_types}
            found = read_search(active)
        search_hits["无日期" if found == "None" else "有日期"] += 1
        print(f"  轮 {read:>2}  search {SEARCH_QUERY}.list_date={found}")
        for name in asset_types:
            rows, valued, stamp, request_id = seen[name]
            state = fill_state(rows, valued)
            fills[name][state] += 1
            stamps[name][stamp] += 1
            hollow = rows - valued
            print(
                f"        {name:<10} 行数 {rows:>5} 有值 {valued:>5} 缺 {hollow:>5} "
                f"形态 {state:<4} 快照 {stamp} req={request_id}"
            )
        if len(asset_types) > 1:
            page_stamps = {seen[name][2] for name in asset_types}
            page_states = {fill_state(seen[name][0], seen[name][1]) for name in asset_types}
            pairs[f"同标签={len(page_stamps) == 1} 形态一致={len(page_states) == 1}"] += 1
        if read < args.reads and args.pause > 0:
            time.sleep(args.pause)

    print("\n[2] 逐资产类型汇总（填充形态 / 应答标签的快照时刻）")
    for name in asset_types:
        print(f"    {name:<10} {dict(fills[name])}  {dict(stamps[name])}")
    if len(asset_types) > 1:
        print(f"\n[3] 同一轮内的标签配对：{dict(pairs)}")
    print(f"    search 路由填充态：{dict(search_hits)}")
    print(
        "\n[4] 读法：形态只随快照标签变 ⇒ 缺失在「应答的那份目录构建」，与资产类型无关；\n"
        "    形态在两种标签下都不变 ⇒ 该类型的日期列是常驻口径（全空=不发，散值=部分实体不发）。\n"
        "    对下游的要求不同：前者不许用空值覆盖已有日期，也不是「字段不可用」的证据；\n"
        "    后者才可以按资产类型写进判据。判据因此是按 (类型, 形态) 建的：\n"
        "    某类翻出它自己没出现过的形态才是新变化（futures 常驻散值，a 股散值就是事故）。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
