"""``tests/fuyao_golden.py`` 的毫秒戳表是怎么来的：三条独立推导 + 线上锚点全量复核.

黄金向量必须是**外部事实**，不能是被测实现的镜像；所以这里用三条互不相干的算法
把每个交易日重算一遍，再把录制档案里**所有**真正上过线的 ``start``/``end`` 参数
当第四来源核对（不只取一条，避免挑对自己有利的那个用例）：

1. 纯整数历法算术：``距 1970-01-01 的天数 × 86400000 − 8 小时的毫秒数``
   （上海相对 UTC 恒为 +8，且 1991 年后不再用夏令时，所以这个偏移是常数）；
2. 固定偏移 ``timezone(timedelta(hours=8))``：走 datetime 库但不碰 tzdata；
3. ``zoneinfo.ZoneInfo("Asia/Shanghai")``：走 tzdata，与 1/2 无共享中间量。

线上锚点取录制件每条记录的 ``start``（应为某日零点）与 ``end + 1ms``（``end`` 是不含
上界的最后时刻，+1ms 即次日零点）。任一来源与表里的值不一致、或录制参数本身不落在
零点上，都非零退出。生产实现 ``opendata.data.providers.ths.endpoints.shanghai_midnight_millis``
（C66 之前名为 ``opendata_fuyao.endpoints``，同一函数搬家）
只做信息性对照：它若与表不一致，说明实现漂移（该红的是用例，不是这张表）。

用法::

    python docs/evidence/C44/golden_millis.py
"""

from __future__ import annotations

import gzip
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from tests.fuyao_golden import MILLIS_BY_DAY, WIRE_ANCHOR_MILLIS  # noqa: E402

#: 上海自 1991 年废除夏令时后恒为 UTC+8。
SHANGHAI_OFFSET_MILLIS: Final = 8 * 3600 * 1000
EPOCH: Final = date(1970, 1, 1)
FIXED_OFFSET: Final = timezone(timedelta(hours=8))
SHANGHAI: Final = ZoneInfo("Asia/Shanghai")
#: 录制档案：每条带 ``start``/``end`` 的记录都是一个线上锚点。
ARCHIVE: Final = (
    REPO_ROOT / "tests" / "fixtures" / "upstream" / "fuyao_t1_envelopes" / "responses.json.gz"
)


def calendar_millis(day: date) -> int:
    """推导①：纯整数历法算术，不用任何时区库."""
    return (day - EPOCH).days * 86_400_000 - SHANGHAI_OFFSET_MILLIS


def fixed_offset_millis(day: date) -> int:
    """推导②：``timezone(timedelta(hours=8))``，依赖 datetime 而不依赖 tzdata."""
    midnight = datetime.combine(day, time.min, tzinfo=FIXED_OFFSET)
    return int(midnight.timestamp() * 1000)


def zoneinfo_millis(day: date) -> int:
    """推导③：``zoneinfo`` 查表换算（与 ①② 无共享中间量）."""
    midnight = datetime.combine(day, time.min, tzinfo=SHANGHAI)
    return int(midnight.astimezone(timezone.utc).timestamp() * 1000)


def derivations(day: date) -> tuple[int, int, int]:
    """同一天在三条独立推导下的结果."""
    return calendar_millis(day), fixed_offset_millis(day), zoneinfo_millis(day)


def recorded_wire_anchors() -> list[tuple[str, str, date, int]]:
    """录制件里全部线上锚点：``(用例, start|end+1ms, 该瞬时对应的上海日, 毫秒戳)``."""
    anchors: list[tuple[str, str, date, int]] = []
    with gzip.open(ARCHIVE) as handle:
        entries = json.loads(handle.read())
    for entry in entries:
        params = entry.get("params") or {}
        if "start" not in params or "end" not in params:
            continue
        for kind, millis in (("start", int(params["start"])), ("end+1ms", int(params["end"]) + 1)):
            instant = datetime.fromtimestamp(millis / 1000, SHANGHAI)
            anchors.append((str(entry["name"]), kind, instant.date(), millis))
    return anchors


