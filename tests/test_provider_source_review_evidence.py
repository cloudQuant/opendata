"""Counterfactual tests for bounded provider-source review evidence validation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

import pytest

from scripts.quality.provider_source_review_evidence import (
    EXPECTED_BASELINE_PROVIDERS,
    EXPECTED_OPENBB_COMMIT,
    EXPECTED_PACKAGE_REVIEW_RESULT,
    EXPECTED_PROVIDERS,
    EXPECTED_REVIEW_AUTHORIZATION,
    NEAREST_REVIEW_REL,
    SIMILARITY_REL,
    SOURCE_REVIEW_REL,
    validate,
)

if TYPE_CHECKING:
    from pathlib import Path

NOW = datetime(2026, 10, 1, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
REVIEW_GENERATED_AT = "2026-10-01T08:00:00+02:00"
NEAREST_GENERATED_AT = "2026-10-01T09:00:00+02:00"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_nearest_bindings(root: Path) -> None:
    review_path = root / SOURCE_REVIEW_REL
    similarity_path = root / SIMILARITY_REL
    nearest_path = root / NEAREST_REVIEW_REL
    nearest = _read_json(nearest_path)
    nearest["provider_review_sha256"] = _file_hash(review_path)
    nearest["similarity_inventory_sha256"] = _file_hash(similarity_path)
    _write_json(nearest_path, nearest)


def _write_valid_bundle(root: Path) -> None:
    source_hashes: dict[str, str] = {}
    source_by_provider: dict[str, dict[str, str]] = {}
    local_paths: list[str] = []
    for provider in sorted(EXPECTED_PROVIDERS):
        provider_dir = root / "opendata/data/providers" / provider
        provider_dir.mkdir(parents=True, exist_ok=True)
        registration = provider_dir / "registration.py"
        registration.write_text(
            'def register():\n    return "openbb is a harmless string here"\n',
            encoding="utf-8",
        )
        labels = provider_dir / "_labels.py"
        labels.write_text('LABEL = "openbb_platform is also only a string"\n', encoding="utf-8")
        files = {
            registration.relative_to(root).as_posix(): _file_hash(registration),
            labels.relative_to(root).as_posix(): _file_hash(labels),
        }
        source_by_provider[provider] = files
        source_hashes.update(files)
        local_paths.append(registration.relative_to(root).as_posix())

    baseline_hashes: dict[str, str] = {}
    for provider in sorted(EXPECTED_BASELINE_PROVIDERS):
        relative = f"openbb_platform/providers/{provider}/openbb_{provider}/models/review_target.py"
        baseline_hashes[relative] = hashlib.sha256(relative.encode("utf-8")).hexdigest()

    package_records = [
        {
            "provider": provider,
            "python_files": len(files),
            "source_files_sha256": files,
            "active_legs": 1,
            "verified_legs": 1,
            "reviewed": True,
            "review_result": EXPECTED_PACKAGE_REVIEW_RESULT,
            "comparison": "Current provider implementation compared with the fixed baseline.",
            "basis": "Reviewed current source in matched scope.",
        }
        for provider, files in sorted(source_by_provider.items())
    ]
    source_review = {
        "archive_round": "C65",
        "generated_at": REVIEW_GENERATED_AT,
        "reviewer": {
            "name": "Codex primary",
            "kind": "AI",
            "authorization": EXPECTED_REVIEW_AUTHORIZATION,
        },
        "openbb_baseline": {
            "path": "/pinned/openbb",
            "head": EXPECTED_OPENBB_COMMIT,
            "read_only": True,
        },
        "scope": "Current seven provider packages only.",
        "packages": package_records,
    }
    _write_json(root / SOURCE_REVIEW_REL, source_review)

    nearest_pairs: list[dict[str, Any]] = []
    for index, _provider in enumerate(sorted(EXPECTED_PROVIDERS)[:3]):
        nearest_pairs.append(
            {
                "local_path": local_paths[index],
                "local_function": "register",
                "local_function_kind": "function",
                "local_block_kind": "function",
                "local_span": [1, 2],
                "local_ast_node_count": 4,
                "baseline_path": sorted(baseline_hashes)[index],
                "baseline_function": "fetch",
                "baseline_function_kind": "function",
                "baseline_block_kind": "function",
                "baseline_span": [10 + index, 12 + index],
                "baseline_ast_node_count": 4,
                "ordered_token_ratio": 0.5,
                "multiset_dice": 0.5,
                "node_count_ratio": 1.0,
                "shared_anchor_counts": {"name": 1},
                "score": 0.9 - index * 0.1,
                "tier": "nearest_pair",
            }
        )
    similarity = {
        "schema": "opendata-c65-provider-similarity/v1",
        "generated_date": "2026-10-01",
        "comparison": {
            "baseline_commit": EXPECTED_OPENBB_COMMIT,
            "providers_local": sorted(EXPECTED_PROVIDERS),
            "providers_baseline": sorted(EXPECTED_BASELINE_PROVIDERS),
        },
        "counts": {
            "local_provider_python_files": len(source_hashes),
            "baseline_provider_python_files": len(baseline_hashes),
            "review_candidates": 0,
            "nearest_pairs": len(nearest_pairs),
            "local_parse_errors": [],
            "baseline_parse_errors": [],
            "by_provider_local": {
                provider: {"python_files": len(files), "functions_methods": 1}
                for provider, files in sorted(source_by_provider.items())
            },
        },
        "source_manifests": {
            "local_files_sha256": [
                {"path": path, "sha256": digest} for path, digest in sorted(source_hashes.items())
            ],
            "local_sorted_manifest_sha256": hashlib.sha256(b"local manifest").hexdigest(),
            "baseline_files_sha256": [
                {"path": path, "sha256": digest} for path, digest in sorted(baseline_hashes.items())
            ],
            "baseline_sorted_manifest_sha256": hashlib.sha256(b"baseline manifest").hexdigest(),
        },
        "candidates": [],
        "nearest_pairs": nearest_pairs,
    }
    _write_json(root / SIMILARITY_REL, similarity)

    nearest_review = {
        "archive_round": "C65",
        "generated_at": NEAREST_GENERATED_AT,
        "reviewer": {
            "name": "Codex primary",
            "kind": "AI",
            "authorization": EXPECTED_REVIEW_AUTHORIZATION,
        },
        "review_complete": True,
        "unreviewed_candidates": 0,
        "nearest_pair_reviews": [
            {
                **pair,
                "reviewed": True,
                "result": "NO_SHARED_COMPLEX_IMPLEMENTATION_OBSERVED",
                "basis": "The operation and data flow differ in the matched source spans.",
            }
            for pair in nearest_pairs
        ],
    }
    _write_json(root / NEAREST_REVIEW_REL, nearest_review)
    _refresh_nearest_bindings(root)


@pytest.fixture
def evidence_root(tmp_path: Path) -> Path:
    """Create a small, internally bound seven-provider evidence bundle."""
    _write_valid_bundle(tmp_path)
    return tmp_path


def _issue_codes(result: Any) -> set[str]:
    return {issue.code for issue in result.issues}


def test_current_provider_review_bundle_validates_with_bounded_facts(evidence_root: Path) -> None:
    result = validate(evidence_root, now=NOW)

    assert result.valid is True
    assert result.facts["scope"] == "current_registered_provider_packages"
    assert result.facts["provider_names"] == sorted(EXPECTED_PROVIDERS)
    assert result.facts["provider_count"] == 7
    assert result.facts["python_files"] == 14
    assert result.facts["reviewed_python_files"] == 14
    assert result.facts["all_current_provider_python_parsed"] is True
    assert result.facts["openbb_import_paths"] == []
    assert result.facts["candidate_count"] == 0
    assert result.facts["nearest_review_count"] == 3
    assert result.facts["artifact_sha_bindings_match"] is True


def test_missing_provider_file_is_rejected_by_both_manifests(evidence_root: Path) -> None:
    (evidence_root / "opendata/data/providers/ths/_labels.py").unlink()

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "review-ths-file-set-mismatch" in _issue_codes(result)
    assert "similarity-local-file-set-mismatch" in _issue_codes(result)


def test_current_openbb_import_root_is_rejected_but_string_literals_are_not(
    evidence_root: Path,
) -> None:
    source = evidence_root / "opendata/data/providers/ths/registration.py"
    source.write_text(
        'import openbb_platform.providers\n\ndef register():\n    return "still a string"\n',
        encoding="utf-8",
    )

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "openbb-source-import" in _issue_codes(result)
    assert "review-ths-hash-mismatch" in _issue_codes(result)


def test_unreviewed_package_and_similarity_candidate_are_rejected(evidence_root: Path) -> None:
    review_path = evidence_root / SOURCE_REVIEW_REL
    review = _read_json(review_path)
    review["packages"][0]["reviewed"] = False
    _write_json(review_path, review)

    similarity_path = evidence_root / SIMILARITY_REL
    similarity = _read_json(similarity_path)
    similarity["candidates"] = [{"local_path": "opendata/data/providers/akshare/registration.py"}]
    similarity["counts"]["review_candidates"] = 1
    _write_json(similarity_path, similarity)
    _refresh_nearest_bindings(evidence_root)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "provider-review-incomplete" in _issue_codes(result)
    assert "similarity-candidates-unreviewed" in _issue_codes(result)
    assert "similarity-candidate-count-invalid" in _issue_codes(result)


def test_changed_review_artifact_without_nearest_rebinding_is_rejected(
    evidence_root: Path,
) -> None:
    review_path = evidence_root / SOURCE_REVIEW_REL
    review = _read_json(review_path)
    review["scope"] = "Changed after primary nearest review."
    _write_json(review_path, review)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "nearest-source-review-binding-mismatch" in _issue_codes(result)


def test_nearest_pair_must_match_ranked_identity_and_real_local_span(
    evidence_root: Path,
) -> None:
    nearest_path = evidence_root / NEAREST_REVIEW_REL
    nearest = _read_json(nearest_path)
    nearest["nearest_pair_reviews"][0]["local_span"] = [1, 999]
    _write_json(nearest_path, nearest)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "nearest-review-pair-binding-mismatch" in _issue_codes(result)
    assert "nearest-local-span-invalid" in _issue_codes(result)


def test_nearest_pair_shared_implementation_conclusion_is_rejected(
    evidence_root: Path,
) -> None:
    nearest_path = evidence_root / NEAREST_REVIEW_REL
    nearest = _read_json(nearest_path)
    nearest["nearest_pair_reviews"][0]["result"] = "FOUND_SHARED_COMPLEX_IMPLEMENTATION"
    _write_json(nearest_path, nearest)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "nearest-pair-result-invalid" in _issue_codes(result)


def test_future_inventory_date_and_wrong_baseline_commit_are_rejected(
    evidence_root: Path,
) -> None:
    similarity_path = evidence_root / SIMILARITY_REL
    similarity = _read_json(similarity_path)
    similarity["generated_date"] = "2026-10-02"
    similarity["comparison"]["baseline_commit"] = "0" * 40
    _write_json(similarity_path, similarity)
    _refresh_nearest_bindings(evidence_root)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "similarity-date-future" in _issue_codes(result)
    assert "similarity-baseline-mismatch" in _issue_codes(result)


def test_review_timestamp_must_be_recent_and_not_future(evidence_root: Path) -> None:
    review_path = evidence_root / SOURCE_REVIEW_REL
    review = _read_json(review_path)
    review["generated_at"] = "2026-09-30T08:00:00+02:00"
    _write_json(review_path, review)

    nearest_path = evidence_root / NEAREST_REVIEW_REL
    nearest = _read_json(nearest_path)
    nearest["generated_at"] = "2026-10-01T11:00:00+02:00"
    _write_json(nearest_path, nearest)
    _refresh_nearest_bindings(evidence_root)

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "source-review-stale" in _issue_codes(result)
    assert "nearest-review-timestamp-future" in _issue_codes(result)


def test_missing_registered_package_is_rejected(evidence_root: Path) -> None:
    package_dir = evidence_root / "opendata/data/providers/oecd"
    for path in package_dir.rglob("*.py"):
        path.unlink()
    package_dir.rmdir()

    result = validate(evidence_root, now=NOW)

    assert result.valid is False
    assert "provider-package-set-mismatch" in _issue_codes(result)
    assert "review-oecd-file-set-mismatch" in _issue_codes(result)
