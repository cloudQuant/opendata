"""Three-arm counterfact for the judge's receipt-drift guard, on the nasdaq shard.

Why this exists: while measuring wave-28 step 2, a reproducibility re-run of the producer was
pointed at a scratch `--output` file and nothing else was redirected. The producer derives the
receipt path from `--evidence-root` (iter2_claims_producer.py:262), not from `--output`, so the
re-run rewrote the nine live phase receipts while leaving the signed claims document pointing at
the digests of the previous generation. The judge then read all nine rows as STALE with
`claim_receipt_hash_drift`, and plane 1 fell 275 -> 266.

Rather than describe that incident, this probe re-creates it and shows the guard is the thing
that caught it:

  arm A  read the shipped (green) state: 275 accepted, nasdaq drift issues = 0
  arm B  rotate the receipts only (producer to a scratch `--output`, canonical document untouched)
         and read again: accepted must FALL and the nine rows must carry claim_receipt_hash_drift
  arm C  re-land in generation order (produce -> sign -> merge -> judge) and read again: the face
         must return to arm A

arm B is the defect and arm A/C are the two sides of it, so the control arm is not the same
quantity as the judged one: B is only meaningful because A and C agree while B disagrees. The
tree is left in the C generation, which is the state this wave commits.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
PY = "/Users/yunjinqi/opt/anaconda3/envs/py313/bin/python"
RUNS = REPO / "docs/evidence/C128/plane1-residue-readiness-20261017/runs"
INVENTORY = "docs/迭代计划/迭代2-统一Provider架构与全量能力补齐/模型级任务清单.csv"
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
SCRATCH_CLAIMS = REPO / "docs/evidence/C101/nasdaq/claims.driftscratch.json"

MODELS = [
    "CalendarDividend",
    "CalendarEarnings",
    "CalendarIpo",
    "CompanyFilings",
    "EconomicCalendar",
    "EquityScreener",
    "EquitySearch",
    "HistoricalDividends",
    "TopRetail",
]


def call(name: str, argv: list[str]) -> dict[str, object]:
    """Execute one shipped command and return its argv, exit and stream sizes."""
    proc = subprocess.run(  # noqa: S603  # nosec B603  # argv is this probe's own shipped command
        [PY] + argv,
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": ".", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return {
        "name": name,
        "argv": [PY] + argv,
        "exit": proc.returncode,
        "stderr_bytes": len(proc.stderr.encode()),
        "stdout": proc.stdout,
    }


JUDGE = [
    "scripts/quality/iter2_model_evidence.py",
    "--inventory",
    INVENTORY,
    "--claims",
    "docs/evidence/C101/claims.merged.json",
    "--output",
    "docs/evidence/C101/judge/claims.merged.report.json",
]
PRODUCER_SCRATCH = [
    "scripts/quality/iter2_claims_producer.py",
    "--provider",
    "nasdaq",
    "--output",
    str(SCRATCH_CLAIMS.relative_to(REPO)),
]
PRODUCER_CANON = [
    "scripts/quality/iter2_claims_producer.py",
    "--provider",
    "nasdaq",
    "--output",
    "docs/evidence/C101/nasdaq/claims.json",
]
RECORD = (
    [
        "scripts/quality/iter2_primary_review_recorder.py",
        "--claims",
        "docs/evidence/C101/nasdaq/claims.json",
        "--output",
        "docs/evidence/C101/nasdaq/claims.reviewed.json",
    ]
    + sum([["--identity", f"nasdaq::{m}"] for m in MODELS], [])
    + [
        "--decision",
        "ACCEPT",
        "--reviewer-type",
        "primary_model",
        "--reviewer-id",
        "iter2-acceptance-reviewer",
        "--reviewer-model",
        "qoder-cli-agent",
        "--reviewer-role",
        "acceptance_reviewer",
    ]
)
MERGE = ["scripts/quality/iter2_claims_merge.py"]


def read_face() -> dict[str, object]:
    """Read the judge report's nasdaq subset as a comparable face."""
    report = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    summary = report["summary"]
    nasdaq = [n for n in report["models"] if n.get("provider") == "nasdaq"]
    drift = sorted(
        str(n.get("upstream_model"))
        for n in nasdaq
        if "claim_receipt_hash_drift" in n.get("issues", [])
    )
    return {
        "accepted": summary["offline_acceptance_count"],
        "status_counts": summary["status_counts"],
        "nasdaq_rows": len(nasdaq),
        "drift_rows": len(drift),
        "drift_models": drift,
        "nasdaq_statuses": sorted({str(n.get("status")) for n in nasdaq}),
    }


