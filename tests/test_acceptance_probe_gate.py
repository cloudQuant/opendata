"""The gate member round C50 adds has to be able to fail, and so has the tree.

``make gate`` gained ``acceptance-probe-check``, which runs
``scripts/quality/acceptance_item_probe.py --gate-check``. Wiring a judge in only proves that it
ran, so the same three questions get asserted here on planted inputs instead of being quoted from
one favourable pass:

* Is the member really in the recipe, launched in its own sub-make, by the mode that compares
  something? ``--all`` prints readings and exits 0 whatever the ledger says, which is how three
  stale cells survived until C50 measured it (face 4 of
  ``docs/evidence/C50/census-run2-final-readings.txt``).
* Does the work-tree / index / history classification come from source rather than from this run's
  luck? That classification is what decides whether a cell may turn red mid-round, so an author who
  could quietly move a probe from ``moment`` to ``stable`` would be holding the release valve.
* Does the reconciliation bite in both directions -- ledger-proven-that-reads-gap is red, and a
  probe that stops reading ``git status`` is red until someone writes that down?

The shipped tree is asserted against all three, so a rot in the recipe, in the walk, or in the
committed face baseline fails here rather than at the next ``make gate``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from scripts.quality import acceptance_item_probe as tool
from tests import test_p0_integration_surface as guard

if TYPE_CHECKING:
    from pathlib import Path

REPO_ROOT: Path = tool.REPO_ROOT
MAKEFILE_TEXT = (REPO_ROOT / tool.MAKEFILE_REL).read_text(encoding="utf-8")

#: Synthetic module for the surface walk: four measures, four different amounts of git.
SYNTHETIC = """
def measure_worktree(ctx: Context) -> Facts:
    return {"out": run_argv(["git", "status", "--porcelain"])[1]}


def measure_files_only(ctx: Context) -> Facts:
    return {"n": str(len(ctx.read("Makefile")))}


def a_helper(ctx: Context) -> str:
    code, out = run_argv(["git", "log", "--oneline", "-n", "3"])
    return out


def measure_through_a_helper(ctx: Context) -> Facts:
    return {"log": a_helper(ctx)}


def measure_index(ctx: Context) -> Facts:
    return {"tracked": str(len(ctx.tracked()))}
"""


def context_at(root: Path) -> tool.Context:
    """A context that reads files from ``root`` and measures nothing else."""
    return tool.Context(root=root, doc_items=(), ledger={})


class TestGateRecipeShape:
    """The faces AC-17|01 judges, asserted directly so the reading is not the only witness."""

    def test_the_judging_member_is_in_the_gate_recipe(self) -> None:
        assert tool.GATE_MEMBER in tool.gate_members(MAKEFILE_TEXT)

    def test_the_member_runs_the_mode_that_compares(self) -> None:
        """``--all`` reports and exits 0; only ``--gate-check`` can stop the gate."""
        recipes = tool.make_recipes(MAKEFILE_TEXT)
        body = "\n".join(recipes[tool.GATE_MEMBER])
        assert "--gate-check" in body
        assert "--all" not in body
        assert "--item" not in body

    def test_every_member_is_announced_in_order_and_the_run_closes(self) -> None:
        members = tool.gate_members(MAKEFILE_TEXT)
        banners = tool.gate_banners(MAKEFILE_TEXT)
        paired, unpaired = tool.banner_pairing(members, banners)
        assert (paired, unpaired) == (True, 0)
        assert banners[-1] == "PASSED"

    def test_no_recipe_line_can_swallow_a_member_failure(self) -> None:
        ignore, masked, chained = tool.masking_lines(MAKEFILE_TEXT)
        assert (ignore, masked, chained) == ([], [], [])

    def test_the_gate_launches_each_member_in_its_own_sub_make(self) -> None:
        """One member per line, or a single line decides several items' fate at once."""
        per_line = [len(tool.launched_by_line(line)) for line in tool.gate_recipe(MAKEFILE_TEXT)]
        assert [n for n in per_line if n not in (0, 1)] == []
        assert sum(per_line) == len(tool.gate_members(MAKEFILE_TEXT))

    def test_a_developer_view_never_reached_the_gate(self) -> None:
        members = tool.gate_members(MAKEFILE_TEXT)
        assert [name for name in members if name in tool.DEV_VIEW_TARGETS] == []


