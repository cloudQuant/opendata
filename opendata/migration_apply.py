"""Import reviewed migration plans as disabled, declarative job definitions.

This module does not import or execute legacy code, fetch data, create runs,
enable jobs, or read application settings. It only records one reviewed
``TaskDefinition`` and its hash-only audit receipt in an explicit isolated
control-metadata engine after recomputing all source pins and the live target
warehouse schema fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import stat as stat_module
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from weakref import WeakKeyDictionary

from pydantic import ValidationError
from sqlalchemy import create_engine as sqlalchemy_create_engine
from sqlalchemy import inspect, make_url, text
from sqlalchemy.engine import URL, Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool, Pool

from opendata.job_definition_store import (
    JOB_DEFINITION_TABLE,
    MAX_JOB_DEFINITION_LIST_ROWS,
    JobDefinitionStore,
    JobDefinitionStoreError,
    SaveJobDefinitionResult,
)
from opendata.migration_plan import (
    MAX_INPUT_BYTES,
    MAX_PLAN_BYTES,
    MigrationPlanError,
    WarehouseFingerprint,
    build_migration_plan,
    canonical_json_bytes,
    canonical_json_sha256,
)
from opendata.pipeline.run_identity import TaskDefinition

WAREHOUSE_FINGERPRINT_ALGORITHM = "opendata-warehouse-catalog-v1"
MAX_SQLITE_CATALOG_ROWS = 100_000
MAX_SQLITE_CATALOG_BYTES = 64 * 1024 * 1024
_TASK_ID_PATTERN = re.compile(r"^scheduled:[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_ISOLATED_METADATA_ENGINES: WeakKeyDictionary[Engine, tuple[URL, Pool]] = WeakKeyDictionary()


class MigrationApplyError(ValueError):
    """Raised when a reviewed offline plan cannot be safely imported."""


def create_isolated_metadata_engine(database_url: str) -> Engine:
    """Create a trusted, no-pool SQLite engine for isolated import metadata.

    The URL must identify a plain absolute SQLite file with no symlink in any
    existing path component. The file itself may not exist yet; creating or
    migrating its schema remains an explicit caller action. This factory uses
    SQLAlchemy's default SQLite connection creator and ``NullPool``. Do not
    add custom creators, pool hooks, or replace its pool before importing.

    Args:
        database_url: Plain ``sqlite:////absolute/path`` URL, without query
            options or credentials.

    Returns:
        A caller-owned engine accepted by the isolated prototype importer.

    Raises:
        MigrationApplyError: If the URL is ambiguous or its path is unsafe.
    """
    try:
        url = make_url(database_url)
    except (SQLAlchemyError, TypeError, ValueError) as exc:
        raise MigrationApplyError("metadata_sqlite_url_invalid") from exc
    if (
        url.drivername not in {"sqlite", "sqlite+pysqlite"}
        or url.database is None
        or url.query
        or url.host
        or url.username
        or url.password
        or str(url.database).casefold().startswith("file:")
    ):
        raise MigrationApplyError("metadata_sqlite_url_options_unsupported")
    path = Path(url.database)
    try:
        _validate_sqlite_path_components(path, allow_missing_final=True)
    except OSError as exc:
        raise MigrationApplyError("metadata_sqlite_path_unsafe") from exc
    engine = sqlalchemy_create_engine(url, poolclass=NullPool)
    _ISOLATED_METADATA_ENGINES[engine] = (engine.url, engine.pool)
    return engine


@dataclass(frozen=True, slots=True)
class WarehouseFingerprintObservation:
    """Actual fingerprint and physical database identity derived from a live engine."""

    sha256: str
    database_identity_sha256: str
    algorithm: str = WAREHOUSE_FINGERPRINT_ALGORITHM


@dataclass(frozen=True, slots=True)
class MigrationImportResult:
    """A stored disabled definition and its deterministic import receipt."""

    task_id: str
    definition: TaskDefinition
    created: bool
    receipt_sha256: str
    plan_sha256: str
    target_fingerprint_sha256: str


def inspect_warehouse_fingerprint(engine: Engine) -> WarehouseFingerprintObservation:
    """Compute a bounded fingerprint from a live, file-backed SQLite catalog.

    The digest binds the physical database identity to schema objects, columns,
    indexes, constraints, views, and triggers. It reads no business rows and
    never accepts a caller-provided fingerprint string. The SQLite file named
    by ``engine.url`` is opened by this module through a short-lived read-only
    connection; caller pool connections and custom connection creators are not
    used. The 64 MiB limit bounds the accepted encoded payload after catalog
    rows have been materialized, not peak transient memory. MySQL warehouse
    apply remains blocked until catalog visibility and grants can be proven.

    Args:
        engine: Engine whose plain absolute SQLite URL names the target file.
            Its caller-owned pool and connection creator are not used.

    Returns:
        The target's schema digest and a separate digest of its database identity.

    Raises:
        MigrationApplyError: If target identity is ambiguous, the dialect is
            unsupported, or the catalog is empty or unreadable.
    """
    if not isinstance(engine, Engine):
        raise TypeError("engine must be a synchronous SQLAlchemy Engine")
    if engine.dialect.name != "sqlite":
        raise MigrationApplyError("target_warehouse_catalog_completeness_unproven")
    if engine.url.query or engine.url.host or engine.url.username or engine.url.password:
        raise MigrationApplyError("target_sqlite_url_options_unsupported")
    target_path, expected_file_identity = _sqlite_engine_file_identity(engine)
    try:
        with closing(_open_readonly_sqlite(target_path)) as connection:
            identity = _sqlite_connection_identity(connection, target_path)
            if _physical_identity(identity) != expected_file_identity:
                raise MigrationApplyError("target_warehouse_file_identity_changed")
            catalog = _sqlite_catalog(connection)
            latest_identity = _sqlite_connection_identity(connection, target_path)
            if _physical_identity(latest_identity) != _physical_identity(identity):
                raise MigrationApplyError("target_warehouse_file_identity_changed")
            if _sqlite_engine_file_identity(engine)[1] != _physical_identity(identity):
                raise MigrationApplyError("target_warehouse_file_identity_changed")
    except MigrationApplyError:
        raise
    except (SQLAlchemyError, sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise MigrationApplyError("target_warehouse_catalog_unreadable") from exc
    if _sqlite_engine_file_identity(engine)[1] != _physical_identity(identity):
        raise MigrationApplyError("target_warehouse_file_identity_changed")

    encoded = canonical_json_bytes(
        {
            "algorithm": WAREHOUSE_FINGERPRINT_ALGORITHM,
            "database_identity": identity,
            "catalog": catalog,
        }
    )
    if len(encoded) > MAX_SQLITE_CATALOG_BYTES:
        raise MigrationApplyError("target_warehouse_catalog_exceeds_size_limit")
    if not catalog.get("tables"):
        raise MigrationApplyError("target_warehouse_has_no_user_tables")
    return WarehouseFingerprintObservation(
        sha256=hashlib.sha256(encoded).hexdigest(),
        database_identity_sha256=canonical_json_sha256(identity),
    )


def apply_reviewed_job_definition(
    *,
    metadata_engine: Engine,
    target_engine: Engine,
    plan: Mapping[str, object],
    runtime_inventory: Mapping[str, object],
    static_catalog: Mapping[str, object],
    reviewed_mapping: Mapping[str, object],
    target_allowlist: Mapping[str, object],
    task_id: str,
) -> MigrationImportResult:
    """Import exactly one reviewed plan entry as a disabled job definition.

    The caller must first create ``od_job_definitions`` on a dedicated SQLite
    metadata database using :func:`create_isolated_metadata_engine`. That
    database may contain only this table. The target must be a separate,
    pre-existing, absolute SQLite file. The target fingerprint describes the
    physical file named by ``target_engine.url`` and is read through this
    module's fresh read-only SQLite connection; it does not prove that a
    caller-supplied custom creator or pool would connect to that same file.
    The metadata engine must come from the safe factory, which uses the
    default SQLAlchemy creator and ``NullPool``. MySQL target apply remains
    blocked until complete catalog visibility is proven.

    Every JSON input is copied and bounded, all hashes and the complete plan
    are recomputed, and the target schema is checked again inside the metadata
    transaction before it commits.

    No input parameter values, executable paths, run rows, checkpoints, or
    enablement state are persisted. Idempotent replays return the existing
    row; any identity, provenance, plan, or target drift is refused.

    Args:
        metadata_engine: Explicit isolated engine containing the definition table.
        target_engine: Explicit engine for the warehouse named by the allowlist.
        plan: Frozen offline plan generated from the following full input snapshots.
        runtime_inventory: Full hash-pinned runtime inventory JSON object.
        static_catalog: Full hash-pinned static migration catalog JSON object.
        reviewed_mapping: Full reviewer-approved per-task mapping JSON object.
        target_allowlist: Full explicit target and warehouse-fingerprint allowlist.
        task_id: One scheduled task ID present in the frozen plan.

    Returns:
        The persisted disabled definition and an idempotent import receipt.

    Raises:
        MigrationApplyError: If any input pin, review, destination, or safety
            condition does not match the frozen plan and live databases.
        JobDefinitionConflictError: If the same legacy task key was imported
            with changed identity or provenance.
    """
    if not isinstance(metadata_engine, Engine) or not isinstance(target_engine, Engine):
        raise TypeError("metadata_engine and target_engine must be SQLAlchemy Engines")
    if not _TASK_ID_PATTERN.fullmatch(task_id):
        raise MigrationApplyError("scheduled_task_id_invalid")

    frozen_plan = _copy_json_object(plan, label="plan", byte_limit=MAX_PLAN_BYTES)
    inventory = _copy_json_object(
        runtime_inventory, label="runtime_inventory", byte_limit=MAX_INPUT_BYTES
    )
    catalog = _copy_json_object(static_catalog, label="static_catalog", byte_limit=MAX_INPUT_BYTES)
    mapping = _copy_json_object(
        reviewed_mapping, label="reviewed_mapping", byte_limit=MAX_INPUT_BYTES
    )
    allowlist = _copy_json_object(
        target_allowlist, label="target_allowlist", byte_limit=MAX_INPUT_BYTES
    )
    _verify_plan_self_hash(frozen_plan)

    actual = inspect_warehouse_fingerprint(target_engine)
    metadata_identity = _inspect_database_identity(metadata_engine)
    if metadata_identity == actual.database_identity_sha256:
        raise MigrationApplyError("metadata_and_target_engines_address_same_database")
    _require_isolated_metadata_schema(metadata_engine)

    try:
        expected_plan = build_migration_plan(
            inventory,
            catalog,
            mapping,
            warehouse_fingerprint=WarehouseFingerprint(actual.sha256),
            target_allowlist=allowlist,
        )
    except (MigrationPlanError, TypeError, ValueError, RecursionError) as exc:
        raise MigrationApplyError("migration_plan_inputs_failed_revalidation") from exc
    if canonical_json_bytes(frozen_plan) != canonical_json_bytes(expected_plan):
        raise MigrationApplyError("migration_plan_or_input_hash_drift")

    job = _single_task(expected_plan, task_id)
    task_mapping = _find_reviewed_mapping(mapping, task_id)
    definition = _build_task_definition(job)
    provenance = _build_provenance(
        task_id=task_id,
        job=job,
        mapping_document=mapping,
        mapping_row=task_mapping,
        plan=expected_plan,
        actual=actual,
        metadata_identity_sha256=metadata_identity,
    )
    definition_payload = definition.model_dump(mode="json")
    identity_sha256 = canonical_json_sha256(definition_payload)
    provenance_sha256 = canonical_json_sha256(provenance)
    receipt_sha256 = canonical_json_sha256(
        {
            "schema_version": 1,
            "legacy_task_id": task_id,
            "identity_sha256": identity_sha256,
            "provenance_sha256": provenance_sha256,
        }
    )

    store = JobDefinitionStore(metadata_engine)

    def verify_target_unchanged() -> None:
        """Abort if either pinned database changed before metadata commit."""
        latest = inspect_warehouse_fingerprint(target_engine)
        if latest != actual:
            raise MigrationApplyError("target_warehouse_changed_during_metadata_import")
        if _inspect_database_identity(metadata_engine) != metadata_identity:
            raise MigrationApplyError("metadata_database_changed_during_import")

    try:
        saved: SaveJobDefinitionResult = store._save_reviewed_definition(
            legacy_task_id=task_id,
            definition=definition,
            provenance=provenance,
            receipt_sha256=receipt_sha256,
            verify_target_unchanged=verify_target_unchanged,
        )
    except MigrationApplyError:
        raise
    except JobDefinitionStoreError:
        raise
    except SQLAlchemyError as exc:
        raise MigrationApplyError("job_definition_metadata_write_failed") from exc
    return MigrationImportResult(
        task_id=task_id,
        definition=saved.record.definition,
        created=saved.created,
        receipt_sha256=saved.record.receipt_sha256,
        plan_sha256=str(expected_plan["plan_sha256"]),
        target_fingerprint_sha256=actual.sha256,
    )


def _copy_json_object(
    value: Mapping[str, object], *, label: str, byte_limit: int
) -> dict[str, object]:
    """Bound and deep-copy one object through the strict canonical JSON form."""
    if not isinstance(value, Mapping):
        raise MigrationApplyError(f"{label}_must_be_json_object")
    try:
        encoded = canonical_json_bytes(value)
    except (MigrationPlanError, TypeError, ValueError, RecursionError) as exc:
        raise MigrationApplyError(f"{label}_must_be_canonical_json") from exc
    if len(encoded) > byte_limit:
        raise MigrationApplyError(f"{label}_exceeds_size_limit")
    try:
        copied = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise MigrationApplyError(f"{label}_is_not_strict_json") from exc
    if type(copied) is not dict:
        raise MigrationApplyError(f"{label}_must_be_json_object")
    return copied


def _verify_plan_self_hash(plan: Mapping[str, object]) -> None:
    """Verify the stored plan digest before reconstructing it from source inputs."""
    digest = plan.get("plan_sha256")
    unsigned = {key: value for key, value in plan.items() if key != "plan_sha256"}
    if not _is_sha256(digest) or canonical_json_sha256(unsigned) != digest:
        raise MigrationApplyError("migration_plan_sha256_invalid")


def _single_task(plan: Mapping[str, object], task_id: str) -> Mapping[str, object]:
    """Require a fully ready, disabled, unique scheduled entry in the plan."""
    summary = plan.get("summary")
    tasks = plan.get("tasks")
    helpers = plan.get("helpers")
    if (
        not isinstance(summary, Mapping)
        or not isinstance(tasks, list)
        or not isinstance(helpers, list)
    ):
        raise MigrationApplyError("migration_plan_shape_invalid")
    if type(summary.get("scheduled_task_count")) is not int or summary.get(
        "scheduled_task_count"
    ) != len(tasks):
        raise MigrationApplyError("migration_plan_scheduled_task_denominator_mismatch")
    if (
        plan.get("execution_supported") is not False
        or plan.get("enabled_jobs_created") is not False
    ):
        raise MigrationApplyError("migration_plan_execution_flags_invalid")
    if plan.get("integrity_issues") != []:
        raise MigrationApplyError("migration_plan_has_integrity_issues")
    if len({item.get("task_id") for item in tasks if isinstance(item, Mapping)}) != len(tasks):
        raise MigrationApplyError("migration_plan_scheduled_task_ids_not_unique")
    ready_count = sum(
        isinstance(item, Mapping) and item.get("disposition") == "READY_DISABLED" for item in tasks
    )
    blocked_count = sum(
        isinstance(item, Mapping) and item.get("disposition") == "BLOCKED" for item in tasks
    )
    if (
        type(summary.get("ready_disabled_task_count")) is not int
        or summary.get("ready_disabled_task_count") != ready_count
        or type(summary.get("blocked_task_count")) is not int
        or summary.get("blocked_task_count") != blocked_count
        or summary.get("scheduled_task_count") != ready_count + blocked_count
    ):
        raise MigrationApplyError("migration_plan_task_counts_mismatch")
    expected_status = "BLOCKED" if blocked_count else "READY_DISABLED"
    if plan.get("plan_status") != expected_status:
        raise MigrationApplyError("migration_plan_status_count_mismatch")
    matches = [
        item for item in tasks if isinstance(item, Mapping) and item.get("task_id") == task_id
    ]
    if len(matches) != 1:
        raise MigrationApplyError("scheduled_task_missing_or_duplicated_in_plan")
    if any(isinstance(item, Mapping) and item.get("task_id") == task_id for item in helpers):
        raise MigrationApplyError("non_scheduled_helper_cannot_be_imported")
    job = matches[0]
    if job.get("disposition") != "READY_DISABLED" or job.get("enabled") is not False:
        raise MigrationApplyError("selected_job_is_blocked_or_enabled")
    if job.get("blocked_reasons") != [] or job.get("static_status") != "READY_FOR_ADAPTER":
        raise MigrationApplyError("selected_job_has_unresolved_migration_blockers")
    if not isinstance(job.get("target"), Mapping) or not isinstance(job.get("review"), Mapping):
        raise MigrationApplyError("selected_job_review_or_target_missing")
    if not isinstance(job.get("target_schedule"), Mapping):
        raise MigrationApplyError("selected_job_schedule_missing")
    inputs = plan.get("inputs")
    if (
        not isinstance(inputs, Mapping)
        or inputs.get("target_warehouse_fingerprint_allowlisted") is not True
    ):
        raise MigrationApplyError("actual_warehouse_fingerprint_not_allowlisted")
    if not _is_sha256(inputs.get("target_warehouse_fingerprint_sha256")):
        raise MigrationApplyError("actual_warehouse_fingerprint_missing")
    return job


def _find_reviewed_mapping(mapping: Mapping[str, object], task_id: str) -> Mapping[str, object]:
    """Locate the exact approved per-task mapping row for its audit evidence."""
    rows = mapping.get("tasks")
    if not isinstance(rows, list):
        raise MigrationApplyError("reviewed_mapping_tasks_missing")
    matches = [row for row in rows if isinstance(row, Mapping) and row.get("task_id") == task_id]
    if len(matches) != 1:
        raise MigrationApplyError("reviewed_mapping_task_missing_or_duplicated")
    review = matches[0].get("review")
    if not isinstance(review, Mapping) or review.get("status") != "APPROVED":
        raise MigrationApplyError("mapping_review_not_approved")
    if not _is_sha256(review.get("evidence_sha256")):
        raise MigrationApplyError("mapping_review_evidence_missing")
    return matches[0]


def _build_task_definition(job: Mapping[str, object]) -> TaskDefinition:
    """Translate only reviewed declarative fields into a default-disabled task."""
    target = job.get("target")
    schedule_row = job.get("target_schedule")
    if not isinstance(target, Mapping) or not isinstance(schedule_row, Mapping):
        raise MigrationApplyError("reviewed_target_or_schedule_missing")
    kind = schedule_row.get("schedule_type")
    expression = schedule_row.get("expression")
    timezone = schedule_row.get("timezone")
    calendar = schedule_row.get("calendar")
    if kind not in {"cron", "interval", "date"}:
        raise MigrationApplyError("legacy_schedule_kind_requires_explicit_normalization")
    try:
        return TaskDefinition.model_validate(
            {
                "contract_version": 1,
                "job_id": target.get("job_id"),
                "version": target.get("version"),
                "dataset_id": target.get("dataset_id"),
                "provider": target.get("provider"),
                "model": target.get("model"),
                "timezone": timezone,
                "calendar": calendar,
                "schedule": {
                    "kind": kind,
                    "expression": expression,
                    "description": None,
                },
                "enabled": False,
            },
            strict=True,
        )
    except ValidationError as exc:
        raise MigrationApplyError("reviewed_mapping_cannot_form_task_definition") from exc


def _build_provenance(
    *,
    task_id: str,
    job: Mapping[str, object],
    mapping_document: Mapping[str, object],
    mapping_row: Mapping[str, object],
    plan: Mapping[str, object],
    actual: WarehouseFingerprintObservation,
    metadata_identity_sha256: str,
) -> dict[str, object]:
    """Build a bounded audit object using approved IDs and hashes only."""
    inputs = plan.get("inputs")
    if not isinstance(inputs, Mapping):
        raise MigrationApplyError("migration_plan_input_pins_missing")
    target = job.get("target")
    review = job.get("review")
    schedule = job.get("target_schedule")
    governance = job.get("execution_governance")
    if not isinstance(target, Mapping):
        raise MigrationApplyError("selected_job_target_invalid")
    if not isinstance(review, Mapping):
        raise MigrationApplyError("selected_job_review_invalid")
    if not isinstance(schedule, Mapping):
        raise MigrationApplyError("selected_job_schedule_invalid")
    if not isinstance(governance, Mapping):
        raise MigrationApplyError("selected_job_audit_fields_invalid")
    safe_target = _select_fields(
        target,
        (
            "dataset_id",
            "provider",
            "domain",
            "model",
            "job_id",
            "version",
            "sink_id",
            "contract_sha256",
        ),
    )
    safe_review = _select_fields(
        review,
        ("status", "reviewer_id", "reviewed_at", "evidence_sha256"),
    )
    safe_schedule = _select_fields(
        schedule,
        ("schedule_type", "expression", "timezone", "calendar"),
    )
    safe_governance = _select_fields(
        governance,
        ("source_quota", "ownership_group", "shard"),
    )
    expected_hashes = (
        "runtime_inventory_sha256",
        "static_catalog_sha256",
        "reviewed_mapping_sha256",
        "target_allowlist_sha256",
        "target_warehouse_fingerprint_sha256",
    )
    hash_inputs: dict[str, str] = {}
    for name in expected_hashes:
        value = inputs.get(name)
        if not isinstance(value, str) or not _is_sha256(value):
            raise MigrationApplyError("migration_plan_hash_pin_missing")
        hash_inputs[name] = value
    plan_digest = plan.get("plan_sha256")
    if not _is_sha256(plan_digest):
        raise MigrationApplyError("migration_plan_sha256_missing")
    if hash_inputs["target_warehouse_fingerprint_sha256"] != actual.sha256:
        raise MigrationApplyError("target_fingerprint_does_not_match_recomputed_plan")
    provenance = {
        "schema_version": 1,
        "legacy_task_id": task_id,
        "input_sha256": hash_inputs,
        "plan_sha256": plan_digest,
        "warehouse_fingerprint_algorithm": actual.algorithm,
        "warehouse_observation_scope": {
            "subject": "plain_absolute_sqlite_file_named_by_engine_url",
            "connector": "module_owned_fresh_readonly_sqlite3_connection",
            "caller_engine_pool_equivalence_verified": False,
            "caller_custom_creator_equivalence_verified": False,
            "encoded_catalog_byte_limit": "64MiB_after_materialization_not_peak_memory",
        },
        "target_database_identity_sha256": actual.database_identity_sha256,
        "metadata_database_identity_sha256": metadata_identity_sha256,
        "review": safe_review,
        "target_identity": safe_target,
        "target_schedule": safe_schedule,
        "execution_governance": safe_governance,
        "source_evidence": {
            "script_id": job.get("script_id"),
            "script_path": job.get("script_path"),
            "script_sha256": job.get("script_sha256"),
            "task_params_sha256": job.get("task_params_sha256"),
            "script_params_sha256": job.get("script_params_sha256"),
            "params_sha256": job.get("params_sha256"),
            "static_status": job.get("static_status"),
        },
    }
    mapping_hash = inputs.get("reviewed_mapping_sha256")
    mapping_pins = mapping_document.get("inputs")
    if not _is_sha256(mapping_hash) or not isinstance(mapping_pins, Mapping):
        raise MigrationApplyError("reviewed_mapping_hash_pin_missing")
    if mapping_row.get("review") != safe_review:
        raise MigrationApplyError("reviewed_mapping_review_differs_from_plan")
    if mapping_pins.get("runtime_inventory_sha256") != hash_inputs["runtime_inventory_sha256"]:
        raise MigrationApplyError("reviewed_mapping_runtime_pin_mismatch")
    if mapping_pins.get("static_catalog_sha256") != hash_inputs["static_catalog_sha256"]:
        raise MigrationApplyError("reviewed_mapping_catalog_pin_mismatch")
    if mapping_pins.get("target_allowlist_sha256") != hash_inputs["target_allowlist_sha256"]:
        raise MigrationApplyError("reviewed_mapping_allowlist_pin_mismatch")
    return provenance


def _select_fields(source: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    """Copy required review fields and reject absent or unsafe values."""
    if any(field not in source for field in fields):
        raise MigrationApplyError("reviewed_audit_field_missing")
    selected = {field: source[field] for field in fields}
    try:
        encoded = canonical_json_bytes(selected)
    except (TypeError, ValueError, RecursionError) as exc:
        raise MigrationApplyError("reviewed_audit_field_invalid") from exc
    if len(encoded) > 16_384:
        raise MigrationApplyError("reviewed_audit_field_exceeds_size_limit")
    return selected


def _require_isolated_metadata_schema(engine: Engine) -> None:
    """Require a dedicated prototype metadata database with one table."""
    if engine.dialect.name not in {"sqlite", "mysql"}:
        raise MigrationApplyError("metadata_engine_dialect_unsupported")
    if engine.dialect.name == "sqlite":
        _require_sqlite_metadata_engine(engine)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        views = set(inspector.get_view_names())
        if tables != {JOB_DEFINITION_TABLE.name} or views:
            raise MigrationApplyError("metadata_engine_must_be_dedicated_to_job_definitions")
        columns = inspector.get_columns(JOB_DEFINITION_TABLE.name)
        expected_columns = [column.name for column in JOB_DEFINITION_TABLE.columns]
        if [str(column["name"]) for column in columns] != expected_columns:
            raise MigrationApplyError("metadata_job_definition_table_schema_mismatch")
        primary_key = inspector.get_pk_constraint(JOB_DEFINITION_TABLE.name).get(
            "constrained_columns"
        )
        if primary_key != ["legacy_task_id"]:
            raise MigrationApplyError("metadata_job_definition_primary_key_mismatch")
        indexes = inspector.get_indexes(JOB_DEFINITION_TABLE.name)
        if not any(
            (index.get("unique") is True or index.get("unique") == 1)
            and index.get("column_names") == ["job_id", "version"]
            for index in indexes
        ):
            raise MigrationApplyError("metadata_job_identity_unique_index_missing")
        if engine.dialect.name == "mysql":
            options = inspector.get_table_options(JOB_DEFINITION_TABLE.name)
            if str(options.get("mysql_engine", "")).casefold() != "innodb":
                raise MigrationApplyError("metadata_table_must_use_transactional_innodb")
    except MigrationApplyError:
        raise
    except SQLAlchemyError as exc:
        raise MigrationApplyError("metadata_schema_unreadable") from exc
    store = JobDefinitionStore(engine)
    try:
        records = store.list_all(limit=MAX_JOB_DEFINITION_LIST_ROWS)
    except JobDefinitionStoreError as exc:
        raise MigrationApplyError("metadata_job_definition_rows_invalid") from exc
    if any(record.definition.enabled for record in records):
        raise MigrationApplyError("metadata_contains_enabled_job_definition")


def _inspect_database_identity(engine: Engine) -> str:
    """Hash the live metadata database identity without retaining credentials."""
    try:
        expected_file_identity = (
            _sqlite_engine_file_identity(engine)[1] if engine.dialect.name == "sqlite" else None
        )
        if engine.dialect.name == "sqlite":
            _require_sqlite_metadata_engine(engine)
        with engine.connect() as connection:
            identity = _database_identity(connection, engine)
    except MigrationApplyError:
        raise
    except (SQLAlchemyError, OSError, ValueError, TypeError) as exc:
        raise MigrationApplyError("metadata_database_identity_unreadable") from exc
    if (
        expected_file_identity is not None
        and _physical_identity(identity) != expected_file_identity
    ):
        raise MigrationApplyError("metadata_database_identity_changed")
    if expected_file_identity is not None and (
        _sqlite_engine_file_identity(engine)[1] != expected_file_identity
    ):
        raise MigrationApplyError("metadata_database_identity_changed")
    return canonical_json_sha256(identity)


def _database_identity(connection: Connection, engine: Engine) -> dict[str, object]:
    """Read the physical SQLite file or MySQL server/database identity."""
    dialect = engine.dialect.name
    if dialect == "sqlite":
        rows = connection.exec_driver_sql("PRAGMA database_list").all()
        attached = [row for row in rows if str(row[1]).casefold() != "temp"]
        if len(attached) != 1 or str(attached[0][1]).casefold() != "main":
            raise MigrationApplyError("sqlite_attached_databases_are_not_supported")
        filename = str(attached[0][2] or "")
        if not filename or filename.casefold().startswith("file:"):
            raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
        connected_path = Path(filename)
        try:
            configured_path, configured_identity = _sqlite_engine_file_identity(engine)
            connected_identity = _sqlite_path_identity(connected_path)
        except OSError as exc:
            raise MigrationApplyError("sqlite_physical_file_identity_unavailable") from exc
        if connected_path != configured_path or connected_identity != configured_identity:
            raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
        return {
            "dialect": "sqlite",
            "device_id": configured_identity[0],
            "inode_id": configured_identity[1],
        }
    if dialect == "mysql":
        row = connection.execute(
            text(
                "SELECT DATABASE(), @@server_uuid, @@hostname, @@port, @@version, @@version_comment"
            )
        ).one_or_none()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise MigrationApplyError("mysql_current_database_unavailable")
        if not isinstance(row[1], str) or not row[1]:
            raise MigrationApplyError("mysql_server_uuid_unavailable")
        return {
            "dialect": "mysql",
            "database_name": row[0],
            "server_uuid": row[1],
            "server_hostname": str(row[2]),
            "server_port": int(row[3]),
            "server_version": str(row[4]),
            "server_version_comment": str(row[5]),
        }
    raise MigrationApplyError("database_identity_dialect_unsupported")


def _sqlite_engine_file_identity(engine: Engine) -> tuple[Path, tuple[int, int]]:
    """Require an unambiguous absolute file path and return its physical inode."""
    database = engine.url.database
    if not isinstance(database, str) or not database or database.casefold().startswith("file:"):
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    path = Path(database)
    try:
        identity = _validate_sqlite_path_components(path, allow_missing_final=False)
    except OSError as exc:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable") from exc
    if identity is None:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    return path, identity


def _validate_sqlite_path_components(
    path: Path, *, allow_missing_final: bool
) -> tuple[int, int] | None:
    """Lstat every path component without following links or creating files."""
    if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    parts = path.parts[1:]
    if not parts:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    current = Path(path.anchor)
    identity: tuple[int, int] | None = None
    for index, part in enumerate(parts):
        current = current / part
        is_final = index == len(parts) - 1
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            if allow_missing_final and is_final:
                return None
            raise
        if stat_module.S_ISLNK(component_stat.st_mode):
            raise MigrationApplyError("sqlite_path_component_is_symlink")
        if not is_final and not stat_module.S_ISDIR(component_stat.st_mode):
            raise MigrationApplyError("sqlite_path_parent_is_not_directory")
        if is_final:
            if not stat_module.S_ISREG(component_stat.st_mode):
                raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
            identity = component_stat.st_dev, component_stat.st_ino
    return identity


def _sqlite_path_identity(path: Path) -> tuple[int, int]:
    """Lstat every absolute path component, rejecting symlinks and aliases."""
    identity = _validate_sqlite_path_components(path, allow_missing_final=False)
    if identity is None:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    return identity


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    """Open a fresh read-only SQLite connection for a verified plain file."""
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1.0)
    connection.row_factory = sqlite3.Row
    return connection


def _sqlite_connection_identity(
    connection: sqlite3.Connection, expected_path: Path
) -> dict[str, object]:
    """Bind the fresh SQLite connection's main database path to its live inode."""
    rows = connection.execute("PRAGMA database_list").fetchall()
    attached = [row for row in rows if str(row["name"]).casefold() != "temp"]
    if len(attached) != 1 or str(attached[0]["name"]).casefold() != "main":
        raise MigrationApplyError("sqlite_attached_databases_are_not_supported")
    filename = str(attached[0]["file"] or "")
    if not filename or filename.casefold().startswith("file:"):
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    connected_path = Path(filename)
    try:
        if connected_path != expected_path:
            raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
        device, inode = _sqlite_path_identity(connected_path)
    except OSError as exc:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable") from exc
    return {"dialect": "sqlite", "device_id": device, "inode_id": inode}


