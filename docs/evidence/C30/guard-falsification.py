#!/usr/bin/env python3
r"""Prove the frontend e2e judgment plane catches each shape and nothing else.

C30 / AC-17 falsification surface (offline, no browser, no network, no warehouse).

Why this file exists: ``make gate`` runs the guard against today's tree, and
today's tree is sound — 10 asserting tests, 7 named skips, zero findings. A
green reading of a clean plane says nothing about whether the guard could go
red, so every rule here is fed the shape it exists to catch, the shapes it must
not catch, and the shapes that would make the harness itself worthless. The
reading is taken from ``scripts.quality.frontend_e2e_plane`` — ``classify``,
``judge`` and ``main`` — the same functions the gate calls, not from a copy.

Three things are read out:

* the census: shape -> per-leaf verdict -> which rule spoke;
* the separation: each rule fires in a case the others cannot see, otherwise
  one of them is decoration;
* the exit-code contract: the gate's entry point has to keep 0, 1 and 2
  distinct, so an unreadable plane cannot be reported as a pass.

Two shapes are marked ``BLIND``. They are shapes the plane reads as sound and
should not: an assertion behind an early ``return``, and an assertion inside a
one-line ``if`` nested in a branch that an ``else`` then releases. Each is
recorded with the reading it produces, so the limitation is a fact a reader can
check instead of something discovered the day a test passes while checking
nothing. If either reading changes, this harness says so.
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from scripts.quality import frontend_e2e_plane as guard

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND = REPO_ROOT / "frontend"

GUARDED: Final = "GUARDED"
VACUOUS: Final = "VACUOUS"
NO_ASSERTION: Final = "NO-ASSERTION"
GAP_DRIFT: Final = "GAP-DRIFT"

#: Every rule the plane has. A kind missing from the run means this harness
#: proved nothing, and it reports that instead of exiting 0.
RULE_KINDS: Final = (GUARDED, VACUOUS, NO_ASSERTION, GAP_DRIFT)

SUITE: Final = "Suite"
LISTED_SKIP: Final = "Suite › a skip that is on the list"


def spec(*bodies: str) -> str:
    """Wrap one or more test bodies in a single describe block."""
    tests = "\n".join(
        f"  test('test {index}', async ({{ page }}) => {{\n{body}\n  }})"
        for index, body in enumerate(bodies, start=1)
    )
    return f"test.describe('{SUITE}', () => {{\n{tests}\n}})\n"


def single(body: str, keyword: str = "test") -> str:
    """One test written bare, for leaves whose title must be exact."""
    return (
        f"test.describe('{SUITE}', () => {{\n"
        f"  {keyword}('a gap that moved on', async ({{ page }}) => {{\n{body}\n  }})\n"
        "})\n"
    )


@dataclass(frozen=True)
class Shape:
    """One injected spec and the reading the plane has to produce for it."""

    name: str
    text: str
    expect_verdicts: tuple[str, ...]
    expect_kinds: frozenset[str] = field(default_factory=frozenset)
    gaps: frozenset[str] = frozenset()
    blind: bool = False


def shapes() -> list[Shape]:
    """The injected specs, each covering a case the other rules cannot see."""
    return [
        # --- a finding for each rule -----------------------------------------
        Shape(
            name="an assertion that only runs when a lookup succeeded",
            text=spec(
                "    if (await page.locator('input').isVisible()) {\n"
                "      await expect(page.locator('.error')).toBeVisible()\n"
                "    }"
            ),
            expect_verdicts=("GUARDED",),
            expect_kinds=frozenset({GUARDED}),
        ),
        Shape(
            name="an assertion beside the if is only partly guarded",
            text=spec(
                "    if (await page.locator('input').isVisible()) {\n"
                "      await expect(page.locator('.error')).toBeVisible()\n"
                "    }\n"
                "    await expect(page).toHaveURL(/login/)"
            ),
            expect_verdicts=("PARTLY-GUARDED",),
            expect_kinds=frozenset({GUARDED}),
        ),
        Shape(
            name="an assertion that cannot be false",
            text=spec(
                "    await page.goto('/login')\n"
                "    const url = page.url()\n"
                "    expect(url.includes('login')).toBeTruthy()"
            ),
            expect_verdicts=("VACUOUS",),
            expect_kinds=frozenset({VACUOUS}),
        ),
        Shape(
            name="a declared test that never asserts",
            text=spec("    await page.goto('/login')\n    await page.getByRole('button').click()"),
            expect_verdicts=("NO-ASSERTION",),
            expect_kinds=frozenset({NO_ASSERTION}),
        ),
        Shape(
            name="a new skip with no named reason",
            text=single("    await expect(page).toBeVisible()", "test.skip"),
            expect_verdicts=("SKIPPED",),
            expect_kinds=frozenset({GAP_DRIFT}),
        ),
        Shape(
            name="a listed gap that started running again",
            text=single("    await expect(page).toBeVisible()"),
            expect_verdicts=("ASSERTS",),
            expect_kinds=frozenset({GAP_DRIFT}),
            gaps=frozenset({"Suite › a gap that moved on"}),
        ),
        Shape(
            name="a listed gap whose test disappeared",
            text=spec("    await expect(page).toBeVisible()"),
            expect_verdicts=("ASSERTS",),
            expect_kinds=frozenset({GAP_DRIFT}),
            gaps=frozenset({"Suite › gone entirely"}),
        ),
        Shape(
            name="one guarded and one clean leaf in the same spec",
            text=spec(
                "    if (await page.locator('a').isVisible()) {\n"
                "      await expect(page.locator('.a')).toBeVisible()\n"
                "    }",
                "    await page.goto('/tasks')\n    await expect(page).toHaveURL(/tasks/)",
            ),
            expect_verdicts=("GUARDED", "ASSERTS"),
            expect_kinds=frozenset({GUARDED}),
        ),
        # --- shapes it must leave alone --------------------------------------
        Shape(
            name="MUST-NOT-CATCH if/else asserting in both branches covers every path",
            text=spec(
                "    if (await page.locator('input').isVisible()) {\n"
                "      await expect(page.locator('.error')).toBeVisible()\n"
                "    } else {\n"
                "      await expect(page.locator('.empty')).toBeVisible()\n"
                "    }"
            ),
            expect_verdicts=("ASSERTS",),
        ),
        Shape(
            name="MUST-NOT-CATCH the same pair with the else on its own line",
            text=spec(
                "    if (await page.locator('input').isVisible()) {\n"
                "      await expect(page.locator('.error')).toBeVisible()\n"
                "    }\n"
                "    else {\n"
                "      await expect(page.locator('.empty')).toBeVisible()\n"
                "    }"
            ),
            expect_verdicts=("ASSERTS",),
        ),
        Shape(
            name="MUST-NOT-CATCH includes against a string the test never navigated to",
            text=spec(
                "    await page.goto('/login')\n"
                "    const params = new URL(page.url()).searchParams\n"
                "    expect(params.toString().includes('redirect')).toBe(true)"
            ),
            expect_verdicts=("ASSERTS",),
        ),
        Shape(
            name="MUST-NOT-CATCH a skip on the list is a stated gap, not a finding",
            text=single("    await expect(page).toBeVisible()", "test.skip"),
            expect_verdicts=("SKIPPED",),
            gaps=frozenset({"Suite › a gap that moved on"}),
        ),
        # --- recorded blind spots --------------------------------------------
        Shape(
            name="BLIND an assertion behind an early return",
            text=spec(
                "    if (!(await page.locator('input').isVisible())) return\n"
                "    await expect(page.locator('.error')).toBeVisible()"
            ),
            expect_verdicts=("ASSERTS",),
            blind=True,
        ),
        Shape(
            name="BLIND a one-line if nested in a branch the else releases",
            text=spec(
                "    if (await page.locator('a').isVisible()) {\n"
                "      if (await page.locator('b').isEnabled()) { expect(page).toBeVisible() }\n"
                "    } else {\n"
                "      await expect(page.locator('.empty')).toBeVisible()\n"
                "    }"
            ),
            expect_verdicts=("ASSERTS",),
            blind=True,
        ),
    ]


def kinds(reasons: list[str]) -> frozenset[str]:
    """Reduce failure reasons to which rules spoke."""
    return frozenset(reason.split(":", 1)[0] for reason in reasons)


def read_shapes() -> tuple[list[str], set[str]]:
    """Run every shape through the guard's own classifier and judgment."""
    problems: list[str] = []
    seen: set[str] = set()
    print("== injected specs through classify() + judge() ==")
    for shape in shapes():
        leaves = guard.classify("e2e/injected.spec.ts", shape.text)
        verdicts = tuple(leaf.verdict for leaf in leaves)
        reasons = guard.judge(leaves, shape.gaps)
        got = kinds(reasons)
        seen.update(got)

        right = verdicts == shape.expect_verdicts and got == shape.expect_kinds
        tag = "BLIND-DRIFT" if shape.blind and verdicts != shape.expect_verdicts else "MISMATCH"
        want_verdicts = list(shape.expect_verdicts)
        if not right:
            problems.append(
                f"{shape.name}: want verdicts {want_verdicts} got {list(verdicts)}; "
                f"want kinds {sorted(shape.expect_kinds)} got {sorted(got)}"
            )
        print(f"  [{'OK' if right else tag}] {shape.name}")
        want = sorted(shape.expect_kinds) or ["PASS"]
        print(f"      verdicts want={want_verdicts} got={list(verdicts)}")
        print(f"      kinds    want={want} got={sorted(got) or ['PASS']}")
        for reason in reasons:
            print(f"      reason: {reason}")
    return problems, seen


