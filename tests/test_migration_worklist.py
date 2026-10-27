"""Behavior tests for the redacted, pinned AFI204 migration worklist."""

from __future__ import annotations

import json
import re
import stat
from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

import opendata.migration_worklist as migration_worklist_module
from opendata.migration_plan import (
    MigrationPlanError,
    build_migration_plan,
    canonical_json_bytes,
    canonical_json_sha256,
)
from opendata.migration_worklist import (
    MigrationWorklistError,
    _project_helper_row,
    _project_scheduled_row,
    _verify_plan_against_sources,
    _verify_plan_self_hash,
    build_migration_worklist,
    render_migration_worklist,
    write_migration_worklist_new_file,
)
from scripts.ops.build_migration_worklist import main as build_migration_worklist_main

if TYPE_CHECKING:
    from pathlib import Path

_TASK_PARAMETER_SHA = "1" * 64
_SCRIPT_PARAMETER_SHA = "2" * 64
_SOURCE_HASHES = {
    "shared_script": "a" * 64,
    "second_script": "b" * 64,
    "registry_helper": "c" * 64,
    "orphan_helper": "d" * 64,
}
_SECRET_SENTINEL = "mysql://worklist-user:do-not-print@secret-host/private-db"
_PARAMETER_SENTINEL = "private-api-token-value-do-not-print"
_PRIVATE_PATH_SENTINEL = "private-legacy-root/secret_records.py"


def _parameter_summary(digest: str) -> dict[str, object]:
    """Return a synthetic already-redacted runtime parameter summary."""
    return {
        "canonical_payload_sha256": digest,
        "key_names": ["api_token_name"],
        "credential_flag": True,
        "payload_present": True,
    }


def _source_inputs() -> tuple[dict[str, object], dict[str, object]]:
    """Create a minimal full runtime/catalog pair with 3 jobs and 2 helpers."""
    script_specs = [
        ("shared_script", _PRIVATE_PATH_SENTINEL, _SOURCE_HASHES["shared_script"]),
        ("second_script", "jobs/second.py", _SOURCE_HASHES["second_script"]),
        ("registry_helper", "helpers/registered.py", _SOURCE_HASHES["registry_helper"]),
        ("orphan_helper", "helpers/orphan.py", _SOURCE_HASHES["orphan_helper"]),
    ]
    script_records = [
        {
            "script_id": script_id,
            "source_path": path,
            "path_status": "PATH_CANDIDATE",
            "is_active": 1,
            "parameter_summary": _parameter_summary(_SCRIPT_PARAMETER_SHA),
            "parameters": {"api_key": _PARAMETER_SENTINEL},
        }
        for script_id, path, _source_hash in script_specs
    ]
    scheduled_records = [
        {
            "id": task_id,
            "script_id": script_id,
            "schedule_type": "cron",
            "expression": expression,
            "is_active": 1,
            "last_execution": None,
            "next_execution": None,
            "timeout": 60,
            "retries": 0,
            "retry_on_failure": False,
            "parameter_summary": _parameter_summary(_TASK_PARAMETER_SHA),
            "parameters": {"api_key": _PARAMETER_SENTINEL},
        }
        for task_id, script_id, expression in (
            (10, "second_script", "0 4 * * 1-5"),
            (3, "shared_script", "15 2 * * *"),
            (2, "shared_script", "30 2 * * *"),
        )
    ]
    task_map = [
        {
            "task_id": f"scheduled:{record['id']}",
            "script_id": record["script_id"],
            "script_path": next(
                path for script_id, path, _ in script_specs if script_id == record["script_id"]
            ),
            "path_status": "PATH_CANDIDATE",
            "runtime_is_active": record["is_active"],
        }
        for record in scheduled_records
    ]
    task_map.extend(
        {
            "task_id": f"script:{script_id}",
            "script_id": script_id,
            "script_path": path,
            "path_status": "PATH_CANDIDATE",
            "runtime_is_active": 0,
            "runtime_task_kind": "script_registry_only",
        }
        for script_id, path, _ in script_specs
        if script_id in {"registry_helper", "orphan_helper"}
    )
    runtime = {
        "schema_version": "1.0",
        "source_root": "/legacy/afi",
        "generated_at_utc": "2026-10-26T00:00:00+00:00",
        "counts": {
            "scheduled_task_rows_exact": len(scheduled_records),
            "data_script_rows_exact": len(script_records),
        },
        "readonly_transaction": {
            "metadata_confirmed": True,
            "warehouse_confirmed": True,
        },
        "metadata": {
            "scheduled_tasks": {
                "row_count_exact": len(scheduled_records),
                "records": scheduled_records,
            },
            "data_scripts": {
                "row_count_exact": len(script_records),
                "records": script_records,
            },
        },
        "tasks": task_map,
        "api_endpoint": _SECRET_SENTINEL,
    }
    catalog = {
        "schema_version": "1.1",
        "source_root": "/legacy/afi",
        "opendata_root": "/local/opendata",
        "summary": {"legacy_script_count": len(script_records)},
        "errors": [],
        "files": [
            {
                "source_path": path,
                "source_sha256": source_hash,
                "status": "MISSING_PROVIDER",
                "calls": [],
            }
            for _script_id, path, source_hash in script_specs
        ],
        "opendata_provider_models": [],
        "database_endpoint": _SECRET_SENTINEL,
    }
    return runtime, catalog


