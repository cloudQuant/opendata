#!/usr/bin/env python3
"""Rewrite the seven C86 ledger cells from the probe's own readings.

``reason`` is copied verbatim out of ``probe-seven-readings.json``, which the carrier generator
writes from the instrument's stdout, so what the ledger claims and what the gate measures cannot
drift apart. The human note stays in ``note`` and names only the residual act and the faces that
still fail it -- never a number that can rot on its own.

It refuses to run twice: once a cell carries ``round=C86`` the patch is already in the tree, and
re-running would overwrite a later round's reading with this one. ``--verify-only`` is the
read-back arm of the same claim -- it re-parses the ledger as written and requires each of the
seven cells to equal this script's own inputs (verbatim probe ``reason``, this round's ``note``,
``state=gap``, ``round=C86``, every evidence path present on disk), so "the ledger says what the
probe measured" is a re-runnable check rather than a one-time assertion.

Run: python3 docs/evidence/C86/ledger-seven-patch.py
     python3 docs/evidence/C86/ledger-seven-patch.py --refresh-notes
     python3 docs/evidence/C86/ledger-seven-patch.py --verify-only
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any, Final

REPO: Final = pathlib.Path(__file__).resolve().parents[3]
LEDGER: Final = REPO / "docs/quality/acceptance-item-ledger.json"
READINGS: Final = REPO / "docs/evidence/C86/probe-seven-readings.json"
CARRIER: Final = "docs/evidence/C86/probe-seven.txt"
ROUND: Final = "C86"
DATE: Final = "2026-10-10"

NOTES: Final[dict[str, str]] = {
    "§4|02": (
        "C86 起本格由 acceptance_item_probe 逐面实测（腿分母、逐腿人工确认、原始响应抽样覆盖、"
        "单位/刻度风险、业务键值冲突组、确认者身份、整体批准）。转绿还差人工逐腿签署与冲突样本修数据，"
        "属用户授权动作。"
    ),
    "§4|04": (
        "C86 起由探针实测搬运对照报告的逐腿判读（腿数为分母，未判读腿逐面点名）与官方 Parquet/"
        "文档示例对照；不再用整体 PASS 代替逐腿读数。待录案腿与官方对照缺口仍在本轮之外。"
    ),
    "§5|02": (
        "C86 起由探针实测十年回补的中断/续跑与进度表同一性（分片算术、跳过数、两轮行数、"
        "独立核对的缺键与值差）。剩余两面——本轮取数来自供应商、当前全市场范围自证——需授权的取数动作。"
    ),
    "AC-8|08": (
        "C86 起由探针实测仓表盘点、日线双源两表、四件套双源覆盖与两源行数/窗口/键对齐面。"
        "本轮另记两处缺陷：C64 盘点载体的表头声明与正文列出对不上"
        "（roster_table_delta、roster_rows_delta 均非 0；差额归因于载体自身而非判定普查，"
        "见 docs/evidence/C86/roster-census-coverage.txt 的 census 覆盖面），"
        "akshare 行数在两份载体间不一致（见 " + CARRIER + " 的 akshare_rows_across_carriers 面）。"
    ),
    "AC-13|08": (
        "C86 起由探针按 B3 观测表逐行读数（时刻/查询截止日/返回K日）并用 B4 的注册窗口筛选，"
        "不再以「文件里有没有待办字样」判定；证据面补进 B3/B4，原 runtime-stack 读数撑不起本格。"
        "转绿需在注册窗口内的交易日做多时点实测并真拿到当日 K。"
    ),
    "AC-15|01": (
        "C86 起由探针实测保留声明、结构签名基线、跨轮行数同一与留档不含凭据。"
        "唯一未达标面是旧库账户仍具写权限（connection_read_only），只读改造属需授权的库侧动作。"
    ),
    "AC-15|02": (
        "C86 起由探针实测；「无业务映射则不适用」的占位文本不再被当作迁移价值结论，"
        "处置面改成每张表都要落到归档或删除，并新增迁移留档自己的行数哈希与全量校验两面。"
        "转绿需业务映射与授权后的迁移/DROP 动作。"
    ),
}

EXTRA_EVIDENCE: Final[dict[str, list[str]]] = {
    "AC-13|08": [
        "docs/evidence/B3/schedule-calibration.txt",
        "docs/evidence/B4/scheduler_startup.log",
    ],
}


def item_of(key: str) -> str:
    """Return the ledger key without its wording digest."""
    return key.rsplit("|", 1)[0]


def load_readings() -> dict[str, Any]:
    """Read the probe's structured readings for this round."""
    raw: dict[str, Any] = json.loads(READINGS.read_text(encoding="utf-8"))
    return {str(entry["item"]): entry for entry in raw["items"]}


