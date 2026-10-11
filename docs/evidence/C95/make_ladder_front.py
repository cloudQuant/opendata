#!/usr/bin/env python3
"""C95: rebuild the dispatch brief for the front of the repaired ladder.

``ladder-front-steps0-3.json`` is the packet a kernel-building subagent is handed: for each of the
first four greedy steps it carries the rows that step unlocks, the raw census spellings those rows
cite, the fold each spelling takes, and the registry entry describing the engine gap. The repair
moved the front (``param.relative_default`` is now step 0 and ``path.object_keys`` left the front
for ``request.body_pagination``), so a brief carried over from the pre-repair ladder would send
builders at capabilities that no longer unlock anything.

Deterministic and re-runnable: it reads the shipped roadmap artifact, the census files and the
judge's own ``LABEL_TO_CAPABILITY`` fold table, and writes nothing derived by hand. Re-run it after
every re-derive of the ladder, then let ``vocabulary-repair-face.py`` F7 prove the snapshot still
matches the artifact.
"""

from __future__ import annotations

import json
import pathlib
import sys
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CENSUS_DIR = REPO_ROOT / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐"
ARTIFACT = CENSUS_DIR / "capability-roadmap.json"
OUT = pathlib.Path(__file__).resolve().parent / "ladder-front-steps0-3.json"
STEPS = 4

sys.path.insert(0, str(REPO_ROOT / "scripts" / "quality"))
import model_capability_census as judge  # noqa: E402


def census_rows_by_task() -> dict[str, tuple[str, dict[str, Any]]]:
    """Index the live census by task id, keeping the file each row came from.

    Raises:
        SystemExit: The same task id appears twice -- a duplicated row would silently rewrite a
            cluster, so the brief is refused rather than guessed at.
    """
    out: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in sorted(CENSUS_DIR.glob("census-*.json")):
        if path.name == judge.CENSUS_AUDIT_NAME:
            continue
        for row in json.loads(path.read_text(encoding="utf-8"))["rows"]:
            tid = str(row["task_id"])
            if tid in out:
                raise SystemExit(f"duplicate task_id in the census: {tid}")
            out[tid] = (path.name, row)
    return out


def main() -> int:
    """Write the front-of-ladder brief and print the per-step row counts it recorded."""
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    registry = artifact["capability_registry"]
    absent = {c for c, e in registry.items() if e["status"] != "present"}
    rows_by_task = census_rows_by_task()
    steps = artifact["roadmap"]["greedy_build_order"][:STEPS]
    missing = sorted(
        {r["task_id"] for s in steps for r in s["newly_unlocked_rows"]} - set(rows_by_task)
    )
    if missing:
        raise SystemExit(f"ladder names rows the census does not carry: {missing}")

    clusters = []
    for step_no, step in enumerate(steps):
        cap = str(step["capability"])
        members = []
        for ident in step["newly_unlocked_rows"]:
            tid = str(ident["task_id"])
            census_file, row = rows_by_task[tid]
            raw = [str(x) for x in row["needs_engine_capability"]]
            folded = {lbl: judge.LABEL_TO_CAPABILITY[lbl] for lbl in raw}
            members.append(
                {
                    **{k: v for k, v in row.items() if k != "task_id"},
                    "task_id": tid,
                    "census_file": census_file,
                    "raw_needs": raw,
                    "folded": folded,
                    "canonical_still_absent": sorted(set(folded.values()) & absent),
                }
            )
        clusters.append(
            {
                "step": step_no,
                "capability": cap,
                "rows_newly_unlocked": step["rows_newly_unlocked"],
                "credit_list_length": len(members),
                "registry_entry": registry[cap],
                "rows": members,
            }
        )
        if step["rows_newly_unlocked"] != len(members):
            raise SystemExit(f"step {step_no} count disagrees with its own list")

    brief = {
        "round": artifact["round_id"],
        "artifact": ARTIFACT.relative_to(REPO_ROOT).as_posix(),
        "census_row_total": artifact["census"]["row_count_measured"],
        "present_capabilities": sorted(c for c, e in registry.items() if e["status"] == "present"),
        "generated_by": (
            "make_ladder_front.py: verbatim read of the committed roadmap artifact plus "
            "LABEL_TO_CAPABILITY from scripts/quality/model_capability_census.py; "
            "no number in this file is typed by hand"
        ),
        "clusters": clusters,
    }
    OUT.write_text(
        json.dumps(brief, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {OUT.name} ({OUT.stat().st_size} B) round={brief['round']}")
    for c in clusters:
        print(
            f"   step {c['step']} {c['capability']:26s} rows={c['rows_newly_unlocked']} "
            f"absent_blockers={sorted({b for r in c['rows'] for b in r['canonical_still_absent']})}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
