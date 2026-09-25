"""C21 证据：把搬运层 ``更新日期`` 的时区基准固定为北京时间.

为什么要单独证这一点：C21 的门禁第一次跑挂了，挂的不是本轮改的巡检面，而是
AC-6 保真回放用例 ``financial_statement``——10 个差异单元格全部是同一列
``更新日期``，每个都差整 5 小时。本机时区从 C20 那几跑的 +0800 变成了 +0300，
而夹具是在 +0800 录的。也就是说搬运层（连同它上游的 akshare）把 epoch 秒按
**本机**时区渲染：同一份应答换一台机器就给出不同的值。

判据不放宽的方式：不重录夹具去迁就 +0300，而是把渲染基准钉在数据语义所属的
时区（A 股公告时间 = 北京时间），于是回放结果与录制值重新逐字相等。

本脚本三件事，全部离线（请求来自录制夹具）：

1. 证明夹具里的 ``update_time`` 是 epoch 秒，且**上游原式**（不传 tz）随主机时区漂移；
2. 证明**固定后的搬运层**在三台"虚拟机器"（Asia/Shanghai / Etc/UTC /
   America/Los_Angeles）下给出同一列，且等于录制值；
3. 打印固定前后的首行取值，便于人工复核。

免 Key、只读、零写入。运行：``python docs/evidence/C21/update_time-tz-pin.py``
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.codemod import compare_with_upstream as comparator  # noqa: E402

#: 三个互相远离的时区：数据语义时区 + 零偏移 + 负偏移。
ZONES = ("Asia/Shanghai", "Etc/UTC", "America/Los_Angeles")
#: pristine 上游 checkout（AC-6 录制用的那一份），这里只读它的源码与 HEAD。
UPSTREAM_CHECKOUT = REPO_ROOT.parent / "akshare"
UPSTREAM_MODULE = "akshare/stock_fundamental/stock_finance_sina.py"
CASE_NAME = "financial_statement"


def _upstream_head(path: Path) -> str:
    """Return the HEAD commit of a git checkout (literal argv, no shell)."""
    argv = ["git", "-C", str(path), "rev-parse", "HEAD"]
    completed = subprocess.run(  # noqa: S603  # nosec B603
        argv, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip()


def _upstream_render(epochs: list[int]) -> list[str]:
    """Render epochs the way pristine upstream does: no ``tz``, so the host zone wins."""
    return [datetime.fromtimestamp(epoch).isoformat() for epoch in epochs]


def _replay_ported(zone: str) -> list[str]:
    """Replay the recorded transcript into the ported tree under one host zone."""
    case = next(item for item in comparator.CASES if item.name == CASE_NAME)
    fixture = comparator.FIXTURES_DIR / case.name
    entries = comparator._read_transcript(fixture / "responses.json.gz")
    function = comparator._load_case_function("opendata_http", case.function)
    os.environ["TZ"] = zone
    time.tzset()
    with comparator.HttpReplayer(entries) as replayer:
        frame = function(**case.kwargs)
    if replayer.cursor != len(entries):
        message = f"replay consumed {replayer.cursor} of {len(entries)} recorded calls"
        raise RuntimeError(message)
    return [str(value) for value in frame["更新日期"]]


def _recorded_epochs() -> list[int]:
    """Pull the ``update_time`` epoch seconds out of the recorded body."""
    entries = comparator._read_transcript(comparator.FIXTURES_DIR / CASE_NAME / "responses.json.gz")
    payload = comparator._deserialize_response(entries[0]).json()
    reports = payload["result"]["data"]["report_list"]
    date_values = [item["date_value"] for item in payload["result"]["data"]["report_date"]]
    return [int(reports[date_value]["update_time"]) for date_value in date_values]


def main() -> int:
    """Compare host-zone rendering with the pinned one and judge the fix."""
    lock = json.loads(comparator.LOCK_PATH.read_text(encoding="utf-8"))
    print("## 运行环境")
    print(f"  captured_at={datetime.now().astimezone().isoformat()}")
    print(f"  本机时区 tzname={time.tzname!r} timezone={time.timezone} TZ={os.environ.get('TZ')!r}")
    print(f"  录制基线 lock commit={lock['upstream']['commit']}")
    print(f"  pristine checkout HEAD={_upstream_head(UPSTREAM_CHECKOUT)}")
    print(f"  AC-6 用例={CASE_NAME}（夹具录制于 Asia/Shanghai，即 UTC+8）")

    print(f"\n## 上游原式（{UPSTREAM_MODULE}，不传 tz ⇒ 取本机时区）")
    lines = (UPSTREAM_CHECKOUT / UPSTREAM_MODULE).read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if "fromtimestamp" in line:
            print("  " + "\n  ".join(lines[index - 1 : index + 3]))
            break

    print("\n## 录制应答里的 update_time（epoch 秒，前 3 行）")
    previous_zone = os.environ.get("TZ")
    epochs = _recorded_epochs()
    for epoch in epochs[:3]:
        in_utc = datetime.fromtimestamp(epoch, ZoneInfo("Etc/UTC")).isoformat()
        in_shanghai = datetime.fromtimestamp(epoch, ZoneInfo("Asia/Shanghai")).isoformat()
        print(f"  epoch={epoch} → UTC {in_utc} / 北京时间 {in_shanghai}")

    reference_path = comparator.FIXTURES_DIR / CASE_NAME / "reference.csv.gz"
    reference = comparator._read_reference_frame(reference_path)
    recorded = [str(value) for value in reference["更新日期"]]
    ported: dict[str, list[str]] = {}
    host_local: dict[str, list[str]] = {}
    try:
        for zone in ZONES:
            ported[zone] = _replay_ported(zone)
            host_local[zone] = _upstream_render(epochs)
    finally:
        if previous_zone is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous_zone
        time.tzset()

    print("\n## 同一份录制应答，三种主机时区下的 更新日期（首行）")
    print(f"  录制值（夹具，+08:00 主机）  {recorded[0]}")
    for zone in ZONES:
        verdict = "相同" if ported[zone] == recorded else "不同"
        print(f"  TZ={zone:<20s} 上游原式 {host_local[zone][0]}")
        print(f"    搬运层（本轮固定后） {ported[zone][0]}  |  与录制：{verdict}")

    drift = len({host_local[zone][0] for zone in ZONES}) > 1
    pinned_stable = all(ported[zone] == ported[ZONES[0]] for zone in ZONES)
    matches_record = all(ported[zone] == recorded for zone in ZONES)
    print("\n## 判定")
    print(f"  {'PASS' if drift else 'FAIL'} 上游原式随主机时区漂移（不固定就无法跨机复现）")
    print(f"  {'PASS' if pinned_stable else 'FAIL'} 固定后三档主机时区输出逐列相同")
    print(f"  {'PASS' if matches_record else 'FAIL'} 固定后与 +08:00 录制值逐字相等（判据未放宽）")
    return 0 if (drift and pinned_stable and matches_record) else 1


if __name__ == "__main__":
    raise SystemExit(main())
