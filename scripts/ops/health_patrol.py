#!/usr/bin/env python3
r"""Daily P0 source health patrol (B3.4 / AC-4).

Probes every registered verified capability with a minimal real query,
updates the registry's routing health and exits non-zero when any
probe failed - so a cron line can page on it:

    30 9 * * *  cd <repo> && set -a; . ./.env; set +a; \\
                 python scripts/ops/health_patrol.py || notify-health

Also prints the key configuration so a missing credential is visible in
the same report. A leg that passed only on its retry prints as FLAKY and
is counted separately: it does not flip the exit code, because the
source is reachable, but it is never printed as a clean pass either.

A leg with watched columns (AC-19) prints one more line per column, since a
full-size page can still carry a hollow column - the a-share catalog's
``list_date`` did exactly that while this report printed ``[ok]``. A column
whose fill shape was never measured on the source *does* flip the exit code,
but never the routing mark: the source answered, so taking it out of ``auto``
would be a different claim from the one the measurement supports.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendata.data.providers import register_providers
from opendata.pipeline.key_health import (
    LEVEL_ALERT,
    LEVEL_NOT_APPLICABLE,
    LEVEL_PRESENCE_ONLY,
    LEVEL_WARN,
)
from opendata.pipeline.patrol import (
    SHAPE_NO_READING,
    CanaryReading,
    credential_health,
    key_status,
    run_patrol,
)


def print_canary(reading: CanaryReading) -> None:
    """Print one watched column's reading, alarm first when it is one.

    Args:
        reading: The column measurement the patrol produced.
    """
    mark = "[CANARY]" if reading.deviates else "  canary"
    if reading.rows is None:
        print(f"        {mark} {reading.asset_type}/{reading.field}: 未判读 {reading.error}")
        return
    measured = "/".join(sorted(reading.allowed))
    print(
        f"        {mark} {reading.asset_type}/{reading.field}: "
        f"缺 {reading.missing}/{reading.rows} 形态 {reading.shape}（实测 {measured}）"
    )


def main() -> int:
    """Run the patrol and report.

    Returns:
        0 when every verified capability probed OK and no watched column
        deviated, 1 otherwise.
    """
    register_providers()
    results = run_patrol()
    keys = key_status()
    print("== 健康巡检结果 ==")
    for result in results:
        leg = f"{result.source}/{result.domain}"
        probe = f"{result.rows} rows, {result.latency_ms:.0f}ms"
        if not result.ok:
            print(f"  [FAIL] {leg}: {result.error}")
        elif result.flaky:
            # 靠重试才过：源是可达的，但这条腿不健康到可以不打字
            print(f"  [FLAKY] {leg}: 重试后 {probe}；首次失败 {result.error}")
        else:
            print(f"  [ok] {leg}: {probe}")
        for reading in result.canaries:
            print_canary(reading)
    print("== 凭证配置 ==")
    health = credential_health(results=results)
    for source, info in keys.items():
        mark = "配置" if info["configured"] else "缺失"
        print(f"  {source}: {mark}（必须={info['required']}）")
    print("== 凭证分级（AC-19）==")
    for source, report in health.items():
        if report.level in {LEVEL_PRESENCE_ONLY, LEVEL_NOT_APPLICABLE}:
            # 没有失败可归因的行只说一句：配了 Key 不等于 Key 有效。
            quiet = f"｜{report.note}" if report.level == LEVEL_PRESENCE_ONLY else ""
            print(f"  {source}: {report.level}{quiet}")
            continue
        classes = "/".join(f"{name}×{count}" for name, count in report.classes)
        attribution = " ".join(report.attributions)
        tail = f"｜{attribution}" if attribution else ""
        print(f"  {source}: {report.level} {classes} 处置方={report.owner}{tail}")
        print(f"        未主动探测：{'/'.join(report.unverified)}")
    failed = sum(1 for result in results if not result.ok)
    flaky = sum(1 for result in results if result.flaky)
    deviations = sum(len(result.field_deviations) for result in results)
    unreadable = sum(
        1 for result in results for reading in result.canaries if reading.shape == SHAPE_NO_READING
    )
    missing_keys = sum(1 for info in keys.values() if info["required"] and not info["configured"])
    alerts = sum(1 for report in health.values() if report.level == LEVEL_ALERT)
    warns = sum(1 for report in health.values() if report.level == LEVEL_WARN)
    presence = sum(1 for report in health.values() if report.level == LEVEL_PRESENCE_ONLY)
    print(
        f"\n失败 {failed} 项；抖动 {flaky} 项；字段级偏差 {deviations} 项"
        f"（未判读 {unreadable} 项）；必配 Key 缺失 {missing_keys} 项；"
        f"凭证告警 {alerts} 项 / 观察 {warns} 项 / 仅在场 {presence} 项"
    )
    # 分级不改退出码：会红的仍然是失败腿和字段级塌陷。凭证面只把「这是谁的麻烦」
    # 写进同一份报告——抖动不算封禁，在场不算健康，到期与配额没有主动源就不判。
    return 1 if failed or deviations else 0


if __name__ == "__main__":
    sys.exit(main())
