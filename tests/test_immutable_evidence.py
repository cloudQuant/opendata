"""Adversarial tests for explicit immutable-evidence classification and A2 partitioning."""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.quality import immutable_evidence as evidence

SNAPSHOT_PATH = "docs/evidence/C100/test-run/snapshot.py"
ACTIVE_PATH = "docs/evidence/C100/test-run/probe.py"
FEDERAL_RESERVE_PROBE_PATH = (
    "docs/evidence/C100/acceptance-quality-preservation/federal-reserve/probe_federal_reserve.py"
)
RECEIPT_PATH = "docs/evidence/C100/test-run/receipt.json"
REVIEW_PATH = "docs/evidence/C100/test-run/review.json"


@dataclass
class EvidenceFixture:
    """One synthetic pinned classification, source, receipt, and manifest."""

    root: Path
    manifest: dict[str, Any]
    snapshot_sha256: str
    classification_sha256: str
    receipt_sha256: str
    approval_sha256: str | None = None


def _digest(data: bytes) -> str:
    """Return SHA-256 for test evidence bytes."""
    return hashlib.sha256(data).hexdigest()


def _write_json(path: Path, value: object) -> str:
    """Write stable test JSON and return its byte digest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2) + "\n").encode()
    path.write_bytes(raw)
    return _digest(raw)


def _fixture(root: Path) -> EvidenceFixture:
    """Create a pending candidate whose evidence is wholly synthetic and offline."""
    snapshot = root / SNAPSHOT_PATH
    active = root / ACTIVE_PATH
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    active.write_text("def current_probe() -> str:\n    return 'current'\n", encoding="utf-8")
    snapshot_raw = b"frozen probe snapshot\n"
    snapshot.write_bytes(snapshot_raw)
    snapshot_sha = _digest(snapshot_raw)

    classification = {
        "generated_at_utc": "2026-10-12T14:09:39Z",
        "files": [
            {
                "path": SNAPSHOT_PATH,
                "category": "B",
                "captured_source_sha256": snapshot_sha,
                "ruff_finding_count": 5,
            }
        ],
    }
    classification_sha = _write_json(root / evidence.CLASSIFICATION_PATH, classification)
    receipt = {
        "capture": {"source_sha256": snapshot_sha},
        "command": ["python", "-m", "pytest", "tests/test_probe.py"],
        "started_utc": "2026-10-12T14:09:39.243218Z",
    }
    receipt_sha = _write_json(root / RECEIPT_PATH, receipt)
    manifest: dict[str, Any] = {
        "schema_version": evidence.MANIFEST_SCHEMA_VERSION,
        "classification": {
            "path": evidence.CLASSIFICATION_PATH,
            "sha256": classification_sha,
            "run_id": evidence.CLASSIFICATION_RUN_ID,
            "eligible_category": "B",
        },
        "active_entrypoints": [ACTIVE_PATH],
        "historical_diagnostics": {
            "ruff_count": 5,
            "classification_sha256": classification_sha,
            "status": "NOT_GREEN_HISTORICAL_FINDINGS",
        },
        "approval": {
            "status": "pending_review",
            "artifact_path": None,
            "artifact_sha256": None,
        },
        "candidates": [
            {
                "path": SNAPSHOT_PATH,
                "sha256": snapshot_sha,
                "classification_category": "B",
                "active_entrypoint": ACTIVE_PATH,
                "source_path": None,
                "archival_reason": (
                    "Preserve exact bytes captured by a run receipt so the historical "
                    "execution remains reproducible."
                ),
                "receipt": {
                    "path": RECEIPT_PATH,
                    "sha256": receipt_sha,
                    "source_sha256_pointer": "/capture/source_sha256",
                    "command_pointer": "/command",
                    "executed_at_utc_pointer": "/started_utc",
                    "missing_execution_time_reason": None,
                    "installed_directory_pointer": None,
                    "source_relative_path_pointer": None,
                },
                "review": {
                    "status": "pending",
                    "reviewer": None,
                    "reviewed_at_utc": None,
                    "decision": None,
                    "note": "Awaiting independent review.",
                },
            }
        ],
    }
    return EvidenceFixture(root, manifest, snapshot_sha, classification_sha, receipt_sha)


def _set_classification_ruff_finding_count(
    fixture: EvidenceFixture,
    finding_count: object,
    *,
    remove: bool = False,
) -> None:
    """Change one fixture B-row finding count and refresh its explicit pins."""
    path = fixture.root / evidence.CLASSIFICATION_PATH
    classification = json.loads(path.read_text(encoding="utf-8"))
    row = next(item for item in classification["files"] if item["path"] == SNAPSHOT_PATH)
    if remove:
        row.pop("ruff_finding_count", None)
    else:
        row["ruff_finding_count"] = finding_count
    fixture.classification_sha256 = _write_json(path, classification)
    fixture.manifest["classification"]["sha256"] = fixture.classification_sha256
    fixture.manifest["historical_diagnostics"]["classification_sha256"] = (
        fixture.classification_sha256
    )


def _validate(
    fixture: EvidenceFixture,
    *,
    expected_manifest_sha256: str | None = None,
    expected_approval_sha256: str | None = None,
) -> evidence.EvidenceValidation:
    """Persist the fixture manifest and validate it against explicit test pins."""
    manifest_path = fixture.root / evidence.MANIFEST_PATH
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(fixture.manifest, indent=2) + "\n").encode()
    manifest_path.write_bytes(raw)
    return evidence.validate_manifest(
        fixture.root,
        expected_manifest_sha256=(
            expected_manifest_sha256 if expected_manifest_sha256 is not None else _digest(raw)
        ),
        expected_classification_sha256=fixture.classification_sha256,
        expected_approval_sha256=expected_approval_sha256,
    )


def _approve(fixture: EvidenceFixture) -> str:
    """Create an exact-row review artifact and bind its digest into the manifest."""
    row = fixture.manifest["candidates"][0]
    review = {
        "status": "approved",
        "reviewer": "root-reviewer",
        "reviewed_at_utc": "2026-10-12T15:00:00Z",
        "decision": evidence.IMMUTABLE_DECISION,
        "note": "Reviewed the exact snapshot and receipt binding.",
    }
    row["review"] = review
    artifact = {
        "schema_version": evidence.APPROVAL_SCHEMA_VERSION,
        "decision": "approve_exact_manifest_rows",
        "items": [
            {
                "path": row["path"],
                "sha256": row["sha256"],
                "receipt_path": row["receipt"]["path"],
                "receipt_sha256": row["receipt"]["sha256"],
                "source_sha256_pointer": row["receipt"]["source_sha256_pointer"],
                "decision": evidence.IMMUTABLE_DECISION,
                "reviewer": review["reviewer"],
                "reviewed_at_utc": review["reviewed_at_utc"],
            }
        ],
    }
    digest = _write_json(fixture.root / REVIEW_PATH, artifact)
    fixture.manifest["approval"] = {
        "status": "approved",
        "artifact_path": REVIEW_PATH,
        "artifact_sha256": digest,
    }
    fixture.approval_sha256 = digest
    return digest


# The repository manifest pins 5055 snapshot paths and 343 live entrypoints of the
# archived evidence corpus. The corpus lives outside a fresh checkout by ruling
# (gitignored since C113; owner moved it out of the working tree on 2026-10-23),
# so this one test only runs where the corpus is actually present - the sentinel
# is the manifest's own classification source, not a guess: when it exists the
# test still runs and still reddens on any genuine manifest drift.
_CORPUS_SENTINEL = (
    Path(__file__).resolve().parents[1]
    / "docs/evidence/C100/quality-scope-audit/immutable-wave9-exact5055/classification.json"
)


@pytest.mark.skipif(
    not _CORPUS_SENTINEL.is_file(),
    reason="archived evidence corpus (docs/evidence/C100) is not present in this checkout",
)
def test_repository_manifest_sums_all_exact5055_classification_findings() -> None:
    """The exact5055 manifest accounts for findings and keeps live A2 code."""
    root = Path(__file__).resolve().parents[1]
    validation = evidence.validate_manifest(root)

    assert not validation.errors
    assert not validation.blocked_paths
    assert len(validation.snapshot_paths) == 5055
    assert len(validation.excluded_paths) == 5055
    assert validation.historical_ruff_count == 149323
    source_trio_probe = (
        "docs/evidence/C100/acceptance-quality-preservation/source-trio/probe_source_trio.py"
    )
    active_examples = [
        source_trio_probe,
        FEDERAL_RESERVE_PROBE_PATH,
        "docs/evidence/C100/acceptance-quality-preservation/imf-intrinio-remaining-typing/offline_probe.py",
        "docs/evidence/C100/acceptance-quality-preservation/index-projection-probe-maintenance/probe_index_projection_maintenance.py",
        "docs/evidence/C100/acceptance-quality-preservation/model-evidence-typing/probe_model_evidence_typing.py",
        "docs/evidence/C100/acceptance-ac24-independent/run_pytest_io_guard_combined_a2_stage3.py",
        "docs/evidence/C100/acceptance-quality-preservation/sec-remainder-typing/probe.py",
        "docs/evidence/C100/acceptance-quality-preservation/shared-contract-probe-maintenance/review_shared_contract_probe_maintenance.py",
        "docs/evidence/C100/acceptance-quality-preservation/tmx-contract-typing/offline_probe.py",
    ]
    assert len(validation.active_entrypoints) == 343
    assert all(path in validation.active_entrypoints for path in active_examples)
    assert all(path not in validation.excluded_paths for path in active_examples)
    tool_files, excluded = evidence.partition_static_files(
        [*active_examples, *validation.snapshot_paths], validation
    )
    assert tool_files == active_examples
    assert len(excluded) == 5055


def test_historical_ruff_count_matches_typed_classification_row_sum(tmp_path: Path) -> None:
    """A valid aggregate is exact; stale or malformed row counts fail closed."""
    fixture = _fixture(tmp_path)
    _set_classification_ruff_finding_count(fixture, 2110)
    fixture.manifest["historical_diagnostics"]["ruff_count"] = 2110
    approval_sha = _approve(fixture)

    valid = _validate(fixture, expected_approval_sha256=approval_sha)
    assert not valid.blocked
    assert valid.historical_ruff_count == 2110

    for incorrect_total in (0, 1120, 2111):
        fixture.manifest["historical_diagnostics"]["ruff_count"] = incorrect_total
        invalid = _validate(fixture, expected_approval_sha256=approval_sha)
        assert invalid.blocked
        assert any(
            "historical_diagnostics.ruff_count must equal the sum of pinned classification B-row"
            in error
            for error in invalid.errors
        )

    fixture.manifest["historical_diagnostics"]["ruff_count"] = 2110
    for malformed_count in (True, -1, None, "2110"):
        _set_classification_ruff_finding_count(fixture, malformed_count)
        invalid = _validate(fixture, expected_approval_sha256=approval_sha)
        assert invalid.blocked
        assert any(
            "pinned classification B row ruff_finding_count must be a non-negative integer" in error
            for error in invalid.errors
        )

    _set_classification_ruff_finding_count(fixture, None, remove=True)
    missing = _validate(fixture, expected_approval_sha256=approval_sha)
    assert missing.blocked
    assert any(
        "pinned classification B row ruff_finding_count must be a non-negative integer" in error
        for error in missing.errors
    )


def test_pending_rows_are_blocked_and_never_excluded(tmp_path: Path) -> None:
    """Unreviewed candidates remain in A2's original static execution set."""
    fixture = _fixture(tmp_path)
    validation = _validate(fixture)

    assert validation.blocked
    assert validation.excluded_paths == ()
    assert validation.blocked_paths == (SNAPSHOT_PATH,)
    assert "candidate awaits independent review" in validation.blocked_details[0][1]
    tool_files, excluded = evidence.partition_static_files([SNAPSHOT_PATH, ACTIVE_PATH], validation)
    assert tool_files == [SNAPSHOT_PATH, ACTIVE_PATH]
    assert excluded == []


