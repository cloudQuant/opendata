"""The frontend e2e judgment plane has to be able to fail (C30, C32).

``make frontend-e2e`` runs ``scripts/quality/frontend_e2e_plane.py`` inside
``make gate``, and that script reads the spec files with regexes. Wiring it in
only proves it ran: a guard whose classifier had rotted — one that found no
leaf, or labelled every leaf ASSERTS — would also let the gate pass, and the
thing it exists to catch is precisely a plane that reads as green while saying
nothing. So the rules are asserted here on synthetic specs, the shipped specs
are asserted against them, and the paths where the plane cannot be read are
asserted to exit 2 rather than 0.

C32 retired the seven ``test.skip`` leaves, which emptied the plane's other
input: with nothing left to declare skipped, the GAP-DRIFT rule had become a
rule about an empty list, and the plane's size had stopped being a statement
about which pages it covers. The required-page floor is the rule that replaced
it, and it is asserted here in both directions too.
"""

from pathlib import Path

import pytest

from scripts.quality import frontend_e2e_plane as guard

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"

#: Rules about one leaf in isolation must not be answered by the shipped gap
#: list. It is empty as of C32, so what it asserts about the tree is "no skip
#: is explained" — a claim about the whole plane, not about the leaf.
NO_GAPS = frozenset()

#: Same reason for the page floor: ``/data`` being uncovered is a finding about
#: the tree, and a test of one leaf's `if` handling must not be red because of it.
NO_PAGES = frozenset()


def leaf(body: str, title: str = "does a thing") -> guard.Leaf:
    """Classify one synthetic spec whose test body is ``body``."""
    spec = (
        "import { test, expect } from '@playwright/test'\n"
        "\n"
        "test.describe('Suite', () => {\n"
        f"  test('{title}', async ({{ page }}) => {{\n"
        f"{body}\n"
        "  })\n"
        "})\n"
    )
    leaves = guard.classify("e2e/synthetic.spec.ts", spec)
    assert len(leaves) == 1, "the synthetic spec must produce exactly one leaf"
    return leaves[0]


def signed_in_leaf(path: str, body: str = "    await expect(page).toHaveURL(/x/)") -> guard.Leaf:
    """Classify one synthetic leaf below an ``Authenticated`` describe."""
    spec = (
        "test.describe('Suite', () => {\n"
        "  test.describe('Authenticated', () => {\n"
        f"    test('shows {path}', async ({{ page }}) => {{\n"
        f"      await page.goto('{path}')\n"
        f"{body}\n"
        "    })\n"
        "  })\n"
        "})\n"
    )
    leaves = guard.classify("e2e/synthetic.spec.ts", spec)
    assert len(leaves) == 1
    return leaves[0]


def test_a_clean_assertion_is_read_as_a_verdict() -> None:
    leaf_ = leaf("    await page.goto('/login')\n    await expect(page).toHaveURL(/login/)")

    assert leaf_.verdict == "ASSERTS"
    assert leaf_.title == "Suite › does a thing"
    assert guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES) == []


def test_an_assertion_that_only_runs_when_a_lookup_succeeds_is_a_finding() -> None:
    # The shape auth.spec.ts carried: the single expect sits inside an `if`, so
    # an element that stops existing makes the test pass without checking it.
    leaf_ = leaf(
        "    if (await page.locator('input').isVisible()) {\n"
        "      await expect(page.locator('.error')).toBeVisible()\n"
        "    }"
    )

    assert leaf_.verdict == "GUARDED"
    reasons = guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES)
    assert len(reasons) == 1
    assert reasons[0].startswith("GUARDED:")
    assert "1/1" in reasons[0]


def test_an_assertion_beside_the_if_is_not_counted_as_guarded() -> None:
    # Regression on the if-depth bookkeeping: the first version pushed the depth
    # *after* the opening line, so every `if` closed on the next line and a
    # sibling assertion read as guarded.
    leaf_ = leaf(
        "    if (await page.locator('input').isVisible()) {\n"
        "      await expect(page.locator('.error')).toBeVisible()\n"
        "    }\n"
        "    await expect(page).toHaveURL(/login/)"
    )

    assert (leaf_.expects, leaf_.guarded_expects) == (2, 1)
    assert leaf_.verdict == "PARTLY-GUARDED"
    assert "1/2" in guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES)[0]


def test_an_if_else_pair_that_asserts_in_both_branches_is_sound() -> None:
    # False positive the falsification set found: `} else {` leaves the brace
    # depth unchanged, so nothing closed the if-frame and both branch asserts
    # read as guarded — a finding raised against a test that asserts on every
    # path. Formatting is not part of the rule, so the split style reads the
    # same way.
    joined = leaf(
        "    if (await page.locator('input').isVisible()) {\n"
        "      await expect(page.locator('.error')).toBeVisible()\n"
        "    } else {\n"
        "      await expect(page.locator('.empty')).toBeVisible()\n"
        "    }"
    )
    split = leaf(
        "    if (await page.locator('input').isVisible()) {\n"
        "      await expect(page.locator('.error')).toBeVisible()\n"
        "    }\n"
        "    else {\n"
        "      await expect(page.locator('.empty')).toBeVisible()\n"
        "    }"
    )

    assert (joined.expects, joined.guarded_expects) == (2, 0)
    assert (split.expects, split.guarded_expects) == (2, 0)
    assert joined.verdict == "ASSERTS"
    assert split.verdict == "ASSERTS"
    assert guard.judge([joined, split], NO_GAPS, required_pages=NO_PAGES) == []


