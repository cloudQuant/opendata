"""Re-run the residue-gating face on the shipped tree and diff its census line with wave 39.

The scoreboard cites ``w67`` for the residue partition (4 CREDENTIAL_GATED / 5 PROBED_UNANSWERED /
2 NOT_PROBED). Its inputs are the task ledger and the reference provider packages, which waves 40-41
did not touch -- but "did not touch" is a claim, so this face re-executes the same instrument with
its ``RUNS`` re-pointed into this bundle and compares the census line with the committed record. The
equality arm is proved able to fail by mutating one parsed number.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C147/wave41-objective-scoreboard-20261020"
RUNS = REPO / BUNDLE / "runs"
CARRIER = RUNS / "w113_residue_retake.txt"
W39 = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
W67 = W39 / "probes/w67_residue_gating_face.py"
OLD = W39 / "runs/w67_residue_gating_face.txt"


def _line(text: str) -> str:
    match = re.search(r"ledger_population .*", text)
    return match.group(0).strip() if match else "MISSING"


def main() -> int:
    """Run the residue face with writes re-pointed, compare the census, and write the carrier."""
    RUNS.mkdir(parents=True, exist_ok=True)
    spec = importlib.util.spec_from_file_location("w67_residue_wave41", W67)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import residue face {W67}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w67_residue_wave41"] = module
    spec.loader.exec_module(module)
    module.RUNS = RUNS
    saved = sys.argv
    sys.argv = [saved[0]]
    try:
        rc = int(module.main())
    finally:
        sys.argv = saved
    fresh_text = (RUNS / "w67_residue_gating_face.txt").read_text(
        encoding="utf-8", errors="replace"
    )
    old_text = OLD.read_text(encoding="utf-8", errors="replace")
    fresh_line = _line(fresh_text)
    old_line = _line(old_text)
    sums = re.search(r"sums=(\d+) of=(\d+)", fresh_line)
    forged = fresh_line.replace("sums=11 of=11", "sums=10 of=11")
    lines = [
        f"retake_exit={rc}",
        f"fresh={fresh_line}",
        f"wave39={old_line}",
    ]
    arms = {
        "A_the_face_passes_on_the_shipped_tree": rc == 0 and "PASS=True" in fresh_text,
        "B_census_line_unchanged": fresh_line == old_line and fresh_line != "MISSING",
        "C_sums_close_to_the_residue": sums is not None and sums.group(1) == sums.group(2) == "11",
        "D_equality_can_bite": forged != old_line,
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"residue_retake arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w113_residue_retake_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w113_residue_retake_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
