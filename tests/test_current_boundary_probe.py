"""Current-disk and isolated-copy checks for AC16|04 and dynamic probe counterfacts."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from scripts.quality import acceptance_item_probe as probe_module
from scripts.quality.acceptance_item_probe import (
    AC16_VENDOR_TREE,
    GAP,
    PORTED_METRICS,
    PROVEN,
    Context,
    DocItem,
    Probe,
    Verdict,
    count,
    judge_ac16_04,
    judge_ac17_05,
    measure_ac16_04,
    number,
    probe_for,
    resolve_break,
    resolve_repair,
    self_test_findings,
)


def test_ac16_04_measures_current_disk_and_proves_real_isolated_imports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The positive path audits untracked vendor files and the real child-import report."""
    context = Context(probe_module.REPO_ROOT, (), {})
    monkeypatch.setattr(context, "tracked", lambda: pytest.fail("must read the current tree"))

    facts = measure_ac16_04(context)

    assert facts["vendor_root"] == AC16_VENDOR_TREE.as_posix()
    assert facts["vendor_python_files"] == "325"
    assert facts["parsed_python_files"] == "325"
    assert facts["vendor_resource_files"] == "2"
    assert facts["manifest_python_files"] == "325"
    assert facts["manifest_resource_files"] == "2"
    assert facts["imported_modules"] == "3"
    assert facts["control_count"] == "3"
    assert facts["isolation_status"] == "passed"
    assert facts["network_attempts_json"] == "[]"
    assert facts["repository_path_leaks_json"] == "[]"
    assert judge_ac16_04(facts).state == PROVEN
    probe = probe_for("AC-16|04")
    assert not (set(probe.repair) - set(facts))
    assert judge_ac16_04(resolve_repair(facts, probe.repair)).state == PROVEN


def test_ac16_04_real_malformed_fixture_fails_closed(tmp_path: Path) -> None:
    """A real damaged source tree is measured as a gap without attempting isolated imports."""
    vendor_root = tmp_path / AC16_VENDOR_TREE
    shutil.copytree(probe_module.REPO_ROOT / AC16_VENDOR_TREE, vendor_root)
    (vendor_root / "datasets.py").write_text("if True print('broken')\n", encoding="utf-8")

    facts = measure_ac16_04(Context(tmp_path, (), {}))

    assert (vendor_root / "datasets.py").is_file()
    assert facts["vendor_python_files"] == "325"
    assert facts["parsed_python_files"] != "325"
    assert facts["manifest_present"] == "yes"
    assert facts["isolation_status"] == "not_run_invalid_source"
    assert judge_ac16_04(facts).state == GAP


def test_ac16_04_all_declared_boundary_counterfacts_make_the_judge_gap() -> None:
    """Legacy and newly measured boundary faces each fail against a real clean reading."""
    probe = probe_for("AC-16|04")
    clean = measure_ac16_04(Context(probe_module.REPO_ROOT, (), {}))
    assert probe.judge(clean).state == PROVEN

    for counterfact in probe.breaks:
        assert counterfact.expect == GAP
        assert probe.judge(resolve_break(clean, counterfact.facts)).state == GAP, counterfact.label


def test_ac16_04_loaded_module_names_must_be_inside_the_canonical_vendor_prefix() -> None:
    """Core aliases and prefix collisions fail even with a genuine copied-vendor origin."""
    clean = measure_ac16_04(Context(probe_module.REPO_ROOT, (), {}))
    assert judge_ac16_04(clean).state == PROVEN
    loaded = json.loads(clean["loaded_opendata_json"])
    vendor_source = loaded["opendata.data.providers.akshare._vendor.datasets"]["file"]

    for alias in (
        "opendata.core.database",
        "opendata.data.providers.akshare._vendor_evil.datasets",
    ):
        malicious = dict(loaded)
        malicious[alias] = {
            "file": vendor_source,
            "origin": vendor_source,
            "search_locations": [],
        }
        mutated = {
            **clean,
            "loaded_opendata_count": str(len(malicious)),
            "loaded_opendata_json": json.dumps(malicious, sort_keys=True),
        }
        assert mutated["vendor_valid"] == "yes"
        assert mutated["isolation_valid"] == "yes"
        assert mutated["loaded_modules_isolated"] == "yes"
        assert judge_ac16_04(mutated).state == GAP, alias


def _clean_ac17_05_facts(snapshot: int) -> dict[str, str]:
    facts = {
        "ratchet_exit": "0",
        "ratchet_face": "none",
        "printed": count(len(PORTED_METRICS)),
        "ef_violations": "1",
        "project_violations": "2",
        "select_matches": "yes",
        "http_matches": "yes",
        "verbs_ok": "yes",
        "verbs_declared": "delete, get, head, patch, post, put, request",
        "report_exit": "0",
        "report_todos": "0",
        "rows_total": "1",
        "rows_replay_ok": "1",
        "lock_records": "1",
        "report_files": "1",
        "disk_py": "1",
        "unrecorded_py": "0",
        "unrecorded_sample": "",
        "render_matches_archive": "yes",
        "rows_manual": "0",
        "edits_in_lock": "0",
        "edits_in_notices": "0",
        "edits_in_codemod": "0",
        "edits_disagree": "0",
        "exemption_shown": "yes",
        "isort_violations": "1",
        "format_would_rewrite": "1",
        "hooks_excluding": "1",
        "hooks_lost": "0",
        "hooks_total": "1",
        "hooks_total_head": "1",
        "hook_ids": "ruff-format",
        "hooks_lost_detail": "",
        "vanish_recent": "0",
        "vanish_recent_detail": "",
        "hist_steps": "2",
        "raises": "0",
        "flat_raises": "0",
        "flat_raise_detail": "",
        "vanish_older_detail": "",
        # ``ceiling_history()`` emits these two with the vanish faces: a root that leaves the
        # census while its file count reappears under another root is forgiven by the scope face
        # but still printed for the reader (docs/evidence/C67/README.md §2), so the harness has to
        # measure it too. ``raise_detail`` / ``vanish_older`` are read only by AC-17|03's judge.
        "reparented": "0",
        "reparented_detail": "",
        "trend": "flat",
    }
    for metric in PORTED_METRICS:
        facts[f"snap_{metric}"] = str(snapshot)
        facts[f"cur_{metric}"] = str(snapshot)
    return facts


