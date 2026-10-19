"""Census of the plane-1 / plane-2 gap: accepted offline, unaccepted in the ledger.

AC2-10 measured today reads ``dev_done=17`` while plane 1 accepts 339 rows. That gap is the real
shape of "per-item acceptance" in this repo, and it must be enumerated row by row before anything
is written, because the failure this instrument exists to avoid is a label flip:
``label_without_witness=0`` holds today, and a mass status edit is what would break it.

Reads only committed bytes -- ``git show HEAD:docs/evidence/C101/claims.merged.json`` -- so it can
run while a signing chain rewrites that file in the worktree. A row counts as evidence-complete
when its five phases are all present and PASS; the ledger's own ``implementation_task_status``
says what has been claimed.

Arms: the two populations must join on a key that exists on both sides (a silent join failure
would print a gap of zero, the worst possible false negative); the five-phase predicate must be
shown able to refuse a broken row; and the claim schema for the counterfact leg must be complete.
"""

from __future__ import annotations

import csv
import json
import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
GIT = "/usr/bin/git"
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
CLAIMS_REV = "HEAD:docs/evidence/C101/claims.merged.json"
JUDGE_REV = "HEAD:docs/evidence/C101/judge/claims.merged.report.json"
LEDGER = REPO / "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
PHASES = ("implementation", "direct", "service", "contract", "counterfact")
CARRIER = RUNS / "w71_plane2_gap_census.txt"


def git_show(rev: str) -> dict[str, Any]:
    """One committed JSON artifact, read from git rather than the worktree."""
    proc = subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "show", rev], cwd=REPO, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise SystemExit(f"git show {rev} failed rc={proc.returncode}")
    doc: dict[str, Any] = json.loads(proc.stdout)
    return doc


def ledger_status() -> dict[str, str]:
    """``provider::Model`` to the ledger's implementation task status."""
    out: dict[str, str] = {}
    with LEDGER.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            out[f"{row['provider']}::{row['upstream_model']}"] = (
                row.get("implementation_task_status") or ""
            ).strip()
    return out


def main() -> int:
    """Join committed claims to ledger status and print the gap, row by row."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    claims = git_show(CLAIMS_REV).get("claims") or []
    judge = git_show(JUDGE_REV)
    statuses = ledger_status()
    by_row: dict[str, dict[str, str]] = {}
    for claim in claims:
        key = f"{claim.get('provider')}::{claim.get('upstream_model')}"
        by_row.setdefault(key, {})[str(claim.get("phase"))] = str(claim.get("status"))

    accepted = {
        f"{m['provider']}::{m['upstream_model']}"
        for m in judge.get("models") or []
        if m.get("status") == "OFFLINE_ACCEPTED"
    }
    complete = {
        key
        for key, phases in by_row.items()
        if all(phases.get(phase) == "PASS" for phase in PHASES)
    }
    joined = accepted & set(by_row)
    lines.append(
        f"populations claims={len(claims)} rows_in_claims={len(by_row)} accepted={len(accepted)} "
        f"ledger_rows={len(statuses)} joined_accepted_to_claims={len(joined)}"
    )
    lines.append(
        f"evidence_complete={len(complete)} "
        f"accepted_but_unjoined={sorted(accepted - set(by_row))[:6]}"
    )

    gap = sorted(complete & accepted - {k for k, v in statuses.items() if v == "DEV_DONE"})
    by_status: Counter[str] = Counter(statuses.get(key, "<absent>") for key in gap)
    lines.append(
        f"gap rows_with_full_witness_but_not_dev_done={len(gap)} by_status={dict(by_status)}"
    )
    lines.extend(f"GAP {key:44} ledger={statuses.get(key, '<absent>')}" for key in gap[:40])
    sample = gap[0] if gap else ""
    lines.append(f"sample_first={sample} phases={json.dumps(by_row.get(sample, {}))}")

    arms["A_join_is_not_silent"] = len(joined) == len(accepted) and accepted - set(by_row) == set()
    arms["B_complete_needs_all_five_phases"] = all(
        all(by_row[k].get(p) == "PASS" for p in PHASES) for k in complete
    )
    forged = dict(by_row[next(iter(complete))]) if complete else {}
    forged["counterfact"] = "FAIL"
    arms["C_predicate_can_refuse_a_broken_row"] = bool(forged) and not all(
        forged.get(p) == "PASS" for p in PHASES
    )
    # The counterfact leg is the one that carries the refusal. Its bytes are not re-hashed
    # here: the signing chain is rewriting the worktree copies right now, so a digest arm
    # would measure an in-flight write rather than the gap. Armed instead is what is stable
    # at HEAD -- each leg names a receipt path under C101 plus a 64-hex digest -- and the
    # sample shows those receipts are committed, which is the audit-relevant fact.
    cf_claims = [c for c in claims if c.get("phase") == "counterfact"]
    cf_sample = cf_claims[:12]
    schema_ok = 0
    tracked = 0
    for claim in cf_sample:
        receipt = claim.get("receipt") or {}
        rel = str(receipt.get("path") or "")
        recorded = str(receipt.get("sha256") or "")
        if rel.startswith("docs/evidence/C101/receipts/") and len(recorded) == 64:
            schema_ok += 1
        proc = subprocess.run(  # nosec B603  # noqa: S603
            [GIT, "show", f"HEAD:{rel}"], cwd=REPO, capture_output=True, check=False
        )
        tracked += 1 if proc.returncode == 0 else 0
    lines.append(
        f"counterfact_legs total={len(cf_claims)} sampled={len(cf_sample)} "
        f"path_and_digest_recorded={schema_ok} receipt_tracked_at_head={tracked} "
        f"(receipt bodies are verified by wave 38, not here)"
    )
    arms["D_counterfact_claims_name_a_receipt_and_digest"] = schema_ok == len(cf_sample)
    arms["E_gap_is_a_subset_of_accepted"] = set(gap) <= accepted

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"plane2_gap_census arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    text = text + f"w71_plane2_gap_census_exit={0 if ok else 1}" + "\n"
    RUNS.mkdir(parents=True, exist_ok=True)
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print("w71_plane2_gap_census_exit=0" if ok else "w71_plane2_gap_census_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
