"""What a refused em call actually looks like, and what each tree does with it.

``index_daily_em`` has been ``pending`` since A2.5 with the reason
``EmptyReferenceFrame: upstream returned 0 rows`` - and the recorded note says the
pristine upstream returned zero rows on this same network too. This script pins
what "0 rows" means at the HTTP level and which of the two trees hides it.

Three reads, in this order, inside whatever window the hosts are in:

* A - raw HTTP shape per host (``push2his`` and its ``push2delay`` fallback): the
  status code and a body preview. A 5xx / connection reset is a **refusal**; an
  HTTP 200 whose envelope says ``data: null`` is an **empty answer**. The two are
  different facts and only the second can be mistaken for "this index has no
  quotes in that window".
* B - the pristine upstream ``index_zh_a_hist`` (the pinned checkout, unmodified):
  rows or exception.
* C - the ported ``index_zh_a_hist`` (production import path): exception or rows.

B and C are run on the same network state, so a difference between them is the
port's fail-closed edit showing up, not two different moments.

Read-only, no credential touched, nothing written to the warehouse.

Usage (py313 env):
    python docs/evidence/C27/refusal-shape.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UPSTREAM_CHECKOUT = Path("/Users/yunjinqi/Documents/new_projects/akshare")
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
DELAY_URL = KLINE_URL.replace("push2his", "push2delay")
#: The pending ``index_daily_em`` case's own arguments, so both trees are asked the
#: exact question whose answer is recorded as ``EmptyReferenceFrame``.
CASE_KWARGS: dict[str, str] = {
    "symbol": "000300",
    "period": "daily",
    "start_date": "20240101",
    "end_date": "20240229",
}


def _raw_shape(url: str) -> str:
    """Print one host's HTTP answer for the kline request the index call makes.

    Args:
        url: The kline endpoint to hit directly (no retry, no fallback).

    Returns:
        A one-line description: status + envelope identity, or the exception name.
    """
    import requests

    params = {
        "secid": "1.000300",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": "0",
        "beg": "0",
        "end": "20500000",
    }
    host = url.split("/")[2]
    try:
        response = requests.get(url, params=params, timeout=15)
    except requests.RequestException as exc:
        return f"{host:<24s} 拒绝 {type(exc).__name__}: {str(exc)[:80]}"
    body = response.text
    try:
        envelope = response.json()
    except ValueError:
        return f"{host:<24s} status={response.status_code} 非 JSON preview={body[:60]!r}"
    data = envelope.get("data")
    if isinstance(data, dict):
        klines = data.get("klines") or []
        kind = "有数据" if klines else "空答复(data 为对象但无 klines)"
        return (
            f"{host:<24s} status={response.status_code} {kind} code={data.get('code')!r} "
            f"name={data.get('name')!r} klines={len(klines)}"
        )
    return f"{host:<24s} status={response.status_code} 空答复 data={data!r} body[:60]={body[:60]!r}"


def _tree_call(label: str, function: Any) -> str:  # noqa: ANN401 - untyped callable from either tree
    """Run one tree's ``index_zh_a_hist`` and report rows versus exception.

    Args:
        label: Which tree is being described.
        function: That tree's ``index_zh_a_hist``.

    Returns:
        A printable line; the frames' row count or the raised exception.
    """
    started = time.time()
    try:
        frame = function(**CASE_KWARGS)
    except Exception as exc:
        took = time.time() - started
        return f"{label}: 抛错 {type(exc).__name__}: {str(exc)[:110]} ({took:.0f}s)"
    took = time.time() - started
    rows = 0 if frame is None else len(frame)
    columns = list(getattr(frame, "columns", []))[:4]
    return f"{label}: 返回 {rows} 行 列={columns} ({took:.0f}s)"


def main() -> int:
    """Read the HTTP shape, then both trees, and print the comparison.

    Returns:
        ``0`` - both readings are reports, and a refusal is a measurement of the
        network state rather than a failure of the code under test.
    """
    os.environ["AKSHARE_EASTMONEY_AUTO_CURL_INTERFACE"] = "0"
    os.environ["AKSHARE_EASTMONEY_CURL_INTERFACE"] = "0"
    print(f"采集时间 {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    print("== A. 单次 HTTP 直连（无重试、无回退），看被拒时到底是什么形状 ==")
    for url in (KLINE_URL, DELAY_URL):
        print(f"  {_raw_shape(url)}")
        time.sleep(20)
    print()
    print("== B/C. 同一网络状态下两棵树各自的 index_zh_a_hist ==")
    import opendata_http.index.index_zh_em as ported

    sys.path.insert(0, str(UPSTREAM_CHECKOUT))
    import akshare  # the pinned pristine checkout, not an installed dependency

    print(f"  upstream 模块文件：{akshare.__file__} version={akshare.__version__}")
    print(f"  {_tree_call('B pristine upstream ', akshare.index_zh_a_hist)}")
    time.sleep(20)
    print(f"  {_tree_call('C ported (生产)  ', ported.index_zh_a_hist)}")
    print(
        "判据：两棵树面对同一网络状态时的差别，就是 AC-5/C11a 那次「吞错返回空帧 → 改为区分"
        "『被拒』与『确实没数据』」的现场形状。"
    )
    print("REFUSAL_SHAPE_EXIT=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