class TestMembershipBites:
    """The member's own absence has to be a finding, not a silently shorter gate."""

    def test_a_recipe_without_the_member_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / tool.MAKEFILE_REL).write_text(
            'gate:\n\t@echo "===== gate: brand-check ====="\n\t@$(MAKE) -C . brand-check\n'
            '\t@echo "===== gate: PASSED ====="\n',
            encoding="utf-8",
        )
        findings = tool.membership_findings(context_at(tmp_path))
        assert len(findings) == 1
        assert tool.GATE_MEMBER in findings[0]

    def test_the_shipped_recipe_is_not_a_finding(self) -> None:
        assert tool.membership_findings(context_at(REPO_ROOT)) == []


class TestSurfaceWalk:
    """Which readings are about this moment, decided by walking source rather than by luck."""

    def test_a_work_tree_reading_is_moment_dependent(self) -> None:
        assert tool.surfaces_in_closure("measure_worktree", SYNTHETIC) == ("worktree(git status)",)

    def test_a_file_only_reading_is_stable(self) -> None:
        assert tool.surfaces_in_closure("measure_files_only", SYNTHETIC) == ()

    def test_the_face_is_counted_through_a_helper_too(self) -> None:
        """Reaching git via a shared primitive is the same risk as shelling out inline."""
        assert tool.surfaces_in_closure("measure_through_a_helper", SYNTHETIC) == (
            "history(git log)",
        )

    def test_the_index_is_a_moment_face_when_read_through_the_context(self) -> None:
        assert tool.surfaces_in_closure("measure_index", SYNTHETIC) == ("index(ctx.tracked)",)

    def test_the_new_member_reads_the_repository_not_this_moment(self) -> None:
        """AC-17|01 is judged even mid-round, because nothing it reads depends on dirt."""
        source = tool.own_source()
        assert tool.surfaces_in_closure("measure_ac17_01", source) == ()
        assert tool.surfaces_in_closure("measure_ac1_10", source) != ()

    def test_the_walk_covers_every_registered_measure(self) -> None:
        """A probe whose measure this walk cannot name would be booked as stable for free."""
        source = tool.own_source()
        for probe in tool.PROBES:
            name = probe.measure.__name__
            assert name in tool.top_level_defs(source), f"{probe.item}: {name} is not top-level"


class TestReconcileRule:
    """The one red label is ledger-proven-reading-gap, and only on a face that can support it."""

    def test_a_stable_gap_under_a_proven_ledger_is_red(self) -> None:
        assert tool.reconcile_state(tool.PROVEN, tool.GAP, [], quiet=False) == "stale-proof", (
            "a clean tree is not required: file content does not change when evidence lands"
        )

    def test_a_moment_gap_under_a_proven_ledger_waits_for_a_quiet_tree(self) -> None:
        faces = ["worktree(git status)"]
        assert tool.reconcile_state(tool.PROVEN, tool.GAP, faces, quiet=False) == "deferred"
        assert tool.reconcile_state(tool.PROVEN, tool.GAP, faces, quiet=True) == "stale-proof"

    def test_reading_proven_under_an_open_ledger_is_not_a_finding(self) -> None:
        """Flipping the box is the backfill step's job; the judge does not pre-empt it."""
        assert tool.reconcile_state("unreviewed", tool.PROVEN, [], quiet=False) == "unflipped"
        assert tool.reconcile_state(tool.GAP, tool.PROVEN, [], quiet=False) == "unflipped"

    def test_agreement_and_an_honest_gap_are_quiet(self) -> None:
        assert tool.reconcile_state(tool.PROVEN, tool.PROVEN, [], quiet=False) == "agrees"
        assert tool.reconcile_state(tool.GAP, tool.GAP, [], quiet=False) == "agrees"
        assert tool.reconcile_state("unreviewed", tool.GAP, [], quiet=False) == "open"

    def test_only_stale_proof_can_come_back_as_a_finding(self) -> None:
        labels = {
            tool.reconcile_state(ledger, reading, faces, quiet)
            for ledger in (tool.PROVEN, tool.GAP, "unreviewed")
            for reading in (tool.PROVEN, tool.GAP)
            for faces in ([], ["index(git ls-files)"])
            for quiet in (True, False)
        }
        assert labels == {"agrees", "unflipped", "open", "deferred", "stale-proof"}
        assert tool.stale_cell_finding("AC-1|10", []).startswith("台账把 AC-1|10 记成 proven")


