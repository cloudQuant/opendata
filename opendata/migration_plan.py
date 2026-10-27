"""Build a bounded, offline migration plan from pinned legacy inventories.

The planner reads JSON snapshots only. It never imports a legacy script,
resolves credentials, contacts a database, publishes data, or enables a job.
Every planned job is emitted with ``enabled=False``; this module has no apply
or execution operation.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import NoReturn
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PLAN_SCHEMA_VERSION = "1.0"
SUPPORTED_RUNTIME_SCHEMA_VERSION = "1.1"
SUPPORTED_RUNTIME_SCHEMA_VERSIONS = frozenset({"1.0", SUPPORTED_RUNTIME_SCHEMA_VERSION})
SUPPORTED_CATALOG_SCHEMA_VERSION = "1.1"
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_JSON_NODES = 1_000_000
MAX_JSON_DEPTH = 24
MAX_SCHEDULED_TASKS = 25_000
MAX_STATIC_FILES = 25_000
MAX_MAPPINGS = 25_000
MAX_PLAN_BYTES = 16 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
SCRIPT_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._:/+\-]{0,127}$")
SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"bearer\s+\S+|mysql(?:\+\w+)?://[^\s/:]+:[^\s@]+@|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
TASK_PARAMETER_HASH_SEMANTICS = "sha256(canonical-json(task_and_script_parameter_sha256_v1))"
INPUT_HASH_SEMANTICS = "sha256(canonical UTF-8 JSON with sorted keys and compact separators)"

JsonObject = dict[str, object]


class MigrationPlanError(ValueError):
    """An expected migration-plan input or output validation failure."""


@dataclass(frozen=True, slots=True)
class WarehouseFingerprint:
    """Explicit SHA-256 identity for the target warehouse schema snapshot."""

    sha256: str

    def __post_init__(self) -> None:
        """Require a canonical lowercase SHA-256 fingerprint."""
        if not SHA256_PATTERN.fullmatch(self.sha256):
            raise MigrationPlanError("warehouse_fingerprint_must_be_sha256")


def canonical_json_bytes(value: object) -> bytes:
    """Serialize JSON-compatible input deterministically with finite numbers only."""
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise MigrationPlanError("input_not_canonical_json") from exc


def canonical_json_sha256(value: object) -> str:
    """Hash the canonical JSON representation of one input snapshot."""
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def load_json_input(path: Path, *, label: str) -> tuple[JsonObject, str]:
    """Load one bounded strict-JSON object and return its semantic content hash."""
    input_path = _absolute_path(path)
    if _has_symlink_component(input_path):
        raise MigrationPlanError(f"{label}_symlink_refused")
    try:
        with input_path.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    except OSError as exc:
        raise MigrationPlanError(f"{label}_unreadable") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise MigrationPlanError(f"{label}_exceeds_size_limit")
    try:
        document = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except MigrationPlanError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise MigrationPlanError(f"{label}_not_strict_utf8_json") from exc
    _validate_json_tree(document)
    if type(document) is not dict:
        raise MigrationPlanError(f"{label}_root_must_be_object")
    return document, canonical_json_sha256(document)


def build_migration_plan(
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
    reviewed_mapping: Mapping[str, object] | None = None,
    *,
    warehouse_fingerprint: WarehouseFingerprint | None = None,
    target_allowlist: Mapping[str, object] | None = None,
) -> JsonObject:
    """Build a deterministic, fail-closed plan without enabling or applying jobs.

    ``params_sha256`` in each reviewed mapping row is defined as the SHA-256
    of canonical JSON containing the two separately retained runtime digests
    ``task_params_sha256`` and ``script_params_sha256``. Those source digests
    are already value-redacted by the runtime inventory.

    Args:
        runtime_inventory: Full read-only runtime inventory JSON object.
        static_catalog: AST-only legacy-to-provider catalog JSON object.
        reviewed_mapping: Optional human-reviewed, hash-pinned per-task mapping.
        warehouse_fingerprint: Explicit fingerprint of the inspected target schema.
        target_allowlist: Explicit target identities and approved fingerprints.

    Returns:
        JSON-compatible deterministic dry-run plan with a canonical plan hash.

    Raises:
        MigrationPlanError: If an input is malformed, unsafe, or exceeds bounds.
    """
    _require_object(runtime_inventory, "runtime_inventory")
    _require_object(static_catalog, "static_catalog")
    if reviewed_mapping is not None:
        _require_object(reviewed_mapping, "reviewed_mapping")
    if target_allowlist is not None:
        _require_object(target_allowlist, "target_allowlist")

    inventory_hash = canonical_json_sha256(runtime_inventory)
    catalog_hash = canonical_json_sha256(static_catalog)
    mapping_hash = canonical_json_sha256(reviewed_mapping) if reviewed_mapping is not None else None
    allowlist_hash = (
        canonical_json_sha256(target_allowlist) if target_allowlist is not None else None
    )

    runtime, catalog, tasks, scripts, static_files, integrity_issues = _read_snapshots(
        runtime_inventory, static_catalog
    )
    runtime_schema_version = runtime.get("schema_version")
    runtime_identity_evidence_valid = (
        runtime_schema_version == SUPPORTED_RUNTIME_SCHEMA_VERSION
        and _runtime_v11_identity_evidence_valid(runtime)
    )
    mapping_rows, mapping_global_issues = _read_mapping(
        reviewed_mapping,
        runtime_hash=inventory_hash,
        catalog_hash=catalog_hash,
        allowlist_hash=allowlist_hash,
        expected_task_ids={str(task["task_id"]) for task in tasks},
    )
    integrity_issues.extend(mapping_global_issues)

    targets, allowed_fingerprints, allowlist_issues = _read_target_allowlist(target_allowlist)
    integrity_issues.extend(allowlist_issues)
    fingerprint_allowed = bool(
        warehouse_fingerprint is not None and warehouse_fingerprint.sha256 in allowed_fingerprints
    )
    if warehouse_fingerprint is None:
        fingerprint_issue = "target_warehouse_fingerprint_missing"
    elif not allowed_fingerprints:
        fingerprint_issue = "target_warehouse_fingerprint_allowlist_missing"
    elif not fingerprint_allowed:
        fingerprint_issue = "target_warehouse_fingerprint_not_allowlisted"
    else:
        fingerprint_issue = ""

    static_by_path = {
        str(item["source_path"]): item
        for item in static_files
        if _safe_relative_path(item.get("source_path"))
    }
    registered_scripts = {str(item["script_id"]): item for item in scripts}
    provider_models = _provider_model_index(catalog)
    global_blockers = sorted(set(integrity_issues))
    jobs: list[JsonObject] = []
    for task in tasks:
        task_id = str(task["task_id"])
        script_path = str(task["script_path"])
        script = registered_scripts.get(str(task.get("script_id")))
        static_file = static_by_path.get(script_path)
        raw_metadata = task.get("task_metadata")
        task_metadata = raw_metadata if isinstance(raw_metadata, Mapping) else None
        reasons: set[str] = set(global_blockers)
        if fingerprint_issue:
            reasons.add(fingerprint_issue)
        if task_metadata is None:
            reasons.add("runtime_task_metadata_missing")
        if script is None:
            reasons.add("runtime_script_registration_missing")
        if task.get("path_status") != "PATH_CANDIDATE" or script is None:
            reasons.add("runtime_script_path_unresolved")
        if static_file is None:
            reasons.add("static_catalog_path_missing")
        else:
            status = static_file.get("status")
            if status == "MISSING_PROVIDER":
                reasons.add("static_missing_provider")
            elif status == "REQUIRES_MANUAL":
                reasons.add("static_requires_manual")
            elif status != "READY_FOR_ADAPTER":
                reasons.add("static_status_unknown")
        task_parameters = _parameter_digest(task_metadata)
        script_parameters = _parameter_digest(script)
        if task_parameters is None:
            reasons.add("runtime_task_params_digest_missing")
        if script_parameters is None:
            reasons.add("runtime_script_params_digest_missing")

        mapping_row = mapping_rows.get(task_id)
        target_summary: JsonObject | None = None
        review_summary: JsonObject | None = None
        schedule_summary: JsonObject | None = None
        if mapping_row is None:
            reasons.add("reviewed_mapping_missing")
            reasons.add("mapping_review_missing")
            reasons.add("target_mapping_missing")
        else:
            row_reasons, target_summary, review_summary, schedule_summary = _validate_task_mapping(
                mapping_row,
                task=task,
                task_metadata=task_metadata,
                script=script,
                static_file=static_file,
                task_params_sha256=task_parameters,
                script_params_sha256=script_parameters,
                allowed_targets=targets,
                provider_models=provider_models,
                fingerprint_allowed=fingerprint_allowed,
                allowlist_present=target_allowlist is not None,
            )
            reasons.update(row_reasons)

        runtime_active = task.get("runtime_is_active")
        if type(runtime_active) is not int or runtime_active not in (0, 1):
            reasons.add("runtime_task_active_state_invalid")
        if task_metadata is not None and not _safe_schedule_metadata(task_metadata):
            reasons.add("runtime_schedule_metadata_invalid")
        legacy_schedule: JsonObject = {
            "schedule_type": _safe_schedule_type(task_metadata.get("schedule_type"))
            if task_metadata
            else None,
            "expression": _safe_schedule_expression(task_metadata.get("expression"))
            if task_metadata
            else None,
            "runtime_active": runtime_active == 1 if type(runtime_active) is int else None,
        }
        job: JsonObject = {
            "task_id": task_id,
            "script_id": task.get("script_id") if _valid_script_id(task.get("script_id")) else None,
            "script_path": script_path if _safe_relative_path(script_path) else None,
            "script_sha256": (
                static_file.get("source_sha256")
                if static_file and _is_sha256(static_file.get("source_sha256"))
                else None
            ),
            "task_params_sha256": task_parameters,
            "script_params_sha256": script_parameters,
            "params_sha256_semantics": TASK_PARAMETER_HASH_SEMANTICS,
            "params_sha256": _combined_parameter_digest(task_parameters, script_parameters),
            "legacy_schedule": legacy_schedule,
            "static_status": _safe_static_status(static_file.get("status"))
            if static_file
            else "UNKNOWN",
            "target": target_summary,
            "review": review_summary,
            "target_schedule": schedule_summary,
            "execution_governance": _execution_governance(mapping_row),
            "enabled": False,
            "disposition": "BLOCKED" if reasons else "READY_DISABLED",
            "blocked_reasons": sorted(reasons),
        }
        jobs.append(job)

    helpers = _build_helper_dispositions(tasks, scripts, static_by_path, mapping_rows)
    reason_counts = Counter(
        reason for item in jobs for reason in _string_list(item.get("blocked_reasons"))
    )
    ready_count = sum(item["disposition"] == "READY_DISABLED" for item in jobs)
    blocked_count = len(jobs) - ready_count
    scheduled_tasks_with_mapping = sum(str(task["task_id"]) in mapping_rows for task in tasks)
    summary: JsonObject = {
        "scheduled_task_count": len(tasks),
        "mapping_row_count": len(mapping_rows),
        "scheduled_tasks_with_mapping": scheduled_tasks_with_mapping,
        "missing_mapping_count": len(tasks) - scheduled_tasks_with_mapping,
        "blocked_task_count": blocked_count,
        "ready_disabled_task_count": ready_count,
        "script_registry_count": len(scripts),
        "static_catalog_file_count": len(static_files),
        "non_scheduled_helper_count": len(helpers),
        "task_blocked_reason_counts": dict(sorted(reason_counts.items())),
        "runtime_static_anti_join_issue_count": sum(
            issue.startswith(("runtime_", "static_", "catalog_")) for issue in global_blockers
        ),
    }
    plan_inputs: JsonObject = {
        "runtime_inventory_sha256": inventory_hash,
        "runtime_inventory_schema_version": runtime.get("schema_version"),
        "static_catalog_sha256": catalog_hash,
        "static_catalog_schema_version": catalog.get("schema_version"),
        "reviewed_mapping_sha256": mapping_hash,
        "target_allowlist_sha256": allowlist_hash,
        "target_warehouse_fingerprint_sha256": (
            warehouse_fingerprint.sha256 if warehouse_fingerprint is not None else None
        ),
        "target_warehouse_fingerprint_allowlisted": fingerprint_allowed,
        "hash_semantics": INPUT_HASH_SEMANTICS,
    }
    if runtime_schema_version == SUPPORTED_RUNTIME_SCHEMA_VERSION:
        plan_inputs["runtime_inventory_identity_evidence_status"] = (
            "STRUCTURALLY_PRESENT_UNVERIFIED"
            if runtime_identity_evidence_valid
            else "INVALID_OR_INCOMPLETE"
        )

    plan: JsonObject = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "plan_mode": "OFFLINE_DRY_RUN",
        "plan_status": "BLOCKED" if global_blockers or blocked_count else "READY_DISABLED",
        "execution_supported": False,
        "enabled_jobs_created": False,
        "parameter_hash_semantics": TASK_PARAMETER_HASH_SEMANTICS,
        "inputs": plan_inputs,
        "integrity_issues": global_blockers,
        "summary": summary,
        "tasks": jobs,
        "helpers": helpers,
    }
    rendered = canonical_json_bytes(plan)
    if len(rendered) > MAX_PLAN_BYTES:
        raise MigrationPlanError("migration_plan_exceeds_size_limit")
    plan["plan_sha256"] = hashlib.sha256(rendered).hexdigest()
    final_bytes = canonical_json_bytes(plan)
    if len(final_bytes) > MAX_PLAN_BYTES:
        raise MigrationPlanError("migration_plan_exceeds_size_limit")
    return plan


def render_plan(plan: Mapping[str, object]) -> str:
    """Render a bounded plan as stable, human-readable JSON."""
    output = (
        json.dumps(
            plan,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )
    encoded = output.encode("utf-8")
    if len(encoded) > MAX_PLAN_BYTES:
        raise MigrationPlanError("migration_plan_exceeds_size_limit")
    return output


def write_plan_new_file(
    output: Path,
    payload: str,
    *,
    input_paths: Sequence[Path],
    protected_roots: Sequence[Path],
) -> Path:
    """Atomically create a new private plan file without overwriting sources."""
    target = _absolute_path(output)
    if _has_symlink_component(target):
        raise MigrationPlanError("output_symlink_refused")
    if target.name.casefold().startswith(".env"):
        raise MigrationPlanError("environment_file_output_refused")
    if not target.parent.is_dir():
        raise MigrationPlanError("output_parent_directory_missing")
    target_resolved = target.parent.resolve() / target.name
    protected = {_absolute_path(path).resolve() for path in input_paths}
    if target_resolved in protected:
        raise MigrationPlanError("output_aliases_input_file")
    for root in protected_roots:
        resolved_root = _absolute_path(root).resolve()
        if target_resolved == resolved_root or resolved_root in target_resolved.parents:
            raise MigrationPlanError("output_inside_protected_source_root")
    if target.exists():
        raise MigrationPlanError("output_exists_refused")
    data = payload.encode("utf-8")
    if len(data) > MAX_PLAN_BYTES:
        raise MigrationPlanError("migration_plan_exceeds_size_limit")
    descriptor = -1
    temporary_name = ""
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".afi204-plan-", suffix=".tmp", dir=target.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary_name, target, follow_symlinks=False)
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except FileExistsError as exc:
        raise MigrationPlanError("output_exists_refused") from exc
    except OSError as exc:
        raise MigrationPlanError("output_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)
    return target


def _runtime_v11_identity_evidence_valid(runtime_inventory: Mapping[str, object]) -> bool:
    """Check the declared v1.1 identity evidence shape without authenticating it.

    The inventory is an operator-provided JSON snapshot. These checks prevent a
    v1.1 report from silently falling back to the less-specific v1.0 contract;
    they do not prove that the report or the database identities are genuine.
    """
    if runtime_inventory.get("schema_version") != SUPPORTED_RUNTIME_SCHEMA_VERSION:
        return False
    if not _is_sha256(runtime_inventory.get("identity_pin_sha256")):
        return False

    readonly = runtime_inventory.get("readonly_transaction")
    if not isinstance(readonly, Mapping) or any(
        readonly.get(key) is not True for key in ("metadata_confirmed", "warehouse_confirmed")
    ):
        return False

    endpoints = runtime_inventory.get("endpoints")
    if not isinstance(endpoints, Mapping) or set(endpoints) != {"metadata", "warehouse"}:
        return False
    expected_databases = {
        "metadata": "ai_for_investor",
        "warehouse": "akshare_data",
    }
    endpoint_fields = {
        "requested_database",
        "observed_database",
        "endpoint_fingerprint",
        "read_only_confirmed",
        "snapshot_isolation",
        "snapshot_captured_at_utc",
        "server_identity",
        "server_uuid_pin_matched",
    }
    identity_fields = {
        "database_name",
        "server_uuid",
        "server_hostname",
        "server_port",
        "server_version",
        "server_version_comment",
    }
    for role, expected_database in expected_databases.items():
        endpoint = endpoints.get(role)
        if not isinstance(endpoint, Mapping) or set(endpoint) != endpoint_fields:
            return False
        if (
            endpoint.get("requested_database") != expected_database
            or endpoint.get("observed_database") != expected_database
            or not _is_sha256(endpoint.get("endpoint_fingerprint"))
            or endpoint.get("read_only_confirmed") is not True
            or endpoint.get("snapshot_isolation") != "repeatable-read"
            or not _is_utc_timestamp(endpoint.get("snapshot_captured_at_utc"))
            or endpoint.get("server_uuid_pin_matched") is not True
        ):
            return False
        identity = endpoint.get("server_identity")
        if not isinstance(identity, Mapping) or set(identity) != identity_fields:
            return False
        server_uuid = identity.get("server_uuid")
        try:
            canonical_server_uuid = isinstance(server_uuid, str) and str(UUID(server_uuid)) == (
                server_uuid
            )
        except (AttributeError, TypeError, ValueError):
            canonical_server_uuid = False
        server_port = identity.get("server_port")
        version_comment = identity.get("server_version_comment")
        if (
            identity.get("database_name") != expected_database
            or not canonical_server_uuid
            or not _nonempty_string(identity.get("server_hostname"), max_length=255)
            or not _valid_server_port(server_port)
            or not _nonempty_string(identity.get("server_version"), max_length=128)
            or not isinstance(version_comment, str)
            or len(version_comment) > 256
        ):
            return False

    engine_check = runtime_inventory.get("runtime_table_engine_check")
    if not isinstance(engine_check, Mapping) or set(engine_check) != {
        "status",
        "check",
        "source",
        "database",
        "tables",
    }:
        return False
    if (
        engine_check.get("status") != "VERIFIED"
        or engine_check.get("check") != "BOTH_SCHEDULER_TABLES_ARE_BASE_INNODB"
        or engine_check.get("source") != "information_schema.TABLES"
        or engine_check.get("database") != "ai_for_investor"
    ):
        return False
    tables = engine_check.get("tables")
    if not isinstance(tables, Mapping) or set(tables) != {
        "ak_scheduled_tasks",
        "ak_data_scripts",
    }:
        return False
    for table in tables.values():
        if (
            not isinstance(table, Mapping)
            or set(table) != {"table_type", "engine"}
            or table.get("table_type") != "BASE TABLE"
            or not isinstance(table.get("engine"), str)
            or table["engine"].casefold() != "innodb"
        ):
            return False
    return True


def _is_utc_timestamp(value: object) -> bool:
    """Return whether one timestamp is ISO-8601 with an explicit UTC offset."""
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _nonempty_string(value: object, *, max_length: int) -> bool:
    """Check a bounded non-blank string without copying it into the plan."""
    return isinstance(value, str) and bool(value.strip()) and len(value) <= max_length


def _valid_server_port(value: object) -> bool:
    """Check a non-boolean TCP port reported as part of the MySQL identity."""
    return type(value) is int and 1 <= value <= 65535


def _read_snapshots(
    runtime_inventory: Mapping[str, object], static_catalog: Mapping[str, object]
) -> tuple[
    Mapping[str, object],
    Mapping[str, object],
    list[Mapping[str, object]],
    list[Mapping[str, object]],
    list[Mapping[str, object]],
    list[str],
]:
    """Validate input snapshot shapes and compute two-way runtime/catalog joins."""
    runtime_schema_version = runtime_inventory.get("schema_version")
    if (
        not isinstance(runtime_schema_version, str)
        or runtime_schema_version not in SUPPORTED_RUNTIME_SCHEMA_VERSIONS
    ):
        raise MigrationPlanError("unsupported_runtime_inventory_schema")
    if static_catalog.get("schema_version") != SUPPORTED_CATALOG_SCHEMA_VERSION:
        raise MigrationPlanError("unsupported_static_catalog_schema")
    tasks = _object_rows(runtime_inventory.get("tasks"), "runtime_inventory.tasks", MAX_MAPPINGS)
    metadata = _mapping(runtime_inventory.get("metadata"), "runtime_inventory.metadata")
    scheduled_section = _mapping(metadata.get("scheduled_tasks"), "runtime scheduled tasks")
    script_section = _mapping(metadata.get("data_scripts"), "runtime script registry")
    scheduled_records = _object_rows(
        scheduled_section.get("records"), "runtime scheduled task records", MAX_SCHEDULED_TASKS
    )
    script_records = _object_rows(
        script_section.get("records"), "runtime script registry records", MAX_STATIC_FILES
    )
    catalog_files = _object_rows(
        static_catalog.get("files"), "static_catalog.files", MAX_STATIC_FILES
    )
    blockers: list[str] = []
    _validate_reported_count(scheduled_section, "row_count_exact", len(scheduled_records), blockers)
    _validate_reported_count(script_section, "row_count_exact", len(script_records), blockers)
    counts = _mapping(runtime_inventory.get("counts"), "runtime inventory counts")
    _validate_count_value(counts, "scheduled_task_rows_exact", len(scheduled_records), blockers)
    _validate_count_value(counts, "data_script_rows_exact", len(script_records), blockers)
    if static_catalog.get("errors") != []:
        blockers.append("catalog_reports_errors")
    catalog_summary = _mapping(static_catalog.get("summary"), "static catalog summary")
    if catalog_summary.get("legacy_script_count") != len(catalog_files):
        blockers.append("catalog_reported_script_count_mismatch")
    source_root = runtime_inventory.get("source_root")
    catalog_root = static_catalog.get("source_root")
    if not isinstance(source_root, str) or source_root != catalog_root:
        blockers.append("runtime_static_source_root_mismatch")
    readonly = _mapping(runtime_inventory.get("readonly_transaction"), "readonly transaction")
    if (
        readonly.get("metadata_confirmed") is not True
        or readonly.get("warehouse_confirmed") is not True
    ):
        blockers.append("runtime_snapshot_not_read_only_confirmed")
    if (
        runtime_schema_version == SUPPORTED_RUNTIME_SCHEMA_VERSION
        and not _runtime_v11_identity_evidence_valid(runtime_inventory)
    ):
        blockers.append("runtime_v11_identity_evidence_invalid")

    scheduled_by_id: dict[str, Mapping[str, object]] = {}
    for record in scheduled_records:
        raw_id = record.get("id")
        if isinstance(raw_id, bool) or not (
            (isinstance(raw_id, int) and 0 <= raw_id < 10**18)
            or (isinstance(raw_id, str) and raw_id.isdecimal() and len(raw_id) <= 18)
        ):
            raise MigrationPlanError("runtime_task_id_invalid")
        task_id = f"scheduled:{raw_id}"
        if task_id in scheduled_by_id:
            blockers.append("runtime_duplicate_scheduled_task_id")
        scheduled_by_id[task_id] = record

    scripts_by_id: dict[str, Mapping[str, object]] = {}
    scripts_by_path: dict[str, Mapping[str, object]] = {}
    for record in script_records:
        script_id = record.get("script_id")
        source_path = record.get("source_path")
        if not _valid_script_id(script_id):
            raise MigrationPlanError("runtime_script_id_invalid")
        if script_id in scripts_by_id:
            blockers.append("runtime_duplicate_script_id")
        scripts_by_id[str(script_id)] = record
        if not _safe_relative_path(source_path):
            blockers.append("runtime_script_path_unresolved")
        else:
            if source_path in scripts_by_path:
                blockers.append("runtime_duplicate_script_path")
            scripts_by_path[str(source_path)] = record

    catalog_by_path: dict[str, Mapping[str, object]] = {}
    for record in catalog_files:
        path = record.get("source_path")
        digest = record.get("source_sha256")
        if not _safe_relative_path(path) or not _is_sha256(digest):
            blockers.append("catalog_source_identity_invalid")
            continue
        if path in catalog_by_path:
            blockers.append("catalog_duplicate_source_path")
        catalog_by_path[str(path)] = record
    if set(scripts_by_path) != set(catalog_by_path):
        blockers.append("runtime_static_path_anti_join_mismatch")

    task_rows_by_id: dict[str, Mapping[str, object]] = {}
    for row in tasks:
        task_map_id = row.get("task_id")
        if not isinstance(task_map_id, str) or not _valid_task_map_id(task_map_id):
            blockers.append("runtime_task_map_identity_invalid")
            continue
        if task_map_id in task_rows_by_id:
            blockers.append("runtime_duplicate_task_map_id")
        task_rows_by_id[task_map_id] = row
        if task_map_id.startswith("scheduled:") and task_map_id not in scheduled_by_id:
            blockers.append("runtime_task_map_unknown_scheduled_id")
        elif task_map_id.startswith("script:") and task_map_id[7:] not in scripts_by_id:
            blockers.append("runtime_task_map_unknown_helper_id")
        script_id = row.get("script_id")
        script = scripts_by_id.get(str(script_id)) if script_id is not None else None
        if script is None:
            blockers.append("runtime_task_map_script_reference_missing")
            continue
        if row.get("script_path") != script.get("source_path"):
            blockers.append("runtime_task_map_path_join_mismatch")
        if task_map_id.startswith("scheduled:"):
            task_meta = scheduled_by_id.get(task_map_id)
            if task_meta is None or str(task_meta.get("script_id")) != str(script_id):
                blockers.append("runtime_task_map_scheduled_script_join_mismatch")

    expected_ids = set(scheduled_by_id)
    expected_ids.update(
        f"script:{script_id}"
        for script_id in scripts_by_id
        if not any(str(record.get("script_id")) == script_id for record in scheduled_records)
    )
    if set(task_rows_by_id) != expected_ids:
        blockers.append("runtime_task_map_bidirectional_anti_join_mismatch")

    task_plan_rows: list[Mapping[str, object]] = []
    for task_map_id, task_metadata in scheduled_by_id.items():
        task_map_row = task_rows_by_id.get(task_map_id)
        if task_map_row is not None:
            plan_row: JsonObject = dict(task_map_row)
        else:
            script_id = task_metadata.get("script_id")
            registered_script = scripts_by_id.get(str(script_id)) if script_id is not None else None
            plan_row = {
                "task_id": task_map_id,
                "script_id": script_id,
                "script_path": (
                    registered_script.get("source_path") if registered_script is not None else ""
                ),
                "path_status": "UNMAPPED",
                "runtime_is_active": task_metadata.get("is_active"),
            }
        plan_row["task_id"] = task_map_id
        plan_row["script_path"] = str(plan_row.get("script_path") or "")
        plan_row["task_metadata"] = task_metadata
        task_plan_rows.append(plan_row)
    task_plan_rows.sort(key=lambda row: _task_id_sort_key(str(row["task_id"])))
    if len(task_plan_rows) != len(scheduled_records):
        blockers.append("runtime_scheduled_task_anti_join_mismatch")
    return (
        runtime_inventory,
        static_catalog,
        task_plan_rows,
        script_records,
        catalog_files,
        blockers,
    )


def _read_mapping(
    document: Mapping[str, object] | None,
    *,
    runtime_hash: str,
    catalog_hash: str,
    allowlist_hash: str | None,
    expected_task_ids: set[str],
) -> tuple[dict[str, Mapping[str, object]], list[str]]:
    """Read strict mapping pins and flag missing or extra scheduled-task rows."""
    if document is None:
        return {}, []
    if set(document) != {"schema_version", "inputs", "tasks"}:
        raise MigrationPlanError("reviewed_mapping_root_shape_invalid")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise MigrationPlanError("reviewed_mapping_schema_version_unsupported")
    pins = _mapping(document.get("inputs"), "reviewed mapping inputs")
    if set(pins) != {
        "runtime_inventory_sha256",
        "static_catalog_sha256",
        "target_allowlist_sha256",
    }:
        raise MigrationPlanError("reviewed_mapping_input_pins_invalid")
    issues: list[str] = []
    if pins.get("runtime_inventory_sha256") != runtime_hash:
        issues.append("mapping_runtime_inventory_hash_mismatch")
    if pins.get("static_catalog_sha256") != catalog_hash:
        issues.append("mapping_static_catalog_hash_mismatch")
    if pins.get("target_allowlist_sha256") != allowlist_hash:
        issues.append("mapping_target_allowlist_hash_mismatch")
    rows = _object_rows(document.get("tasks"), "reviewed mapping tasks", MAX_MAPPINGS)
    by_task: dict[str, Mapping[str, object]] = {}
    for row in rows:
        task_id = row.get("task_id")
        if not isinstance(task_id, str):
            issues.append("mapping_contains_non_scheduled_task_id")
            continue
        if task_id in by_task:
            issues.append("mapping_duplicate_task_id")
        by_task[task_id] = row
        if not task_id.startswith("scheduled:"):
            issues.append("mapping_contains_non_scheduled_task_id")
    if set(by_task) - expected_task_ids:
        issues.append("mapping_contains_unknown_task_id")
    return by_task, issues


def _read_target_allowlist(
    document: Mapping[str, object] | None,
) -> tuple[set[tuple[str, ...]], set[str], list[str]]:
    """Validate explicit dataset targets and warehouse fingerprint allowlists."""
    if document is None:
        return set(), set(), ["target_allowlist_missing"]
    if set(document) != {"schema_version", "warehouse_fingerprints", "targets"}:
        raise MigrationPlanError("target_allowlist_root_shape_invalid")
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise MigrationPlanError("target_allowlist_schema_version_unsupported")
    raw_fingerprints = document.get("warehouse_fingerprints")
    if not isinstance(raw_fingerprints, list) or len(raw_fingerprints) > 1000:
        raise MigrationPlanError("target_allowlist_fingerprints_invalid")
    fingerprints: set[str] = set()
    issues: list[str] = []
    for value in raw_fingerprints:
        if not _is_sha256(value):
            issues.append("target_allowlist_fingerprint_invalid")
        else:
            if value in fingerprints:
                issues.append("target_allowlist_duplicate_fingerprint")
            fingerprints.add(value)
    target_rows = _object_rows(document.get("targets"), "target allowlist targets", MAX_MAPPINGS)
    targets: set[tuple[str, ...]] = set()
    required = (
        "dataset_id",
        "provider",
        "domain",
        "model",
        "job_id",
        "version",
        "sink_id",
        "contract_sha256",
    )
    for row in target_rows:
        if set(row) != set(required):
            issues.append("target_allowlist_target_shape_invalid")
            continue
        values = tuple(str(row[key]) for key in required)
        if any(not _valid_identifier(row[key]) for key in required[:-1]) or not _is_sha256(
            row["contract_sha256"]
        ):
            issues.append("target_allowlist_target_identity_invalid")
            continue
        if values in targets:
            issues.append("target_allowlist_duplicate_target")
        targets.add(values)
    if not fingerprints:
        issues.append("target_warehouse_fingerprint_allowlist_missing")
    if not targets:
        issues.append("target_dataset_allowlist_missing")
    return targets, fingerprints, issues


def _validate_task_mapping(
    row: Mapping[str, object],
    *,
    task: Mapping[str, object],
    task_metadata: Mapping[str, object] | None,
    script: Mapping[str, object] | None,
    static_file: Mapping[str, object] | None,
    task_params_sha256: str | None,
    script_params_sha256: str | None,
    allowed_targets: set[tuple[str, ...]],
    provider_models: set[tuple[str, str, str]],
    fingerprint_allowed: bool,
    allowlist_present: bool,
) -> tuple[set[str], JsonObject | None, JsonObject | None, JsonObject | None]:
    """Validate one human mapping row and return safe display-only subsets."""
    required = {
        "task_id",
        "script_id",
        "script_path",
        "script_sha256",
        "task_params_sha256",
        "script_params_sha256",
        "params_sha256_semantics",
        "params_sha256",
        "dataset_id",
        "provider",
        "domain",
        "model",
        "job_id",
        "version",
        "sink_id",
        "contract_sha256",
        "schedule",
        "source_quota",
        "ownership_group",
        "shard",
        "review",
    }
    reasons: set[str] = set()
    if set(row) != required:
        reasons.add("mapping_row_shape_invalid")
    if row.get("task_id") != task.get("task_id"):
        reasons.add("mapping_task_id_mismatch")
    if row.get("script_id") != task.get("script_id"):
        reasons.add("mapping_script_id_mismatch")
    if not _valid_script_id(row.get("script_id")):
        reasons.add("mapping_script_id_invalid")
    if row.get("script_path") != task.get("script_path"):
        reasons.add("mapping_script_path_mismatch")
    if script is None:
        reasons.add("mapping_runtime_script_missing")
    else:
        if row.get("script_id") != script.get("script_id"):
            reasons.add("mapping_script_registry_id_mismatch")
        if row.get("script_path") != script.get("source_path"):
            reasons.add("mapping_script_registry_path_mismatch")
    expected_script_hash = static_file.get("source_sha256") if static_file else None
    if not _is_sha256(row.get("script_sha256")) or row.get("script_sha256") != expected_script_hash:
        reasons.add("mapping_script_hash_mismatch")
    if (
        not _is_sha256(row.get("task_params_sha256"))
        or row.get("task_params_sha256") != task_params_sha256
    ):
        reasons.add("mapping_task_params_hash_mismatch")
    if (
        not _is_sha256(row.get("script_params_sha256"))
        or row.get("script_params_sha256") != script_params_sha256
    ):
        reasons.add("mapping_script_params_hash_mismatch")
    if row.get("params_sha256_semantics") != TASK_PARAMETER_HASH_SEMANTICS:
        reasons.add("mapping_effective_params_hash_semantics_mismatch")
    combined_hash = _combined_parameter_digest(task_params_sha256, script_params_sha256)
    if not _is_sha256(row.get("params_sha256")) or row.get("params_sha256") != combined_hash:
        reasons.add("mapping_effective_params_hash_mismatch")

    identity_fields = (
        "dataset_id",
        "provider",
        "domain",
        "model",
        "job_id",
        "version",
        "sink_id",
    )
    identity_valid = all(_valid_identifier(row.get(field)) for field in identity_fields)
    contract_hash = row.get("contract_sha256")
    if not identity_valid or not _is_sha256(contract_hash):
        reasons.add("mapping_target_identity_invalid")
    target_tuple: tuple[str, ...] | None = None
    if identity_valid and _is_sha256(contract_hash):
        target_tuple = tuple(str(row[field]) for field in identity_fields) + (str(contract_hash),)
        if not allowlist_present:
            reasons.add("target_allowlist_missing")
        elif target_tuple not in allowed_targets:
            reasons.add("target_not_allowlisted")

    provider = row.get("provider")
    domain = row.get("domain")
    model = row.get("model")
    if all(_valid_identifier(value) for value in (provider, domain, model)):
        provider_tuple = (str(provider), str(domain), str(model))
        if provider_tuple not in provider_models:
            reasons.add("provider_model_not_registered")
        if static_file is not None and not _static_file_matches_provider_model(
            static_file, provider, domain, model
        ):
            reasons.add("provider_model_not_matched_by_legacy_call")

    schedule_summary: JsonObject | None = None
    if task_metadata is not None:
        schedule = row.get("schedule")
        if not isinstance(schedule, Mapping):
            reasons.add("mapping_schedule_missing")
        else:
            if set(schedule) != {"schedule_type", "expression", "timezone", "calendar"}:
                reasons.add("mapping_schedule_shape_invalid")
            if schedule.get("schedule_type") != task_metadata.get("schedule_type"):
                reasons.add("mapping_schedule_type_mismatch")
            if schedule.get("expression") != task_metadata.get("expression"):
                reasons.add("mapping_schedule_expression_mismatch")
            timezone = schedule.get("timezone")
            if not isinstance(timezone, str) or not _valid_timezone(timezone):
                reasons.add("mapping_schedule_timezone_unverified")
            if not _valid_identifier(schedule.get("calendar")):
                reasons.add("mapping_schedule_calendar_unverified")
            schedule_summary = {
                "schedule_type": _safe_schedule_type(schedule.get("schedule_type")),
                "expression": _safe_schedule_expression(schedule.get("expression")),
                "timezone": timezone
                if isinstance(timezone, str) and _valid_timezone(timezone)
                else None,
                "calendar": schedule.get("calendar")
                if _valid_identifier(schedule.get("calendar"))
                else None,
            }
    else:
        schedule_summary = None
        reasons.add("runtime_task_metadata_missing")
    for field in ("source_quota", "ownership_group", "shard"):
        if not _valid_identifier(row.get(field)):
            reasons.add(f"mapping_{field}_unverified")

    review = row.get("review")
    review_summary: JsonObject | None = None
    if not isinstance(review, Mapping):
        reasons.add("mapping_review_missing")
    else:
        if set(review) != {"status", "reviewer_id", "reviewed_at", "evidence_sha256"}:
            reasons.add("mapping_review_shape_invalid")
        reviewer = review.get("reviewer_id")
        reviewed_at = review.get("reviewed_at")
        evidence_hash = review.get("evidence_sha256")
        if review.get("status") != "APPROVED":
            reasons.add("mapping_review_not_approved")
        if not _valid_identifier(reviewer):
            reasons.add("mapping_reviewer_missing")
        if not isinstance(reviewed_at, str) or not _valid_utc_timestamp(reviewed_at):
            reasons.add("mapping_review_timestamp_invalid")
        if not _is_sha256(evidence_hash):
            reasons.add("mapping_review_evidence_missing")
        review_summary = {
            "status": review.get("status") if review.get("status") == "APPROVED" else "UNAPPROVED",
            "reviewer_id": reviewer if _valid_identifier(reviewer) else None,
            "reviewed_at": reviewed_at
            if isinstance(reviewed_at, str) and _valid_utc_timestamp(reviewed_at)
            else None,
            "evidence_sha256": evidence_hash if _is_sha256(evidence_hash) else None,
        }
    if not fingerprint_allowed:
        reasons.add("target_warehouse_fingerprint_not_verified")

    target_summary = None
    if target_tuple is not None:
        target_summary = {
            key: row[key]
            for key in (
                "dataset_id",
                "provider",
                "domain",
                "model",
                "job_id",
                "version",
                "sink_id",
                "contract_sha256",
            )
        }
    return reasons, target_summary, review_summary, schedule_summary


def _build_helper_dispositions(
    scheduled_tasks: Sequence[Mapping[str, object]],
    scripts: Sequence[Mapping[str, object]],
    static_by_path: Mapping[str, Mapping[str, object]],
    mappings: Mapping[str, Mapping[str, object]],
) -> list[JsonObject]:
    """Describe script-registry entries without scheduled tasks as helpers."""
    scheduled_script_ids = {str(row.get("script_id")) for row in scheduled_tasks}
    helpers: list[JsonObject] = []
    for script in scripts:
        script_id = str(script.get("script_id"))
        if script_id in scheduled_script_ids:
            continue
        task_id = f"script:{script_id}"
        source_path = script.get("source_path")
        static_file = static_by_path.get(str(source_path)) if isinstance(source_path, str) else None
        reasons = ["not_a_scheduled_task", "helper_migration_requires_separate_review"]
        if task_id in mappings:
            reasons.append("helper_must_not_have_scheduled_job_mapping")
        helpers.append(
            {
                "task_id": task_id,
                "script_id": script_id,
                "script_path": source_path if _safe_relative_path(source_path) else None,
                "script_sha256": (
                    static_file.get("source_sha256")
                    if static_file and _is_sha256(static_file.get("source_sha256"))
                    else None
                ),
                "static_status": _safe_static_status(static_file.get("status"))
                if static_file
                else "UNKNOWN",
                "enabled": False,
                "disposition": "NON_SCHEDULED_HELPER",
                "blocked_reasons": sorted(reasons),
            }
        )
    helpers.sort(key=lambda row: str(row["task_id"]))
    return helpers


def _provider_model_index(catalog: Mapping[str, object]) -> set[tuple[str, str, str]]:
    """Index registered provider model identities from static catalog metadata."""
    rows = _object_rows(
        catalog.get("opendata_provider_models"), "static catalog provider models", MAX_STATIC_FILES
    )
    return {
        (str(row["source"]), str(row["domain"]), str(row["model_class"]))
        for row in rows
        if row.get("registered_by_descriptor") is True
        and _valid_identifier(row.get("source"))
        and _valid_identifier(row.get("domain"))
        and _valid_identifier(row.get("model_class"))
    }


def _static_file_matches_provider_model(
    static_file: Mapping[str, object], provider: object, domain: object, model: object
) -> bool:
    """Check that AST call evidence names the reviewed provider model exactly."""
    calls = static_file.get("calls")
    if not isinstance(calls, list):
        return False
    expected = f"{provider}:{domain}:{model}"
    return any(
        isinstance(call, Mapping)
        and isinstance(call.get("target_models"), list)
        and expected in call["target_models"]
        for call in calls
    )


def _parameter_digest(record: Mapping[str, object] | None) -> str | None:
    """Read one already-redacted parameter hash from a runtime record."""
    if record is None:
        return None
    summary = record.get("parameter_summary")
    if not isinstance(summary, Mapping):
        return None
    digest = summary.get("canonical_payload_sha256")
    return digest if _is_sha256(digest) else None


def _execution_governance(mapping: Mapping[str, object] | None) -> JsonObject:
    """Return only reviewed quota, ownership, and shard identifiers."""
    return {
        field: mapping.get(field) if mapping and _valid_identifier(mapping.get(field)) else None
        for field in ("source_quota", "ownership_group", "shard")
    }


def _combined_parameter_digest(task_hash: str | None, script_hash: str | None) -> str | None:
    """Hash the named pair of source parameter digests without parameter values."""
    if not _is_sha256(task_hash) or not _is_sha256(script_hash):
        return None
    return canonical_json_sha256(
        {
            "task_params_sha256": task_hash,
            "script_params_sha256": script_hash,
            "semantics": "task_and_script_parameter_sha256_v1",
        }
    )


def _validate_reported_count(
    section: Mapping[str, object], name: str, actual: int, blockers: list[str]
) -> None:
    """Compare one embedded exact count with the number of captured rows."""
    if type(section.get(name)) is not int or section.get(name) != actual:
        blockers.append(f"runtime_{name}_mismatch")


def _validate_count_value(
    counts: Mapping[str, object], name: str, actual: int, blockers: list[str]
) -> None:
    """Compare one top-level exact count with the number of captured rows."""
    if type(counts.get(name)) is not int or counts.get(name) != actual:
        blockers.append(f"runtime_{name}_mismatch")


def _object_rows(value: object, label: str, maximum: int) -> list[Mapping[str, object]]:
    """Validate a bounded list of JSON objects."""
    if not isinstance(value, list) or len(value) > maximum:
        raise MigrationPlanError(f"{label}_invalid_or_too_large")
    return [_mapping(item, label) for item in value]


def _mapping(value: object, label: str) -> Mapping[str, object]:
    """Require a JSON object without coercing keys or values."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise MigrationPlanError(f"{label}_must_be_object")
    return value


