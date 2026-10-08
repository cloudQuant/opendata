"""Regression tests for C65 evidence-backed acceptance probes."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from shutil import copytree
from types import ModuleType, SimpleNamespace

import pytest

from scripts.quality import provider_source_review_evidence
from scripts.quality.acceptance_item_probe import (
    GAP,
    PROVEN,
    REPO_ROOT,
    Context,
    Probe,
    ProbeError,
    _benchmark_facts,
    _case_source_mapping,
    _read_historical_source,
    load_context,
    measure_ac6_02,
    measure_ac10_02,
    measure_ac10_03,
    probe_for,
    resolve_repair,
    script_module,
    wording_drift,
)
from tests.test_provider_source_review_evidence import (
    EXPECTED_BASELINE_PROVIDERS,
    EXPECTED_PROVIDERS,
    SIMILARITY_REL,
    SOURCE_REVIEW_REL,
    _file_hash,
    _read_json,
    _refresh_nearest_bindings,
    _write_json,
    _write_valid_bundle,
)
from tests.test_provider_source_review_evidence import (
    NOW as FIXTURE_NOW,
)

SYNTHETIC_PROVIDER_FILE_COUNTS = {
    "akshare": 11,
    "ecb": 10,
    "fred": 10,
    "imf": 10,
    "oecd": 10,
    "ths": 10,
    "yfinance": 10,
}
SYNTHETIC_PROVIDER_FILE_COUNT = sum(SYNTHETIC_PROVIDER_FILE_COUNTS.values())
SYNTHETIC_BASELINE_FILE_COUNTS = {
    "ecb": 25,
    "fred": 24,
    "imf": 24,
    "oecd": 24,
    "yfinance": 24,
}
SYNTHETIC_BASELINE_FILE_COUNT = sum(SYNTHETIC_BASELINE_FILE_COUNTS.values())


def _assert_breaks(probe: Probe, facts: dict[str, str]) -> None:
    """Every registered counterfactual must independently fail the measured facts."""
    assert 4 <= len(probe.breaks) <= 6
    repaired = resolve_repair(facts, probe.repair)
    assert probe.judge(repaired).state == PROVEN
    for counterfactual in probe.breaks:
        broken = {**repaired, **dict(counterfactual.facts)}
        assert probe.judge(broken).state == counterfactual.expect == GAP, counterfactual.label


def _measure_ac10_03_at(
    root: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Run the real validator with a controlled clock for a synthetic evidence bundle."""
    validate = provider_source_review_evidence.validate
    with monkeypatch.context() as scoped_patch:
        scoped_patch.setattr(
            provider_source_review_evidence,
            "validate",
            lambda validation_root: validate(validation_root, now=now),
        )
        return measure_ac10_03(Context(root, (), {}))


