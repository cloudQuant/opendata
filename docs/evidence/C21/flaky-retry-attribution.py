"""C21 巡检告警面的真机证据:一次抖动、一次真失败、一次没发生的请求.

为什么需要注入：本轮自然巡检 20 条腿全绿（live-patrol.txt / live-patrol-rerun.txt），
自然抖动没来；而 C20 的现场正是「第一跑 oecd 两条腿 FAIL，报告里只有一个裸
``OECD_HTTP_ERROR``」——既无法归因，也没法判断那是不是值得叫醒人的事
（见 docs/evidence/C20/oecd-flakiness-attribution.txt 的来由）。⇒ 只能在真机上把
上游瞬态复现出来，才说明本轮改动改变了「现场能说什么」。

注入点只有一个：最底层的 ``oecd._http_get``。其上全部走生产路径（patrol 的重试与
判定、registry 的路由标记、provider 的请求构造、真实上游应答），假的只有「这一轮
上游没答好」这一件事，且错误消息里明写是注入。四个场景：

- A1 一次传输抖动 ⇒ 重试后通过，且显式打成 flaky（第一次的失败留在报告里，不是打绿）。
- A2 连续两次抖动 ⇒ 仍然判死并标不可达（重试不是万能消音器，这是 A1 的反向对照）。
- B 零注入：向 OECD/ECB/IMF 各发一个上游确实不认的键 ⇒ 消息自带 status + query-free URL。
- C 零注入：fred 未配 Key ⇒ 没有请求发生，所以只有裸码（无请求可归因）。

只读、免额外 Key、零写入。
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from opendata.data.providers.ecb.models import _client as ecb_client
from opendata.data.providers.fred.models import _client as fred_client
from opendata.data.providers.imf.models import _client as imf_client
from opendata.data.providers.oecd import register as register_oecd
from opendata.data.providers.oecd.models import _client as oecd_client
from opendata.data.registry import ProviderRegistry
from opendata.pipeline import patrol as patrol_module

if TYPE_CHECKING:
    from collections.abc import Callable

    from opendata.pipeline.patrol import PatrolResult

#: 被注入的那条腿的序列键（也是 C20 唯一真抖过的两条腿之一）。
BLIP_URL_MARK = "GBR.M.HICP.CPI"
#: 探针窗口，与 PROBE_PARAMS 里 oecd/economy_cpi 用的那一份一致。
START = date(2023, 1, 1)
END = date(2024, 12, 31)
#: 还剩下几次注入。
_INJECT: dict[str, int] = {"left": 0}
REAL_OECD_GET = oecd_client._http_get


def _inject_then_real(url: str, params: dict[str, str], timeout: float | None) -> tuple[int, str]:
    """Fail the target leg's next requests, then pass everything through untouched."""
    if _INJECT["left"] and BLIP_URL_MARK in url:
        _INJECT["left"] -= 1
        blip = f"【C21 证据注入，非上游真实行为】connection reset left={_INJECT['left']}"
        raise httpx.ConnectError(blip)
    return REAL_OECD_GET(url, params, timeout)


def _patrol_oecd(injects: int) -> tuple[PatrolResult, bool]:
    """Probe the two oecd legs through the production patrol with ``injects`` blips."""
    registry = ProviderRegistry()
    register_oecd(registry)
    _INJECT["left"] = injects
    oecd_client._http_get = _inject_then_real
    try:
        results = asyncio.run(patrol_module.patrol(registry))
    finally:
        oecd_client._http_get = REAL_OECD_GET
    leg = next(result for result in results if result.domain == "economy_cpi")
    print(f"  attempts={leg.attempts} ok={leg.ok} flaky={leg.flaky} rows={leg.rows}")
    print(f"  error={leg.error!r}")
    try:
        routed = registry.resolve("macro", "economy_cpi", source="auto")
        print(f"  auto 路由：{routed.capability.source}（该腿仍可达）")
        reachable = True
    except LookupError:
        print("  auto 路由：LookupError —— 该腿已被标成不可达")
        reachable = False
    return leg, reachable


def _scenario_flaky() -> bool:
    """A1/A2：一次抖动救得回来，两次抖动救不回来，两者都必须显式."""
    print("## A1 注入一次传输抖动（重试后通过，但不是干净的通过）")
    leg, reachable = _patrol_oecd(injects=1)
    rescued = bool(leg.ok and leg.attempts == 2 and leg.flaky and reachable)
    print("## A2 注入连续两次抖动（反向对照：重试不是消音器）")
    leg2, reachable2 = _patrol_oecd(injects=patrol_module.PROBE_ATTEMPTS)
    used_both_attempts = leg2.attempts == patrol_module.PROBE_ATTEMPTS
    still_fails = not leg2.ok and used_both_attempts and not reachable2
    print(f"  注入消耗完毕：{_INJECT['left'] == 0}")
    return rescued and still_fails


def _failure_line(label: str, call: Callable[[], object]) -> bool:
    """Run one real request and judge the shape of the failure it produces."""
    try:
        call()
    except Exception as exc:  # 要看的就是这一行的形态
        message = str(exc)
        print(f"  {label}: {type(exc).__name__}: {message}")
        return " status=" in message and " url=http" in message
    print(f"  {label}: 上游答了 200，本例未取到失败行")
    return False


def _scenario_attribution() -> bool:
    """B：零注入，真实上游拒绝的键，失败行必须自己说清是哪一次请求."""
    print("## B 三个 provider 各发一个上游确实不认的键（真实 status，零注入）")
    calls: list[tuple[str, Callable[[], object]]] = [
        (
            "oecd",
            lambda: oecd_client.fetch_observations(
                "NO.SUCH.KEY", start=START, end=END, timeout=30.0
            ),
        ),
        (
            "ecb",
            lambda: ecb_client.fetch_observations(
                "ICP/NO.SUCH", start=START, end=END, timeout=30.0
            ),
        ),
        ("imf", lambda: imf_client.fetch_indicator("NO_SUCH", "USA", timeout=30.0)),
    ]
    return all(_failure_line(label, call) for label, call in calls)


def _scenario_bare_code() -> bool:
    """C：没有请求发生，所以只有裸码，不带 status/url."""
    print("## C fred 未配 Key（没有请求，也就没有可归因的请求）")
    try:
        fred_client.require_api_key()
        print("  fred: 本机配了 Key，本例跳过（不影响 A/B 判据）")
        return True
    except Exception as exc:
        print(f"  fred: {type(exc).__name__}: {exc!s}")
        return " status=" not in str(exc) and " url=" not in str(exc)


def main() -> int:
    """Run the four scenarios and judge them.

    Returns:
        0 when every shape came out as designed, 1 otherwise.
    """
    checks = {
        "A 一次抖动重试并显式标 flaky / 两次抖动仍判死": _scenario_flaky(),
        "B 真失败自带 status + query-free URL": _scenario_attribution(),
        "C 未发生的请求只报裸码": _scenario_bare_code(),
    }
    print("## 判定")
    for label, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'} {label}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