def verify(readings: dict[str, Any]) -> int:
    """Re-parse the ledger as written and require each cell to equal this script's inputs."""
    ledger: dict[str, Any] = json.loads(LEDGER.read_text(encoding="utf-8"))
    problems = 0
    checked = 0
    for key, entry in ledger["items"].items():
        item = item_of(str(key))
        if item not in NOTES:
            continue
        checked += 1
        probe = readings[item]
        cell = dict(entry)
        if str(cell.get("reason")) != str(probe["reason"]):
            problems += 1
            print(f"PROBLEM {item}: reason is not the probe's verbatim reading")
        if cell.get("state") != "gap" or cell.get("round") != ROUND or cell.get("date") != DATE:
            problems += 1
            print(f"PROBLEM {item}: state={cell.get('state')} round={cell.get('round')}")
        if str(cell.get("note")) != NOTES[item]:
            problems += 1
            print(f"PROBLEM {item}: note does not equal this script's NOTES entry")
        for path in [str(p) for p in cell.get("evidence", [])]:
            if not (REPO / path).is_file():
                problems += 1
                print(f"PROBLEM {item}: evidence path missing: {path}")
        if CARRIER not in [str(p) for p in cell.get("evidence", [])]:
            problems += 1
            print(f"PROBLEM {item}: carrier not cited in evidence")
    print(f"VERIFY cells={checked}/{len(NOTES)} problems={problems}")
    return 0 if problems == 0 and checked == len(NOTES) else 1


def write_ledger(ledger: dict[str, Any]) -> None:
    """Serialize the ledger the way the repository stores it (CJK verbatim, indent 2)."""
    text = json.dumps(ledger, ensure_ascii=False, indent=2)
    if not text.endswith("\n"):
        text += "\n"
    LEDGER.write_text(text, encoding="utf-8")
    print(f"wrote {LEDGER.relative_to(REPO)} bytes={LEDGER.stat().st_size}")


def refresh_notes() -> int:
    """Rewrite only the ``note`` field of the seven cells, from this script's NOTES table."""
    ledger: dict[str, Any] = json.loads(LEDGER.read_text(encoding="utf-8"))
    touched = 0
    for key, entry in ledger["items"].items():
        item = item_of(str(key))
        if item in NOTES and str(entry.get("note")) != NOTES[item]:
            entry["note"] = NOTES[item]
            touched += 1
            print(f"note refreshed {key}")
    if touched == 0:
        print("no note differs from NOTES")
    write_ledger(ledger)
    return 0


def main() -> int:
    """Patch the seven cells, refresh their notes, or read back an already-patched ledger."""
    readings = load_readings()
    if "--verify-only" in sys.argv[1:]:
        return verify(readings)
    if "--refresh-notes" in sys.argv[1:]:
        return refresh_notes()
    ledger: dict[str, Any] = json.loads(LEDGER.read_text(encoding="utf-8"))
    changed = 0
    for key, entry in ledger["items"].items():
        item = item_of(str(key))
        if item not in NOTES:
            continue
        cell = dict(entry)
        probe = readings[item]
        if probe["state"] != "gap":
            print(f"ABORT {item}: probe reads {probe['state']}, the cell may need flipping")
            return 1
        if str(cell.get("round")) == ROUND:
            print(f"ABORT {item}: already written by {ROUND}, refusing to overwrite a later round")
            return 1
        evidence = [str(path) for path in cell.get("evidence", [])]
        if CARRIER not in evidence:
            evidence.append(CARRIER)
        for extra in EXTRA_EVIDENCE.get(item, []):
            if extra not in evidence:
                evidence.append(extra)
        for path in evidence:
            if not (REPO / path).is_file():
                print(f"ABORT {item}: evidence path missing: {path}")
                return 1
        cell.update(
            {
                "state": "gap",
                "round": ROUND,
                "date": DATE,
                "command": (
                    f"python scripts/quality/acceptance_item_probe.py --item '{item}'"
                    f"（判定面为实测读数，逐面见 {CARRIER}）"
                ),
                "reason": str(probe["reason"]),
                "evidence": evidence,
                "note": NOTES[item],
            }
        )
        entry.clear()
        entry.update(cell)
        changed += 1
        print(f"patched {key}: evidence={len(evidence)} reason={probe['reason'][:52]}…")
    if changed != len(NOTES):
        print(f"ABORT: patched {changed}, expected {len(NOTES)}")
        return 1
    write_ledger(ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
