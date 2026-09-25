"""C18 真机复算：指数目录不再整表拒绝，裸码解析路径恢复（只读，不碰数仓）.

C16 留下的失败记录（``docs/evidence/C16/index-bare-code-defect.txt``）是
``resolve_index_code("000300")`` 一律抛 ``ticker_code_mismatch``。本轮改了
``_codes_agree`` 的适用范围（指数按 :data:`DISPLAY_CODE_ASSET_TYPES` 豁免对账），
本脚本把「恢复」这一面在同一批标的上重放一遍：解析、两条指数的取数腿、以及被
拒的那一页目录本身（主系列上证综指 ``000001.SH`` 的展示码是 ``1A0001``，它此前
是被整表拒掉的行之一）。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C18/index-bare-code-live.py
"""

from __future__ import annotations

from collections import Counter
from datetime import date
from typing import TYPE_CHECKING, cast

from opendata.data.providers import register_providers
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    resolve_index_code,
)
from opendata.data.registry import get_registry
from opendata_fuyao.errors import FuyaoError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opendata.data.models import Bar, IndexConstituent, Instrument
    from opendata_fuyao import FuyaoHttpClient

#: 裸码样本：沪深300 / 上证综指（展示码 1A0001）/ 同花顺行业码 / 中证500 / 深证成指。
BARE_CODES = ("000300", "000001", "886042", "000905", "399001")

#: 探针窗口：与 ``PROBE_PARAMS`` 同一条无休市日的工作周。
WINDOW_START = date(2024, 9, 2)
WINDOW_END = date(2024, 9, 6)

#: 目录里必须能翻出来的两个写法（此前正是这两行让整页被拒）。
CATALOG_PROOFS = ("000001.SH", "991001.TI", "970006.SZ")


def resolve_one(active: FuyaoHttpClient, code: str) -> str:
    """Resolve one bare index code and render what a caller would see."""
    try:
        return f"  {code} -> {resolve_index_code(active, code)}"
    except (ThsProviderError, FuyaoError) as exc:
        return f"  {code} -> 抛 {type(exc).__name__}: {exc}"


def resolve_all() -> None:
    """Print what each bare index code resolves to through the real catalog."""
    print("[1] 裸码解析（resolve_index_code，走 a-share-index 目录）")
    with client(timeout_seconds=60) as active:
        for code in BARE_CODES:
            print(resolve_one(active, code))


def fetch_legs() -> None:
    """Print what the two index legs return when the caller passes a bare code."""
    registry = get_registry()
    print("\n[2] 指数两条腿按裸码取数（经注册表路由）")
    # ``fetch`` is typed against the union every capability can answer with;
    # these legs answer with contract rows.
    bars = cast(
        "Sequence[Bar]",
        registry.resolve_domain("index_daily", source="ths").fetch(
            symbol="000300", start_date=WINDOW_START, end_date=WINDOW_END
        ),
    )
    dates = [row.trade_date for row in bars]
    symbols = sorted({row.symbol for row in bars})
    print(f"  index_daily: {len(bars)} 行 symbol={symbols} 首末={dates[0]}..{dates[-1]}")
    members = cast(
        "Sequence[IndexConstituent]",
        registry.resolve_domain("index_constituent", source="ths").fetch(symbol="000300"),
    )
    print(f"  index_constituent: {len(members)} 行 首个成员={members[0].symbol}")


def catalog_page() -> None:
    """Print the a-share-index catalog that used to be rejected wholesale."""
    rows = cast(
        "Sequence[Instrument]",
        get_registry().resolve_domain("instrument", source="ths").fetch(asset_type="a-share-index"),
    )
    symbols = {row.symbol for row in rows}
    print(f"\n[3] a-share-index 目录：{len(rows)} 行（此前该页整表抛 ticker_code_mismatch）")
    for proof in CATALOG_PROOFS:
        print(f"  {proof} 在目录内: {proof in symbols}")
    suffixes = Counter(row.symbol.rpartition(".")[2] for row in rows)
    print(f"  后缀分布: {dict(sorted(suffixes.items()))}")


def main() -> int:
    """Replay the C16 failure face on the live endpoint after the rule change."""
    register_providers()
    resolve_all()
    fetch_legs()
    catalog_page()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
