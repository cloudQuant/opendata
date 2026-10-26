"""Atomic warehouse writes guarded by a live run lease."""

from __future__ import annotations

import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    Table,
    create_engine,
    insert,
    inspect,
    select,
)

from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunLeaseExpiredError,
    RunLedger,
)

NOW = datetime(2026, 10, 26, 9, 30, 0, 123456, tzinfo=timezone.utc)
SNAPSHOT = '{"dataset":"daily","symbol":"600519"}'
FACT_METADATA = MetaData()
FACT_TABLE = Table(
    "atomic_test_facts",
    FACT_METADATA,
    Column("value", Integer(), primary_key=True),
)


def _engine(path: Path) -> Engine:
    """Create an independent SQLite pool with bounded lock waits."""
    return create_engine(f"sqlite:///{path}", connect_args={"timeout": 15})


@pytest.fixture
def atomic_setup(tmp_path: Path) -> tuple[Engine, RunLedger]:
    """Provide a file-backed warehouse ledger and a fact table."""
    engine = _engine(tmp_path / "atomic-run-ledger.sqlite")
    RUN_LEDGER_METADATA.create_all(engine)
    FACT_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    yield engine, ledger
    engine.dispose()


def _claimed(ledger: RunLedger, run_key: str = "atomic-run"):
    """Insert one queued run and return its worker lease."""
    ledger.enqueue(run_key, SNAPSHOT)
    claimed = ledger.claim("worker-a", NOW, timedelta(seconds=10))
    assert claimed is not None
    return claimed


