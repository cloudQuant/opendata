#!/usr/bin/env python
"""C59 counterfact runner: break each declared guard, prove it reads red, restore.

Read-only toward the warehouse. The subprocess legs inherit ``MYSQL_*`` and
``DATA_MYSQL_*`` pointed at 127.0.0.1:1 (connection refused), so even the breaks
that open the live plane cannot reach a real server. Every patched file is
restored byte-for-byte and verified by sha256 before the runner exits.

Usage: ``python docs/evidence/C59/run_counterfacts.py``
"""

import hashlib
import os
import subprocess  # nosec B404
import sys
from pathlib import Path

#: this file sits at ``<repo>/docs/evidence/C59/``.
REPO = Path(__file__).resolve().parents[3]
GUARD = "tests/test_e2e_opt_in_guard.py"
DEAD_DB = {
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "1",
    "DATA_MYSQL_HOST": "127.0.0.1",
    "DATA_MYSQL_PORT": "1",
    "PYTHONPATH": str(REPO),
}

#: name, file, exact text to remove, text to put in its place.
BREAKS: list[tuple[str, str, str, str]] = [
    (
        "B1 a truthy opt-in releases the plane",
        "tests/conftest.py",
        'return os.environ.get(LIVE_E2E_OPT_IN_ENV, "").strip() == LIVE_E2E_OPT_IN_TOKEN',
        "return bool(os.environ.get(LIVE_E2E_OPT_IN_ENV))",
    ),
    (
        "B2 the hook skips nothing (the C58 command runs live legs)",
        "tests/conftest.py",
        "    if live_e2e_allowed():\n        return\n",
        "    return\n",
    ),
    (
        "B3 the live-warehouse leg is unmarked again",
        "tests/test_freshness.py",
        "@pytest.mark.e2e\nclass TestPartitionPlanCollector",
        "class TestPartitionPlanCollector",
    ),
    (
        "B4 a make plane forgets the marker filter",
        "Makefile",
        'test:\n\tpytest tests -n 8 -m "not e2e"\n',
        "test:\n\tpytest tests -n 8\n",
    ),
    (
        "B5 the e2e marker is no longer declared",
        "pytest.ini",
        "    e2e: mark test as end-to-end (requires a running application)\n",
        "",
    ),
]

PYTEST_TAIL = 24


def sha(name: str) -> str:
    """First twelve hex digits of the file's sha256."""
    return hashlib.sha256((REPO / name).read_bytes()).hexdigest()[:12]


def census(names: list[str]) -> dict[str, str]:
    """Digest of every patched file, for the before/after comparison."""
    return {name: sha(name) for name in names}


def run(label: str) -> subprocess.CompletedProcess[str]:
    """Run the guard module and echo the tail of its output verbatim."""
    command = [
        sys.executable,
        "-m",
        "pytest",
        GUARD,
        "-q",
        "--no-cov",
        "-m",
        "not e2e",
        "-p",
        "no:cacheprovider",
    ]
    print(f"\n### {label}")
    print(f"command: {' '.join(command[1:])}")
    print("env: MYSQL_HOST=DATA_MYSQL_HOST=127.0.0.1  MYSQL_PORT=DATA_MYSQL_PORT=1")
    result = subprocess.run(  # noqa: S603  # nosec B603  # fixed argv
        command,
        cwd=REPO,
        env={**os.environ, **DEAD_DB},
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    lines = [line for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    print("\n".join(lines[-PYTEST_TAIL:]))
    print(f"PYTEST_EXIT={result.returncode}")
    return result


def patch(label: str, name: str, old: str, new: str) -> str:
    """Apply one declared break and hand back the text that restores it."""
    path = REPO / name
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"anchor for {label} matched {text.count(old)} times in {name}")
    path.write_text(text.replace(old, new), encoding="utf-8")
    return text


def main() -> int:
    """Run the clean pass, every break, the restore check, then the clean pass again."""
    names = sorted({name for _, name, _, _ in BREAKS})
    original = census(names)
    print("=== patched files, sha256[:12] before ===")
    for name in names:
        print(f"  {original[name]}  {name}")

    clean_green = run("clean tree: every guard reads green").returncode == 0
    verdicts: list[tuple[str, bool]] = [("clean tree", clean_green)]
    for label, name, old, new in BREAKS:
        restore = patch(label, name, old, new)
        verdicts.append((label, run(label).returncode != 0))
        (REPO / name).write_text(restore, encoding="utf-8")

    print("\n=== verdicts (expected: green, then red for every break) ===")
    for label, satisfied in verdicts:
        print(f"  {'OK ' if satisfied else 'BAD'}  {label}")
    after = census(names)
    for name in names:
        state = "identical" if after[name] == original[name] else "DRIFT"
        print(f"  restore {name}: {original[name]} -> {after[name]} {state}")

    final_green = run("restored tree: green again").returncode == 0
    ok = all(s for _, s in verdicts) and final_green and original == after
    print(f"\nCOUNTERFACT_RUNNER_EXIT={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
