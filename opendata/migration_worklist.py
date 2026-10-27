"""Create a redacted, deterministic review queue from a verified migration plan."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

from opendata.migration_plan import (
    MAX_PLAN_BYTES,
    MigrationPlanError,
    build_migration_plan,
    canonical_json_bytes,
    canonical_json_sha256,
    write_plan_new_file,
)

WORKLIST_SCHEMA_VERSION = "1.0"
LANE_COUNT = 5
LANE_ALGORITHM = "sha256(afi204-worklist-lane-v1-nul+script_id_utf8)[:8]-big-endian-mod-5-plus-1-v1"
LANE_HASH_DOMAIN = b"afi204-worklist-lane-v1\0"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TASK_ID_PATTERN = re.compile(r"^scheduled:(?:0|[1-9][0-9]{0,17})$")
_SCRIPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_]{1,128}$")
_SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"bearer\s+\S+|mysql(?:\+\w+)?://[^\s/:]+:[^\s@]+@|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_STATIC_STATUSES = frozenset({"MISSING_PROVIDER", "REQUIRES_MANUAL", "READY_FOR_ADAPTER"})


class MigrationWorklistError(ValueError):
    """Raised when a plan cannot safely produce a migration review worklist."""


def build_migration_worklist(
    plan: Mapping[str, object],
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
) -> dict[str, object]:
    """Build an unassigned review queue from a plan rebuilt against pinned inputs.

    The plan must be the exact offline plan produced from ``runtime_inventory``
    and ``static_catalog`` without a reviewed mapping or target allowlist. The
    output contains only stable task/script identifiers, source-file SHA-256
    evidence, planner status codes, and review-lane metadata. It never copies
    source paths, schedules, endpoint details, or parameter fields. This is a
    local-private artifact only: its identifiers and hashes must not be
    distributed, and its digests bind content without authenticating or
    authorizing anyone.

    Args:
        plan: Serialized output of :func:`build_migration_plan`.
        runtime_inventory: The captured runtime snapshot used to build the plan.
        static_catalog: The static source catalog used to build the plan.

    Returns:
        A deterministic JSON-compatible, disabled-only worklist.

    Raises:
        MigrationWorklistError: If the plan, source pins, or row identities fail
            validation.
    """
    verified_plan = _verify_plan_against_sources(plan, runtime_inventory, static_catalog)
    summary = _mapping(verified_plan.get("summary"), "plan_summary")
    plan_inputs = _mapping(verified_plan.get("inputs"), "plan_inputs")
    scheduled_rows = _mapping_rows(verified_plan.get("tasks"), "plan_tasks")
    helper_rows = _mapping_rows(verified_plan.get("helpers"), "plan_helpers")
    captured_at = _normalize_utc_timestamp(runtime_inventory.get("generated_at_utc"))

    expected_tasks = _count(summary.get("scheduled_task_count"), "scheduled_task_count")
    expected_helpers = _count(
        summary.get("non_scheduled_helper_count"), "non_scheduled_helper_count"
    )
    if len(scheduled_rows) != expected_tasks:
        raise MigrationWorklistError("scheduled_task_count_mismatch")
    if len(helper_rows) != expected_helpers:
        raise MigrationWorklistError("helper_count_mismatch")
    if _count(summary.get("mapping_row_count"), "mapping_row_count") != 0:
        raise MigrationWorklistError("reviewed_mapping_not_allowed_for_initial_worklist")
    if _count(summary.get("runtime_static_anti_join_issue_count"), "anti_join_issue_count") != 0:
        raise MigrationWorklistError("runtime_static_join_integrity_failed")

    scheduled_items: list[dict[str, object]] = []
    helper_items: list[dict[str, object]] = []
    seen_task_ids: set[str] = set()
    scheduled_script_ids: set[str] = set()
    helper_script_ids: set[str] = set()
    lane_counts: Counter[str] = Counter()
    lane_task_counts: Counter[str] = Counter()
    lane_helper_counts: Counter[str] = Counter()

    for row in scheduled_rows:
        item = _project_scheduled_row(row, seen_task_ids)
        script_id = str(item["script_id"])
        scheduled_script_ids.add(script_id)
        lane = str(item["review_lane"])
        lane_counts[lane] += 1
        lane_task_counts[lane] += 1
        scheduled_items.append(item)

    for row in helper_rows:
        item = _project_helper_row(row, seen_task_ids, scheduled_script_ids)
        script_id = str(item["script_id"])
        helper_script_ids.add(script_id)
        lane = str(item["review_lane"])
        lane_counts[lane] += 1
        lane_helper_counts[lane] += 1
        helper_items.append(item)

    if scheduled_script_ids & helper_script_ids:
        raise MigrationWorklistError("scheduled_helper_script_overlap")
    registry_count = _count(summary.get("script_registry_count"), "script_registry_count")
    if len(scheduled_script_ids | helper_script_ids) != registry_count:
        raise MigrationWorklistError("script_registry_anti_join_mismatch")

    scheduled_items.sort(key=lambda item: _task_id_sort_key(str(item["task_id"])))
    helper_items.sort(key=lambda item: str(item["task_id"]))
    lanes = [
        {
            "lane_id": _lane_id(index),
            "scheduled_task_count": lane_task_counts[_lane_id(index)],
            "registry_helper_count": lane_helper_counts[_lane_id(index)],
            "work_item_count": lane_counts[_lane_id(index)],
        }
        for index in range(1, LANE_COUNT + 1)
    ]

    unsigned_worklist: dict[str, object] = {
        "schema_version": WORKLIST_SCHEMA_VERSION,
        "worklist_type": "AFI204_MIGRATION_REVIEW_QUEUE",
        "queue_status": "HISTORICAL_SNAPSHOT_PENDING_REVIEW",
        "freshness_status": "REQUIRES_REFRESH_BEFORE_EXECUTION",
        "source_snapshot_generated_at_utc": captured_at,
        "source_plan_sha256": _require_sha256(verified_plan.get("plan_sha256"), "plan_sha256"),
        "security_boundary": {
            "classification": "LOCAL_PRIVATE_ONLY",
            "file_mode_octal": "0600",
            "distribution_status": "NOT_FOR_DISTRIBUTION",
            "hash_semantics": "CONTENT_BINDING_ONLY_NOT_AUTHENTICATION_OR_AUTHORIZATION",
        },
        "inputs": {
            "runtime_inventory_sha256": _require_sha256(
                plan_inputs.get("runtime_inventory_sha256"), "runtime_inventory_sha256"
            ),
            "static_catalog_sha256": _require_sha256(
                plan_inputs.get("static_catalog_sha256"), "static_catalog_sha256"
            ),
        },
        "lane_assignment": {
            "algorithm": LANE_ALGORITHM,
            "lane_count": LANE_COUNT,
            "group_key": "script_id",
            "assignment_is_ownership": False,
        },
        "scope": {
            "scheduled_tasks": "VERIFIED_PLAN_TASK_ROWS_ONLY",
            "registered_helpers": "SEPARATE_UNSCHEDULED_REVIEW_QUEUE",
            "non_task_dependencies": "NOT_ASSESSED",
            "migration_completeness": "NOT_CLAIMED",
        },
        "summary": {
            "scheduled_task_work_item_count": len(scheduled_items),
            "registry_helper_work_item_count": len(helper_items),
            "unassigned_work_item_count": len(scheduled_items) + len(helper_items),
        },
        "lanes": lanes,
        "scheduled_tasks": scheduled_items,
        "registry_helpers": helper_items,
    }
    unsigned_bytes = canonical_json_bytes(unsigned_worklist)
    if len(unsigned_bytes) > MAX_PLAN_BYTES:
        raise MigrationWorklistError("migration_worklist_exceeds_size_limit")
    worklist = dict(unsigned_worklist)
    worklist["worklist_sha256"] = hashlib.sha256(unsigned_bytes).hexdigest()
    if len(canonical_json_bytes(worklist)) > MAX_PLAN_BYTES:
        raise MigrationWorklistError("migration_worklist_exceeds_size_limit")
    return worklist


def render_migration_worklist(worklist: Mapping[str, object]) -> str:
    """Render a local-private worklist as stable, bounded, human-readable JSON."""
    _validate_worklist_projection(worklist)
    digest = worklist.get("worklist_sha256")
    unsigned = {key: value for key, value in worklist.items() if key != "worklist_sha256"}
    if not _is_sha256(digest) or canonical_json_sha256(unsigned) != digest:
        raise MigrationWorklistError("migration_worklist_sha256_invalid")
    try:
        output = (
            json.dumps(
                worklist,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise MigrationWorklistError("migration_worklist_not_canonical_json") from exc
    if len(output.encode("utf-8")) > MAX_PLAN_BYTES:
        raise MigrationWorklistError("migration_worklist_exceeds_size_limit")
    return output


def _validate_worklist_projection(worklist: Mapping[str, object]) -> None:
    """Reject non-allowlisted or internally inconsistent output before writing."""
    document = _mapping(worklist, "worklist")
    expected_fields = {
        "schema_version",
        "worklist_type",
        "queue_status",
        "freshness_status",
        "source_snapshot_generated_at_utc",
        "source_plan_sha256",
        "security_boundary",
        "inputs",
        "lane_assignment",
        "scope",
        "summary",
        "lanes",
        "scheduled_tasks",
        "registry_helpers",
        "worklist_sha256",
    }
    if set(document) != expected_fields:
        raise MigrationWorklistError("worklist_projection_fields_invalid")
    expected_constants = {
        "schema_version": WORKLIST_SCHEMA_VERSION,
        "worklist_type": "AFI204_MIGRATION_REVIEW_QUEUE",
        "queue_status": "HISTORICAL_SNAPSHOT_PENDING_REVIEW",
        "freshness_status": "REQUIRES_REFRESH_BEFORE_EXECUTION",
    }
    if any(document.get(field) != value for field, value in expected_constants.items()):
        raise MigrationWorklistError("worklist_projection_metadata_invalid")
    timestamp = _normalize_utc_timestamp(document.get("source_snapshot_generated_at_utc"))
    if document.get("source_snapshot_generated_at_utc") != timestamp:
        raise MigrationWorklistError("worklist_snapshot_timestamp_not_canonical")
    _require_sha256(document.get("source_plan_sha256"), "source_plan_sha256")

    security_boundary = _mapping(document.get("security_boundary"), "security_boundary")
    if security_boundary != {
        "classification": "LOCAL_PRIVATE_ONLY",
        "file_mode_octal": "0600",
        "distribution_status": "NOT_FOR_DISTRIBUTION",
        "hash_semantics": "CONTENT_BINDING_ONLY_NOT_AUTHENTICATION_OR_AUTHORIZATION",
    }:
        raise MigrationWorklistError("security_boundary_metadata_invalid")

    inputs = _mapping(document.get("inputs"), "worklist_inputs")
    if set(inputs) != {"runtime_inventory_sha256", "static_catalog_sha256"}:
        raise MigrationWorklistError("worklist_input_projection_invalid")
    for name in inputs:
        _require_sha256(inputs.get(name), name)

    lane_assignment = _mapping(document.get("lane_assignment"), "lane_assignment")
    if set(lane_assignment) != {
        "algorithm",
        "lane_count",
        "group_key",
        "assignment_is_ownership",
    } or lane_assignment != {
        "algorithm": LANE_ALGORITHM,
        "lane_count": LANE_COUNT,
        "group_key": "script_id",
        "assignment_is_ownership": False,
    }:
        raise MigrationWorklistError("lane_assignment_metadata_invalid")
    scope = _mapping(document.get("scope"), "worklist_scope")
    if scope != {
        "scheduled_tasks": "VERIFIED_PLAN_TASK_ROWS_ONLY",
        "registered_helpers": "SEPARATE_UNSCHEDULED_REVIEW_QUEUE",
        "non_task_dependencies": "NOT_ASSESSED",
        "migration_completeness": "NOT_CLAIMED",
    }:
        raise MigrationWorklistError("worklist_scope_metadata_invalid")

    scheduled_raw = document.get("scheduled_tasks")
    helpers_raw = document.get("registry_helpers")
    lanes_raw = document.get("lanes")
    if not isinstance(scheduled_raw, list) or not isinstance(helpers_raw, list):
        raise MigrationWorklistError("worklist_item_lists_invalid")
    if not isinstance(lanes_raw, list) or len(lanes_raw) != LANE_COUNT:
        raise MigrationWorklistError("worklist_lanes_invalid")

    seen_task_ids: set[str] = set()
    scheduled_script_ids: set[str] = set()
    helper_script_ids: set[str] = set()
    lane_task_counts: Counter[str] = Counter()
    lane_helper_counts: Counter[str] = Counter()
    for row_value in scheduled_raw:
        row = _mapping(row_value, "worklist_scheduled_row")
        if set(row) != {
            "task_id",
            "script_id",
            "source_sha256",
            "static_status",
            "planner_blocker_count",
            "review_lane",
            "assignment_status",
            "assignee",
            "review_status",
            "enabled",
        }:
            raise MigrationWorklistError("scheduled_worklist_projection_fields_invalid")
        task_id = row.get("task_id")
        if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
            raise MigrationWorklistError("scheduled_worklist_task_id_invalid")
        _record_unique_task_id(task_id, seen_task_ids)
        script_id = _require_script_id(row.get("script_id"))
        _require_sha256(row.get("source_sha256"), "source_sha256")
        _static_status(row.get("static_status"))
        _count(row.get("planner_blocker_count"), "planner_blocker_count")
        _validate_unassigned_row(row, script_id)
        scheduled_script_ids.add(script_id)
        lane_task_counts[_review_lane(script_id)] += 1
    scheduled_order = sorted(
        scheduled_raw, key=lambda item: _task_id_sort_key(str(item["task_id"]))
    )
    if scheduled_raw != scheduled_order:
        raise MigrationWorklistError("scheduled_worklist_order_invalid")

    for row_value in helpers_raw:
        row = _mapping(row_value, "worklist_helper_row")
        if set(row) != {
            "task_id",
            "script_id",
            "source_sha256",
            "static_status",
            "work_item_type",
            "review_lane",
            "assignment_status",
            "assignee",
            "review_status",
            "enabled",
        }:
            raise MigrationWorklistError("helper_worklist_projection_fields_invalid")
        script_id = _require_script_id(row.get("script_id"))
        task_id = row.get("task_id")
        if task_id != f"script:{script_id}":
            raise MigrationWorklistError("helper_worklist_task_id_invalid")
        _record_unique_task_id(str(task_id), seen_task_ids)
        _require_sha256(row.get("source_sha256"), "source_sha256")
        _static_status(row.get("static_status"))
        if row.get("work_item_type") != "UNSCHEDULED_REGISTRY_HELPER":
            raise MigrationWorklistError("helper_worklist_type_invalid")
        _validate_unassigned_row(row, script_id)
        helper_script_ids.add(script_id)
        lane_helper_counts[_review_lane(script_id)] += 1
    if helpers_raw != sorted(helpers_raw, key=lambda item: str(item["task_id"])):
        raise MigrationWorklistError("helper_worklist_order_invalid")
    if scheduled_script_ids & helper_script_ids:
        raise MigrationWorklistError("scheduled_helper_script_overlap")

    summary = _mapping(document.get("summary"), "worklist_summary")
    expected_summary = {
        "scheduled_task_work_item_count": len(scheduled_raw),
        "registry_helper_work_item_count": len(helpers_raw),
        "unassigned_work_item_count": len(scheduled_raw) + len(helpers_raw),
    }
    if summary != expected_summary:
        raise MigrationWorklistError("worklist_summary_mismatch")
    expected_lanes = [
        {
            "lane_id": _lane_id(index),
            "scheduled_task_count": lane_task_counts[_lane_id(index)],
            "registry_helper_count": lane_helper_counts[_lane_id(index)],
            "work_item_count": lane_task_counts[_lane_id(index)]
            + lane_helper_counts[_lane_id(index)],
        }
        for index in range(1, LANE_COUNT + 1)
    ]
    normalized_lanes = [_mapping(row, "worklist_lane_row") for row in lanes_raw]
    if (
        any(
            set(row)
            != {"lane_id", "scheduled_task_count", "registry_helper_count", "work_item_count"}
            for row in normalized_lanes
        )
        or normalized_lanes != expected_lanes
    ):
        raise MigrationWorklistError("worklist_lane_counts_mismatch")


def _validate_unassigned_row(row: Mapping[str, object], script_id: str) -> None:
    """Require deterministic grouping and the explicit human-review gate."""
    if (
        row.get("review_lane") != _review_lane(script_id)
        or row.get("assignment_status") != "UNASSIGNED"
        or row.get("assignee") is not None
        or row.get("review_status") != "PENDING"
        or row.get("enabled") is not False
    ):
        raise MigrationWorklistError("worklist_item_assignment_state_invalid")


def write_migration_worklist_new_file(
    output: Path,
    worklist: Mapping[str, object],
    *,
    input_paths: Sequence[Path],
    protected_roots: Sequence[Path],
) -> Path:
    """Create a new local-private mode-0600 file without overwrite or symlinks.

    The generated worklist is not a distributable task board. Its SHA-256
    fields bind source content but do not authenticate identity or grant
    approval.
    """
    return write_plan_new_file(
        output,
        render_migration_worklist(worklist),
        input_paths=input_paths,
        protected_roots=protected_roots,
    )


def _verify_plan_against_sources(
    plan: Mapping[str, object],
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
) -> Mapping[str, object]:
    """Rebuild the offline plan and compare it with its self-hashed input."""
    _verify_plan_self_hash(plan)
    try:
        expected = build_migration_plan(runtime_inventory, static_catalog)
        if canonical_json_bytes(plan) != canonical_json_bytes(expected):
            raise MigrationWorklistError("plan_does_not_match_pinned_sources")
    except MigrationPlanError as exc:
        raise MigrationWorklistError("plan_source_rebuild_failed") from exc
    if (
        expected.get("schema_version") != "1.0"
        or expected.get("plan_mode") != "OFFLINE_DRY_RUN"
        or expected.get("execution_supported") is not False
        or expected.get("enabled_jobs_created") is not False
        or expected.get("plan_status") != "BLOCKED"
    ):
        raise MigrationWorklistError("plan_not_an_unreviewed_disabled_dry_run")
    inputs = _mapping(expected.get("inputs"), "plan_inputs")
    if any(
        inputs.get(name) is not None
        for name in (
            "reviewed_mapping_sha256",
            "target_allowlist_sha256",
            "target_warehouse_fingerprint_sha256",
        )
    ):
        raise MigrationWorklistError("reviewed_or_target_inputs_not_allowed")
    summary = _mapping(expected.get("summary"), "plan_summary")
    if _count(summary.get("mapping_row_count"), "mapping_row_count") != 0:
        raise MigrationWorklistError("reviewed_mapping_not_allowed_for_initial_worklist")
    if _count(summary.get("runtime_static_anti_join_issue_count"), "anti_join_issue_count") != 0:
        raise MigrationWorklistError("runtime_static_join_integrity_failed")
    integrity_issues = expected.get("integrity_issues")
    if not isinstance(integrity_issues, list) or any(
        not isinstance(issue, str) for issue in integrity_issues
    ):
        raise MigrationWorklistError("plan_integrity_issue_shape_invalid")
    if any(issue.startswith(("runtime_", "static_", "catalog_")) for issue in integrity_issues):
        raise MigrationWorklistError("runtime_static_join_integrity_failed")
    return expected


def _verify_plan_self_hash(plan: Mapping[str, object]) -> None:
    """Verify the planner's canonical hash before rebuilding its source pins."""
    digest = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    try:
        valid = _is_sha256(digest) and canonical_json_sha256(unsigned) == digest
    except MigrationPlanError as exc:
        raise MigrationWorklistError("migration_plan_sha256_invalid") from exc
    if not valid:
        raise MigrationWorklistError("migration_plan_sha256_invalid")