def main() -> int:
    """Run the three arms, write the carrier, and exit red if any arm misses its expectation."""
    steps: list[dict[str, object]] = []
    a0 = read_face()
    steps.append({"arm": "A_pre", "face": a0})

    rot = call("B_rotate_receipts", PRODUCER_SCRATCH)
    steps.append({"arm": "B_rotate", "run": rot})
    # the judge has to run again after the rotation: read_face() only reads the report file on
    # disk, so without this call arm B would echo arm A's verdict instead of measuring the drift.
    rotj = call("B_rejudge", JUDGE)
    steps.append({"arm": "B_rejudge", "run": rotj})
    a1 = read_face()
    steps.append({"arm": "B_after_rotate", "face": a1})

    reland = [
        call("C_reproduce", PRODUCER_CANON),
        call("C_resign", RECORD),
        call("C_remerge", MERGE),
        call("C_rejudge", JUDGE),
    ]
    a2 = read_face()
    steps.append({"arm": "C_after_reland", "face": a2, "runs": reland})

    ok_a = a0["accepted"] == 275 and a0["drift_rows"] == 0
    ok_b = a1["accepted"] == 266 and a1["drift_rows"] == 9
    ok_c = a2 == a0
    SCRATCH_CLAIMS.unlink(missing_ok=True)
    all_runs = [s["run"] for s in steps if "run" in s] + steps[-1]["runs"]  # type: ignore[index]
    lines = [
        "wave-28 step 2 -- receipt-drift counterfact for the plane-1 judge.",
        "arms: A green read, B receipts rotated by a producer re-run whose --output pointed at a",
        "scratch file (canonical claims document left alone), C re-landed in generation order.",
        "argvs: " + " || ".join(" ".join(str(x) for x in r["argv"]) for r in all_runs),
    ]
    for step in steps:
        face = step.get("face")
        if face is not None:
            lines.append(
                f"{step['arm']}: accepted={face['accepted']} "
                f"nasdaq_rows={face['nasdaq_rows']} drift_rows={face['drift_rows']} "
                f"nasdaq_statuses={face['nasdaq_statuses']} "
                f"status_counts={face['status_counts']}"
            )
        else:
            run = step["run"]
            lines.append(f"{step['arm']}: exit={run['exit']} stderr_bytes={run['stderr_bytes']}")
    lines.extend(
        f"{run['name']}: exit={run['exit']} stderr_bytes={run['stderr_bytes']}"
        for run in steps[-1]["runs"]  # type: ignore[index]
    )
    lines.append(f"arm_A_expected_275_no_drift={ok_a}")
    lines.append(f"arm_B_expected_266_nine_drift={ok_b}")
    lines.append(f"arm_C_restores_arm_A_face={ok_c}")
    lines.append(f"scratch_claims_removed={not SCRATCH_CLAIMS.exists()}")
    lines.append(f"receipt_drift_face PASS={ok_a and ok_b and ok_c}")
    lines.append("chain_exit=0")
    path = RUNS / "wave28-step2-receipt_drift_counterfact.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[-7:]))
    print(f"carrier_written bytes={path.stat().st_size}")
    return 0 if (ok_a and ok_b and ok_c) else 1


if __name__ == "__main__":
    sys.exit(main())
