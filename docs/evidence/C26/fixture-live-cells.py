"""Where the C26 test fixtures' cells come from: the live sina pages.

The 配股 bug survived a green unit test because that test's fixture was not
the shape upstream sends: it hand-wrote ``配股方案 = "10配3"`` while the page
coerces the column to ``float64`` (``1.5``). A fixture that disagrees with
production cannot catch a production bug, so this script records the cells the
new fixtures copy, verbatim and with their Python types, and shows what the
real ``transform_data`` decides about each row.

Read-only: it fetches public pages and prints them. No warehouse write, no
credential read.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pandas as pd

from opendata.data.protocol import FetchContext
from opendata.data.providers import register_providers
from opendata.data.registry import get_registry

if TYPE_CHECKING:
    from opendata.data.protocol import Fetcher

#: symbol, indicator, and the 公告日期 prefixes whose rows the fixtures copy.
CAPTURES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("601318", "分红", ("2018-04", "2018-05", "2018-08")),
    ("600519", "分红", ("2024", "2023")),
    ("600030", "配股", ()),
)


def _fetcher(indicator: str) -> Fetcher:
    """Resolve the real akshare corporate-action leg.

    Args:
        indicator: Kept for symmetry with the callers; both pages come from
            one fetcher.

    Returns:
        The registered fetcher.
    """
    register_providers()
    resolved = get_registry().resolve(
        asset_class="equity", domain="stock_action", period="1D", market="cn", source="akshare"
    )
    if resolved is None:  # a missing registration is this script's own bug
        raise RuntimeError("akshare stock_action leg is not registered")
    return resolved


def _cells(frame: pd.DataFrame) -> str:
    """Render each row as ``column=(python type, value)`` pairs.

    Args:
        frame: Rows to render.

    Returns:
        One line per row, joined with newlines.
    """
    lines: list[str] = []
    for record in frame.to_dict("records"):
        pairs = ", ".join(
            f"{key}=({type(value).__name__}, {value!r})" for key, value in record.items()
        )
        lines.append(f"    {pairs}")
    return "\n".join(lines)


def main() -> int:
    """Print the live cells and the adapter's verdict per row.

    Returns:
        0, always: this is a capture, not a judge.
    """
    print(
        f"===== C26 夹具来源：新浪页真机逐格读数（适配层判定的就是这些单元格）=====\n"
        f"python={sys.version.split()[0]} pandas={pd.__version__} 只读，不写数仓，不读密钥"
    )
    for symbol, indicator, prefixes in CAPTURES:
        fetcher = _fetcher(indicator)
        params = fetcher.transform_query(symbol=symbol)
        raw = fetcher.extract_data(params, FetchContext())
        page = raw[raw["indicator"] == indicator]
        print(f"\n===== {symbol} {indicator}页 =====")
        print(f"  列={list(page.columns)}")
        print(f"  dtype={ {str(c): str(t) for c, t in page.dtypes.items()} }")
        dates = page["公告日期"].astype(str)
        picked = page[dates.str.startswith(prefixes)] if prefixes else page
        print(f"  夹具复制的行（公告日期前缀={prefixes or '全部'}）共 {len(picked)} 行：")
        print(_cells(picked))
        actions = list(fetcher.transform_data(page.copy(), params))
        dated = {action.ex_date: action for action in actions}
        print(f"  适配层在整页 {len(page)} 行上返回 {len(actions)} 条：")
        for ex_date in sorted(dated):
            action = dated[ex_date]
            print(
                f"    {ex_date} cash={action.cash_dividend} stock={action.stock_dividend} "
                f"rights_shares={action.rights_shares} rights_price={action.rights_price}"
            )
        for record in page.to_dict("records"):
            key = "除权除息日" if indicator == "分红" else "除权日"
            value = record.get(key)
            if value is None or pd.isna(value):
                print(
                    f"    [丢弃] 公告 {record.get('公告日期')} 进度={record.get('进度')}"
                    f" {key}={value!r}（{type(value).__name__}）"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