def _physical_identity(identity: Mapping[str, object]) -> tuple[int, int]:
    """Return a checked SQLite device/inode pair from a live identity record."""
    device = identity.get("device_id")
    inode = identity.get("inode_id")
    if type(device) is not int or type(inode) is not int:
        raise MigrationApplyError("sqlite_physical_file_identity_unavailable")
    return device, inode


def _require_sqlite_null_pool(engine: Engine, *, label: str) -> None:
    """Reject pooled SQLite metadata engines that could retain a stale file."""
    if not isinstance(engine.pool, NullPool):
        raise MigrationApplyError(f"{label}_sqlite_requires_null_pool")


def _require_sqlite_metadata_engine(engine: Engine) -> None:
    """Require the factory-created default-creator SQLite metadata engine."""
    _require_sqlite_null_pool(engine, label="metadata")
    trusted = _ISOLATED_METADATA_ENGINES.get(engine)
    if trusted is None or engine.url != trusted[0] or engine.pool is not trusted[1]:
        raise MigrationApplyError("metadata_sqlite_engine_must_use_safe_factory")


def _sqlite_catalog(connection: sqlite3.Connection) -> dict[str, object]:
    """Fingerprint the SQLite main database's canonical stored schema SQL."""
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM main.sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchmany(MAX_SQLITE_CATALOG_ROWS + 1)
    if len(rows) > MAX_SQLITE_CATALOG_ROWS:
        raise MigrationApplyError("target_warehouse_catalog_exceeds_row_limit")
    objects = [
        {
            "type": str(row["type"]),
            "name": str(row["name"]),
            "table_name": str(row["tbl_name"]),
            "sql": row["sql"],
        }
        for row in rows
    ]
    return {
        "tables": [item for item in objects if item["type"] == "table"],
        "views": [item for item in objects if item["type"] == "view"],
        "triggers": [item for item in objects if item["type"] == "trigger"],
        "indexes": [item for item in objects if item["type"] == "index"],
    }


def _is_sha256(value: object) -> bool:
    """Return whether the value is a canonical lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
