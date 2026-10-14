from __future__ import annotations

import csv
import hashlib
import json
from typing import TYPE_CHECKING

import pytest

from scripts.quality.iter2_model_evidence import (
    PHASE_EVIDENCE_KIND,
    REQUIRED_PHASES,
    EvidenceIndexError,
    build_report,
    identity_projection_sha256,
    load_inventory,
    render_markdown,
    sha256_bytes,
)

if TYPE_CHECKING:
    from pathlib import Path


IDENTITIES = [("sample", "Alpha"), ("sample", "Beta")]


def _write_inventory(path: Path, identities: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["task_id", "provider", "upstream_model"])
        writer.writeheader()
        for provider, model in identities:
            writer.writerow(
                {
                    "task_id": f"OBB2-{provider}-{model}",
                    "provider": provider,
                    "upstream_model": model,
                }
            )


def _write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, sort_keys=True).encode("utf-8")
    path.write_bytes(data)
    return sha256_bytes(data)


def _execution_receipt_document(
    claim: dict[str, object], identity_fields: dict[str, object]
) -> dict[str, object]:
    document: dict[str, object] = {
        "receipt_schema_version": 1,
        "phase": claim["phase"],
        "status": claim["status"],
        "pins": claim["pins"],
        "machine_execution": {
            "record_type": "machine_execution",
            "status": claim["status"],
            "actual_command": claim["actual_command"],
            "exit_code": claim["exit_code"],
            "executed_at_utc": claim["executed_at_utc"],
        },
    }
    document.update(identity_fields)
    return document


def _pin(path: Path, root: Path) -> dict[str, str]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "path": path.relative_to(root).as_posix(),
        "before_sha256": digest,
        "after_sha256": digest,
    }


def _review_evidence_snapshot_document(
    claims: list[dict[str, object]],
) -> dict[str, object]:
    phases: dict[str, object] = {}
    for claim in claims:
        phase = claim["phase"]
        receipt = claim["receipt"]
        pins = claim["pins"]
        phases[phase] = {
            "receipt": {
                "path": receipt["path"],
                "sha256": receipt["sha256"],
            },
            "source_pins": pins["source"],
            "contract_pins": pins["contract"],
            "contract_version": pins["contract_version"],
        }
    return {"phases": phases}


def _refresh_review_evidence_snapshot(
    root: Path,
    claims: list[dict[str, object]],
    review: dict[str, object],
) -> None:
    receipt = review["receipt"]
    path = root / receipt["path"]
    document = json.loads(path.read_text(encoding="utf-8"))
    document["evidence_snapshot"] = _review_evidence_snapshot_document(claims)
    receipt["sha256"] = _write_json(path, document)