@pytest.mark.parametrize("snapshot", [0, 5000])
def test_ac17_05_upper_bound_counterfacts_follow_each_snapshot(snapshot: int) -> None:
    """Both dynamic upper-bound breaks remain one above low and high snapshots."""
    probe = probe_for("AC-17|05")
    clean = _clean_ac17_05_facts(snapshot)
    assert judge_ac17_05(clean).state == PROVEN

    for counterfact in probe.breaks:
        if counterfact.label not in {"ruff_ported 高于快照", "direct_http_ported 高于快照"}:
            continue
        mutated = resolve_break(clean, counterfact.facts)
        target, value = counterfact.facts[0]
        referenced = value[1:-2]
        assert mutated[target] == str(snapshot + 1)
        assert mutated[referenced] == str(snapshot)
        assert judge_ac17_05(mutated).state == GAP


def test_ac17_05_clean_fixture_declares_every_fact_the_judge_reads() -> None:
    """A fact ``judge_ac17_05`` interpolates has to be measured here, never defaulted there.

    This harness hand-writes the clean reading, so it silently out-grows ``ceiling_history()``
    whenever a reading gains a face: the judge then raises ``KeyError`` from inside its own prose,
    which takes both counterfact arms down for a reason that has nothing to do with ceiling
    arithmetic. Defaulting the key to ``0`` in the judge would be worse -- it would print a
    measured-looking zero for a scope change nobody measured.
    """
    missing: list[str] = []

    class _Recording(dict[str, str]):
        def __missing__(self, key: str) -> str:
            missing.append(key)
            raise KeyError(key)

    try:
        verdict = judge_ac17_05(_Recording(_clean_ac17_05_facts(1)))
    except KeyError as error:
        pytest.fail(
            f"judge_ac17_05 interpolates the fact {error.args[0]!r}, which this fixture does not "
            "measure: add it to _clean_ac17_05_facts, do not default it inside the judge"
        )
    assert missing == []
    assert verdict.state == PROVEN


def test_ac17_05_prints_a_measured_reparenting_without_gating_it() -> None:
    """A forgiven re-parenting is a reading, not a ceiling: AC-17|05 原文只要求债务不高于快照.

    So a non-zero ``reparented`` has to reach the reader verbatim and still leave the item proven.
    The pair also proves the prose interpolates the measurement instead of printing a constant.
    """
    clean = _clean_ac17_05_facts(7)
    moved = {
        **clean,
        "reparented": "1",
        "reparented_detail": "3cf0cf7->3f05f63 opendata_fuyao(9)",
    }
    assert judge_ac17_05(clean).state == PROVEN

    verdict = judge_ac17_05(moved)
    reading = "\n".join(verdict.readings)
    assert verdict.state == PROVEN
    assert "从名单退出但文件计数在他处回来的 1 个" in reading
    assert "3cf0cf7->3f05f63 opendata_fuyao(9)" in reading
    assert "从他处回来的 0 个" not in reading


@pytest.mark.parametrize(
    ("facts", "expression"),
    [
        ({}, "*missing+1"),
        ({"snapshot": "-1"}, "*snapshot+1"),
        ({"snapshot": "1.5"}, "*snapshot+1"),
        ({"snapshot": "3"}, "*snapshot+2"),
        ({"snapshot": "3"}, "*snapshot +1"),
    ],
)
def test_resolve_break_rejects_missing_or_invalid_relative_references(
    facts: dict[str, str], expression: str
) -> None:
    with pytest.raises(ValueError):
        resolve_break(facts, (("current", expression),))


def test_resolve_break_keeps_literal_mutations_and_accepts_exact_reference() -> None:
    assert resolve_break(
        {"snapshot": "19"},
        (("current", "*snapshot+1"), ("state", "gap"), ("literal", "*provider_pkgs")),
    ) == {
        "snapshot": "19",
        "current": "20",
        "state": "gap",
        "literal": "*provider_pkgs",
    }


def test_self_test_findings_uses_resolve_break_for_dynamic_counterfacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = DocItem("synthetic", "AC-16", 4, 1, "source pins are checked", False)

    def judge(facts: dict[str, str]) -> Verdict:
        state = PROVEN if number(facts["current"]) <= number(facts["snapshot"]) else GAP
        return Verdict(state, (), "")

    synthetic = Probe(
        item="AC-16|04",
        expects="source pins",
        summary="unit fixture for self-test wiring",
        measure=lambda _context: {"snapshot": "8", "current": "8"},
        judge=judge,
        breaks=(probe_module.Break("one over snapshot", (("current", "*snapshot+1"),), GAP),),
        repair={"current": "*snapshot"},
    )
    monkeypatch.setattr(probe_module, "PROBES", (synthetic,))

    failures, reading = self_test_findings(
        Context(probe_module.REPO_ROOT, (item,), {}),
        {"AC-16|04": {"snapshot": "8", "current": "8"}},
        {},
    )

    assert failures == []
    assert "1 条 break" in reading
