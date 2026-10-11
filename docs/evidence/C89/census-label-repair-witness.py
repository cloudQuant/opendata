"""C89 witness: what keeps the three repaired rows out of cover, and which arm returns non-zero?

The #81 repair added cite-backed need labels to three census rows whose prose already named the
mechanisms while ``needs_engine_capability`` did not (benzinga/AnalystSearch,
federal_reserve/YieldCurve, government_us/TreasuryAuctions), and moved
government_us/... off ``date_format -> columns.cast`` onto ``query_key_date_mapping ->
request.serialised_param`` because the only date rendering that fetcher performs is request-side.

Every number below is measured. The pre-repair face is not typed: it is read out of the census
blob at :data:`REPAIR_BASE` with ``git show``, so the added/removed label sets and both arms are
recomputed on every run.

  * arm A -- cast/rescale assumed anchored present, labels as they are on disk now;
  * arm B -- the same anchoring with each audited row restored to its REPAIR_BASE label list.

Arm A must print 0 flipping rows and arm B must print 3. If either fails to move the witness dies
instead of publishing an unproven zero. No network, no database, no engine execution, no gate.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404  # read-only git argv, shell disabled
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REPO = next(
    ancestor
    for ancestor in Path(__file__).resolve().parents
    if (ancestor / "scripts" / "quality" / "model_capability_census.py").is_file()
)
sys.path.insert(0, str(REPO / "scripts" / "quality"))
import model_capability_census as mcc  # noqa: E402

#: The commit whose census bytes are the pre-repair face. Read from git, never retyped.
REPAIR_BASE: Final = "aa62daa"
CENSUS_DIR = REPO / mcc.CENSUS_DIR_REL
AUDITED: Final = (
    "OBB2-benzinga-AnalystSearch",
    "OBB2-federal_reserve-YieldCurve",
    "OBB2-government_us-TreasuryAuctions",
)
#: The two capabilities whose anchoring the repaired rows must survive.
ASSUMED = frozenset(
    entry.capability
    for entry in mcc.REGISTRY
    if entry.capability in ("columns.cast", "columns.rescale")
)


def die(msg: str) -> None:
    """Print a FAIL line and exit non-zero."""
    print(f"FAIL: {msg}")
    raise SystemExit(1)


def git_show(rev: str, rel: str) -> str:
    """Return one blob from history, failing loudly when the revision or path is gone."""
    proc = subprocess.run(  # nosec B603, B607  # noqa: S603
        ["git", "show", f"{rev}:{rel}"],  # noqa: S607
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        die(f"git show {rev}:{rel} exited {proc.returncode}: {proc.stderr.strip()[:160]}")
    return proc.stdout


@dataclass(frozen=True)
class RowFace:
    """One audited row, as the disk and as the repair base saw it."""

    task_id: str
    census_file: str
    before: list[str]
    after: list[str]
    needs_before: frozenset[str]
    needs_after: frozenset[str]


def payloads() -> dict[str, tuple[Path, dict[str, Any]]]:
    """Index every census row payload by task id beside the file it came from."""
    found: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in mcc.census_row_files(CENSUS_DIR):
        for item in json.loads(path.read_text(encoding="utf-8"))["rows"]:
            found[str(item["task_id"])] = (path, item)
    return found


def flip_count(rows: dict[str, RowFace], present: frozenset[str], *, repaired: bool) -> list[str]:
    """Rows that would enter shipped cover under arm A, using one label face or the other."""
    flipping: list[str] = []
    for face in rows.values():
        needs = face.needs_after if repaired else face.needs_before
        expressible = not (needs - present)
        blocked_only_by_assumed = bool(needs - present) and not (needs - (present | ASSUMED))
        if not expressible and blocked_only_by_assumed:
            flipping.append(face.task_id)
    return sorted(flipping)


def main() -> int:
    """Print the measured face for the repaired rows and both arms."""
    report = mcc.run(round_id="C89-label-repair")
    present = frozenset(report.statuses["present"])
    live = {row.task_id: row for row in mcc.load_rows(CENSUS_DIR)}
    payloads_by_id = payloads()

    print("census label repair witness -- every count below is read off disk or out of git")
    print(
        f"census rows read: {report.row_count}; expressible_today: "
        f"{report.roadmap.rows_expressible_today}; pending: {report.roadmap.rows_pending}"
    )
    groups = report.statuses
    print(
        f"status groups: present={len(groups['present'])} absent={len(groups['absent'])} "
        f"drifted={len(groups['drifted'])} unanchored={len(groups['unanchored'])} "
        "(control: the same counter reads those four numbers for four distinct statuses)"
    )
    for cap in sorted(ASSUMED):
        reading = report.readings[cap]
        print(f"anchor {cap}: status={reading.status} (arm A assumes present)")

    rows: dict[str, RowFace] = {}
    for task in AUDITED:
        path, payload = payloads_by_id[task]
        rel = path.relative_to(REPO).as_posix()
        base_doc = json.loads(git_show(REPAIR_BASE, rel))
        base_row = next(r for r in base_doc["rows"] if r["task_id"] == task)
        before = [str(label) for label in base_row["needs_engine_capability"]]
        after = [str(label) for label in payload["needs_engine_capability"]]
        rows[task] = RowFace(
            task_id=task,
            census_file=path.name,
            before=before,
            after=after,
            needs_before=mcc.map_needs(before, task),
            needs_after=mcc.map_needs(after, task),
        )

    for face in rows.values():
        added = sorted(set(face.after) - set(face.before))
        removed = sorted(set(face.before) - set(face.after))
        print(f"\n== {face.task_id} == census {face.census_file}")
        print(f"  labels at {REPAIR_BASE}: {face.before}")
        print(f"  labels on disk now   : {face.after}")
        print(f"  added: {added or ['-']} removed: {removed or ['-']}")
        print(f"  needs then: {sorted(face.needs_before)}")
        print(f"  needs now : {sorted(face.needs_after)}")
        for name, needs in (("arm B", face.needs_before), ("arm A", face.needs_after)):
            blocked = sorted(needs - present)
            blocked_after_flip = sorted(needs - (present | ASSUMED))
            print(
                f"  {name}: unmet now={blocked} unmet if cast+rescale present={blocked_after_flip}"
            )

    arm_a = flip_count(rows, present, repaired=True)
    arm_b = flip_count(rows, present, repaired=False)
    print(f"\narm A rows flipping if cast+rescale anchored present (labels on disk): {arm_a}")
    print(
        f"arm B rows flipping with the {REPAIR_BASE} label lists restored: {len(arm_b)} -> {arm_b}"
    )
    if arm_a:
        die(f"arm A must read 0 flipping rows, it read {len(arm_a)} -> {arm_a}")
    if len(arm_b) != len(AUDITED):
        die(
            "arm B must return a non-zero flip count for every audited row, "
            f"it read {len(arm_b)} -> {arm_b}"
        )
    print(
        "    so the 0 in arm A is a reading: the same counter returns "
        f"{len(arm_b)} the moment the added labels are taken away"
    )

    fold = mcc.LABEL_TO_CAPABILITY.get("date_format", "NO FOLD")
    print("\ndate_format fold face:")
    print(f"  LABEL_TO_CAPABILITY['date_format'] = {fold}")
    print(
        f"  LABEL_TO_CAPABILITY['query_key_date_mapping'] = "
        f"{mcc.LABEL_TO_CAPABILITY['query_key_date_mapping']}"
    )
    now = sum(1 for row in live.values() if "date_format" in mcc_row_labels(row))
    before_rows = 0
    for path in mcc.census_row_files(CENSUS_DIR):
        rel = path.relative_to(REPO).as_posix()
        doc = json.loads(git_show(REPAIR_BASE, rel))
        before_rows += sum(
            1
            for item in doc["rows"]
            if "date_format" in [str(label) for label in item["needs_engine_capability"]]
        )
    print(
        f"  rows carrying label date_format: at {REPAIR_BASE}={before_rows} on disk now={now} "
        f"(delta {now - before_rows}) -- the fold stays because {now} rows still carry the label "
        "and none of them was audited site-by-site this round"
    )
    if now >= before_rows:
        die(f"expected the repaired census to carry fewer date_format rows, read {now}")
    print("\nNOT TOUCHED: no network, no database, no engine execution, no make gate")
    return 0


def mcc_row_labels(row: mcc.CensusRow) -> list[str]:
    """The raw label tuple of a loaded census row."""
    return [str(label) for label in row.raw_labels]


if __name__ == "__main__":
    raise SystemExit(main())