def _plan_with_sources() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    """Build a self-hashed offline plan from the synthetic source pair."""
    runtime, catalog = _source_inputs()
    plan = build_migration_plan(runtime, catalog)
    return plan, runtime, catalog


def _replace_script_id(runtime: dict[str, object], old_id: str, new_id: str) -> None:
    """Replace one synthetic ID consistently across the runtime snapshot."""
    metadata = runtime["metadata"]
    for section_name in ("data_scripts", "scheduled_tasks"):
        section = metadata[section_name]
        for row in section["records"]:
            if row.get("script_id") == old_id:
                row["script_id"] = new_id
    for row in runtime["tasks"]:
        if row.get("script_id") == old_id:
            row["script_id"] = new_id
        if row.get("task_id") == f"script:{old_id}":
            row["task_id"] = f"script:{new_id}"


def _rehash_plan(plan: dict[str, object]) -> None:
    """Recompute the planner-compatible digest after a test-only mutation."""
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    plan["plan_sha256"] = canonical_json_sha256(unsigned)


def _rehash_worklist(worklist: dict[str, object]) -> None:
    """Recompute the worklist digest after a test-only structural mutation."""
    unsigned = {key: value for key, value in worklist.items() if key != "worklist_sha256"}
    worklist["worklist_sha256"] = canonical_json_sha256(unsigned)


def _mutate_worklist(worklist: dict[str, object], mutation: str) -> None:
    """Apply one focused structural mutation to a valid synthetic queue."""
    scheduled = worklist["scheduled_tasks"]
    helpers = worklist["registry_helpers"]
    assert isinstance(scheduled, list) and isinstance(helpers, list)
    first_scheduled = scheduled[0]
    second_scheduled = scheduled[1]
    first_helper = helpers[0]
    second_helper = helpers[1]
    assert isinstance(first_scheduled, dict) and isinstance(second_scheduled, dict)
    assert isinstance(first_helper, dict) and isinstance(second_helper, dict)

    if mutation == "metadata":
        worklist["queue_status"] = "READY"
    elif mutation == "timestamp_noncanonical":
        worklist["source_snapshot_generated_at_utc"] = "2026-10-26T00:00:00+00:00"
    elif mutation == "timestamp_naive":
        worklist["source_snapshot_generated_at_utc"] = "2026-10-26T00:00:00"
    elif mutation == "timestamp_too_long":
        worklist["source_snapshot_generated_at_utc"] = "x" * 65
    elif mutation == "source_plan_hash":
        worklist["source_plan_sha256"] = "A" * 64
    elif mutation == "security_boundary":
        boundary = worklist["security_boundary"]
        assert isinstance(boundary, dict)
        boundary["distribution_status"] = "DISTRIBUTABLE"
    elif mutation == "inputs_extra_field":
        inputs = worklist["inputs"]
        assert isinstance(inputs, dict)
        inputs["source_path"] = "private/path"
    elif mutation == "input_hash":
        inputs = worklist["inputs"]
        assert isinstance(inputs, dict)
        inputs["runtime_inventory_sha256"] = "A" * 64
    elif mutation == "lane_assignment":
        lane_assignment = worklist["lane_assignment"]
        assert isinstance(lane_assignment, dict)
        lane_assignment["assignment_is_ownership"] = True
    elif mutation == "scope":
        scope = worklist["scope"]
        assert isinstance(scope, dict)
        scope["migration_completeness"] = "COMPLETE"
    elif mutation == "scheduled_not_list":
        worklist["scheduled_tasks"] = None
    elif mutation == "lanes_bad_length":
        lanes = worklist["lanes"]
        assert isinstance(lanes, list)
        worklist["lanes"] = lanes[:-1]
    elif mutation == "scheduled_extra_field":
        first_scheduled["source_path"] = "private/path"
    elif mutation == "scheduled_bad_id":
        first_scheduled["task_id"] = "scheduled:02"
    elif mutation == "scheduled_duplicate_id":
        second_scheduled["task_id"] = first_scheduled["task_id"]
    elif mutation == "scheduled_bad_script_id":
        first_scheduled["script_id"] = "private/path"
    elif mutation == "scheduled_secret_script_id":
        first_scheduled["script_id"] = "AKIA1234567890ABCDEF"
    elif mutation == "scheduled_bad_hash":
        first_scheduled["source_sha256"] = "A" * 64
    elif mutation == "scheduled_bad_status":
        first_scheduled["static_status"] = "UNKNOWN"
    elif mutation == "scheduled_bool_blocker_count":
        first_scheduled["planner_blocker_count"] = True
    elif mutation == "scheduled_negative_blocker_count":
        first_scheduled["planner_blocker_count"] = -1
    elif mutation == "scheduled_order":
        worklist["scheduled_tasks"] = list(reversed(scheduled))
    elif mutation == "scheduled_bad_lane":
        first_scheduled["review_lane"] = "lane-99"
    elif mutation == "scheduled_bad_assignment_status":
        first_scheduled["assignment_status"] = "ASSIGNED"
    elif mutation == "scheduled_assignee":
        first_scheduled["assignee"] = "developer"
    elif mutation == "scheduled_bad_review_status":
        first_scheduled["review_status"] = "APPROVED"
    elif mutation == "scheduled_enabled":
        first_scheduled["enabled"] = True
    elif mutation == "helper_extra_field":
        first_helper["source_path"] = "private/path"
    elif mutation == "helper_bad_id":
        first_helper["task_id"] = "script:other_helper"
    elif mutation == "helper_duplicate_id":
        helpers[1] = deepcopy(first_helper)
    elif mutation == "helper_bad_script_id":
        first_helper["script_id"] = "private/path"
    elif mutation == "helper_bad_hash":
        first_helper["source_sha256"] = "A" * 64
    elif mutation == "helper_bad_status":
        first_helper["static_status"] = "UNKNOWN"
    elif mutation == "helper_bad_type":
        first_helper["work_item_type"] = "SCHEDULED"
    elif mutation == "helper_bad_lane":
        first_helper["review_lane"] = "lane-99"
    elif mutation == "helper_bad_assignment_status":
        first_helper["assignment_status"] = "ASSIGNED"
    elif mutation == "helper_assignee":
        first_helper["assignee"] = "developer"
    elif mutation == "helper_bad_review_status":
        first_helper["review_status"] = "APPROVED"
    elif mutation == "helper_enabled":
        first_helper["enabled"] = True
    elif mutation == "helper_order":
        worklist["registry_helpers"] = list(reversed(helpers))
    elif mutation == "helper_overlap":
        first_helper["script_id"] = first_scheduled["script_id"]
        first_helper["task_id"] = f"script:{first_scheduled['script_id']}"
        first_helper["review_lane"] = first_scheduled["review_lane"]
        worklist["registry_helpers"] = sorted(helpers, key=lambda item: str(item["task_id"]))
    elif mutation == "summary":
        summary = worklist["summary"]
        assert isinstance(summary, dict)
        summary["unassigned_work_item_count"] += 1
    elif mutation == "lane_counts":
        lanes = worklist["lanes"]
        assert isinstance(lanes, list) and isinstance(lanes[0], dict)
        lanes[0]["work_item_count"] += 1
    elif mutation == "digest":
        worklist["worklist_sha256"] = "0" * 64
        return
    else:
        raise AssertionError(f"Unknown test mutation: {mutation}")
    _rehash_worklist(worklist)