def _require_object(value: object, label: str) -> None:
    """Ensure a public builder input is a string-keyed object."""
    _mapping(value, label)


def _valid_identifier(value: object) -> bool:
    """Return whether a value is a bounded, non-empty safe identifier."""
    return (
        isinstance(value, str)
        and IDENTIFIER_PATTERN.fullmatch(value) is not None
        and not _looks_like_secret(value)
    )


def _is_sha256(value: object) -> bool:
    """Return whether a value is a lowercase hexadecimal SHA-256 digest."""
    return isinstance(value, str) and SHA256_PATTERN.fullmatch(value) is not None


def _safe_relative_path(value: object) -> bool:
    """Reject absolute, empty, or traversing legacy source paths."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or _looks_like_secret(value)
    ):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and all(part not in ("", ".", "..") for part in path.parts)


def _valid_task_map_id(value: object) -> bool:
    """Validate scheduled and helper task-map identities without coercion."""
    if not isinstance(value, str) or len(value) > 256:
        return False
    prefix, separator, suffix = value.partition(":")
    if not separator:
        return False
    if prefix == "scheduled":
        return suffix.isdecimal() and len(suffix) <= 18
    return prefix == "script" and _valid_script_id(suffix)


def _valid_script_id(value: object) -> bool:
    """Validate legacy script registry ids, including underscore prefixes."""
    return (
        isinstance(value, str)
        and SCRIPT_IDENTIFIER_PATTERN.fullmatch(value) is not None
        and not _looks_like_secret(value)
    )


def _safe_schedule_type(value: object) -> str | None:
    """Return only short schedule-kind identifiers."""
    if not isinstance(value, str) or not value or len(value) > 64 or _looks_like_secret(value):
        return None
    return value if re.fullmatch(r"[A-Za-z0-9_.-]+", value) else None


def _safe_schedule_expression(value: object) -> str | None:
    """Return only bounded, control-free schedule expressions."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or _looks_like_secret(value)
        or any(ord(character) < 32 for character in value)
    ):
        return None
    return value


