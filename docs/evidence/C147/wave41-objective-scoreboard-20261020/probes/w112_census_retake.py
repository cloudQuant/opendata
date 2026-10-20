"""Re-run the provider-coverage and clean-room censuses on the shipped tree and diff with wave 39.

The scoreboard cites ``w72`` for "29 of 32 providers have at least one accepted row" and ``w73``
for "0 OpenBB imports across 445 parsed modules, 31 provider packages". Both were recorded before
waves 40-41 touched two adapters and one test fixture, so this face re-executes them with their
write targets re-pointed into this bundle (import-and-re-point, never a rewrite) and compares the
key lines with the committed records. A drift would mean the citations aged; equality means they
did not.

The equality predicate is proved able to fail by mutating one parsed value in memory.
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
CARRIER = RUNS / "w112_census_retake.txt"
W39 = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
PROBES = {
    "w72": (W39 / "probes/w72_provider_coverage_census.py", "w72_provider_coverage_census.txt"),
    "w73": (W39 / "probes/w73_clean_room_face.py", "w73_clean_room_face.txt"),
}
JUDGE_REPORT = REPO / "docs/evidence/C101/judge/claims.merged.report.json"


def _run(name: str, probe: Path, carrier_name: str) -> tuple[int, str]:
    spec = importlib.util.spec_from_file_location(f"{name}_retake", probe)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import probe {probe}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_retake"] = module
    spec.loader.exec_module(module)
    module.RUNS = RUNS
    if hasattr(module, "CARRIER"):
        module.CARRIER = RUNS / carrier_name
    saved = sys.argv
    sys.argv = [saved[0]]
    try:
        rc = int(module.main())
    finally:
        sys.argv = saved
    text = (RUNS / carrier_name).read_text(encoding="utf-8", errors="replace")
    return rc, text


def _key(text: str, pattern: str) -> str:
    match = re.search(pattern, text)
    return match.group(0) if match else "MISSING"


def main() -> int:
    """Re-run both censuses, compare their key lines with wave 39, and write the carrier."""
    RUNS.mkdir(parents=True, exist_ok=True)
    judge = json.loads(JUDGE_REPORT.read_text(encoding="utf-8"))
    judge_accepted = sum(1 for model in judge["models"] if model["status"] == "OFFLINE_ACCEPTED")
    rc72, fresh72 = _run("w72", *PROBES["w72"])
    rc73, fresh73 = _run("w73", *PROBES["w73"])
    old72 = (W39 / "runs/w72_provider_coverage_census.txt").read_text(encoding="utf-8")
    old73 = (W39 / "runs/w73_clean_room_face.txt").read_text(encoding="utf-8")

    keys72 = [
        r"accepted=\d+",
        r"providers_with_at_least_one_accepted_row=\d+",
        r"zero=\[[^\]]*\]",
    ]
    keys73 = [
        r"in_scope_files=\d+",
        r"openbb_import_violations=\d+",
        r"provider_packages=\d+",
        r"inventory_without_package=\[[^\]]*\]",
    ]
    pairs72 = [(_key(fresh72, key), _key(old72, key)) for key in keys72]
    pairs73 = [(_key(fresh73, key), _key(old73, key)) for key in keys73]
    lines = [f"w72_exit={rc72} w73_exit={rc73}"]
    lines.extend(f"w72 {new} | wave39 {old}" for new, old in pairs72)
    lines.extend(f"w73 {new} | wave39 {old}" for new, old in pairs73)
    lines.append(f"judge_accepted={judge_accepted}")

    forged = list(pairs72)
    forged[0] = ("accepted=338", pairs72[0][1])
    arms = {
        "A_censuses_exit_zero": rc72 == 0 and rc73 == 0,
        "B_coverage_readings_unchanged": all(new == old for new, old in pairs72),
        "C_clean_room_readings_unchanged": all(new == old for new, old in pairs73),
        "D_accepted_matches_the_judge": _key(fresh72, r"accepted=\d+")
        == f"accepted={judge_accepted}",
        "E_equality_can_bite": any(new != old for new, old in forged)
        and all(new == old for new, old in pairs72),
    }
    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"census_retake arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w112_census_retake_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w112_census_retake_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
