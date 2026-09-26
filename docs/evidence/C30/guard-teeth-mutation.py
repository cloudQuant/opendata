#!/usr/bin/env python3
r"""Gut the frontend e2e guard seven ways and see which plane notices.

C30 / AC-17 teeth check (local files only, no network, no warehouse).

Why this file exists: the gate item runs the guard against **today's tree**, and
today's tree is sound. So the gate cannot see the destruction of a rule that has
nothing to say about this tree — it is only heard from ``scripts/quality`` when
the tree grows a finding. The pytest plane added in this round
(``tests/test_frontend_e2e_guard.py``) pins the rules themselves on synthetic
specs, and this script shows the two planes disagreeing on purpose.

Each mutation is written into the real module, both planes are run against it,
and the file is restored by bytes with a sha256 check — the guard has to come
back exactly as it was found, or the proof proves nothing.

The expected result is not "every mutation is caught by the gate". Six of the
seven leave ``make frontend-e2e`` green, and that is the finding: the reason the
test plane exists is that the gate's own reading cannot cover for it.

Run: python -u docs/evidence/C30/guard-teeth-mutation.py
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "scripts" / "quality" / "frontend_e2e_plane.py"
TESTS = REPO_ROOT / "tests" / "test_frontend_e2e_guard.py"

EXIT_CAUGHT: Final = 0
EXIT_MISSED: Final = 1


@dataclass(frozen=True)
class Mutation:
    """One way to make the guard stop reporting, and what should notice.

    Attributes:
        key: Short name used in the report.
        old: Literal text in the module that makes the rule work.
        new: The same text with the rule made dead.
        must_fail: Test names that have to fail while the mutation is in place.
        guard_rc: What the gate item itself exits with (0 = it would go green).
    """

    key: str
    old: str
    new: str
    must_fail: tuple[str, ...]
    guard_rc: int


MUTATIONS: Final = (
    Mutation(
        key="guarded_rule_dead",
        old='        elif verdict in {"GUARDED", "PARTLY-GUARDED"}:\n',
        new='        elif verdict in {"", ""}:\n',
        must_fail=(
            "test_an_assertion_that_only_runs_when_a_lookup_succeeds_is_a_finding",
            "test_an_assertion_beside_the_if_is_not_counted_as_guarded",
            "test_an_assertion_that_only_lives_in_the_else_branch_is_a_finding",
            "test_a_plane_with_a_finding_exits_one",
        ),
        guard_rc=0,
    ),
    Mutation(
        key="vacuity_rule_dead",
        old="        if self.vacuous_against:\n",
        new="        if False:\n",
        must_fail=("test_asserting_a_substring_of_the_visited_path_is_vacuous",),
        guard_rc=0,
    ),
    Mutation(
        key="no_assertion_rule_dead",
        old='        elif verdict == "NO-ASSERTION":\n',
        new='        elif verdict == "":\n',
        must_fail=("test_a_test_that_never_asserts_is_a_finding",),
        guard_rc=0,
    ),
    Mutation(
        key="unnamed_skip_rule_dead",
        old='        if verdict == "SKIPPED" and leaf.title not in gaps:\n',
        new="        if False:\n",
        must_fail=("test_a_skip_without_a_named_reason_is_a_finding",),
        guard_rc=0,
    ),
    Mutation(
        key="listed_gap_rule_dead",
        old="    for listed in sorted(gaps - skipped):\n",
        new="    for listed in ():\n",
        must_fail=(
            "test_a_listed_gap_that_started_running_is_a_finding_too",
            "test_a_listed_gap_whose_test_disappeared_says_so",
        ),
        guard_rc=0,
    ),
    Mutation(
        key="runner_crosscheck_dead",
        old="        mismatch = listed != len(leaves)\n",
        new="        mismatch = False\n",
        must_fail=("test_a_size_disagreement_with_the_runner_is_a_finding",),
        guard_rc=0,
    ),
    Mutation(
        # The one mutation the gate does notice: the guard reads its input
        # fail-closed, so a plane that yields no leaf is exit 2, not a pass.
        key="classifier_dead",
        old="        match = TEST_RE.match(raw)\n",
        new="        match = None\n",
        must_fail=(
            "test_a_clean_assertion_is_read_as_a_verdict",
            "test_the_guard_reads_the_shipped_e2e_plane",
            "test_the_gap_list_is_exactly_the_shipped_skips",
            "test_the_shipped_plane_passes_the_guard_end_to_end",
        ),
        guard_rc=2,
    ),
)


def run(cmd: list[str]) -> tuple[int, str]:
    """Run a command in the repo and hand back its exit code and output."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return result.returncode, f"{result.stdout}{result.stderr}"


