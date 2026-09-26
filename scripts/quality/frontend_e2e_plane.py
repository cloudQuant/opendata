#!/usr/bin/env python3
"""Fail the gate when a declared frontend e2e test carries no verdict.

C30 / AC-11 + AC-17 judgment plane. One module, run by ``make frontend-e2e``
inside ``make gate`` and imported by ``tests/test_frontend_e2e_guard.py``.
``docs/evidence/C30/e2e-plane-census.py`` measures the same tree as a separate
implementation — C27's rule is that measurement planes may be independent while
the judgment plane must be unique, so the census borrows nothing from here and
only this module decides. The playwright runner is the independent cross-check
on size.

Why this file exists: ``make gate`` could run playwright and report
``9 passed / 7 skipped`` in one green line. That line does not distinguish
three shapes that exist in the tree today —

  * ``test.skip`` (never runs, and reads as an intentional gap or as rot
    depending on whether someone wrote down why),
  * an assertion that cannot be false,
  * an assertion that only runs when a lookup already succeeded.

so the run's own summary cannot be the gate. This plane reads each leaf and
refuses to call a green run a statement about verified behaviour.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]

TEST_RE: Final = re.compile(r"^(\s*)(?:test|it)(\.skip|\.fixme|\.fail)?\s*\(\s*(['\"])(.+?)\3")
DESCRIBE_RE: Final = re.compile(
    r"^(?P<indent>\s*)(?:test\.)?describe\s*\(\s*(?P<q>['\"])(?P<title>.+?)(?P=q)"
)
EXPECT_RE: Final = re.compile(r"\bexpect(?:\.soft)?\s*\(")
GOTO_RE: Final = re.compile(r"\.goto\(\s*(['\"])(?P<path>[^'\"]+)\1")
INCLUDES_RE: Final = re.compile(r"\.includes\(\s*(['\"])(?P<needle>[^'\"]+)\1")
IF_RE: Final = re.compile(r"\bif\s*\(")
ELSE_RE: Final = re.compile(r"(?<![\w.])else\b(?!\s*if\b)")
ELSE_IF_RE: Final = re.compile(r"^else\s+if\b")
RUNNER_TOTAL_RE: Final = re.compile(
    r"Total:\s*(?P<tests>\d+)\s+tests?\s+in\s+(?P<files>\d+)\s+files?"
)

#: Tests the tree declares skipped, each with the reason measured on 2026-09-26.
#: Membership is a claim about the tree, not a comment: a new skip that is not
#: listed here, and a listed gap that starts running again, both fail the gate
#: (GAP-DRIFT, both directions).
#:
#: All seven need a per-endpoint response fixture before they can assert
#: anything real. Measured: ``/scripts`` reads ``/api/v1/scripts/?page=…``,
#: ``/tables`` reads ``/api/v1/tables/``, ``/tables/<name>`` reads its own
#: detail and preview endpoints, ``/tasks`` reads ``/api/v1/tasks/`` — a single
#: generic ``{items, total}`` stub renders three of the four pages only, so
#: half-implementing them would assert against the mock instead of the app.
GAPS: Final[frozenset[str]] = frozenset(
    {
        "Scripts & Data Tables E2E › Authenticated › scripts list shows data",
        "Scripts & Data Tables E2E › Authenticated › tables list shows data",
        "Scripts & Data Tables E2E › Authenticated › table detail shows schema and preview",
        "Scripts & Data Tables E2E › Authenticated › executions list shows history",
        "Tasks Management E2E › Authenticated › tasks list page loads",
        "Tasks Management E2E › Authenticated › can create a new task",
        "Tasks Management E2E › Authenticated › can trigger task execution",
    }
)


@dataclass(frozen=True)
class Leaf:
    """One declared playwright test and what its body actually does.

    Attributes:
        file: Spec path relative to the frontend directory.
        line: 1-based line of the ``test(...)`` call.
        title: Full title including enclosing ``describe`` titles.
        modifier: ``, ``.skip``, ``.fixme`` or ``.fail``.
        expects: ``expect(`` calls inside the leaf body.
        guarded_expects: How many of those sit in an ``if`` block with no else.
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
        """Single label for the leaf, most specific first."""
        if self.modifier:
            return "SKIPPED"
        if self.vacuous_against:
            return "VACUOUS"
        if self.expects == 0:
            return "NO-ASSERTION"
        if self.guarded_expects == self.expects:
            return "GUARDED"
        if self.guarded_expects:
            return "PARTLY-GUARDED"
        return "ASSERTS"

    @property
    def where(self) -> str:
        """``file:line`` for the leaf."""
        return f"{self.file}:{self.line}"


def strip_noise(line: str) -> str:
    """Blank string literals and ``//`` comments so braces seen are structure."""
    without_comment = re.sub(r"//.*$", "", line)
    return re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "''", without_comment)


