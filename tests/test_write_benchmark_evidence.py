"""Counterfacts for the independent C65 write-benchmark evidence validator."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from scripts.quality import write_benchmark_evidence as evidence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
COMMAND_PREFIX = "/tmp/c65-clean-env/bin/python scripts/ops/benchmark_ods_write.py"
SOURCE_SHA256 = evidence.EXPECTED_SOURCE_SHA256


def _command(plan: evidence.RunPlan) -> str:
    command = f"{COMMAND_PREFIX} --source ths --page-size {evidence.PAGE_SIZE} --apply"
    if plan.limit_rows is not None:
        command += f" --limit-rows {plan.limit_rows}"
    return command


def _record(
    plan: evidence.RunPlan,
    kind: str,
    *,
    page_number: int = 0,
    rows_read: int = 0,
    pages_read: int = 0,
    elapsed: float = 1.0,
) -> dict[str, Any]:
    requested = plan.requested_rows or evidence.EXPECTED_SOURCE_ROWS
    is_final = kind == "final"
    is_full_final = is_final and plan.is_full
    return {
        "business_key": ["thscode", "trade_date"],
        "byte_unit": "bytes",
        "current_rss_bytes": 10_000_000 + page_number,
        "current_rss_source": "ps",
        "domain": "stock_daily",
        "elapsed_seconds": elapsed,
        "full_source_write_verified": is_full_final,
        "limit_rows": plan.limit_rows,
        "mode": "apply",
        "page_size": evidence.PAGE_SIZE,
        "pages_read": pages_read,
        "pages_written": pages_read,
        "peak_rss_bytes": {
            "100000": 180_000_000,
            "1000000": 360_000_000,
            "full": 320_000_000,
        }[plan.size],
        "peak_rss_source": "resource.getrusage",
        "record": kind,
        "rows_read": rows_read,
        "rows_requested": requested,
        "rows_written": rows_read,
        "rss_unit": "bytes",
        "source": "ths",
        "source_rows_exact": evidence.EXPECTED_SOURCE_ROWS,
        "source_scan_complete": is_full_final,
        "source_sha256": SOURCE_SHA256,
        "status": ("complete" if plan.is_full else "limited-complete") if is_final else "error",
        "table": "ods_stock_daily_ths",
        "target_rows_after": requested if is_final else None,
        "target_rows_before": 0,
    }


def _run_records(plan: evidence.RunPlan) -> list[dict[str, Any]]:
    requested = plan.requested_rows or evidence.EXPECTED_SOURCE_ROWS
    records = [_record(plan, "initial", elapsed=2.0)]
    pages = (requested + evidence.PAGE_SIZE - 1) // evidence.PAGE_SIZE
    cumulative = 0
    for page in range(1, pages + 1):
        cumulative = min(page * evidence.PAGE_SIZE, requested)
        records.append(
            _record(
                plan,
                "batch",
                page_number=page,
                rows_read=cumulative,
                pages_read=page,
                elapsed=2.0 + page,
            )
        )
        records[-1]["page_number"] = page
        records[-1]["page_rows"] = cumulative - min((page - 1) * evidence.PAGE_SIZE, requested)
    final = _record(
        plan,
        "final",
        page_number=pages,
        rows_read=requested,
        pages_read=pages,
        elapsed=3.0 + pages,
    )
    records.append(final)
    return records


def _write_fixture(root: Path) -> dict[str, list[dict[str, Any]]]:
    frozen_dir = root / "docs/evidence/C65"
    source_dir = root / "scripts/ops"
    frozen_dir.mkdir(parents=True)
    source_dir.mkdir(parents=True)
    shutil.copyfile(
        REPOSITORY_ROOT / "docs/evidence/C65/write-benchmark-source-frozen.txt",
        frozen_dir / "write-benchmark-source-frozen.txt",
    )
    shutil.copyfile(
        REPOSITORY_ROOT / "scripts/ops/benchmark_ods_write.py",
        source_dir / "benchmark_ods_write.py",
    )

    runs: list[dict[str, Any]] = []
    all_records: dict[str, list[dict[str, Any]]] = {}
    driver_lines = ["ROUND=C65"]
    for index, plan in enumerate(evidence.RUN_PLANS):
        records = _run_records(plan)
        all_records[plan.size] = records
        evidence_path = root / plan.evidence
        evidence_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
        schema = f"opendata_c65_bench_{plan.size}_fixture"
        command = _command(plan)
        elapsed_driver = 20.0 + index
        run = {
            "size": plan.size,
            "schema": schema,
            "elapsed_driver_seconds": elapsed_driver,
            "command": command,
            "exit": 0,
            "evidence": plan.evidence,
            "final": records[-1],
        }
        runs.append(run)
        driver_lines.append(f"BENCHMARK_START size={plan.size} schema={schema} command={command}")
        driver_lines.append(
            "BENCHMARK_FINISH "
            + json.dumps(
                {
                    "size": plan.size,
                    "schema": schema,
                    "elapsed_driver_seconds": elapsed_driver,
                    "command": command,
                    "exit": 0,
                    "evidence": plan.evidence,
                    "final": records[-1],
                },
                sort_keys=True,
            )
        )

    (frozen_dir / "write-benchmark-matrix.json").write_text(
        json.dumps(
            {
                "round": "C65",
                "created_schemas": [run["schema"] for run in runs],
                "runs": runs,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    driver_lines.append("EXIT=0")
    (frozen_dir / "write-benchmark-matrix-driver.txt").write_text(
        "\n".join(driver_lines) + "\n", encoding="utf-8"
    )
    return all_records


def _issue_codes(result: evidence.ValidationResult) -> set[str]:
    return {issue.code for issue in result.issues}


def _rewrite_run(root: Path, plan: evidence.RunPlan, records: list[dict[str, Any]]) -> None:
    (root / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    matrix_path = root / "docs/evidence/C65/write-benchmark-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    for run in matrix["runs"]:
        if run["size"] == plan.size:
            run["final"] = records[-1]
    matrix_path.write_text(json.dumps(matrix, sort_keys=True), encoding="utf-8")

    driver_path = root / "docs/evidence/C65/write-benchmark-matrix-driver.txt"
    lines = driver_path.read_text(encoding="utf-8").splitlines()
    rewritten: list[str] = []
    for line in lines:
        prefix = "BENCHMARK_FINISH "
        if line.startswith(prefix):
            finish = json.loads(line.removeprefix(prefix))
            if finish["size"] == plan.size:
                finish["final"] = records[-1]
                line = prefix + json.dumps(finish, sort_keys=True)
        rewritten.append(line)
    driver_path.write_text("\n".join(rewritten) + "\n", encoding="utf-8")


def test_realistic_three_scale_fixture_passes_and_reports_raw_measurements(tmp_path: Path) -> None:
    _write_fixture(tmp_path)

    result = evidence.validate(tmp_path)

    assert result.valid, result.issues
    assert result.facts["scale_count"] == 3
    assert result.facts["source"]["frozen_sha256"] == SOURCE_SHA256
    assert result.facts["source"]["allowed_initial_state_status_correction"] is True
    assert result.facts["scales"]["full"]["source_rows_exact"] == 10_310_289
    assert result.facts["scales"]["full"]["rows_written"] == 10_310_289
    assert result.facts["scales"]["full"]["pages_written"] == 207
    assert result.facts["scales"]["full"]["elapsed_seconds"] == 210.0
    assert result.facts["scales"]["full"]["peak_rss_bytes"] == 320_000_000
    assert result.facts["scales"]["100000"]["historical_progress_annotation"] is True
    assert result.facts["memory_observation"]["all_scales_below_2_gib"] is True
    assert result.facts["memory_observation"]["full_peak_le_1000000_peak"] is True


def test_peak_above_two_gib_is_an_observation_not_a_structure_failure(tmp_path: Path) -> None:
    records_by_scale = _write_fixture(tmp_path)
    peak_bytes = evidence.TWO_GIB_BYTES + 1
    for plan in evidence.RUN_PLANS:
        records = records_by_scale[plan.size]
        for record in records:
            record["peak_rss_bytes"] = peak_bytes
        _rewrite_run(tmp_path, plan, records)

    result = evidence.validate(tmp_path)

    assert result.valid, result.issues
    assert result.facts["memory_observation"]["all_scales_below_2_gib"] is False


def test_full_peak_above_one_million_peak_is_an_observation_not_a_structure_failure(
    tmp_path: Path,
) -> None:
    records_by_scale = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[2]
    for record in records_by_scale[plan.size]:
        record["peak_rss_bytes"] = 400_000_000
    _rewrite_run(tmp_path, plan, records_by_scale[plan.size])

    result = evidence.validate(tmp_path)

    assert result.valid, result.issues
    assert result.facts["memory_observation"]["full_peak_le_1000000_peak"] is False


def test_missing_batch_is_rejected(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    records[plan.size].pop(1)
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-batch-sequence" in _issue_codes(result)


def test_duplicate_final_is_rejected_even_when_matrix_claims_success(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    records[plan.size].append(dict(records[plan.size][-1]))
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert {"raw-final-count", "raw-final-eof"} & _issue_codes(result)


def test_target_count_mismatch_is_rejected_from_raw_records(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[1]
    records[plan.size][-1]["target_rows_after"] = 999_999
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-final-counter-mismatch" in _issue_codes(result)


def test_source_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[2]
    records[plan.size][1]["source_sha256"] = "0" * 64
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-source_sha256-mismatch" in _issue_codes(result)


def test_unapproved_current_business_source_change_is_rejected(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    source = tmp_path / "scripts/ops/benchmark_ods_write.py"
    source.write_bytes(source.read_bytes() + b"\n# unrelated behavioral change\n")

    result = evidence.validate(tmp_path)

    assert "source-current-drift" in _issue_codes(result)


def test_historical_error_status_needs_the_exact_frozen_source(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    frozen = tmp_path / "docs/evidence/C65/write-benchmark-source-frozen.txt"
    frozen.write_bytes(frozen.read_bytes() + b"\n")

    result = evidence.validate(tmp_path)

    assert "source-frozen-sha-mismatch" in _issue_codes(result)
    assert result.facts["scales"]["100000"]["historical_progress_annotation"] is False


def test_live_source_reconstructs_only_through_the_correction_register(tmp_path: Path) -> None:
    """Green face of the register, and proof the tolerance is not the byte-equality face."""
    _write_fixture(tmp_path)

    result = evidence.validate(tmp_path)

    assert result.valid, result.issues
    source = result.facts["source"]
    assert source["current_matches_frozen"] is False
    assert source["documented_corrections_reproduce_current"] is True
    assert source["documented_corrections_unmatched"] == "-"
    assert source["source_identity_verified"] is True


def test_every_registered_correction_is_load_bearing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drop one pair at a time: the register is a whitelist of exact diffs, so each must bite."""
    register = evidence.DOCUMENTED_SOURCE_CORRECTIONS
    assert len(register) >= 2, register
    for index, (label, _before, _after) in enumerate(register):
        arm_root = tmp_path / f"arm-{index}"
        _write_fixture(arm_root)
        monkeypatch.setattr(
            evidence,
            "DOCUMENTED_SOURCE_CORRECTIONS",
            register[:index] + register[index + 1 :],
        )

        result = evidence.validate(arm_root)

        assert "source-current-drift" in _issue_codes(result), label
        assert result.facts["source"]["documented_corrections_reproduce_current"] is False, label


