"""Why four AC-6 cases are still pending, and what the ported index path returns (task #14).

AC-6 replays a recorded upstream transcript into the ported tree and asserts both
trees compute the same frame. That proves the *computation* survived the port; it
cannot prove the frame is about the security that was asked for, because both trees
read the same transcript. Four cases have been sitting on ``pending`` since A2.5
with reasons that read like network trouble (``stock_daily_raw`` / ``stock_daily_qfq``
``ConnectionError``, ``fund_etf_daily_em`` ``request failed``, and ``index_daily_em``
with the odd one out: ``EmptyReferenceFrame: upstream returned 0 rows``).

This script measures what is actually on the wire before any fixture is re-recorded.

* A - 标的身份：``index_zh_a_hist`` 的 4 个 secid 候选逐个问「你是谁」，把 envelope 里的
  ``code`` / ``name`` / klines 行数与日期范围打出来。候选是按顺序试、**第一个有任何
  klines 的就采纳**，所以这一步决定「拿错标的」是不是一个真 reachable 的状态。
* B - 生产路径实跑：给 ``request_eastmoney`` 装一个只读 spy，跑真实的
  ``index_zh_a_hist(symbol="000300")``，记录哪个 secid 被采纳、envelope 说的是哪个标的、
  返回多少行。同一请求重复跑，量化「同样的参数两次结果不同」到底多常见。
* C - 离线同一性参照：``index_daily_sina`` 夹具是 sina 通道录的同一个指数（sh000300），
  拿它的收盘价当 oracle，判断 em 侧返回的是不是同一个标的（不依赖网络）。
* D - ETF 侧同一条判据：``fund_etf_hist_em`` 在 map 未命中时先猜 market 1 再猜 0，
  与 A/B 同形，逐个候选问身份。

Read-only: every call is a GET against a public quote host, nothing is written to the
warehouse, no credential is read or printed. Throttling is handled by sleeping
between calls, and a refused call is reported as ``未判读`` rather than as a finding.

Usage (py313 env):
    python docs/evidence/C27/em-channel-measure.py
"""

from __future__ import annotations

import gzip
import io
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
FIELDS1 = "f1,f2,f3,f4,f5,f6"
FIELDS2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
#: Seconds between live calls; the em push hosts disconnect a burst (C10 measured
#: 10~20s recovery), so a refusal here would otherwise be misread as a data finding.
THROTTLE_SECONDS = 12.0
#: Symbols whose candidate markets the ported code walks through.
CANDIDATE_MARKETS = ("1", "0", "2", "47")


def _params(secid: str, fields2: str = FIELDS2) -> dict[str, str]:
    """Build the kline params one candidate secid is asked with.

    Args:
        secid: ``market.code`` as the ported loops would try it.
        fields2: Response field list (the ETF call ships one extra field).

    Returns:
        Query params for a full-history daily request.
    """
    return {
        "secid": secid,
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "fields1": FIELDS1,
        "fields2": fields2,
        "klt": "101",
        "fqt": "0",
        "beg": "0",
        "end": "20500000",
    }


def _identity(secid: str, fields2: str = FIELDS2) -> str:
    """Ask one candidate secid who it is, without interpreting the answer.

    Args:
        secid: ``market.code`` to query.
        fields2: Response field list.

    Returns:
        One printable line: envelope ``code`` / ``name`` / kline count / date range,
        or ``未判读`` when the host refused.
    """
    import requests

    try:
        response = requests.get(KLINE_URL, params=_params(secid, fields2), timeout=15)
        response.raise_for_status()
        body = response.json()
    except (requests.RequestException, ValueError) as exc:
        return f"{secid:<14s} 未判读 {type(exc).__name__}: {str(exc)[:70]}"
    data = body.get("data") or {}
    klines = data.get("klines") or []
    first = str(klines[0]).split(",")[0] if klines else "-"
    last = str(klines[-1]).split(",")[0] if klines else "-"
    return (
        f"{secid:<14s} code={data.get('code')!r} market={data.get('market')!r} "
        f"name={data.get('name')!r} klines={len(klines)} range={first}..{last}"
    )


def _section_a() -> None:
    """Print section A: what each candidate secid actually is."""
    print("== A. 候选 secid 的身份（生产代码按顺序试、第一个有 klines 的就采纳） ==")
    for symbol in ("000300", "510300"):
        print(f"  requested symbol={symbol}（沪深300 指数 / 沪深300ETF）")
        for market in CANDIDATE_MARKETS:
            print(f"    {_identity(f'{market}.{symbol}')}")
            time.sleep(THROTTLE_SECONDS)