def _write_probe_bundle(root: Path) -> None:
    """Expand the evidence fixture to the probe's synthetic 71-file scope."""
    assert set(SYNTHETIC_PROVIDER_FILE_COUNTS) == EXPECTED_PROVIDERS
    assert set(SYNTHETIC_BASELINE_FILE_COUNTS) == EXPECTED_BASELINE_PROVIDERS
    _write_valid_bundle(root)

    for provider, expected_count in SYNTHETIC_PROVIDER_FILE_COUNTS.items():
        provider_dir = root / "opendata/data/providers" / provider
        existing_files = sorted(provider_dir.glob("*.py"))
        for index in range(len(existing_files), expected_count):
            fixture_source = provider_dir / f"review_fixture_{index:02d}.py"
            fixture_source.write_text(
                f'FIXTURE_VALUE = "{provider}-{index:02d}"\n', encoding="utf-8"
            )

    source_review_path = root / SOURCE_REVIEW_REL
    source_review = _read_json(source_review_path)
    source_hashes_by_provider = {
        provider: {
            path.relative_to(root).as_posix(): _file_hash(path)
            for path in sorted((root / "opendata/data/providers" / provider).rglob("*.py"))
        }
        for provider in sorted(EXPECTED_PROVIDERS)
    }
    for package in source_review["packages"]:
        provider = package["provider"]
        package_hashes = source_hashes_by_provider[provider]
        package["python_files"] = len(package_hashes)
        package["source_files_sha256"] = package_hashes
    _write_json(source_review_path, source_review)

    similarity_path = root / SIMILARITY_REL
    similarity = _read_json(similarity_path)
    local_manifest = [
        {"path": path, "sha256": digest}
        for provider_hashes in source_hashes_by_provider.values()
        for path, digest in sorted(provider_hashes.items())
    ]
    counts = similarity["counts"]
    counts["local_provider_python_files"] = len(local_manifest)
    for provider, provider_hashes in source_hashes_by_provider.items():
        counts["by_provider_local"][provider]["python_files"] = len(provider_hashes)

    source_manifests = similarity["source_manifests"]
    source_manifests["local_files_sha256"] = sorted(local_manifest, key=lambda row: row["path"])
    source_manifests["local_sorted_manifest_sha256"] = sha256(
        json.dumps(
            source_manifests["local_files_sha256"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    baseline_manifest = source_manifests["baseline_files_sha256"]
    for provider, expected_count in SYNTHETIC_BASELINE_FILE_COUNTS.items():
        existing_count = sum(
            1 for row in baseline_manifest if row["path"].split("/")[2] == provider
        )
        for index in range(existing_count, expected_count):
            relative = (
                f"openbb_platform/providers/{provider}/openbb_{provider}/models/"
                f"fixture_review_{index:02d}.py"
            )
            baseline_manifest.append(
                {"path": relative, "sha256": sha256(relative.encode("utf-8")).hexdigest()}
            )
    baseline_manifest.sort(key=lambda row: row["path"])
    counts["baseline_provider_python_files"] = len(baseline_manifest)
    source_manifests["baseline_sorted_manifest_sha256"] = sha256(
        json.dumps(baseline_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    _write_json(similarity_path, similarity)
    _refresh_nearest_bindings(root)


def _foreign_ac6_case_function() -> None:
    """A runtime callable whose source deliberately lives outside the vendor tree."""


def test_ac6_historical_metadata_reads_use_canonical_files_and_refuse_old_decoys(
    tmp_path: Path,
) -> None:
    context = Context(tmp_path, (), {})
    canonical_lock = tmp_path / "opendata/data/providers/akshare/_vendor/upstream.lock"
    canonical_lock.parent.mkdir(parents=True)
    canonical_lock.write_text('{"source": "canonical"}', encoding="utf-8")

    old_lock = tmp_path / "opendata_http/upstream.lock"
    old_lock.parent.mkdir(parents=True)
    old_lock.write_text("not the canonical lock", encoding="utf-8")

    assert _read_historical_source(context, "opendata_http/upstream.lock") == (
        '{"source": "canonical"}'
    )

    canonical_lock.unlink()
    with pytest.raises(
        ProbeError,
        match="opendata/data/providers/akshare/_vendor/upstream.lock is missing",
    ):
        _read_historical_source(context, "opendata_http/upstream.lock")


def test_ac6_case_source_mapping_rejects_foreign_runtime_functions() -> None:
    compare_module = ModuleType("foreign_ac6_compare")
    compare_module.__dict__["_load_case_function"] = lambda *_args: _foreign_ac6_case_function
    compare_module.__dict__["_PORTED_CASE_MODULE"] = "opendata.data.providers.akshare._vendor"
    compare_module.__dict__["CASES"] = (
        SimpleNamespace(name="foreign_case", function="_foreign_ac6_case_function"),
    )

    mapped, problems = _case_source_mapping(Context(REPO_ROOT, (), {}), compare_module, {}, {}, {})

    assert mapped == {}
    assert problems == ["foreign_case: ValueError"]


def test_c65_probe_wording_and_break_contracts() -> None:
    ctx = load_context()
    for item in ("AC-8|04", "§5|01", "AC-10|02", "AC-10|03", "AC-6|02"):
        probe = probe_for(item)
        assert wording_drift(ctx, probe) == ""
        assert 4 <= len(probe.breaks) <= 6


def test_c65_benchmark_records_are_validated_and_thresholds_are_item_specific() -> None:
    facts = _benchmark_facts(Context(REPO_ROOT, (), {}))

    assert facts["benchmark_valid"] == "yes"
    assert facts["benchmark_issue_count"] == "0"
    assert facts["source_identity_verified"] == "yes"
    assert facts["benchmark_scale_count"] == "3"
    assert facts["benchmark_scales_present"] == "yes"
    assert facts["all_scales_complete"] == "yes"
    assert all(facts[f"{scale}_rows_equal"] == "yes" for scale in ("100000", "1000000", "full"))
    assert all(facts[f"{scale}_pages_equal"] == "yes" for scale in ("100000", "1000000", "full"))
    assert all(
        facts[f"{scale}_target_rows_equal"] == "yes"
        and facts[f"{scale}_elapsed_positive"] == "yes"
        and facts[f"{scale}_current_rss_positive"] == "yes"
        and facts[f"{scale}_peak_rss_positive"] == "yes"
        for scale in ("100000", "1000000", "full")
    )
    assert facts["full_source_rows_match"] == "yes"
    assert facts["full_status"] == "complete"
    assert facts["full_source_sha256_bound"] == "yes"
    assert facts["full_body_final_identity"] == "yes"
    assert facts["full_peak_le_1m"] == "yes"

    ac8 = probe_for("AC-8|04")
    section5 = probe_for("§5|01")
    assert ac8.judge(facts).state == PROVEN
    assert section5.judge(facts).state == PROVEN
    assert ac8.judge({**facts, "memory_all_scales_below_2_gib": "no"}).state == PROVEN
    _assert_breaks(ac8, facts)
    _assert_breaks(section5, facts)


def test_ac10_02_reconciles_direct_confirmation_to_all_active_legs() -> None:
    facts = measure_ac10_02(Context(REPO_ROOT, (), {}))
    probe = probe_for("AC-10|02")

    assert facts["active_map_leg_count"] == "33"
    assert facts["active_provider_count"] == "7"
    assert facts["pair_set_equal"] == "yes"
    assert facts["direct_human_confirmation"] == "yes"
    assert facts["requester_evidence_valid"] == "yes"
    assert (
        facts["proof_scope"]
        == "requester/purpose/activation only; not copyright/source/field mapping"
    )
    assert probe.judge(facts).state == PROVEN
    _assert_breaks(probe, facts)


def test_ac10_03_requires_current_authorized_hash_bound_provider_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # NOW and the 71-file distribution describe only this synthetic review fixture.
    fresh_root = tmp_path / "fresh"
    _write_probe_bundle(fresh_root)
    facts = _measure_ac10_03_at(fresh_root, FIXTURE_NOW, monkeypatch)
    probe = probe_for("AC-10|03")

    assert facts["provider_review_valid"] == "yes"
    assert facts["provider_review_issue_count"] == "0"
    assert facts["provider_count"] == "7"
    assert facts["current_source_file_count"] == str(SYNTHETIC_PROVIDER_FILE_COUNT)
    assert facts["source_hash_entry_count"] == str(SYNTHETIC_PROVIDER_FILE_COUNT)
    assert facts["reviewed_source_file_count"] == str(SYNTHETIC_PROVIDER_FILE_COUNT)
    assert facts["similarity_source_file_count"] == str(SYNTHETIC_PROVIDER_FILE_COUNT)
    assert facts["baseline_source_file_count"] == str(SYNTHETIC_BASELINE_FILE_COUNT)
    assert facts["current_source_hashes_bound"] == "yes"
    assert facts["reviewer_authorized"] == "yes"
    assert facts["zero_openbb_imports"] == "yes"
    assert facts["candidate_count"] == "0"
    assert facts["review_complete"] == "yes"
    assert facts["nearest_review_count"] == "3"
    assert facts["nearest_positive_result"] == "yes"
    assert facts["artifact_sha_bindings_match"] == "yes"
    assert probe.judge(facts).state == PROVEN
    _assert_breaks(probe, facts)

    stale_root = tmp_path / "stale"
    copytree(fresh_root, stale_root)
    stale_facts = _measure_ac10_03_at(stale_root, FIXTURE_NOW + timedelta(days=2), monkeypatch)

    assert stale_facts["provider_review_valid"] == "no"
    assert int(stale_facts["provider_review_issue_count"]) > 0
    assert "stale" in stale_facts["provider_review_issue_codes"]
    assert stale_facts["current_source_hashes_bound"] == "yes"
    assert probe.judge(stale_facts).state == GAP


def test_ac10_03_fails_closed_when_validator_reports_source_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_root = tmp_path / "fresh"
    _write_probe_bundle(fresh_root)
    fresh_facts = _measure_ac10_03_at(fresh_root, FIXTURE_NOW, monkeypatch)
    probe = probe_for("AC-10|03")
    assert probe.judge(fresh_facts).state == PROVEN

    drift_root = tmp_path / "drift"
    copytree(fresh_root, drift_root)
    source = drift_root / "opendata/data/providers/ths/registration.py"
    mutated_source = source.read_text(encoding="utf-8") + "# changed after review\n"
    source.write_text(mutated_source, encoding="utf-8")

    facts = _measure_ac10_03_at(drift_root, FIXTURE_NOW, monkeypatch)

    assert facts["provider_review_valid"] == "no"
    assert int(facts["provider_review_issue_count"]) > 0
    assert "hash-mismatch" in facts["provider_review_issue_codes"]
    assert facts["current_source_hashes_bound"] == "no"
    assert probe.judge(facts).state == GAP


def test_ac6_02_keeps_gap_when_current_canonical_scope_differs_from_frozen_evidence() -> None:
    compare_module = script_module("scripts/codemod/compare_with_upstream.py")
    archived_report = Path(compare_module.REPORT_PATH)
    archived_before = archived_report.read_bytes() if archived_report.is_file() else None
    archived_digest = sha256(archived_before).hexdigest() if archived_before is not None else None

    facts = measure_ac6_02(Context(REPO_ROOT, (), {}))

    archived_after = archived_report.read_bytes() if archived_report.is_file() else None
    archived_after_digest = (
        sha256(archived_after).hexdigest() if archived_after is not None else None
    )
    assert archived_after_digest == archived_digest
    assert Path(compare_module.REPORT_PATH) == archived_report

    probe = probe_for("AC-6|02")
    assert facts["port_scope_valid"] == "no"
    assert "disk tree/upstream.lock path sets differ" in facts["port_scope_problem_summary"]
    assert "disk hash differs from port manifest" in facts["port_scope_problem_summary"]
    assert "port scope inputs unavailable (ProbeError)" not in facts["port_scope_problem_summary"]
    assert facts["b1_scope_groups_valid"] == "no"
    assert facts["b1_scope_group_count"] == "9"
    assert facts["b1_python_file_count"] == "245"
    assert facts["case_path_mapping_valid"] == "no"
    assert facts["case_path_problem_count"] == "18"
    assert "ValueError" in facts["case_path_problem_summary"]
    assert facts["case_count"] == "18"
    assert facts["case_statuses_complete"] == "yes"
    assert facts["case_pass_count"] == "14"
    assert facts["case_pending_count"] == "4"
    assert facts["case_fail_count"] == "0"
    assert facts["compare_exit_code"] == "1"
    assert facts["compare_exit_consistent"] == "yes"
    assert facts["compare_report_temporary"] == "yes"
    assert facts["compare_rtol"] == "1e-09"
    assert facts["passing_groups"] == "0"
    assert facts["pass_files"] == "0"
    assert facts["file_coverage_percent"] == "0.00"
    assert probe.judge(facts).state == GAP
    _assert_breaks(probe, facts)
