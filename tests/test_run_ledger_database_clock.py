"""Database-clock policy tests for cross-host lease decisions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import sleep
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import Column, Engine, Integer, MetaData, Table, create_engine, insert, select

import opendata.pipeline.run_ledger as run_ledger_module
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunLeaseExpiredError,
    RunLedger,
)

if TYPE_CHECKING:
    from pathlib import Path

PAST = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)
SNAPSHOT = '{"dataset":"daily"}'
FACT_METADATA = MetaData()
FACT_TABLE = Table(
    "database_clock_test_facts",
    FACT_METADATA,
    Column("value", Integer(), primary_key=True),
)


@pytest.fixture
def database_setup(tmp_path: Path) -> tuple[Engine, RunLedger]:
    """Provide a file-backed SQLite ledger that uses its own database clock."""
    engine = create_engine(f"sqlite:///{tmp_path / 'database-clock.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    FACT_METADATA.create_all(engine)
    ledger = RunLedger(engine, clock_policy="database")
    yield engine, ledger
    engine.dispose()


def _near_database_time(value: datetime) -> None:
    """Assert a stored timestamp came from the current real database clock."""
    difference = abs((datetime.now(timezone.utc) - value).total_seconds())
    assert difference < 5


def test_sqlite_database_clock_timestamps_every_ledger_transition(database_setup) -> None:
    _, ledger = database_setup

    queued = ledger.enqueue("database-clock-run", SNAPSHOT)
    _near_database_time(queued.created_at)
    assert queued.created_at.microsecond % 1_000 == 0

    claimed = ledger.claim("worker-a", PAST, timedelta(seconds=30))
    assert claimed is not None
    _near_database_time(claimed.started_at)
    assert claimed.lease_expires_at == claimed.started_at + timedelta(seconds=30)

    heartbeated = ledger.heartbeat(
        claimed.run_key,
        "worker-a",
        claimed.fencing_token,
        PAST,
        timedelta(seconds=30),
    )
    assert heartbeated is not None
    _near_database_time(heartbeated.heartbeat_at)

    finished = ledger.finish(
        heartbeated.run_key,
        "worker-a",
        heartbeated.fencing_token,
        PAST,
    )
    assert finished is not None
    _near_database_time(finished.finished_at)

    failed_queue = ledger.enqueue("database-clock-failed", SNAPSHOT)
    failed_claim = ledger.claim("worker-b", PAST, timedelta(seconds=30))
    assert failed_claim is not None
    assert failed_claim.run_key == failed_queue.run_key
    failed = ledger.fail(
        failed_claim.run_key,
        "worker-b",
        failed_claim.fencing_token,
        PAST,
        "normalization failed",
    )
    assert failed is not None
    _near_database_time(failed.finished_at)

    cancelled_queue = ledger.enqueue("database-clock-cancelled", SNAPSHOT)
    cancelled = ledger.cancel(cancelled_queue.run_key, PAST)
    assert cancelled is not None
    _near_database_time(cancelled.finished_at)


def test_database_policy_ignores_worker_clock_skew_for_success_commit(database_setup) -> None:
    _, ledger = database_setup
    queued = ledger.enqueue("skewed-worker", SNAPSHOT)
    claim = ledger.claim("worker-a", PAST, timedelta(seconds=30))
    assert claim is not None
    assert claim.run_key == queued.run_key

    result = ledger.commit_result(
        claim.run_key,
        worker="worker-a",
        fencing_token=claim.fencing_token,
        now=FUTURE,
        operation=lambda _connection: "committed",
    )

    assert result is not None
    assert result[1] == "committed"
    assert result[0].status == "succeeded"
    _near_database_time(result[0].finished_at)


def test_database_clock_rejects_commit_when_database_lease_expires_mid_write(
    database_setup,
) -> None:
    engine, ledger = database_setup
    queued = ledger.enqueue("slow-worker", SNAPSHOT)
    claim = ledger.claim("worker-a", PAST, timedelta(seconds=1))
    assert claim is not None
    assert claim.run_key == queued.run_key

    def slow_fact_write(connection):
        connection.execute(insert(FACT_TABLE).values(value=1))
        sleep(1.1)

    with pytest.raises(RunLeaseExpiredError, match="expired before"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=PAST,
            operation=slow_fact_write,
        )

    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(select(FACT_TABLE.c.value)).scalars().all() == []


def test_database_policy_rejects_commit_clock_override(database_setup) -> None:
    _, ledger = database_setup
    queued = ledger.enqueue("clock-override", SNAPSHOT)
    claim = ledger.claim("worker-a", PAST, timedelta(seconds=30))
    assert claim is not None
    assert claim.run_key == queued.run_key
    operation_calls: list[str] = []

    with pytest.raises(ValueError, match="clock overrides are forbidden"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=PAST,
            operation=lambda _connection: operation_calls.append("called"),
            clock=lambda: FUTURE,
        )

    assert operation_calls == []
    assert ledger.get(claim.run_key) == claim


def test_database_policy_never_falls_back_to_application_clock(
    database_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger = database_setup

    def forbidden_application_clock() -> datetime:
        raise AssertionError("database clock policy used the application host clock")

    monkeypatch.setattr(run_ledger_module, "_application_utc_now", forbidden_application_clock)
    queued = ledger.enqueue("no-fallback", SNAPSHOT)
    claim = ledger.claim("worker-a", PAST, timedelta(seconds=30))
    assert claim is not None
    assert claim.run_key == queued.run_key
    assert ledger.finish(claim.run_key, "worker-a", claim.fencing_token, PAST) is not None


@pytest.mark.parametrize(
    ("dialect_name", "database_value", "expected_sql"),
    [
        (
            "mysql",
            datetime(2026, 10, 26, 9, 30, 0, 123456),
            "SELECT UTC_TIMESTAMP(6)",
        ),
        (
            "postgresql",
            datetime(2026, 10, 26, 9, 30, 0, 123456, tzinfo=timezone.utc),
            "SELECT clock_timestamp()",
        ),
    ],
)
def test_database_clock_uses_dialect_server_function(
    dialect_name: str, database_value: datetime, expected_sql: str
) -> None:
    statements: list[str] = []

    class Result:
        def scalar_one(self) -> datetime:
            return database_value

    class Connection:
        dialect = SimpleNamespace(name=dialect_name)

        def exec_driver_sql(self, statement: str) -> Result:
            statements.append(statement)
            return Result()

    timestamp_us = run_ledger_module._database_clock_us(Connection())

    assert timestamp_us == run_ledger_module._datetime_to_epoch_us(
        database_value.replace(tzinfo=timezone.utc)
        if database_value.tzinfo is None
        else database_value
    )
    assert statements == [expected_sql]


def test_database_clock_fails_closed_for_unsupported_dialect() -> None:
    class Connection:
        dialect = SimpleNamespace(name="unsupported")

        def exec_driver_sql(self, _statement: str) -> None:
            raise AssertionError("unsupported database clock must not attempt a fallback")

    with pytest.raises(RuntimeError, match="database clock is unsupported"):
        run_ledger_module._database_clock_us(Connection())
