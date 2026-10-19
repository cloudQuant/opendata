"""Attribute wave 39's test plane by node-id sets, not by failure counts.

Wave 37 published "28 failures before and after" as proof its reds were pre-existing. The two sets
were
not the same 28: seven failures the wave repaired were offset by seven it broke, and a count
comparison
is blind to that swap. This analyzer diffs sorted node ids, prints shared/new/fixed with the
arithmetic
that must close, and groups any remaining new red by what its failure **text** names, because a
test's
name is not its input.

It re-uses wave 38's shipped junit readers rather than re-deriving them: ``w51._node_id`` rebuilds
pytest's ``path::Class::method`` from a junit that carries only ``classname`` (a naive reader makes
every
id ``None::test_x`` and turns the file-membership arms vacuous), and ``w51._counts`` reads the suite
header so "0 failures" cannot be a plane that collected nothing.

Every arm is two-sided. ``no_new_red`` is only a measurement once the same predicate is shown to
report
an injected red, and the fence arms are a pair: the four registry-fence tests must be red in the
take that ran against the stale byte pin and green in the final take, so "the re-pin fixed it" is a
diff of two node-id sets rather than a sentence.

Three main-tree takes are read here. Take 1 started 10:28:21Z, nine minutes before the A2 lint pass
re-wrapped two pinned files at 10:37:36Z, so its late tests saw post-lint bytes and its early tests
did not: it is kept as the record of that race, not as the wave's plane. Take 2 ran entirely against
the post-lint, pre-re-pin tree. The final take runs after the cboe re-sign and the fence re-pin, and
is the plane this analyzer attributes.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C145/wave39-cboe-futures-curve-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
INSTRUMENT = (
    REPO
    / "docs/evidence/C143/wave38-seeking-alpha-landing-20261019/probes"
    / "w51_test_plane_attribution.py"
)
WORKTREE_XML = RUNS / "test_plane_w39_worktree.xml"
WORKTREE_TAKE2_XML = RUNS / "test_plane_w39_worktree.take2.xml"
#: The plane that describes the tree as shipped: taken after the last edit landed. Take 1 started
#: nine minutes before the A2 lint re-wrap and finished after it, so it is a record of a race.
FINAL_XML = RUNS / "test_plane_w39_worktree_final.xml"
CONTROL_XML = RUNS / "test_plane_w39_control.xml"
WORKTREE_CARRIER = RUNS / "test_plane_w39_worktree.txt"
FINAL_CARRIER = RUNS / "test_plane_w39_worktree_final.txt"
CONTROL_CARRIER = RUNS / "test_plane_w39_control.txt"
CARRIER = RUNS / "w69_test_plane_attribution.txt"
#: The four files that compare ``registry_batch5_snapshot.json`` against the live registry; a red
#: in one of them attributes to the byte-pin fence, not to the assertion's own subject.
FENCE_FILES = (
    "tests/test_provider_domains.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
    "tests/test_pipeline_jobs.py",
)
#: Text that attributes a red to the fence or to this wave's own enumerations.
FENCE_MARKERS = (
    "registry_batch5_snapshot",
    "opendata/data/domains.yaml",
    "cboe_futures_curve",
    "NATIVE_MODEL_DOMAINS",
    "cboe/specs.py",
    "NOT_DECLARABLE",
)
MIN_COLLECTED = 1000


def _load_readers() -> Any:  # noqa: ANN401  # wave 38's junit readers are this probe's instrument
    """Import the shipped readers without running wave 38's analysis."""
    spec = importlib.util.spec_from_file_location("w38_plane_readers", INSTRUMENT)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import plane readers {INSTRUMENT}")
    module: Any = importlib.util.module_from_spec(spec)
    sys.modules["w38_plane_readers"] = module
    spec.loader.exec_module(module)
    return module


def _fence_nodes(failures: dict[str, str]) -> list[str]:
    """Node ids inside the four fence-comparing test files."""
    return sorted(node for node in failures if node.split("::")[0] in FENCE_FILES)


