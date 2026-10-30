"""Reconcile sanitized runtime and host candidates against a static migration catalog.

This module is an offline evidence join only. It does not access the filesystem,
environment, network, database, scheduler, or subprocesses. A host path match is
always a candidate: neither systemd/crontab/process observations nor a path/hash
join proves which process writes a dataset.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import PurePosixPath

from opendata.migration_plan import MigrationPlanError, build_migration_plan, canonical_json_sha256

RECONCILIATION_SCHEMA_VERSION = "1.0"
RUNTIME_SCHEMA_VERSION = "1.1"
HOST_SCHEMA_VERSION = "1.0"
CATALOG_SCHEMA_VERSION = "1.1"
MAX_RECONCILIATION_ROWS = 100_000
MAX_HOST_SCRIPT_PATHS = 4_096
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SYSTEMD_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}\.(?:service|timer)$")
SECRET_LIKE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"bearer\s+\S+|mysql(?:\+\w+)?://[^\s/:]+:[^\s@]+@)",
    re.IGNORECASE,
)

JsonObject = dict[str, object]


def build_host_runtime_static_reconciliation(
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
    host_inventory: Mapping[str, object],
    *,
    as_of_utc: datetime | str | None,
    max_snapshot_age_seconds: int | None,
) -> JsonObject:
    """Build a blocked-by-default host/runtime/static evidence crosswalk.

    ``as_of_utc`` and ``max_snapshot_age_seconds`` are mandatory evidence
    context; the function never consults the system clock. The current catalog
    schema has no capture timestamp, so its temporal freshness always remains
    UNKNOWN. Current-user host scope and offline identity claims also prevent
    this report from becoming an exhaustive inventory or writer attribution.

    Args:
        runtime_inventory: Sanitized runtime inventory DTO (schema 1.1 expected).
        static_catalog: AST-only static migration catalog DTO (schema 1.1 expected).
        host_inventory: Sanitized host inventory DTO (schema 1.0 expected).
        as_of_utc: Explicit aware evaluation time, or ``None`` when unavailable.
        max_snapshot_age_seconds: Explicit freshness policy, or ``None`` when
            no policy has been approved.

    Returns:
        JSON-compatible report with candidate path joins, explicit unknowns,
        freshness states, input digests, and stable blocker codes.
    """
    blockers: set[str] = {
        "catalog_capture_timestamp_absent_from_schema",
        "host_scope_current_uid_only_not_exhaustive",
        "host_observations_do_not_prove_writer_identity",
        "offline_input_hashes_do_not_authenticate_source_identity",
    }
    input_hashes = {
        "runtime_inventory_sha256": _safe_content_hash(runtime_inventory),
        "static_catalog_sha256": _safe_content_hash(static_catalog),
        "host_inventory_sha256": _safe_content_hash(host_inventory),
    }
    for key, digest in input_hashes.items():
        if digest is None:
            blockers.add(f"{key.removesuffix('_sha256')}_not_canonical_json")

    runtime_schema = runtime_inventory.get("schema_version")
    host_schema = host_inventory.get("schema_version")
    catalog_schema = static_catalog.get("schema_version")
    if runtime_schema != RUNTIME_SCHEMA_VERSION:
        blockers.add("runtime_inventory_schema_not_current")
    if host_schema != HOST_SCHEMA_VERSION:
        blockers.add("host_inventory_schema_unsupported")
    if catalog_schema != CATALOG_SCHEMA_VERSION:
        blockers.add("static_catalog_schema_unsupported")

    as_of = _parse_aware_utc(as_of_utc)
    if as_of is None:
        blockers.add("explicit_as_of_utc_missing_or_invalid")
    age_limit = _valid_age_limit(max_snapshot_age_seconds)
    if age_limit is None:
        blockers.add("explicit_snapshot_age_policy_missing_or_invalid")

    runtime_freshness = _freshness(runtime_inventory.get("generated_at_utc"), as_of, age_limit)
    host_freshness = _freshness(host_inventory.get("observed_at_utc"), as_of, age_limit)
    freshness: JsonObject = {
        "as_of_utc": as_of.isoformat().replace("+00:00", "Z") if as_of else None,
        "max_snapshot_age_seconds": age_limit,
        "runtime_inventory": runtime_freshness,
        "host_inventory": host_freshness,
        "static_catalog": {
            "status": "UNKNOWN",
            "unrecognized_generated_at_utc_field_present": isinstance(
                static_catalog.get("generated_at_utc"), str
            ),
            "reason": "catalog_schema_1_1_has_no_capture_timestamp",
        },
    }
    for label, section_freshness in (
        ("runtime_inventory", runtime_freshness),
        ("host_inventory", host_freshness),
    ):
        state = section_freshness["status"]
        if state != "FRESH_WITHIN_POLICY":
            blockers.add(f"{label}_{str(state).lower()}")

    root_pin_status = _host_root_pin_status(runtime_inventory, static_catalog, host_inventory)
    if root_pin_status != "MATCHED":
        blockers.add(f"host_project_root_pin_{root_pin_status.lower()}")
    source_root_match = _safe_absolute_path(
        runtime_inventory.get("source_root")
    ) and runtime_inventory.get("source_root") == static_catalog.get("source_root")
    path_roots_match = root_pin_status == "MATCHED" and source_root_match
    if not source_root_match:
        blockers.add("runtime_static_source_root_mismatch")

    _validate_host_scope(host_inventory, blockers)
    _validate_host_sections(host_inventory, blockers)

    planner_report: Mapping[str, object] | None = None
    planner_issues: list[str] = []
    try:
        raw_plan = build_migration_plan(runtime_inventory, static_catalog)
        if isinstance(raw_plan, Mapping):
            planner_report = _mapping(raw_plan)
            planner_issues = _safe_string_list(planner_report.get("integrity_issues"))
        else:
            blockers.add("runtime_static_crosswalk_contract_invalid")
    except MigrationPlanError:
        blockers.add("runtime_static_crosswalk_contract_invalid")
    # The offline planner also emits execution-governance issues when its
    # target allowlist is absent. Those issues keep the migration plan blocked,
    # but do not invalidate a runtime/static source-path anti-join. Keep that
    # distinction explicit; any other planner integrity issue remains
    # crosswalk-blocking so unknown future issue codes fail closed.
    planner_governance_issues = [
        issue for issue in planner_issues if issue == "target_allowlist_missing"
    ]
    planner_crosswalk_issues = [
        issue for issue in planner_issues if issue not in planner_governance_issues
    ]
    if planner_governance_issues:
        blockers.add("migration_plan_governance_incomplete")
    if planner_crosswalk_issues:
        blockers.update(f"runtime_static_{issue}" for issue in planner_crosswalk_issues)

    blockers_before_crosswalk_indexes = set(blockers)
    static_by_path = _static_path_index(static_catalog, blockers)
    runtime_by_path = _runtime_path_index(planner_report, runtime_inventory, blockers)
    crosswalk_index_blockers = blockers - blockers_before_crosswalk_indexes
    crosswalk_status = (
        "VERIFIED_PATH_ANTI_JOIN"
        if (
            planner_report is not None
            and not planner_crosswalk_issues
            and not crosswalk_index_blockers
            and root_pin_status == "MATCHED"
            and source_root_match
            and runtime_freshness.get("status") == "FRESH_WITHIN_POLICY"
            and host_freshness.get("status") == "FRESH_WITHIN_POLICY"
            and runtime_schema == RUNTIME_SCHEMA_VERSION
            and catalog_schema == CATALOG_SCHEMA_VERSION
        )
        else "UNVERIFIED_OR_BLOCKED"
    )
    unresolved_runtime_paths, runtime_path_review_status = _unresolved_runtime_paths(
        planner_report, blockers
    )

    candidates = _collect_host_candidates(
        host_inventory,
        static_by_path,
        runtime_by_path,
        crosswalk_status=crosswalk_status,
        path_roots_match=path_roots_match,
        blockers=blockers,
    )
    runtime_paths_not_observed, static_paths_not_observed = _host_path_anti_joins(
        candidates,
        static_by_path,
        runtime_by_path,
        crosswalk_status=crosswalk_status,
        path_roots_match=path_roots_match,
        host_inventory=host_inventory,
        blockers=blockers,
    )
    candidate_status_counts = Counter(str(item["mapping_status"]) for item in candidates)
    summary: JsonObject = {
        "host_candidate_count": len(candidates),
        "host_candidate_with_path_count": sum(
            item.get("host_path") is not None for item in candidates
        ),
        "exact_path_candidate_count": candidate_status_counts.get("EXACT_PATH_CANDIDATE", 0),
        "ambiguous_path_candidate_count": candidate_status_counts.get(
            "AMBIGUOUS_PATH_CANDIDATE", 0
        ),
        "unmapped_static_path_count": candidate_status_counts.get("STATIC_PATH_MISSING", 0),
        "unmapped_runtime_path_count": candidate_status_counts.get("RUNTIME_PATH_MISSING", 0),
        "unmapped_host_path_count": candidate_status_counts.get(
            "HOST_PATH_OUTSIDE_LEGACY_CROSSWALK", 0
        ),
        "host_path_missing_count": candidate_status_counts.get("HOST_SCRIPT_PATH_MISSING", 0),
        "runtime_paths_not_observed_in_host_scope_count": len(runtime_paths_not_observed),
        "static_paths_not_observed_in_host_scope_count": len(static_paths_not_observed),
        "runtime_path_unresolved_count": len(unresolved_runtime_paths),
        "runtime_static_crosswalk_status": crosswalk_status,
        "blocked_reason_count": len(blockers),
    }

    return {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "reconciliation_status": "BLOCKED",
        "candidate_interpretation": (
            "path_candidates_from_supplied_snapshots_only; "
            "current_state_and_writer_identity_unknown"
        ),
        "candidate_time_interpretation": (
            "runtime_and_host_freshness_are_policy_checked; static_catalog_capture_time_is_unknown"
        ),
        "digest_semantics": {
            "static_source_sha256": "SHA-256 of source-file bytes from the AST catalog",
            "runtime_module_path_sha256": (
                "SHA-256 of the legacy module_path value; not source bytes and not compared "
                "to host command digests"
            ),
            "host_command_sha256": "SHA-256 of sanitized-in-memory command representation",
            "host_line_sha256": "SHA-256 of the user crontab line",
            "input_sha256": "Canonical JSON content binding only; not authenticity proof",
        },
        "inputs": {
            "runtime_inventory_schema_version": _safe_schema_version(runtime_schema),
            "static_catalog_schema_version": _safe_schema_version(catalog_schema),
            "host_inventory_schema_version": _safe_schema_version(host_schema),
            **input_hashes,
        },
        "freshness": freshness,
        "host_project_root_pin_status": root_pin_status,
        "runtime_static_crosswalk_status": crosswalk_status,
        "runtime_static_integrity_issues": planner_crosswalk_issues,
        "migration_plan_governance_issues": planner_governance_issues,
        "migration_plan_integrity_issues": planner_issues,
        "runtime_path_review_status": runtime_path_review_status,
        "blocked_reasons": sorted(blockers),
        "host_candidates": candidates,
        "runtime_paths_unresolved": unresolved_runtime_paths,
        "runtime_paths_not_observed_in_host_scope": runtime_paths_not_observed,
        "static_paths_not_observed_in_host_scope": static_paths_not_observed,
        "summary": summary,
    }


def _collect_host_candidates(
    host_inventory: Mapping[str, object],
    static_by_path: Mapping[str, list[Mapping[str, object]]],
    runtime_by_path: Mapping[str, list[JsonObject]],
    *,
    crosswalk_status: str,
    path_roots_match: bool,
    blockers: set[str],
) -> list[JsonObject]:
    """Flatten host sections into sanitized path candidates without writer claims."""
    result: list[JsonObject] = []
    systemd = _mapping(host_inventory.get("systemd_user"))
    systemd_records = _object_rows(systemd.get("records"), blockers, "systemd_user")
    if systemd_records is not None:
        seen_units: set[str] = set()
        for record in systemd_records:
            if (
                len(result) >= MAX_RECONCILIATION_ROWS
                or "host_candidate_limit_exceeded" in blockers
            ):
                blockers.add("host_candidate_limit_exceeded")
                break
            unit = record.get("unit")
            if (
                not isinstance(unit, str)
                or SYSTEMD_UNIT_PATTERN.fullmatch(unit) is None
                or SECRET_LIKE_PATTERN.search(unit) is not None
            ):
                blockers.add("host_systemd_unit_identity_invalid")
                continue
            identity_status = "UNIT_NAME_WITHIN_SNAPSHOT"
            if unit in seen_units:
                identity_status = "AMBIGUOUS_DUPLICATE_UNIT"
                blockers.add("host_duplicate_systemd_unit_identity")
            seen_units.add(unit)
            exec_record = _mapping(record.get("exec"))
            common: JsonObject = {
                "host_source": "systemd_user",
                "host_record_key": f"systemd:{unit}",
                "host_record_identity_status": identity_status,
                "unit": unit,
                "unit_type": _safe_enum(record.get("unit_type"), {"service", "timer"}),
                "active": record.get("active") if type(record.get("active")) is bool else None,
                "active_state": _safe_state(record.get("active_state")),
                "installed": record.get("installed")
                if type(record.get("installed")) is bool
                else None,
                "related_via_service": _safe_systemd_target(record.get("related_via_service")),
                "host_command_sha256": _safe_sha256(exec_record.get("command_sha256")),
                "host_command_sha256_is_prefix": False,
            }
            if common["host_command_sha256"] is None:
                blockers.add("host_systemd_command_digest_unknown")
            _append_host_paths(
                result,
                common,
                exec_record.get("script_paths"),
                static_by_path,
                runtime_by_path,
                crosswalk_status,
                path_roots_match,
                blockers,
                context_project=_project_name(
                    record.get("fragment_project"), record.get("working_directory_project")
                ),
            )

    crontab = _mapping(host_inventory.get("crontab"))
    cron_records = _object_rows(crontab.get("records"), blockers, "crontab")
    if cron_records is not None:
        line_counts = Counter(
            line_number
            for record in cron_records
            if type(line_number := record.get("line_number")) is int and line_number >= 1
        )
        duplicate_line_numbers = {
            line_number for line_number, count in line_counts.items() if count > 1
        }
        if duplicate_line_numbers:
            blockers.add("host_duplicate_crontab_line_identity")
        for record in cron_records:
            if (
                len(result) >= MAX_RECONCILIATION_ROWS
                or "host_candidate_limit_exceeded" in blockers
            ):
                blockers.add("host_candidate_limit_exceeded")
                break
            line_number = record.get("line_number")
            line_hash = _safe_sha256(record.get("line_sha256"))
            if type(line_number) is not int or line_number < 1 or line_hash is None:
                blockers.add("host_crontab_record_identity_invalid")
                continue
            identity_status = "LINE_NUMBER_AND_HASH_WITHIN_SNAPSHOT"
            if line_number in duplicate_line_numbers:
                identity_status = "AMBIGUOUS_DUPLICATE_LINE_NUMBER"
            common = {
                "host_source": "crontab",
                "host_record_key": f"crontab:{line_number}:{line_hash}",
                "host_record_identity_status": identity_status,
                "line_number": line_number,
                "schedule": _safe_cron_schedule(record.get("schedule")),
                "host_line_sha256": line_hash,
                "host_command_sha256": _safe_sha256(record.get("command_sha256")),
                "host_command_sha256_is_prefix": False,
            }
            if common["host_command_sha256"] is None:
                blockers.add("host_crontab_command_digest_unknown")
            _append_host_paths(
                result,
                common,
                record.get("script_paths"),
                static_by_path,
                runtime_by_path,
                crosswalk_status,
                path_roots_match,
                blockers,
            )

    processes = _mapping(host_inventory.get("processes"))
    process_records = _object_rows(processes.get("records"), blockers, "processes")
    if process_records is not None:
        process_groups: dict[
            tuple[str | None, tuple[tuple[str, str], ...], str | None],
            list[Mapping[str, object]],
        ]
        process_groups = defaultdict(list)
        process_hash_counts: Counter[str] = Counter()
        process_path_count = 0
        for record in process_records:
            if "host_candidate_limit_exceeded" in blockers:
                break
            remaining_paths = MAX_RECONCILIATION_ROWS - len(result) - process_path_count
            if remaining_paths <= 0:
                blockers.add("host_candidate_limit_exceeded")
                break
            path_pairs = _safe_host_paths(
                record.get("script_paths"),
                blockers,
                max_paths=min(MAX_HOST_SCRIPT_PATHS, remaining_paths),
            )
            process_path_count += len(path_pairs)
            command_hash = _safe_sha256(record.get("command_sha256"))
            if command_hash is None:
                blockers.add("host_process_command_digest_unknown")
            else:
                process_hash_counts[command_hash] += 1
            cwd_project = _safe_project(record.get("cwd_project"))
            process_groups[(command_hash, tuple(path_pairs), cwd_project)].append(record)
        for (command_hash, group_paths, group_cwd_project), records in sorted(
            process_groups.items(),
            key=lambda pair: (pair[0][0] or "", pair[0][1], pair[0][2] or ""),
        ):
            duplicate_hash = command_hash is not None and process_hash_counts[command_hash] > 1
            if command_hash is None:
                identity_status = "UNKNOWN_COMMAND_HASH"
            elif duplicate_hash:
                identity_status = "AMBIGUOUS_DUPLICATE_COMMAND_HASH"
                blockers.add("host_process_command_hash_ambiguous")
            else:
                identity_status = "COMMAND_HASH_ONLY"
            allowed_classifications = {
                "owned_test_infrastructure",
                "project_related_unclassified",
            }
            classification_values = {
                value
                for record in records
                if (value := _safe_enum(record.get("classification"), allowed_classifications))
                is not None
            }
            has_unknown_classification = any(
                _safe_enum(record.get("classification"), allowed_classifications) is None
                for record in records
            )
            if len(classification_values) > 1:
                classification = None
                classification_status = "AMBIGUOUS_CONFLICT"
                blockers.add("host_process_classification_ambiguous")
            elif classification_values and has_unknown_classification:
                classification = None
                classification_status = "PARTIAL"
                blockers.add("host_process_classification_incomplete")
            elif classification_values:
                classification = next(iter(classification_values))
                classification_status = "CONSISTENT_WITHIN_SNAPSHOT"
            else:
                classification = None
                classification_status = "UNKNOWN"
            common = {
                "host_source": "same_uid_process",
                "host_record_key": f"process:{command_hash or 'unknown'}",
                "host_record_identity_status": identity_status,
                "observation_count": len(records),
                "cwd_project": group_cwd_project,
                "classification": classification,
                "classification_status": classification_status,
                "host_command_sha256": command_hash,
                "host_command_sha256_is_prefix": any(
                    record.get("command_hash_is_prefix") is True for record in records
                ),
            }
            _append_host_paths(
                result,
                common,
                [{"project": project, "path": path} for project, path in group_paths],
                static_by_path,
                runtime_by_path,
                crosswalk_status,
                path_roots_match,
                blockers,
                context_project=group_cwd_project,
            )

    if len(result) > MAX_RECONCILIATION_ROWS:
        blockers.add("host_candidate_limit_exceeded")
        return result[:MAX_RECONCILIATION_ROWS]
    return sorted(
        result,
        key=lambda item: (
            str(item.get("host_source")),
            str(item.get("host_record_key")),
            str(item.get("host_path") or ""),
        ),
    )


def _append_host_paths(
    destination: list[JsonObject],
    common: Mapping[str, object],
    raw_paths: object,
    static_by_path: Mapping[str, list[Mapping[str, object]]],
    runtime_by_path: Mapping[str, list[JsonObject]],
    crosswalk_status: str,
    path_roots_match: bool,
    blockers: set[str],
    *,
    context_project: str | None = None,
) -> None:
    """Append one record per validated script path, or a pathless host candidate."""
    remaining_rows = MAX_RECONCILIATION_ROWS - len(destination)
    if remaining_rows <= 0:
        blockers.add("host_candidate_limit_exceeded")
        return
    path_limit = min(MAX_HOST_SCRIPT_PATHS, remaining_rows)
    paths = _safe_host_paths(raw_paths, blockers, max_paths=path_limit)
    if not paths:
        blockers.add("host_script_path_missing")
        destination.append(
            {
                **common,
                "project": context_project,
                "host_path": None,
                "static_source_sha256": None,
                "static_source_hash_status": "UNKNOWN",
                "runtime_module_path_sha256_candidates": [],
                "runtime_module_path_hash_status": "UNKNOWN",
                "runtime_static_digest_comparison": "NOT_COMPARABLE_DIFFERENT_SEMANTICS",
                "runtime_task_candidates": [],
                "mapping_status": "HOST_SCRIPT_PATH_MISSING",
                "writer_status": "UNKNOWN",
            }
        )
        return

    for project, path in paths:
        if len(destination) >= MAX_RECONCILIATION_ROWS:
            blockers.add("host_candidate_limit_exceeded")
            return
        static_rows = static_by_path.get(path, []) if project == "afi" else []
        runtime_rows = runtime_by_path.get(path, []) if project == "afi" else []
        static_hashes = sorted(
            {
                digest
                for row in static_rows
                if (digest := _safe_sha256(row.get("source_sha256"))) is not None
            }
        )
        runtime_ids = sorted({str(row["task_id"]) for row in runtime_rows})
        runtime_path_hashes = sorted(
            {
                str(row["module_path_sha256"])
                for row in runtime_rows
                if isinstance(row.get("module_path_sha256"), str)
            }
        )
        valid_runtime_hash_count = sum(
            _safe_sha256(row.get("module_path_sha256")) is not None for row in runtime_rows
        )
        runtime_hash_status = (
            "PRESENT"
            if runtime_rows and valid_runtime_hash_count == len(runtime_rows)
            else "PARTIAL"
            if valid_runtime_hash_count
            else "UNKNOWN"
        )
        if project != "afi":
            mapping_status = "HOST_PATH_OUTSIDE_LEGACY_CROSSWALK"
        elif len(static_rows) > 1 or len(runtime_ids) > 1:
            mapping_status = "AMBIGUOUS_PATH_CANDIDATE"
        elif not path_roots_match or crosswalk_status != "VERIFIED_PATH_ANTI_JOIN":
            mapping_status = "CROSSWALK_INPUTS_UNVERIFIED"
        elif not static_rows:
            mapping_status = "STATIC_PATH_MISSING"
        elif not runtime_ids:
            mapping_status = "RUNTIME_PATH_MISSING"
        elif len(static_hashes) != 1:
            mapping_status = "STATIC_SOURCE_HASH_UNKNOWN_OR_AMBIGUOUS"
        else:
            mapping_status = "EXACT_PATH_CANDIDATE"

        if mapping_status == "AMBIGUOUS_PATH_CANDIDATE":
            blockers.add("host_path_join_ambiguous")
        elif mapping_status == "STATIC_PATH_MISSING":
            blockers.add("host_static_path_unmatched")
        elif mapping_status == "RUNTIME_PATH_MISSING":
            blockers.add("host_runtime_path_unmatched")
        elif mapping_status == "HOST_PATH_OUTSIDE_LEGACY_CROSSWALK":
            blockers.add("host_opendata_path_outside_legacy_crosswalk")
        elif mapping_status == "STATIC_SOURCE_HASH_UNKNOWN_OR_AMBIGUOUS":
            blockers.add("host_static_source_hash_unknown_or_ambiguous")

        destination.append(
            {
                **common,
                "project": project,
                "host_path": path,
                "static_source_sha256": static_hashes[0] if len(static_hashes) == 1 else None,
                "static_source_hash_status": "PRESENT" if len(static_hashes) == 1 else "UNKNOWN",
                "runtime_module_path_sha256_candidates": runtime_path_hashes,
                "runtime_module_path_hash_status": runtime_hash_status,
                "runtime_static_digest_comparison": "NOT_COMPARABLE_DIFFERENT_SEMANTICS",
                "runtime_task_candidates": runtime_ids,
                "mapping_status": mapping_status,
                "writer_status": "UNKNOWN",
            }
        )


def _host_path_anti_joins(
    host_candidates: Sequence[Mapping[str, object]],
    static_by_path: Mapping[str, list[Mapping[str, object]]],
    runtime_by_path: Mapping[str, list[JsonObject]],
    *,
    crosswalk_status: str,
    path_roots_match: bool,
    host_inventory: Mapping[str, object],
    blockers: set[str],
) -> tuple[list[JsonObject], list[JsonObject]]:
    """Report paths absent from the scoped host snapshot without asserting absence."""
    observed_afi_paths = (
        {
            path
            for record in host_candidates
            if record.get("project") == "afi" and isinstance((path := record.get("host_path")), str)
        }
        if crosswalk_status == "VERIFIED_PATH_ANTI_JOIN" and path_roots_match
        else set()
    )
    if crosswalk_status != "VERIFIED_PATH_ANTI_JOIN" or not path_roots_match:
        observation_status = "UNCOMPARABLE_CROSSWALK_INPUTS"
    elif _host_sections_complete(host_inventory, blockers):
        observation_status = "NOT_OBSERVED_WITHIN_HOST_INVENTORY_SCOPE"
    else:
        observation_status = "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    runtime_not_observed: list[JsonObject] = []
    for path, runtime_rows in sorted(runtime_by_path.items()):
        if path in observed_afi_paths:
            continue
        runtime_not_observed.extend(
            {
                "task_id": row.get("task_id"),
                "script_id": row.get("script_id"),
                "source_path": path,
                "static_source_sha256": row.get("static_source_sha256"),
                "crosswalk_status": crosswalk_status,
                "observation_status": observation_status,
            }
            for row in runtime_rows
        )

    static_not_observed: list[JsonObject] = []
    for path, static_rows in sorted(static_by_path.items()):
        if path in observed_afi_paths:
            continue
        hashes = sorted(
            {
                digest
                for row in static_rows
                if (digest := _safe_sha256(row.get("source_sha256"))) is not None
            }
        )
        static_not_observed.append(
            {
                "source_path": path,
                "static_source_sha256": hashes[0] if len(hashes) == 1 else None,
                "runtime_task_candidates": (
                    sorted({str(row["task_id"]) for row in runtime_by_path.get(path, [])})
                    if crosswalk_status == "VERIFIED_PATH_ANTI_JOIN" and path_roots_match
                    else []
                ),
                "crosswalk_status": crosswalk_status,
                "observation_status": observation_status,
            }
        )
    return runtime_not_observed, static_not_observed


def _host_sections_complete(host_inventory: Mapping[str, object], blockers: set[str]) -> bool:
    """Return whether host sections and path records were fully normalized."""
    if host_inventory.get("schema_version") != HOST_SCHEMA_VERSION:
        return False
    if "host_candidate_limit_exceeded" in blockers:
        return False
    for section_name in ("systemd_user", "crontab", "processes"):
        section = _mapping(host_inventory.get(section_name))
        if section.get("status") != "OK":
            return False
        errors = section.get("errors")
        error_counts = section.get("error_counts")
        if errors is not None and (not isinstance(errors, Mapping) or bool(errors)):
            return False
        if error_counts is not None and (
            not isinstance(error_counts, Mapping) or bool(error_counts)
        ):
            return False
        records = section.get("records")
        if (
            not isinstance(records, Sequence)
            or isinstance(records, (str, bytes, bytearray))
            or len(records) > MAX_RECONCILIATION_ROWS
            or any(
                not isinstance(record, Mapping) or any(not isinstance(key, str) for key in record)
                for record in records
            )
        ):
            return False
        for record in records:
            if not isinstance(record, Mapping):
                return False
            if section_name == "systemd_user":
                unit = record.get("unit")
                if (
                    not isinstance(unit, str)
                    or SYSTEMD_UNIT_PATTERN.fullmatch(unit) is None
                    or SECRET_LIKE_PATTERN.search(unit) is not None
                ):
                    return False
                exec_record = _mapping(record.get("exec"))
                raw_paths = exec_record.get("script_paths")
            else:
                raw_paths = record.get("script_paths")
                if section_name == "crontab" and (
                    type(record.get("line_number")) is not int
                    or record.get("line_number", 0) < 1
                    or _safe_sha256(record.get("line_sha256")) is None
                ):
                    return False
            if not _host_path_rows_complete(raw_paths):
                return False
    return True


def _host_path_rows_complete(value: object) -> bool:
    """Check that a host script-path list can be fully normalized without dropping rows."""
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > MAX_HOST_SCRIPT_PATHS
    ):
        return False
    return all(
        isinstance(row, Mapping)
        and all(isinstance(key, str) for key in row)
        and isinstance(row.get("project"), str)
        and row.get("project") in {"afi", "opendata"}
        and _safe_relative_path(row.get("path"))
        for row in value
    )


def _static_path_index(
    static_catalog: Mapping[str, object], blockers: set[str]
) -> dict[str, list[Mapping[str, object]]]:
    """Index catalog source paths while retaining duplicate entries as ambiguity."""
    files = _object_rows(static_catalog.get("files"), blockers, "static_catalog_files")
    index: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    if files is None:
        return index
    for record in files:
        path = record.get("source_path")
        digest = record.get("source_sha256")
        if (
            not isinstance(path, str)
            or not _safe_relative_path(path)
            or _safe_sha256(digest) is None
        ):
            blockers.add("static_catalog_source_identity_invalid")
            continue
        index[path].append(record)
    for records in index.values():
        records.sort(key=lambda record: _safe_sha256(record.get("source_sha256")) or "")
    return index


def _runtime_path_index(
    planner_report: Mapping[str, object] | None,
    runtime_inventory: Mapping[str, object],
    blockers: set[str],
) -> dict[str, list[JsonObject]]:
    """Index planner-validated task rows by exact project-relative source path."""
    if planner_report is None:
        return {}
    metadata = _mapping(runtime_inventory.get("metadata"))
    script_section = _mapping(metadata.get("data_scripts"))
    script_records = _object_rows(script_section.get("records"), blockers, "runtime_scripts")
    path_digest_by_script: dict[str, str] = {}
    if script_records is not None:
        for record in script_records:
            script_id = record.get("script_id")
            digest = _safe_sha256(record.get("module_path_sha256"))
            if isinstance(script_id, str) and digest is not None:
                path_digest_by_script[script_id] = digest

    result: dict[str, list[JsonObject]] = defaultdict(list)
    for collection_key in ("tasks", "helpers"):
        rows = _object_rows(
            planner_report.get(collection_key), blockers, f"planner_{collection_key}"
        )
        if rows is None:
            continue
        for row in rows:
            path = row.get("script_path")
            task_id = row.get("task_id")
            script_id = row.get("script_id")
            if (
                not _safe_relative_path(path)
                or not isinstance(task_id, str)
                or not isinstance(path, str)
                or row.get("path_status", "PATH_CANDIDATE") != "PATH_CANDIDATE"
                or "runtime_script_path_unresolved" in _safe_string_list(row.get("blocked_reasons"))
            ):
                continue
            result[path].append(
                {
                    "task_id": task_id,
                    "script_id": script_id if isinstance(script_id, str) else None,
                    "module_path_sha256": (
                        path_digest_by_script.get(script_id) if isinstance(script_id, str) else None
                    ),
                    "static_source_sha256": _safe_sha256(row.get("script_sha256")),
                }
            )
    for indexed_rows in result.values():
        indexed_rows.sort(key=lambda row: str(row.get("task_id")))
    return result


def _unresolved_runtime_paths(
    planner_report: Mapping[str, object] | None, blockers: set[str]
) -> tuple[list[JsonObject], str]:
    """Expose unresolved runtime paths using only bounded, sanitized plan fields."""
    if planner_report is None:
        blockers.add("runtime_path_review_inputs_unavailable")
        return [], "UNKNOWN"

    unresolved: list[JsonObject] = []
    review_incomplete = False
    task_id_pattern = re.compile(
        r"(?:scheduled:[0-9]{1,18}|script:[A-Za-z0-9_][A-Za-z0-9._:/+\-]{0,127})"
    )
    script_id_pattern = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:/+\-]{0,127}")
    for collection_key in ("tasks", "helpers"):
        rows = _object_rows(
            planner_report.get(collection_key), blockers, f"planner_{collection_key}"
        )
        if rows is None:
            review_incomplete = True
            continue
        for row in rows:
            raw_task_id = row.get("task_id")
            raw_script_id = row.get("script_id")
            task_id = (
                raw_task_id
                if isinstance(raw_task_id, str)
                and task_id_pattern.fullmatch(raw_task_id)
                and SECRET_LIKE_PATTERN.search(raw_task_id) is None
                else None
            )
            script_id = (
                raw_script_id
                if isinstance(raw_script_id, str)
                and script_id_pattern.fullmatch(raw_script_id)
                and SECRET_LIKE_PATTERN.search(raw_script_id) is None
                else None
            )
            raw_path = row.get("script_path")
            safe_path = raw_path if _safe_relative_path(raw_path) else None
            raw_status = row.get("path_status", "PATH_CANDIDATE")
            path_blocked = "runtime_script_path_unresolved" in _safe_string_list(
                row.get("blocked_reasons")
            )
            if (
                task_id is not None
                and safe_path is not None
                and raw_status == "PATH_CANDIDATE"
                and not path_blocked
            ):
                continue

            if safe_path is None:
                path_status = "PATH_UNKNOWN_OR_UNSAFE"
            elif task_id is None:
                path_status = "RUNTIME_ROW_IDENTITY_UNKNOWN"
            elif raw_status != "PATH_CANDIDATE" or path_blocked:
                path_status = "PATH_STATUS_UNRESOLVED"
            else:
                path_status = "PATH_UNRESOLVED"
            unresolved.append(
                {
                    "task_id": task_id,
                    "script_id": script_id,
                    "source_path": safe_path,
                    "path_status": path_status,
                    "static_source_sha256": _safe_sha256(row.get("script_sha256")),
                }
            )
            blockers.add("runtime_path_unresolved")

    if review_incomplete:
        return unresolved, "PARTIAL"
    if unresolved:
        return unresolved, "PARTIAL"
    return unresolved, "REVIEWED_WITHIN_PLAN"


def _validate_host_scope(host_inventory: Mapping[str, object], blockers: set[str]) -> None:
    """Require the known sanitized scope contract and exact project-root pins."""
    scope = _mapping(host_inventory.get("scope"))
    if scope.get("filesystem_scope") != "explicit_project_roots_and_same_uid_procfs_only":
        blockers.add("host_filesystem_scope_unknown")
    for field in (
        "environment_values_read",
        "sensitive_material_files_read",
        "command_text_persisted",
    ):
        if scope.get(field) is not False:
            blockers.add(f"host_scope_{field}_not_false")
    if _mapping(scope.get("project_roots")) == {}:
        blockers.add("host_project_roots_missing")


def _validate_host_sections(host_inventory: Mapping[str, object], blockers: set[str]) -> None:
    """Keep incomplete host sections explicit and fail closed."""
    expected = {
        "systemd_user": "UNKNOWN",
        "crontab": "UNKNOWN",
        "processes": "PARTIAL",
    }
    for section_name, incomplete_status in expected.items():
        section = _mapping(host_inventory.get(section_name))
        if section.get("status") != "OK":
            blockers.add(
                f"host_{section_name}_{str(section.get('status', incomplete_status)).lower()}"
            )
        errors = section.get("errors")
        if errors is not None and (not isinstance(errors, Mapping) or bool(errors)):
            blockers.add(f"host_{section_name}_reports_errors")
        error_counts = section.get("error_counts")
        if error_counts is not None and (
            not isinstance(error_counts, Mapping) or bool(error_counts)
        ):
            blockers.add(f"host_{section_name}_reports_errors")


def _host_root_pin_status(
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
    host_inventory: Mapping[str, object],
) -> str:
    """Compare supplied host root digests to paths pinned in the two DTOs."""
    scope = _mapping(host_inventory.get("scope"))
    roots = _mapping(scope.get("project_roots"))
    afi_root = _mapping(roots.get("afi"))
    opendata_root = _mapping(roots.get("opendata"))
    runtime_root = runtime_inventory.get("source_root")
    catalog_opendata_root = static_catalog.get("opendata_root")
    if not all(_safe_absolute_path(value) for value in (runtime_root, catalog_opendata_root)):
        return "UNKNOWN"
    if afi_root.get("available") is not True or opendata_root.get("available") is not True:
        return "UNAVAILABLE"
    try:
        expected_afi = hashlib.sha256(str(runtime_root).encode("utf-8")).hexdigest()
        expected_opendata = hashlib.sha256(str(catalog_opendata_root).encode("utf-8")).hexdigest()
    except UnicodeError:
        return "UNKNOWN"
    observed_afi = _safe_sha256(afi_root.get("root_path_sha256"))
    observed_opendata = _safe_sha256(opendata_root.get("root_path_sha256"))
    if observed_afi is None or observed_opendata is None:
        return "UNKNOWN"
    return (
        "MATCHED"
        if (observed_afi, observed_opendata) == (expected_afi, expected_opendata)
        else "MISMATCH"
    )


def _freshness(value: object, as_of: datetime | None, age_limit: int | None) -> JsonObject:
    """Classify a timestamp only against an explicit caller-provided policy."""
    captured = _parse_aware_utc(value)
    if captured is None or as_of is None or age_limit is None:
        return {"status": "UNKNOWN", "captured_at_utc": None, "age_seconds": None}
    age = (as_of - captured).total_seconds()
    if age < 0:
        status = "FUTURE"
    elif age > age_limit:
        status = "STALE"
    else:
        status = "FRESH_WITHIN_POLICY"
    return {
        "status": status,
        "captured_at_utc": captured.isoformat().replace("+00:00", "Z"),
        "age_seconds": int(age) if age >= 0 else None,
    }


def _parse_aware_utc(value: object) -> datetime | None:
    """Parse an explicit offset timestamp and normalize it to UTC."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_age_limit(value: object) -> int | None:
    """Accept only a non-negative, non-boolean integer freshness window."""
    return value if type(value) is int and 0 <= value <= 31_536_000 else None


