#!/usr/bin/env python3
"""Fail the gate when a frontend test file exists but nothing collects it.

C29 / AC-17 judgment plane. One module, run by ``make frontend-collection``
inside ``make gate`` and imported by ``docs/evidence/C29/guard-falsification.py``.

Why this file exists: the frontend plane closed green on
``Test Files 8 passed (8) / Tests 79 passed (79)`` for every round since A0,
while ``vite.config.ts`` carried ``exclude: ['src/components/common/__tests__/**']``
— five files and eighteen cases that no run ever collected. A collector rule
can only be seen from the outside: the passing line and the silence produce
the same green, so the count of files on disk has to be compared against the
count vitest admits, and that comparison must itself be able to fail.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Test-file shapes vitest's default ``include`` would reach.
TEST_SUFFIXES: Final = (".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx")
#: The only exclusions the collector is allowed to have: a separate runner's
#: specs, and installed or build output. Any other rule has to be added here
#: on purpose, which makes a new silence a reviewable act rather than a diff
#: that passes the gate.
ALLOWED_EXCLUDES: Final = frozenset({"e2e/**", "node_modules/**", "dist/**"})

_SKIP_DIRS: Final = frozenset({"node_modules", "dist", "coverage", ".vite"})
_EXCLUDE_ARRAY_RE = re.compile(r"exclude:\s*\[(?P<body>[^\]]*)\]", re.S)
_GLOB_RE = re.compile(r"['\"](?P<glob>[^'\"]+)['\"]")


@dataclass(frozen=True)
class Verdict:
    """What the collector and the disk agree on, and where they do not.

    Attributes:
        reasons: Failure reasons; empty means the plane is fully collected.
        vacuous: The check could not read the plane, so it measured nothing.
        on_disk: Repo-relative test files under ``src`` found on disk.
        collected: The subset vitest admits to the run.
    """

    reasons: tuple[str, ...]
    vacuous: bool
    on_disk: tuple[str, ...]
    collected: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """True when the plane was readable and nothing was silenced."""
        return not self.reasons and not self.vacuous


def on_disk_tests(frontend: Path, subdir: str = "src") -> list[str]:
    """List test files under ``frontend/src`` as paths relative to ``frontend``."""
    root = frontend / subdir
    if not root.is_dir():
        return []
    found: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or not path.name.endswith(TEST_SUFFIXES):
            continue
        relative = path.relative_to(frontend).as_posix()
        if any(part in _SKIP_DIRS for part in Path(relative).parts):
            continue
        found.append(relative)
    return found


def object_block(config_text: str, key: str) -> str | None:
    """Return the body of the object literal assigned to ``key`` in a config.

    Braces are matched rather than the text being sliced at a guessed
    indentation, because the only failure mode worth having here is the loud
    one: an unreadable config returns ``None`` and the caller reports VACUOUS
    instead of concluding that nothing is excluded.
    """
    anchor = re.search(rf"^\s*{key}:\s*\{{", config_text, re.MULTILINE)
    if anchor is None:
        return None
    depth = 0
    for index in range(anchor.end() - 1, len(config_text)):
        char = config_text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return config_text[anchor.end() : index]
    return None


def parse_test_excludes(config_text: str) -> tuple[list[str], bool]:
    """Read the collector's exclude globs out of a Vite config.

    Returns:
        The globs, and whether a ``test.exclude`` array was found at all. Only
        the part of the ``test`` block that precedes its nested ``coverage``
        object is read, so a ``coverage.exclude`` entry — which hides a file
        from the denominator, not from the run — cannot be mistaken for a
        silenced test.
    """
    block = object_block(config_text, "test")
    if block is None:
        return [], False
    coverage_at = block.find("coverage:")
    head = block[:coverage_at] if coverage_at != -1 else block
    array = _EXCLUDE_ARRAY_RE.search(head)
    if array is None:
        return [], False
    return [match.group("glob") for match in _GLOB_RE.finditer(array.group("body"))], True


def vitest_collected(frontend: Path) -> list[str] | None:
    """Ask vitest which files it collects, or ``None`` when it cannot answer."""
    npx = shutil.which("npx")
    if npx is None:
        return None
    result = subprocess.run(  # noqa: S603  # nosec B603
        [npx, "vitest", "list", "--json"],
        cwd=frontend,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    collected: list[str] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        raw = str(entry.get("file", ""))
        if not raw:
            continue
        absolute = Path(raw)
        try:
            collected.append(absolute.relative_to(frontend).as_posix())
        except ValueError:
            collected.append(absolute.as_posix())
    return sorted(set(collected))


def judge(on_disk: list[str], collected: list[str], excludes: list[str]) -> Verdict:
    """Decide whether the collector hides anything, from three readings.

    Rules, each covering a failure mode the others cannot see:

    * a file on disk that vitest does not admit is silenced **today**;
    * an exclude rule outside :data:`ALLOWED_EXCLUDES` is **unauthorised** —
      it may hide nothing yet, and that is exactly the state in which a
      silence is written down;
    * no test file on disk means the check has nothing to compare, which is
      reported as vacuous instead of as a passing plane.

    ``e2e/**`` matches neither rule, so the playwright plane stays a stated
    boundary rather than being dragged in as a defect.
    """
    reasons: list[str] = []
    admitted = set(collected)
    for relative in on_disk:
        if relative in admitted:
            continue
        culprit = next((glob for glob in excludes if fnmatch.fnmatch(relative, glob)), None)
        rule = f"matched by exclude '{culprit}'" if culprit else "matched by no exclude rule"
        reasons.append(f"SILENCED: {relative} is on disk and never collected ({rule})")
    for glob in excludes:
        if glob in ALLOWED_EXCLUDES:
            continue
        reasons.append(f"UNAUTHORISED: exclude '{glob}' hides tests and is not an allowed boundary")
    return Verdict(
        reasons=tuple(reasons),
        vacuous=not on_disk,
        on_disk=tuple(on_disk),
        collected=tuple(collected),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the comparison against a frontend tree and set the exit code."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--frontend-dir", default=str(REPO_ROOT / "frontend"))
    args = parser.parse_args(argv)
    frontend = Path(args.frontend_dir).resolve()

    config = frontend / "vite.config.ts"
    if not config.is_file():
        print(f"VACUOUS: no {config} to read the collector's rules from.")
        return 2
    excludes, readable = parse_test_excludes(config.read_text(encoding="utf-8"))
    if not readable:
        print(f"VACUOUS: {config} declares no test.exclude array; the rules changed shape.")
        return 2

    on_disk = on_disk_tests(frontend)
    collected = vitest_collected(frontend)
    if collected is None:
        print("VACUOUS: `npx vitest list --json` did not answer; nothing was measured.")
        return 2

    verdict = judge(on_disk, collected, excludes)
    print(f"frontend dir : {frontend}")
    print(f"test excludes: {list(excludes)}")
    print(f"on disk (src): {len(verdict.on_disk)} files")
    print(f"collected    : {len(verdict.collected)} files")
    if verdict.vacuous:
        print("VACUOUS: there is no test file under src/ to collect.")
        return 2
    for reason in verdict.reasons:
        print(reason)
    if verdict.reasons:
        return 1
    print("PASS: every test file on disk is collected by the frontend run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
