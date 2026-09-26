#!/usr/bin/env python3
r"""Put the guard through the tree it actually runs on, then put the tree back.

C29 / AC-17 end-to-end surface (local files only, no network, no warehouse).

Why this file exists: the harness next to it drives ``judge()`` with injected
sets, which proves the rules but not the wiring — a guard whose collector call
or config reader were broken would still pass that. Here the mutations are made
in the real ``frontend/`` tree:

* step 1 adds a test file and expects the guard to follow it (PASS, counts +1);
* step 2 excludes exactly that file the way the A0 rule excluded its five, and
  expects the guard to go red on both rules and name it.

Both mutations are undone in a ``finally`` block and the script reports whether
the tree really came back, because a probe that leaves a repo dirty is worse
than the silence it is measuring.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND = REPO_ROOT / "frontend"
CONFIG = FRONTEND / "vite.config.ts"
PROBE_TEST = FRONTEND / "src" / "__tests__" / "zz-collection-probe.test.ts"
GUARD = REPO_ROOT / "scripts" / "quality" / "frontend_test_collection.py"

PROBE_BODY: Final = """it('is a probe the guard must follow', () => {
  expect(1 + 1).toBe(2)
})
"""
#: An exact-path exclusion — the smallest possible version of the A0 rule.
#: Relative to ``frontend/``, which is how both vitest and the guard resolve it.
ANCHOR: Final = "'dist/**',"
PROBE_REL: Final = PROBE_TEST.relative_to(FRONTEND).as_posix()

EXIT_PASS: Final = 0
EXIT_FAIL: Final = 1
EXIT_UNREADABLE: Final = 2


def run_guard() -> tuple[int, str]:
    """Run the guard the way the gate does and hand back its reading."""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [sys.executable, str(GUARD)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    return result.returncode, f"{result.stdout}{result.stderr}"


def counts(output: str) -> tuple[int, int]:
    """Read the two reported file counts out of a guard run."""
    on_disk = collected = -1
    for line in output.splitlines():
        if "on disk (src):" in line:
            on_disk = int(line.split(":")[-1].strip().split()[0])
        if "collected    :" in line:
            collected = int(line.split(":")[-1].strip().split()[0])
    return on_disk, collected


def main() -> int:
    """Run both steps, restore the tree, and report what each one read."""
    git = shutil.which("git")
    if git is None:
        print("UNREADABLE: git is unavailable, so the tree cannot be checked back in.")
        return EXIT_UNREADABLE
    before = subprocess.run(  # noqa: S603  # nosec B603
        [git, "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    ).stdout
    config_backup = CONFIG.read_bytes()

    problems: list[str] = []
    try:
        baseline_rc, baseline_out = run_guard()
        baseline_counts = counts(baseline_out)
        print(f"step 0  guard on the tree as it stands: rc={baseline_rc} counts={baseline_counts}")
        if baseline_rc != EXIT_PASS:
            problems.append(f"step 0 expected rc=0, got {baseline_rc}")

        PROBE_TEST.write_text(PROBE_BODY, encoding="utf-8")
        rc, output = run_guard()
        probe_counts = counts(output)
        print(f"step 1  probe test added: rc={rc} counts={probe_counts}")
        print("        the run's own line: " + output.splitlines()[-1])
        if rc != EXIT_PASS:
            problems.append(f"step 1 expected rc=0, got {rc}")
        if probe_counts != (baseline_counts[0] + 1, baseline_counts[1] + 1):
            problems.append(
                f"step 1 did not follow the new file: {probe_counts} after {baseline_counts}"
            )

        text = CONFIG.read_text(encoding="utf-8")
        CONFIG.write_text(text.replace(ANCHOR, f"{ANCHOR}\n      '{PROBE_REL}',", 1))
        rc, output = run_guard()
        named = [line for line in output.splitlines() if PROBE_REL in line]
        print(f"step 2  probe excluded by name: rc={rc}")
        for line in output.splitlines():
            if line.startswith(("SILENCED", "UNAUTHORISED", "VACUOUS")):
                print(f"        {line}")
        if rc != EXIT_FAIL:
            problems.append(f"step 2 expected rc=1, got {rc}")
        if not any(line.startswith("SILENCED") for line in output.splitlines()):
            problems.append("step 2 did not report a SILENCED line")
        if not any(line.startswith("UNAUTHORISED") for line in output.splitlines()):
            problems.append("step 2 did not report an UNAUTHORISED line")
        if not named:
            problems.append("step 2 did not name the excluded file")
    finally:
        PROBE_TEST.unlink(missing_ok=True)
        CONFIG.write_bytes(config_backup)

    restored = CONFIG.read_bytes() == config_backup
    after = subprocess.run(  # noqa: S603  # nosec B603
        [git, "status", "--porcelain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    ).stdout
    print()
    print(f"config bytes restored : {'YES' if restored else 'NO'}")
    print(f"probe file gone       : {'YES' if not PROBE_TEST.exists() else 'NO'}")
    print(
        f"worktree unchanged    : {'YES' if after == before else 'NO'}"
        f" (hash before {hashlib.sha256(before.encode()).hexdigest()[:12]},"
        f" after {hashlib.sha256(after.encode()).hexdigest()[:12]})"
    )
    if not restored or after != before:
        problems.append("the probe left the tree dirty")
    for problem in problems:
        print(f"PROBE_MISMATCH: {problem}")
    print(f"PROBLEMS: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