def _safe_content_hash(value: Mapping[str, object]) -> str | None:
    """Hash canonical JSON DTO content without reading any external source."""
    try:
        digest = canonical_json_sha256(value)
        return digest if isinstance(digest, str) else None
    except (MigrationPlanError, RecursionError, TypeError, ValueError, OverflowError):
        return None


def _object_rows(
    value: object,
    blockers: set[str],
    label: str,
    *,
    max_rows: int | None = None,
) -> list[Mapping[str, object]] | None:
    """Return bounded mapping rows or add a stable schema blocker."""
    effective_max_rows = MAX_RECONCILIATION_ROWS if max_rows is None else max_rows
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) > effective_max_rows
        or any(not isinstance(item, Mapping) for item in value)
    ):
        blockers.add(f"{label}_rows_invalid_or_too_large")
        return None
    return [item for item in value if isinstance(item, Mapping)]


def _safe_host_paths(
    value: object,
    blockers: set[str],
    *,
    max_paths: int = MAX_HOST_SCRIPT_PATHS,
) -> list[tuple[str, str]]:
    """Read only validated project labels and canonical relative script paths."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        blockers.add("host_script_paths_rows_invalid_or_too_large")
        return []
    if len(value) > MAX_HOST_SCRIPT_PATHS:
        blockers.add("host_script_path_count_exceeds_limit")
        return []
    rows = _object_rows(
        value,
        blockers,
        "host_script_paths",
        max_rows=MAX_HOST_SCRIPT_PATHS,
    )
    if rows is None:
        return []
    found: set[tuple[str, str]] = set()
    for row in rows:
        project = row.get("project")
        path = row.get("path")
        if (
            not isinstance(project, str)
            or project not in {"afi", "opendata"}
            or not _safe_relative_path(path)
        ):
            blockers.add("host_script_path_invalid")
            continue
        found.add((project, str(path)))
    normalized = sorted(found)
    effective_limit = min(MAX_HOST_SCRIPT_PATHS, max_paths)
    if len(normalized) > effective_limit:
        if effective_limit < MAX_HOST_SCRIPT_PATHS:
            blockers.add("host_candidate_limit_exceeded")
            return normalized[:effective_limit]
        blockers.add("host_script_path_count_exceeds_limit")
        return []
    return normalized


def _safe_relative_path(value: object) -> bool:
    """Reject paths that are absolute, non-canonical, or traverse directories."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or SECRET_LIKE_PATTERN.search(value) is not None
    ):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == path.as_posix()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _safe_absolute_path(value: object) -> bool:
    """Accept only bounded, canonical absolute POSIX paths without controls."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 1024
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or SECRET_LIKE_PATTERN.search(value) is not None
    ):
        return False
    path = PurePosixPath(value)
    return (
        path.is_absolute()
        and value == path.as_posix()
        and all(part not in {".", ".."} for part in path.parts)
    )


def _mapping(value: object) -> Mapping[str, object]:
    """Return a mapping only when all keys are strings."""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        return {}
    return value


def _safe_sha256(value: object) -> str | None:
    """Return only canonical lowercase SHA-256 digests."""
    return value if isinstance(value, str) and SHA256_PATTERN.fullmatch(value) else None


def _safe_schema_version(value: object) -> str | None:
    """Return short schema labels without echoing arbitrary input content."""
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]+\.[0-9]+", value) else None


def _safe_string_list(value: object) -> list[str]:
    """Filter planner issues to bounded machine-readable codes."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return sorted(
        {item for item in value if isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,96}", item)}
    )


