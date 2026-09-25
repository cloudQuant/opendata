"""C19 probe: what exactly hollowed out the ths catalog date columns.

C18 measured that ``list_date`` went from 9 missing / 5,578 (C14 archive) to
5,578 missing / 5,578 for A shares, while futures and indices still carried
dates in the very same call. That single number cannot tell which of the two
competing explanations holds, and they imply opposite follow-ups, so this
script measures the three faces that can separate them:

1. **Row shape.** Is the key absent from the payload (upstream changed its
   contract / column set) or present with ``null``/``""`` (the field exists but
   today's build did not fill it)? Only the second one can be a timing effect.
2. **Snapshot instant.** Two samples a few seconds apart: if ``data.timestamp``
   does not move, the catalog is a batch snapshot rather than a live read, so
   "wait and it fills in" is testable only across batch boundaries, not during
   the morning.
3. **The other endpoint.** ``/api/meta/tickers/search`` and
   ``/api/meta/tickers/list`` are separate upstream chains. If search still
   publishes ``list_date`` while the list page does not, the loss is scoped to
   the list ETL, not to the field itself.

Read-only against the live endpoint; no warehouse and no credentials printed.

Run (stdout redirected to the sibling ``.txt``)::

    python docs/evidence/C19/catalog-date-field-shape.py --samples 2 --interval 45
"""

from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from opendata.data.providers.ths.models._client import client
from opendata_fuyao import endpoints as e

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from opendata_fuyao import FuyaoHttpClient

#: Asset types measured side by side; futures/indices are the controls that
#: show the C18 row-count criteria are not themselves broken.
ASSET_TYPES = ("a-share", "fund-etf", "a-share-index", "futures")

#: Date columns the ``Instrument`` contract consumes (plus ``end_date``, which
#: feeds the delisting fallback in ``normalize_instruments``).
DATE_FIELDS = ("list_date", "last_trade_date", "end_date")

#: Probe instrument for the search route; a heavyweight listed name whose
#: ``list_date`` C14 archived as 2001-08-27.
SEARCH_QUERY = "600519"

SHANGHAI = ZoneInfo("Asia/Shanghai")


def instant(millis: int | None) -> str:
    """Render the envelope snapshot instant in Shanghai time."""
    if millis is None:
        return "无"
    return datetime.fromtimestamp(millis // 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z")


def classify(rows: Sequence[Mapping[str, object]], field: str) -> str:
    """Summarise one date column across rows by occurrence shape."""
    absent = sum(1 for row in rows if field not in row)
    null = sum(1 for row in rows if row.get(field) is None)
    blank = sum(1 for row in rows if row.get(field) == "")
    valued = sum(1 for row in rows if row.get(field) not in (None, ""))
    return f"缺键 {absent:>5} | null {null:>5} | 空串 {blank:>5} | 有值 {valued:>5}"


def probe_list(active: FuyaoHttpClient) -> None:
    """Print the row shape and date-column shape of every asset type's page."""
    print("[1] /api/meta/tickers/list 逐资产类型：行形 + 日期列形态")
    for asset_type in ASSET_TYPES:
        response = active.get(
            e.TICKERS_LIST_ENDPOINT,
            params=e.build_tickers_list_request(
                limit=e.MAX_LIST_LIMIT, offset=0, asset_type=asset_type
            ),
        )
        rows = list(e._items(response.envelope, context="ticker"))
        keys = sorted({key for row in rows for key in row})
        stamp = instant(response.envelope.data_timestamp_ms)
        print(f"  {asset_type:<14} 行数 {len(rows):>5} 快照 {stamp}")
        print(f"    键集合: {keys}")
        for field in DATE_FIELDS:
            print(f"    {field:<16} {classify(rows, field)}")


def probe_search(active: FuyaoHttpClient) -> None:
    """Print what the search route publishes for the same instruments."""
    print("\n[2] /api/meta/tickers/search 同一批标的的日期列（上游是另一条链路）")
    response = active.get(
        e.TICKERS_SEARCH_ENDPOINT,
        params=e.build_tickers_search_request(query=SEARCH_QUERY, limit=5),
    )
    rows = list(e._items(response.envelope, context="ticker"))
    print(f"  命中 {len(rows)} 行 快照 {instant(response.envelope.data_timestamp_ms)}")
    for row in rows:
        dates = "  ".join(f"{field}={row.get(field)!r}" for field in DATE_FIELDS)
        print(f"    thscode={row.get('thscode')!r} ticker={row.get('ticker')!r} {dates}")


def envelope_fields(active: FuyaoHttpClient) -> None:
    """Print the non-row fields of ``data`` - a build stamp would settle timing."""
    print("\n[3] 信封 ``data`` 除 item 外的字段（有没有构建时间戳可依赖）")
    response = active.get(
        e.TICKERS_LIST_ENDPOINT,
        params=e.build_tickers_list_request(limit=1, offset=0, asset_type="a-share"),
    )
    extras = {key: value for key, value in response.envelope.data.items() if key != "item"}
    print(f"  data 非行字段: {extras}")
    print(f"  request_id: {response.envelope.request_id}")


def main() -> int:
    """Sample the live catalog once per round and label each face."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--samples", type=int, default=1, help="How many rounds to sample.")
    parser.add_argument("--interval", type=float, default=0.0, help="Seconds between rounds.")
    args = parser.parse_args()

    for sample in range(1, args.samples + 1):
        print(
            f"\n{'#' * 12} 采样 {sample}/{args.samples} "
            f"@ {datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S %Z')} {'#' * 12}"
        )
        with client(timeout_seconds=60) as active:
            probe_list(active)
            probe_search(active)
            envelope_fields(active)
        if sample < args.samples and args.interval > 0:
            time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