class TestFaceBaseline:
    """The frozen baseline is the release valve, so it may only move under a reviewed diff."""

    @staticmethod
    def book(moment: dict[str, list[str]], unplumbed: list[str]) -> tool.FaceBook:
        """A baseline of one's own making, for the directions that must each be caught."""
        return tool.FaceBook(moment=moment, unplumbed=unplumbed)

    def test_a_matching_baseline_is_not_a_finding(self) -> None:
        saved = self.book({"AC-1|10": ["worktree(git status)"]}, ["AC-16|09"])
        findings, reading = tool.face_diff(saved, saved)
        assert findings == []
        assert "1 个在读 moment 面" in reading

    def test_a_probe_that_declared_itself_stable_is_red(self) -> None:
        saved = self.book({"AC-1|10": ["worktree(git status)"]}, [])
        now = self.book({"AC-1|10": []}, [])
        findings, _ = tool.face_diff(saved, now)
        assert len(findings) == 1
        assert "moment 面不符" in findings[0]

    def test_a_probe_that_stopped_existing_is_red_too(self) -> None:
        saved = self.book({"AC-1|10": [], "AC-9|02": []}, [])
        now = self.book({"AC-1|10": []}, [])
        findings, _ = tool.face_diff(saved, now)
        assert len(findings) == 1
        assert "AC-9|02" in findings[0]

    def test_a_missing_baseline_is_a_finding_not_a_pass(self) -> None:
        findings, _ = tool.face_diff(None, self.book({}, []))
        assert len(findings) == 1
        assert tool.FACES_REL in findings[0]

    def test_the_unplumbed_set_may_only_shrink(self) -> None:
        saved = self.book({}, ["AC-16|09"])
        assert tool.face_diff(saved, self.book({}, ["AC-16|09", "AC-17|04"]))[0]
        assert tool.face_diff(saved, self.book({}, []))[0] == []

    def test_the_committed_baseline_agrees_with_the_source_right_now(self) -> None:
        """The pass that reddens a drift is this one, so the two must not disagree on disk."""
        ctx = tool.load_context()
        saved = tool.read_face_baseline()
        assert saved is not None, f"{tool.FACES_REL} is missing or malformed"
        assert saved == tool.face_book(ctx, tool.own_source())

    def test_this_round_closed_one_of_the_cells_with_no_plane(self) -> None:
        ctx = tool.load_context()
        book = tool.face_book(ctx, tool.own_source())
        assert "AC-17|01" not in book.unplumbed, "the probe registered this round has to count"
        baseline = {
            "AC-16|05",
            "AC-16|09",
            "AC-17|02",
            "AC-17|04",
            "AC-17|06",
            "AC-17|09",
            "§4|03",
        }
        assert set(book.unplumbed) <= baseline, "the only way off this list is a new probe"