def _safe_text(value: object, *, max_length: int) -> str | None:
    """Retain concise display metadata without control characters."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
        or SECRET_LIKE_PATTERN.search(value) is not None
    ):
        return None
    return value


def _safe_enum(value: object, allowed: set[str]) -> str | None:
    """Return a member of a fixed safe vocabulary."""
    return value if isinstance(value, str) and value in allowed else None


def _safe_state(value: object) -> str | None:
    """Expose only systemd state strings from the inventory's known vocabulary."""
    return _safe_enum(
        value,
        {"active", "inactive", "failed", "activating", "deactivating", "reloading", "UNKNOWN"},
    )


def _safe_systemd_target(value: object) -> str | None:
    """Expose a related unit only when it matches the fixed unit-name pattern."""
    return (
        value
        if isinstance(value, str)
        and SYSTEMD_UNIT_PATTERN.fullmatch(value)
        and SECRET_LIKE_PATTERN.search(value) is None
        else None
    )


def _safe_project(value: object) -> str | None:
    """Expose only known project labels."""
    return value if isinstance(value, str) and value in {"afi", "opendata"} else None


def _safe_cron_schedule(value: object) -> str | None:
    """Expose only conventional cron fields, never arbitrary command text."""
    schedule = _safe_text(value, max_length=80)
    if schedule is None:
        return None
    special_schedules = {
        "@reboot",
        "@yearly",
        "@annually",
        "@monthly",
        "@weekly",
        "@daily",
        "@midnight",
        "@hourly",
    }
    if schedule.startswith("@"):
        return schedule if schedule in special_schedules else None
    fields = schedule.split()
    if len(fields) != 5:
        return None
    valid_field = r"[A-Za-z0-9*/?,#LW+%\-]+"
    return schedule if all(re.fullmatch(valid_field, field) for field in fields) else None


def _project_name(*values: object) -> str | None:
    """Return a project only when all present location hints agree."""
    projects = {_safe_project(value) for value in values}
    projects.discard(None)
    return next(iter(projects)) if len(projects) == 1 else None