def _walk_keys(value: object) -> set[str]:
    """Collect mapping keys recursively from a JSON-like value."""
    keys: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(str(key))
            keys.update(_walk_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(_walk_keys(item))
    return keys


def test_worklist_preserves_exact_rows_and_assigns_stable_script_lanes() -> None:
    """Keep every task/helper once, naturally sorted, and grouped by script."""
    plan, runtime, catalog = _plan_with_sources()

    worklist = build_migration_worklist(plan, runtime, catalog)

    scheduled = worklist["scheduled_tasks"]
    helpers = worklist["registry_helpers"]
    assert isinstance(scheduled, list)
    assert isinstance(helpers, list)
    assert [row["task_id"] for row in scheduled] == [
        "scheduled:2",
        "scheduled:3",
        "scheduled:10",
    ]
    assert [row["task_id"] for row in helpers] == [
        "script:orphan_helper",
        "script:registry_helper",
    ]
    assert len({row["task_id"] for row in scheduled + helpers}) == 5
    assert scheduled[0]["review_lane"] == scheduled[1]["review_lane"] == "lane-03"
    assert scheduled[2]["review_lane"] == "lane-05"
    assert helpers[1]["review_lane"] == "lane-03"
    assert helpers[0]["review_lane"] == "lane-05"
    assert scheduled[0]["source_sha256"] == _SOURCE_HASHES["shared_script"]
    assert helpers[0]["source_sha256"] == _SOURCE_HASHES["orphan_helper"]
    assert all(row["enabled"] is False for row in scheduled + helpers)
    assert all(row["assignee"] is None for row in scheduled + helpers)
    assert all(row["assignment_status"] == "UNASSIGNED" for row in scheduled + helpers)
    assert all(row["review_status"] == "PENDING" for row in scheduled + helpers)
    assert worklist["summary"] == {
        "scheduled_task_work_item_count": 3,
        "registry_helper_work_item_count": 2,
        "unassigned_work_item_count": 5,
    }
    assert worklist["scope"]["registered_helpers"] == "SEPARATE_UNSCHEDULED_REVIEW_QUEUE"
    assert worklist["scope"]["non_task_dependencies"] == "NOT_ASSESSED"
    assert worklist["scope"]["migration_completeness"] == "NOT_CLAIMED"
    assert worklist["security_boundary"] == {
        "classification": "LOCAL_PRIVATE_ONLY",
        "file_mode_octal": "0600",
        "distribution_status": "NOT_FOR_DISTRIBUTION",
        "hash_semantics": "CONTENT_BINDING_ONLY_NOT_AUTHENTICATION_OR_AUTHORIZATION",
    }
    assert worklist["queue_status"] == "HISTORICAL_SNAPSHOT_PENDING_REVIEW"
    assert worklist["freshness_status"] == "REQUIRES_REFRESH_BEFORE_EXECUTION"


def test_worklist_projection_is_deterministic_and_excludes_sensitive_plan_fields() -> None:
    """Project only allowlisted fields and omit paths, schedules, and parameters."""
    plan, runtime, catalog = _plan_with_sources()

    first = build_migration_worklist(plan, runtime, catalog)
    second = build_migration_worklist(plan, runtime, catalog)
    rendered = render_migration_worklist(first)
    keys = _walk_keys(first)

    assert first == second
    assert rendered == render_migration_worklist(second)
    assert first["worklist_sha256"] == canonical_json_sha256(
        {key: value for key, value in first.items() if key != "worklist_sha256"}
    )
    assert not keys & {
        "script_path",
        "legacy_schedule",
        "task_params_sha256",
        "script_params_sha256",
        "params_sha256",
        "parameters",
        "parameter_summary",
        "review",
        "target",
        "target_schedule",
        "execution_governance",
    }
    for sentinel in (
        _SECRET_SENTINEL,
        _PARAMETER_SENTINEL,
        _PRIVATE_PATH_SENTINEL,
        "15 2 * * *",
    ):
        assert sentinel not in rendered
    assert "task_id" in keys
    assert "script_id" in keys
    assert "source_sha256" in keys


def test_renderer_rejects_non_allowlisted_fields_even_with_recomputed_hash() -> None:
    """Do not let a caller bypass the redacted projection through the writer."""
    plan, runtime, catalog = _plan_with_sources()
    worklist = build_migration_worklist(plan, runtime, catalog)
    tampered = deepcopy(worklist)
    tampered["script_path"] = _PRIVATE_PATH_SENTINEL
    unsigned = {key: value for key, value in tampered.items() if key != "worklist_sha256"}
    tampered["worklist_sha256"] = canonical_json_sha256(unsigned)

    with pytest.raises(MigrationWorklistError, match="worklist_projection_fields_invalid"):
        render_migration_worklist(tampered)


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("metadata", "worklist_projection_metadata_invalid"),
        ("timestamp_noncanonical", "worklist_snapshot_timestamp_not_canonical"),
        ("timestamp_naive", "source_snapshot_timestamp_not_utc"),
        ("timestamp_too_long", "source_snapshot_timestamp_invalid"),
        ("source_plan_hash", "source_plan_sha256_invalid"),
        ("security_boundary", "security_boundary_metadata_invalid"),
        ("inputs_extra_field", "worklist_input_projection_invalid"),
        ("input_hash", "runtime_inventory_sha256_invalid"),
        ("lane_assignment", "lane_assignment_metadata_invalid"),
        ("scope", "worklist_scope_metadata_invalid"),
        ("scheduled_not_list", "worklist_item_lists_invalid"),
        ("lanes_bad_length", "worklist_lanes_invalid"),
        ("scheduled_extra_field", "scheduled_worklist_projection_fields_invalid"),
        ("scheduled_bad_id", "scheduled_worklist_task_id_invalid"),
        ("scheduled_duplicate_id", "duplicate_work_item_id"),
        ("scheduled_bad_script_id", "script_id_invalid"),
        ("scheduled_secret_script_id", "script_id_invalid"),
        ("scheduled_bad_hash", "source_sha256_invalid"),
        ("scheduled_bad_status", "static_status_invalid"),
        ("scheduled_bool_blocker_count", "planner_blocker_count_invalid"),
        ("scheduled_negative_blocker_count", "planner_blocker_count_invalid"),
        ("scheduled_order", "scheduled_worklist_order_invalid"),
        ("scheduled_bad_lane", "worklist_item_assignment_state_invalid"),
        ("scheduled_bad_assignment_status", "worklist_item_assignment_state_invalid"),
        ("scheduled_assignee", "worklist_item_assignment_state_invalid"),
        ("scheduled_bad_review_status", "worklist_item_assignment_state_invalid"),
        ("scheduled_enabled", "worklist_item_assignment_state_invalid"),
        ("helper_extra_field", "helper_worklist_projection_fields_invalid"),
        ("helper_bad_id", "helper_worklist_task_id_invalid"),
        ("helper_duplicate_id", "duplicate_work_item_id"),
        ("helper_bad_script_id", "script_id_invalid"),
        ("helper_bad_hash", "source_sha256_invalid"),
        ("helper_bad_status", "static_status_invalid"),
        ("helper_bad_type", "helper_worklist_type_invalid"),
        ("helper_order", "helper_worklist_order_invalid"),
        ("helper_bad_lane", "worklist_item_assignment_state_invalid"),
        ("helper_bad_assignment_status", "worklist_item_assignment_state_invalid"),
        ("helper_assignee", "worklist_item_assignment_state_invalid"),
        ("helper_bad_review_status", "worklist_item_assignment_state_invalid"),
        ("helper_enabled", "worklist_item_assignment_state_invalid"),
        ("helper_overlap", "scheduled_helper_script_overlap"),
        ("summary", "worklist_summary_mismatch"),
        ("lane_counts", "worklist_lane_counts_mismatch"),
        ("digest", "migration_worklist_sha256_invalid"),
    ],
)
def test_renderer_rejects_rehashed_structural_tampering(mutation: str, error_code: str) -> None:
    """Check each sink-side invariant after the attacker recomputes the digest."""
    plan, runtime, catalog = _plan_with_sources()
    worklist = build_migration_worklist(plan, runtime, catalog)
    tampered = deepcopy(worklist)
    _mutate_worklist(tampered, mutation)

    with pytest.raises(MigrationWorklistError, match=error_code):
        render_migration_worklist(tampered)