def brace_delta(line: str) -> int:
    """Net ``{`` minus ``}`` on one line, after :func:`strip_noise`."""
    text = strip_noise(line)
    return text.count("{") - text.count("}")


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
        own = len(match.group("indent"))
        if own < indent:
            titles.insert(0, match.group("title"))
            indent = own
    return titles


@dataclass
class _IfBranch:
    """One ``if`` whose block is still open, and what it has asserted so far.

    Attributes:
        level: Brace depth just before the ``if`` line.
        asserts: ``expect`` calls counted in the branch now being read.
        covered: Set once an ``else`` follows a branch that already asserted —
            the two together always run an assertion, so neither half is a
            vacuity risk and the pair is released from the guarded count.
    """

    level: int
    asserts: int = 0
    covered: bool = False


def continues_with_else(lines: list[str], index: int) -> bool:
    """True when the next code line is the ``else`` of the block closing here.

    ``} else {`` and a ``}`` followed by a separate ``else {`` are the same
    branch; only the first keeps the brace depth unchanged, so without this
    peek the release would depend on how the author formatted the file. An
    ``else if`` does not qualify — continuing a chain is not covering a path.
    """
    for follow in lines[index + 1 :]:
        if not follow.strip():
            continue
        text = strip_noise(follow).lstrip()
        return text.startswith("else") and ELSE_IF_RE.match(text) is None
    return False


def count_expects(lines: list[str], start: int, end: int) -> tuple[int, int]:
    """Count ``(expect calls, how many are inside an open if-block)``.

    Two readings the plane has to get right, both found by feeding the rules
    shapes instead of reading them:

    * The branch bookkeeping records the depth *before* the opening line and
      pops the innermost frame when the block closes back to it, so a sibling
      statement after the ``if`` is not counted as guarded.
    * ``if (a) { expect(x) } else { expect(y) }`` asserts on every path. An
      ``else`` line does not open a new block at a deeper level, so without
      :data:`ELSE_RE` the frame stayed open and both branch asserts read as
      guarded — GUARDED, a finding against a sound test. The closing ``}`` of
      the if-block is where :func:`continues_with_else` keeps the frame alive
      one line longer, so the release works whether the author wrote
      ``} else {`` or split it over two lines.

    An ``else if`` is deliberately not a release: the chain may still have a
    path with no assertion. Its branch keeps the original frame open, so those
    asserts stay counted and a sound chain is over-reported. Flagging a test
    that happens to be fine is the direction this plane takes on; hiding one
    that never asserts is the one it cannot.
    """
    total = 0
    guarded = 0
    depth = 0
    frames: list[_IfBranch] = []
    for index in range(start, end + 1):
        line = lines[index]
        text = strip_noise(line)
        delta = brace_delta(line)
        found = len(EXPECT_RE.findall(line))
        total += found
        if found and frames:
            top = frames[-1]
            if not top.covered:
                guarded += found
                top.asserts += found
        level_before = depth
        depth += delta
        if ELSE_RE.search(text) and frames:
            top = frames[-1]
            if top.asserts:
                top.covered = True
                guarded -= top.asserts
                top.asserts = 0
        if IF_RE.search(text) and delta > 0:
            frames.append(_IfBranch(level=level_before))
        while frames and depth <= frames[-1].level:
            if continues_with_else(lines, index):
                break
            frames.pop()
    return total, guarded


