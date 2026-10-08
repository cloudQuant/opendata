r"""C25 判据底稿：巡检的字段级 canary 只能钉「实测过的形态」，本脚本把形态量出来.

AC-19 三轮登记的缺口（C18 提出、C19 设计、C20/C21 未取）：`instrument` 探针只数行数，
5,578 行里 `list_date` 全空它也打 `[ok]`。要补成判据，就得先回答「什么形态算异常」——
而 C19 已经量到 a 股目录本身在两份构建之间翻（36 次读：满值 14 次、全空 22 次，且 36/36
随信封毫秒标签走）。**把翻动的一面钉成告警等于天天报假故障**，所以能钉进
`FIELD_CANARIES` 的只有「从未被观测到的形态」。

本脚本因此不是「跑一次看结果」，而是把候选面逐资产类型 × 逐字段重复读 N 次，产出每个
(资产类型, 字段) 的**形态分布**与缺失数取值范围，再分成「只出现过一种形态」（可钉候选：
另一种形态即新信息）与「多形态」（不可钉，否则报假故障）。分类与缺失判定都走生产代码
`patrol.evaluate_canary` ⇒ 归档里的形态字面与巡检日报同源。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C25/field-canary-measure.py
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

from opendata.data.providers import register_providers
from opendata.data.registry import get_registry
from opendata.pipeline.patrol import SHAPE_NO_READING, FieldCanary, evaluate_canary

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any

    from opendata.data.models import ContractModel
    from opendata.data.protocol import Fetcher

#: 逐类量的资产类型：两类常驻缺口（fund-etf 全空 / futures 散值）、两类满值基线。
ASSET_TYPES = ("a-share", "a-share-index", "futures", "fund-etf")

#: 被量的字段：契约 ``Instrument`` 的全部列。身份列（symbol/exchange/name/status/currency）
#: 是「塌了就是事故」的候选；日期列（list_date/delist_date）是 C18/C19 现场的那两列。
FIELDS = ("symbol", "exchange", "name", "status", "currency", "list_date", "delist_date", "board")

#: 每个资产类型重复读的次数。a 股的翻动是**逐次请求**级别（C19 36 次里 14 次满值），
#: 次数必须足够大到两类构建都被读到，否则「稳定」这个结论没有依据。
READS_PER_TYPE = 10

#: C19 归档里同一列的对照读数（``catalog-flip-rate-all-types.txt``）。
C19_BASELINE = (
    "a-share 满值 5570/5578 ↔ 全空（随标签翻）；fund-etf 24/24 全空；"
    "a-share-index 24/24 满值；futures 恒定 877/1142"
)

#: 读数三元组：(形态, 行数, 缺失数)。后两列为 None 当且仅当形态是 no-reading。
Reading = tuple[str, int | None, int | None]


def measure(fetcher: Fetcher[Any, Any]) -> dict[tuple[str, str], list[Reading]]:
    """Collect ``(shape, rows, missing)`` for every field of every asset type.

    Args:
        fetcher: The production instrument-catalog fetcher.

    Returns:
        Readings keyed by ``(asset_type, field)`` in read order.
    """
    observed: dict[tuple[str, str], list[Reading]] = {}
    for asset_type in ASSET_TYPES:
        print(f"\n== {asset_type} ==")
        for read in range(1, READS_PER_TYPE + 1):
            try:
                rows = cast("Sequence[ContractModel]", fetcher.fetch(asset_type=asset_type))
            except Exception as exc:  # 上游瞬态：记 no-reading，不记成字段塌陷
                for field in FIELDS:
                    observed.setdefault((asset_type, field), []).append((SHAPE_NO_READING, 0, 0))
                print(f"  read {read:>2} 读取失败 {type(exc).__name__}: {exc}")
                continue
            line = f"  read {read:>2} 行数 {len(rows):>5}"
            for field in FIELDS:
                reading = evaluate_canary(
                    FieldCanary(
                        asset_type=asset_type,
                        field=field,
                        allowed=frozenset(),
                        reason="measure only",
                    ),
                    rows,
                )
                observed.setdefault((asset_type, field), []).append(
                    (reading.shape, reading.rows, reading.missing)
                )
                line += f" {field[:4]}={reading.shape[0]}"
            print(line)
    return observed


def summarize(observed: dict[tuple[str, str], list[Reading]]) -> tuple[list[str], list[str]]:
    """Print the shape distribution and split the columns into钉得住/钉不住.

    Args:
        observed: Readings keyed by ``(asset_type, field)``.

    Returns:
        ``(single_shape, mixed_shape)`` column labels, for the exit-code check.
    """
    print("\n[1] 形态分布（判据底稿）")
    stable: list[str] = []
    mixed: list[str] = []
    for (asset_type, field), readings in observed.items():
        shapes = Counter(shape for shape, _, _ in readings)
        measured = [
            missing
            for shape, _, missing in readings
            if shape != SHAPE_NO_READING and missing is not None
        ]
        sizes = sorted({rows for _, rows, _ in readings if rows is not None and rows > 0})
        detail = " ".join(f"{shape}×{count}" for shape, count in sorted(shapes.items()))
        print(
            f"  {asset_type:<15} {field:<12} 形态 {detail:<36} "
            f"缺失 {min(measured) if measured else '-'}..{max(measured) if measured else '-'} "
            f"行数 {sizes[0] if sizes else '-'}..{sizes[-1] if sizes else '-'}"
        )
        label = f"{asset_type}/{field}"
        if len(shapes) == 1 and SHAPE_NO_READING not in shapes:
            stable.append(f"{label}: {next(iter(shapes))}")
        elif len(shapes) > 1:
            mixed.append(f"{label}: {sorted(shapes)}")

    print("\n[2] 只出现过一种形态的列（可钉候选；出现另一种形态即为新信息）")
    for line in stable:
        print(f"  {line}")
    print("\n[3] 多形态列（不可钉为单一形态，否则报假故障）")
    for line in mixed:
        print(f"  {line}")
    return stable, mixed


def main() -> int:
    """Measure the candidate canary faces the patrol may pin.

    Returns:
        0 when every asset type answered at least once; 1 otherwise, because
        a column that could not be measured cannot seed a judgement.
    """
    register_providers()
    fetcher = get_registry().resolve("metadata", "instrument", source="ths")
    print(f"[0] 本地判定时刻 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print(f"    C19 基线：{C19_BASELINE}")
    print(f"    每类重复读 {READS_PER_TYPE} 次，字段 {', '.join(FIELDS)}")
    observed = measure(fetcher)
    stable, mixed = summarize(observed)
    no_reading = sum(
        1 for readings in observed.values() for shape, _, _ in readings if shape == SHAPE_NO_READING
    )
    print(f"\n[4] 汇总：可钉 {len(stable)} 列 / 多形态 {len(mixed)} 列 / 未判读 {no_reading} 次")
    print(
        "[5] 判据落法：可钉候选里只取「上游语义上塌了就是事故」的列进 "
        "patrol.FIELD_CANARIES；\n    多形态列留在文档里说明为什么不钉（见 README §3）。"
    )
    unreadable: list[str] = []
    shapes_by_asset: dict[str, set[str]] = {}
    for (asset_type, _), readings in observed.items():
        shapes_by_asset.setdefault(asset_type, set()).update(shape for shape, _, _ in readings)
    for asset in ASSET_TYPES:
        if shapes_by_asset.get(asset, set()) in (set(), {SHAPE_NO_READING}):
            unreadable.append(asset)
            print(f"    警告：{asset} 全程读不到")
    return 1 if unreadable else 0


if __name__ == "__main__":
    raise SystemExit(main())