def _project_scheduled_row(row: Mapping[str, object], seen_task_ids: set[str]) -> dict[str, object]:
    """Return only allowed identifiers and review evidence from a scheduled row."""
    task_id = row.get("task_id")
    script_id = row.get("script_id")
    if not isinstance(task_id, str) or _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise MigrationWorklistError("scheduled_task_id_invalid")
    _record_unique_task_id(task_id, seen_task_ids)
    safe_script_id = _require_script_id(script_id)
    source_hash = _require_sha256(row.get("script_sha256"), "script_sha256")
    if row.get("enabled") is not False or row.get("disposition") != "BLOCKED":
        raise MigrationWorklistError("scheduled_plan_row_not_blocked_and_disabled")
    if any(row.get(field) is not None for field in ("target", "review", "target_schedule")):
        raise MigrationWorklistError("scheduled_plan_row_contains_reviewed_mapping")
    static_status = _static_status(row.get("static_status"))
    blocked_reasons = row.get("blocked_reasons")
    if not isinstance(blocked_reasons, list) or any(
        not isinstance(reason, str) for reason in blocked_reasons
    ):
        raise MigrationWorklistError("scheduled_blocked_reasons_invalid")
    return {
        "task_id": task_id,
        "script_id": safe_script_id,
        "source_sha256": source_hash,
        "static_status": static_status,
        "planner_blocker_count": len(blocked_reasons),
        "review_lane": _review_lane(safe_script_id),
        "assignment_status": "UNASSIGNED",
        "assignee": None,
        "review_status": "PENDING",
        "enabled": False,
    }