def test_registered_correction_is_byte_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered pair whose replacement text drifted by one space no longer authorizes it."""
    _write_fixture(tmp_path)
    label, before, after = evidence.DOCUMENTED_SOURCE_CORRECTIONS[-1]
    tampered = after.replace(b"# noqa: S608", b"# noqa:S608", 1)
    assert tampered != after
    monkeypatch.setattr(
        evidence,
        "DOCUMENTED_SOURCE_CORRECTIONS",
        (*evidence.DOCUMENTED_SOURCE_CORRECTIONS[:-1], (label, before, tampered)),
    )

    result = evidence.validate(tmp_path)

    assert "source-current-drift" in _issue_codes(result)
    source = result.facts["source"]
    assert source["documented_corrections_unmatched"] == "-"
    assert source["documented_corrections_reproduce_current"] is False
    assert source["source_identity_verified"] is False


def test_error_type_on_a_progress_record_is_not_hidden_by_final_success(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    records[plan.size][1]["error_type"] = "RuntimeError"
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-error-type-present" in _issue_codes(result)
    assert result.facts["scales"][plan.size]["historical_progress_annotation"] is False


def test_missing_evidence_fails_closed_without_raising(tmp_path: Path) -> None:
    result = evidence.validate(tmp_path)

    assert not result.valid
    assert result.issues


def test_nonfinite_json_number_is_rejected(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    path = tmp_path / plan.evidence
    content = path.read_text(encoding="utf-8")
    content = content.replace('"elapsed_seconds": 5.0', '"elapsed_seconds": NaN', 1)
    path.write_text(content, encoding="utf-8")

    result = evidence.validate(tmp_path)

    assert "raw-jsonl-unreadable" in _issue_codes(result)


def test_non_bytes_rss_unit_is_rejected(tmp_path: Path) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    records[plan.size][-1]["rss_unit"] = "MiB"
    (tmp_path / plan.evidence).write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records[plan.size]),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-rss-unit-mismatch" in _issue_codes(result)


def test_nonzero_driver_exit_is_rejected(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    path = tmp_path / "docs/evidence/C65/write-benchmark-matrix-driver.txt"
    path.write_text(
        path.read_text(encoding="utf-8").replace("EXIT=0\n", "EXIT=1\n"), encoding="utf-8"
    )

    result = evidence.validate(tmp_path)

    assert "driver-exit" in _issue_codes(result)


def test_raw_final_body_must_match_driver_and_matrix(tmp_path: Path) -> None:
    _write_fixture(tmp_path)
    matrix_path = tmp_path / "docs/evidence/C65/write-benchmark-matrix.json"
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    matrix["runs"][0]["final"]["rows_written"] = 100_001
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

    result = evidence.validate(tmp_path)

    assert "matrix-final-identity" in _issue_codes(result)


@pytest.mark.parametrize(
    "field,value", [("peak_rss_bytes", float("nan")), ("elapsed_seconds", float("inf"))]
)
def test_nonfinite_measurements_are_rejected(tmp_path: Path, field: str, value: float) -> None:
    records = _write_fixture(tmp_path)
    plan = evidence.RUN_PLANS[0]
    records[plan.size][-1][field] = value
    (tmp_path / plan.evidence).write_text(
        "".join(
            json.dumps(record, allow_nan=True, sort_keys=True) + "\n"
            for record in records[plan.size]
        ),
        encoding="utf-8",
    )

    result = evidence.validate(tmp_path)

    assert "raw-jsonl-unreadable" in _issue_codes(result)
