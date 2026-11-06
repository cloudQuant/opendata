"""Guards that stop a rebuilt Bandit bundle from carrying a review it never made."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts.quality import acceptance_item_probe as gate
from scripts.quality import ported_security_evidence as evidence
from scripts.quality.ported_security_evidence_build import (
    BuildError,
    _align_roots,
    _carry_js_review,
    _carry_rule_reviews,
    _sanitize,
    _verify_identity_coverage,
    build_bundle,
    judge_target,
    main,
)

if TYPE_CHECKING:
    from pathlib import Path

MAKEFILE_TEXT = (gate.REPO_ROOT / gate.MAKEFILE_REL).read_text(encoding="utf-8")
#: The gate member that re-scans this bundle. The 24-hour window in the judge makes the bundle a
#: per-round reading, so a recipe that stops invoking the builder reddens AC-5|07 on clock time.
REBUILD_MEMBER = "security-bundle-rebuild"

TREE = {
    "futures/cons.py": "\n".join(f"line{i} = {i}" for i in range(1, 30)),
    "utils/request.py": "\n".join(f"call{i} = {i}" for i in range(1, 30)),
}
RULE = "B301"


def make_root(tmp_path: Path) -> Path:
    """A ported tree with the two files the carried prose cites."""
    for relative, text in TREE.items():
        path = tmp_path / evidence.PORT_ROOT / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def finding(relative: str, line: int, rule: str = RULE) -> dict[str, Any]:
    return {
        "filename": f"{evidence.PORT_ROOT}/{relative}",
        "test_id": rule,
        "test_name": f"Test {rule}",
        "issue_severity": "MEDIUM",
        "issue_confidence": "HIGH",
        "line_number": line,
        "line_range": [line],
    }


def prior_triage(basis: str, count: int = 1, root: str = "opendata_http") -> dict[str, Any]:
    return {
        "archive_round": "C65",
        "finding_count": count,
        "reviewer": {"name": "Codex primary", "kind": "AI", "requester": "cloudQuant"},
        "rules": {
            RULE: {
                "count": count,
                "disposition": "RETAIN_DESERIALIZATION_RCE",
                "basis": basis,
                "follow_up": "Track remediation separately.",
            }
        },
        "findings": [
            {"filename": f"{root}/futures/cons.py", "test_id": RULE, "line_number": 13 + offset}
            for offset in range(count)
        ],
        "additional_javascript_review": {
            "decision": "保留远端JS风险",
            "remote_response_eval": "futures/cons.py:13",
        },
        "source_preservation_basis": "FR-4 搬运保真",
    }


def test_a_relocated_tree_aligns_at_the_carried_root_depth() -> None:
    aligned = _align_roots(
        {"opendata_http/futures/cons.py", "opendata_http/utils/request.py"},
        {f"{evidence.PORT_ROOT}/futures/cons.py", f"{evidence.PORT_ROOT}/utils/request.py"},
    )

    assert aligned == (1, "opendata_http")


def test_two_files_differing_only_by_directory_are_not_matched_by_a_tail() -> None:
    with pytest.raises(BuildError, match="do not line up"):
        _align_roots(
            {"opendata_http/request.py"},
            {
                f"{evidence.PORT_ROOT}/request.py",
                f"{evidence.PORT_ROOT}/utils/request.py",
            },
        )


def test_identical_identities_carry_and_the_counts_are_returned() -> None:
    prior = prior_triage("futures/cons.py:13调用pickle.load")
    facts = _verify_identity_coverage(prior, [finding("futures/cons.py", 13)])

    assert facts == {
        "prior_root": "opendata_http",
        "prior_rows": 1,
        "current_rows": 1,
        "identities": 1,
    }


def test_a_moved_line_is_not_the_object_the_prior_round_reviewed(tmp_path: Path) -> None:
    prior = prior_triage("futures/cons.py:13调用pickle.load")

    with pytest.raises(BuildError, match="not the objects the prior round reviewed"):
        _verify_identity_coverage(prior, [finding("futures/cons.py", 14)])


def test_a_dropped_finding_is_refused() -> None:
    prior = prior_triage("no locator here", count=2)

    with pytest.raises(BuildError, match="dropped="):
        _verify_identity_coverage(prior, [finding("futures/cons.py", 13)])


def test_rule_count_drift_is_refused_because_the_basis_quotes_the_number(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    prior = prior_triage("2 pickle调用", count=2)

    with pytest.raises(BuildError, match="classified 2 findings, the current tree has 1"):
        _carry_rule_reviews(root, prior, {RULE: 1}, [finding("futures/cons.py", 13)])


def test_a_rule_the_prior_round_never_reviewed_is_refused(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    prior = prior_triage("no locator here")

    with pytest.raises(BuildError, match="no prior review: B999"):
        _carry_rule_reviews(root, prior, {RULE: 1, "B999": 1}, [finding("futures/cons.py", 13)])


def test_a_basis_locator_must_belong_to_a_finding_of_that_rule(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    prior = prior_triage("futures/cons.py:21调用pickle.load")

    with pytest.raises(BuildError, match="the scan disagrees with"):
        _carry_rule_reviews(root, prior, {RULE: 1}, [finding("futures/cons.py", 13)])


def test_a_matching_basis_locator_carries_the_prose_verbatim(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    basis = "futures/cons.py:13调用pickle.load"
    prior = prior_triage(basis)

    carried, citations = _carry_rule_reviews(
        root, prior, {RULE: 1}, [finding("futures/cons.py", 13)]
    )

    assert carried[RULE]["basis"] == basis
    assert carried[RULE]["count"] == 1
    assert citations == [f"{RULE} futures/cons.py:13"]


def test_javascript_review_line_must_exist_in_the_shipped_tree(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    prior = prior_triage("no locator here")
    prior["additional_javascript_review"]["remote_response_eval"] = "futures/cons.py:999"

    with pytest.raises(BuildError, match="no longer resolves"):
        _carry_js_review(root, prior)


def test_sanitize_keeps_only_the_redacted_keys(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    row = finding("futures/cons.py", 13) | {"code": "import pickle", "issue_text": "src line"}

    findings = _sanitize(root, evidence.PORT_ROOT, [row])

    assert set(findings[0]) == set(evidence.SCAN_FINDING_KEYS)
    assert findings[0]["line_number"] == 13


def test_sanitize_refuses_a_finding_outside_the_ported_root(tmp_path: Path) -> None:
    root = make_root(tmp_path)
    row = finding("futures/cons.py", 13) | {"filename": "opendata/safe.py"}

    with pytest.raises(BuildError, match="outside the ported root"):
        _sanitize(root, evidence.PORT_ROOT, [row])


@pytest.mark.parametrize("out_dir", ["docs/evidence/C65", "docs/evidence/A2", "docs/evidence/C73"])
def test_a_prior_round_archive_is_never_a_destination(tmp_path: Path, out_dir: str) -> None:
    root = make_root(tmp_path)
    prior_rel = "docs/quality/prior-triage.json"
    (root / prior_rel).parent.mkdir(parents=True, exist_ok=True)
    (root / prior_rel).write_text(json.dumps(prior_triage("no locator here")), encoding="utf-8")

    with pytest.raises(BuildError, match="prior round's archive"):
        build_bundle(root, round_label="C99", out_dir_rel=out_dir, prior_triage_rel=prior_rel)


def test_a_round_label_has_to_look_like_a_round(tmp_path: Path) -> None:
    root = make_root(tmp_path)

    with pytest.raises(BuildError, match="round label"):
        build_bundle(
            root,
            round_label="C74-tampered",
            out_dir_rel="docs/evidence/C99",
            prior_triage_rel="docs/quality/prior-triage.json",
        )


class TestJudgeTargetBinding:
    """The builder has to rebuild the bundle the judge reads, or the rebuild proves nothing."""

    def test_defaults_are_the_judges_own_paths(self) -> None:
        round_label, out_dir, prior_triage = judge_target()

        assert (round_label, out_dir, prior_triage) == (
            evidence.EXPECTED_ROUND,
            evidence.EVIDENCE_DIR.as_posix(),
            evidence.TRIAGE_REL.as_posix(),
        )

    def test_main_resolves_its_defaults_against_the_judge(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: dict[str, Any] = {}

        def record(root: Path, **kwargs: Any) -> dict[str, Any]:
            calls.update({"root": root, **kwargs})
            return {}

        monkeypatch.setattr("scripts.quality.ported_security_evidence_build.build_bundle", record)

        assert main([]) == 0
        assert calls["round_label"] == evidence.EXPECTED_ROUND
        assert calls["out_dir_rel"] == evidence.EVIDENCE_DIR.as_posix()
        assert calls["prior_triage_rel"] == evidence.TRIAGE_REL.as_posix()
        assert calls["force"] is False

    def test_an_explicit_target_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A developer may point the tool elsewhere; the gate just never has to."""
        calls: dict[str, Any] = {}
        monkeypatch.setattr(
            "scripts.quality.ported_security_evidence_build.build_bundle",
            lambda root, **kwargs: calls.update(kwargs) or {},
        )

        assert main(["--round", "C99", "--out-dir", "docs/evidence/C99", "--force"]) == 0
        assert calls["round_label"] == "C99"
        assert calls["out_dir_rel"] == "docs/evidence/C99"
        assert calls["force"] is True


