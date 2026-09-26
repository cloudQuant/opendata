#!/usr/bin/env python3
r"""Gut the frontend collector guard four ways and see which plane notices.

C29 / AC-17 judgment plane (local files only, no network, no warehouse).

Why this file exists: ``make frontend-collection`` judges **today's tree**. On a
tree with nothing silenced it prints PASS, so three of the four ways to destroy
the guard are invisible to the gate item that runs it — and the fourth is only
loud because the guard reads its own input fail-closed. The pytest plane added
in this round (``tests/test_frontend_collection_guard.py``) pins the rules
themselves; this script shows the two planes disagreeing on purpose.

Each mutation is written into the real module, both planes are run against it,
and the file is restored by bytes with a sha256 check — the guard has to come
back exactly as it was found, or the proof proves nothing.
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD = REPO_ROOT / "scripts" / "quality" / "frontend_test_collection.py"
TESTS = REPO_ROOT / "tests" / "test_frontend_collection_guard.py"

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
        key="silence_rule_dead",
        old="    for relative in on_disk:\n",
        new="    for relative in ():\n",
        must_fail=(
            "test_a_file_on_disk_that_is_not_collected_is_named_with_its_rule",
            "test_a_missing_file_without_a_matching_rule_is_still_a_finding",
        ),
        guard_rc=0,
    ),
    Mutation(
        key="allow_list_dead",
        old="        if glob in ALLOWED_EXCLUDES:\n",
        new="        if True:\n",
        must_fail=("test_a_new_exclusion_is_rejected_before_it_hides_anything",),
        guard_rc=0,
    ),
    Mutation(
        key="vacuity_dead",
        old="        vacuous=not on_disk,\n",
        new="        vacuous=False,\n",
        must_fail=("test_no_test_file_on_disk_is_read_as_nothing_measured_rather_than_a_pass",),
        guard_rc=0,
    ),
    Mutation(
        key="reader_dead",
        old='    block = object_block(config_text, "test")\n',
        new="    block = None\n",
        must_fail=(
            "test_the_coverage_exclusions_do_not_read_as_silenced_tests",
            "test_the_shipped_config_declares_only_allowed_boundaries",
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
    lines = output.splitlines()
    failed = {
        line.rsplit("::", 1)[-1].split(" ")[0] for line in lines if line.startswith("FAILED ")
    }
    return rc, failed


def guard_plane() -> tuple[int, str]:
    """Run ``make frontend-collection``'s command and keep its verdict line."""
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
        caught = rc != 0 and not missed
        print()
        print(f"[{mutation.key}] {mutation.old.strip()!r} -> {mutation.new.strip()!r}")
        report("pytest", rc, f"failed={sorted(failed)}")
        report("guard", guard_rc, guard_detail)
        print(f"    verdict      {'CAUGHT' if caught else 'MISSED'}")
        if missed:
            problems.extend(f"{mutation.key}: {name} stayed green" for name in missed)
        if rc == 0:
            problems.append(f"{mutation.key}: the test plane went green")
        if guard_rc != mutation.guard_rc:
            expect = mutation.guard_rc
            problems.append(f"{mutation.key}: guard exited {guard_rc}, expected {expect}")
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
    for problem in problems:
        print(f"PROBLEM: {problem}")
    print(f"PROBLEMS: {len(problems)}")
    return EXIT_CAUGHT if not problems else EXIT_MISSED


if __name__ == "__main__":
    sys.exit(main())