def production_millis(day: date) -> int:
    """信息性对照：被测实现算出来的同一个零点."""
    from opendata.data.providers.ths.endpoints import shanghai_midnight_millis

    return shanghai_midnight_millis(day)


def main() -> int:
    """Re-derive every pinned day and exit non-zero on any disagreement."""
    anchors = recorded_wire_anchors()
    #: 同一日在录制件里出现多次时取最后一次（下面的循环会逐条校验，不依赖这里）。
    wire: dict[str, int] = {day.isoformat(): millis for _, _, day, millis in anchors}
    problems: list[str] = []
    production_drift: list[str] = []

    print(f"days_pinned = {len(MILLIS_BY_DAY)} + wire_anchors = {len(WIRE_ANCHOR_MILLIS)}")
    print(f"recorded_records_with_window = {len(anchors) // 2}")
    for case, kind, day, millis in anchors:
        first, second, third = derivations(day)
        # 录制参数声称是「上海零点」：这本身是个可反证的事实，不能只跟推导比。
        aligned_to_midnight = datetime.fromtimestamp(millis / 1000, SHANGHAI).time() == time.min
        if first != millis or second != millis or third != millis or not aligned_to_midnight:
            problems.append(
                f"录制锚点 {case}/{kind}={millis} 三条推导={first}/{second}/{third}"
                f"（上海零点成立={aligned_to_midnight}）"
            )

    for text, pinned in sorted(MILLIS_BY_DAY.items()):
        day = date.fromisoformat(text)
        first, second, third = derivations(day)
        if first != pinned or second != pinned or third != pinned:
            problems.append(f"{text}: 表={pinned} 历法={first} 固定偏移={second} zoneinfo={third}")
        recorded = wire.get(text)
        if recorded is not None and recorded != pinned:
            problems.append(f"{text}: 录制线上值={recorded} 与表={pinned} 不一致")

    for text, pinned in sorted(WIRE_ANCHOR_MILLIS.items()):
        day = date.fromisoformat(text)
        first, second, third = derivations(day)
        if first != pinned or second != pinned or third != pinned:
            problems.append(
                f"{text}: 锚点={pinned} 历法={first} 固定偏移={second} zoneinfo={third}"
            )
        recorded = wire.get(text)
        if recorded is None:
            problems.append(f"{text}: 声明是录制锚点，但档案里查不到这个日的线上参数")
        elif recorded != pinned:
            problems.append(f"{text}: 录制线上值={recorded} 与锚点={pinned} 不一致")

    for text in sorted({day.isoformat() for _, _, day, _ in anchors}):
        derived = production_millis(date.fromisoformat(text))
        if derived != wire[text]:
            production_drift.append(f"{text}: 生产实现={derived} 录制={wire[text]}")

    # 1991 后无夏令时：任何一天的偏移都必须是常数 8 小时，否则推导①的假设不成立。
    for sample in (date.fromisoformat(min(MILLIS_BY_DAY)), date.fromisoformat(max(MILLIS_BY_DAY))):
        offset = datetime.combine(sample, time.min, tzinfo=SHANGHAI).utcoffset()
        if offset != timedelta(hours=8):
            problems.append(f"{sample} 的上海偏移不再是 +8:00，而是 {offset}")

    pinned_days_hit = len([day for day in MILLIS_BY_DAY if day in wire])
    print(f"recorded_wire_days = {sorted(wire)}")
    for line in problems:
        print(f"PROBLEM {line}")
    for line in production_drift:
        print(f"NOTE(production-drift) {line}")
    print(
        f"VERDICT derivations=3 pinned={len(MILLIS_BY_DAY)} "
        f"wire_anchors={len(anchors)} wire_pinned_days={pinned_days_hit} "
        f"problems={len(problems)} exit={'1' if problems else '0'}"
    )
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
