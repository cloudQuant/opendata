"""C16 附带发现的证据脚本：指数腿的**裸码解析路径已经不可用**（只读，不碰数仓）.

写探针参数时发现：``ths/index_daily``、``ths/index_constituent`` 用裸码 ``000300``
调用抛 ``FUYAO_ENVELOPE_INVALID_ticker_code_mismatch``，换成带后缀的 ``000300.SH``
即正常。本脚本把根因量出来：``resolve_index_code`` 走 ``_resolve_by_listing`` ⇒
``list_instruments(asset_type="a-share-index")`` ⇒ ``normalize_instruments`` 对每一行
要求 ``thscode`` 与 ``ticker`` 对得上（``_codes_agree``），而指数目录里存在大量
「展示码 ≠ thscode」的行（港币/美元计价变体、旧代码），**一行不合就整表拒绝**，
于是该资产类型的目录列出必抛，裸码解析永远拿不到候选。

运行（输出重定向到同名 ``.txt``）::

    python docs/evidence/C16/index-bare-code-defect.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from opendata.data.providers.ths.models._client import client, resolve_index_code
from opendata_fuyao import endpoints as e
from opendata_fuyao.errors import FuyaoError

if TYPE_CHECKING:
    from collections.abc import Mapping

INDEX_ASSET_TYPE = "a-share-index"
BARE_CODES = ("000300", "000905", "932000")
EXAMPLES = 6


def _mismatches(rows: list[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Pick the catalog rows whose ``ticker`` disagrees with ``thscode``."""
    return [
        row
        for row in rows
        if not e._codes_agree(  # 复现 normalize_instruments 的判定
            str(row.get("thscode")), row.get("ticker"), str(row.get("asset_type") or "")
        )
    ]


def _resolve_one(code: str) -> str:
    """Resolve one bare index code and render what the caller would see."""
    try:
        with client(timeout_seconds=30) as active:
            return f"  {code} -> {resolve_index_code(active, code)}"
    except FuyaoError as exc:
        return f"  {code} -> 抛 {type(exc).__name__}: {exc}"


def main() -> int:
    """Report the index catalog shape and the bare-code resolution outcome."""
    with client(timeout_seconds=60) as active:
        response = active.get(
            e.TICKERS_LIST_ENDPOINT,
            params=e.build_tickers_list_request(limit=10000, offset=0, asset_type=INDEX_ASSET_TYPE),
        )
        rows = list(e._items(response.envelope, context="ticker"))
        bad = _mismatches(rows)
        print(f"指数目录（asset_type={INDEX_ASSET_TYPE}）单次返回 {len(rows)} 行，")
        print(f"其中 ticker 与 thscode 不一致 {len(bad)} 行 → normalize_instruments 整表拒绝")
        print(f"示例（前 {EXAMPLES} 行）：")
        for row in bad[:EXAMPLES]:
            print(
                f"  thscode={row.get('thscode')} ticker={row.get('ticker')} "
                f"asset_type={row.get('asset_type')} name={row.get('name')}"
            )
    print("\n裸码解析实测：")
    for code in BARE_CODES:
        print(_resolve_one(code))
    print("\n对照：带后缀的 000300.SH 在 index_daily / index_constituent 两条腿上均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