def test_an_assertion_that_only_lives_in_the_else_branch_is_a_finding() -> None:
    # The release is not a pardon for `else`. With no assertion in the if-branch
    # there is still a path that checks nothing, and that is the defect.
    leaf_ = leaf(
        "    if (await page.locator('input').isVisible()) {\n"
        "      await page.goto('/login')\n"
        "    } else {\n"
        "      await expect(page.locator('.error')).toBeVisible()\n"
        "    }"
    )

    assert (leaf_.expects, leaf_.guarded_expects) == (1, 1)
    assert leaf_.verdict == "GUARDED"
    assert guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES)[0].startswith("GUARDED:")


def test_an_else_if_chain_keeps_counting_its_branches_as_guarded() -> None:
    # `else if` continues a chain, it does not cover a path: the branch may not
    # run and there may be no final else. A chain that does assert in every
    # branch is therefore over-reported — the direction this plane accepts.
    leaf_ = leaf(
        "    if (await page.locator('a').isVisible()) {\n"
        "      await expect(page.locator('.a')).toBeVisible()\n"
        "    } else if (await page.locator('b').isVisible()) {\n"
        "      await expect(page.locator('.b')).toBeVisible()\n"
        "    }"
    )

    assert (leaf_.expects, leaf_.guarded_expects) == (2, 2)
    assert leaf_.verdict == "GUARDED"


def test_asserting_a_substring_of_the_visited_path_is_vacuous() -> None:
    # What the old 404 test did: goto('/nonexistent-page-xyz'), then assert the
    # url includes('nonexistent'). No product behaviour can make that false.
    leaf_ = leaf(
        "    await page.goto('/nonexistent-page-xyz')\n"
        "    const url = page.url()\n"
        "    expect(url.includes('login') || url.includes('nonexistent')).toBeTruthy()"
    )

    assert leaf_.verdict == "VACUOUS"
    assert guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES)[0].startswith("VACUOUS:")


def test_an_includes_check_against_an_unvisited_path_is_a_real_assertion() -> None:
    leaf_ = leaf(
        "    await page.goto('/login')\n"
        "    const url = page.url()\n"
        "    expect(url.includes('redirect')).toBe(true)"
    )

    assert leaf_.verdict == "ASSERTS"
    assert guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES) == []


def test_a_test_that_never_asserts_is_a_finding() -> None:
    leaf_ = leaf("    await page.goto('/login')\n    await page.click('button')")

    assert leaf_.verdict == "NO-ASSERTION"
    assert guard.judge([leaf_], NO_GAPS, required_pages=NO_PAGES)[0].startswith("NO-ASSERTION:")


def test_a_skip_without_a_named_reason_is_a_finding() -> None:
    leaves = guard.classify(
        "e2e/x.spec.ts",
        "test.skip('a new skip', async ({ page }) => {\n  await expect(page).toBeVisible()\n})\n",
    )

    assert [item.verdict for item in leaves] == ["SKIPPED"]
    assert guard.judge(leaves, NO_GAPS, required_pages=NO_PAGES)[0].startswith("GAP-DRIFT:")


def test_a_listed_gap_that_started_running_is_a_finding_too() -> None:
    # The other direction. The first version compared the list against every
    # title in the plane, so an un-skipped gap was still "seen" and this rule
    # could never fire — the shape of a check that reads as coverage and is not.
    leaves = guard.classify(
        "e2e/x.spec.ts",
        "test('runs now', async ({ page }) => {\n  await expect(page).toBeVisible()\n})\n",
    )

    assert [item.verdict for item in leaves] == ["ASSERTS"]
    reasons = guard.judge(leaves, frozenset({"runs now"}), required_pages=NO_PAGES)
    assert len(reasons) == 1
    assert "no longer declared skipped" in reasons[0]


def test_a_listed_gap_whose_test_disappeared_says_so() -> None:
    reasons = guard.judge([], frozenset({"gone"}), required_pages=NO_PAGES)

    assert reasons == ["GAP-DRIFT: 'gone' is a listed gap but no such test exists"]


def test_a_page_with_no_signed_in_verdict_is_a_finding() -> None:
    # C32's floor. The seven skips retiring means a green plane can now shrink
    # by deletion instead of by declaration, and every surviving leaf stays
    # ASSERTS while it does — so the size cross-check with the runner cannot
    # see it. /scripts is absent from this tree, and nothing else is wrong.
    reasons = guard.judge([signed_in_leaf("/tables"), signed_in_leaf("/data")])

    assert reasons == [
        f"PAGE-COVERAGE: {page} is navigated by no signed-in leaf that asserts, so the "
        "plane holds no verdict about that page"
        for page in sorted(guard.REQUIRED_PAGES - {"/tables", "/data"})
    ]


