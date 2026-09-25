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
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from opendata.data.providers import register_providers
from opendata.pipeline.patrol import key_status, run_patrol


def main() -> int:
    """Run the patrol and report.

    Returns:
        0 when every verified capability probed OK, 1 otherwise.
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
    print("== 凭证配置 ==")
    for source, info in keys.items():
        mark = "配置" if info["configured"] else "缺失"
        print(f"  {source}: {mark}（必须={info['required']}）")
    failed = sum(1 for result in results if not result.ok)
    flaky = sum(1 for result in results if result.flaky)
    missing_keys = sum(1 for info in keys.values() if info["required"] and not info["configured"])
    print(f"\n失败 {failed} 项；抖动 {flaky} 项；必配 Key 缺失 {missing_keys} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
