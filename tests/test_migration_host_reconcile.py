"""Behavior tests for offline host/runtime/static reconciliation."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

import opendata.migration_host_reconcile as reconciliation
from opendata.migration_host_reconcile import build_host_runtime_static_reconciliation
from opendata.migration_plan import MigrationPlanError

AFI_ROOT = "/projects/afi"
OPENDATA_ROOT = "/projects/opendata"
SOURCE_PATH = "data_fetch/sina/daily_bars.py"
SCRIPT_ID = "sina_daily_bars"
SOURCE_HASH = "a" * 64
MODULE_PATH_HASH = "b" * 64
COMMAND_HASH = "c" * 64
PARAM_HASH = "d" * 64


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _endpoint(database: str, server_uuid: str) -> dict:
    return {
        "requested_database": database,
        "observed_database": database,
        "endpoint_fingerprint": "e" * 64,
        "read_only_confirmed": True,
        "snapshot_isolation": "repeatable-read",
        "snapshot_captured_at_utc": "2026-10-30T00:00:00Z",
        "server_identity": {
            "database_name": database,
            "server_uuid": server_uuid,
            "server_hostname": "fixture-db",
            "server_port": 3306,
            "server_version": "8.4.0",
            "server_version_comment": "synthetic fixture",
        },
        "server_uuid_pin_matched": True,
    }


def _snapshots(task_count: int = 1) -> tuple[dict, dict, dict]:
    """Create sanitized DTOs satisfying the existing inventory contracts."""
    scheduled_tasks = [
        {
            "id": task_id,
            "script_id": SCRIPT_ID,
            "schedule_type": "cron",
            "expression": "0 16 * * 1-5",
            "is_active": 1,
            "parameter_summary": {"canonical_payload_sha256": PARAM_HASH},
        }
        for task_id in range(1, task_count + 1)
    ]
    data_scripts = [
        {
            "script_id": SCRIPT_ID,
            "source_path": SOURCE_PATH,
            "path_status": "PATH_CANDIDATE",
            "is_active": 1,
            "module_path_sha256": MODULE_PATH_HASH,
            "parameter_summary": {"canonical_payload_sha256": PARAM_HASH},
        }
    ]
    runtime = {
        "schema_version": "1.1",
        "source_root": AFI_ROOT,
        "generated_at_utc": "2026-10-30T00:00:00Z",
        "identity_pin_sha256": "f" * 64,
        "readonly_transaction": {"metadata_confirmed": True, "warehouse_confirmed": True},
        "endpoints": {
            "metadata": _endpoint("ai_for_investor", "11111111-1111-4111-8111-111111111111"),
            "warehouse": _endpoint("akshare_data", "22222222-2222-4222-8222-222222222222"),
        },
        "runtime_table_engine_check": {
            "status": "VERIFIED",
            "check": "BOTH_SCHEDULER_TABLES_ARE_BASE_INNODB",
            "source": "information_schema.TABLES",
            "database": "ai_for_investor",
            "tables": {
                "ak_scheduled_tasks": {"table_type": "BASE TABLE", "engine": "InnoDB"},
                "ak_data_scripts": {"table_type": "BASE TABLE", "engine": "InnoDB"},
            },
        },
        "counts": {
            "scheduled_task_rows_exact": task_count,
            "data_script_rows_exact": 1,
        },
        "metadata": {
            "scheduled_tasks": {
                "row_count_exact": task_count,
                "records": scheduled_tasks,
            },
            "data_scripts": {"row_count_exact": 1, "records": data_scripts},
        },
        "tasks": [
            {
                "task_id": f"scheduled:{task_id}",
                "script_id": SCRIPT_ID,
                "script_path": SOURCE_PATH,
                "runtime_is_active": 1,
                "path_status": "PATH_CANDIDATE",
            }
            for task_id in range(1, task_count + 1)
        ],
    }
    catalog = {
        "schema_version": "1.1",
        "source_root": AFI_ROOT,
        "opendata_root": OPENDATA_ROOT,
        "errors": [],
        "summary": {"legacy_script_count": 1},
        "files": [
            {
                "source_path": SOURCE_PATH,
                "source_sha256": SOURCE_HASH,
                "status": "READY_FOR_ADAPTER",
                "calls": [],
            }
        ],
        "opendata_provider_models": [],
    }
    host = {
        "schema_version": "1.0",
        "observed_at_utc": "2026-10-30T00:00:00Z",
        "scope": {
            "user_id": 1000,
            "project_roots": {
                "afi": {"available": True, "root_path_sha256": _sha256(AFI_ROOT)},
                "opendata": {"available": True, "root_path_sha256": _sha256(OPENDATA_ROOT)},
            },
            "filesystem_scope": "explicit_project_roots_and_same_uid_procfs_only",
            "environment_values_read": False,
            "sensitive_material_files_read": False,
            "command_text_persisted": False,
        },
        "systemd_user": {
            "status": "OK",
            "errors": {},
            "records": [
                {
                    "unit": "afi-refresh.service",
                    "unit_type": "service",
                    "active": True,
                    "active_state": "active",
                    "installed": True,
                    "fragment_project": "afi",
                    "working_directory_project": "afi",
                    "related_via_service": None,
                    "exec": {
                        "command_sha256": COMMAND_HASH,
                        "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
                    },
                }
            ],
        },
        "crontab": {"status": "OK", "records": [], "absence": "no_user_crontab"},
        "processes": {"status": "OK", "records": [], "error_counts": {}},
    }
    return runtime, catalog, host


def _reconcile(runtime: dict, catalog: dict, host: dict, *, as_of: str | None = None) -> dict:
    return build_host_runtime_static_reconciliation(
        runtime,
        catalog,
        host,
        as_of_utc=as_of or datetime(2026, 10, 30, tzinfo=timezone.utc),
        max_snapshot_age_seconds=3600,
    )


def test_exact_path_join_is_only_a_candidate_and_keeps_digest_domains_separate() -> None:
    runtime, catalog, host = _snapshots()

    report = _reconcile(runtime, catalog, host)

    candidate = report["host_candidates"][0]
    assert report["reconciliation_status"] == "BLOCKED"
    assert candidate["mapping_status"] == "EXACT_PATH_CANDIDATE"
    assert candidate["runtime_task_candidates"] == ["scheduled:1"]
    assert candidate["static_source_sha256"] == SOURCE_HASH
    assert candidate["runtime_module_path_sha256_candidates"] == [MODULE_PATH_HASH]
    assert candidate["host_command_sha256"] == COMMAND_HASH
    assert candidate["writer_status"] == "UNKNOWN"
    assert candidate["runtime_static_digest_comparison"] == "NOT_COMPARABLE_DIFFERENT_SEMANTICS"
    assert "current_state_and_writer_identity_unknown" in report["candidate_interpretation"]
    assert "catalog_capture_timestamp_absent_from_schema" in report["blocked_reasons"]
    assert report["runtime_static_crosswalk_status"] == "VERIFIED_PATH_ANTI_JOIN"
    assert report["runtime_static_integrity_issues"] == []
    assert report["migration_plan_governance_issues"] == ["target_allowlist_missing"]
    assert "migration_plan_governance_incomplete" in report["blocked_reasons"]
    assert report["freshness"]["static_catalog"]["status"] == "UNKNOWN"
    assert report["digest_semantics"]["runtime_module_path_sha256"].find("not source bytes") >= 0


def test_same_script_path_used_by_multiple_runtime_tasks_is_ambiguous() -> None:
    runtime, catalog, host = _snapshots(task_count=2)

    candidate = _reconcile(runtime, catalog, host)["host_candidates"][0]

    assert candidate["mapping_status"] == "AMBIGUOUS_PATH_CANDIDATE"
    assert candidate["runtime_task_candidates"] == ["scheduled:1", "scheduled:2"]
    assert candidate["writer_status"] == "UNKNOWN"


def test_partial_host_section_blocks_even_when_path_evidence_matches() -> None:
    runtime, catalog, host = _snapshots()
    host["processes"]["status"] = "PARTIAL"
    host["processes"]["error_counts"] = {"process_raced": 1}

    report = _reconcile(runtime, catalog, host)

    assert "host_processes_partial" in report["blocked_reasons"]
    assert "host_processes_reports_errors" in report["blocked_reasons"]
    assert report["host_candidates"][0]["mapping_status"] == "EXACT_PATH_CANDIDATE"
    assert report["reconciliation_status"] == "BLOCKED"


def test_stale_runtime_and_host_snapshots_never_appear_current() -> None:
    runtime, catalog, host = _snapshots()
    runtime["generated_at_utc"] = "2026-10-27T00:00:00Z"
    host["observed_at_utc"] = "2026-10-27T00:00:00Z"

    report = _reconcile(runtime, catalog, host)

    assert report["freshness"]["runtime_inventory"]["status"] == "STALE"
    assert report["freshness"]["host_inventory"]["status"] == "STALE"
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"
    assert report["migration_plan_integrity_issues"] == ["target_allowlist_missing"]
    assert report["host_candidates"][0]["mapping_status"] == "CROSSWALK_INPUTS_UNVERIFIED"
    assert report["reconciliation_status"] == "BLOCKED"


def test_source_root_mismatch_keeps_host_join_unverified() -> None:
    runtime, catalog, host = _snapshots()
    catalog["source_root"] = "/projects/other-afi"

    report = _reconcile(runtime, catalog, host)

    assert "runtime_static_source_root_mismatch" in report["blocked_reasons"]
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"
    assert report["host_candidates"][0]["mapping_status"] == "CROSSWALK_INPUTS_UNVERIFIED"
    assert (
        report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"]
        == "UNCOMPARABLE_CROSSWALK_INPUTS"
    )


def test_unsafe_runtime_path_is_reported_without_echoing_raw_path() -> None:
    runtime, catalog, host = _snapshots()
    runtime["tasks"][0]["script_path"] = "../outside.py"
    runtime["tasks"][0]["path_status"] = "UNMAPPED"

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_path_review_status"] == "PARTIAL"
    assert report["runtime_paths_unresolved"] == [
        {
            "task_id": "scheduled:1",
            "script_id": SCRIPT_ID,
            "source_path": None,
            "path_status": "PATH_UNKNOWN_OR_UNSAFE",
            "static_source_sha256": None,
        }
    ]
    assert "runtime_path_unresolved" in report["blocked_reasons"]
    assert "../outside.py" not in repr(report)


def test_invalid_host_records_make_negative_observations_partial() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"] = "malformed"

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )
    assert "systemd_user_rows_invalid_or_too_large" in report["blocked_reasons"]


def test_token_like_systemd_unit_is_not_echoed() -> None:
    runtime, catalog, host = _snapshots()
    token_like = "sk-" + "A" * 40
    host["systemd_user"]["records"][0]["unit"] = token_like + ".service"
    github_token_like = "ghp_" + "G" * 24
    second_record = dict(host["systemd_user"]["records"][0])
    second_record["unit"] = github_token_like + ".service"
    host["systemd_user"]["records"].append(second_record)

    report = _reconcile(runtime, catalog, host)

    assert "host_systemd_unit_identity_invalid" in report["blocked_reasons"]
    assert token_like not in repr(report)
    assert github_token_like not in repr(report)


def test_unhashable_host_project_is_rejected_without_exception() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = [
        {"project": [], "path": SOURCE_PATH}
    ]

    report = _reconcile(runtime, catalog, host)

    assert "host_script_path_invalid" in report["blocked_reasons"]
    assert report["host_candidates"][0]["host_path"] is None
    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )


def test_candidate_expansion_stops_at_the_global_row_limit(monkeypatch) -> None:
    runtime, catalog, host = _snapshots()
    host["crontab"]["records"] = [
        {
            "line_number": 1,
            "line_sha256": "1" * 64,
            "command_sha256": "2" * 64,
            "script_paths": [{"project": "afi", "path": "data_fetch/other.py"}],
        }
    ]
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 1)

    report = _reconcile(runtime, catalog, host)

    assert len(report["host_candidates"]) == 1
    assert "host_candidate_limit_exceeded" in report["blocked_reasons"]


def test_unobserved_paths_are_scoped_absence_candidates_not_completeness_claims() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"] = []

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_paths_not_observed_in_host_scope"] == [
        {
            "task_id": "scheduled:1",
            "script_id": SCRIPT_ID,
            "source_path": SOURCE_PATH,
            "static_source_sha256": SOURCE_HASH,
            "crosswalk_status": "VERIFIED_PATH_ANTI_JOIN",
            "observation_status": "NOT_OBSERVED_WITHIN_HOST_INVENTORY_SCOPE",
        }
    ]
    assert report["static_paths_not_observed_in_host_scope"][0]["source_path"] == SOURCE_PATH
    assert (
        report["static_paths_not_observed_in_host_scope"][0]["observation_status"]
        == "NOT_OBSERVED_WITHIN_HOST_INVENTORY_SCOPE"
    )
    assert report["reconciliation_status"] == "BLOCKED"


def test_missing_freshness_context_is_unknown_and_has_no_clock_fallback() -> None:
    runtime, catalog, host = _snapshots()

    report = build_host_runtime_static_reconciliation(
        runtime,
        catalog,
        host,
        as_of_utc=None,
        max_snapshot_age_seconds=None,
    )

    assert report["freshness"]["runtime_inventory"]["status"] == "UNKNOWN"
    assert report["freshness"]["host_inventory"]["status"] == "UNKNOWN"
    assert "explicit_as_of_utc_missing_or_invalid" in report["blocked_reasons"]
    assert "explicit_snapshot_age_policy_missing_or_invalid" in report["blocked_reasons"]


def test_host_path_outside_afi_catalog_and_pathless_units_remain_unmapped() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"].append(
        {
            "unit": "opendata-refresh.service",
            "unit_type": "service",
            "active": False,
            "active_state": "inactive",
            "installed": True,
            "fragment_project": "opendata",
            "working_directory_project": "opendata",
            "related_via_service": None,
            "exec": {"command_sha256": "1" * 64, "script_paths": []},
        }
    )
    host["systemd_user"]["records"].append(
        {
            "unit": "unknown.service",
            "unit_type": "service",
            "active": False,
            "active_state": "inactive",
            "installed": True,
            "fragment_project": "afi",
            "working_directory_project": "afi",
            "related_via_service": None,
            "exec": {
                "command_sha256": "2" * 64,
                "script_paths": [{"project": "opendata", "path": "scripts/run.py"}],
            },
        }
    )

    report = _reconcile(runtime, catalog, host)
    candidates = report["host_candidates"]

    assert any(item["mapping_status"] == "HOST_SCRIPT_PATH_MISSING" for item in candidates)
    assert any(
        item["mapping_status"] == "HOST_PATH_OUTSIDE_LEGACY_CROSSWALK" for item in candidates
    )


def test_old_runtime_schema_and_pid_values_cannot_be_promoted_to_current_identity() -> None:
    runtime, catalog, host = _snapshots()
    runtime["schema_version"] = "1.0"
    host["processes"]["records"] = [
        {
            "process_id": 4242,
            "command_sha256": "3" * 64,
            "command_hash_is_prefix": False,
            "cwd_project": "afi",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        },
        {
            "process_id": 4243,
            "command_sha256": "3" * 64,
            "command_hash_is_prefix": False,
            "cwd_project": "opendata",
            "script_paths": [{"project": "opendata", "path": "scripts/worker.py"}],
        },
    ]

    report = _reconcile(runtime, catalog, host)

    assert report["inputs"]["runtime_inventory_schema_version"] == "1.0"
    assert "runtime_inventory_schema_not_current" in report["blocked_reasons"]
    assert "host_process_command_hash_ambiguous" in report["blocked_reasons"]
    assert all("process_id" not in candidate for candidate in report["host_candidates"])
    assert all(candidate["writer_status"] == "UNKNOWN" for candidate in report["host_candidates"])
    assert all(
        candidate["host_record_identity_status"] == "AMBIGUOUS_DUPLICATE_COMMAND_HASH"
        for candidate in report["host_candidates"]
        if candidate["host_source"] == "same_uid_process"
    )


def test_invalid_schema_values_and_noncanonical_host_input_are_explicit() -> None:
    runtime, catalog, host = _snapshots()
    runtime["schema_version"] = []
    catalog["schema_version"] = {"version": 1}
    host["schema_version"] = 2
    host["non_json_value"] = float("nan")
    catalog["generated_at_utc"] = "2026-10-30T00:00:00Z"

    report = _reconcile(runtime, catalog, host)

    assert report["inputs"]["runtime_inventory_schema_version"] is None
    assert report["inputs"]["static_catalog_schema_version"] is None
    assert report["inputs"]["host_inventory_schema_version"] is None
    assert report["inputs"]["host_inventory_sha256"] is None
    assert "host_inventory_not_canonical_json" in report["blocked_reasons"]
    assert "runtime_inventory_schema_not_current" in report["blocked_reasons"]
    assert "static_catalog_schema_unsupported" in report["blocked_reasons"]
    assert "host_inventory_schema_unsupported" in report["blocked_reasons"]
    assert report["freshness"]["static_catalog"]["unrecognized_generated_at_utc_field_present"]


@pytest.mark.parametrize(
    ("capture_time", "expected"),
    [
        ("2026-10-30T01:00:00Z", "FUTURE"),
        ("2026-10-29T00:00:00", "UNKNOWN"),
        ("not-a-time", "UNKNOWN"),
    ],
)
def test_runtime_freshness_never_accepts_future_or_naive_times(
    capture_time: str, expected: str
) -> None:
    runtime, catalog, host = _snapshots()
    runtime["generated_at_utc"] = capture_time

    report = _reconcile(runtime, catalog, host)

    assert report["freshness"]["runtime_inventory"]["status"] == expected
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"


@pytest.mark.parametrize("age_limit", [True, -1, 31_536_001, 1.5])
def test_invalid_freshness_policy_is_unknown(age_limit: object) -> None:
    runtime, catalog, host = _snapshots()

    report = build_host_runtime_static_reconciliation(
        runtime,
        catalog,
        host,
        as_of_utc="2026-10-30T00:00:00Z",
        max_snapshot_age_seconds=age_limit,  # type: ignore[arg-type]
    )

    assert report["freshness"]["max_snapshot_age_seconds"] is None
    assert report["freshness"]["runtime_inventory"]["status"] == "UNKNOWN"
    assert "explicit_snapshot_age_policy_missing_or_invalid" in report["blocked_reasons"]


@pytest.mark.parametrize(
    ("root_state", "expected"),
    [
        ("unavailable", "UNAVAILABLE"),
        ("missing_digest", "UNKNOWN"),
        ("mismatch", "MISMATCH"),
    ],
)
def test_host_root_pins_require_two_available_exact_digests(root_state: str, expected: str) -> None:
    runtime, catalog, host = _snapshots()
    opendata_root = host["scope"]["project_roots"]["opendata"]
    if root_state == "unavailable":
        opendata_root["available"] = False
    elif root_state == "missing_digest":
        opendata_root["root_path_sha256"] = None
    else:
        opendata_root["root_path_sha256"] = "0" * 64

    report = _reconcile(runtime, catalog, host)

    assert report["host_project_root_pin_status"] == expected
    assert f"host_project_root_pin_{expected.lower()}" in report["blocked_reasons"]


@pytest.mark.parametrize("planner_behavior", ["returns_non_mapping", "raises_contract_error"])
def test_planner_contract_failure_keeps_all_joins_unverified(
    monkeypatch: pytest.MonkeyPatch, planner_behavior: str
) -> None:
    runtime, catalog, host = _snapshots()

    def broken_planner(*_args: object, **_kwargs: object) -> object:
        if planner_behavior == "raises_contract_error":
            raise MigrationPlanError("synthetic planner rejection")
        return []

    monkeypatch.setattr(reconciliation, "build_migration_plan", broken_planner)

    report = _reconcile(runtime, catalog, host)

    assert "runtime_static_crosswalk_contract_invalid" in report["blocked_reasons"]
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"
    assert report["runtime_path_review_status"] == "UNKNOWN"
    assert "runtime_path_review_inputs_unavailable" in report["blocked_reasons"]
    assert report["host_candidates"][0]["mapping_status"] == "CROSSWALK_INPUTS_UNVERIFIED"


def test_planner_row_defenses_keep_unsafe_ids_and_paths_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    runtime["metadata"]["data_scripts"]["records"] = "malformed scripts"
    unsafe_secret = "sk-" + "B" * 40
    fake_plan = {
        "integrity_issues": ["target_allowlist_missing", "BAD CODE", 9],
        "tasks": [
            {
                "task_id": "scheduled:1",
                "script_id": SCRIPT_ID,
                "script_path": SOURCE_PATH,
                "script_sha256": SOURCE_HASH,
                "path_status": "PATH_CANDIDATE",
                "blocked_reasons": [],
            },
            {
                "task_id": unsafe_secret,
                "script_id": unsafe_secret,
                "script_path": "../secret.py",
                "script_sha256": "invalid",
                "path_status": "UNMAPPED",
                "blocked_reasons": ["runtime_script_path_unresolved"],
            },
            {
                "task_id": "scheduled:2",
                "script_id": SCRIPT_ID,
                "script_path": "data_fetch/blocked.py",
                "script_sha256": None,
                "path_status": "UNMAPPED",
                "blocked_reasons": ["runtime_script_path_unresolved"],
            },
        ],
        "helpers": [
            {
                "task_id": "script:helper_job",
                "script_id": "helper_job",
                "script_path": "data_fetch/helper.py",
                "script_sha256": None,
                "path_status": "PATH_CANDIDATE",
                "blocked_reasons": [],
            }
        ],
    }
    monkeypatch.setattr(reconciliation, "build_migration_plan", lambda *_args, **_kwargs: fake_plan)

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_static_integrity_issues"] == []
    assert report["migration_plan_governance_issues"] == ["target_allowlist_missing"]
    assert report["migration_plan_integrity_issues"] == ["target_allowlist_missing"]
    assert "runtime_scripts_rows_invalid_or_too_large" in report["blocked_reasons"]
    assert report["runtime_path_review_status"] == "PARTIAL"
    assert any(
        item["path_status"] == "PATH_UNKNOWN_OR_UNSAFE"
        and item["task_id"] is None
        and item["script_id"] is None
        for item in report["runtime_paths_unresolved"]
    )
    assert any(
        item["path_status"] == "PATH_STATUS_UNRESOLVED"
        for item in report["runtime_paths_unresolved"]
    )
    assert unsafe_secret not in repr(report)


def test_systemd_duplicate_units_and_bad_digests_remain_ambiguous_and_sanitized() -> None:
    runtime, catalog, host = _snapshots()
    duplicate = dict(host["systemd_user"]["records"][0])
    duplicate["exec"] = dict(duplicate["exec"])
    duplicate["exec"]["command_sha256"] = "not-a-digest"
    duplicate["related_via_service"] = "not a unit"
    duplicate["unit_type"] = "socket"
    host["systemd_user"]["records"].append(duplicate)

    report = _reconcile(runtime, catalog, host)
    sanitized_duplicate = next(
        item
        for item in report["host_candidates"]
        if item["host_source"] == "systemd_user" and item["host_command_sha256"] is None
    )

    assert "host_duplicate_systemd_unit_identity" in report["blocked_reasons"]
    assert "host_systemd_command_digest_unknown" in report["blocked_reasons"]
    assert sanitized_duplicate["host_record_identity_status"] == "AMBIGUOUS_DUPLICATE_UNIT"
    assert sanitized_duplicate["related_via_service"] is None
    assert sanitized_duplicate["unit_type"] is None


def test_crontab_identity_and_schedule_validation_preserve_only_safe_fields() -> None:
    runtime, catalog, host = _snapshots()
    host["crontab"]["records"] = [
        {
            "line_number": 9,
            "line_sha256": "4" * 64,
            "command_sha256": "5" * 64,
            "schedule": "@daily",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        },
        {
            "line_number": 9,
            "line_sha256": "6" * 64,
            "command_sha256": None,
            "schedule": "run arbitrary command",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        },
        {
            "line_number": True,
            "line_sha256": "invalid",
            "command_sha256": "7" * 64,
            "schedule": "0 0 * * *",
            "script_paths": [],
        },
        {
            "line_number": 0,
            "line_sha256": "8" * 64,
            "command_sha256": "7" * 64,
            "schedule": "0 0 * * *",
            "script_paths": [],
        },
    ]

    report = _reconcile(runtime, catalog, host)
    cron_candidates = [
        item for item in report["host_candidates"] if item["host_source"] == "crontab"
    ]

    assert len(cron_candidates) == 2
    assert all(
        item["host_record_identity_status"] == "AMBIGUOUS_DUPLICATE_LINE_NUMBER"
        for item in cron_candidates
    )
    assert {item["schedule"] for item in cron_candidates} == {"@daily", None}
    assert "host_duplicate_crontab_line_identity" in report["blocked_reasons"]
    assert "host_crontab_command_digest_unknown" in report["blocked_reasons"]
    assert "host_crontab_record_identity_invalid" in report["blocked_reasons"]

    statuses_by_hash = {
        item["host_line_sha256"]: item["host_record_identity_status"] for item in cron_candidates
    }
    host["crontab"]["records"].reverse()
    reordered_report = _reconcile(runtime, catalog, host)
    reordered_statuses_by_hash = {
        item["host_line_sha256"]: item["host_record_identity_status"]
        for item in reordered_report["host_candidates"]
        if item["host_source"] == "crontab"
    }
    assert reordered_statuses_by_hash == statuses_by_hash


def test_process_grouping_uses_only_digest_path_and_coarse_project() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"] = []
    host["processes"]["records"] = [
        {
            "process_id": 111,
            "command_sha256": "8" * 64,
            "command_hash_is_prefix": False,
            "cwd_project": "afi",
            "classification": "owned_test_infrastructure",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        },
        {
            "process_id": 222,
            "command_sha256": "8" * 64,
            "command_hash_is_prefix": True,
            "cwd_project": "afi",
            "classification": "project_related_unclassified",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        },
        {
            "process_id": 333,
            "command_sha256": None,
            "command_hash_is_prefix": True,
            "cwd_project": "unknown",
            "classification": "not-an-enum",
            "script_paths": [],
        },
        {
            "process_id": 444,
            "command_sha256": "9" * 64,
            "command_hash_is_prefix": False,
            "cwd_project": "opendata",
            "classification": "not-an-enum",
            "script_paths": [{"project": "opendata", "path": "scripts/worker.py"}],
        },
    ]

    report = _reconcile(runtime, catalog, host)
    process_candidates = [
        item for item in report["host_candidates"] if item["host_source"] == "same_uid_process"
    ]
    grouped = next(item for item in process_candidates if item["host_command_sha256"] == "8" * 64)
    unidentified = next(item for item in process_candidates if item["host_command_sha256"] is None)

    assert grouped["observation_count"] == 2
    assert grouped["host_record_identity_status"] == "AMBIGUOUS_DUPLICATE_COMMAND_HASH"
    assert grouped["host_command_sha256_is_prefix"] is True
    assert grouped["classification"] is None
    assert grouped["classification_status"] == "AMBIGUOUS_CONFLICT"
    assert "host_process_classification_ambiguous" in report["blocked_reasons"]
    assert unidentified["host_record_identity_status"] == "UNKNOWN_COMMAND_HASH"
    assert unidentified["host_path"] is None
    assert unidentified["classification"] is None
    assert unidentified["classification_status"] == "UNKNOWN"
    assert "host_process_command_hash_ambiguous" in report["blocked_reasons"]
    assert "host_process_command_digest_unknown" in report["blocked_reasons"]
    assert all("process_id" not in candidate for candidate in process_candidates)

    reversed_runtime, reversed_catalog, reversed_host = _snapshots()
    reversed_host["systemd_user"]["records"] = []
    reversed_host["processes"]["records"] = list(reversed(host["processes"]["records"]))
    reversed_report = _reconcile(reversed_runtime, reversed_catalog, reversed_host)
    reversed_group = next(
        item
        for item in reversed_report["host_candidates"]
        if item["host_source"] == "same_uid_process" and item["host_command_sha256"] == "8" * 64
    )
    assert (
        grouped["classification"],
        grouped["classification_status"],
        grouped["host_record_identity_status"],
    ) == (
        reversed_group["classification"],
        reversed_group["classification_status"],
        reversed_group["host_record_identity_status"],
    )


def test_candidate_mapping_reports_static_path_missing_from_catalog() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = [
        {"project": "afi", "path": "data_fetch/missing.py"},
        {"project": "afi", "path": SOURCE_PATH},
    ]

    report = _reconcile(runtime, catalog, host)

    statuses = {item["mapping_status"] for item in report["host_candidates"]}
    assert "STATIC_PATH_MISSING" in statuses
    assert "host_static_path_unmatched" in report["blocked_reasons"]


def test_duplicate_static_paths_remain_an_ambiguous_candidate() -> None:
    runtime, catalog, host = _snapshots()
    catalog["files"].append(dict(catalog["files"][0]))

    report = _reconcile(runtime, catalog, host)

    assert report["host_candidates"][0]["mapping_status"] == "AMBIGUOUS_PATH_CANDIDATE"
    assert "host_path_join_ambiguous" in report["blocked_reasons"]
    assert "runtime_static_catalog_duplicate_source_path" in report["blocked_reasons"]


def test_host_rows_with_duplicate_paths_are_deduplicated_but_not_marked_complete() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = [
        {"project": "afi", "path": SOURCE_PATH},
        {"project": "afi", "path": SOURCE_PATH},
    ]

    report = _reconcile(runtime, catalog, host)

    assert len(report["host_candidates"]) == 1
    assert report["host_candidates"][0]["mapping_status"] == "EXACT_PATH_CANDIDATE"
    assert report["runtime_paths_not_observed_in_host_scope"] == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("data/jobs/refresh.py", True),
        ("../outside.py", False),
        ("/absolute.py", False),
        ("data\\jobs\\refresh.py", False),
        ("data/./refresh.py", False),
        ("data/refresh\n.py", False),
        ("sk-" + "C" * 32 + ".py", False),
    ],
)
def test_relative_path_normalizer_rejects_noncanonical_and_sensitive_paths(
    value: str, expected: bool
) -> None:
    assert reconciliation._safe_relative_path(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/projects/opendata", True),
        ("/projects/../opendata", False),
        ("projects/opendata", False),
        ("/projects\\opendata", False),
        ("/projects/\x01bad", False),
        (None, False),
    ],
)
def test_absolute_path_normalizer_requires_canonical_project_roots(
    value: object, expected: bool
) -> None:
    assert reconciliation._safe_absolute_path(value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@daily", "@daily"),
        ("0 16 * * 1-5", "0 16 * * 1-5"),
        ("60 0 * * *", "60 0 * * *"),
        ("run; rm -rf /", None),
        ("@every-minute", None),
        ("sk-" + "D" * 32, None),
        ("x" * 81, None),
    ],
)
def test_cron_schedule_filter_keeps_only_safe_bounded_text(
    value: str, expected: str | None
) -> None:
    assert reconciliation._safe_cron_schedule(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-30T00:00:00Z", True),
        ("2026-10-30T08:00:00+08:00", True),
        ("2026-10-30T00:00:00", False),
        ("bad", False),
        (None, False),
    ],
)
def test_timestamp_parser_requires_explicit_aware_time(value: object, expected: bool) -> None:
    assert (reconciliation._parse_aware_utc(value) is not None) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (31_536_000, 31_536_000), (-1, None), (True, None), (31_536_001, None), (1.0, None)],
)
def test_age_policy_accepts_only_bounded_nonboolean_integers(
    value: object, expected: int | None
) -> None:
    assert reconciliation._valid_age_limit(value) == expected


def test_freshness_classification_uses_explicit_policy_boundaries() -> None:
    as_of = datetime(2026, 10, 30, tzinfo=timezone.utc)

    assert reconciliation._freshness(as_of, as_of, 0)["status"] == "FRESH_WITHIN_POLICY"
    assert reconciliation._freshness("2026-10-30T00:00:00Z", as_of, 0)["status"] == (
        "FRESH_WITHIN_POLICY"
    )
    assert reconciliation._freshness("2026-10-29T23:59:59Z", as_of, 0)["status"] == "STALE"
    assert reconciliation._freshness("2026-10-30T00:00:01Z", as_of, 0)["status"] == "FUTURE"
    assert reconciliation._freshness("2026-10-30T00:00:00", as_of, 0)["status"] == "UNKNOWN"


def test_row_and_mapping_normalizers_fail_closed_without_echoing_values() -> None:
    blockers: set[str] = set()
    assert reconciliation._object_rows([{"a": 1}], blockers, "demo") == [{"a": 1}]
    assert reconciliation._object_rows("not rows", blockers, "demo") is None
    assert reconciliation._object_rows([{}, None], blockers, "bad") is None
    assert "demo_rows_invalid_or_too_large" in blockers
    assert "bad_rows_invalid_or_too_large" in blockers
    assert reconciliation._mapping({"safe": True}) == {"safe": True}
    assert reconciliation._mapping({1: "not a string key"}) == {}
    assert reconciliation._safe_sha256("a" * 64) == "a" * 64
    assert reconciliation._safe_sha256("A" * 64) is None
    assert reconciliation._safe_schema_version("2.0") == "2.0"
    assert reconciliation._safe_schema_version("v2") is None
    assert reconciliation._safe_string_list(["safe_issue", "UPPER", "x" * 97, 2]) == ["safe_issue"]


def test_row_limits_resolve_current_global_policy_and_keep_path_limit_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 1)
    blockers: set[str] = set()

    assert reconciliation._object_rows([{}], blockers, "within_limit") == [{}]
    assert reconciliation._object_rows([{}, {}], blockers, "global_limit") is None
    assert "global_limit_rows_invalid_or_too_large" in blockers

    path_blockers: set[str] = set()
    paths = reconciliation._safe_host_paths(
        [
            {"project": "afi", "path": "data/one.py"},
            {"project": "afi", "path": "data/two.py"},
        ],
        path_blockers,
        max_paths=1,
    )
    assert paths == [("afi", "data/one.py")]
    assert "host_candidate_limit_exceeded" in path_blockers


def test_safe_text_and_fixed_enum_filters_do_not_persist_secret_like_input() -> None:
    secret = "ghp_" + "E" * 24

    assert reconciliation._safe_text("brief operator note", max_length=24) == "brief operator note"
    assert reconciliation._safe_text(secret, max_length=80) is None
    assert reconciliation._safe_text("line\nfeed", max_length=80) is None
    assert reconciliation._safe_text("x" * 25, max_length=24) is None
    assert reconciliation._safe_enum("service", {"service", "timer"}) == "service"
    assert reconciliation._safe_enum("socket", {"service", "timer"}) is None
    assert reconciliation._safe_state("active") == "active"
    assert reconciliation._safe_state("unknown-new-state") is None
    assert reconciliation._safe_systemd_target("related.timer") == "related.timer"
    assert reconciliation._safe_systemd_target(secret + ".service") is None
    assert reconciliation._safe_project("afi") == "afi"
    assert reconciliation._safe_project("other") is None
    assert reconciliation._project_name("afi", None) == "afi"
    assert reconciliation._project_name("afi", "opendata") is None


def test_host_section_errors_and_missing_rows_force_partial_negative_observations() -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["errors"] = {"unit_scan": "unavailable"}
    # The default fixture positively observes SOURCE_PATH through systemd. Remove
    # that evidence so this case tests a partial-scope negative observation.
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = []
    host["crontab"].pop("records")
    host["processes"]["error_counts"] = ["process_raced"]

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_static_crosswalk_status"] == "VERIFIED_PATH_ANTI_JOIN"
    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )
    assert "host_systemd_user_reports_errors" in report["blocked_reasons"]
    # Row schema blockers use the exact section label passed to _object_rows.
    assert "crontab_rows_invalid_or_too_large" in report["blocked_reasons"]
    assert "host_processes_reports_errors" in report["blocked_reasons"]


def test_unsupported_host_schema_cannot_support_complete_absence_claims() -> None:
    runtime, catalog, host = _snapshots()
    host["schema_version"] = "2.0"
    host["systemd_user"]["records"] = []

    report = _reconcile(runtime, catalog, host)

    assert report["runtime_static_crosswalk_status"] == "VERIFIED_PATH_ANTI_JOIN"
    assert "host_inventory_schema_unsupported" in report["blocked_reasons"]
    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )


def test_host_scope_must_disclose_read_boundaries_and_root_pins() -> None:
    runtime, catalog, host = _snapshots()
    host["scope"]["filesystem_scope"] = "unspecified"
    host["scope"]["environment_values_read"] = True
    host["scope"]["sensitive_material_files_read"] = None
    host["scope"].pop("project_roots")

    report = _reconcile(runtime, catalog, host)

    assert "host_filesystem_scope_unknown" in report["blocked_reasons"]
    assert "host_scope_environment_values_read_not_false" in report["blocked_reasons"]
    assert "host_scope_sensitive_material_files_read_not_false" in report["blocked_reasons"]
    assert "host_project_roots_missing" in report["blocked_reasons"]
    assert report["host_project_root_pin_status"] == "UNAVAILABLE"
    assert "host_project_root_pin_unavailable" in report["blocked_reasons"]


def test_unencodable_root_path_stays_unknown_and_fails_closed() -> None:
    runtime, catalog, host = _snapshots()
    runtime["source_root"] = "/projects/\udcff"
    catalog["source_root"] = runtime["source_root"]

    report = _reconcile(runtime, catalog, host)

    assert report["host_project_root_pin_status"] == "UNKNOWN"
    assert "host_project_root_pin_unknown" in report["blocked_reasons"]
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"


def test_static_catalog_invalid_and_duplicate_rows_are_not_silently_joined() -> None:
    runtime, catalog, host = _snapshots()
    catalog["files"].append({"source_path": "../unsafe.py", "source_sha256": SOURCE_HASH})
    catalog["files"].append({"source_path": SOURCE_PATH, "source_sha256": "invalid"})

    report = _reconcile(runtime, catalog, host)

    assert "static_catalog_source_identity_invalid" in report["blocked_reasons"]
    assert "runtime_static_catalog_source_identity_invalid" in report["blocked_reasons"]
    assert report["host_candidates"][0]["mapping_status"] == "CROSSWALK_INPUTS_UNVERIFIED"
    assert report["runtime_static_crosswalk_status"] == "UNVERIFIED_OR_BLOCKED"


def test_runtime_module_path_digest_absence_remains_unknown_not_source_hash() -> None:
    runtime, catalog, host = _snapshots()
    runtime["metadata"]["data_scripts"]["records"][0].pop("module_path_sha256")

    report = _reconcile(runtime, catalog, host)
    candidate = report["host_candidates"][0]

    assert candidate["mapping_status"] == "EXACT_PATH_CANDIDATE"
    assert candidate["runtime_module_path_sha256_candidates"] == []
    assert candidate["runtime_module_path_hash_status"] == "UNKNOWN"
    assert candidate["runtime_static_digest_comparison"] == "NOT_COMPARABLE_DIFFERENT_SEMANTICS"


def test_process_candidate_budget_blocks_before_expanding_unbounded_path_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"] = []
    host["processes"]["records"] = [
        {
            "command_sha256": "a" * 64,
            "cwd_project": "afi",
            "script_paths": [
                {"project": "afi", "path": "data_fetch/one.py"},
                {"project": "afi", "path": "data_fetch/two.py"},
            ],
        }
    ]
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 1)

    report = _reconcile(runtime, catalog, host)

    assert len(report["host_candidates"]) <= 1
    assert "host_candidate_limit_exceeded" in report["blocked_reasons"]
    assert [item["host_path"] for item in report["host_candidates"]] == ["data_fetch/one.py"]
    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )


def test_oversized_host_sections_are_not_treated_as_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"].append(dict(host["systemd_user"]["records"][0]))
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 1)

    report = _reconcile(runtime, catalog, host)

    assert report["host_candidates"] == []
    assert "systemd_user_rows_invalid_or_too_large" in report["blocked_reasons"]
    assert report["runtime_paths_not_observed_in_host_scope"][0]["observation_status"] == (
        "NOT_OBSERVED_WITHIN_PARTIAL_HOST_SCOPE"
    )


def test_systemd_candidate_budget_stops_before_processing_more_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    first = host["systemd_user"]["records"][0]
    first["exec"]["script_paths"] = [
        {"project": "afi", "path": "data_fetch/one.py"},
        {"project": "afi", "path": "data_fetch/two.py"},
    ]
    host["systemd_user"]["records"].append(dict(first))
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 2)

    report = _reconcile(runtime, catalog, host)

    assert len(report["host_candidates"]) == 2
    assert "host_candidate_limit_exceeded" in report["blocked_reasons"]
    assert all(item["writer_status"] == "UNKNOWN" for item in report["host_candidates"])


def test_process_collection_stops_after_systemd_reaches_global_row_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = [
        {"project": "afi", "path": "data_fetch/one.py"},
        {"project": "afi", "path": "data_fetch/two.py"},
    ]
    host["processes"]["records"] = [
        {
            "command_sha256": "b" * 64,
            "cwd_project": "afi",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        }
    ]
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 2)

    report = _reconcile(runtime, catalog, host)

    assert len(report["host_candidates"]) == 2
    assert all(item["host_source"] == "systemd_user" for item in report["host_candidates"])
    assert "host_candidate_limit_exceeded" in report["blocked_reasons"]
    assert "host_process_command_digest_unknown" not in report["blocked_reasons"]


def test_existing_limit_blocker_short_circuits_process_record_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    host["systemd_user"]["records"][0]["exec"]["script_paths"] = [
        {"project": "afi", "path": f"data_fetch/{index}.py"} for index in range(3)
    ]
    host["processes"]["records"] = [
        {
            "command_sha256": "c" * 64,
            "cwd_project": "afi",
            "script_paths": [{"project": "afi", "path": SOURCE_PATH}],
        }
    ]
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 2)

    report = _reconcile(runtime, catalog, host)

    assert "host_candidate_limit_exceeded" in report["blocked_reasons"]
    assert not any(item["host_source"] == "same_uid_process" for item in report["host_candidates"])


def test_host_path_list_limits_and_pathless_records_are_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blockers: set[str] = set()
    assert reconciliation._safe_host_paths("not a path list", blockers) == []
    assert "host_script_paths_rows_invalid_or_too_large" in blockers

    monkeypatch.setattr(reconciliation, "MAX_HOST_SCRIPT_PATHS", 1)
    assert (
        reconciliation._safe_host_paths(
            [
                {"project": "afi", "path": "data/one.py"},
                {"project": "afi", "path": "data/two.py"},
            ],
            blockers,
            max_paths=1,
        )
        == []
    )
    assert "host_script_path_count_exceeds_limit" in blockers


def test_path_append_limit_stops_mid_record_without_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(reconciliation, "MAX_RECONCILIATION_ROWS", 1)
    destination: list[dict] = []
    blockers: set[str] = set()
    reconciliation._append_host_paths(
        destination,
        {"host_source": "fixture"},
        [
            {"project": "afi", "path": "data/one.py"},
            {"project": "afi", "path": "data/two.py"},
        ],
        {},
        {},
        "VERIFIED_PATH_ANTI_JOIN",
        True,
        blockers,
    )
    assert len(destination) == 1
    assert destination[0]["host_path"] == "data/one.py"
    assert "host_candidate_limit_exceeded" in blockers

    reconciliation._append_host_paths(
        destination,
        {"host_source": "fixture"},
        [],
        {},
        {},
        "VERIFIED_PATH_ANTI_JOIN",
        True,
        blockers,
    )
    assert len(destination) == 1


def test_append_host_paths_classifies_missing_runtime_and_unknown_digest_evidence() -> None:
    destination: list[dict] = []
    blockers: set[str] = set()
    static_by_path = {SOURCE_PATH: [{"source_path": SOURCE_PATH, "source_sha256": SOURCE_HASH}]}
    common = {"host_source": "synthetic", "host_record_key": "synthetic:1"}
    reconciliation._append_host_paths(
        destination,
        common,
        [{"project": "afi", "path": SOURCE_PATH}],
        static_by_path,
        {},
        "VERIFIED_PATH_ANTI_JOIN",
        True,
        blockers,
    )
    assert destination[-1]["mapping_status"] == "RUNTIME_PATH_MISSING"
    assert "host_runtime_path_unmatched" in blockers

    destination.clear()
    runtime_by_path = {
        SOURCE_PATH: [
            {"task_id": "scheduled:1", "module_path_sha256": MODULE_PATH_HASH},
            {"task_id": "scheduled:2", "module_path_sha256": None},
        ]
    }
    reconciliation._append_host_paths(
        destination,
        common,
        [{"project": "afi", "path": SOURCE_PATH}],
        {SOURCE_PATH: [{"source_path": SOURCE_PATH, "source_sha256": "invalid"}]},
        runtime_by_path,
        "VERIFIED_PATH_ANTI_JOIN",
        True,
        blockers,
    )
    assert destination[-1]["mapping_status"] == "AMBIGUOUS_PATH_CANDIDATE"
    assert destination[-1]["runtime_module_path_hash_status"] == "PARTIAL"

    destination.clear()
    reconciliation._append_host_paths(
        destination,
        common,
        [{"project": "afi", "path": SOURCE_PATH}],
        {SOURCE_PATH: [{"source_path": SOURCE_PATH, "source_sha256": "invalid"}]},
        {SOURCE_PATH: [{"task_id": "scheduled:1", "module_path_sha256": None}]},
        "VERIFIED_PATH_ANTI_JOIN",
        True,
        blockers,
    )
    assert destination[-1]["mapping_status"] == "STATIC_SOURCE_HASH_UNKNOWN_OR_AMBIGUOUS"
    assert destination[-1]["runtime_module_path_hash_status"] == "UNKNOWN"
    assert "host_static_source_hash_unknown_or_ambiguous" in blockers


def test_malformed_fake_planner_collections_are_partial_and_not_joined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, catalog, host = _snapshots()
    planner_report = {"integrity_issues": [], "tasks": "not rows", "helpers": [None]}
    monkeypatch.setattr(
        reconciliation, "build_migration_plan", lambda *_args, **_kwargs: planner_report
    )

    report = _reconcile(runtime, catalog, host)

    assert "planner_tasks_rows_invalid_or_too_large" in report["blocked_reasons"]
    assert "planner_helpers_rows_invalid_or_too_large" in report["blocked_reasons"]
    assert report["runtime_path_review_status"] == "PARTIAL"
    assert report["runtime_paths_unresolved"] == []
    assert report["host_candidates"][0]["runtime_task_candidates"] == []
    assert report["host_candidates"][0]["mapping_status"] == "CROSSWALK_INPUTS_UNVERIFIED"
