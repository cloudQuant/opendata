#!/usr/bin/env python3
r"""Measure the frontend e2e plane: what can actually produce a verdict.

C30 / AC-11 measurement surface (offline, no browser, no network, no warehouse).

A playwright run prints one green line per test, and that line means something
different for a test that asserts, a test declared with `test.skip`, a test
whose only assertion sits inside `if (await …isVisible())`, and a test whose
assertion cannot be false. This script reads the spec files and splits those
shapes apart, so the size of the plane the run's summary describes is visible
before any rule is applied to it.

Deliberately an independent reading: it re-implements the classifier and does
not import ``scripts/quality/frontend_e2e_plane.py``, the judgment module this
round adds (C27's rule — measurement planes may be independent, the judgment
plane must be unique). Its labels therefore differ from the guard's wording,
and ``== label map ==`` below is the reconciliation a reader needs:

  DECLARED-SKIPPED -> SKIPPED   GUARDED-BY-IF -> GUARDED   (same rule, longer name)

This script is not a check and has no pass/fail reading. Exit 2 means nothing
was readable, exit 0 means a census was printed; only the guard decides.

Run: python -u docs/evidence/C30/e2e-plane-census.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FRONTEND: Final = Path(__file__).resolve().parents[3] / "frontend"
E2E_DIR: Final = FRONTEND / "e2e"

TEST_RE: Final = re.compile(r"^(\s*)(?:test|it)(\.skip|\.fixme|\.fail)?\s*\(\s*(['\"])(.+?)\3")
DESCRIBE_RE: Final = re.compile(r"^(\s*)(?:test\.)?describe\s*\(\s*(['\"])(.+?)\2")
EXPECT_RE: Final = re.compile(r"\bexpect(?:\.soft)?\s*\(")
GOTO_RE: Final = re.compile(r"\.goto\(\s*(['\"])([^'\"]+)\1")
INCLUDES_RE: Final = re.compile(r"\.includes\(\s*(['\"])([^'\"]+)\1")
IF_RE: Final = re.compile(r"\bif\s*\(")


def strip_noise(line: str) -> str:
    """Drop string literals and // comments so brace counts see only structure."""
    without_comment = re.sub(r"//.*$", "", line)
    return re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", without_comment)


def brace_delta(line: str) -> int:
    """Net ``{`` minus ``}`` on one line, after :func:`strip_noise`."""
    text = strip_noise(line)
    return text.count("{") - text.count("}")


@dataclass(frozen=True)
class Leaf:
    """One declared playwright test and what its body can say.

    Attributes:
        file: Spec path relative to ``frontend``.
        line: 1-based line of the ``test(...)`` call.
        title: Enclosing describe titles joined with the test title.
        modifier: ``, ``.skip``, ``.fixme`` or ``.fail``.
        expects: Number of ``expect(`` calls in the leaf body.
        guarded_expects: How many of those sit inside an ``if`` with no else.
        vacuous_against: Substrings asserted present in a path the test itself
            navigated to, which makes the assertion true by construction.
    """

    file: str
    line: int
    title: str
    modifier: str
    expects: int
    guarded_expects: int
    vacuous_against: tuple[str, ...]

    @property
    def verdict(self) -> str:
        """One label per leaf; most specific shape wins."""
        if self.modifier in {".skip", ".fixme", ".fail"}:
            return "DECLARED-SKIPPED"
        if self.vacuous_against:
            return "VACUOUS"
        if self.expects == 0:
            return "NO-ASSERTION"
        if self.guarded_expects == self.expects:
            return "GUARDED-BY-IF"
        if self.guarded_expects:
            return "PARTLY-GUARDED"
        return "ASSERTS"


def block_end(lines: list[str], start: int) -> int:
    """Index of the line closing the block that ``lines[start]`` opens."""
    depth = 0
    opened = False
    for index in range(start, len(lines)):
        depth += brace_delta(lines[index])
        opened = opened or depth > 0
        if opened and depth <= 0:
            return index
    return len(lines) - 1


def titles_above(lines: list[str], start: int) -> list[str]:
    """Enclosing ``describe`` titles, outermost first, by decreasing indent."""
    titles: list[str] = []
    indent = len(lines[start]) - len(lines[start].lstrip())
    for index in range(start - 1, -1, -1):
        match = DESCRIBE_RE.match(lines[index])
        if match is None:
            continue
        own = len(match.group(1))
        if own < indent:
            titles.insert(0, match.group(3))
            indent = own
    return titles