def test_worklist_builder_rejects_summary_mismatches_and_reviewed_mappings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep work item counts and the initial no-reviewed-mapping scope exact."""
    plan, runtime, catalog = _plan_with_sources()
    cases = (
        ("scheduled_task_count", 4, "scheduled_task_count_mismatch"),
        ("non_scheduled_helper_count", 3, "helper_count_mismatch"),
        ("mapping_row_count", 1, "reviewed_mapping_not_allowed_for_initial_worklist"),
        ("runtime_static_anti_join_issue_count", 1, "runtime_static_join_integrity_failed"),
        ("script_registry_count", 99, "script_registry_anti_join_mismatch"),
    )
    for summary_field, bad_value, error_code in cases:
        invalid_plan = deepcopy(plan)
        summary = invalid_plan["summary"]
        assert isinstance(summary, dict)
        summary[summary_field] = bad_value
        monkeypatch.setattr(
            migration_worklist_module,
            "_verify_plan_against_sources",
            lambda *_args, candidate=invalid_plan: candidate,
        )
        with pytest.raises(MigrationWorklistError, match=error_code):
            build_migration_worklist(invalid_plan, runtime, catalog)


def test_worklist_builder_rechecks_projected_script_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retain a final anti-join guard even if a row projector is bypassed."""
    plan, runtime, catalog = _plan_with_sources()
    scheduled_rows = plan["tasks"]
    helper_rows = plan["helpers"]
    assert isinstance(scheduled_rows, list) and isinstance(helper_rows, list)
    overlapping_script_id = str(scheduled_rows[0]["script_id"])
    helper_record = deepcopy(helper_rows[0])
    helper_record["script_id"] = overlapping_script_id
    helper_record["task_id"] = f"script:{overlapping_script_id}"
    helper_item = _project_helper_row(helper_record, set(), set())
    monkeypatch.setattr(migration_worklist_module, "_verify_plan_against_sources", lambda *_: plan)
    monkeypatch.setattr(
        migration_worklist_module,
        "_project_helper_row",
        lambda _row, _seen, _scheduled: helper_item,
    )

    with pytest.raises(MigrationWorklistError, match="scheduled_helper_script_overlap"):
        build_migration_worklist(plan, runtime, catalog)


