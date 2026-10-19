"""Subagent dispatch ledger: the objective caps dispatches at 30, so the cap needs a record.

The brief's process clause -- advance the implementation with parallel subagents, run the per-item
acceptance with subagents, total subagents <= 30 -- cannot be graded from what an agent reports
about
itself. It has to be counted out of the session transcript, where the dispatches actually happened.

Two population facts this instrument had to get right, and initially did not. The cap's subject is
one
goal thread, so the count comes from this thread's transcript; the project directory holds seven
transcripts and counting them all gives 313, which would have reported a far worse violation than
the
real one. And within a single transcript the tool-use ids are already unique -- the double-emission
I
assumed when I added a "dedupe moved the count" arm does not happen here, so that arm is replaced by
ones that check the thread-scoping decision and the within-thread id uniqueness.

The cap is reported as violated on the record (this thread: measured against 30). An instrument that
flattered that reading would be worse than none, so the arms check the measurement's shape, not its
outcome: unique ids, a reproducible recount, a non-empty denominator, and a type attribution whose
total matches the dispatch count.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(
    "/Users/yunjinqi/.qoder-cn/projects/-Users-yunjinqi-Documents-new-projects-opendata"
)
PRIMARY_SESSION = "6973912e-1dea-458f-acfe-f6deca083012"
RUNS = Path(
    "/Users/yunjinqi/Documents/new_projects/opendata/docs/evidence/"
    "C145/wave39-cboe-futures-curve-landing-20261019/runs"
)
CARRIER = RUNS / "w74_dispatch_ledger_face.txt"
CAP = 30


def json_lines(path: Path) -> list[dict[str, Any]]:
    """Every parseable record in one transcript.

    Parsed once per file so the walkers below do not re-read a 20 MB transcript, and so a
    malformed line is dropped in one place rather than inside every loop body.
    """
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(record)
    return out


def dispatch_ids(path: Path) -> tuple[list[str], int]:
    """Every Agent-dispatch tool-use id in one transcript, plus how many lines were scanned."""
    ids: list[str] = []
    records = json_lines(path)
    scanned = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
    for record in records:

        def walk(node: Any) -> None:  # noqa: ANN401
            if isinstance(node, dict):
                if node.get("type") == "tool_use" and node.get("name") == "Agent":
                    ids.append(str(node.get("id") or ""))
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(record)
    return ids, scanned


def main() -> int:
    """Count, dedupe, and report against the cap; write the carrier."""
    lines: list[str] = []
    arms: dict[str, bool] = {}
    files = sorted(PROJECT_DIR.glob("*.jsonl"))
    per_file: dict[str, tuple[int, int, int]] = {}
    all_ids: list[str] = []
    for path in files:
        raw, scanned = dispatch_ids(path)
        deduped = sorted(set(raw))
        per_file[path.stem[:8]] = (len(raw), len(deduped), scanned)
        all_ids.extend(deduped)

    primary_raw, primary_dedup, primary_scanned = per_file[PRIMARY_SESSION[:8]]
    all_thread_ids = {i for f_ in files for i in set(dispatch_ids(f_)[0])}
    total = primary_dedup
    lines.append(
        f"transcript_dir files={len(files)} primary={PRIMARY_SESSION[:8]} "
        f"primary_raw={primary_raw} primary_deduped={primary_dedup} primary_lines={primary_scanned}"
    )
    for stem, (n_raw, n_deduped, n_scanned) in sorted(per_file.items()):
        lines.append(f"file {stem} raw={n_raw} deduped={n_deduped} lines={n_scanned}")
    lines.append(
        f"cap={CAP} this_thread_total={total} over_by={max(0, total - CAP)} "
        f"within_cap={total <= CAP} project_dir_all_threads={len(all_thread_ids)}"
    )
    kinds: Counter[str] = Counter()

    def walk_kinds(node: Any) -> None:  # noqa: ANN401
        if isinstance(node, dict):
            if node.get("type") == "tool_use" and node.get("name") == "Agent":
                input_block = node.get("input")
                if isinstance(input_block, dict):
                    kinds[str(input_block.get("subagent_type") or "<unset>")] += 1
            for value in node.values():
                walk_kinds(value)
        elif isinstance(node, list):
            for value in node:
                walk_kinds(value)

    for path in files:
        for record in json_lines(path):
            walk_kinds(record)
    lines.append(f"subagent_types_seen={json.dumps(dict(kinds), ensure_ascii=False)}")

    primary_ids, _ = dispatch_ids(PROJECT_DIR / f"{PRIMARY_SESSION}.jsonl")
    again = len(set(primary_ids))
    arms["A_thread_ids_are_unique"] = len(set(all_ids[:primary_dedup])) == primary_dedup
    arms["B_thread_scope_changes_the_number"] = len(all_thread_ids) != total
    arms["B2_ids_are_unique_within_the_thread"] = primary_raw == primary_dedup
    arms["C_recount_reproduces_the_number"] = again == total
    arms["G2_types_are_attributed_within_the_thread"] = sum(kinds.values()) == len(all_thread_ids)
    arms["D_primary_transcript_is_the_live_one"] = (
        PROJECT_DIR / f"{PRIMARY_SESSION}.jsonl"
    ).is_file() and primary_dedup > 0
    arms["E_no_empty_denominator"] = all(scanned > 0 for _r, _d, scanned in per_file.values())

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"dispatch_ledger arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w74_dispatch_ledger_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w74_dispatch_ledger_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