def _project_helper_row(
    row: Mapping[str, object],
    seen_task_ids: set[str],
    scheduled_script_ids: set[str],
) -> dict[str, object]:
    """Return one separately scoped, unassigned registry-helper review row."""
    task_id = row.get("task_id")
    script_id = _require_script_id(row.get("script_id"))
    if task_id != f"script:{script_id}":
        raise MigrationWorklistError("registry_helper_id_mismatch")
    _record_unique_task_id(str(task_id), seen_task_ids)
    if script_id in scheduled_script_ids:
        raise MigrationWorklistError("scheduled_helper_script_overlap")
    if row.get("enabled") is not False or row.get("disposition") != "NON_SCHEDULED_HELPER":
        raise MigrationWorklistError("registry_helper_disposition_invalid")
    blocked_reasons = row.get("blocked_reasons")
    if not isinstance(blocked_reasons, list) or any(
        not isinstance(reason, str) for reason in blocked_reasons
    ):
        raise MigrationWorklistError("registry_helper_blocked_reasons_invalid")
    if "not_a_scheduled_task" not in blocked_reasons:
        raise MigrationWorklistError("registry_helper_scheduled_status_missing")
    return {
        "task_id": str(task_id),
        "script_id": script_id,
        "source_sha256": _require_sha256(row.get("script_sha256"), "script_sha256"),
        "static_status": _static_status(row.get("static_status")),
        "work_item_type": "UNSCHEDULED_REGISTRY_HELPER",
        "review_lane": _review_lane(script_id),
        "assignment_status": "UNASSIGNED",
        "assignee": None,
        "review_status": "PENDING",
        "enabled": False,
    }