class _Spy:
    """Record which secid the production loop adopted, without changing behaviour.

    Attributes:
        calls: One line per answered call.
        real: The unpatched ``request_eastmoney`` this spy delegates to.
    """

    def __init__(self, real: Any) -> None:  # noqa: ANN401 - untyped requests boundary
        """Bind the original callable.

        Args:
            real: ``request_eastmoney`` as the ported module imported it.
        """
        self.calls: list[str] = []
        self.real = real

    def __call__(self, url: str, params: dict | None = None, **kwargs: Any) -> Any:  # noqa: ANN401
        """Delegate the call and print who answered.

        Args:
            url: Request URL (passed through untouched).
            params: Query params, including the candidate ``secid``.
            **kwargs: Passed through (``timeout``).

        Returns:
            The upstream response.
        """
        response = self.real(url, params=params, **kwargs)
        try:
            body = response.json()
        except ValueError:
            self.calls.append(f"{(params or {}).get('secid')} 未判读 non-json")
            return response
        data = body.get("data") or {}
        klines = data.get("klines") or []
        self.calls.append(
            f"secid={(params or {}).get('secid')} answered code={data.get('code')!r} "
            f"name={data.get('name')!r} klines={len(klines)}"
        )
        return response


def _sina_reference() -> dict[str, float]:
    """Read the already-recorded sina index fixture as an offline identity oracle.

    Returns:
        ``date string -> close`` for the recorded sh000300 frame (empty if missing).
    """
    import pandas as pd

    path = ROOT / "tests" / "fixtures" / "upstream" / "index_daily_sina" / "reference.csv.gz"
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        frame = pd.read_csv(io.StringIO(handle.read()))
    if "date" not in frame.columns or "close" not in frame.columns:
        return {}
    return {
        str(row["date"]): float(row["close"])
        for _, row in frame.iterrows()
        if str(row["date"]) >= "2024-01-01"
    }


def _section_b() -> None:
    """Print section B: what the production call returns, twice, with the spy on."""
    import opendata_http.index.index_zh_em as index_module

    print("== B. 真实生产调用（同一参数跑两次，spy 记录哪个 secid 被采纳） ==")
    reference = _sina_reference()
    print(f"  sina 夹具 oracle：2024 年起 {len(reference)} 个交易日（离线，不依赖网络）")
    original = index_module.request_eastmoney
    try:
        for label, end_date in (("窄窗 20240229", "20240229"), ("宽窗 20241231", "20241231")):
            spy = _Spy(original)
            index_module.request_eastmoney = spy
            started = time.time()
            try:
                frame = index_module.index_zh_a_hist(
                    symbol="000300",
                    period="daily",
                    start_date="20240101",
                    end_date=end_date,
                )
            except Exception as exc:  # a refusal is a measurement gap, not a finding
                print(f"  {label}: 未判读 {type(exc).__name__}: {str(exc)[:110]}")
                time.sleep(THROTTLE_SECONDS)
                continue
            finally:
                index_module.request_eastmoney = original
            print(f"  {label}: rows={len(frame)} in {time.time() - started:.0f}s")
            for line in spy.calls:
                print(f"    {line}")
            if frame.empty:
                print("    -> 空帧：窗口内一行没有（契约侧就是「这个指数这段时间没行情」）")
                continue
            dates = [str(d) for d in frame["日期"]]
            print(f"    返回日期范围 {dates[0]}..{dates[-1]}")
            overlapped = [d for d in dates if d in reference]
            if overlapped:
                first = overlapped[0]
                em_close = float(frame.loc[frame["日期"] == first, "收盘"].iloc[0])
                print(
                    f"    vs sina 同一交易日 {first}: em 收盘 {em_close} / "
                    f"sina 收盘 {reference[first]} -> "
                    f"{'同一标的' if abs(em_close - reference[first]) < 1.0 else '不是同一标的'}"
                )
            else:
                print("    -> 与 sina 窗口无重叠日期，身份未判读")
            time.sleep(THROTTLE_SECONDS)
    finally:
        index_module.request_eastmoney = original


def _section_c() -> None:
    """Print section C: the map that decides candidate order, measured live."""
    import opendata_http.index.index_zh_em as index_module

    print("== C. 候选顺序的来源 index_code_id_map_em（吞错返回 {} 的那个 map） ==")
    started = time.time()
    try:
        mapping = index_module.index_code_id_map_em()
    except Exception as exc:
        print(f"  未判读 {type(exc).__name__}: {str(exc)[:110]}")
        return
    print(
        f"  {time.time() - started:.0f}s 后 map 大小={len(mapping)}，"
        f"'000300' in map={'000300' in mapping} 值={mapping.get('000300')!r}"
    )
    print("  map 未命中时候选退化为 1./0./2./47. 逐个试，第一个有 klines 的即被采纳（见 A 节身份）")


def main() -> int:
    """Run every section and report the read/no-read tally.

    Returns:
        ``0`` when the script ran; refusals are printed as 未判读 and do not fail it,
        because a host that will not answer is not evidence that data is wrong.
    """
    import os

    os.environ["AKSHARE_EASTMONEY_AUTO_CURL_INTERFACE"] = "0"
    os.environ["AKSHARE_EASTMONEY_CURL_INTERFACE"] = "0"
    for section in (_section_a, _section_b, _section_c):
        section()
        print()
    print("MEASURE_EXIT=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
