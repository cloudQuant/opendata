#!/usr/bin/env python3
r"""Inject the failure shapes this repo has already recorded and read the grades.

C28 / AC-19 measurement surface (offline, no network, no warehouse write).

Why this file exists: the live patrol can only grade what it happens to see,
and the archived runs contain no 401/403/429 from any source - so a live run
alone cannot show that the graded plane is reachable per class. Here each shape
is taken from a real raise site (the same exception objects the providers
build, including the ones whose message carries a Key-in-query URL), handed to
the one judgment plane the reports use, and rendered the way
``scripts/ops/health_patrol.py`` renders it.

Two things are read out, not asserted by hand:

* the census: shape -> class -> attribution -> level -> owner;
* the leak check, read in both directions: the settings stub's Key value is the
  canary, so a value that reaches the payload shows up here - and the shapes
  that *would* have leaked are counted first, because a check that nothing
  could fail would be the same shape as the tautological assertion C27 deleted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import httpx
from requests.exceptions import HTTPError

from opendata.data.providers.ecb.models._client import EcbProviderError
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.ths.models._client import ThsProviderError
from opendata.pipeline.key_health import (
    LEVEL_NOT_APPLICABLE,
    LEVEL_PRESENCE_ONLY,
    attribution_of,
    classify_failure,
    redact,
)
from opendata.pipeline.patrol import PatrolResult, credential_health
from opendata_fuyao.errors import error_for_transport, error_for_upstream_code

if TYPE_CHECKING:
    from collections.abc import Callable

#: Stands in for a real Key: the settings stub holds it, and the rendered
#: payload must not. A check that used an empty value would prove nothing.
CANARY = "CANARY-SECRET-do-not-render"
#: Exactly how httpx/requests write a Key-in-query URL into a failure message.
LEAKY_URL = f"https://api.stlouisfed.org/fred/series/observations?series_id=CPIA&api_key={CANARY}"


def _httpx_error(status: int) -> httpx.HTTPStatusError:
    """Build the exception a Key-in-query transport raises on a refusal."""
    request = httpx.Request("GET", LEAKY_URL)
    response = httpx.Response(status, request=request, headers={"content-type": "text/plain"})
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return exc
    raise AssertionError(f"status {status} did not raise")


#: ``(source, label, builder)`` - every builder is a shape a probe can actually
#: raise, taken from the provider's own raise site.
SHAPES: list[tuple[str, str, Callable[[], BaseException]]] = [
    ("ths", "fuyao 2001（上游拒绝凭证）", lambda: error_for_upstream_code(2001)),
    ("ths", "fuyao 2003（凭证被拒）", lambda: error_for_upstream_code(2003)),
    ("ths", "fuyao transport http 401", lambda: error_for_transport("http", detail="401")),
    ("ths", "fuyao 4001（限流）", lambda: error_for_upstream_code(4001)),
    (
        "ths",
        "fuyao transport rate_limited 429",
        lambda: error_for_transport("rate_limited", detail="429"),
    ),
    ("ths", "fuyao 5001（源侧故障）", lambda: error_for_upstream_code(5001)),
    ("ths", "fuyao transport timeout", lambda: error_for_transport("timeout")),
    ("ths", "ths 未配置 Key", lambda: ThsProviderError("THS_NOT_CONFIGURED")),
    ("ths", "ths 空响应", lambda: ThsProviderError("THS_EMPTY_RESPONSE")),
    ("ths", "ths 裸码无法解析", lambda: ThsProviderError("THS_SYMBOL_UNRESOLVED: 600519")),
    ("ths", "未知形状（无码无状态）", lambda: RuntimeError("boom")),
    ("fred", "fred 未配置 Key", lambda: FredProviderError("FRED_API_KEY_MISSING")),
    (
        "fred",
        "fred 401（Key 在 query 里）",
        lambda: FredProviderError("FRED_HTTP_ERROR", status=401, url=LEAKY_URL),
    ),
    (
        "fred",
        "fred 429（Key 在 query 里）",
        lambda: FredProviderError("FRED_HTTP_ERROR", status=429, url=LEAKY_URL),
    ),
    ("ecb", "ecb 403", lambda: EcbProviderError("ECB_HTTP_ERROR", status=403, url=None)),
    ("yfinance", "httpx 429（消息含完整 URL）", lambda: _httpx_error(429)),
    (
        "akshare",
        "requests 502（搬运层 raise_for_status）",
        lambda: HTTPError(f"502 Server Error: Bad Gateway for url: {LEAKY_URL}"),
    ),
    ("oecd", "读超时", lambda: TimeoutError()),
    (
        "imf",
        "最坏形状：文案里直接写出 Key 值（query 规则抓不到）",
        lambda: RuntimeError(f"Authorization: Bearer {CANARY}"),
    ),
]


def main() -> int:
    """Print the census, the graded rows, and the two-way leak check."""
    print("== 形状普查：每条已录制形状进分类器一次 ==")
    legs: list[PatrolResult] = []
    raw_with_key: list[str] = []
    raw_with_query: list[str] = []
    escaped: list[str] = []
    for index, (source, label, build) in enumerate(SHAPES):
        exc = build()
        raw = f"{type(exc).__name__}: {exc}"
        if CANARY in raw:
            raw_with_key.append(label)
        if "api_key=" in raw:
            raw_with_query.append(label)
        # 与 patrol 的 catch site 同形：CANARY 在这里充当「本机已配置的 Key 值」，
        # 所以 URL query 之外的文案形状也走一遍值脱敏。
        message = redact(raw, (CANARY,))
        if CANARY in message:
            escaped.append(label)
        failure_class = classify_failure(exc)
        attribution = attribution_of(exc)
        legs.append(
            PatrolResult(
                domain=f"probe{index}",
                source=source,
                ok=False,
                error=message,
                latency_ms=0.0,
                verified=True,
                failure_class=failure_class,
                attribution=attribution,
            )
        )
        print(f"  {source:9} {label:34} -> {failure_class:19} 归因={attribution}")
        print(f"  {'':9} 归档文案={message}")

    print("\n== 分级渲染（与 scripts/ops/health_patrol.py 同一判据面）==")
    settings = type("S", (), {"fuyao_api_key": CANARY, "fred_api_key": CANARY})()
    health = credential_health(settings, legs)
    for source, report in health.items():
        classes = "/".join(f"{name}×{count}" for name, count in report.classes)
        attribution = " ".join(report.attributions)
        tail = f"｜{attribution}" if attribution else ""
        head = f"  {source}: {report.level} {classes} 处置方={report.owner}{tail}"
        print(head)
        if report.level not in {LEVEL_PRESENCE_ONLY, LEVEL_NOT_APPLICABLE}:
            print(f"        未主动探测：{'/'.join(report.unverified)}")
        print(f"        判据：{report.note}")

    rendered = json.dumps(
        {source: report.as_dict() for source, report in health.items()},
        ensure_ascii=False,
    )
    print("\n== 泄漏检查（双向：既要抓到，也要证明这些形状本来会漏）==")
    print(f"  注入前文案含 Key 值的形状：{len(raw_with_key)} 条 -> {'、'.join(raw_with_key)}")
    print(f"  注入前文案含 api_key= query 的形状：{len(raw_with_query)} 条")
    print(f"  脱敏后文案仍含 Key 值：{len(escaped)} 条（必须为 0）")
    print(f"  settings 里的 Key 值是否出现在分级载荷中：{CANARY in rendered}")
    print(f"  载荷是否含 api_key= 形式的 query：{'api_key=' in rendered}")
    print(f"  巡检文案字段是否含 Key 值：{any(CANARY in (leg.error or '') for leg in legs)}")
    # 正值控制：一条永远不会红的泄漏检查，和 C27 删掉的那条恒真复权判据是同一形状。
    if not raw_with_key or not raw_with_query:
        print(
            "\nLEAK_CHECK_VACUOUS：注入的形状里没有任何形状携带 Key 值/query，"
            "上面的 False 是白拿的，不能读成「脱敏生效」。",
            file=sys.stderr,
        )
        return 2
    if escaped or CANARY in rendered or "api_key=" in rendered:
        print("\nLEAK_DETECTED：脱敏面漏掉了形状，见上方计数。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