def test_exact_approved_row_partitions_only_that_file(tmp_path: Path) -> None:
    """A pinned review can remove one historical snapshot while retaining active code."""
    fixture = _fixture(tmp_path)
    approval_sha = _approve(fixture)
    validation = _validate(fixture, expected_approval_sha256=approval_sha)

    assert not validation.blocked
    assert validation.excluded_paths == (SNAPSHOT_PATH,)
    tool_files, excluded = evidence.partition_static_files([SNAPSHOT_PATH, ACTIVE_PATH], validation)
    assert tool_files == [ACTIVE_PATH]
    assert excluded == [SNAPSHOT_PATH]


def test_new_unreviewed_file_never_inherits_directory_exemption(tmp_path: Path) -> None:
    """A new neighbor stays in the original A2 tool set even beside an approved copy."""
    fixture = _fixture(tmp_path)
    approval_sha = _approve(fixture)
    validation = _validate(fixture, expected_approval_sha256=approval_sha)
    new_file = "docs/evidence/C100/test-run/new_probe.py"
    (tmp_path / new_file).write_text("new_value = 1\n", encoding="utf-8")

    tool_files, excluded = evidence.partition_static_files(
        [SNAPSHOT_PATH, ACTIVE_PATH, new_file], validation
    )
    assert tool_files == [ACTIVE_PATH, new_file]
    assert excluded == [SNAPSHOT_PATH]