def _safe_schedule_metadata(record: Mapping[str, object]) -> bool:
    """Check source schedule fields before placing them in a plan report."""
    return (
        _safe_schedule_type(record.get("schedule_type")) is not None
        and _safe_schedule_expression(record.get("expression")) is not None
    )


def _safe_static_status(value: object) -> str:
    """Expose only known static scanner status codes."""
    if isinstance(value, str) and value in {
        "MISSING_PROVIDER",
        "REQUIRES_MANUAL",
        "READY_FOR_ADAPTER",
    }:
        return value
    return "UNKNOWN"


def _looks_like_secret(value: str) -> bool:
    """Reject common credential formats before copying user-controlled text."""
    return SECRET_VALUE_PATTERN.search(value) is not None


def _valid_timezone(value: str) -> bool:
    """Validate a schedule timezone using the installed IANA database."""
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True


def _valid_utc_timestamp(value: str) -> bool:
    """Require a strict UTC ISO-8601 review timestamp with seconds precision."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|\+00:00)", value) is None:
        return False
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None and parsed.microsecond == 0


def _task_id_sort_key(task_id: str) -> tuple[int, int | str]:
    """Sort numeric scheduled ids naturally and preserve deterministic fallback."""
    prefix, _, suffix = task_id.partition(":")
    if prefix == "scheduled" and suffix.isdecimal():
        return 0, int(suffix)
    return 1, task_id


def _string_list(value: object) -> list[str]:
    """Return only string members from an already-produced reason collection."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _unique_object(pairs: list[tuple[str, object]]) -> JsonObject:
    """Construct strict JSON objects while rejecting duplicate keys."""
    result: JsonObject = {}
    for key, value in pairs:
        if key in result:
            raise MigrationPlanError("json_duplicate_key")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    """Reject JavaScript-style non-finite numeric tokens in JSON files."""
    del value
    raise MigrationPlanError("json_non_finite_number")


