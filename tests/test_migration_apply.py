"""Behavior tests for the offline reviewed-definition import prototype."""

from __future__ import annotations

import copy
import os
from types import MappingProxyType, SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event, text, update
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.dml import Insert

import opendata.migration_apply as migration_apply
from opendata.job_definition_store import (
    JOB_DEFINITION_TABLE,
    JobDefinitionConflictError,
    JobDefinitionStore,
    JobDefinitionStoreError,
)
from opendata.migration_apply import (
    MigrationApplyError,
    WarehouseFingerprintObservation,
    apply_reviewed_job_definition,
    inspect_warehouse_fingerprint,
)
from opendata.migration_plan import (
    WarehouseFingerprint,
    build_migration_plan,
    canonical_json_sha256,
)
from opendata.pipeline.run_identity import TaskDefinition

if TYPE_CHECKING:
    from pathlib import Path
    from sqlite3 import Connection as SQLite3Connection

TASK_PARAMS_HASH = "1" * 64
SCRIPT_PARAMS_HASH = "2" * 64
SOURCE_HASH = "3" * 64
CONTRACT_HASH = "4" * 64
REVIEW_HASH = "5" * 64
SOURCE_PATH = "src/backend/data/jobs/daily.py"
SCRIPT_ID = "daily_job"
TASK_ID = "scheduled:7"


def _inputs() -> tuple[dict, dict, dict, dict]:
    """Build a synthetic, fully reviewed plan input with no raw parameters."""
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
        "counts": {"scheduled_task_rows_exact": 1, "data_script_rows_exact": 1},
        "readonly_transaction": {"metadata_confirmed": True, "warehouse_confirmed": True},
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
    catalog = {
        "schema_version": "1.1",
        "source_root": "/legacy/afi",
        "opendata_root": "/local/opendata",
        "errors": [],
        "summary": {"legacy_script_count": 1},
        "files": [
            {
                "source_path": SOURCE_PATH,
                "source_sha256": SOURCE_HASH,
                "status": "READY_FOR_ADAPTER",
                "calls": [
                    {
                        "function": "daily_bars",
                        "target_models": ["openbb:equity:DailyBarsFetcher"],
                    }
                ],
            }
        ],
        "opendata_provider_models": [
            {
                "source": "openbb",
                "domain": "equity",
                "model_class": "DailyBarsFetcher",
                "registered_by_descriptor": True,
            }
        ],
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
        "warehouse_fingerprints": [],
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
                "params_sha256": canonical_json_sha256(
                    {
                        "task_params_sha256": TASK_PARAMS_HASH,
                        "script_params_sha256": SCRIPT_PARAMS_HASH,
                        "semantics": "task_and_script_parameter_sha256_v1",
                    }
                ),
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


def _scenario(tmp_path: Path, request: pytest.FixtureRequest):
    metadata_engine = create_engine(f"sqlite:///{tmp_path / 'control.sqlite'}")
    target_engine = create_engine(f"sqlite:///{tmp_path / 'warehouse.sqlite'}")
    request.addfinalizer(metadata_engine.dispose)
    request.addfinalizer(target_engine.dispose)
    with target_engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE daily_bars "
                "(symbol TEXT NOT NULL, business_date TEXT NOT NULL, close REAL, "
                "PRIMARY KEY (symbol, business_date))"
            )
        )
    actual = inspect_warehouse_fingerprint(target_engine)
    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["warehouse_fingerprints"] = [actual.sha256]
    mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(allowlist)
    plan = build_migration_plan(
        inventory,
        catalog,
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(actual.sha256),
        target_allowlist=allowlist,
    )
    JobDefinitionStore(metadata_engine).create_schema()
    return metadata_engine, target_engine, inventory, catalog, mapping, allowlist, plan


def _apply(scenario, **overrides):
    metadata_engine, target_engine, inventory, catalog, mapping, allowlist, plan = scenario
    arguments = {
        "metadata_engine": metadata_engine,
        "target_engine": target_engine,
        "plan": plan,
        "runtime_inventory": inventory,
        "static_catalog": catalog,
        "reviewed_mapping": mapping,
        "target_allowlist": allowlist,
        "task_id": TASK_ID,
    }
    arguments.update(overrides)
    return apply_reviewed_job_definition(**arguments)


def test_import_persists_only_disabled_definition_and_is_idempotent(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)

    first = _apply(scenario)
    second = _apply(scenario)

    assert first.created is True
    assert second.created is False
    assert first.receipt_sha256 == second.receipt_sha256
    assert first.definition.enabled is False
    assert first.definition.job_id == "market.daily_bars.refresh"
    assert first.definition.schedule is not None
    assert first.definition.schedule.expression == "5 6 * * 1-5"
    assert not hasattr(first.definition, "script_path")
    store = JobDefinitionStore(scenario[0])
    record = store.get(TASK_ID)
    assert record is not None
    assert record.provenance["target_identity"]["sink_id"] == "warehouse.market_daily_bars"
    assert record.provenance["plan_sha256"] == first.plan_sha256
    assert len(store.list_all()) == 1


