"""Attribute wave 40's test plane against wave 39's plane by node-id sets, not by counts.

Wave 40 changed two product modules (the two runtime annotation repairs) and re-signed their
claims; the question this face answers is whether the shipped tree's failure set moved. Counting
would hide a swap -- wave 37 published "28 failures before and after" while seven were repaired and
seven broke -- so the comparison is sorted node ids, with the arithmetic closing and an injected red
proving the "new red" arm can bite.

The two planes are both main-tree takes: wave 39's final take
(``C145/runs/test_plane_w39_worktree_final.xml``) and wave 40's (``test_plane_w40_final.xml``). No
control worktree is needed here because the question is narrower than wave 39's: excluding the two
claims' provider rows, does anything else move?
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C146/wave40-query-annotation-divergence-20261019"
RUNS = REPO / BUNDLE / "runs"
W39_RUNS = REPO / "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019/runs"
INSTRUMENT = (
    REPO
    / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes"
    / "w51_test_plane_attribution.py"
)
BEFORE_XML = W39_RUNS / "test_plane_w39_worktree_final.xml"
AFTER_XML = RUNS / "test_plane_w40_final.xml"
CARRIER = RUNS / "w103_test_plane_attribution.txt"
MIN_COLLECTED = 1000


def _load_readers() -> Any:  # noqa: ANN401  # wave 38's junit readers are this instrument
    spec = importlib.util.spec_from_file_location("w38_plane_readers_wave40", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import plane readers {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_plane_readers_wave40"] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    """Diff the two planes, prove the arms can bite, and write the carrier."""
    readers = _load_readers()
    lines: list[str] = []
    arms: dict[str, bool] = {}
    before: dict[str, str] = readers._failures(BEFORE_XML)
    after: dict[str, str] = readers._failures(AFTER_XML)
    before_counts: dict[str, str] = readers._counts(BEFORE_XML)
    after_counts: dict[str, str] = readers._counts(AFTER_XML)
    shared = sorted(set(before) & set(after))
    new = sorted(set(after) - set(before))
    fixed = sorted(set(before) - set(after))
    lines.append(
        f"set_diff before={len(before)} after={len(after)} shared={len(shared)} "
        f"new={len(new)} fixed={len(fixed)}"
    )
    lines.append(f"suites before={json.dumps(before_counts)} after={json.dumps(after_counts)}")
    lines.extend(f"NEW_RED {node} :: {after[node][:180]}" for node in new)
    lines.extend(f"FIXED {node} :: {before[node][:180]}" for node in fixed)

    arms["A_both_planes_collected_the_suite"] = (
        int(after_counts.get("tests", 0)) >= MIN_COLLECTED
        and int(before_counts.get("tests", 0)) >= MIN_COLLECTED
    )
    arms["B_neither_plane_had_collection_errors"] = (
        int(after_counts.get("errors", -1)) == 0 and int(before_counts.get("errors", -1)) == 0
    )
    arms["C_arithmetic_closes"] = len(before) == len(shared) + len(fixed) and len(after) == len(
        shared
    ) + len(new)
    forged = dict(after)
    forged["tests/synthetic.py::test_injected"] = "AssertionError: injected by this arm"
    forged_new = sorted(set(forged) - set(before))
    lines.append(f"arm_can_bite forged_new={forged_new} real_new={len(new)}")
    arms["D_new_red_arm_can_bite"] = forged_new != [] and new == []
    arms["E_no_new_red_in_the_wave_40_plane"] = new == []

    families: Counter[str] = Counter()
    for node in new:
        path = node.split("::")[0]
        families[path] += 1
    lines.append(f"new_red_families={json.dumps(dict(families), sort_keys=True)}")

    before_ids = set(before)
    after_ids = set(after)
    lines.append(
        f"collected_delta={int(after_counts.get('tests', 0)) - int(before_counts.get('tests', 0))} "
        f"failure_overlap={len(before_ids & after_ids)}"
    )

    passed = sum(1 for ok in arms.values() if ok)
    ok = passed == len(arms)
    lines.append(
        f"test_plane_attribution_w40 arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(name for name, value in arms.items() if not value)}"
    )
    text = "\n".join(lines) + "\n" + f"w103_test_plane_attribution_exit={0 if ok else 1}\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print(f"w103_test_plane_attribution_exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