@pytest.mark.parametrize(
    ("timestamp", "error_code"),
    [
        (None, "source_snapshot_timestamp_invalid"),
        ("not-a-timestamp", "source_snapshot_timestamp_invalid"),
        ("2026-10-26T00:00:00", "source_snapshot_timestamp_not_utc"),
        ("2026-10-26T00:00:00+08:00", "source_snapshot_timestamp_not_utc"),
        ("x" * 65, "source_snapshot_timestamp_invalid"),
    ],
)
def test_worklist_builder_rejects_invalid_source_timestamps(
    timestamp: object, error_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not turn invalid, local-time, or non-UTC snapshot times into evidence."""
    plan, runtime, catalog = _plan_with_sources()
    monkeypatch.setattr(migration_worklist_module, "_verify_plan_against_sources", lambda *_: plan)
    runtime["generated_at_utc"] = timestamp

    with pytest.raises(MigrationWorklistError, match=error_code):
        build_migration_worklist(plan, runtime, catalog)


@pytest.mark.parametrize(
    ("size_delta", "error_code"),
    [(-1, "migration_worklist_exceeds_size_limit"), (0, "migration_worklist_exceeds_size_limit")],
)
def test_worklist_builder_enforces_both_canonical_size_limits(
    size_delta: int, error_code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound both the unsigned projection and the final digest-bearing document."""
    plan, runtime, catalog = _plan_with_sources()
    baseline = build_migration_worklist(plan, runtime, catalog)
    unsigned = {key: value for key, value in baseline.items() if key != "worklist_sha256"}
    unsigned_size = len(canonical_json_bytes(unsigned))
    monkeypatch.setattr(migration_worklist_module, "MAX_PLAN_BYTES", unsigned_size + size_delta)

    with pytest.raises(MigrationWorklistError, match=error_code):
        build_migration_worklist(plan, runtime, catalog)


def test_renderer_enforces_pretty_json_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the human-readable file bounded even when its canonical form fits."""
    plan, runtime, catalog = _plan_with_sources()
    worklist = build_migration_worklist(plan, runtime, catalog)
    monkeypatch.setattr(migration_worklist_module, "MAX_PLAN_BYTES", 1)

    with pytest.raises(MigrationWorklistError, match="migration_worklist_exceeds_size_limit"):
        render_migration_worklist(worklist)


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("task_id", "scheduled_task_id_invalid"),
        ("duplicate_task_id", "duplicate_work_item_id"),
        ("script_id", "script_id_invalid"),
        ("enabled", "scheduled_plan_row_not_blocked_and_disabled"),
        ("target_mapping", "scheduled_plan_row_contains_reviewed_mapping"),
        ("review_mapping", "scheduled_plan_row_contains_reviewed_mapping"),
        ("target_schedule", "scheduled_plan_row_contains_reviewed_mapping"),
        ("disposition", "scheduled_plan_row_not_blocked_and_disabled"),
        ("static_status", "static_status_invalid"),
        ("blocked_reasons", "scheduled_blocked_reasons_invalid"),
        ("blocked_reasons_not_list", "scheduled_blocked_reasons_invalid"),
        ("script_sha256", "script_sha256_invalid"),
    ],
)
def test_scheduled_plan_projection_rejects_invalid_rows(mutation: str, error_code: str) -> None:
    """Validate planner rows before projecting them into the narrow worklist."""
    plan, _runtime, _catalog = _plan_with_sources()
    rows = plan["tasks"]
    assert isinstance(rows, list)
    row = deepcopy(rows[0])
    seen: set[str] = set()
    if mutation == "task_id":
        row["task_id"] = "scheduled:01"
    elif mutation == "duplicate_task_id":
        row["task_id"] = "scheduled:2"
        seen.add("scheduled:2")
    elif mutation == "script_id":
        row["script_id"] = "private/path"
    elif mutation == "enabled":
        row["enabled"] = True
    elif mutation == "target_mapping":
        row["target"] = {"target_id": "invented"}
    elif mutation == "review_mapping":
        row["review"] = {"reviewed": True}
    elif mutation == "target_schedule":
        row["target_schedule"] = {"schedule": "invented"}
    elif mutation == "disposition":
        row["disposition"] = "READY_DISABLED"
    elif mutation == "static_status":
        row["static_status"] = "UNKNOWN"
    elif mutation == "blocked_reasons":
        row["blocked_reasons"] = [None]
    elif mutation == "blocked_reasons_not_list":
        row["blocked_reasons"] = "not-a-list"
    elif mutation == "script_sha256":
        row["script_sha256"] = "A" * 64
    else:
        raise AssertionError(f"Unknown test mutation: {mutation}")

    with pytest.raises(MigrationWorklistError, match=error_code):
        _project_scheduled_row(row, seen)


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("task_id", "registry_helper_id_mismatch"),
        ("duplicate_task_id", "duplicate_work_item_id"),
        ("scheduled_overlap", "scheduled_helper_script_overlap"),
        ("enabled", "registry_helper_disposition_invalid"),
        ("disposition", "registry_helper_disposition_invalid"),
        ("blocked_reasons", "registry_helper_blocked_reasons_invalid"),
        ("blocked_reasons_not_list", "registry_helper_blocked_reasons_invalid"),
        ("missing_marker", "registry_helper_scheduled_status_missing"),
        ("script_sha256", "script_sha256_invalid"),
    ],
)
def test_helper_plan_projection_rejects_invalid_rows(mutation: str, error_code: str) -> None:
    """Keep registry helpers separate from scheduled rows and execution scope."""
    plan, _runtime, _catalog = _plan_with_sources()
    rows = plan["helpers"]
    assert isinstance(rows, list)
    row = deepcopy(rows[0])
    seen: set[str] = set()
    scheduled_ids: set[str] = set()
    if mutation == "task_id":
        row["task_id"] = "script:other_helper"
    elif mutation == "duplicate_task_id":
        seen.add(str(row["task_id"]))
    elif mutation == "scheduled_overlap":
        scheduled_ids.add(str(row["script_id"]))
    elif mutation == "enabled":
        row["enabled"] = True
    elif mutation == "disposition":
        row["disposition"] = "BLOCKED"
    elif mutation == "blocked_reasons":
        row["blocked_reasons"] = [None]
    elif mutation == "blocked_reasons_not_list":
        row["blocked_reasons"] = "not-a-list"
    elif mutation == "missing_marker":
        row["blocked_reasons"] = ["some_other_blocker"]
    elif mutation == "script_sha256":
        row["script_sha256"] = "A" * 64
    else:
        raise AssertionError(f"Unknown test mutation: {mutation}")

    with pytest.raises(MigrationWorklistError, match=error_code):
        _project_helper_row(row, seen, scheduled_ids)


def test_plan_verifier_rejects_invalid_and_reviewed_plan_states(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the verifier's fail-closed state, pin, and integrity boundaries."""
    plan, runtime, catalog = _plan_with_sources()
    cases = (
        ("plan_state", "schema_version", "2.0", "plan_not_an_unreviewed_disabled_dry_run"),
        ("plan_state", "plan_mode", "NOT_DRY_RUN", "plan_not_an_unreviewed_disabled_dry_run"),
        (
            "plan_state",
            "execution_supported",
            True,
            "plan_not_an_unreviewed_disabled_dry_run",
        ),
        (
            "plan_state",
            "enabled_jobs_created",
            True,
            "plan_not_an_unreviewed_disabled_dry_run",
        ),
        (
            "plan_state",
            "plan_status",
            "READY_DISABLED",
            "plan_not_an_unreviewed_disabled_dry_run",
        ),
        (
            "inputs",
            "reviewed_mapping_sha256",
            "f" * 64,
            "reviewed_or_target_inputs_not_allowed",
        ),
        (
            "inputs",
            "target_allowlist_sha256",
            "f" * 64,
            "reviewed_or_target_inputs_not_allowed",
        ),
        (
            "inputs",
            "target_warehouse_fingerprint_sha256",
            "f" * 64,
            "reviewed_or_target_inputs_not_allowed",
        ),
        (
            "summary",
            "mapping_row_count",
            1,
            "reviewed_mapping_not_allowed_for_initial_worklist",
        ),
        (
            "summary",
            "runtime_static_anti_join_issue_count",
            1,
            "runtime_static_join_integrity_failed",
        ),
        ("integrity", "integrity_issues", "not-a-list", "plan_integrity_issue_shape_invalid"),
        (
            "integrity",
            "integrity_issues",
            ["runtime_fixture_issue"],
            "runtime_static_join_integrity_failed",
        ),
    )
    for category, field, bad_value, error_code in cases:
        candidate = deepcopy(plan)
        if category == "plan_state":
            candidate[field] = bad_value
        elif category == "inputs":
            inputs = candidate["inputs"]
            assert isinstance(inputs, dict)
            inputs[field] = bad_value
        elif category == "summary":
            summary = candidate["summary"]
            assert isinstance(summary, dict)
            summary[field] = bad_value
        else:
            candidate[field] = bad_value
        _rehash_plan(candidate)
        monkeypatch.setattr(
            migration_worklist_module,
            "build_migration_plan",
            lambda *_args, expected=candidate: expected,
        )

        with pytest.raises(MigrationWorklistError, match=error_code):
            _verify_plan_against_sources(candidate, runtime, catalog)


def test_plan_verifier_rejects_source_rebuild_and_digest_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject source rebuild errors, mismatched plans, and noncanonical hashes."""
    plan, runtime, catalog = _plan_with_sources()
    monkeypatch.setattr(
        migration_worklist_module,
        "build_migration_plan",
        lambda *_: (_ for _ in ()).throw(MigrationPlanError("fixture failure")),
    )
    with pytest.raises(MigrationWorklistError, match="plan_source_rebuild_failed"):
        _verify_plan_against_sources(plan, runtime, catalog)

    other_plan = deepcopy(plan)
    other_plan["plan_status"] = "NOT_THE_SAME"
    _rehash_plan(other_plan)
    monkeypatch.setattr(migration_worklist_module, "build_migration_plan", lambda *_: plan)
    with pytest.raises(MigrationWorklistError, match="plan_does_not_match_pinned_sources"):
        _verify_plan_against_sources(other_plan, runtime, catalog)

    noncanonical = deepcopy(plan)
    noncanonical["non_json_value"] = float("nan")
    with pytest.raises(MigrationWorklistError, match="migration_plan_sha256_invalid"):
        _verify_plan_self_hash(noncanonical)


def test_private_validation_primitives_reject_wrong_shapes_and_types() -> None:
    """Keep shared validators strict at untrusted JSON boundaries."""
    from opendata.migration_worklist import (
        _count,
        _mapping,
        _mapping_rows,
        _normalize_utc_timestamp,
        _require_sha256,
        _static_status,
        _task_id_sort_key,
    )

    for value in (None, [], {1: "not-a-string-key"}):
        with pytest.raises(MigrationWorklistError, match="fixture_must_be_object"):
            _mapping(value, "fixture")
    with pytest.raises(MigrationWorklistError, match="fixture_must_be_object_list"):
        _mapping_rows({}, "fixture")
    with pytest.raises(MigrationWorklistError, match="fixture_row_must_be_object"):
        _mapping_rows([None], "fixture")
    for value in (True, -1, 1.0):
        with pytest.raises(MigrationWorklistError, match="fixture_invalid"):
            _count(value, "fixture")
    with pytest.raises(MigrationWorklistError, match="fixture_invalid"):
        _require_sha256("A" * 64, "fixture")
    with pytest.raises(MigrationWorklistError, match="static_status_invalid"):
        _static_status("UNKNOWN")
    with pytest.raises(MigrationWorklistError, match="source_snapshot_timestamp_invalid"):
        _normalize_utc_timestamp("not-a-timestamp")
    with pytest.raises(MigrationWorklistError, match="source_snapshot_timestamp_not_utc"):
        _normalize_utc_timestamp("2026-10-26T00:00:00+08:00")
    assert _task_id_sort_key("script:helper") == (1, "script:helper")


def test_cli_stdout_reports_creation_without_emitting_digests(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Show a safe count summary while keeping content digests in the private file."""
    plan, runtime, catalog = _plan_with_sources()
    input_paths = {
        "plan": tmp_path / "plan.json",
        "runtime": tmp_path / "runtime.json",
        "catalog": tmp_path / "catalog.json",
        "output": tmp_path / "worklist.json",
    }
    for name, value in (
        ("plan", plan),
        ("runtime", runtime),
        ("catalog", catalog),
    ):
        input_paths[name].write_text(json.dumps(value), encoding="utf-8")

    result = build_migration_worklist_main(
        [
            "--plan",
            str(input_paths["plan"]),
            "--runtime-inventory",
            str(input_paths["runtime"]),
            "--static-catalog",
            str(input_paths["catalog"]),
            "--output",
            str(input_paths["output"]),
        ]
    )
    stdout = capsys.readouterr().out

    assert result == 0
    assert '"output_created": true' in stdout
    assert '"scheduled_task_work_item_count": 3' in stdout
    assert re.search(r"\b[0-9a-f]{64}\b", stdout) is None
    assert "worklist_sha256" not in stdout
    assert stat.S_IMODE(input_paths["output"].stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "unsafe_script_id",
    [
        "https://internal.example/path",
        "internal.example",
        "127.0.0.1",
        "db:3306/legacy",
        "private/secret",
        "/etc/private-script.py",
    ],
)
def test_worklist_rejects_url_host_and_path_shaped_script_ids(unsafe_script_id: str) -> None:
    """Reject URL, host, and path syntax before retaining an identifier."""
    runtime, catalog = _source_inputs()
    _replace_script_id(runtime, "shared_script", unsafe_script_id)

    if unsafe_script_id.startswith("/"):
        with pytest.raises(MigrationPlanError, match="runtime_script_id_invalid"):
            build_migration_plan(runtime, catalog)
        return

    plan = build_migration_plan(runtime, catalog)
    with pytest.raises(MigrationWorklistError, match="script_id_invalid"):
        build_migration_worklist(plan, runtime, catalog)


def test_worklist_rejects_tampered_or_stale_plans() -> None:
    """Require both the planner self-hash and exact reconstruction from inputs."""
    plan, runtime, catalog = _plan_with_sources()
    tampered = deepcopy(plan)
    rows = tampered["tasks"]
    assert isinstance(rows, list)
    rows[0]["script_sha256"] = "f" * 64

    with pytest.raises(MigrationWorklistError, match="migration_plan_sha256_invalid"):
        build_migration_worklist(tampered, runtime, catalog)

    _rehash_plan(tampered)
    with pytest.raises(MigrationWorklistError, match="plan_does_not_match_pinned_sources"):
        build_migration_worklist(tampered, runtime, catalog)

    changed_runtime = deepcopy(runtime)
    changed_runtime["generated_at_utc"] = "2026-10-27T00:00:00+00:00"
    with pytest.raises(MigrationWorklistError, match="plan_does_not_match_pinned_sources"):
        build_migration_worklist(plan, changed_runtime, catalog)


def test_worklist_rejects_duplicate_source_task_ids() -> None:
    """Fail closed when a rehashed plan's source inventory has duplicate task IDs."""
    runtime, catalog = _source_inputs()
    metadata = runtime["metadata"]["scheduled_tasks"]
    duplicate = deepcopy(metadata["records"][0])
    metadata["records"].append(duplicate)
    metadata["row_count_exact"] += 1
    runtime["counts"]["scheduled_task_rows_exact"] += 1
    runtime["tasks"].append(deepcopy(runtime["tasks"][0]))
    plan = build_migration_plan(runtime, catalog)

    with pytest.raises(MigrationWorklistError, match="runtime_static_join_integrity_failed"):
        build_migration_worklist(plan, runtime, catalog)


def test_private_writer_creates_new_file_and_rejects_overwrite_and_symlinks(
    tmp_path: Path,
) -> None:
    """Use the planner's atomic private writer and refuse unsafe output paths."""
    plan, runtime, catalog = _plan_with_sources()
    worklist = build_migration_worklist(plan, runtime, catalog)
    output = tmp_path / "worklist.json"
    roots = [tmp_path / "protected"]

    written = write_migration_worklist_new_file(
        output,
        worklist,
        input_paths=[tmp_path / "plan.json"],
        protected_roots=roots,
    )

    assert written == output
    assert json.loads(output.read_text(encoding="utf-8")) == worklist
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with pytest.raises(MigrationPlanError, match="output_exists_refused"):
        write_migration_worklist_new_file(
            output,
            worklist,
            input_paths=[tmp_path / "plan.json"],
            protected_roots=roots,
        )

    link_parent = tmp_path / "linked-reports"
    try:
        link_parent.symlink_to(tmp_path, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {type(exc).__name__}")
    with pytest.raises(MigrationPlanError, match="output_symlink_refused"):
        write_migration_worklist_new_file(
            link_parent / "linked-worklist.json",
            worklist,
            input_paths=[tmp_path / "plan.json"],
            protected_roots=roots,
        )