def guarded_counts(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """(expect calls, how many of them sit inside a still-open `if (` block)."""
    total = 0
    guarded = 0
    depth = 0
    open_if_levels: list[int] = []
    for index in range(start, end + 1):
        line = lines[index]
        text = strip_noise(line)
        delta = brace_delta(line)
        found = len(EXPECT_RE.findall(line))
        total += found
        if found and open_if_levels:
            guarded += found
        level_before = depth
        depth += delta
        if IF_RE.search(text) and delta > 0:
            open_if_levels.append(level_before)
        while open_if_levels and depth <= open_if_levels[0]:
            open_if_levels.pop(0)
    return total, guarded


def leaves_of(rel: str, text: str) -> list[Leaf]:
    """Classify every ``test(...)`` leaf in one spec file's text."""
    lines = text.split("\n")
    out: list[Leaf] = []
    for index, raw in enumerate(lines):
        match = TEST_RE.match(raw)
        if match is None:
            continue
        end = block_end(lines, index)
        body = lines[index : end + 1]
        goto_paths = [m.group(2) for line in body for m in GOTO_RE.finditer(line)]
        expects, guarded = guarded_counts(lines, index, end)
        vacuous = tuple(
            needle
            for line in body
            for needle in (m.group(2) for m in INCLUDES_RE.finditer(line))
            if any(needle in path for path in goto_paths)
        )
        out.append(
            Leaf(
                file=rel,
                line=index + 1,
                title=" › ".join([*titles_above(lines, index), match.group(4)]),
                modifier=match.group(2) or "",
                expects=expects,
                guarded_expects=guarded,
                vacuous_against=vacuous,
            )
        )
    return out


def census() -> list[Leaf]:
    """Read every ``e2e/**/*.spec.ts`` on disk."""
    if not E2E_DIR.is_dir():
        return []
    found: list[Leaf] = []
    for spec in sorted(E2E_DIR.rglob("*.spec.ts")):
        rel = spec.relative_to(FRONTEND).as_posix()
        found.extend(leaves_of(rel, spec.read_text(encoding="utf-8")))
    return found


def main() -> int:
    """Print the census. Exit 2 when the plane could not be read at all."""
    print(f"e2e dir: {E2E_DIR} ({'exists' if E2E_DIR.is_dir() else 'MISSING'})")
    specs = sorted(E2E_DIR.rglob("*.spec.ts")) if E2E_DIR.is_dir() else []
    print(f"spec files: {len(specs)}")
    tests = census()
    if not tests:
        print("NO-LEAVES: nothing was read, so this is not a pass -> exit 2")
        return 2
    tally: dict[str, int] = {}
    for leaf in tests:
        tally[leaf.verdict] = tally.get(leaf.verdict, 0) + 1
        marker = "  " if leaf.verdict == "ASSERTS" else "->"
        print(
            f"  [{marker}] {leaf.file}:{leaf.line} {leaf.verdict:16} "
            f"expects={leaf.expects} guarded={leaf.guarded_expects} {leaf.title}"
        )
        for needle in leaf.vacuous_against:
            print(f"        vacuous: includes('{needle}') is already true of the goto path")
    print("")
    print("== verdict tally ==")
    for verdict in sorted(tally):
        print(f"  {verdict:16} {tally[verdict]}")
    runnable = [t for t in tests if t.verdict != "DECLARED-SKIPPED"]
    silent = [t for t in tests if t.verdict in {"VACUOUS", "NO-ASSERTION", "GUARDED-BY-IF"}]
    print(f"  leaves total     : {len(tests)}")
    print(f"  can produce one  : {len(runnable)}")
    print(f"  no unconditional : {len(silent)} (vacuous / none / only inside an unguarded if)")
    print("")
    print("== label map (this census -> scripts/quality/frontend_e2e_plane.py) ==")
    for census_label, guard_label in (
        ("ASSERTS", "ASSERTS"),
        ("DECLARED-SKIPPED", "SKIPPED"),
        ("GUARDED-BY-IF", "GUARDED"),
        ("PARTLY-GUARDED", "PARTLY-GUARDED"),
        ("VACUOUS", "VACUOUS"),
        ("NO-ASSERTION", "NO-ASSERTION"),
    ):
        print(f"  {census_label:16} -> {guard_label:16} {tally.get(census_label, 0)}")
    print("")
    print("Measurement only. The guard, not this script, decides the gate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