def _claim_bundle(
    root: Path,
    identity: tuple[str, str],
    *,
    phases: tuple[str, ...] = REQUIRED_PHASES,
    status: str = "PASS",
    exit_code: int = 0,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    provider, model = identity
    canonical = f"{provider}::{model}"
    artifacts = {
        "source": root / "src.py",
        "fixture": root / "fixtures" / "wire.json",
        "probe": root / "probes" / "probe.py",
        "contract": root / "contracts" / "schema.json",
    }
    for path in artifacts.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(path.name, encoding="utf-8")
    pins: dict[str, object] = {group: [_pin(path, root)] for group, path in artifacts.items()}
    pins["contract_version"] = f"sha256:{pins['contract'][0]['after_sha256']}"

    claims: list[dict[str, object]] = []
    for phase in phases:
        receipt_path = root / "receipts" / f"{phase}.json"
        claim: dict[str, object] = {
            "provider": provider,
            "upstream_model": model,
            "phase": phase,
            "status": status,
            "evidence_kind": PHASE_EVIDENCE_KIND[phase],
            "execution_mode": "offline_fixture",
            "actual_command": f"pytest -q tests/test_{phase}.py",
            "exit_code": exit_code,
            "executed_at_utc": "2026-10-12T08:15:00Z",
            "pins": pins,
        }
        receipt_document = _execution_receipt_document(claim, {"model_identity": canonical})
        if phase == "counterfact":
            expected_observation = {"probe_exit_code": 1, "outcome": "expected_rejection"}
            receipt_document["counterfact"] = {
                "expected": expected_observation,
                "observed": expected_observation,
                "reason": "The negative probe was rejected before source I/O.",
            }
        receipt_hash = _write_json(receipt_path, receipt_document)
        claim["receipt"] = {
            "path": receipt_path.relative_to(root).as_posix(),
            "sha256": receipt_hash,
            "model_identity": canonical,
        }
        claims.append(claim)

    review_path = root / "reviews" / f"{model}.json"
    review = {
        "provider": provider,
        "upstream_model": model,
        "reviewer_type": "human",
        "reviewer_id": "reviewer-001",
        "reviewer_model": None,
        "reviewer_role": "external acceptance reviewer",
        "decision": "ACCEPT",
        "reviewed_at_utc": "2026-10-12T08:16:00+00:00",
    }
    review_hash = _write_json(
        review_path,
        {
            "model_identity": canonical,
            "reviewer_type": review["reviewer_type"],
            "reviewer_id": review["reviewer_id"],
            "reviewer_model": review["reviewer_model"],
            "reviewer_role": review["reviewer_role"],
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )
    review["receipt"] = {
        "path": review_path.relative_to(root).as_posix(),
        "sha256": review_hash,
        "model_identity": canonical,
    }
    return claims, review


def _report(
    tmp_path: Path,
    claims: list[dict[str, object]],
    reviews: list[object] | None = None,
) -> dict[str, object]:
    inventory_path = tmp_path / "inventory.csv"
    _write_inventory(inventory_path, IDENTITIES)
    inventory = load_inventory(
        inventory_path,
        expected_count=len(IDENTITIES),
        expected_identity_sha256=identity_projection_sha256(IDENTITIES),
    )
    document = {
        "schema_version": 1,
        "identity_set_sha256": inventory["identity_set_sha256"],
        "claims": claims,
        "primary_reviews": reviews or [],
    }
    return build_report(
        inventory,
        document,
        claims_sha256="a" * 64,
        claims_path="claims.json",
        repo_root=tmp_path,
    )


def _model(report: dict[str, object], model: str = "Alpha") -> dict[str, object]:
    models = report["models"]
    assert isinstance(models, list)
    return next(item for item in models if item["upstream_model"] == model)


def test_fixed_inventory_pin_rejects_same_count_identity_replacement(tmp_path: Path) -> None:
    inventory_path = tmp_path / "inventory.csv"
    changed = [("sample", "Alpha"), ("sample", "Gamma")]
    _write_inventory(inventory_path, changed)

    with pytest.raises(EvidenceIndexError, match="identity set changed"):
        load_inventory(
            inventory_path,
            expected_count=len(IDENTITIES),
            expected_identity_sha256=identity_projection_sha256(IDENTITIES),
        )

    _write_inventory(inventory_path, [IDENTITIES[0], IDENTITIES[0]])
    with pytest.raises(EvidenceIndexError, match="duplicates"):
        load_inventory(
            inventory_path,
            expected_count=len(IDENTITIES),
            expected_identity_sha256=identity_projection_sha256(IDENTITIES),
        )


def test_no_claims_are_unreviewed_and_index_contains_all_fixed_identities(tmp_path: Path) -> None:
    report = _report(tmp_path, [])

    assert report["summary"]["identity_count"] == 2
    assert report["summary"]["offline_acceptance_count"] == 0
    assert report["summary"]["primary_review_count"] == 0
    assert [row["status"] for row in report["models"]] == ["UNREVIEWED", "UNREVIEWED"]
    assert all(
        row["primary_review"]["evidence_snapshot_status"] is None for row in report["models"]
    )
    markdown = render_markdown(report)
    assert "add implementation claim" in markdown
    assert "primary-model or human review receipt" in markdown


def test_explicit_complete_claims_need_external_primary_acceptance_receipt(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "OFFLINE_ACCEPTED"
    assert report["summary"]["offline_acceptance_count"] == 1
    counterfact = _model(report)["phases"]["counterfact"]["counterfact"]
    assert claims[-1]["exit_code"] == 0
    assert counterfact["expected"]["probe_exit_code"] == 1
    assert counterfact["observed"]["probe_exit_code"] == 1
    review = _model(report)["primary_review"]
    assert review["reviewer_type"] == "human"
    assert review["reviewer_id"] == "reviewer-001"
    assert review["reviewer_model"] is None
    assert review["evidence_snapshot_status"] == "MATCHED"


def test_primary_model_review_is_explicitly_identified_and_can_accept(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "primary_model"
    review["reviewer_id"] = "root-primary-review"
    review["reviewer_model"] = "gpt-6.1-sol"
    review["reviewer_role"] = "primary requirements and acceptance reviewer"
    receipt_path = tmp_path / review["receipt"]["path"]
    review["receipt"]["sha256"] = _write_json(
        receipt_path,
        {
            "model_identity": "sample::Alpha",
            "reviewer_type": review["reviewer_type"],
            "reviewer_id": review["reviewer_id"],
            "reviewer_model": review["reviewer_model"],
            "reviewer_role": review["reviewer_role"],
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )
    report = _report(tmp_path, claims, [review])

    primary_review = _model(report)["primary_review"]
    assert _model(report)["status"] == "OFFLINE_ACCEPTED"
    assert primary_review["reviewer_type"] == "primary_model"
    assert primary_review["reviewer_id"] == "root-primary-review"
    assert primary_review["reviewer_model"] == "gpt-6.1-sol"


def test_old_primary_acceptance_is_stale_after_phase_receipts_and_pins_change(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review_path = tmp_path / review["receipt"]["path"]
    old_review_receipt = review_path.read_bytes()

    pins = claims[0]["pins"]
    for group, contents in (
        ("source", "new provider source"),
        ("contract", "new public contract"),
    ):
        old_pin = pins[group][0]
        artifact = tmp_path / old_pin["path"]
        artifact.write_text(contents, encoding="utf-8")
        pins[group] = [_pin(artifact, tmp_path)]
    pins["contract_version"] = f"sha256:{pins['contract'][0]['after_sha256']}"

    for claim in claims:
        receipt_path = tmp_path / claim["receipt"]["path"]
        receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt_document["pins"] = claim["pins"]
        claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)

    # A byte-identical receipt at a new path is still a new reviewed artifact.
    first_claim = claims[0]
    old_receipt_path = tmp_path / first_claim["receipt"]["path"]
    new_receipt_path = tmp_path / "receipts" / "replacement-implementation.json"
    new_receipt_path.write_bytes(old_receipt_path.read_bytes())
    first_claim["receipt"]["path"] = new_receipt_path.relative_to(tmp_path).as_posix()

    report = _report(tmp_path, claims, [review])

    model = _model(report)
    assert model["status"] == "STALE"
    assert "primary_review_evidence_snapshot_mismatch" in model["primary_review"]["issues"]
    assert model["primary_review"]["evidence_snapshot_status"] == "MISMATCH"
    assert review_path.read_bytes() == old_review_receipt


def test_primary_review_snapshot_is_required(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review_path = tmp_path / review["receipt"]["path"]
    review_document = json.loads(review_path.read_text(encoding="utf-8"))
    review_document.pop("evidence_snapshot")
    review["receipt"]["sha256"] = _write_json(review_path, review_document)

    report = _report(tmp_path, claims, [review])

    model = _model(report)
    assert model["status"] == "INCOMPLETE"
    assert (
        "primary_review_evidence_snapshot_missing_or_invalid" in model["primary_review"]["issues"]
    )
    assert model["primary_review"]["evidence_snapshot_status"] == "MISSING_OR_INVALID"


def test_non_object_primary_review_is_reported_and_keeps_snapshot_null(tmp_path: Path) -> None:
    report = _report(tmp_path, [], [None])

    assert report["invalid_claims"] == [{"kind": "primary_review", "issues": ["review_not_object"]}]
    assert _model(report)["primary_review"]["evidence_snapshot_status"] is None


def test_duplicate_primary_reviews_have_no_snapshot_status(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    report = _report(tmp_path, claims, [review, dict(review)])

    model = _model(report)
    assert model["status"] == "STALE"
    assert model["primary_review"]["issues"] == ["duplicate_primary_review"]
    assert model["primary_review"]["evidence_snapshot_status"] is None


def test_primary_review_with_missing_receipt_keeps_invalid_snapshot_status(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["receipt"] = None
    report = _report(tmp_path, claims, [review])

    model = _model(report)
    assert model["primary_review"]["evidence_snapshot_status"] == "MISSING_OR_INVALID"
    assert "primary_review_receipt_missing" in model["primary_review"]["issues"]


def test_primary_model_review_without_model_identity_cannot_accept(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "primary_model"
    review["reviewer_model"] = None
    receipt_path = tmp_path / review["receipt"]["path"]
    review["receipt"]["sha256"] = _write_json(
        receipt_path,
        {
            "model_identity": "sample::Alpha",
            "reviewer_type": review["reviewer_type"],
            "reviewer_id": review["reviewer_id"],
            "reviewer_model": None,
            "reviewer_role": review["reviewer_role"],
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "primary_review_model_missing" in _model(report)["primary_review"]["issues"]


def test_counterfact_receipt_requires_matching_expected_observed_and_reason(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    claim = next(item for item in claims if item["phase"] == "counterfact")
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["counterfact"]["observed"]["outcome"] = "accepted"
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt)
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "counterfact_observation_mismatch" in _model(report)["phases"]["counterfact"]["issues"]


def test_counterfact_receipt_missing_expected_observed_or_reason_is_incomplete(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    claim = next(item for item in claims if item["phase"] == "counterfact")
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["counterfact"].pop("expected")
    receipt["counterfact"].pop("observed")
    receipt["counterfact"].pop("reason")
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt)
    report = _report(tmp_path, claims, [review])

    issues = _model(report)["phases"]["counterfact"]["issues"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert "counterfact_expected_missing" in issues
    assert "counterfact_observed_missing" in issues
    assert "counterfact_reason_missing" in issues


def test_counterfact_cannot_pass_on_the_negative_probe_exit_alone(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    counterfact = next(item for item in claims if item["phase"] == "counterfact")
    counterfact["exit_code"] = 1
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "command_exit_nonzero" in _model(report)["phases"]["counterfact"]["issues"]


def test_claims_identity_set_pin_mismatch_blocks_acceptance(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    inventory_path = tmp_path / "inventory.csv"
    _write_inventory(inventory_path, IDENTITIES)
    inventory = load_inventory(
        inventory_path,
        expected_count=len(IDENTITIES),
        expected_identity_sha256=identity_projection_sha256(IDENTITIES),
    )
    bad_document = {
        "schema_version": 1,
        "identity_set_sha256": "0" * 64,
        "claims": claims,
        "primary_reviews": [review],
    }
    report = build_report(
        inventory,
        bad_document,
        claims_sha256="a" * 64,
        claims_path="claims.json",
        repo_root=tmp_path,
    )

    assert _model(report)["status"] == "INCOMPLETE"
    assert "claims_identity_set_pin_mismatch" in report["global_issues"]


def test_claim_pass_alone_or_ready_console_cannot_promote_identity(tmp_path: Path) -> None:
    claims, _ = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claims[0]["status"] = "READY"
    claims[0]["actual_command"] = "self-test console"
    claims[0]["execution_mode"] = "self_test_console"
    report = _report(tmp_path, claims)
    model = _model(report)

    assert model["status"] == "INCOMPLETE"
    assert set(model["missing_phases"]) == set(REQUIRED_PHASES) - {"service"}
    assert "claim_status_unrecognized" in model["phases"]["service"]["issues"]
    assert "status_or_console_is_not_a_command" in model["phases"]["service"]["issues"]


def test_missing_fixture_and_probe_pins_are_incomplete(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claims[0]["pins"].pop("fixture")
    claims[0]["pins"].pop("probe")
    report = _report(tmp_path, claims, [review])

    issues = _model(report)["phases"]["service"]["issues"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert "fixture_pins_missing" in issues
    assert "probe_pins_missing" in issues


def test_missing_phase_keeps_otherwise_passing_claims_incomplete(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=REQUIRED_PHASES[:-1])
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert _model(report)["missing_phases"] == ["counterfact"]


def test_duplicate_identity_phase_claim_is_reported_stale(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claims.append(dict(claims[0]))
    report = _report(tmp_path, claims, [review])

    model = _model(report)
    assert model["status"] == "STALE"
    assert model["phases"]["service"]["issues"] == ["duplicate_identity_phase_claim"]


def test_receipt_tamper_is_stale(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    receipt_path = tmp_path / claims[0]["receipt"]["path"]
    receipt_path.write_text('{"model_identity":"sample::Alpha","tampered":true}', encoding="utf-8")
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "STALE"
    assert "claim_receipt_hash_drift" in _model(report)["phases"]["service"]["issues"]


@pytest.mark.parametrize("receipt_document", [[], None, "not an object", 42])
def test_non_object_json_receipt_is_incomplete_without_crashing_report(
    tmp_path: Path, receipt_document: object
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    claim = claims[0]
    receipt_path = tmp_path / claim["receipt"]["path"]
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)

    report = _report(tmp_path, claims, [review])

    phase = _model(report)["phases"]["implementation"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert "receipt_not_json_object" in phase["issues"]


def test_pre_post_source_drift_is_stale(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    source_pin = claims[0]["pins"]["source"][0]
    source_pin["before_sha256"] = "b" * 64
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "STALE"
    assert "source_pin_1_pre_post_hash_drift" in _model(report)["phases"]["service"]["issues"]


def test_current_source_hash_drift_is_stale(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    source_path = tmp_path / claims[0]["pins"]["source"][0]["path"]
    source_path.write_text("changed after evidence", encoding="utf-8")
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "STALE"
    assert "source_pin_1_current_hash_drift" in _model(report)["phases"]["service"]["issues"]


def test_public_contract_version_change_is_stale(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    contract_path = tmp_path / claims[0]["pins"]["contract"][0]["path"]
    contract_path.write_text("new public typed contract", encoding="utf-8")
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "STALE"
    assert "contract_pin_1_current_hash_drift" in _model(report)["phases"]["service"]["issues"]


def test_changed_contract_version_fingerprint_is_invalid(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claims[0]["pins"]["contract_version"] = "sha256:" + "c" * 64
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "contract_version_pin_mismatch" in _model(report)["phases"]["service"]["issues"]


def test_nonzero_exit_and_wrong_model_receipt_do_not_accept(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",), exit_code=2)
    report = _report(tmp_path, claims, [review])
    assert _model(report)["status"] == "INCOMPLETE"
    assert "command_exit_nonzero" in _model(report)["phases"]["service"]["issues"]

    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    receipt = tmp_path / claims[0]["receipt"]["path"]
    receipt_hash = _write_json(
        receipt,
        _execution_receipt_document(claims[0], {"model_identity": "sample::Beta"}),
    )
    claims[0]["receipt"]["sha256"] = receipt_hash
    report = _report(tmp_path, claims, [review])
    assert _model(report)["status"] == "INCOMPLETE"
    assert "wrong_model_receipt" in _model(report)["phases"]["service"]["issues"]


@pytest.mark.parametrize(
    ("receipt_patch", "expected_issue"),
    [
        (
            {"receipt_schema_version": 2},
            "claim_receipt_schema_version_unsupported",
        ),
        (
            {"status": "FAIL", "execution_status": "FAIL"},
            "claim_receipt_status_mismatch",
        ),
        ({"exit_code": 1}, "claim_receipt_exit_code_mismatch"),
        ({"actual_command": "pytest another_test.py"}, "claim_receipt_command_mismatch"),
        (
            {"executed_at_utc": "2026-10-12T08:15:01Z"},
            "claim_receipt_executed_at_utc_mismatch",
        ),
    ],
)
def test_claim_pass_cannot_override_receipt_execution_failure_or_mismatch(
    tmp_path: Path,
    receipt_patch: dict[str, object],
    expected_issue: str,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claim = claims[0]
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
    execution = receipt_document["machine_execution"]
    if "receipt_schema_version" in receipt_patch:
        receipt_document["receipt_schema_version"] = receipt_patch["receipt_schema_version"]
    if "status" in receipt_patch:
        receipt_document["status"] = receipt_patch["status"]
        execution["status"] = receipt_patch["execution_status"]
    for field in ("exit_code", "actual_command", "executed_at_utc"):
        if field in receipt_patch:
            execution[field] = receipt_patch[field]
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)

    report = _report(tmp_path, claims, [review])

    phase = _model(report)["phases"]["service"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert expected_issue in phase["issues"]


def test_machine_execution_time_matches_as_utc_instant(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    claim = claims[0]
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_document["machine_execution"]["executed_at_utc"] = "2026-10-12T08:15:00+00:00"
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)
    _refresh_review_evidence_snapshot(tmp_path, claims, review)

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "OFFLINE_ACCEPTED"


def test_source_review_requires_independent_machine_execution_record(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("direct",))
    claim = claims[0]
    claim["execution_mode"] = "source_review"
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_document.pop("machine_execution")
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "source_review_machine_execution_missing" in _model(report)["phases"]["direct"]["issues"]


def test_old_receipt_cannot_be_rebound_to_forged_current_source_pin(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claim = claims[0]
    old_receipt_bytes = (tmp_path / claim["receipt"]["path"]).read_bytes()
    forged_source = tmp_path / "src-current.py"
    forged_source.write_text("current source pin", encoding="utf-8")
    claim["pins"]["source"] = [_pin(forged_source, tmp_path)]

    report = _report(tmp_path, claims, [review])

    phase = _model(report)["phases"]["service"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert "claim_receipt_pins_mismatch" in phase["issues"]
    assert (tmp_path / claim["receipt"]["path"]).read_bytes() == old_receipt_bytes


def test_receipt_pin_groups_may_be_reordered_but_must_match_exactly(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    claim = claims[0]
    extra_fixture = tmp_path / "fixtures" / "extra.json"
    extra_fixture.write_text('{"fixture": "extra"}', encoding="utf-8")
    claim["pins"]["fixture"].append(_pin(extra_fixture, tmp_path))

    for claim in claims:
        receipt_path = tmp_path / claim["receipt"]["path"]
        receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt_document["pins"]["fixture"] = list(reversed(claim["pins"]["fixture"]))
        claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)
    _refresh_review_evidence_snapshot(tmp_path, claims, review)

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "OFFLINE_ACCEPTED", _model(report)


def test_legacy_receipt_without_pin_snapshot_is_incomplete(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claim = claims[0]
    receipt_path = tmp_path / claim["receipt"]["path"]
    receipt_document = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_document.pop("pins")
    claim["receipt"]["sha256"] = _write_json(receipt_path, receipt_document)

    report = _report(tmp_path, claims, [review])

    phase = _model(report)["phases"]["service"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert "claim_receipt_pins_missing_or_invalid" in phase["issues"]


@pytest.mark.parametrize(
    ("receipt_document", "expected_issue"),
    [
        ({"model": "Alpha"}, "receipt_has_no_machine_readable_model_identity"),
        ({"models": ["Alpha"]}, "receipt_has_no_machine_readable_model_identity"),
        (
            {"model_identity": "sample::Alpha", "provider": "other"},
            "wrong_model_receipt",
        ),
        (
            {"model_identity": "sample::Alpha", "model": "Beta"},
            "wrong_model_receipt",
        ),
        (
            {"models": [{"provider": "other", "upstream_model": "Alpha"}]},
            "wrong_model_receipt",
        ),
        (
            {"models": [{"model_identity": "sample::Alpha", "upstream_model": "Beta"}]},
            "wrong_model_receipt",
        ),
    ],
)
def test_receipt_identity_requires_provider_model_pair_and_rejects_cross_source_collision(
    tmp_path: Path,
    receipt_document: dict[str, object],
    expected_issue: str,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    receipt = tmp_path / claims[0]["receipt"]["path"]
    claims[0]["receipt"]["sha256"] = _write_json(
        receipt, _execution_receipt_document(claims[0], receipt_document)
    )
    _refresh_review_evidence_snapshot(tmp_path, claims, review)

    report = _report(tmp_path, claims, [review])

    phase = _model(report)["phases"]["implementation"]
    assert _model(report)["status"] == "INCOMPLETE"
    assert expected_issue in phase["issues"]


@pytest.mark.parametrize(
    "receipt_document",
    [
        {"model": {"provider": "sample", "upstream_model": "Alpha"}},
        {"provider": "sample", "models": ["Alpha"]},
        {"models": [{"provider": "sample", "upstream_model": "Alpha"}]},
    ],
)
def test_receipt_accepts_full_provider_model_pair(
    tmp_path: Path, receipt_document: dict[str, object]
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    receipt = tmp_path / claims[0]["receipt"]["path"]
    claims[0]["receipt"]["sha256"] = _write_json(
        receipt, _execution_receipt_document(claims[0], receipt_document)
    )
    _refresh_review_evidence_snapshot(tmp_path, claims, review)

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "OFFLINE_ACCEPTED"


def test_primary_review_receipt_must_match_exact_reviewer_model(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "primary_model"
    review["reviewer_id"] = "root-primary-review"
    review["reviewer_model"] = "gpt-6.1-sol"
    review["reviewer_role"] = "primary requirements and acceptance reviewer"
    receipt_path = tmp_path / review["receipt"]["path"]
    review["receipt"]["sha256"] = _write_json(
        receipt_path,
        {
            "model_identity": "sample::Alpha",
            "reviewer_type": "primary_model",
            "reviewer_id": "root-primary-review",
            "reviewer_model": "gpt-6-luna",
            "reviewer_role": "primary requirements and acceptance reviewer",
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert (
        "primary_review_receipt_reviewer_model_mismatch"
        in _model(report)["primary_review"]["issues"]
    )


def test_primary_model_receipt_identity_match_is_required_for_acceptance(
    tmp_path: Path,
) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "primary_model"
    review["reviewer_id"] = "root-primary-review"
    review["reviewer_model"] = "gpt-6.1-sol"
    review["reviewer_role"] = "primary requirements and acceptance reviewer"
    receipt_path = tmp_path / review["receipt"]["path"]
    review["receipt"]["sha256"] = _write_json(
        receipt_path,
        {
            "model_identity": "sample::Alpha",
            "reviewer_type": "primary_model",
            "reviewer_id": "different-reviewer",
            "reviewer_model": "gpt-6.1-sol",
            "reviewer_role": "primary requirements and acceptance reviewer",
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert (
        "primary_review_receipt_reviewer_id_mismatch" in _model(report)["primary_review"]["issues"]
    )


def test_worker_review_cannot_self_approve(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "worker"
    review["reviewer_id"] = "implementation-worker"
    review["reviewer_model"] = "gpt-6-luna"
    review["reviewer_role"] = "implementation worker"
    receipt_path = tmp_path / review["receipt"]["path"]
    review["receipt"]["sha256"] = _write_json(
        receipt_path,
        {
            "model_identity": "sample::Alpha",
            "reviewer_type": "worker",
            "reviewer_id": "implementation-worker",
            "reviewer_model": "gpt-6-luna",
            "reviewer_role": "implementation worker",
            "decision": review["decision"],
            "reviewed_at_utc": review["reviewed_at_utc"],
            "evidence_snapshot": _review_evidence_snapshot_document(claims),
        },
    )

    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "primary_review_reviewer_type_unrecognized" in _model(report)["primary_review"]["issues"]


def test_tool_review_cannot_substitute_for_external_primary(tmp_path: Path) -> None:
    claims, review = _claim_bundle(tmp_path, IDENTITIES[0])
    review["reviewer_type"] = "tool"
    report = _report(tmp_path, claims, [review])

    assert _model(report)["status"] == "INCOMPLETE"
    assert "primary_review_reviewer_type_unrecognized" in _model(report)["primary_review"]["issues"]


def test_unknown_claim_identity_is_not_added_to_inventory(tmp_path: Path) -> None:
    claims, _ = _claim_bundle(tmp_path, IDENTITIES[0], phases=("service",))
    claims[0]["provider"] = "unknown"
    report = _report(tmp_path, claims)

    assert len(report["models"]) == 2
    assert report["invalid_claims"][0]["issues"] == ["unknown_inventory_identity"]
