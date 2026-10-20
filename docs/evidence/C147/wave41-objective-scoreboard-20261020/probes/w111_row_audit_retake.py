"""Re-run the per-row witness audit on the shipped tree and diff it with its wave-39 record.

The scoreboard cites ``w75`` for the core acceptance claim -- 339 accepted rows, five phase
receipts each, zero stale pins, zero counterfact receipts without a refusal token. That citation is
only as fresh as the carrier, and two providers were re-signed after it was written (wave 40's
annotation repairs), so this face re-executes the same instrument against the current tree and
compares the substantive readings with the committed record.

Import-and-re-point, not a rewrite: ``w75`` keeps its own audit logic; only ``RUNS`` and ``CARRIER``
move into this bundle so the wave-39 carrier is not overwritten. Arms are two-sided -- the retake
has to pass, every substantive line has to equal the old record, and the accepted count has to
agree with the judge report, which is a different instrument.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w111_row_audit_retake.txt"
W75 = (
    REPO
    / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/probes"
    / "w75_witness_completeness_face.py"
)
W39_CARRIER = (
    REPO
    / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
    / "w75_witness_completeness_face.txt"
)
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"
KEYS = (
    "stale_pin_files_across_accepted_rows",
    "rows_missing_a_phase",
    "receipt_digest_mismatches",
    "counterfact_without_refusal_text",
    "accepted",
    "rows_in_claims",
    "claims_total",
)


def _readings(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for key in KEYS:
        match = re.search(rf"\b{key}=(-?\d+)", text)
        if match is not None:
            found[key] = match.group(1)
    return found


def main() -> int:
    """Run the shipped audit with its writes re-pointed, then diff it with the wave-39 record."""
    RUNS.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("w75_row_audit_wave41", W75)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import row audit {W75}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w75_row_audit_wave41"] = module
    spec.loader.exec_module(module)
    module.RUNS = RUNS
    module.CARRIER = CARRIER
    saved_argv = sys.argv
    sys.argv = [saved_argv[0]]
    try:
        rc = int(module.main())
    finally:
        sys.argv = saved_argv
    fresh = _readings(CARRIER.read_text(encoding="utf-8", errors="replace"))
    old = _readings(W39_CARRIER.read_text(encoding="utf-8", errors="replace"))
    report = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    judge_accepted = sum(1 for model in report["models"] if model["status"] == "OFFLINE_ACCEPTED")

    lines = [f"retake_exit={rc}", f"fresh={json.dumps(fresh, sort_keys=True)}"]
    lines.append(f"wave39={json.dumps(old, sort_keys=True)}")
    lines.append(f"judge_accepted={judge_accepted}")
    arms = {
        "A_the_audit_passes_on_the_shipped_tree": rc == 0
        and "PASS=True" in CARRIER.read_text(encoding="utf-8", errors="replace"),
        "B_every_substantive_reading_matches_the_committed_record": fresh == old
        and set(fresh) == set(KEYS),
        "C_accepted_count_agrees_with_the_judge": fresh.get("accepted") == str(judge_accepted),
        "D_zero_stale_and_zero_refusal_gaps": fresh.get("stale_pin_files_across_accepted_rows")
        == "0"
        and fresh.get("counterfact_without_refusal_text") == "0"
        and fresh.get("receipt_digest_mismatches") == "0",
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"row_audit_retake arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w111_row_audit_retake_exit={0 if ok else 1}\n"
    CARRIER.write_text(
        CARRIER.read_text(encoding="utf-8", errors="replace") + "\n" + text, encoding="utf-8"
    )
    print(text, end="")
    print(f"w111_row_audit_retake_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
