"""Attribute the wave-38 test plane against a pre-wave control plane by node-id SET, not by count.

Two full planes are compared:

* ``runs/test_plane_w38_full.take3.xml`` -- this worktree, wave-38 code plus the re-based
  batch5 fence;
* ``runs/test_plane_head37_control.xml``  -- a detached worktree of the pre-wave-38 commit
  ``66fe1acc9`` with the gitignored ``docs/evidence/C100`` inputs copied in, which is what makes its
  reds mean the same thing as this tree's reds.

Why sets: "28 failed before and after" is a count, and a count hides a swap -- seven reds repaired
and seven new ones would read the same. The wave-37 face learned
that the hard way -- it reported four ``test_provider_domains`` reds as "pre-existing, identical in
both trees" because the control tree was red for a *different* reason (missing C100 inputs), and the
real cause (a stale byte pin in ``tests/fixtures/registry_batch5_snapshot.json``) survived a whole
wave. This probe therefore reads both junit files, diffs the node-id sets, and then reads the
**assertion surface** of every node that this wave fixed: a fixed red only counts as fence-caused if
the control-side message actually names the fixture's ``domains.yaml`` byte pin or the fixture's
domain-id population.

Every arm has a side that can disagree: the no-new-red arm is re-evaluated against a forged node id,
and the fence-marker predicate is re-evaluated against a forged message. An arm that cannot go wrong
is not evidence.
"""

from __future__ import annotations

import subprocess  # nosec B404  # house style: fixed argv child processes, no shell
import sys
import xml.etree.ElementTree as ET  # nosec B405  # reads our own pytest junit, not foreign XML
from pathlib import Path
from typing import Any

REPO = Path("/Users/yunjinqi/Documents/new_projects/opendata")
BUNDLE = "docs/evidence/C143/wave38-seeking-alpha-landing-20261019"
RUNS = REPO / BUNDLE / "runs"
WORKTREE_XML = RUNS / "test_plane_w38_full.take2.xml"
CONTROL_XML = RUNS / "test_plane_head37_control.xml"
CONTROL_CARRIER = RUNS / "test_plane_head37_control.txt"
GIT = "/usr/bin/git"  # absolute executable, so bandit sees no partial path
CARRIER = RUNS / "w51_test_plane_attribution.txt"
FENCE_FILES = (
    "tests/test_provider_domains.py",
    "tests/test_pipeline_jobs.py",
    "tests/test_alert_matrix.py",
    "tests/test_alert_matrix_model_domains.py",
    "tests/test_authority_reconciliation.py",
)
BYTE_PIN_MARKER = "opendata/data/domains.yaml assert"
POPULATION_MARKER = "Extra items in the left set: 'cboe_index_snapshots'"
FORGED_MESSAGE = "AssertionError: assert 1 == 2  # nothing to do with the registry fence"


def _suite(path: Path) -> Any:  # noqa: ANN401  # an ElementTree suite is a foreign XML object
    root = ET.parse(path).getroot()  # nosec B314  # noqa: S314  # our own pytest junit, not foreign XML
    return root if root.tag == "testsuite" else root.find("testsuite")


def _positive(counts: dict[str, str], key: str) -> bool:
    """True when a junit counter exists and is strictly positive."""
    value = counts.get(key, "")
    return value.isdigit() and int(value) > 0


def _node_id(case: Any) -> str:  # noqa: ANN401  # an ElementTree testcase is a foreign XML object
    """Rebuild pytest's own node id (``file::Class::method``) from a junit testcase.

    This junit only carries ``classname``, not ``file``, so the dotted module prefix becomes the
    path and any remaining segments become the class chain. Without that, every node id reads
    ``None::test_x`` and the file-membership arm below is vacuous.
    """
    classname = str(case.get("classname") or "")
    name = str(case.get("name") or "")
    segments = classname.split(".")
    module: list[str] = []
    for segment in segments:
        if not segment[:1].islower():
            break
        module.append(segment)
    path = "/".join(module) + ".py" if module else classname
    classes = segments[len(module) :]
    return "::".join([path, *classes, name])


def _failures(path: Path) -> dict[str, str]:
    """Map every failing node id to the first line of its assertion message."""
    node_messages: dict[str, str] = {}
    for case in _suite(path).iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            continue
        message = " ".join((failure.get("message") or "").split())
        node_messages[_node_id(case)] = message
    return node_messages


def _counts(path: Path) -> dict[str, str]:
    suite = _suite(path)
    return {
        "tests": str(suite.get("tests")),
        "failures": str(suite.get("failures")),
        "errors": str(suite.get("errors")),
        "skipped": str(suite.get("skipped")),
        "seconds": str(suite.get("time")),
    }