def _review_lane(script_id: str) -> str:
    """Assign a stable review lane from the script ID without assigning an owner."""
    digest = hashlib.sha256(LANE_HASH_DOMAIN + script_id.encode("utf-8")).digest()
    lane_number = int.from_bytes(digest[:8], byteorder="big") % LANE_COUNT + 1
    return _lane_id(lane_number)


def _lane_id(number: int) -> str:
    """Format one stable zero-padded lane identifier."""
    return f"lane-{number:02d}"


def _task_id_sort_key(task_id: str) -> tuple[int, int | str]:
    """Sort scheduled numeric IDs naturally and helpers deterministically."""
    prefix, _, suffix = task_id.partition(":")
    if prefix == "scheduled" and suffix.isdecimal():
        return 0, int(suffix)
    return 1, task_id


def _record_unique_task_id(task_id: str, seen_task_ids: set[str]) -> None:
    """Reject repeated task/helper identities instead of silently deduplicating."""
    if task_id in seen_task_ids:
        raise MigrationWorklistError("duplicate_work_item_id")
    seen_task_ids.add(task_id)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """Require a JSON-like mapping at one named boundary."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MigrationWorklistError(f"{label}_must_be_object")
    return value


def _mapping_rows(value: object, label: str) -> list[Mapping[str, object]]:
    """Require a list of object rows without accepting duplicate identities."""
    if not isinstance(value, list):
        raise MigrationWorklistError(f"{label}_must_be_object_list")
    return [_mapping(row, f"{label}_row") for row in value]


def _count(value: object, label: str) -> int:
    """Require one nonnegative exact integer count."""
    if type(value) is not int or value < 0:
        raise MigrationWorklistError(f"{label}_invalid")
    return value


def _require_sha256(value: object, label: str) -> str:
    """Require a lowercase SHA-256 digest without exposing the invalid value."""
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise MigrationWorklistError(f"{label}_invalid")
    return value


def _is_sha256(value: object) -> bool:
    """Return whether a value is a canonical lowercase SHA-256 string."""
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _require_script_id(value: object) -> str:
    """Validate a script ID before retaining it in the private worklist."""
    if (
        not isinstance(value, str)
        or _SCRIPT_ID_PATTERN.fullmatch(value) is None
        or _SECRET_VALUE_PATTERN.search(value) is not None
    ):
        raise MigrationWorklistError("script_id_invalid")
    return value


def _static_status(value: object) -> str:
    """Return only one known planner static status code."""
    if not isinstance(value, str) or value not in _STATIC_STATUSES:
        raise MigrationWorklistError("static_status_invalid")
    return value


def _normalize_utc_timestamp(value: object) -> str:
    """Validate and normalize the captured snapshot timestamp to UTC."""
    if not isinstance(value, str) or not value or len(value) > 64:
        raise MigrationWorklistError("source_snapshot_timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MigrationWorklistError("source_snapshot_timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise MigrationWorklistError("source_snapshot_timestamp_not_utc")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "LANE_ALGORITHM",
    "LANE_COUNT",
    "MigrationWorklistError",
    "WORKLIST_SCHEMA_VERSION",
    "build_migration_worklist",
    "render_migration_worklist",
    "write_migration_worklist_new_file",
]
