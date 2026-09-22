#!/usr/bin/env python3
"""A2 zero-tolerance gate (quality spec §1 and §7).

The A1 legacy layer carries debt that is managed by ``quality-ratchet``
(only-down). A2 — code that is **new or modified** — has no snapshot: every
violation blocks.

This script finds the A2 Python files and runs the full self-developed standard
on them:

* ``ruff check`` (full rule set) and ``ruff format --check``
* ``mypy`` with the project's strict settings
* ``bandit``

A2 is defined relative to the recorded baseline commit
(``docs/quality/baseline.json``, written when the A0 baseline commit is created):
the A2 set is everything added or modified since then, plus untracked files.
Set ``A2_BASE_REF`` in CI to the target branch so a pull request is checked
against its merge base instead. The A2 base is the *later* of that merge base
and the recorded baseline, so a PR whose target predates the baseline cannot
sweep frozen A1 files into the A2 set.

The gate fails closed: if a configured baseline cannot be resolved (shallow
clone, stale ``A2_BASE_REF``), it errors instead of reporting "nothing changed".

Before the baseline commit exists, A1 and A2 are indistinguishable, so the gate
reports that state explicitly instead of failing on the whole tree.

The ported tree, ``alembic`` and the frontend are out of scope: they have their
own layers (B / D / E in the quality spec).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil

# subprocess is only ever called with a literal argv and shell disabled (B404).
import subprocess  # nosec B404
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

BASELINE_PATH = "docs/quality/baseline.json"

EXCLUDED_PARTS = frozenset({"akshare", "opendata_http", "alembic", "frontend"})

# Resolved once so the subprocess call never uses a partial executable path.
GIT = shutil.which("git")


def _git(*args: str) -> str:
    """Run git and return its stdout, or "" when git is unavailable or fails."""
    if GIT is None:
        return ""
    result = subprocess.run(  # noqa: S603  # nosec B603
        [GIT, *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def baseline_commit() -> str | None:
    """Return the recorded baseline commit SHA, if one has been established."""
    path = REPO_ROOT / BASELINE_PATH
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return None
    commit = raw.get("commit")
    return commit if isinstance(commit, str) and commit.strip() else None


def _is_a2_candidate(name: str) -> bool:
    if not name.endswith(".py"):
        return False
    if any(part in EXCLUDED_PARTS for part in Path(name).parts):
        return False
    return (REPO_ROOT / name).is_file()


def changed_files(base: str | None) -> list[str]:
    """Return A2 Python files as repo-relative POSIX paths, sorted.

    The comparison is against the *working tree* rather than ``base..HEAD``, so
    uncommitted local edits are gated too. Comparing two commits would silently
    pass during development and only fire after a commit — the wrong feedback
    loop for a pre-commit-style gate.
    """
    names: set[str] = set()
    revision = base or "HEAD"
    names.update(_git("diff", "--name-only", "--diff-filter=ACMR", revision).splitlines())
    names.update(_git("ls-files", "--others", "--exclude-standard").splitlines())
    return sorted({name.strip() for name in names if _is_a2_candidate(name.strip())})


class BaselineError(RuntimeError):
    """Raised when the A2 baseline cannot be resolved.

    The gate must fail closed: a shallow clone (``fetch-depth: 1``) or a stale
    ``A2_BASE_REF`` would otherwise make the check silently pass by reporting
    "no A2 files changed".
    """


def _resolve_ref(ref: str) -> str | None:
    """Return ``ref`` or ``origin/<ref>``, whichever resolves to a commit."""
    for candidate in (ref, f"origin/{ref}"):
        resolved = _git("rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").strip()
        if resolved:
            return candidate
    return None


def _baseline_is_ahead(merge_base: str, baseline: str) -> bool:
    """True when the A0 baseline is a descendant of ``merge_base``.

    A merge base *older* than the A0 baseline (e.g. a pull request targeting a
    branch that predates the baseline) would drag pre-baseline A1 files into the
    A2 set and apply zero tolerance to legacy debt. The later of the two wins.
    """
    return _git("rev-list", "--count", f"{merge_base}..{baseline}").strip() not in ("", "0")


def _checked_baseline() -> str | None:
    """Return the recorded baseline commit, failing closed if it is unreachable."""
    baseline = baseline_commit()
    if baseline is None:
        return None
    if not _git("rev-parse", "--verify", "--quiet", f"{baseline}^{{commit}}").strip():
        raise BaselineError(
            f"baseline commit {baseline} is not present in this clone; "
            "check out with full history (actions/checkout with fetch-depth: 0)."
        )
    return baseline


def resolve_files(explicit: list[str] | None) -> list[str] | None:
    """Resolve the A2 file list.

    The A2 base is the later of the A0 baseline commit (``docs/quality/baseline.json``)
    and, when ``A2_BASE_REF`` is set, the merge base with that ref. The earlier
    one would sweep already-frozen A1 files into the A2 set.

    Returns:
        The A2 file paths, or ``None`` when no baseline has been recorded yet
        (i.e. before milestone A0.11, when A1 and A2 are indistinguishable).

    Raises:
        BaselineError: When a baseline is configured but cannot be resolved.
    """
    if explicit is not None:
        return [name for name in explicit if _is_a2_candidate(name)]

    baseline = _checked_baseline()

    env_base = os.environ.get("A2_BASE_REF", "").strip()
    if not env_base:
        return None if baseline is None else changed_files(baseline)

    resolved = _resolve_ref(env_base)
    if resolved is None:
        raise BaselineError(
            f"A2_BASE_REF={env_base!r} does not resolve to a commit; "
            "fetch it first (actions/checkout with fetch-depth: 0)."
        )
    merge_base = _git("merge-base", resolved, "HEAD").strip()
    if not merge_base:
        raise BaselineError(f"no merge base between {resolved} and HEAD")
    if baseline is not None and _baseline_is_ahead(merge_base, baseline):
        return changed_files(baseline)
    return changed_files(merge_base)


def _run(args: list[str]) -> tuple[bool, str]:
    result = subprocess.run(  # noqa: S603  # nosec B603
        args,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode == 0, output.strip()


def _bandit(files: list[str]) -> tuple[bool, str]:
    ok, output = _run(
        [sys.executable, "-m", "bandit", "-c", "bandit.yaml", "-f", "json", "-q", *files]
    )
    if not ok:
        return False, output
    try:
        payload = json.loads(output or "{}")
    except json.JSONDecodeError:
        return True, ""
    results = payload.get("results", [])
    if not isinstance(results, list):
        return True, ""
    if results:
        return False, "\n".join(
            f"  {item.get('filename')}:{item.get('line_number')} "
            f"{item.get('test_id')} {item.get('issue_text')}"
            for item in results
        )
    return True, ""


def run(explicit: list[str] | None = None) -> int:
    """Run the A2 zero-tolerance gate and report the result."""
    try:
        files = resolve_files(explicit)
    except BaselineError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1

    if files is None:
        print(
            "NOTE: A2 gating is inactive until the baseline commit is recorded in "
            f"{BASELINE_PATH} (created at milestone A0.11).\n"
            "      Before that commit the whole tree is untracked, so A1 and A2 "
            "cannot be told apart; A1 debt is already frozen by quality-ratchet."
        )
        return 0

    if not files:
        print("OK: no A2 files changed (nothing to check).")
        return 0

    print(f"A2 files: {len(files)}")
    for name in files:
        print(f"  {name}")
    print()

    failures: list[str] = []

    ok, output = _run([sys.executable, "-m", "ruff", "check", *files])
    print(f"{'ok  ' if ok else 'FAIL'} ruff check")
    if not ok:
        failures.append("ruff check")
        print(output)

    ok, output = _run([sys.executable, "-m", "ruff", "format", "--check", *files])
    print(f"{'ok  ' if ok else 'FAIL'} ruff format --check")
    if not ok:
        failures.append("ruff format")
        print(output)

    # mypy and bandit follow the spec's layer definitions: test code is C layer,
    # where mypy is gradual (not enforced) and bandit's assert rule is exempt.
    typed_files = [name for name in files if not name.startswith("tests/")]

    if typed_files:
        ok, output = _run(
            [
                sys.executable,
                "-m",
                "mypy",
                "--no-color-output",
                "--no-error-summary",
                "--follow-imports=silent",
                *typed_files,
            ]
        )
        print(f"{'ok  ' if ok else 'FAIL'} mypy")
        if not ok:
            failures.append("mypy")
            print(output)

        ok, output = _bandit(typed_files)
        print(f"{'ok  ' if ok else 'FAIL'} bandit")
        if not ok:
            failures.append("bandit")
            print(output)
    else:
        print("skip mypy / bandit (no non-test A2 files)")

    if failures:
        print(f"\nFAIL: A2 gate failed on: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nOK: A2 files meet the full A2 standard (ruff + format + mypy + bandit).")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run the A2 gate from the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--files",
        nargs="*",
        default=None,
        help="check exactly these files instead of the computed A2 set",
    )
    args = parser.parse_args(argv)
    return run(args.files)


if __name__ == "__main__":
    raise SystemExit(main())