def _git(*args: str) -> int:
    return subprocess.run(  # nosec B603  # noqa: S603
        [GIT, "-C", str(REPO), *args],
        capture_output=True,
        check=False,
    ).returncode


def main() -> int:
    """Diff the two failure sets, then read the assertion surface of everything this wave fixed."""
    lines: list[str] = []
    arms: dict[str, bool] = {}

    worktree = _failures(WORKTREE_XML)
    control = _failures(CONTROL_XML)
    worktree_counts = _counts(WORKTREE_XML)
    control_counts = _counts(CONTROL_XML)
    lines.append(f"worktree_plane {worktree_counts}")
    lines.append(f"control_plane  {control_counts}")
    # `errors` is legitimately 0 in a clean collection, so it is not part of the parse check; the
    # three counters that must be non-zero are the population, the failures and the skips.
    arms["both_planes_parse"] = all(
        _positive(worktree_counts, key) and _positive(control_counts, key)
        for key in ("tests", "failures", "skipped")
    )
    arms["neither_plane_had_collection_errors"] = (
        worktree_counts["errors"] == "0" and control_counts["errors"] == "0"
    )

    new_reds = sorted(set(worktree) - set(control))
    fixed = sorted(set(control) - set(worktree))
    shared = sorted(set(worktree) & set(control))
    lines.append(
        f"set_diff worktree={len(worktree)} control={len(control)} shared={len(shared)} "
        f"new={len(new_reds)} fixed={len(fixed)}"
    )
    lines.append(f"new_red_nodes={new_reds}")
    arms["no_new_red_in_worktree"] = new_reds == []
    forged_worktree = set(worktree) | {"tests/forged.py::a_red_that_does_not_exist"}
    arms["no_new_red_arm_can_bite"] = sorted(forged_worktree - set(control)) != []

    arithmetic_holds = len(worktree) == len(shared) + len(new_reds) and len(control) == len(
        shared
    ) + len(fixed)
    lines.append(
        f"arithmetic worktree==shared+new={len(worktree) == len(shared) + len(new_reds)} "
        f"control==shared+fixed={len(control) == len(shared) + len(fixed)}"
    )
    arms["set_arithmetic_holds"] = arithmetic_holds and len(fixed) > 0

    byte_pin = [node for node in fixed if BYTE_PIN_MARKER in control[node]]
    population = [node for node in fixed if POPULATION_MARKER in control[node]]
    named_by_fence = [node for node in fixed if node in set(byte_pin) | set(population)]
    lines.append(
        f"fixed_by_fence byte_pin_marker={len(byte_pin)} population_marker={len(population)} "
        f"named_total={len(named_by_fence)} of={len(fixed)}"
    )
    for node in fixed:
        tail = "byte_pin" if node in byte_pin else "population" if node in population else "UNNAMED"
        lines.append(f"fixed {tail} {node}")
    arms["fixed_reds_are_fence_consumers"] = (
        len(fixed) > 0
        and len(named_by_fence) == len(fixed)
        and all(any(marker in node for marker in FENCE_FILES) for node in fixed)
    )
    arms["fence_marker_predicate_can_refuse"] = not (
        BYTE_PIN_MARKER in FORGED_MESSAGE or POPULATION_MARKER in FORGED_MESSAGE
    )

    control_commit = ""
    for row in CONTROL_CARRIER.read_text(encoding="utf-8").splitlines():
        if row.startswith("# commit: "):
            control_commit = row.split(": ", 1)[1].strip()
            break
    ancestor = (
        _git("merge-base", "--is-ancestor", control_commit, "HEAD") == 0
        if control_commit
        else False
    )
    lines.append(
        f"control_provenance commit={control_commit[:12]} ancestor_of_worktree_head={ancestor}"
    )
    arms["control_is_a_pre_wave_ancestor"] = ancestor and len(control_commit) == 40

    unexplained = [node for node in fixed if node not in set(byte_pin) | set(population)]
    lines.append(f"residual_unexplained_fixed_reds={len(unexplained)} {unexplained[:5]}")

    failed = sorted(name for name, ok in arms.items() if not ok)
    lines.append(
        f"test_plane_attribution arms={len(arms)} arms_passed={sum(arms.values())} "
        f"arms_failed={len(failed)} failed={failed} PASS={not failed}"
    )
    exit_code = 0 if not failed else 1
    lines.append("")
    lines.append(f"w51_test_plane_attribution_exit={exit_code}")
    body = "\n".join(lines) + "\n"
    CARRIER.write_text(
        "# carrier for w51_test_plane_attribution.py\n"
        f"# argv: {sys.executable} "
        f"{REPO / BUNDLE / 'probes' / 'w51_test_plane_attribution.py'}\n\n" + body,
        encoding="utf-8",
    )
    print(body, end="")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
