"""Guards that stop a rebuilt Bandit bundle from carrying a review it never made."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from scripts.quality import ported_security_evidence as evidence
from scripts.quality.ported_security_evidence_build import (
    BuildError,
    _align_roots,
    _carry_js_review,
    _carry_rule_reviews,
    _sanitize,
    _verify_identity_coverage,
    build_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path

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