class TestGateRebuildWiring:
    """AC-5|07 was red on wall-clock time because nothing in CI rebuilt the bundle.

    These three assertions are the regression guard: the member is in the recipe, it runs before
    the member that judges the bundle it writes, and its command really invokes this builder with
    the builder's own defaults. Removing any of that re-creates the time-dependent red.
    """

    def test_the_rebuild_is_a_member_of_the_gate_recipe(self) -> None:
        members = gate.gate_members(MAKEFILE_TEXT)

        assert REBUILD_MEMBER in members
        assert members.index(REBUILD_MEMBER) < members.index(gate.GATE_MEMBER)

    def test_the_member_invokes_this_builder_with_no_hardcoded_target(self) -> None:
        body = "\n".join(gate.make_recipes(MAKEFILE_TEXT)[REBUILD_MEMBER])

        assert "scripts/quality/ported_security_evidence_build.py" in body
        assert "--force" in body
        assert "--round" not in body
        assert "--out-dir" not in body

    def test_the_member_line_cannot_be_masked(self) -> None:
        """A `-` prefix or a `|| echo` would make the rebuild a printed suggestion, not a member."""
        lines = [
            line
            for line in gate.gate_recipe(MAKEFILE_TEXT)
            if REBUILD_MEMBER in gate.launched_by_line(line)
        ]

        assert len(lines) == 1
        assert gate.masking_lines(MAKEFILE_TEXT) == ([], [], [])
        assert gate.banner_pairing(
            gate.gate_members(MAKEFILE_TEXT), gate.gate_banners(MAKEFILE_TEXT)
        ) == (True, 0)
