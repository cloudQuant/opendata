#!/usr/bin/env python3
r"""Prove the collection guard catches each failure mode and nothing else.

C29 / AC-17 falsification surface (offline, no network, no warehouse access).

Why this file exists: the guard this round adds would look exactly as green as
the plane it replaced if it could never go red. So each rule is fed the shape
it exists to catch, the shapes it must not catch, and the shapes that make the
harness itself worthless — and the reading is taken from
``scripts.quality.frontend_test_collection.judge``, the same function the gate
calls, not from a copy of it.

Three things are read out:

* the census: shape -> verdict -> which rule spoke;
* the separation: each rule fires in a case the others cannot see, otherwise
  one of them is decoration;
* the false-positive guards: the boundaries a passing frontend run legitimately
  has today (playwright e2e, the coverage denominator) must stay green.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.quality.frontend_test_collection import (
    Verdict,
    judge,
    parse_test_excludes,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
REAL_CONFIG = REPO_ROOT / "frontend" / "vite.config.ts"

SILENCED: Final = "SILENCED"
UNAUTHORISED: Final = "UNAUTHORISED"
VACUOUS: Final = "VACUOUS"

A_TEST = "src/__tests__/a.test.ts"
B_TEST = "src/components/common/__tests__/b.test.ts"
ALLOWED = ["e2e/**", "node_modules/**", "dist/**"]

#: Entries that only hide a file from the coverage denominator. If any of them
#: ever comes back from ``parse_test_excludes``, the reader has started mixing
#: the two arrays and a coverage carve-out would read as a silenced test.
DENOMINATOR_ONLY_RULES: Final = (
    "src/**/*.d.ts",
    "src/main.ts",
    "src/auto-imports.d.ts",
    "src/components.d.ts",
)


@dataclass(frozen=True)
class Shape:
    """One injected state of the plane and the verdict it must produce."""

    name: str
    on_disk: list[str]
    collected: list[str]
    excludes: list[str]
    expect: frozenset[str] = field(default_factory=frozenset)


def kinds(verdict: Verdict) -> frozenset[str]:
    """Reduce a verdict to which of its rules spoke."""
    seen = {reason.split(":", 1)[0] for reason in verdict.reasons}
    if verdict.vacuous:
        seen.add(VACUOUS)
    return frozenset(seen)


def shapes() -> list[Shape]:
    """The injected states, each covering a mode the others cannot."""
    return [
        Shape(
            name="everything collected, only allowed boundaries excluded",
            on_disk=[A_TEST, B_TEST],
            collected=[A_TEST, B_TEST],
            excludes=ALLOWED,
        ),
        Shape(
            name="a file the collector hides (what the A0 rule did)",
            on_disk=[A_TEST, B_TEST],
            collected=[A_TEST],
            excludes=[*ALLOWED, "src/components/**"],
            expect=frozenset({SILENCED, UNAUTHORISED}),
        ),
        Shape(
            name="a file missing with no rule to blame",
            on_disk=[A_TEST, B_TEST],
            collected=[A_TEST],
            excludes=["e2e/**"],
            expect=frozenset({SILENCED}),
        ),
        Shape(
            name="an unauthorised rule that hides nothing yet",
            on_disk=[A_TEST, B_TEST],
            collected=[A_TEST, B_TEST],
            excludes=[*ALLOWED, "src/legacy/**"],
            expect=frozenset({UNAUTHORISED}),
        ),
        Shape(
            name="no test file on disk to compare against",
            on_disk=[],
            collected=[],
            excludes=["e2e/**"],
            expect=frozenset({VACUOUS}),
        ),
        # False-positive guards: boundaries a green run legitimately has today.
        Shape(
            name="GUARD playwright e2e stays a stated boundary",
            on_disk=[A_TEST],
            collected=[A_TEST],
            excludes=["e2e/**"],
        ),
        Shape(
            name="GUARD installed and build output are not silence",
            on_disk=[A_TEST],
            collected=[A_TEST],
            excludes=["node_modules/**", "dist/**"],
        ),
    ]


@dataclass(frozen=True)
class ConfigShape:
    """A config text, what the reader takes from it, and the verdict it earns."""

    name: str
    text: str
    expect_globs: list[str]
    expect_readable: bool
    expect_kinds: frozenset[str]


def config_shapes() -> list[ConfigShape]:
    """Config readers judged against the file the gate actually parses."""
    real = REAL_CONFIG.read_text(encoding="utf-8")
    armed = real.replace("'e2e/**',", "'e2e/**',\n      'src/legacy/**',", 1)
    untestable = real.replace("exclude: [", "skipped: [", 1)
    return [
        ConfigShape(
            name="the config in the tree",
            text=real,
            expect_globs=list(ALLOWED),
            expect_readable=True,
            expect_kinds=frozenset(),
        ),
        ConfigShape(
            name="with an extra src rule added",
            text=armed,
            expect_globs=["e2e/**", "src/legacy/**", "node_modules/**", "dist/**"],
            expect_readable=True,
            expect_kinds=frozenset({UNAUTHORISED}),
        ),
        ConfigShape(
            name="no test.exclude array at all",
            text=untestable,
            expect_globs=[],
            expect_readable=False,
            expect_kinds=frozenset(),
        ),
    ]


def read_configs() -> tuple[list[str], set[str]]:
    """Check the reader end to end, and report the reasons it disagrees for."""
    problems: list[str] = []
    seen: set[str] = set()
    print("== the config reader, judged through judge() ==")
    for case in config_shapes():
        globs, readable = parse_test_excludes(case.text)
        wrong = globs != case.expect_globs or readable is not case.expect_readable
        if wrong:
            problems.append(f"{case.name}: expected {case.expect_globs} got {globs}")
        print(f"  [{'MISMATCH' if wrong else 'OK'}] {case.name}")
        print(f"      globs={globs} readable={readable}")

        verdict = judge([A_TEST], [A_TEST], globs)
        got = kinds(verdict)
        seen.update(got)
        judged = got == case.expect_kinds
        if not judged:
            problems.append(f"{case.name}: expected {sorted(case.expect_kinds)} got {sorted(got)}")
        print(f"  [{'MISMATCH' if not judged else 'OK'}] -> verdict {sorted(got) or ['PASS']}")

        crossed = [glob for glob in DENOMINATOR_ONLY_RULES if glob in globs]
        if crossed:
            problems.append(f"{case.name}: coverage rules read as collector rules: {crossed}")
        print(f"      coverage entries leaking in: {crossed or 'none'}")
    return problems, seen


def main() -> int:
    """Run every shape through the gate's own judgment and report the reading."""
    mismatches: list[str] = []
    seen: set[str] = set()

    print("== injected states through judge() ==")
    for shape in shapes():
        verdict = judge(shape.on_disk, shape.collected, shape.excludes)
        got = kinds(verdict)
        seen.update(got)
        state = "MISMATCH" if got != shape.expect else "OK"
        if got != shape.expect:
            want = sorted(shape.expect) or ["PASS"]
            mismatches.append(f"{shape.name}: expected {want} got {sorted(got) or ['PASS']}")
        print(f"  [{state}] {shape.name}")
        print(f"      expects {sorted(shape.expect) or ['PASS']}  got {sorted(got) or ['PASS']}")
        for reason in verdict.reasons:
            print(f"      reason: {reason}")

    config_problems, config_seen = read_configs()
    mismatches.extend(config_problems)
    seen.update(config_seen)

    print()
    untested = {SILENCED, UNAUTHORISED, VACUOUS} - seen
    if untested:
        print(f"HARNESS_VACUOUS: no shape exercised {sorted(untested)}")
        return 2
    for mismatch in mismatches:
        print(f"GUARD_MISMATCH: {mismatch}")
    print(f"SHAPES: {len(shapes())} judge + {len(config_shapes())} config")
    print(f"MISMATCHES: {len(mismatches)}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