def test_wrong_source_hash_blocks_the_row(tmp_path: Path) -> None:
    """A manifest source hash cannot override the pinned classification or actual bytes."""
    fixture = _fixture(tmp_path)
    fixture.manifest["candidates"][0]["sha256"] = "f" * 64
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert any("source sha256 differs" in item for item in validation.blocked_details[0][1])


def test_wrong_receipt_hash_blocks_the_row(tmp_path: Path) -> None:
    """The receipt's current bytes must match its individually pinned digest."""
    fixture = _fixture(tmp_path)
    fixture.manifest["candidates"][0]["receipt"]["sha256"] = "0" * 64
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert any("receipt hash mismatch" in item for item in validation.blocked_details[0][1])


def test_wrong_json_pointer_blocks_the_row(tmp_path: Path) -> None:
    """A missing receipt pointer cannot be substituted with an unbound claim."""
    fixture = _fixture(tmp_path)
    fixture.manifest["candidates"][0]["receipt"]["source_sha256_pointer"] = "/missing/hash"
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert any("does not resolve" in item for item in validation.blocked_details[0][1])


def test_receipt_binding_to_another_source_hash_blocks_the_row(tmp_path: Path) -> None:
    """The receipt pointer must resolve to this candidate's exact bytes."""
    fixture = _fixture(tmp_path)
    receipt_path = tmp_path / RECEIPT_PATH
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["capture"]["source_sha256"] = "1" * 64
    receipt_sha = _write_json(receipt_path, receipt)
    fixture.manifest["candidates"][0]["receipt"]["sha256"] = receipt_sha
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert any(
        "expected" in item and "source hash pointer" in item
        for item in validation.blocked_details[0][1]
    )