def pytest_plane() -> tuple[int, set[str]]:
    """Run the guard's own test file and return the failing test names."""
    rc, output = run([sys.executable, "-m", "pytest", str(TESTS), "-q", "--no-cov", "-rf"])
    failed = {
        line.rsplit("::", 1)[-1].split(" ")[0]
        for line in output.splitlines()
        if line.startswith("FAILED ")
    }
    return rc, failed


def guard_plane() -> tuple[int, str]:
    """Run ``make frontend-e2e``'s first command and keep its verdict line."""
    rc, output = run([sys.executable, str(GUARD)])
    lines = [line for line in output.splitlines() if line]
    return rc, lines[-1] if lines else "(no output)"


def report(label: str, rc: int, detail: str) -> None:
    """Print one plane's reading on one line."""
    print(f"    {label:<12} rc={rc}  {detail}")


def main() -> int:
    """Apply each mutation, read both planes, restore the module, and judge."""
    original = GUARD.read_bytes()
    digest = hashlib.sha256(original).hexdigest()[:12]
    print(f"guard module: {GUARD.relative_to(REPO_ROOT)} (sha256-12 {digest})")
    rc, failed = pytest_plane()
    print(f"baseline      pytest rc={rc} failed={sorted(failed)}")
    if rc != 0:
        print("PROBLEM: the test plane was already red before any mutation.")
        return EXIT_MISSED
    baseline_rc, baseline_detail = guard_plane()
    print(f"baseline      guard rc={baseline_rc} {baseline_detail}")

    problems: list[str] = []
    invisible: list[str] = []
    for mutation in MUTATIONS:
        text = original.decode("utf-8")
        if text.count(mutation.old) != 1:
            problems.append(f"{mutation.key}: anchor found {text.count(mutation.old)} times")
            continue
        GUARD.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")
        try:
            rc, failed = pytest_plane()
            guard_rc, guard_detail = guard_plane()
        finally:
            GUARD.write_bytes(original)
        missed = [name for name in mutation.must_fail if name not in failed]
        print()
        print(f"[{mutation.key}] {mutation.old.strip()!r} -> {mutation.new.strip()!r}")
        report("pytest", rc, f"failed={sorted(failed)}")
        report("guard", guard_rc, guard_detail)
        if missed:
            problems.extend(f"{mutation.key}: {name} stayed green" for name in missed)
        if rc == 0:
            problems.append(f"{mutation.key}: the test plane went green")
        if guard_rc != mutation.guard_rc:
            problems.append(
                f"{mutation.key}: guard exited {guard_rc}, expected {mutation.guard_rc}"
            )
        if guard_rc == 0:
            invisible.append(mutation.key)
        if hashlib.sha256(GUARD.read_bytes()).hexdigest()[:12] != digest:
            problems.append(f"{mutation.key}: module bytes did not come back")

    print()
    rc, failed = pytest_plane()
    guard_rc, guard_detail = guard_plane()
    print()
    print(f"restored      pytest rc={rc} failed={sorted(failed)}")
    print(f"restored      guard  rc={guard_rc} {guard_detail}")
    if rc != 0 or guard_rc != 0:
        problems.append("the restored plane is not green")

    print()
    blind = f"{len(invisible)}/{len(MUTATIONS)}"
    print(f"gate-blind mutations (guard still exits 0): {blind}")
    print(f"  {invisible}")
    print("That is the argument for tests/test_frontend_e2e_guard.py, not against it.")
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print(f"PROBLEMS: {len(problems)}")
    return EXIT_CAUGHT if not problems else EXIT_MISSED


if __name__ == "__main__":
    sys.exit(main())
