"""Behavior tests for the offline and fail-closed legacy migration planner."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import opendata.migration_plan as migration_plan_module
from opendata.migration_plan import (
    MigrationPlanError,
    WarehouseFingerprint,
    build_migration_plan,
    canonical_json_bytes,
    canonical_json_sha256,
    load_json_input,
    render_plan,
    write_plan_new_file,
)

if TYPE_CHECKING:
    from pathlib import Path

TASK_PARAMS_HASH = "1" * 64
SCRIPT_PARAMS_HASH = "2" * 64
SOURCE_HASH = "3" * 64
CONTRACT_HASH = "4" * 64
REVIEW_HASH = "5" * 64
WAREHOUSE_HASH = "6" * 64
SOURCE_PATH = "src/backend/data/jobs/daily.py"
SCRIPT_ID = "daily_job"
TASK_ID = "scheduled:7"


def _combined_hash(task_hash: str = TASK_PARAMS_HASH, script_hash: str = SCRIPT_PARAMS_HASH) -> str:
    return canonical_json_sha256(
        {
            "task_params_sha256": task_hash,
            "script_params_sha256": script_hash,
            "semantics": "task_and_script_parameter_sha256_v1",
        }
    )


def _inputs(static_status: str = "READY_FOR_ADAPTER") -> tuple[dict, dict, dict, dict]:
    task_parameters = {
        "canonical_payload_sha256": TASK_PARAMS_HASH,
        "key_names": ["start_date"],
        "payload_present": True,
        "credential_flag": False,
    }
    script_parameters = {
        "canonical_payload_sha256": SCRIPT_PARAMS_HASH,
        "key_names": ["symbol"],
        "payload_present": True,
        "credential_flag": False,
    }
    task_metadata = {
        "id": 7,
        "script_id": SCRIPT_ID,
        "schedule_type": "cron",
        "expression": "5 6 * * 1-5",
        "is_active": 1,
        "parameter_summary": task_parameters,
    }
    script_metadata = {
        "script_id": SCRIPT_ID,
        "source_path": SOURCE_PATH,
        "path_status": "PATH_CANDIDATE",
        "is_active": 1,
        "parameter_summary": script_parameters,
    }
    inventory = {
        "schema_version": "1.0",
        "source_root": "/legacy/afi",
        "generated_at_utc": "2026-10-26T00:00:00+00:00",
        "counts": {
            "scheduled_task_rows_exact": 1,
            "data_script_rows_exact": 1,
        },
        "readonly_transaction": {
            "metadata_confirmed": True,
            "warehouse_confirmed": True,
        },
        "metadata": {
            "scheduled_tasks": {"row_count_exact": 1, "records": [task_metadata]},
            "data_scripts": {"row_count_exact": 1, "records": [script_metadata]},
        },
        "tasks": [
            {
                "task_id": TASK_ID,
                "script_id": SCRIPT_ID,
                "script_path": SOURCE_PATH,
                "runtime_is_active": 1,
                "path_status": "PATH_CANDIDATE",
            }
        ],
    }
    provider_model = {
        "source": "openbb",
        "domain": "equity",
        "model_class": "DailyBarsFetcher",
        "registered_by_descriptor": True,
    }
    static_file = {
        "source_path": SOURCE_PATH,
        "source_sha256": SOURCE_HASH,
        "status": static_status,
        "calls": [
            {
                "function": "daily_bars",
                "target_models": ["openbb:equity:DailyBarsFetcher"],
            }
        ],
    }
    catalog = {
        "schema_version": "1.1",
        "source_root": "/legacy/afi",
        "opendata_root": "/local/opendata",
        "errors": [],
        "summary": {"legacy_script_count": 1},
        "files": [static_file],
        "opendata_provider_models": [provider_model],
    }
    target = {
        "dataset_id": "market.daily_bars",
        "provider": "openbb",
        "domain": "equity",
        "model": "DailyBarsFetcher",
        "job_id": "market.daily_bars.refresh",
        "version": "v1",
        "sink_id": "warehouse.market_daily_bars",
        "contract_sha256": CONTRACT_HASH,
    }
    allowlist = {
        "schema_version": 1,
        "warehouse_fingerprints": [WAREHOUSE_HASH],
        "targets": [target],
    }
    mapping = {
        "schema_version": 1,
        "inputs": {
            "runtime_inventory_sha256": canonical_json_sha256(inventory),
            "static_catalog_sha256": canonical_json_sha256(catalog),
            "target_allowlist_sha256": canonical_json_sha256(allowlist),
        },
        "tasks": [
            {
                "task_id": TASK_ID,
                "script_id": SCRIPT_ID,
                "script_path": SOURCE_PATH,
                "script_sha256": SOURCE_HASH,
                "task_params_sha256": TASK_PARAMS_HASH,
                "script_params_sha256": SCRIPT_PARAMS_HASH,
                "params_sha256_semantics": (
                    "sha256(canonical-json(task_and_script_parameter_sha256_v1))"
                ),
                "params_sha256": _combined_hash(),
                **target,
                "schedule": {
                    "schedule_type": "cron",
                    "expression": "5 6 * * 1-5",
                    "timezone": "Asia/Shanghai",
                    "calendar": "XSHG",
                },
                "source_quota": "public_market_data",
                "ownership_group": "market-data-platform",
                "shard": "CN-A",
                "review": {
                    "status": "APPROVED",
                    "reviewer_id": "reviewer-1",
                    "reviewed_at": "2026-10-26T12:00:00Z",
                    "evidence_sha256": REVIEW_HASH,
                },
            }
        ],
    }
    return inventory, catalog, mapping, allowlist


def _build(
    inventory: dict,
    catalog: dict,
    mapping: dict | None = None,
    allowlist: dict | None = None,
    fingerprint: str | None = WAREHOUSE_HASH,
) -> dict:
    return build_migration_plan(
        inventory,
        catalog,
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(fingerprint) if fingerprint else None,
        target_allowlist=allowlist,
    )


def test_reviewed_matching_inputs_remain_disabled_and_plan_hash_is_stable() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    plan = _build(inventory, catalog, mapping, allowlist)

    assert plan["plan_status"] == "READY_DISABLED"
    assert plan["execution_supported"] is False
    assert plan["enabled_jobs_created"] is False
    assert plan["tasks"][0]["enabled"] is False
    assert plan["tasks"][0]["disposition"] == "READY_DISABLED"
    assert plan["tasks"][0]["params_sha256"] == _combined_hash()
    assert plan["tasks"][0]["task_params_sha256"] == TASK_PARAMS_HASH
    assert plan["tasks"][0]["script_params_sha256"] == SCRIPT_PARAMS_HASH
    assert plan["plan_sha256"] == _build(inventory, catalog, mapping, allowlist)["plan_sha256"]
    expected = dict(plan)
    expected.pop("plan_sha256")
    assert plan["plan_sha256"] == canonical_json_sha256(expected)


@pytest.mark.parametrize(
    ("status", "expected_reason"),
    [
        ("MISSING_PROVIDER", "static_missing_provider"),
        ("REQUIRES_MANUAL", "static_requires_manual"),
    ],
)
def test_static_provider_gaps_always_block_a_reviewed_mapping(
    status: str, expected_reason: str
) -> None:
    inventory, catalog, mapping, allowlist = _inputs(status)

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    assert job["enabled"] is False
    assert expected_reason in job["blocked_reasons"]


def test_missing_mapping_produces_explicit_disabled_blocker() -> None:
    inventory, catalog, _, _ = _inputs("MISSING_PROVIDER")

    plan = _build(inventory, catalog)

    assert plan["summary"]["scheduled_task_count"] == 1
    assert plan["summary"]["missing_mapping_count"] == 1
    assert plan["summary"]["blocked_task_count"] == 1
    assert plan["tasks"][0]["disposition"] == "BLOCKED"
    assert "reviewed_mapping_missing" in plan["tasks"][0]["blocked_reasons"]
    assert plan["tasks"][0]["enabled"] is False


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("script_sha256", "9" * 64, "mapping_script_hash_mismatch"),
        ("task_params_sha256", "9" * 64, "mapping_task_params_hash_mismatch"),
        ("script_params_sha256", "9" * 64, "mapping_script_params_hash_mismatch"),
        ("params_sha256", "9" * 64, "mapping_effective_params_hash_mismatch"),
        ("script_path", "src/changed.py", "mapping_script_path_mismatch"),
    ],
)
def test_mapping_hash_or_source_mismatch_blocks_task(field: str, value: str, reason: str) -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0][field] = value

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    assert job["disposition"] == "BLOCKED"
    assert reason in job["blocked_reasons"]
    assert job["enabled"] is False


def test_mapping_must_pin_the_exact_inventory_catalog_and_allowlist_hashes() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["inputs"]["runtime_inventory_sha256"] = "0" * 64

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "mapping_runtime_inventory_hash_mismatch" in plan["integrity_issues"]
    assert "mapping_runtime_inventory_hash_mismatch" in plan["tasks"][0]["blocked_reasons"]


def test_target_identity_must_be_allowlisted_and_match_the_registered_model() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["dataset_id"] = "unreviewed.dataset"

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    assert "target_not_allowlisted" in job["blocked_reasons"]
    assert job["disposition"] == "BLOCKED"

    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["model"] = "UnregisteredFetcher"
    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]
    assert "provider_model_not_registered" in job["blocked_reasons"]
    assert "provider_model_not_matched_by_legacy_call" in job["blocked_reasons"]


@pytest.mark.parametrize(
    ("fingerprint", "allowlisted", "expected_reason"),
    [
        (None, True, "target_warehouse_fingerprint_missing"),
        ("7" * 64, True, "target_warehouse_fingerprint_not_allowlisted"),
        (WAREHOUSE_HASH, False, "target_warehouse_fingerprint_allowlist_missing"),
    ],
)
def test_warehouse_fingerprint_is_explicit_and_allowlisted(
    fingerprint: str | None, allowlisted: bool, expected_reason: str
) -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    if not allowlisted:
        allowlist["warehouse_fingerprints"] = []

    plan = _build(inventory, catalog, mapping, allowlist, fingerprint)

    assert expected_reason in plan["tasks"][0]["blocked_reasons"]
    assert plan["tasks"][0]["enabled"] is False


def test_review_schedule_and_resource_ownership_require_explicit_evidence() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["review"]["status"] = "PENDING"
    mapping["tasks"][0]["schedule"]["timezone"] = "Not/AZone"
    mapping["tasks"][0]["ownership_group"] = ""
    mapping["tasks"][0]["schedule"]["expression"] = "0 0 * * *"

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    assert "mapping_review_not_approved" in job["blocked_reasons"]
    assert "mapping_schedule_timezone_unverified" in job["blocked_reasons"]
    assert "mapping_schedule_expression_mismatch" in job["blocked_reasons"]
    assert "mapping_ownership_group_unverified" in job["blocked_reasons"]


def test_mapping_rejects_extra_secret_fields_without_copying_them_to_plan() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["api_key"] = "secret-value-that-must-not-leak"
    mapping["tasks"][0]["review"]["reviewer_id"] = "sk-12345678901234567890"

    plan = _build(inventory, catalog, mapping, allowlist)
    rendered = render_plan(plan)

    assert "mapping_row_shape_invalid" in plan["tasks"][0]["blocked_reasons"]
    assert "mapping_reviewer_missing" in plan["tasks"][0]["blocked_reasons"]
    assert "secret-value-that-must-not-leak" not in rendered
    assert "sk-12345678901234567890" not in rendered


def test_runtime_and_static_catalog_path_anti_join_mismatch_blocks_all_jobs() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    inventory["metadata"]["data_scripts"]["records"][0]["source_path"] = "src/other.py"

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "runtime_static_path_anti_join_mismatch" in plan["integrity_issues"]
    assert "runtime_static_path_anti_join_mismatch" in plan["tasks"][0]["blocked_reasons"]
    assert "runtime_task_map_path_join_mismatch" in plan["tasks"][0]["blocked_reasons"]
    assert "mapping_script_registry_path_mismatch" in plan["tasks"][0]["blocked_reasons"]


def test_missing_runtime_task_map_row_still_emits_one_blocked_job() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    inventory["tasks"] = []
    mapping["tasks"] = []

    plan = _build(inventory, catalog, mapping, allowlist)

    assert plan["summary"]["scheduled_task_count"] == 1
    assert len(plan["tasks"]) == 1
    assert (
        "runtime_task_map_bidirectional_anti_join_mismatch" in plan["tasks"][0]["blocked_reasons"]
    )
    assert "runtime_script_path_unresolved" in plan["tasks"][0]["blocked_reasons"]


def test_script_only_registry_rows_are_helpers_and_never_count_as_scheduled_jobs() -> None:
    inventory, catalog, _, _ = _inputs()
    script = dict(inventory["metadata"]["data_scripts"]["records"][0])
    script["script_id"] = "maintenance_helper"
    script["source_path"] = "src/backend/data/jobs/helper.py"
    inventory["metadata"]["data_scripts"]["records"].append(script)
    inventory["metadata"]["data_scripts"]["row_count_exact"] = 2
    inventory["counts"]["data_script_rows_exact"] = 2
    inventory["tasks"].append(
        {
            "task_id": "script:maintenance_helper",
            "script_id": "maintenance_helper",
            "script_path": script["source_path"],
            "runtime_is_active": 1,
            "path_status": "PATH_CANDIDATE",
        }
    )
    helper_static = dict(catalog["files"][0])
    helper_static["source_path"] = script["source_path"]
    helper_static["source_sha256"] = "8" * 64
    catalog["files"].append(helper_static)
    catalog["summary"]["legacy_script_count"] = 2

    plan = _build(inventory, catalog)

    assert plan["summary"]["scheduled_task_count"] == 1
    assert plan["summary"]["non_scheduled_helper_count"] == 1
    assert [item["task_id"] for item in plan["tasks"]] == [TASK_ID]
    assert plan["helpers"][0]["task_id"] == "script:maintenance_helper"
    assert plan["helpers"][0]["enabled"] is False


def test_non_readonly_inventory_blocks_a_plan() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    inventory["readonly_transaction"]["warehouse_confirmed"] = False

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "runtime_snapshot_not_read_only_confirmed" in plan["integrity_issues"]
    assert "runtime_snapshot_not_read_only_confirmed" in plan["tasks"][0]["blocked_reasons"]


def test_json_loader_refuses_duplicate_keys_non_finite_numbers_and_symlinks(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema_version":"1.0","schema_version":"2.0"}', encoding="utf-8")
    with pytest.raises(MigrationPlanError, match="json_duplicate_key"):
        load_json_input(duplicate, label="input")

    non_finite = tmp_path / "non-finite.json"
    non_finite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(MigrationPlanError, match="json_non_finite_number"):
        load_json_input(non_finite, label="input")

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "input-link.json"
    link.symlink_to(target)
    with pytest.raises(MigrationPlanError, match="input_symlink_refused"):
        load_json_input(link, label="input")


def test_new_output_writer_refuses_sources_env_symlinks_and_overwrites(tmp_path: Path) -> None:
    source = tmp_path / "source.json"
    source.write_text("{}", encoding="utf-8")
    with pytest.raises(MigrationPlanError, match="output_aliases_input_file"):
        write_plan_new_file(source, "{}", input_paths=[source], protected_roots=[])

    protected = tmp_path / "legacy"
    protected.mkdir()
    with pytest.raises(MigrationPlanError, match="output_inside_protected_source_root"):
        write_plan_new_file(
            protected / "plan.json", "{}", input_paths=[], protected_roots=[protected]
        )

    with pytest.raises(MigrationPlanError, match="environment_file_output_refused"):
        write_plan_new_file(tmp_path / ".env-report", "{}", input_paths=[], protected_roots=[])

    output = tmp_path / "plan.json"
    assert write_plan_new_file(output, "{}", input_paths=[source], protected_roots=[]) == output
    with pytest.raises(MigrationPlanError, match="output_exists_refused"):
        write_plan_new_file(output, "{}", input_paths=[source], protected_roots=[])

    real_file = tmp_path / "real.json"
    real_file.write_text("{}", encoding="utf-8")
    output_link = tmp_path / "plan-link.json"
    output_link.symlink_to(real_file)
    with pytest.raises(MigrationPlanError, match="output_symlink_refused"):
        write_plan_new_file(output_link, "{}", input_paths=[], protected_roots=[])


def test_cli_writes_a_blocked_report_without_touching_inputs(tmp_path: Path, capsys) -> None:
    from scripts.ops.plan_legacy_migration import main

    inventory, catalog, _, _ = _inputs("MISSING_PROVIDER")
    inventory_path = tmp_path / "inventory.json"
    catalog_path = tmp_path / "catalog.json"
    output_path = tmp_path / "out" / "plan.json"
    output_path.parent.mkdir()
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    inventory_before = inventory_path.read_bytes()
    catalog_before = catalog_path.read_bytes()

    exit_code = main(
        [
            "--runtime-inventory",
            str(inventory_path),
            "--static-catalog",
            str(catalog_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["plan_status"] == "BLOCKED"
    assert inventory_path.read_bytes() == inventory_before
    assert catalog_path.read_bytes() == catalog_before
    assert "reviewed_mapping_missing" in capsys.readouterr().out


def test_fingerprint_and_canonical_json_reject_invalid_values() -> None:
    with pytest.raises(MigrationPlanError, match="warehouse_fingerprint_must_be_sha256"):
        WarehouseFingerprint("not-a-digest")
    with pytest.raises(MigrationPlanError, match="input_not_canonical_json"):
        canonical_json_sha256({"value": float("nan")})


@pytest.mark.parametrize(
    ("filename", "payload", "expected"),
    [
        ("invalid-utf8.json", b"\xff", "input_not_strict_utf8_json"),
        ("invalid-json.json", b"{", "input_not_strict_utf8_json"),
        ("array.json", b"[]", "input_root_must_be_object"),
    ],
)
def test_json_loader_rejects_invalid_encoding_syntax_and_root(
    tmp_path: Path, filename: str, payload: bytes, expected: str
) -> None:
    path = tmp_path / filename
    path.write_bytes(payload)

    with pytest.raises(MigrationPlanError, match=expected):
        load_json_input(path, label="input")


def test_json_loader_bounds_file_size_tree_depth_and_node_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oversized = tmp_path / "oversized.json"
    oversized.write_text('{"value":"123456789"}', encoding="utf-8")
    monkeypatch.setattr(migration_plan_module, "MAX_INPUT_BYTES", 4)
    with pytest.raises(MigrationPlanError, match="input_exceeds_size_limit"):
        load_json_input(oversized, label="input")

    monkeypatch.setattr(migration_plan_module, "MAX_INPUT_BYTES", 1024)
    nested = tmp_path / "nested.json"
    nested.write_text('{"a":{"b":1}}', encoding="utf-8")
    monkeypatch.setattr(migration_plan_module, "MAX_JSON_DEPTH", 1)
    with pytest.raises(MigrationPlanError, match="json_depth_limit_exceeded"):
        load_json_input(nested, label="input")

    monkeypatch.setattr(migration_plan_module, "MAX_JSON_DEPTH", 24)
    monkeypatch.setattr(migration_plan_module, "MAX_JSON_NODES", 2)
    with pytest.raises(MigrationPlanError, match="json_node_limit_exceeded"):
        load_json_input(nested, label="input")


def test_json_loader_reports_missing_input_and_tree_validator_rejects_non_json_values(
    tmp_path: Path,
) -> None:
    with pytest.raises(MigrationPlanError, match="input_unreadable"):
        load_json_input(tmp_path / "absent.json", label="input")
    with pytest.raises(MigrationPlanError, match="json_non_finite_number"):
        migration_plan_module._validate_json_tree(float("inf"))
    with pytest.raises(MigrationPlanError, match="json_non_json_value"):
        migration_plan_module._validate_json_tree(object())


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda inv, cat: inv.update(schema_version="9.0"), "unsupported_runtime_inventory_schema"),
        (lambda inv, cat: cat.update(schema_version="9.0"), "unsupported_static_catalog_schema"),
        (lambda inv, cat: inv.update(tasks="bad"), "runtime_inventory.tasks_invalid_or_too_large"),
        (lambda inv, cat: inv.update(metadata=[]), "runtime_inventory.metadata_must_be_object"),
    ],
)
def test_snapshot_loader_rejects_unsupported_schema_and_malformed_sections(
    mutate, expected: str
) -> None:
    inventory, catalog, _, _ = _inputs()
    mutate(inventory, catalog)

    with pytest.raises(MigrationPlanError, match=expected):
        _build(inventory, catalog)


def test_runtime_count_and_catalog_error_mismatches_are_reported_as_integrity_blockers() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    inventory["counts"]["scheduled_task_rows_exact"] = 2
    inventory["metadata"]["scheduled_tasks"]["row_count_exact"] = 2
    catalog["errors"] = [{"path": "ignored", "error": "static_scan_error"}]

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "runtime_scheduled_task_rows_exact_mismatch" in plan["integrity_issues"]
    assert "catalog_reports_errors" in plan["integrity_issues"]


def test_runtime_duplicate_and_invalid_ids_and_catalog_hashes_are_blocked() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    inventory["metadata"]["scheduled_tasks"]["records"].append(
        dict(inventory["metadata"]["scheduled_tasks"]["records"][0])
    )
    inventory["metadata"]["scheduled_tasks"]["row_count_exact"] = 2
    inventory["counts"]["scheduled_task_rows_exact"] = 2
    inventory["tasks"].append(dict(inventory["tasks"][0], task_id="scheduled:8"))
    catalog["files"][0]["source_sha256"] = "bad"

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "runtime_duplicate_scheduled_task_id" in plan["integrity_issues"]
    assert "catalog_source_identity_invalid" in plan["integrity_issues"]
    assert all(job["enabled"] is False for job in plan["tasks"])


def test_mapping_duplicate_unknown_and_helper_ids_create_global_blockers() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    duplicate = dict(mapping["tasks"][0])
    duplicate["task_id"] = "script:unrelated"
    mapping["tasks"].append(duplicate)

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "mapping_contains_non_scheduled_task_id" in plan["integrity_issues"]
    assert plan["tasks"][0]["enabled"] is False

    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"].append(dict(mapping["tasks"][0]))
    plan = _build(inventory, catalog, mapping, allowlist)
    assert "mapping_duplicate_task_id" in plan["integrity_issues"]


def test_mapping_row_rejects_unapproved_review_and_invalid_schedule_contract() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["params_sha256_semantics"] = "ambiguous"
    mapping["tasks"][0]["schedule"]["calendar"] = ""
    mapping["tasks"][0]["review"]["reviewer_id"] = ""
    mapping["tasks"][0]["review"]["reviewed_at"] = "yesterday"
    mapping["tasks"][0]["review"]["evidence_sha256"] = "bad"

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    expected = {
        "mapping_effective_params_hash_semantics_mismatch",
        "mapping_schedule_calendar_unverified",
        "mapping_reviewer_missing",
        "mapping_review_timestamp_invalid",
        "mapping_review_evidence_missing",
    }
    assert expected.issubset(job["blocked_reasons"])


def test_unknown_catalog_status_provider_unregistered_and_missing_runtime_hashes_block() -> None:
    inventory, catalog, mapping, allowlist = _inputs("NEW_STATUS")
    inventory["metadata"]["scheduled_tasks"]["records"][0]["parameter_summary"] = {}
    inventory["metadata"]["data_scripts"]["records"][0]["parameter_summary"] = {}
    catalog["opendata_provider_models"][0]["registered_by_descriptor"] = False
    catalog["files"][0]["calls"] = []

    job = _build(inventory, catalog, mapping, allowlist)["tasks"][0]

    assert "static_status_unknown" in job["blocked_reasons"]
    assert "runtime_task_params_digest_missing" in job["blocked_reasons"]
    assert "runtime_script_params_digest_missing" in job["blocked_reasons"]
    assert "mapping_task_params_hash_mismatch" in job["blocked_reasons"]
    assert "provider_model_not_registered" in job["blocked_reasons"]
    assert "provider_model_not_matched_by_legacy_call" in job["blocked_reasons"]


def test_plan_writer_rejects_missing_parent_and_oversized_plan(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(MigrationPlanError, match="output_parent_directory_missing"):
        write_plan_new_file(
            tmp_path / "absent" / "plan.json", "{}", input_paths=[], protected_roots=[]
        )
    monkeypatch.setattr(migration_plan_module, "MAX_PLAN_BYTES", 1)
    with pytest.raises(MigrationPlanError, match="migration_plan_exceeds_size_limit"):
        write_plan_new_file(tmp_path / "plan.json", "{}", input_paths=[], protected_roots=[])


def test_plan_and_render_size_limits_fail_before_returning_a_partial_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inventory, catalog, _, _ = _inputs()
    plan = _build(inventory, catalog)
    unsigned_plan = dict(plan)
    unsigned_plan.pop("plan_sha256")
    unsigned_size = len(canonical_json_bytes(unsigned_plan))

    monkeypatch.setattr(migration_plan_module, "MAX_PLAN_BYTES", unsigned_size - 1)
    with pytest.raises(MigrationPlanError, match="migration_plan_exceeds_size_limit"):
        _build(inventory, catalog)

    monkeypatch.setattr(migration_plan_module, "MAX_PLAN_BYTES", unsigned_size)
    with pytest.raises(MigrationPlanError, match="migration_plan_exceeds_size_limit"):
        _build(inventory, catalog)

    monkeypatch.setattr(migration_plan_module, "MAX_PLAN_BYTES", 1)
    with pytest.raises(MigrationPlanError, match="migration_plan_exceeds_size_limit"):
        render_plan({"plan": "too large"})


@pytest.mark.parametrize("failure", [FileExistsError, OSError])
def test_plan_writer_handles_atomic_link_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[OSError]
) -> None:
    output = tmp_path / "plan.json"

    def fail_link(source, target, *, follow_symlinks):
        del source, target, follow_symlinks
        raise failure("injected link failure")

    monkeypatch.setattr(migration_plan_module.os, "link", fail_link)
    expected = "output_exists_refused" if failure is FileExistsError else "output_write_failed"
    with pytest.raises(MigrationPlanError, match=expected):
        write_plan_new_file(output, "{}", input_paths=[], protected_roots=[])
    assert not output.exists()
    assert not list(tmp_path.glob(".afi204-plan-*.tmp"))


def test_plan_writer_closes_descriptor_when_temp_stream_cannot_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_fdopen = migration_plan_module.os.fdopen

    def fail_fdopen(descriptor, *args, **kwargs):
        del descriptor, args, kwargs
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(migration_plan_module.os, "fdopen", fail_fdopen)
    with pytest.raises(MigrationPlanError, match="output_write_failed"):
        write_plan_new_file(tmp_path / "plan.json", "{}", input_paths=[], protected_roots=[])
    monkeypatch.setattr(migration_plan_module.os, "fdopen", real_fdopen)
    assert not list(tmp_path.glob(".afi204-plan-*.tmp"))


def test_bad_runtime_task_reference_and_absent_static_path_are_reported() -> None:
    inventory, catalog, _, _ = _inputs()
    inventory["metadata"]["data_scripts"]["records"] = []
    inventory["metadata"]["data_scripts"]["row_count_exact"] = 0
    inventory["counts"]["data_script_rows_exact"] = 0
    catalog["files"] = []
    catalog["summary"]["legacy_script_count"] = 0

    plan = _build(inventory, catalog)

    reasons = plan["tasks"][0]["blocked_reasons"]
    assert "runtime_script_registration_missing" in reasons
    assert "runtime_task_map_script_reference_missing" in plan["integrity_issues"]
    assert "static_catalog_path_missing" in reasons
    assert "runtime_script_params_digest_missing" in reasons


def test_missing_static_file_and_invalid_active_or_schedule_fields_are_sanitized() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    catalog["files"] = []
    catalog["summary"]["legacy_script_count"] = 0
    inventory["tasks"][0]["runtime_is_active"] = True
    inventory["metadata"]["scheduled_tasks"]["records"][0]["expression"] = "bad\nsecret"

    plan = _build(inventory, catalog, mapping, allowlist)
    task = plan["tasks"][0]

    assert "static_catalog_path_missing" in task["blocked_reasons"]
    assert "runtime_task_active_state_invalid" in task["blocked_reasons"]
    assert "runtime_schedule_metadata_invalid" in task["blocked_reasons"]
    assert task["legacy_schedule"]["expression"] is None
    assert "bad\nsecret" not in render_plan(plan)


def test_runtime_and_catalog_source_root_and_count_mismatches_are_reported() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    catalog["source_root"] = "/different/source"
    catalog["summary"]["legacy_script_count"] = 2

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "runtime_static_source_root_mismatch" in plan["integrity_issues"]
    assert "catalog_reported_script_count_mismatch" in plan["integrity_issues"]


def test_runtime_anti_joins_cover_unknown_helpers_duplicate_maps_and_script_ids() -> None:
    inventory, catalog, _, _ = _inputs()
    inventory["tasks"].append(
        {
            "task_id": "script:unknown_helper",
            "script_id": "missing_script",
            "script_path": "src/missing.py",
        }
    )
    inventory["tasks"].append(dict(inventory["tasks"][0]))
    inventory["metadata"]["data_scripts"]["records"].append(
        dict(inventory["metadata"]["data_scripts"]["records"][0])
    )
    inventory["metadata"]["data_scripts"]["row_count_exact"] = 2
    inventory["counts"]["data_script_rows_exact"] = 2

    plan = _build(inventory, catalog)

    for issue in (
        "runtime_duplicate_task_map_id",
        "runtime_task_map_unknown_helper_id",
        "runtime_task_map_script_reference_missing",
        "runtime_duplicate_script_id",
        "runtime_duplicate_script_path",
    ):
        assert issue in plan["integrity_issues"]


def test_mapping_root_pins_and_target_allowlist_schemas_fail_closed() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["unexpected"] = True
    with pytest.raises(MigrationPlanError, match="reviewed_mapping_root_shape_invalid"):
        _build(inventory, catalog, mapping, allowlist)

    inventory, catalog, mapping, allowlist = _inputs()
    mapping["schema_version"] = 2
    with pytest.raises(MigrationPlanError, match="reviewed_mapping_schema_version_unsupported"):
        _build(inventory, catalog, mapping, allowlist)

    inventory, catalog, mapping, allowlist = _inputs()
    mapping["inputs"]["other"] = "invalid"
    with pytest.raises(MigrationPlanError, match="reviewed_mapping_input_pins_invalid"):
        _build(inventory, catalog, mapping, allowlist)

    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["unexpected"] = True
    with pytest.raises(MigrationPlanError, match="target_allowlist_root_shape_invalid"):
        _build(inventory, catalog, mapping, allowlist)

    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["schema_version"] = 2
    with pytest.raises(MigrationPlanError, match="target_allowlist_schema_version_unsupported"):
        _build(inventory, catalog, mapping, allowlist)


def test_mapping_boolean_schema_version_is_rejected_with_complete_hash_pins() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["schema_version"] = True

    assert mapping["inputs"] == {
        "runtime_inventory_sha256": canonical_json_sha256(inventory),
        "static_catalog_sha256": canonical_json_sha256(catalog),
        "target_allowlist_sha256": canonical_json_sha256(allowlist),
    }
    with pytest.raises(MigrationPlanError, match="reviewed_mapping_schema_version_unsupported"):
        _build(inventory, catalog, mapping, allowlist)


def test_allowlist_boolean_schema_version_is_rejected_with_complete_reviewed_mapping() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["schema_version"] = True
    mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(allowlist)

    assert mapping["inputs"] == {
        "runtime_inventory_sha256": canonical_json_sha256(inventory),
        "static_catalog_sha256": canonical_json_sha256(catalog),
        "target_allowlist_sha256": canonical_json_sha256(allowlist),
    }
    with pytest.raises(MigrationPlanError, match="target_allowlist_schema_version_unsupported"):
        _build(inventory, catalog, mapping, allowlist)


def test_allowlist_invalid_hashes_targets_and_duplicate_entries_are_reported() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["warehouse_fingerprints"] = ["bad", WAREHOUSE_HASH, WAREHOUSE_HASH]
    allowlist["targets"].append(dict(allowlist["targets"][0]))
    allowlist["targets"].append({"dataset_id": "missing-fields"})
    mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(allowlist)

    plan = _build(inventory, catalog, mapping, allowlist)

    for issue in (
        "target_allowlist_fingerprint_invalid",
        "target_allowlist_duplicate_fingerprint",
        "target_allowlist_duplicate_target",
        "target_allowlist_target_shape_invalid",
    ):
        assert issue in plan["integrity_issues"]


def test_mapping_static_allowlist_and_unknown_task_pins_are_all_checked() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["inputs"]["static_catalog_sha256"] = "0" * 64
    mapping["inputs"]["target_allowlist_sha256"] = "0" * 64
    mapping["tasks"].append(dict(mapping["tasks"][0], task_id="scheduled:999"))

    plan = _build(inventory, catalog, mapping, allowlist)

    for issue in (
        "mapping_static_catalog_hash_mismatch",
        "mapping_target_allowlist_hash_mismatch",
        "mapping_contains_unknown_task_id",
    ):
        assert issue in plan["integrity_issues"]


def test_mapping_invalid_target_schedule_and_review_shapes_are_not_copied() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    mapping["tasks"][0]["schedule"] = None
    mapping["tasks"][0]["review"] = None
    mapping["tasks"][0]["provider"] = []
    mapping["tasks"][0]["contract_sha256"] = "bad"

    plan = _build(inventory, catalog, mapping, allowlist)
    task = plan["tasks"][0]

    assert "mapping_schedule_missing" in task["blocked_reasons"]
    assert "mapping_review_missing" in task["blocked_reasons"]
    assert "mapping_target_identity_invalid" in task["blocked_reasons"]
    assert task["review"] is None
    assert task["target"] is None
    assert '"provider": []' not in render_plan(plan)


def test_helper_mapping_is_rejected_and_report_does_not_enable_helper() -> None:
    inventory, catalog, mapping, allowlist = _inputs()
    helper_script = dict(inventory["metadata"]["data_scripts"]["records"][0])
    helper_script.update(script_id="helper", source_path="src/helper.py")
    inventory["metadata"]["data_scripts"]["records"].append(helper_script)
    inventory["metadata"]["data_scripts"]["row_count_exact"] = 2
    inventory["counts"]["data_script_rows_exact"] = 2
    inventory["tasks"].append(
        {
            "task_id": "script:helper",
            "script_id": "helper",
            "script_path": "src/helper.py",
            "path_status": "PATH_CANDIDATE",
            "runtime_is_active": 1,
        }
    )
    helper_file = dict(catalog["files"][0], source_path="src/helper.py", source_sha256="7" * 64)
    catalog["files"].append(helper_file)
    catalog["summary"]["legacy_script_count"] = 2
    mapping["tasks"].append(dict(mapping["tasks"][0], task_id="script:helper"))

    plan = _build(inventory, catalog, mapping, allowlist)

    assert "helper_must_not_have_scheduled_job_mapping" in plan["helpers"][0]["blocked_reasons"]
    assert plan["helpers"][0]["enabled"] is False


def test_low_level_validators_reject_invalid_path_id_hash_schedule_and_time() -> None:
    assert not migration_plan_module._valid_identifier("bad id")
    assert not migration_plan_module._is_sha256("A" * 64)
    assert not migration_plan_module._safe_relative_path("../escape.py")
    assert not migration_plan_module._safe_relative_path("/absolute.py")
    assert not migration_plan_module._safe_relative_path("src\\bad.py")
    assert not migration_plan_module._valid_task_map_id("other:1")
    assert not migration_plan_module._valid_task_map_id("scheduled:abc")
    assert not migration_plan_module._valid_script_id("bad id")
    assert migration_plan_module._safe_schedule_type("cron") == "cron"
    assert migration_plan_module._safe_schedule_type("bad value") is None
    assert migration_plan_module._safe_schedule_expression("0 1 * * *") == "0 1 * * *"
    assert migration_plan_module._safe_schedule_expression("bad\nline") is None
    assert migration_plan_module._safe_static_status({"unexpected": True}) == "UNKNOWN"
    assert not migration_plan_module._valid_timezone("Invalid/Zone")
    assert not migration_plan_module._valid_utc_timestamp("2026-13-01T12:00:00Z")
    assert migration_plan_module._task_id_sort_key(
        "scheduled:12"
    ) < migration_plan_module._task_id_sort_key("script:helper")
    assert migration_plan_module._string_list("bad") == []
    assert migration_plan_module._string_list(["one", None]) == ["one"]


def test_small_validators_cover_parameter_hash_and_json_object_errors() -> None:
    assert migration_plan_module._parameter_digest(None) is None
    assert migration_plan_module._parameter_digest({"parameter_summary": []}) is None
    assert (
        migration_plan_module._parameter_digest(
            {"parameter_summary": {"canonical_payload_sha256": "bad"}}
        )
        is None
    )
    assert migration_plan_module._combined_parameter_digest(None, SCRIPT_PARAMS_HASH) is None
    with pytest.raises(MigrationPlanError, match="rows_invalid_or_too_large"):
        migration_plan_module._object_rows({}, "rows", 2)
    with pytest.raises(MigrationPlanError, match="row_must_be_object"):
        migration_plan_module._mapping([], "row")
    with pytest.raises(MigrationPlanError, match="runtime_inventory_must_be_object"):
        build_migration_plan([], {}, None)