def test_missing_candidate_file_blocks_without_following_paths(tmp_path: Path) -> None:
    """A deleted snapshot remains a hard blocker instead of becoming an empty pass."""
    fixture = _fixture(tmp_path)
    (tmp_path / SNAPSHOT_PATH).unlink()
    validation = _validate(fixture)

    assert validation.blocked
    assert validation.excluded_paths == ()
    assert any("missing or unreadable" in item for item in validation.errors)


def test_path_traversal_and_unknown_schema_are_rejected(tmp_path: Path) -> None:
    """Canonical path and exact-key schema validation fail closed."""
    fixture = _fixture(tmp_path)
    fixture.manifest["candidates"][0]["path"] = "../outside.py"
    validation = _validate(fixture)
    assert validation.excluded_paths == ()
    assert any("not a canonical root-relative path" in item for item in validation.errors)

    second = _fixture(tmp_path / "unknown")
    second.manifest["unrecognized"] = True
    invalid = _validate(second)
    assert invalid.excluded_paths == ()
    assert any("unknown" in item for item in invalid.errors)


def test_symlink_candidate_is_rejected(tmp_path: Path) -> None:
    """A candidate cannot use a symlink to point outside its evidence root."""
    fixture = _fixture(tmp_path)
    snapshot = tmp_path / SNAPSHOT_PATH
    snapshot.unlink()
    external = tmp_path / "outside.py"
    external.write_text("payload = 1\n", encoding="utf-8")
    snapshot.symlink_to(external)
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert any("symlink" in item for item in validation.errors)


