"""C18 决策实据：指数目录的「展示码 ≠ thscode」到底是哪几类行（只读，不碰数仓）.

C16 把「ths 指数腿裸码解析必抛 ``ticker_code_mismatch``」登记为缺陷但没修，因为修
法取决于一个当时还没量出来的事实：**哪种不等算合法**。旧代码（``991001.TI`` ↔
``1C0003``）和币种/R 份额变体（``970006.SZ`` ↔ ``988006``）看着是两类东西，放宽规
则还是只丢旧行要拿数据定。本脚本把决策需要的四项量出来：

1. 各资产类型目录里「去后缀 ≠ ticker」的行数——判据在别的目录里是否还成立（成立就
   不能整体放宽）；
2. 指数目录这些行的**形态分类**，以及按名字标签能不能解释（解释不掉的那一桶就是
   「没有取值规则」的证据）；
3. ``thscode`` 前缀的重数——裸码 ``000300`` 在指数全集里到底是不是唯一（``patrol.py``
   注释里那句「bare code resolves to several index listings」是没验过的假设）；
4. 探针要用的几个裸码在目录里的候选，以及 `ticker` 是否会与别的行的前缀相撞（放宽
   会不会引入误解析——解析只匹配 ``thscode``，所以这里量的是「匹配面会不会变宽」）。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C18/index-code-rule-audit.py
"""

from __future__ import annotations

import collections
from typing import TYPE_CHECKING

from opendata.data.providers.ths.models._client import client
from opendata_fuyao import endpoints as e

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opendata_fuyao import FuyaoHttpClient

INDEX_ASSET_TYPE = "a-share-index"

#: 目录里逐个资产类型各取一页，用来对照「代码对账」在谁身上还成立。
CATALOGS = ("a-share", INDEX_ASSET_TYPE, "fund-etf", "futures", "options")

#: 裸码解析要消费的候选（探针码 + 一个已知的中证码 + 一个旧代码样本）。
PROBE_CODES = ("000300", "000905", "000001", "932000", "399001", "886042", "991001")

#: 上游用来标记「这不是当前主系列」的名字片段（实测见分类结果）。
NAME_TAGS = ("旧", "港币", "美元", "CNH", "R", "离岸")
SAMPLES = 5


def catalog_rows(active: FuyaoHttpClient, asset_type: str) -> list[Mapping[str, object]]:
    """Pull one page of one asset class' catalog straight from the envelope."""
    response = active.get(
        e.TICKERS_LIST_ENDPOINT,
        params=e.build_tickers_list_request(
            limit=e.MAX_LIST_LIMIT, offset=0, asset_type=asset_type
        ),
    )
    return list(e._items(response.envelope, context="ticker"))


def prefix_of(symbol: object) -> str:
    """Plain part of a ``thscode``, without the exchange suffix."""
    return str(symbol).strip().upper().rpartition(".")[0]


def is_unequal(row: Mapping[str, object]) -> bool:
    """Are the two code spellings unequal? No exemption here, raw fact only."""
    plain = row.get("ticker")
    if not isinstance(plain, str) or not plain:
        return False
    return prefix_of(row.get("thscode")) != plain.strip().upper()


def classify(row: Mapping[str, object], prefixes: set[str]) -> str:
    """Bucket one unequal row by shape, to see whether a value rule exists."""
    plain = str(row.get("ticker")).strip().upper()
    if not plain.isdigit():
        return "展示码含非数字（旧代码一类）"
    if plain in prefixes:
        return "展示码等于别的行的 thscode 前缀"
    if any(tag in str(row.get("name") or "") for tag in NAME_TAGS):
        return "纯数字变体，名字带币种/份额标记"
    return "纯数字变体，名字无解释"


def report_catalogs(tables: dict[str, list[Mapping[str, object]]]) -> None:
    """Section 1: how many rows break reconciliation in each catalog."""
    print("[1] 各目录「thscode 去后缀 ≠ ticker」行数（不含豁免，原始事实）")
    for asset_type in CATALOGS:
        rows = tables[asset_type]
        unequal = [row for row in rows if is_unequal(row)]
        missing = [row for row in rows if not str(row.get("ticker") or "")]
        note = "（超单页上限，只量第一页）" if len(rows) >= e.MAX_LIST_LIMIT else ""
        print(
            f"  {asset_type:<22} 行数 {len(rows):>6} 不等 {len(unequal):>5} "
            f"ticker 缺失 {len(missing):>5} {note}"
        )


def report_index_classes(rows: list[Mapping[str, object]]) -> None:
    """Section 2: the shape of the index mismatches, and the unexplained residue."""
    prefixes = {prefix_of(row.get("thscode")) for row in rows}
    unequal = [row for row in rows if is_unequal(row)]
    print(f"\n[2] 指数目录 {len(unequal)} 行不等的形态分类")
    buckets = collections.Counter(classify(row, prefixes) for row in unequal)
    for kind, count in buckets.most_common():
        print(f"  {kind:<34} {count:>4} 行")
    for kind in buckets:
        picked = [row for row in unequal if classify(row, prefixes) == kind][:SAMPLES]
        print(f"  ── {kind} 样例：")
        for row in picked:
            print(
                f"     thscode={row.get('thscode'):<14} ticker={str(row.get('ticker')):<10} "
                f"name={row.get('name')}"
            )


def report_prefix_multiplicity(rows: list[Mapping[str, object]]) -> None:
    """Section 3: whether a bare index code is actually ambiguous upstream."""
    by_prefix: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        symbol = str(row.get("thscode")).strip().upper()
        by_prefix[prefix_of(symbol)].append(f"{symbol}({row.get('name')})")
    duplicated = {code: names for code, names in by_prefix.items() if len(names) > 1}
    print(f"\n[3] 前缀重数：{len(by_prefix)} 个不同前缀，其中 {len(duplicated)} 个有多行")
    for code in sorted(duplicated)[:SAMPLES]:
        print(f"  {code} -> " + " / ".join(duplicated[code]))


def report_probe_codes(rows: list[Mapping[str, object]]) -> None:
    """Section 4: what each bare code the probes would use resolves to."""
    matches: dict[str, list[str]] = collections.defaultdict(list)
    for row in rows:
        symbol = str(row.get("thscode")).strip().upper()
        matches[prefix_of(symbol)].append(symbol)
    print("\n[4] 裸码候选（解析只按 thscode 前缀匹配，与 ticker 无关）")
    for code in PROBE_CODES:
        found = sorted(set(matches.get(code, [])))
        if not found:
            verdict = "目录里没有 ⇒ 解析必失败"
        elif len(found) == 1:
            verdict = "唯一 ⇒ 可解析"
        else:
            verdict = f"歧义（{len(found)} 行）⇒ 失败关闭"
        print(f"  {code:<8} -> {', '.join(found) or '-':<28} {verdict}")


def main() -> int:
    """Print the four measurements the C18 rule decision needs."""
    tables: dict[str, list[Mapping[str, object]]] = {}
    with client(timeout_seconds=60) as active:
        for asset_type in CATALOGS:
            tables[asset_type] = catalog_rows(active, asset_type)
    report_catalogs(tables)
    report_index_classes(tables[INDEX_ASSET_TYPE])
    report_prefix_multiplicity(tables[INDEX_ASSET_TYPE])
    report_probe_codes(tables[INDEX_ASSET_TYPE])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