def _validate_json_tree(document: object) -> None:
    """Bound parsed JSON depth, node count, and numeric finiteness."""
    stack: list[tuple[object, int]] = [(document, 0)]
    nodes = 0
    while stack:
        value, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise MigrationPlanError("json_node_limit_exceeded")
        if depth > MAX_JSON_DEPTH:
            raise MigrationPlanError("json_depth_limit_exceeded")
        if isinstance(value, Mapping):
            for key, item in value.items():
                stack.append((key, depth + 1))
                stack.append((item, depth + 1))
        elif isinstance(value, list):
            stack.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise MigrationPlanError("json_non_finite_number")
        elif value is None or isinstance(value, (str, bool, int, float)):
            continue
        else:
            raise MigrationPlanError("json_non_json_value")


def _absolute_path(path: Path) -> Path:
    """Normalize a filesystem path lexically without following its final link."""
    return Path(os.path.abspath(Path(path).expanduser()))


def _has_symlink_component(path: Path) -> bool:
    """Check every existing path component for a symbolic link."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


__all__ = [
    "MigrationPlanError",
    "PLAN_SCHEMA_VERSION",
    "WarehouseFingerprint",
    "build_migration_plan",
    "canonical_json_bytes",
    "canonical_json_sha256",
    "load_json_input",
    "render_plan",
    "write_plan_new_file",
]