def test_an_anonymous_visit_does_not_cover_a_page() -> None:
    # The leaves named "requires authentication" navigate /scripts, /tables,
    # /executions and /tasks and assert the router bounced. If those counted,
    # deleting every real page test would still leave the floor satisfied and
    # the rule would be a reading of the login guard.
    anonymous = leaf("    await page.goto('/scripts')\n    await expect(page).toHaveURL(/login/)")

    assert anonymous.authenticated is False
    assert anonymous.verdict == "ASSERTS"
    assert any(reason.startswith("PAGE-COVERAGE: /scripts") for reason in guard.judge([anonymous]))


def test_a_signed_in_leaf_that_only_asserts_inside_an_if_covers_nothing() -> None:
    body = (
        "    if (await page.locator('x').isVisible()) {\n"
        "      await expect(page).toBeVisible()\n"
        "    }"
    )
    guarded = signed_in_leaf("/tasks", body)

    assert guarded.verdict == "GUARDED"
    assert "/tasks" not in guard.covered_pages([guarded])


def test_the_shipped_plane_covers_every_required_page() -> None:
    missing = guard.REQUIRED_PAGES - guard.covered_pages(guard.classify_tree(FRONTEND))

    assert not missing, f"pages with no signed-in verdict: {sorted(missing)}"


def test_losing_the_data_pages_leaves_reddens_the_plane() -> None:
    # The deletion direction, run against the shipped tree rather than a
    # synthetic one: drop the leaves that navigate /data and the plane must
    # fail while every remaining leaf still reads as a verdict.
    leaves = [item for item in guard.classify_tree(FRONTEND) if "/data" not in item.goto_paths]

    reasons = guard.judge(leaves)
    assert any(reason.startswith("PAGE-COVERAGE: /data") for reason in reasons)
    assert all(item.verdict == "ASSERTS" for item in leaves), (
        "the rest of the plane has rotted too, so this no longer isolates the floor"
    )


def test_the_guard_reads_the_shipped_e2e_plane() -> None:
    leaves = guard.classify_tree(FRONTEND)

    assert leaves, "the guard found no leaf in the plane it polices"
    assert guard.judge(leaves) == [], "a shipped e2e test carries no verdict"
    # 18 ASSERTS leaves today. The floor sits under that on purpose: page
    # deletion is the coverage rule's job, and this number is the backstop for
    # leaves going missing from a page that still holds one — the case nothing
    # else in this file sees.
    assert sum(1 for item in leaves if item.verdict == "ASSERTS") >= 14


def test_the_gap_list_is_exactly_the_shipped_skips() -> None:
    # GAPS is empty as of C32, so read this as "the tree holds no skip at all".
    # It stays a comparison rather than ``assert not skipped``: re-listing a
    # reason in the plane module has to be a deliberate edit, and if a gap is
    # ever genuinely unavoidable the rule still forces the reason to be written
    # down in both places instead of only in a comment.
    skipped = {item.title for item in guard.classify_tree(FRONTEND) if item.verdict == "SKIPPED"}

    assert skipped == set(guard.GAPS), (
        "the named gap list and the tree disagree: add the reason, or retire the "
        "line — a skip nobody has to justify is how the plane stops meaning anything"
    )


def test_the_shipped_plane_passes_the_guard_end_to_end() -> None:
    assert guard.main(["--frontend-dir", str(FRONTEND), "--skip-runner"]) == 0


def test_an_unreadable_plane_exits_two_not_zero(tmp_path: Path) -> None:
    assert guard.main(["--frontend-dir", str(tmp_path), "--skip-runner"]) == 2


def test_a_plane_with_a_finding_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    e2e = tmp_path / "e2e"
    e2e.mkdir()
    (e2e / "bad.spec.ts").write_text(
        "test.describe('Suite', () => {\n"
        "  test('guarded', async ({ page }) => {\n"
        "    if (await page.locator('x').isVisible()) {\n"
        "      await expect(page.locator('y')).toBeVisible()\n"
        "    }\n"
        "  })\n"
        "})\n",
        encoding="utf-8",
    )

    assert guard.main(["--frontend-dir", str(tmp_path), "--skip-runner"]) == 1
    # This temp tree has no signed-in leaf for any required page, so the exit
    # code would be 1 from PAGE-COVERAGE alone. The finding for the leaf itself
    # still has to be there, or the rule that reddens would be the wrong one.
    assert "GUARDED: e2e/bad.spec.ts:2 Suite › guarded" in capsys.readouterr().out


def test_a_size_disagreement_with_the_runner_is_a_finding(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The cross-check on the classifier itself: playwright counts its own plane,
    # so a shape the regexes never see reddens the gate instead of shrinking it.
    monkeypatch.setattr(guard, "runner_totals", lambda _frontend: (999, 3))

    assert guard.main(["--frontend-dir", str(FRONTEND)]) == 1
    assert "COUNT-MISMATCH" in capsys.readouterr().out