def test_active_entrypoint_cannot_masquerade_as_snapshot(tmp_path: Path) -> None:
    """Even a B-shaped row cannot exclude the explicitly active maintenance path."""
    fixture = _fixture(tmp_path)
    fixture.manifest["candidates"][0]["path"] = ACTIVE_PATH
    validation = _validate(fixture)

    assert validation.excluded_paths == ()
    assert validation.errors
    assert any("candidate set differs" in item for item in validation.errors)


def test_federal_reserve_active_probe_cannot_be_registered_as_snapshot(
    tmp_path: Path,
) -> None:
    """The newly listed Federal Reserve probe remains outside snapshot exemptions."""
    fixture = _fixture(tmp_path)
    snapshot = tmp_path / SNAPSHOT_PATH
    snapshot_bytes = snapshot.read_bytes()
    snapshot.unlink()
    federal_probe = tmp_path / FEDERAL_RESERVE_PROBE_PATH
    federal_probe.parent.mkdir(parents=True, exist_ok=True)
    federal_probe.write_bytes(snapshot_bytes)

    classification_path = tmp_path / evidence.CLASSIFICATION_PATH
    classification = json.loads(classification_path.read_text(encoding="utf-8"))
    classification["files"][0]["path"] = FEDERAL_RESERVE_PROBE_PATH
    fixture.classification_sha256 = _write_json(classification_path, classification)
    fixture.manifest["classification"]["sha256"] = fixture.classification_sha256
    fixture.manifest["historical_diagnostics"]["classification_sha256"] = (
        fixture.classification_sha256
    )
    fixture.manifest["active_entrypoints"].append(FEDERAL_RESERVE_PROBE_PATH)
    fixture.manifest["candidates"][0]["path"] = FEDERAL_RESERVE_PROBE_PATH
    fixture.manifest["candidates"][0]["active_entrypoint"] = FEDERAL_RESERVE_PROBE_PATH
    approval_sha = _approve(fixture)

    validation = _validate(fixture, expected_approval_sha256=approval_sha)

    assert validation.blocked
    assert validation.excluded_paths == ()
    assert validation.blocked_paths == (FEDERAL_RESERVE_PROBE_PATH,)
    assert any(
        "active entrypoint cannot be registered as an immutable snapshot" in reason
        for reason in validation.blocked_details[0][1]
    )