def test_one_ready_entry_can_import_without_shrinking_blocked_plan(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    original = _scenario(tmp_path, request)
    inventory = copy.deepcopy(original[2])
    mapping = copy.deepcopy(original[4])
    second_task = copy.deepcopy(inventory["metadata"]["scheduled_tasks"]["records"][0])
    second_task["id"] = 8
    inventory["metadata"]["scheduled_tasks"]["records"].append(second_task)
    inventory["metadata"]["scheduled_tasks"]["row_count_exact"] = 2
    inventory["counts"]["scheduled_task_rows_exact"] = 2
    inventory["tasks"].append(
        {
            "task_id": "scheduled:8",
            "script_id": SCRIPT_ID,
            "script_path": SOURCE_PATH,
            "runtime_is_active": 1,
            "path_status": "PATH_CANDIDATE",
        }
    )
    mapping["inputs"]["runtime_inventory_sha256"] = canonical_json_sha256(inventory)
    actual = inspect_warehouse_fingerprint(original[1])
    plan = build_migration_plan(
        inventory,
        original[3],
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(actual.sha256),
        target_allowlist=original[5],
    )
    assert plan["plan_status"] == "BLOCKED"
    assert plan["summary"]["scheduled_task_count"] == 2
    assert plan["summary"]["ready_disabled_task_count"] == 1
    assert plan["summary"]["blocked_task_count"] == 1

    scenario = (original[0], original[1], inventory, original[3], mapping, original[5], plan)
    result = _apply(scenario)

    assert result.created is True
    assert result.definition.enabled is False
    assert JobDefinitionStore(original[0]).list_all()[0].legacy_task_id == TASK_ID
    assert [item["task_id"] for item in plan["tasks"]] == [TASK_ID, "scheduled:8"]


def test_mutated_plan_and_blocked_mapping_are_refused_without_rows(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    changed_plan = copy.deepcopy(scenario[6])
    changed_plan["tasks"][0]["enabled"] = True
    with pytest.raises(MigrationApplyError, match="migration_plan_sha256_invalid"):
        _apply(scenario, plan=changed_plan)

    rejected_mapping = copy.deepcopy(scenario[4])
    rejected_mapping["tasks"][0]["review"]["status"] = "REJECTED"
    rejected_mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(scenario[5])
    blocked_plan = build_migration_plan(
        scenario[2],
        scenario[3],
        rejected_mapping,
        warehouse_fingerprint=WarehouseFingerprint(
            scenario[6]["inputs"]["target_warehouse_fingerprint_sha256"]
        ),
        target_allowlist=scenario[5],
    )
    with pytest.raises(MigrationApplyError, match="selected_job_is_blocked_or_enabled"):
        _apply(scenario, plan=blocked_plan, reviewed_mapping=rejected_mapping)
    assert JobDefinitionStore(scenario[0]).list_all() == ()


def test_apply_revalidates_call_shape_and_reconstructed_inputs(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    with pytest.raises(MigrationApplyError, match="scheduled_task_id_invalid"):
        _apply(scenario, task_id="helper:daily")
    with pytest.raises(TypeError, match="must be SQLAlchemy Engines"):
        _apply(scenario, metadata_engine=None)

    bad_mapping = copy.deepcopy(scenario[4])
    bad_mapping["schema_version"] = -1
    with pytest.raises(MigrationApplyError, match="inputs_failed_revalidation"):
        _apply(scenario, reviewed_mapping=bad_mapping)


def test_target_schema_drift_between_plan_and_import_is_refused(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    with scenario[1].begin() as connection:
        connection.execute(text("ALTER TABLE daily_bars ADD COLUMN volume INTEGER"))

    with pytest.raises(MigrationApplyError, match="migration_plan_or_input_hash_drift"):
        _apply(scenario)
    assert JobDefinitionStore(scenario[0]).list_all() == ()


def test_changed_reviewed_target_identity_conflicts(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    _apply(scenario)
    mapping = copy.deepcopy(scenario[4])
    allowlist = copy.deepcopy(scenario[5])
    changed_target = dict(
        allowlist["targets"][0], job_id="market.daily_bars.refresh.v2", version="v2"
    )
    allowlist["targets"] = [changed_target]
    mapping["tasks"][0].update(
        {
            "job_id": changed_target["job_id"],
            "version": changed_target["version"],
        }
    )
    mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(allowlist)
    observed = inspect_warehouse_fingerprint(scenario[1])
    changed_plan = build_migration_plan(
        scenario[2],
        scenario[3],
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(observed.sha256),
        target_allowlist=allowlist,
    )

    with pytest.raises(JobDefinitionConflictError, match="identity or pins"):
        _apply(scenario, plan=changed_plan, reviewed_mapping=mapping, target_allowlist=allowlist)
    assert len(JobDefinitionStore(scenario[0]).list_all()) == 1


def test_target_change_during_persist_rolls_back_insert(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch
) -> None:
    scenario = _scenario(tmp_path, request)
    original = migration_apply.inspect_warehouse_fingerprint
    count = 0

    def drift_on_second_read(engine):
        nonlocal count
        count += 1
        observation = original(engine)
        if count == 2:
            return WarehouseFingerprintObservation(
                sha256="0" * 64,
                database_identity_sha256=observation.database_identity_sha256,
            )
        return observation

    monkeypatch.setattr(migration_apply, "inspect_warehouse_fingerprint", drift_on_second_read)

    with pytest.raises(
        MigrationApplyError, match="target_warehouse_changed_during_metadata_import"
    ):
        _apply(scenario)
    assert count == 2
    assert JobDefinitionStore(scenario[0]).list_all() == ()


def test_read_only_metadata_write_fails_without_partial_row(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    metadata_path = scenario[0].url.database
    assert isinstance(metadata_path, str)
    readonly_engine = create_engine(f"sqlite:///{metadata_path}")
    request.addfinalizer(readonly_engine.dispose)

    def enable_query_only(dbapi_connection: SQLite3Connection, connection_record: object) -> None:
        del connection_record
        dbapi_connection.execute("PRAGMA query_only=ON")

    event.listen(readonly_engine, "connect", enable_query_only)
    with pytest.raises(MigrationApplyError, match="metadata_write_failed"):
        _apply(scenario, metadata_engine=readonly_engine)
    assert JobDefinitionStore(scenario[0]).list_all() == ()


def test_non_isolated_metadata_database_and_same_database_are_rejected(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    scenario = _scenario(tmp_path, request)
    with scenario[0].begin() as connection:
        connection.execute(text("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)"))
    with pytest.raises(MigrationApplyError, match="dedicated_to_job_definitions"):
        _apply(scenario)

    same_database_engine = create_engine(f"sqlite:///{tmp_path / 'same.sqlite'}")
    request.addfinalizer(same_database_engine.dispose)
    JobDefinitionStore(same_database_engine).create_schema()
    same_target = create_engine(f"sqlite:///{tmp_path / 'same.sqlite'}")
    request.addfinalizer(same_target.dispose)
    with same_target.begin() as connection:
        connection.execute(text("CREATE TABLE warehouse_rows (id INTEGER PRIMARY KEY)"))
    actual = inspect_warehouse_fingerprint(same_target)
    inventory, catalog, mapping, allowlist = _inputs()
    allowlist["warehouse_fingerprints"] = [actual.sha256]
    mapping["inputs"]["target_allowlist_sha256"] = canonical_json_sha256(allowlist)
    plan = build_migration_plan(
        inventory,
        catalog,
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(actual.sha256),
        target_allowlist=allowlist,
    )
    with pytest.raises(MigrationApplyError, match="same_database"):
        apply_reviewed_job_definition(
            metadata_engine=same_database_engine,
            target_engine=same_target,
            plan=plan,
            runtime_inventory=inventory,
            static_catalog=catalog,
            reviewed_mapping=mapping,
            target_allowlist=allowlist,
            task_id=TASK_ID,
        )


def test_job_version_identity_cannot_be_claimed_by_two_legacy_tasks(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    original = _scenario(tmp_path, request)
    inventory = copy.deepcopy(original[2])
    mapping = copy.deepcopy(original[4])
    second_task = copy.deepcopy(inventory["metadata"]["scheduled_tasks"]["records"][0])
    second_task["id"] = 8
    inventory["metadata"]["scheduled_tasks"]["records"].append(second_task)
    inventory["metadata"]["scheduled_tasks"]["row_count_exact"] = 2
    inventory["counts"]["scheduled_task_rows_exact"] = 2
    inventory["tasks"].append(
        {
            "task_id": "scheduled:8",
            "script_id": SCRIPT_ID,
            "script_path": SOURCE_PATH,
            "runtime_is_active": 1,
            "path_status": "PATH_CANDIDATE",
        }
    )
    second_mapping = copy.deepcopy(mapping["tasks"][0])
    second_mapping["task_id"] = "scheduled:8"
    mapping["tasks"].append(second_mapping)
    mapping["inputs"]["runtime_inventory_sha256"] = canonical_json_sha256(inventory)
    actual = inspect_warehouse_fingerprint(original[1])
    plan = build_migration_plan(
        inventory,
        original[3],
        mapping,
        warehouse_fingerprint=WarehouseFingerprint(actual.sha256),
        target_allowlist=original[5],
    )
    scenario = (original[0], original[1], inventory, original[3], mapping, original[5], plan)

    _apply(scenario)
    with pytest.raises(JobDefinitionConflictError, match="already claimed by another legacy task"):
        _apply(scenario, task_id="scheduled:8")

    assert [row.legacy_task_id for row in JobDefinitionStore(original[0]).list_all()] == [TASK_ID]


def test_target_fingerprint_rejects_memory_and_unproven_mysql_targets(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    memory_engine = create_engine("sqlite:///:memory:")
    request.addfinalizer(memory_engine.dispose)
    with pytest.raises(MigrationApplyError, match="physical_file_identity_unavailable"):
        inspect_warehouse_fingerprint(memory_engine)
    from opendata.migration_apply import _inspect_database_identity

    with pytest.raises(MigrationApplyError, match="metadata_database_identity_unreadable"):
        _inspect_database_identity(memory_engine)

    file_engine = create_engine("sqlite://")
    request.addfinalizer(file_engine.dispose)
    monkeypatch.setattr(file_engine.dialect, "name", "mysql")
    with pytest.raises(MigrationApplyError, match="catalog_completeness_unproven"):
        inspect_warehouse_fingerprint(file_engine)

    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        inspect_warehouse_fingerprint(None)  # type: ignore[arg-type]


def test_fingerprint_does_not_create_a_missing_target_file(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    missing_path = tmp_path / "must-not-be-created.sqlite"
    target = create_engine(f"sqlite:///{missing_path}")
    request.addfinalizer(target.dispose)
    with pytest.raises(MigrationApplyError, match="physical_file_identity_unavailable"):
        inspect_warehouse_fingerprint(target)
    assert missing_path.exists() is False


def test_fingerprint_rejects_attached_sqlite_database(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    target_path = tmp_path / "attached.sqlite"
    target_path.write_bytes(b"")
    target = create_engine(f"sqlite:///{target_path}")
    request.addfinalizer(target.dispose)

    def attach_memory(connection: SQLite3Connection, connection_record: object) -> None:
        del connection_record
        connection.execute("ATTACH DATABASE ':memory:' AS attached")

    event.listen(target, "connect", attach_memory)
    with pytest.raises(MigrationApplyError, match="attached_databases_are_not_supported"):
        inspect_warehouse_fingerprint(target)


def test_sqlite_aliases_share_physical_database_identity(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    database_path = tmp_path / "warehouse.sqlite"
    hardlink_path = tmp_path / "warehouse-hardlink.sqlite"
    first = create_engine(f"sqlite:///{database_path}")
    request.addfinalizer(first.dispose)
    with first.begin() as connection:
        connection.execute(text("CREATE TABLE warehouse_rows (id INTEGER PRIMARY KEY)"))
    os.link(database_path, hardlink_path)
    alias = create_engine(f"sqlite:///{hardlink_path}")
    request.addfinalizer(alias.dispose)

    assert inspect_warehouse_fingerprint(first).database_identity_sha256 == (
        inspect_warehouse_fingerprint(alias).database_identity_sha256
    )


def test_sqlite_symbolic_link_target_is_rejected(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    database_path = tmp_path / "real.sqlite"
    database_path.write_bytes(b"")
    symbolic_link = tmp_path / "alias.sqlite"
    symbolic_link.symlink_to(database_path)
    engine = create_engine(f"sqlite:///{symbolic_link}")
    request.addfinalizer(engine.dispose)

    with pytest.raises(MigrationApplyError, match="physical_file_identity_unavailable"):
        inspect_warehouse_fingerprint(engine)


def test_definition_store_rejects_identity_collision_and_bounds_listing(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'definitions.sqlite'}")
    try:
        store = JobDefinitionStore(engine)
        store.create_schema()
        first = TaskDefinition(
            job_id="market.daily_bars.refresh",
            version="v1",
            dataset_id="market.daily_bars",
            provider="openbb",
            model="DailyBarsFetcher",
        )
        other_identity = first.model_copy(update={"dataset_id": "market.daily_bars_v2"})
        save = store._save_reviewed_definition
        save(
            legacy_task_id="scheduled:7",
            definition=first,
            provenance={"source": "fixture"},
            receipt_sha256="a" * 64,
            verify_target_unchanged=lambda: None,
        )
        with pytest.raises(JobDefinitionConflictError, match="already claimed"):
            save(
                legacy_task_id="scheduled:8",
                definition=other_identity,
                provenance={"source": "fixture"},
                receipt_sha256="b" * 64,
                verify_target_unchanged=lambda: None,
            )
        second = TaskDefinition(
            job_id="market.other_dataset.refresh",
            version="v1",
            dataset_id="market.other_dataset",
            provider="openbb",
            model="DailyBarsFetcher",
        )
        store._save_reviewed_definition(
            legacy_task_id="scheduled:9",
            definition=second,
            provenance={"source": "fixture"},
            receipt_sha256="c" * 64,
            verify_target_unchanged=lambda: None,
        )
        assert len(store.list_all(limit=2)) == 2
        with pytest.raises(JobDefinitionStoreError, match="exceeds requested row limit"):
            store.list_all(limit=1)
        with pytest.raises(ValueError, match="limit must be between"):
            store.list_all(limit=0)
    finally:
        engine.dispose()


def test_stored_digest_corruption_is_detected(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'corrupt.sqlite'}")
    try:
        store = JobDefinitionStore(engine)
        store.create_schema()
        store._save_reviewed_definition(
            legacy_task_id=TASK_ID,
            definition=TaskDefinition(
                job_id="market.daily_bars.refresh",
                version="v1",
                dataset_id="market.daily_bars",
                provider="openbb",
                model="DailyBarsFetcher",
            ),
            provenance={"source": "fixture"},
            receipt_sha256="a" * 64,
            verify_target_unchanged=lambda: None,
        )
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE od_job_definitions SET identity_sha256 = :digest"),
                {"digest": "0" * 64},
            )
        with pytest.raises(JobDefinitionStoreError, match="digest mismatch"):
            store.get(TASK_ID)
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("definition_json", "[]", "failed validation"),
        ("provenance_json", "[]", "not an object"),
        ("receipt_sha256", "z" * 64, "receipt hash is invalid"),
        ("created_at_us", "invalid", "timestamp is invalid"),
    ],
)
def test_store_rejects_malformed_persisted_fields(
    tmp_path: Path, column: str, value: str, message: str
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'malformed-store.sqlite'}")
    try:
        store = JobDefinitionStore(engine)
        store.create_schema()
        store._save_reviewed_definition(
            legacy_task_id=TASK_ID,
            definition=TaskDefinition(
                job_id="market.malformed.refresh",
                version="v1",
                dataset_id="market.malformed",
                provider="openbb",
                model="DailyBarsFetcher",
            ),
            provenance={"source": "fixture"},
            receipt_sha256="a" * 64,
            verify_target_unchanged=lambda: None,
        )
        with engine.begin() as connection:
            connection.execute(update(JOB_DEFINITION_TABLE).values({column: value}))
        with pytest.raises(JobDefinitionStoreError, match=message):
            store.get(TASK_ID)
    finally:
        engine.dispose()


def test_definition_store_validation_and_integrity_race_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opendata.job_definition_store import _revalidate_definition

    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        JobDefinitionStore(None)  # type: ignore[arg-type]
    engine = create_engine(f"sqlite:///{tmp_path / 'store-paths.sqlite'}")
    try:
        store = JobDefinitionStore(engine)
        assert store.engine is engine
        store.create_schema()
        assert store.get("scheduled:missing") is None
        assert store.list_all() == ()
        task = TaskDefinition(
            job_id="market.store_paths.refresh",
            version="v1",
            dataset_id="market.store_paths",
            provider="openbb",
            model="DailyBarsFetcher",
        )
        with pytest.raises(JobDefinitionStoreError, match="must be a TaskDefinition"):
            _revalidate_definition(None)  # type: ignore[arg-type]
        malformed = TaskDefinition.model_construct(job_id="bad")
        with pytest.raises(JobDefinitionStoreError, match="definition validation failed"):
            _revalidate_definition(malformed)
        with pytest.raises(JobDefinitionStoreError, match="value is not canonical JSON"):
            store._save_reviewed_definition(
                legacy_task_id="scheduled:7",
                definition=task,
                provenance={"credential": float("nan")},
                receipt_sha256="a" * 64,
                verify_target_unchanged=lambda: None,
            )
        with pytest.raises(JobDefinitionStoreError, match="provenance must be a JSON object"):
            store._save_reviewed_definition(
                legacy_task_id="scheduled:7",
                definition=task,
                provenance=MappingProxyType({"source": "fixture"}),
                receipt_sha256="a" * 64,
                verify_target_unchanged=lambda: None,
            )
        with pytest.raises(JobDefinitionStoreError, match="receipt hash is invalid"):
            store._save_reviewed_definition(
                legacy_task_id="scheduled:7",
                definition=task,
                provenance={"source": "fixture"},
                receipt_sha256="not-a-hash",
                verify_target_unchanged=lambda: None,
            )

        original_execute = Connection.execute
        injected = False

        def simulate_racing_insert(
            connection: Connection, statement: object, *args: object, **kwargs: object
        ) -> object:
            nonlocal injected
            result = original_execute(connection, statement, *args, **kwargs)
            if isinstance(statement, Insert) and not injected:
                injected = True
                raise IntegrityError("race", {}, RuntimeError("duplicate"))
            return result

        monkeypatch.setattr(Connection, "execute", simulate_racing_insert)
        saved = store._save_reviewed_definition(
            legacy_task_id="scheduled:7",
            definition=task,
            provenance={"source": "fixture"},
            receipt_sha256="b" * 64,
            verify_target_unchanged=lambda: None,
        )
        assert injected is True
        assert saved.created is False
        assert store.get("scheduled:7") is not None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("value", "label", "error"),
    [
        ([], "plan", "must_be_json_object"),
        ({"value": float("nan")}, "plan", "must_be_canonical_json"),
        ({"value": "large"}, "plan", "exceeds_size_limit"),
    ],
)
def test_json_input_copy_fails_closed(value: object, label: str, error: str) -> None:
    from opendata.migration_apply import _copy_json_object

    limit = 2 if error == "exceeds_size_limit" else 1024
    with pytest.raises(MigrationApplyError, match=error):
        _copy_json_object(value, label=label, byte_limit=limit)  # type: ignore[arg-type]


def test_selected_plan_entry_requires_full_consistent_counts(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    from opendata.migration_apply import _single_task

    plan = _scenario(tmp_path, request)[6]
    selected = _single_task(plan, TASK_ID)
    assert selected["task_id"] == TASK_ID

    malformed = copy.deepcopy(plan)
    malformed["summary"] = None
    with pytest.raises(MigrationApplyError, match="plan_shape_invalid"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["summary"]["scheduled_task_count"] = 0
    with pytest.raises(MigrationApplyError, match="denominator_mismatch"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["enabled_jobs_created"] = True
    with pytest.raises(MigrationApplyError, match="execution_flags_invalid"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["integrity_issues"] = ["tampered"]
    with pytest.raises(MigrationApplyError, match="integrity_issues"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["summary"]["ready_disabled_task_count"] = 0
    with pytest.raises(MigrationApplyError, match="task_counts_mismatch"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"].append(copy.deepcopy(malformed["tasks"][0]))
    malformed["summary"]["scheduled_task_count"] = 2
    malformed["summary"]["ready_disabled_task_count"] = 2
    with pytest.raises(MigrationApplyError, match="ids_not_unique"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["summary"]["ready_disabled_task_count"] = 1
    malformed["plan_status"] = "BLOCKED"
    with pytest.raises(MigrationApplyError, match="status_count_mismatch"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"] = []
    malformed["summary"]["scheduled_task_count"] = 0
    malformed["summary"]["ready_disabled_task_count"] = 0
    malformed["summary"]["blocked_task_count"] = 0
    malformed["plan_status"] = "READY_DISABLED"
    with pytest.raises(MigrationApplyError, match="missing_or_duplicated"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["helpers"] = [{"task_id": TASK_ID}]
    with pytest.raises(MigrationApplyError, match="non_scheduled_helper"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"][0]["static_status"] = "BLOCKED"
    with pytest.raises(MigrationApplyError, match="unresolved_migration_blockers"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"][0]["enabled"] = True
    with pytest.raises(MigrationApplyError, match="selected_job_is_blocked_or_enabled"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"][0]["target"] = None
    with pytest.raises(MigrationApplyError, match="review_or_target_missing"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["tasks"][0]["target_schedule"] = None
    with pytest.raises(MigrationApplyError, match="schedule_missing"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["inputs"]["target_warehouse_fingerprint_allowlisted"] = False
    with pytest.raises(MigrationApplyError, match="not_allowlisted"):
        _single_task(malformed, TASK_ID)

    malformed = copy.deepcopy(plan)
    malformed["inputs"]["target_warehouse_fingerprint_sha256"] = "invalid"
    with pytest.raises(MigrationApplyError, match="fingerprint_missing"):
        _single_task(malformed, TASK_ID)


def test_mapping_review_evidence_and_schedule_are_validated() -> None:
    from opendata.migration_apply import _build_task_definition, _find_reviewed_mapping

    _, _, mapping, _ = _inputs()
    row = _find_reviewed_mapping(mapping, TASK_ID)
    assert row["review"]["status"] == "APPROVED"
    with pytest.raises(MigrationApplyError, match="tasks_missing"):
        _find_reviewed_mapping({}, TASK_ID)
    with pytest.raises(MigrationApplyError, match="missing_or_duplicated"):
        _find_reviewed_mapping({"tasks": []}, TASK_ID)
    with pytest.raises(MigrationApplyError, match="missing_or_duplicated"):
        _find_reviewed_mapping({"tasks": [row, row]}, TASK_ID)
    rejected = copy.deepcopy(row)
    rejected["review"]["status"] = "REJECTED"
    with pytest.raises(MigrationApplyError, match="not_approved"):
        _find_reviewed_mapping({"tasks": [rejected]}, TASK_ID)
    with pytest.raises(MigrationApplyError, match="evidence_missing"):
        _find_reviewed_mapping(
            {**mapping, "tasks": [{**row, "review": {"status": "APPROVED"}}]}, TASK_ID
        )

    job = {
        "target": mapping["tasks"][0],
        "target_schedule": mapping["tasks"][0]["schedule"],
    }
    assert _build_task_definition(job).enabled is False
    bad_schedule = copy.deepcopy(job)
    bad_schedule["target_schedule"]["schedule_type"] = "legacy-cron"
    with pytest.raises(MigrationApplyError, match="requires_explicit_normalization"):
        _build_task_definition(bad_schedule)
    with pytest.raises(MigrationApplyError, match="reviewed_target_or_schedule_missing"):
        _build_task_definition({"target": None, "target_schedule": {}})
    bad_timezone = copy.deepcopy(job)
    bad_timezone["target_schedule"]["timezone"] = "Not/A_Timezone"
    with pytest.raises(MigrationApplyError, match="cannot_form_task_definition"):
        _build_task_definition(bad_timezone)


def test_target_audit_fields_do_not_accept_missing_or_unbounded_values() -> None:
    from opendata.migration_apply import _select_fields

    with pytest.raises(MigrationApplyError, match="audit_field_missing"):
        _select_fields({"name": "safe"}, ("missing",))
    with pytest.raises(MigrationApplyError, match="audit_field_invalid"):
        _select_fields({"name": object()}, ("name",))
    with pytest.raises(MigrationApplyError, match="audit_field_exceeds_size_limit"):
        _select_fields({"name": "x" * 20_000}, ("name",))


def test_sqlite_catalog_identity_contains_physical_file_details(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    target = create_engine(f"sqlite:///{tmp_path / 'physical.sqlite'}")
    request.addfinalizer(target.dispose)
    with target.begin() as connection:
        connection.execute(text("CREATE TABLE data_rows (id INTEGER PRIMARY KEY)"))

    observation = inspect_warehouse_fingerprint(target)
    assert len(observation.sha256) == 64
    assert len(observation.database_identity_sha256) == 64
    assert observation.algorithm == "opendata-warehouse-catalog-v1"


def test_target_with_no_user_tables_is_blocked(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    target_path = tmp_path / "empty.sqlite"
    target_path.write_bytes(b"")
    target = create_engine(f"sqlite:///{target_path}")
    request.addfinalizer(target.dispose)
    with pytest.raises(MigrationApplyError, match="has_no_user_tables"):
        inspect_warehouse_fingerprint(target)


def test_target_fingerprint_catalog_limits_fail_closed(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = create_engine(f"sqlite:///{tmp_path / 'bounded-catalog.sqlite'}")
    request.addfinalizer(target.dispose)
    with target.begin() as connection:
        connection.execute(text("CREATE TABLE data_rows (id INTEGER PRIMARY KEY)"))

    monkeypatch.setattr(migration_apply, "MAX_SQLITE_CATALOG_ROWS", 0)
    with pytest.raises(MigrationApplyError, match="catalog_exceeds_row_limit"):
        inspect_warehouse_fingerprint(target)

    monkeypatch.setattr(migration_apply, "MAX_SQLITE_CATALOG_ROWS", 100_000)
    monkeypatch.setattr(migration_apply, "MAX_SQLITE_CATALOG_BYTES", 1)
    with pytest.raises(MigrationApplyError, match="catalog_exceeds_size_limit"):
        inspect_warehouse_fingerprint(target)


def test_metadata_schema_requires_the_unique_identity_index(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    from opendata.migration_apply import _require_isolated_metadata_schema

    metadata = create_engine(f"sqlite:///{tmp_path / 'missing-index.sqlite'}")
    request.addfinalizer(metadata.dispose)
    JobDefinitionStore(metadata).create_schema()
    with metadata.begin() as connection:
        connection.execute(text("DROP INDEX ux_od_job_definitions_job_version"))
    with pytest.raises(MigrationApplyError, match="identity_unique_index_missing"):
        _require_isolated_metadata_schema(metadata)


def test_mysql_metadata_identity_includes_server_uuid() -> None:
    from opendata.migration_apply import _database_identity

    class ResultStub:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def one_or_none(self) -> tuple[object, ...] | None:
            return self._row

    class ConnectionStub:
        def __init__(self, row: tuple[object, ...] | None) -> None:
            self._row = row

        def execute(self, statement: object) -> ResultStub:
            assert "@@server_uuid" in str(statement)
            return ResultStub(self._row)

    engine_stub = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    identity = _database_identity(
        ConnectionStub(("metadata_db", "uuid-1", "mysql-host", 3306, "8.0.36", "MySQL")),
        engine_stub,
    )
    assert identity["server_uuid"] == "uuid-1"
    assert identity["server_port"] == 3306
    with pytest.raises(MigrationApplyError, match="server_uuid_unavailable"):
        _database_identity(
            ConnectionStub(("metadata_db", None, "mysql-host", 3306, "8.0.36", "MySQL")),
            engine_stub,
        )

    with pytest.raises(MigrationApplyError, match="current_database_unavailable"):
        _database_identity(ConnectionStub(None), engine_stub)
    with pytest.raises(MigrationApplyError, match="dialect_unsupported"):
        _database_identity(
            ConnectionStub(None), SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
        )


def test_provenance_revalidates_mapping_and_hash_pins(
    tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    from opendata.migration_apply import (
        _build_provenance,
        _inspect_database_identity,
        _single_task,
    )

    scenario = _scenario(tmp_path, request)
    metadata, target = scenario[:2]
    plan, mapping = scenario[6], scenario[4]
    job = _single_task(plan, TASK_ID)
    actual = inspect_warehouse_fingerprint(target)
    args = {
        "task_id": TASK_ID,
        "job": job,
        "mapping_document": mapping,
        "mapping_row": mapping["tasks"][0],
        "plan": plan,
        "actual": actual,
        "metadata_identity_sha256": _inspect_database_identity(metadata),
    }
    receipt = _build_provenance(**args)
    assert (
        receipt["input_sha256"]["runtime_inventory_sha256"]
        == plan["inputs"]["runtime_inventory_sha256"]
    )

    changed_mapping_row = copy.deepcopy(mapping["tasks"][0])
    changed_mapping_row["review"]["reviewer_id"] = "other-reviewer"
    with pytest.raises(MigrationApplyError, match="reviewed_mapping_review_differs"):
        _build_provenance(**{**args, "mapping_row": changed_mapping_row})

    bad_pins = copy.deepcopy(mapping)
    bad_pins["inputs"]["runtime_inventory_sha256"] = "0" * 64
    with pytest.raises(MigrationApplyError, match="runtime_pin_mismatch"):
        _build_provenance(**{**args, "mapping_document": bad_pins})

    bad_hash_plan = copy.deepcopy(plan)
    bad_hash_plan["inputs"]["runtime_inventory_sha256"] = "invalid"
    with pytest.raises(MigrationApplyError, match="hash_pin_missing"):
        _build_provenance(**{**args, "plan": bad_hash_plan})

    bad_target = WarehouseFingerprintObservation(
        sha256="0" * 64,
        database_identity_sha256=actual.database_identity_sha256,
    )
    with pytest.raises(MigrationApplyError, match="does_not_match_recomputed_plan"):
        _build_provenance(**{**args, "actual": bad_target})

    for field, value, error in (
        ("static_catalog_sha256", "0" * 64, "catalog_pin_mismatch"),
        ("target_allowlist_sha256", "0" * 64, "allowlist_pin_mismatch"),
    ):
        bad_document = copy.deepcopy(mapping)
        bad_document["inputs"][field] = value
        with pytest.raises(MigrationApplyError, match=error):
            _build_provenance(**{**args, "mapping_document": bad_document})

    bad_plan = copy.deepcopy(plan)
    bad_plan["plan_sha256"] = "invalid"
    with pytest.raises(MigrationApplyError, match="plan_sha256_missing"):
        _build_provenance(**{**args, "plan": bad_plan})

    bad_plan = copy.deepcopy(plan)
    bad_plan["inputs"] = None
    with pytest.raises(MigrationApplyError, match="input_pins_missing"):
        _build_provenance(**{**args, "plan": bad_plan})

    for field, value, error in (
        ("target", None, "target_invalid"),
        ("review", None, "review_invalid"),
        ("target_schedule", None, "schedule_invalid"),
        ("execution_governance", None, "audit_fields_invalid"),
    ):
        bad_job = {**job, field: value}
        with pytest.raises(MigrationApplyError, match=error):
            _build_provenance(**{**args, "job": bad_job})