def classify(rel: str, text: str) -> list[Leaf]:
    """Classify every ``test(...)`` leaf in one spec's text."""
    lines = text.split("\n")
    out: list[Leaf] = []
    for index, raw in enumerate(lines):
        match = TEST_RE.match(raw)
        if match is None:
            continue
        end = block_end(lines, index)
        body = lines[index : end + 1]
        goto_paths = [m.group("path") for line in body for m in GOTO_RE.finditer(line)]
        expects, guarded = count_expects(lines, index, end)
        vacuous = tuple(
            needle
            for line in body
            for needle in (m.group("needle") for m in INCLUDES_RE.finditer(line))
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


def classify_tree(frontend: Path) -> list[Leaf]:
    """Classify every ``e2e/**/*.spec.ts`` leaf under ``frontend``."""
    e2e_dir = frontend / "e2e"
    if not e2e_dir.is_dir():
        return []
    found: list[Leaf] = []
    for spec in sorted(e2e_dir.rglob("*.spec.ts")):
        text = spec.read_text(encoding="utf-8")
        found.extend(classify(spec.relative_to(frontend).as_posix(), text))
    return found


def judge(leaves: list[Leaf], gaps: frozenset[str] = GAPS) -> list[str]:
    """Turn leaf verdicts into failure reasons; empty means the plane is sound."""
    reasons: list[str] = []
    titles = {leaf.title for leaf in leaves}
    skipped = {leaf.title for leaf in leaves if leaf.verdict == "SKIPPED"}
    for leaf in leaves:
        verdict = leaf.verdict
        if verdict == "VACUOUS":
            reasons.extend(
                f"VACUOUS: {leaf.where} {leaf.title} — includes('{needle}') is already true of "
                "the path the test navigated to, so no product behaviour can redden it"
                for needle in leaf.vacuous_against
            )
        elif verdict == "NO-ASSERTION":
            reasons.append(f"NO-ASSERTION: {leaf.where} {leaf.title} — the body never asserts")
        elif verdict in {"GUARDED", "PARTLY-GUARDED"}:
            reasons.append(
                f"GUARDED: {leaf.where} {leaf.title} — {leaf.guarded_expects}/{leaf.expects} "
                "assertion(s) sit inside an `if` with no else, so a missing element passes the test"
            )
        if verdict == "SKIPPED" and leaf.title not in gaps:
            reasons.append(f"GAP-DRIFT: {leaf.where} {leaf.title} — skipped with no named reason")
    # Compared against the skips, not against every title: a gap that was
    # un-skipped is still in the tree under the same name, and a rule that read
    # that as fine would fire on exactly the case it exists for never.
    for listed in sorted(gaps - skipped):
        shape = "it is no longer declared skipped" if listed in titles else "no such test exists"
        reasons.append(f"GAP-DRIFT: '{listed}' is a listed gap but {shape}")
    return reasons


def runner_totals(frontend: Path) -> tuple[int, int] | None:
    """Ask playwright how many tests and files its plane holds, or ``None``.

    ``--list`` compiles the specs without starting the webServer, so this is a
    reading of the runner's own view for roughly a second. ``None`` means the
    plane could not be read, which the caller reports as vacuous rather than
    as a match.
    """
    npx = shutil.which("npx")
    if npx is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            [npx, "--no-install", "playwright", "test", "--list"],
            cwd=frontend,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = RUNNER_TOTAL_RE.search(result.stdout)
    if match is None:
        return None
    return int(match.group("tests")), int(match.group("files"))


def main(argv: list[str] | None = None) -> int:
    """Classify the tree, cross-check its size against the runner, set exit code.

    Returns:
        ``0`` sound plane, ``1`` findings or a size disagreement, ``2`` nothing
        readable (no specs, or playwright would not answer).
    """
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("--frontend-dir", default=str(REPO_ROOT / "frontend"))
    parser.add_argument(
        "--skip-runner",
        action="store_true",
        help="do not ask playwright for its own count (used by unit guards only)",
    )
    args = parser.parse_args(argv)
    frontend = Path(args.frontend_dir).resolve()

    leaves = classify_tree(frontend)
    if not leaves:
        print(f"NOTHING-TO-JUDGE: no *.spec.ts under {frontend / 'e2e'} -> exit 2, not a pass")
        return 2

    by_verdict: dict[str, int] = {}
    for leaf in leaves:
        by_verdict[leaf.verdict] = by_verdict.get(leaf.verdict, 0) + 1
        tag = f" [{leaf.modifier}]" if leaf.modifier else ""
        print(f"  {leaf.verdict:15} {leaf.where:24} {leaf.title}{tag}")
    print("")
    print("== classified tally ==")
    for verdict in sorted(by_verdict):
        print(f"  {verdict:15} {by_verdict[verdict]}")
    print(f"  {'LEAVES':15} {len(leaves)}")

    mismatch = False
    if args.skip_runner:
        print("runner tally      : SKIPPED (--skip-runner)")
    else:
        totals = runner_totals(frontend)
        if totals is None:
            print("RUNNER-UNREADABLE: playwright would not list its plane -> exit 2, not a pass")
            return 2
        listed, files = totals
        print(f"runner tally      : {listed} tests in {files} files (playwright test --list)")
        mismatch = listed != len(leaves)
        print(f"static vs runner  : {'MISMATCH' if mismatch else 'MATCH'}")

    reasons = judge(leaves)
    print(f"named gaps        : {len(GAPS)}")
    print(f"findings          : {len(reasons)}")
    for reason in reasons:
        print(f"  {reason}")
    if mismatch:
        print("  COUNT-MISMATCH: classifier and runner disagree about the plane's size")
    if reasons or mismatch:
        return 1
    print("PASS: every declared e2e leaf carries a verdict that can fail.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