class TestCliModes:
    """The two new flags are the entry points; a typo must not look like a pass."""

    @staticmethod
    def run(*args: str) -> tuple[int, str]:
        """Invoke the tool as a child process and hand back exit plus combined output."""
        script = REPO_ROOT / "scripts/quality/acceptance_item_probe.py"
        done = subprocess.run(  # noqa: S603  # nosec B603
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
        return done.returncode, done.stdout + done.stderr

    def test_both_gate_modes_are_documented_on_the_cli(self) -> None:
        code, out = self.run("--help")
        assert code == 0
        assert "--gate-check" in out
        assert "--sync-faces" in out

    @pytest.mark.parametrize("mode", ["--gate-check", "--sync-faces"])
    def test_a_misspelled_mode_is_rejected_not_ignored(self, mode: str) -> None:
        code, out = self.run(f"{mode}-typo")
        assert code == 2
        assert "unrecognized" in out


class TestCounterfactCoverage:
    """Cheap structure only: the tool's own ``--self-test`` is what proves the judges bite."""

    def test_no_probe_reports_without_a_reading_and_something_to_break(self) -> None:
        """A probe with no counterfact is a reading, not a gate -- the C27 lesson."""
        for probe in tool.PROBES:
            assert probe.repair, f"{probe.item}: declares no clean reading"
            assert len(probe.breaks) >= 2, f"{probe.item}: {len(probe.breaks)} counterfact(s)"
            assert probe.expects and probe.summary, probe.item
        keys = [probe.item for probe in tool.PROBES]
        assert len(keys) == len(set(keys)), f"a duplicated item id would shadow: {keys}"

    def test_the_gate_member_probe_breaks_on_each_masking_shape(self) -> None:
        """AC-17|01 exists so a sub-failure cannot reach the summary unnamed."""
        probe = tool.probe_for("AC-17|01")
        broken = {key for brk in probe.breaks for key, _ in brk.facts}
        assert {
            "no_recipe",
            "ignore_prefix",
            "echo_mask",
            "chained",
            "dev_view",
            "banner_match",
            "dry_members",
            "self_member",
        } <= broken


class TestCounterfactReading:
    """The gate log has to say how much it judged, not only that it passed.

    ``--gate-check`` folds the counterfact self-test into its one measure pass and prints two
    numerators. They count what reached judgement *in that pass*, so a run in which every measure
    raised still prints a zero rather than a remembered total: an "all counterfacts hold" line that
    came out of a constant would be indistinguishable from a pass that checked nothing.
    """

    @staticmethod
    def stub_probe() -> tool.Probe:
        """A probe whose judge reads one fact, so both numerators are countable by hand."""

        def judge(facts: tool.Facts) -> tool.Verdict:
            ok = facts.get("gated") == "yes"
            return tool.Verdict(tool.PROVEN if ok else tool.GAP, (), "" if ok else "not gated")

        return tool.Probe(
            item="AC-99|01",
            expects="门禁每一遍都重量一次",
            summary="synthetic probe",
            measure=lambda ctx: {"gated": "yes"},
            judge=judge,
            breaks=(
                tool.Break("member removed", (("gated", "no"),), tool.GAP),
                tool.Break("never launched", (("gated", "absent"),), tool.GAP),
            ),
            repair={"gated": "yes"},
        )

    @staticmethod
    def stub_context() -> tool.Context:
        """A document holding just the synthetic criterion, worded as the probe expects."""
        doc = tool.DocItem(
            key="AC-99|01|stub",
            group="AC-99",
            index=1,
            line=1,
            text="门禁每一遍都重量一次，红则中止",
            ticked=True,
        )
        return tool.Context(root=REPO_ROOT, doc_items=(doc,), ledger={})

    def test_the_reading_counts_probes_and_breaks_that_reached_judgement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(tool, "PROBES", (self.stub_probe(),))
        findings, reading = tool.self_test_findings(
            self.stub_context(), {"AC-99|01": {"gated": "yes"}}, {}
        )
        assert findings == []
        assert "1/1" in reading, reading
        assert "2 条" in reading, reading

    def test_a_pass_that_reached_no_judgement_prints_a_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The measurement failed, so the sentence must not claim counterfacts were checked."""
        monkeypatch.setattr(tool, "PROBES", (self.stub_probe(),))
        unmeasured = {"AC-99|01": "AC-99|01: cannot measure: git missing"}
        findings, reading = tool.self_test_findings(self.stub_context(), {}, unmeasured)
        assert findings == list(unmeasured.values()), "the reason is forwarded, not paraphrased"
        assert "0/1" in reading, reading
        assert "0 条" in reading, reading


class TestC51ZeroDepCells:
    """C51 hands ``AC-16|06`` and ``AC-16|07`` a judge; the judge's own inputs are checked here.

    The item these probes judge is a *test set*, so the failure mode worth asserting against is
    the one C51 measured: a marker registered in ``pytest.ini`` that no test applied, which made
    ``-m "integration and not e2e"`` select 0 of 3358 items in every interpreter while the sentence
    about "the P0 domain integration tests" kept reading as if it named something. Two instruments
    read that surface now -- the guard module and this judge -- so their agreement is asserted,
    because a surface only one of them counts is a surface nobody can trust.
    """

    def test_both_cells_have_a_probe_that_still_matches_the_document(self) -> None:
        ctx = tool.load_context()
        for item in ("AC-16|06", "AC-16|07"):
            probe = tool.probe_for(item)
            assert tool.wording_drift(ctx, probe) == "", probe.item
            assert probe.breaks and probe.repair, probe.item

    def test_the_judge_and_the_guard_module_count_the_same_surface(self) -> None:
        """Two instruments reading one surface must select the same way and find the same units."""
        assert tool.P0_INTEGRATION_SELECTOR == guard.SELECTOR
        declared = {*guard.P0_INTEGRATION_MODULES, *guard.P0_INTEGRATION_CLASSES}
        assert set(tool.integration_units_now()) == declared
        assert tool.registered_markers_in_ini((REPO_ROOT / "pytest.ini").read_text()) >= {
            "integration",
            "e2e",
        }

    def test_the_counted_surface_reaches_every_p0_domain(self) -> None:
        sources = "".join(
            (REPO_ROOT / unit.split("::")[0]).read_text(encoding="utf-8")
            for unit in tool.integration_units_now()
        )
        assert [domain for domain in tool.p0_domains_in_migration() if domain not in sources] == []

    def test_each_new_probe_breaks_a_face_this_round_actually_paid_for(self) -> None:
        broken = {
            item: {key for brk in tool.probe_for(item).breaks for key, _ in brk.facts}
            for item in ("AC-16|06", "AC-16|07")
        }
        assert {
            "detector_exit",
            "self_test_exit",
            "import_live",
            "dynamic_live",
            "no_new_reference",
            "only_down",
            "frozen",
        } <= broken["AC-16|06"]
        assert {
            "registered",
            "strict",
            "units",
            "domains_missing",
            "run_exit",
            "skipped",
            "blocked_here",
            "block_control_hits",
            "block_attempted",
            "block_leaks",
            "block_pkgs",
            "archive_exit",
            "archive_blind",
            "archive_absent",
            "archive_self_written",
        } <= broken["AC-16|07"]

    def test_the_clean_environment_archive_the_judge_reads_is_shipped(self) -> None:
        """``archive_blind == 0`` is only a fact while the body it reads is in the tree.

        The judge reads the newest round's archive rather than one fixed file, so the round pin
        follows that pick: whatever it reads must declare the round its own directory names.
        """
        archive_rel, archive_dir = tool.newest_round_archive(tool.CLEAN_RUN_BASENAME)
        assert archive_rel != "-", "no round has archived a clean-environment run"
        archive = (REPO_ROOT / archive_rel).read_text(encoding="utf-8")
        declared = [line for line in archive.splitlines() if line.startswith("ARCHIVE_ROUND=")]
        assert declared == [f"ARCHIVE_ROUND={archive_dir}"], f"{archive_rel}: {declared}"
        assert tool.CLEAN_SECTION in archive
        assert "CLEAN_RUN_EXIT=0" in archive
        assert "akshare: absent" in archive and "openbb: absent" in archive
        assert archive.count(tool.P0_INTEGRATION_SELECTOR) >= 3, (
            "both interpreters must show the selector they ran"
        )