def read_exit_codes() -> list[str]:
    """Check the gate's entry point keeps 0 / 1 / 2 apart on three real trees."""
    empty = Path(tempfile.mkdtemp(prefix="c30-empty-"))
    planted = Path(tempfile.mkdtemp(prefix="c30-planted-"))
    e2e = planted / "e2e"
    e2e.mkdir(parents=True, exist_ok=True)
    (e2e / "planted.spec.ts").write_text(
        spec(
            "    if (await page.locator('x').isVisible()) {\n"
            "      await expect(page.locator('y')).toBeVisible()\n"
            "    }"
        ),
        encoding="utf-8",
    )
    cases = [
        ("the shipped tree, runner cross-check skipped", str(FRONTEND), 0),
        ("a tree with no e2e directory at all", str(empty), 2),
        ("a tree holding one planted guarded leaf", str(planted), 1),
    ]

    problems: list[str] = []
    print()
    print("== main() exit codes ==")
    for name, frontend_dir, expect in cases:
        rc = guard.main(["--frontend-dir", frontend_dir, "--skip-runner"])
        ok = rc == expect
        if not ok:
            problems.append(f"{name}: expected exit {expect} got {rc}")
        print(f"  [{'OK' if ok else 'MISMATCH'}] {name} -> exit {rc}")
    return problems


def main() -> int:
    """Run every shape, report the census, and refuse to call a blind harness green."""
    problems, seen = read_shapes()
    problems.extend(read_exit_codes())

    print()
    untested = [kind for kind in RULE_KINDS if kind not in seen]
    if untested:
        print(f"HARNESS_VACUOUS: no shape exercised {untested} -> exit 2, not a pass")
        return 2
    print(f"rules exercised: {sorted(seen)}")
    print(
        f"shipped plane: {len(guard.classify_tree(FRONTEND))} leaves, {len(guard.GAPS)} named gaps"
    )
    for problem in problems:
        print(f"GUARD_MISMATCH: {problem}")
    print(f"SHAPES: {len(shapes())} injected")
    print(f"MISMATCHES: {len(problems)}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