def test_manifest_or_review_artifact_tampering_breaks_trusted_pins(tmp_path: Path) -> None:
    """Changing either pinned review surface cannot activate an exclusion."""
    fixture = _fixture(tmp_path)
    approval_sha = _approve(fixture)
    manifest_path = tmp_path / evidence.MANIFEST_PATH
    original = _validate(fixture, expected_approval_sha256=approval_sha)
    assert original.excluded_paths == (SNAPSHOT_PATH,)
    trusted_manifest_sha = _digest(manifest_path.read_bytes())
    fixture.manifest["candidates"][0]["archival_reason"] = "altered after approval"
    manifest_tampered = _validate(
        fixture,
        expected_manifest_sha256=trusted_manifest_sha,
        expected_approval_sha256=approval_sha,
    )
    assert manifest_tampered.excluded_paths == ()
    assert any("manifest SHA-256 mismatch" in item for item in manifest_tampered.errors)

    fixture2 = _fixture(tmp_path / "artifact-tamper")
    approval_sha2 = _approve(fixture2)
    (fixture2.root / REVIEW_PATH).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "decision": "changed",
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    artifact_tampered = _validate(
        fixture2,
        expected_approval_sha256=approval_sha2,
    )
    assert artifact_tampered.excluded_paths == ()
    assert any("approval artifact SHA-256" in item for item in artifact_tampered.errors)


def test_missing_trusted_manifest_pin_blocks_all_exclusions(tmp_path: Path) -> None:
    """A self-declared candidate manifest is not a trusted approval boundary."""
    fixture = _fixture(tmp_path)
    _write_json(tmp_path / evidence.MANIFEST_PATH, fixture.manifest)
    validation = evidence.validate_manifest(
        fixture.root,
        expected_manifest_sha256=None,
        expected_classification_sha256=fixture.classification_sha256,
    )

    assert validation.excluded_paths == ()
    assert any("no trusted code-level SHA-256 pin" in item for item in validation.errors)


def test_snapshot_file_scan_uses_existing_version_and_config_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned Gitleaks scans each exact candidate path without executing the file."""
    fixture = _fixture(tmp_path)
    secret_manifest = Path("docs/quality/secret-scan.json")
    target_manifest = tmp_path / secret_manifest
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(secret_manifest, target_manifest)
    validation = _validate(fixture)
    calls: list[tuple[list[str], Path]] = []

    monkeypatch.setattr(evidence.shutil, "which", lambda _tool: "/fake/gitleaks")

    def fake_run(argv: list[str], *, cwd: Path, **_kwargs: object) -> SimpleNamespace:
        calls.append((argv, cwd))
        if argv == ["/fake/gitleaks", "version"]:
            return SimpleNamespace(returncode=0, stdout="8.30.1\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    result = evidence.scan_snapshot_bytes(tmp_path, validation)

    assert result.ok
    assert result.scanned_paths == (SNAPSHOT_PATH,)
    assert calls[0][0] == ["/fake/gitleaks", "version"]
    assert calls[1][0] == [
        "/fake/gitleaks",
        "dir",
        "--config",
        ".gitleaks.toml",
        "--redact",
        "--no-banner",
        SNAPSHOT_PATH,
    ]
    assert calls[1][1] == tmp_path.resolve()


def test_snapshot_secret_scan_failure_is_not_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned scanner errors and findings remain gate failures."""
    fixture = _fixture(tmp_path)
    target_manifest = tmp_path / "docs/quality/secret-scan.json"
    target_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path("docs/quality/secret-scan.json"), target_manifest)
    validation = _validate(fixture)
    monkeypatch.setattr(evidence.shutil, "which", lambda _tool: "/fake/gitleaks")

    def fake_run(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv == ["/fake/gitleaks", "version"]:
            return SimpleNamespace(returncode=0, stdout="8.30.1", stderr="")
        return SimpleNamespace(returncode=1, stdout="redacted finding", stderr="")

    monkeypatch.setattr(evidence.subprocess, "run", fake_run)
    result = evidence.scan_snapshot_bytes(tmp_path, validation)

    assert not result.ok
    assert result.scanned_paths == (SNAPSHOT_PATH,)
    assert "redacted finding" in result.errors[0]