def test_commit_result_atomically_writes_fact_and_success(atomic_setup) -> None:
    engine, ledger = atomic_setup
    claim = _claimed(ledger)

    def write_fact(connection):
        connection.execute(insert(FACT_TABLE).values(value=17))
        return "warehouse-receipt-17"

    result = ledger.commit_result(
        claim.run_key,
        worker="worker-a",
        fencing_token=claim.fencing_token,
        now=NOW + timedelta(seconds=1),
        operation=write_fact,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert result is not None
    completed, receipt = result
    assert completed.status == "succeeded"
    assert completed.fencing_token == claim.fencing_token
    assert completed.finished_at == NOW + timedelta(seconds=2)
    assert receipt == "warehouse-receipt-17"
    with engine.connect() as connection:
        assert connection.execute(select(FACT_TABLE.c.value)).scalars().all() == [17]
        row = (
            connection.execute(select(RUN_LEDGER_METADATA.tables["od_run_ledger"])).mappings().one()
        )
        assert row["status"] == "succeeded"
        assert row["finished_at_us"] is not None


def test_commit_result_rejects_old_cancelled_and_expired_leases(atomic_setup) -> None:
    _, ledger = atomic_setup
    old_claim = _claimed(ledger, "old-token")
    replacement = ledger.claim("worker-b", NOW + timedelta(seconds=10), timedelta(seconds=10))
    assert replacement is not None
    called: list[str] = []

    def record_call(_connection):
        called.append("called")

    assert (
        ledger.commit_result(
            old_claim.run_key,
            worker="worker-a",
            fencing_token=old_claim.fencing_token,
            now=NOW + timedelta(seconds=11),
            operation=record_call,
            clock=lambda: NOW + timedelta(seconds=12),
        )
        is None
    )
    assert called == []

    cancelled_claim = _claimed(ledger, "cancelled")
    assert ledger.cancel(cancelled_claim.run_key, NOW + timedelta(seconds=1)) is not None
    assert (
        ledger.commit_result(
            cancelled_claim.run_key,
            worker="worker-a",
            fencing_token=cancelled_claim.fencing_token,
            now=NOW + timedelta(seconds=2),
            operation=record_call,
            clock=lambda: NOW + timedelta(seconds=3),
        )
        is None
    )
    assert called == []

    expired_claim = _claimed(ledger, "expired")
    assert (
        ledger.commit_result(
            expired_claim.run_key,
            worker="worker-a",
            fencing_token=expired_claim.fencing_token,
            now=NOW + timedelta(seconds=10),
            operation=record_call,
            clock=lambda: NOW + timedelta(seconds=10),
        )
        is None
    )
    assert called == []


def test_operation_exception_rolls_back_warehouse_write_and_success(atomic_setup) -> None:
    engine, ledger = atomic_setup
    claim = _claimed(ledger)

    def write_then_fail(connection):
        connection.execute(insert(FACT_TABLE).values(value=23))
        raise LookupError("normalizer rejected an inconsistent row")

    with pytest.raises(LookupError, match="inconsistent row"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=NOW + timedelta(seconds=1),
            operation=write_then_fail,
            clock=lambda: NOW + timedelta(seconds=2),
        )

    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(select(FACT_TABLE.c.value)).scalars().all() == []


def test_expiry_after_operation_rolls_back_fact_and_success(atomic_setup) -> None:
    engine, ledger = atomic_setup
    claim = _claimed(ledger)

    def write_fact(connection):
        connection.execute(insert(FACT_TABLE).values(value=31))
        return "must-not-commit"

    with pytest.raises(RunLeaseExpiredError, match="expired before"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=NOW + timedelta(seconds=1),
            operation=write_fact,
            clock=lambda: NOW + timedelta(seconds=10),
        )

    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(select(FACT_TABLE.c.value)).scalars().all() == []


def test_sqlite_write_lock_prevents_concurrent_expired_lease_takeover(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lease-takeover-race.sqlite"
    setup_engine = _engine(database_path)
    RUN_LEDGER_METADATA.create_all(setup_engine)
    FACT_METADATA.create_all(setup_engine)
    setup_ledger = RunLedger(setup_engine)
    claim = _claimed(setup_ledger, "race-run")
    first_engine = _engine(database_path)
    second_engine = _engine(database_path)
    writer_ledger = RunLedger(first_engine)
    takeover_ledger = RunLedger(second_engine)
    operation_entered = Event()
    allow_operation_return = Event()
    takeover_started = Event()

    def write_fact(connection):
        connection.execute(insert(FACT_TABLE).values(value=41))
        operation_entered.set()
        if not allow_operation_return.wait(timeout=5):
            raise TimeoutError("test did not release the fenced warehouse write")
        return "race-result"

    def attempt_takeover():
        takeover_started.set()
        return takeover_ledger.claim(
            "worker-b",
            NOW + timedelta(seconds=10),
            timedelta(seconds=10),
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            commit_future = pool.submit(
                writer_ledger.commit_result,
                claim.run_key,
                worker="worker-a",
                fencing_token=claim.fencing_token,
                now=NOW + timedelta(seconds=1),
                operation=write_fact,
                clock=lambda: NOW + timedelta(seconds=2),
            )
            assert operation_entered.wait(timeout=5)
            takeover_future = pool.submit(attempt_takeover)
            assert takeover_started.wait(timeout=5)
            assert not takeover_future.done()
            allow_operation_return.set()
            committed = commit_future.result(timeout=10)
            takeover = takeover_future.result(timeout=10)
    finally:
        allow_operation_return.set()
        first_engine.dispose()
        second_engine.dispose()
        setup_engine.dispose()

    assert committed is not None
    assert committed[0].status == "succeeded"
    assert takeover is None
    with setup_engine.connect() as connection:
        assert connection.execute(select(FACT_TABLE.c.value)).scalars().all() == [41]


def test_warehouse_run_ledger_migration_upgrades_and_downgrades_sqlite() -> None:
    """Exercise the hand-written revision DDL without importing runtime metadata."""
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic_data"
        / "versions"
        / "20261026-0010_run_ledger.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0010_run_ledger", migration_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    engine = create_engine("sqlite:///:memory:")

    try:
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            module.upgrade()
        inspector = inspect(engine)
        assert inspector.has_table("od_run_ledger")
        assert {column["name"] for column in inspector.get_columns("od_run_ledger")} == {
            "run_key",
            "job_snapshot_json",
            "status",
            "worker",
            "lease_id",
            "fencing_token",
            "lease_expires_at_us",
            "created_at_us",
            "updated_at_us",
            "started_at_us",
            "heartbeat_at_us",
            "finished_at_us",
            "error_text",
        }
        assert {index["name"] for index in inspector.get_indexes("od_run_ledger")} == {
            "ix_od_run_ledger_claim"
        }
        with (
            engine.begin() as connection,
            Operations.context(MigrationContext.configure(connection)),
        ):
            module.downgrade()
        assert not inspect(engine).has_table("od_run_ledger")
    finally:
        engine.dispose()