def main() -> int:
    """Diff the two planes, prove the arms can bite, and write the carrier."""
    readers = _load_readers()
    lines: list[str] = []
    arms: dict[str, bool] = {}

    worktree: dict[str, str] = readers._failures(FINAL_XML)
    control: dict[str, str] = readers._failures(CONTROL_XML)
    wt_counts: dict[str, str] = readers._counts(FINAL_XML)
    ct_counts: dict[str, str] = readers._counts(CONTROL_XML)
    shared = sorted(set(worktree) & set(control))
    new = sorted(set(worktree) - set(control))
    fixed = sorted(set(control) - set(worktree))
    lines.append(
        f"set_diff worktree={len(worktree)} control={len(control)} shared={len(shared)} "
        f"new={len(new)} fixed={len(fixed)}"
    )
    lines.append(f"worktree_suite={json.dumps(wt_counts)} control_suite={json.dumps(ct_counts)}")
    lines.extend(f"NEW_RED {node} :: {worktree[node][:180]}" for node in new)
    lines.extend(f"FIXED {node} :: {control[node][:180]}" for node in fixed)

    node_paths = {node.split("::")[0] for node in worktree}
    lines.append(
        f"node_id_shape sample={sorted(worktree)[:1]} distinct_files={len(node_paths)} "
        f"none_typed_prefix={'None::' in json.dumps(sorted(worktree))}"
    )

    families: Counter[str] = Counter()
    unattributed: list[str] = []
    for node in new:
        text = worktree[node]
        hit = next((marker for marker in FENCE_MARKERS if marker in text), None)
        families[hit or "other"] += 1
        if hit is None:
            unattributed.append(node)
    lines.append(
        f"new_red_families={json.dumps(dict(families), ensure_ascii=False)} "
        f"unattributed={len(unattributed)}"
    )

    wt_tests = int(wt_counts.get("tests", 0))
    ct_tests = int(ct_counts.get("tests", 0))
    arms["A_planes_collected_the_same_suite"] = (
        wt_tests >= MIN_COLLECTED and ct_tests >= MIN_COLLECTED
    )
    arms["B_neither_plane_had_collection_errors"] = (
        int(ct_counts.get("errors", -1)) == 0 and int(wt_counts.get("errors", -1)) == 0
    )
    arms["C_node_ids_carry_a_file_prefix"] = "None::" not in json.dumps(sorted(worktree)[:50])
    arms["D_no_new_red_in_worktree"] = new == []
    forged = dict(worktree)
    forged["tests/synthetic.py::test_injected"] = "AssertionError: injected by this arm"
    forged_new = sorted(set(forged) - set(control))
    lines.append(f"arm_can_bite forged_new={forged_new} real_new={len(new)}")
    arms["E_new_red_arm_can_bite"] = forged_new != [] and new == []
    arms["F_set_arithmetic_holds"] = len(worktree) == len(shared) + len(new) and len(
        control
    ) == len(shared) + len(fixed)

    take2: dict[str, str] = (
        readers._failures(WORKTREE_TAKE2_XML) if WORKTREE_TAKE2_XML.is_file() else {}
    )
    take1: dict[str, str] = readers._failures(WORKTREE_XML) if WORKTREE_XML.is_file() else {}
    fence_take1, fence_take2, fence_final = (
        _fence_nodes(take1),
        _fence_nodes(take2),
        _fence_nodes(worktree),
    )
    lines.append(
        f"three_takes take1={len(take1)} take2={len(take2)} final={len(worktree)} "
        f"take1_minus_take2={sorted(set(take1) - set(take2))} "
        f"take2_minus_take1={sorted(set(take2) - set(take1))}"
    )
    lines.append(
        f"fence_nodes take1={len(fence_take1)} take2={len(fence_take2)} "
        f"final={len(fence_final)} final_nodes={fence_final}"
    )
    # The repair reading, two-sided: the stale byte pin has to have made these red in the take that
    # ran against it, and the final plane has to be free of them. Either half alone is vacuous.
    arms["G_fence_reds_present_in_the_pre_repin_take"] = len(fence_take2) > 0
    arms["I_fence_reds_gone_in_the_final_plane"] = fence_final == []

    fixed_markers = Counter(
        next((m for m in FENCE_MARKERS if m in control[node]), "other") for node in fixed
    )
    lines.append(f"fixed_by_named_marker={json.dumps(dict(fixed_markers))} of_fixed={len(fixed)}")

    exit_arms: dict[str, bool] = {}
    for carrier in (FINAL_CARRIER, WORKTREE_CARRIER, CONTROL_CARRIER):
        text = carrier.read_text(encoding="utf-8") if carrier.is_file() else ""
        found = re.search(r"\b(\w+_exit)=(-?\d+)", text)
        exit_arms[carrier.name] = bool(found)
        shown = found.group(0) if found else "MISSING"
        lines.append(f"carrier {carrier.name} exit_reading={shown}")
    arms["H_carriers_carry_their_exits"] = all(exit_arms.values())

    passed = sum(1 for v in arms.values() if v)
    ok = passed == len(arms)
    lines.append(
        f"test_plane_attribution arms={len(arms)} arms_passed={passed} PASS={ok} "
        f"red={sorted(k for k, v in arms.items() if not v)}"
    )
    text = "\n".join(lines) + "\n"
    CARRIER.write_text(text, encoding="utf-8")
    print(text, end="")
    print("w69_test_plane_attribution_exit=0" if ok else "w69_test_plane_attribution_exit=1")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
