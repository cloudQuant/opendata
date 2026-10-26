"""Opt-in MySQL acceptance in a freshly owned, disposable database.

The administrative URL is injected into the test process, never committed or
printed. The fixture never selects or mutates an existing application schema.
Every database it creates has a random, validated ``od204_test_`` name and is
dropped in ``finally``. An ordinary test run explicitly skips this suite.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    Table,
    create_engine,
    insert,
    inspect,
    select,
    update,
)
from sqlalchemy.engine import make_url

from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger


@pytest.fixture
def owned_mysql():
    """Create a uniquely owned schema; never reuse a requested database name."""
    presented = os.environ.get("OD204_MYSQL_ADMIN_URL")
    if not presented:
        pytest.skip("owned MySQL not configured; set OD204_MYSQL_ADMIN_URL explicitly")
    try:
        url = make_url(presented)
        if url.get_backend_name() != "mysql":
            pytest.fail("owned MySQL harness requires a MySQL administrative URL")
        admin = create_engine(
            url.set(drivername="mysql+pymysql", database=None),
            connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15},
        )
    except Exception as exc:
        pytest.fail(f"owned MySQL configuration failed ({type(exc).__name__})", pytrace=False)
    name = "od204_test_" + uuid4().hex
    assert re.fullmatch(r"od204_test_[a-f0-9]{32}", name)
    created = False
    engine = None
    try:
        try:
            with admin.connect() as connection:
                # Only our generated identifier is interpolated. No value from the
                # supplied URL can become a CREATE/DROP target.
                connection.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
                created = True
        except Exception as exc:
            pytest.fail(
                f"owned MySQL initialization unavailable ({type(exc).__name__})", pytrace=False
            )
        engine = create_engine(
            url.set(drivername="mysql+pymysql", database=name),
            connect_args={"connect_timeout": 5, "read_timeout": 15, "write_timeout": 15},
            pool_size=8,
        )
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT DATABASE()").scalar_one() == name
            assert inspect(connection).get_table_names() == []
        yield engine
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.connect() as connection:
                connection.exec_driver_sql(f"DROP DATABASE `{name}`")
        admin.dispose()


def _run_ledger_revision():
    """Load the literal warehouse revision independently of runtime metadata."""
    path = Path(__file__).resolve().parents[1] / "alembic_data/versions/20261026-0010_run_ledger.py"
    spec = importlib.util.spec_from_file_location("afi204_run_ledger_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _publication_revision():
    """Load the frozen publication revision for both dialect acceptance legs."""
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic_data/versions/20261026-0011_publication_snapshots.py"
    )
    spec = importlib.util.spec_from_file_location("afi204_publication_revision", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_owned_mysql_run_ledger_migration_roundtrip(owned_mysql):
    """Literal migration and repository agree on a real MySQL table."""
    engine = owned_mysql
    revision = _run_ledger_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        assert inspect(connection).get_table_names() == ["od_run_ledger"]
        expected = {column.name for column in RUN_LEDGER_METADATA.tables["od_run_ledger"].columns}
        assert {
            column["name"] for column in inspect(connection).get_columns("od_run_ledger")
        } == expected
    ledger = RunLedger(engine)
    queued = ledger.enqueue("mysql-migration-run", '{"dataset":"stock.daily"}')
    assert queued.status == "queued"
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
        assert inspect(connection).get_table_names() == []


def test_owned_mysql_concurrent_claim_and_fenced_atomic_writes(owned_mysql):
    """Two real pools cannot publish with a reclaimed or cancelled lease."""
    engine = owned_mysql
    RUN_LEDGER_METADATA.create_all(engine)
    metadata = MetaData()
    facts = Table("test_facts", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    other_engine = create_engine(engine.url, pool_size=4)
    first = RunLedger(engine)
    second = RunLedger(other_engine)
    now = datetime.now(timezone.utc)
    first.enqueue("one-run", '{"dataset":"stock.daily"}')
    gate = Barrier(2)

    def claim(ledger, worker):
        gate.wait(timeout=10)
        return ledger.claim(worker, now, timedelta(seconds=10))

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(claim, first, "worker-a"),
                pool.submit(claim, second, "worker-b"),
            ]
            claims = [future.result(timeout=15) for future in futures]
        winners = [record for record in claims if record is not None]
        assert len(winners) == 1
        initial = winners[0]
        reclaimed_at = now + timedelta(seconds=11)
        reclaimed = second.claim("worker-c", reclaimed_at, timedelta(seconds=20))
        assert reclaimed is not None
        assert reclaimed.fencing_token == initial.fencing_token + 1
        called = []

        def stale_operation(connection):
            called.append(True)
            connection.execute(insert(facts).values(id=1))

        assert (
            first.commit_result(
                initial.run_key,
                worker=initial.worker,
                fencing_token=initial.fencing_token,
                now=reclaimed_at,
                operation=stale_operation,
                clock=lambda: reclaimed_at,
            )
            is None
        )
        assert called == []

        def throwing_operation(connection):
            connection.execute(insert(facts).values(id=2))
            raise ValueError("intentional rollback")

        with pytest.raises(ValueError, match="intentional rollback"):
            second.commit_result(
                reclaimed.run_key,
                worker="worker-c",
                fencing_token=reclaimed.fencing_token,
                now=reclaimed_at,
                operation=throwing_operation,
                clock=lambda: reclaimed_at,
            )
        with engine.connect() as connection:
            assert connection.execute(select(facts)).all() == []
        assert first.get("one-run").status == "running"

        def successful_operation(connection):
            connection.execute(insert(facts).values(id=3))
            return "receipt-3"

        completed = second.commit_result(
            reclaimed.run_key,
            worker="worker-c",
            fencing_token=reclaimed.fencing_token,
            now=reclaimed_at,
            operation=successful_operation,
            clock=lambda: reclaimed_at + timedelta(seconds=1),
        )
        assert completed is not None
        assert completed[0].status == "succeeded"
        assert completed[1] == "receipt-3"
        with engine.connect() as connection:
            assert connection.execute(select(facts.c.id)).scalars().all() == [3]

        first.enqueue("cancelled-run", '{"dataset":"stock.daily"}')
        cancelled = first.claim("worker-a", now, timedelta(seconds=20))
        assert cancelled is not None
        second.cancel(cancelled.run_key, now)
        assert (
            first.commit_result(
                cancelled.run_key,
                worker="worker-a",
                fencing_token=cancelled.fencing_token,
                now=now,
                operation=stale_operation,
                clock=lambda: now,
            )
            is None
        )
        assert called == []
    finally:
        other_engine.dispose()


def test_owned_mysql_ledger_exact_identity_and_large_snapshot(owned_mysql):
    """MySQL collation and TEXT capacity must honor the public ledger bounds."""
    RUN_LEDGER_METADATA.create_all(owned_mysql)
    ledger = RunLedger(owned_mysql)
    payload = json.dumps({"padding": "x" * 70_000}, sort_keys=True, separators=(",", ":"))
    ledger.enqueue("CaseKey", payload)
    ledger.enqueue("casekey", payload)
    assert ledger.get("CaseKey").run_key == "CaseKey"
    assert ledger.get("casekey").run_key == "casekey"
    assert ledger.get("CaseKey").job_snapshot_json == payload
    now = datetime.now(timezone.utc)
    claim = ledger.claim("CaseWorker", now, timedelta(seconds=30))
    assert claim is not None
    assert (
        ledger.heartbeat(
            claim.run_key, "caseworker", claim.fencing_token, now, timedelta(seconds=30)
        )
        is None
    )
    assert ledger.get(claim.run_key).worker == "CaseWorker"


def test_owned_mysql_database_clock_rejects_forged_worker_expiry(owned_mysql):
    """A stale worker timestamp cannot extend a warehouse-authoritative lease."""
    RUN_LEDGER_METADATA.create_all(owned_mysql)
    ledger = RunLedger(owned_mysql, clock_policy="database")
    ledger.enqueue("db-clock", '{"dataset":"fixture"}')
    forged_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    forged_future = datetime(2200, 1, 1, tzinfo=timezone.utc)
    first = ledger.claim("clock-worker-a", forged_past, timedelta(minutes=1))
    assert first is not None and first.created_at.year > 2000
    assert first.lease_expires_at.year < 2200
    renewed = ledger.heartbeat(
        first.run_key, "clock-worker-a", first.fencing_token, forged_past, timedelta(minutes=1)
    )
    assert renewed is not None and renewed.updated_at.year > 2000
    called = []

    def write_result(_connection):
        called.append(True)

    with pytest.raises(ValueError, match="clock"):
        ledger.commit_result(
            first.run_key,
            worker="clock-worker-a",
            fencing_token=first.fencing_token,
            now=forged_past,
            operation=write_result,
            clock=lambda: forged_past,
        )
    with owned_mysql.begin() as connection:
        # Test-owned fault injection establishes expiry without sleeping or
        # relying on this machine's wall clock.
        connection.execute(
            update(RUN_LEDGER_TABLE)
            .where(RUN_LEDGER_TABLE.c.run_key == first.run_key)
            .values(lease_expires_at_us=0)
        )
    assert (
        ledger.commit_result(
            first.run_key,
            worker="clock-worker-a",
            fencing_token=first.fencing_token,
            now=forged_past,
            operation=write_result,
        )
        is None
    )
    assert called == []
    reclaimed = ledger.claim("clock-worker-b", forged_future, timedelta(minutes=1))
    assert reclaimed is not None
    assert reclaimed.fencing_token == first.fencing_token + 1
    assert reclaimed.lease_expires_at.year < 2200


def test_owned_mysql_publication_migration_revisions_and_snapshot(owned_mysql):
    """Read fixed snapshots from literal MySQL DDL, retaining 70 KiB rows."""
    from opendata.pipeline.publication_store import (
        PUBLICATION_METADATA,
        PublicationConflictError,
        PublicationStore,
        PublishedRecordInput,
    )

    engine = owned_mysql
    revision = _publication_revision()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.upgrade()
        observed = inspect(connection)
        assert set(observed.get_table_names()) == set(PUBLICATION_METADATA.tables)
        for name, table in PUBLICATION_METADATA.tables.items():
            assert {column["name"] for column in observed.get_columns(name)} == {
                column.name for column in table.columns
            }
            actual_indexes = {
                (tuple(index["column_names"]), bool(index["unique"]))
                for index in observed.get_indexes(name)
            }
            declared_indexes = {
                (tuple(column.name for column in index.columns), index.unique)
                for index in table.indexes
            }
            # InnoDB adds a supporting index for each foreign key unless an
            # explicitly declared index already covers it.
            implicit_foreign_key_indexes = {
                ((foreign_key.parent.name,), False) for foreign_key in table.foreign_keys
            }
            assert declared_indexes <= actual_indexes
            assert actual_indexes <= declared_indexes | implicit_foreign_key_indexes
    store = PublicationStore(engine, cursor_signing_key=b"mysql-owned-signing-key-only-204!!!")
    day = date(2026, 10, 20)
    arguments = {
        "dataset_id": "stock.daily",
        "source_id": "fixture",
        "schema_version": "1.0",
        "semantic_version": "1.0",
        "unit": "field units declared separately",
        "adjustment_basis": "unadjusted",
        "quality": {"coverage": "UNKNOWN"},
        "provenance": {"fixture": "synthetic, not real source evidence"},
        "watermark": {"last_business_date": day.isoformat()},
    }
    records = [
        PublishedRecordInput("AssetA", {"value": 1, "raw": "x" * 70_000}, business_date=day),
        PublishedRecordInput("asseta", {"value": 2}, business_date=day),
    ]
    first = store.publish(**arguments, idempotency_key="initial", records=records)
    same = store.publish(**arguments, idempotency_key="initial", records=records)
    assert same.publication_id == first.publication_id
    snapshot = store.create_snapshot("stock.daily", page_size=1)
    initial = store.read_page(snapshot.snapshot_id)
    assert len(initial.records) == 1
    assert initial.next_cursor is not None
    store.publish(
        **arguments,
        idempotency_key="revision",
        records=[PublishedRecordInput("AssetA", {"value": 9}, business_date=day)],
    )
    later = store.read_page(snapshot.snapshot_id, cursor=initial.next_cursor)
    assert [row.business_key for row in initial.records + later.records] == ["AssetA", "asseta"]
    assert [row.payload["value"] for row in initial.records + later.records] == [1, 2]
    assert len(initial.records[0].payload["raw"]) == 70_000
    assert later.next_cursor is None
    latest = store.create_snapshot("stock.daily", page_size=10)
    assert [row.payload["value"] for row in store.read_page(latest.snapshot_id).records] == [9, 2]
    with pytest.raises(PublicationConflictError):
        store.publish(**arguments, idempotency_key="initial", records=[])
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            revision.downgrade()
        assert inspect(connection).get_table_names() == []


def test_owned_mysql_run_and_publication_share_one_fenced_transaction(owned_mysql):
    """Savepoints inside a publisher never commit ahead of its run ledger."""
    from opendata.pipeline.publication_store import (
        PUBLICATION_METADATA,
        PublicationStore,
        PublishedRecordInput,
        publish_in_connection,
    )

    RUN_LEDGER_METADATA.create_all(owned_mysql)
    PUBLICATION_METADATA.create_all(owned_mysql)
    ledger = RunLedger(owned_mysql)
    store = PublicationStore(owned_mysql, cursor_signing_key=b"mysql-owned-signing-key-only-204!!!")
    ledger.enqueue("publish-run", '{"dataset":"stock.daily"}')
    now = datetime.now(timezone.utc)
    claim = ledger.claim("publisher", now, timedelta(minutes=1))
    assert claim is not None

    def publish(connection):
        return publish_in_connection(
            connection,
            dataset_id="stock.daily",
            source_id="fixture",
            schema_version="1.0",
            semantic_version="1.0",
            unit="per-field",
            adjustment_basis="unadjusted",
            quality={"status": "unknown", "report_id": None, "issue_count": 0},
            provenance={"fixture": "synthetic"},
            idempotency_key=claim.run_key,
            records=[
                PublishedRecordInput("asset", {"close": 10}, business_date=date(2026, 10, 20))
            ],
            watermark={"date": "2026-10-20"},
        )

    def publish_then_fail(connection):
        publish(connection)
        raise ValueError("publication callback failed")

    with pytest.raises(ValueError, match="publication callback failed"):
        ledger.commit_result(
            claim.run_key,
            worker="publisher",
            fencing_token=claim.fencing_token,
            now=now,
            operation=publish_then_fail,
        )
    assert store.list_publications("stock.daily") == ()
    assert ledger.get(claim.run_key).status == "running"
    completed = ledger.commit_result(
        claim.run_key,
        worker="publisher",
        fencing_token=claim.fencing_token,
        now=now,
        operation=publish,
    )
    assert completed is not None and completed[0].status == "succeeded"
    assert len(store.list_publications("stock.daily")) == 1
    assert store.list_publications("stock.daily")[0].watermark == {"date": "2026-10-20"}


def test_owned_mysql_reviewed_run_to_rest_and_sdk_keeps_revision_snapshot(owned_mysql):
    """Run the real writer, MySQL, REST serializer, and standalone SDK together.

    Acquisition is a declared synthetic fixture. Authentication is overridden
    with a scoped service principal; grant checks run normally on every read.
    This test proves integration, not source fidelity or deployed credentials.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from opendata_client.snapshots import SnapshotClient, SnapshotPermissionError

    from opendata.api.dependencies import Principal, get_current_principal
    from opendata.api.published_access import DatasetGrant, DatasetGrantRegistry
    from opendata.api.published_data import (
        get_dataset_contracts,
        get_dataset_grants,
        get_publication_store,
        router,
    )
    from opendata.data.dataset_contract_registry import DatasetContractRegistry, canonical_sha256
    from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
    from opendata.models.user import User, UserRole
    from opendata.pipeline.contract_publication import ValidatedPublicationWriter
    from opendata.pipeline.publication_store import PUBLICATION_METADATA, PublicationStore
    from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
    from opendata.pipeline.run_service import (
        NormalizedBatch,
        NormalizedRecord,
        ReviewedJob,
        ReviewedJobRegistry,
        RunService,
    )

    RUN_LEDGER_METADATA.create_all(owned_mysql)
    PUBLICATION_METADATA.create_all(owned_mysql)
    ledger = RunLedger(owned_mysql, clock_policy="database")
    store = PublicationStore(owned_mysql, cursor_signing_key=b"owned-chain-signing-key-only-204!!!")
    contract = DatasetContract(
        dataset_id="equity.daily",
        schema_version="1",
        semantic_version="1.0.0",
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="instrument_id", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="close", data_type="number"),
        ),
        business_key=("instrument_id", "trade_date"),
        ordering=("trade_date", "instrument_id"),
        temporal_semantics="business_date",
        time_field="trade_date",
        source=SourceIdentity(source_id="fixture.alpha", source="synthetic-fixture"),
        field_units={"close": "CNY/share"},
        field_currencies={"close": "CNY"},
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={"close": "raw"},
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )
    contracts = DatasetContractRegistry((contract,))
    task = TaskDefinition(
        job_id="owned-synthetic-chain",
        version="v1",
        dataset_id=contract.dataset_id,
        provider=contract.source.source,
        model="fixture-prices",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * 1-5"),
        enabled=True,
    )
    fixture_values = [(date(2026, 10, 20), 100.5), (date(2026, 10, 21), 101.25)]

    def normalize(_raw, _request, _context):
        return NormalizedBatch(
            records=tuple(
                NormalizedRecord(
                    business_key=json.dumps(["AAA", day.isoformat()], separators=(",", ":")),
                    business_date=day,
                    payload={"instrument_id": "AAA", "trade_date": day.isoformat(), "close": price},
                )
                for day, price in fixture_values
            ),
            schema_version="1",
            semantic_version="1.0.0",
            unit="per-field",
            adjustment_basis="raw",
            quality={"status": "unknown", "report_id": None, "issue_count": 0},
            provenance={"fixture": "synthetic; source verification not performed"},
        )

    job = ReviewedJob(
        task=task,
        reviewed=True,
        fetch=lambda _request, _context: None,
        normalize=normalize,
        publish=ValidatedPublicationWriter(contracts),
    )
    service = RunService(ledger, ReviewedJobRegistry((job,)))
    request = RunRequest(run_type="manual", idempotency_key="owned-synthetic-request-1")
    queued = service.submit(task.job_id, task.version, request)
    assert service.submit(task.job_id, task.version, request).run_key == queued.run_key
    outcome = service.run_once("owned-worker", timedelta(minutes=1))
    assert outcome is not None and outcome.record.status == "succeeded"
    assert service.run_once("owned-worker", timedelta(minutes=1)) is None
    now = datetime.now(timezone.utc)
    grants = DatasetGrantRegistry(
        (
            DatasetGrant(
                dataset_id=contract.dataset_id,
                domain="equity",
                source_id=contract.source.source_id,
                permitted_operations=("data.read", "snapshot.create", "snapshot.read"),
                purposes=("research",),
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                review_evidence="OWNED-SYNTHETIC-TEST",
            ),
        )
    )
    principal = Principal(
        user=User(
            id=7,
            username="fixture",
            email="fixture@example.test",
            role=UserRole.USER,
            is_active=True,
            hashed_password="synthetic-test-only",
        ),
        api_key_id=17,
        scopes=("equity", "data.read", "snapshot.create", "snapshot.read"),
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_dataset_contracts] = lambda: contracts
    app.dependency_overrides[get_dataset_grants] = lambda: grants
    app.dependency_overrides[get_publication_store] = lambda: store
    semantics = {
        "version": 1,
        "identity_key_encoding": "canonical-json-array-v1",
        "revision": (
            "latest non-deleted revision per business_key at the pinned publication sequence"
        ),
        "temporal_fields": {
            "business_date": (
                "business-date bucket; null ordering last (route rejects null time values)"
            ),
            "observed_at": "source observation time",
            "source_published_at": "source publication time",
            "available_at": "platform availability time",
            "event_at": "source event time",
        },
        "business_key_fields": ["instrument_id", "trade_date"],
        "ordering_fields": ["trade_date", "instrument_id"],
        "storage_order": [
            {"field": "business_date", "direction": "asc", "nulls": "last", "comparison": "native"},
            {"field": "business_key", "direction": "asc", "comparison": "binary_lexicographic"},
        ],
        "temporal_semantics": "business_date",
        "time_field": "trade_date",
    }
    with (
        TestClient(app) as http_client,
        SnapshotClient(
            "http://test", api_key="owned-synthetic-service", http_client=http_client
        ) as client,
    ):
        snapshot = client.create_snapshot(
            contract.dataset_id,
            start_date="2026-10-20",
            end_date="2026-10-22",
            page_size=1,
            expected_identity_semantics=semantics,
        )
        assert snapshot.contract_sha256 == canonical_sha256(contract)
        fixture_values[:] = [(date(2026, 10, 21), 999.0)]
        service.submit(
            task.job_id,
            task.version,
            RunRequest(run_type="manual", idempotency_key="owned-synthetic-request-2"),
        )
        revised = service.run_once("owned-worker", timedelta(minutes=1))
        assert revised is not None and revised.record.status == "succeeded"
        fixed_rows = list(client.iter_records(snapshot, expected_identity_semantics=semantics))
        assert [row["payload"]["close"] for row in fixed_rows] == [100.5, 101.25]
        latest = client.create_snapshot(
            contract.dataset_id,
            start_date="2026-10-20",
            end_date="2026-10-22",
            page_size=1,
            expected_identity_semantics=semantics,
        )
        latest_rows = list(client.iter_records(latest, expected_identity_semantics=semantics))
        assert [row["payload"]["close"] for row in latest_rows] == [100.5, 999.0]
        app.dependency_overrides[get_dataset_grants] = lambda: DatasetGrantRegistry(())
        with pytest.raises(SnapshotPermissionError):
            client.list_records(snapshot, expected_identity_semantics=semantics)
