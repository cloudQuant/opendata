"""C18 巡检副产物：ths 标的目录的 ``list_date`` 在 2026-09-25 快照里对 a 股/ETF 全空.

跑本轮真机用例时 ``test_registry_routes_the_instrument_catalog_to_the_fuyao_source``
挂了（C14 归档的判据是「整表仅 9 行 ``list_date`` 为空」，实测现在是 5,578 行全空）。
本脚本把这件事量成可重放的面：同一时刻逐资产类型取 ``list_date``/到期日的覆盖率，
并把信封快照时刻一并打出来——期货在同一次调用里照常发日期，所以这不是「整个目录
还没回填」那种全局延迟，而是 a 股/ETF 这两类的字段级缺失。

判不了是上游改了口径还是日内加载时序（C14 采的是前一日 16:00 的快照，本轮是当日
09:00 的快照），两种解释对下游的同一条要求是：**``list_date`` 不能在任意时刻当作
可用字段**。巡检现在只判「有没有行」，看不见这种字段级塌方 ⇒ 登记为 C19。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C18/instrument-list-date-canary.py
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from opendata.data.providers.ths.models._client import client
from opendata_fuyao import endpoints as e

if TYPE_CHECKING:
    from opendata_fuyao import FuyaoHttpClient

#: 逐类量的资产类型（含两类已知不发到期日的，用来对照判据本身没坏）。
ASSET_TYPES = ("a-share", "fund-etf", "a-share-index", "futures")

#: 上海时区：快照时刻要按上游的加载时区读，UTC 会把 16:00 的日终加载写成 08:00。
SHANGHAI = ZoneInfo("Asia/Shanghai")

#: C14 归档的对照值（``docs/evidence/C14/instrument-cross-check.txt`` 表 A）。
C14_BASELINE = "a-share list_date 缺失 9/5578；futures 无日期行 265/1139"


def snapshot_day(millis: int | None) -> str:
    """Render the envelope snapshot instant in Shanghai time."""
    if millis is None:
        return "无"
    return datetime.fromtimestamp(millis // 1000, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z")


def report(active: FuyaoHttpClient) -> None:
    """Print per-asset-type date-field coverage from one snapshot instant."""
    print(f"[1] C14 基线：{C14_BASELINE}")
    for asset_type in ASSET_TYPES:
        response = active.get(
            e.TICKERS_LIST_ENDPOINT,
            params=e.build_tickers_list_request(
                limit=e.MAX_LIST_LIMIT, offset=0, asset_type=asset_type
            ),
        )
        rows = list(e._items(response.envelope, context="ticker"))
        listed = sum(1 for row in rows if row.get("list_date"))
        delisted = sum(1 for row in rows if row.get("last_trade_date") or row.get("end_date"))
        print(
            f"  {asset_type:<15} 行数 {len(rows):>5} list_date 非空 {listed:>5} "
            f"缺 {len(rows) - listed:>5} 到期日非空 {delisted:>5} "
            f"快照 {snapshot_day(response.envelope.data_timestamp_ms)}"
        )


def main() -> int:
    """Measure the field-level hollow-out and label it against C14."""
    print(f"[0] 本地判定时刻 {datetime.now(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S %Z')}")
    with client(timeout_seconds=60) as active:
        report(active)
    print(
        "\n[2] 判据后果：巡检的 instrument 探针只数行数（本轮实测 5,578 行 ⇒ [ok]），"
        "对字段级空值无感；\n    而 C14 把「目录 list_date == sina 首根」"
        "当作该腿 verified 的依据之一。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
