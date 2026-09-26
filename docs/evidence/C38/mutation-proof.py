"""C38a mutation proof: every new assertion has to be able to fail.

The round adds one test for a background failure path that used to be covered
only by luck. A test that passes against both the fixed and the broken source
would restore the exact invisibility this round is closing, so each mutation
here is the smallest edit that breaks one claimed invariant, and the expected
reading is a red run naming one specific test.

M4 mutates the test file instead of the source: it asks whether the wait is
load-bearing or just decoration.

Usage:
    python docs/evidence/C38/mutation-proof.py

Run from the repository root. Every touched file is restored and compared by
sha256, so a crash mid-run still leaves the working tree clean.
"""

from __future__ import annotations

import hashlib
import subprocess  # nosec B404  # the driver runs pytest, nothing else
import sys
from pathlib import Path
from typing import Final

DATA: Final = Path("opendata/api/data.py")
TESTS: Final = Path("tests/test_api_data_full.py")
TARGETS: Final = (DATA, TESTS)

#: The download handler's reporting block, quoted for the two mutations that
#: target it.
HANDLER: Final = (
    "            except Exception as e:\n"
    "                logger.error(\n"
    '                    "Background download failed for execution {}: {}",\n'
    "                    exec_id,\n"
    "                    e,\n"
    "                )\n"
)

#: ``(name, file, kind, claim, old, new, -k pattern)`` for each step.
#:
#: ``teeth`` must turn its named test red - it mutates the source a claim is
#: about. ``probe`` only reports a reading: M4 asks whether the test's own wait
#: is load-bearing, and a green answer is information rather than a defect.
MUTATIONS: Final = (
    (
        "M1",
        DATA,
        "teeth",
        "the download log line names the execution and the cause",
        '"Background download failed for execution {}: {}"',
        '"Background download failed for execution %s: %s"',
        "test_trigger_download_logs_background_failure",
    ),
    (
        "M2",
        DATA,
        "teeth",
        "the fallback callback names the exception type and its message",
        '"Unhandled exception in background task: {}: {}"',
        '"Unhandled exception in background task: %s: %s"',
        "test_failed_task_is_attributed_by_type_and_reason",
    ),
    (
        "M3",
        DATA,
        "teeth",
        "a cancelled task is filtered before its exception is read",
        "    if task.cancelled():\n        return\n    exc = task.exception()\n",
        "    exc = task.exception()\n    if task.cancelled():\n        return\n",
        "test_cancelled_task_logs_nothing",
    ),
    (
        "M4",
        TESTS,
        "probe",
        "whether the wait is what makes the judgement deterministic",
        'assert await _wait_until(failed), f"no background failure logged in {log_lines}"',
        'assert failed(), "no background failure logged without yielding"',
        "test_trigger_download_logs_background_failure",
    ),
    (
        "M5",
        DATA,
        "teeth",
        "a failed download is reported through the logger, not swallowed",
        HANDLER,
        "            except Exception as e:\n                print(e)\n",
        "test_trigger_download_logs_background_failure",
    ),
    (
        "M6",
        DATA,
        "teeth",
        "the failure is handled once, so the callback adds no second line",
        HANDLER,
        f"{HANDLER}                raise\n",
        "test_trigger_download_logs_background_failure",
    ),
)


def digests() -> dict[Path, str]:
    """Return the sha256 of every file this script may touch."""
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in TARGETS}


def apply_mutation(path: Path, old: str, new: str) -> None:
    """Replace ``old`` with ``new`` in ``path``, requiring one occurrence.

    Args:
        path: File to edit.
        old: Text to replace.
        new: Replacement text.

    Raises:
        RuntimeError: If ``old`` is not present exactly once.
    """
    source = path.read_text(encoding="utf-8")
    hits = source.count(old)
    if hits != 1:
        msg = f"{path}: expected 1 occurrence, found {hits}"
        raise RuntimeError(msg)
    path.write_text(source.replace(old, new), encoding="utf-8")


def run_tests(pattern: str) -> tuple[int, str]:
    """Run the data-API tests for one node id, without coverage.

    Args:
        pattern: ``pytest -k`` expression.

    Returns:
        ``(exit code, output)``.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        str(TESTS),
        "--no-cov",
        "-q",
        "-p",
        "no:cacheprovider",
        "-k",
        pattern,
    ]
    print("$ " + " ".join(command))
    proc = subprocess.run(  # noqa: S603  # nosec B603
        command,
        capture_output=True,
        text=True,
        check=False,
        encoding="utf-8",
    )
    return proc.returncode, proc.stdout + proc.stderr


def summary(output: str) -> str:
    """Pull the reading out of a pytest run: failed test ids and the tail line.

    Args:
        output: Combined pytest stdout/stderr.

    Returns:
        Human-readable lines, indented for the archive.
    """
    lines = [
        line.strip()
        for line in output.splitlines()
        if line.startswith("FAILED") or line.startswith("ERROR")
    ]
    tail = [line.strip() for line in output.splitlines() if " passed" in line or " failed" in line]
    for line in [*lines, tail[-1] if tail else "<no summary line>"]:
        print(f"    {line}")
    return "\n".join(lines)


def main() -> int:
    """Run base, every mutation, and the restore check.

    Returns:
        0 when each ``teeth`` mutation turned its named test red and the tree
        restored byte for byte, 1 otherwise. ``probe`` steps only report.
    """
    base = digests()
    problems: list[str] = []
    teeth = 0

    print("=== base (unmutated) ===")
    code, output = run_tests("trigger_download or task")
    summary(output)
    if code != 0:
        problems.append("base run is not green")

    for name, path, kind, claim, old, new, pattern in MUTATIONS:
        print(f"\n=== {name} ({kind}): {path} - {claim} ===")
        try:
            apply_mutation(path, old, new)
        except RuntimeError as error:
            print(f"    mutation not applied: {error}")
            problems.append(f"{name}: {error}")
            continue
        code, output = run_tests(pattern)
        summary(output)
        if kind == "teeth":
            teeth += 1
            if code == 0:
                problems.append(f"{name}: {pattern} stayed green, so the claim is not pinned")
        else:
            print(
                f"    probe reading: {'red' if code else 'green'} - the wait is "
                f"{'load-bearing' if code else 'not load-bearing'} in a single-file run"
            )
        path.write_text(path.read_text(encoding="utf-8").replace(new, old), encoding="utf-8")

    print("\n=== restored ===")
    after = digests()
    for path in TARGETS:
        same = after[path] == base[path]
        print(f"    {path} identical={same} sha256={after[path][:16]}...")
        if not same:
            problems.append(f"{path} did not restore")

    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(
        f"\nOK: base green, {teeth}/{teeth} teeth mutations failed their expected test, "
        "sources restored."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
