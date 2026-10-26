"""Offline boundary tests for the persistent pipeline run ledger."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    Table,
    create_engine,
    insert,
    text,
    update,
)

import opendata.pipeline.run_ledger as run_ledger_module
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
    RunRecord,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = datetime(2026, 10, 26, 9, 30, 0, 123456, tzinfo=timezone.utc)
SNAPSHOT = '{"dataset":"daily"}'
FACT_METADATA = MetaData()
FACT_TABLE = Table(
    "run_ledger_boundary_facts",
    FACT_METADATA,
    Column("value", Integer(), primary_key=True),
)


@pytest.fixture
def ledger_setup(tmp_path: Path) -> Iterator[tuple[Engine, RunLedger]]:
    """Use a private SQLite file and always release its pool."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'run-ledger-boundaries.sqlite'}",
        connect_args={"timeout": 5},
    )
    RUN_LEDGER_METADATA.create_all(engine)
    FACT_METADATA.create_all(engine)
    try:
        yield engine, RunLedger(engine)
    finally:
        engine.dispose()


def _claimed(ledger: RunLedger, run_key: str) -> RunRecord:
    """Enqueue and claim one run at a deterministic caller-clock instant."""
    ledger.enqueue(run_key, SNAPSHOT)
    claim = ledger.claim("worker-a", NOW, timedelta(seconds=10))
    assert claim is not None
    assert claim.run_key == run_key
    return claim


def test_constructor_rejects_unknown_and_unsupported_clock_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    try:
        with pytest.raises(ValueError, match="clock_policy"):
            RunLedger(engine, clock_policy="host")

        monkeypatch.setattr(engine.dialect, "name", "unsupported")
        with pytest.raises(ValueError, match="unsupported"):
            RunLedger(engine, clock_policy="database")
    finally:
        engine.dispose()


def test_commit_result_rejects_invalid_callback_inputs_before_transition(ledger_setup) -> None:
    engine, ledger = ledger_setup
    claim = _claimed(ledger, "bad-callback-inputs")

    with pytest.raises(TypeError, match="operation must be callable"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=NOW + timedelta(seconds=1),
            operation=None,
        )
    with pytest.raises(TypeError, match="clock must be callable"):
        ledger.commit_result(
            claim.run_key,
            worker="worker-a",
            fencing_token=claim.fencing_token,
            now=NOW + timedelta(seconds=1),
            operation=lambda _connection: "unused",
            clock=object(),
        )

    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM run_ledger_boundary_facts")).all() == []


def test_enqueue_rejects_surrogates_that_fail_canonical_utf8_encoding(ledger_setup) -> None:
    _, ledger = ledger_setup

    with pytest.raises(ValueError, match="valid UTF-8"):
        ledger.enqueue("surrogate-snapshot", '{"value":"\\ud800"}')

    assert ledger.get("surrogate-snapshot") is None


def test_fail_rejects_non_utf8_diagnostic_without_changing_the_lease(ledger_setup) -> None:
    _, ledger = ledger_setup
    claim = _claimed(ledger, "surrogate-error")

    with pytest.raises(ValueError, match="valid UTF-8"):
        ledger.fail(
            claim.run_key,
            "worker-a",
            claim.fencing_token,
            NOW + timedelta(seconds=1),
            "invalid diagnostic: \ud800",
        )

    assert ledger.get(claim.run_key) == claim


@pytest.mark.parametrize(
    ("dialect_name", "database_value", "expected_sql", "message"),
    [
        ("sqlite", 17, "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')", "non-text"),
        ("sqlite", "not-a-timestamp", "SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')", "invalid"),
        ("mysql", "2026-10-26 09:30:00.123456", "SELECT UTC_TIMESTAMP(6)", "invalid"),
    ],
)
def test_database_clock_fails_closed_on_malformed_driver_values(
    dialect_name: str,
    database_value: object,
    expected_sql: str,
    message: str,
) -> None:
    statements: list[str] = []

    class Result:
        def scalar_one(self) -> object:
            return database_value

    class Connection:
        dialect = SimpleNamespace(name=dialect_name)

        def exec_driver_sql(self, statement: str) -> Result:
            statements.append(statement)
            return Result()

    with pytest.raises(RuntimeError, match=message):
        run_ledger_module._database_clock_us(Connection())

    assert statements == [expected_sql]


def test_callback_cannot_change_its_run_row_and_commit_its_warehouse_write(ledger_setup) -> None:
    engine, ledger = ledger_setup
    claim = _claimed(ledger, "callback-mutated-ledger")

    def mutate_ledger_row(connection) -> str:
        connection.execute(insert(FACT_TABLE).values(value=71))
        connection.execute(
            update(RUN_LEDGER_TABLE)
            .where(RUN_LEDGER_TABLE.c.run_key == claim.run_key)
            .values(heartbeat_at_us=0)
        )
        return "must-roll-back"

    result = ledger.commit_result(
        claim.run_key,
        worker="worker-a",
        fencing_token=claim.fencing_token,
        now=NOW + timedelta(seconds=1),
        operation=mutate_ledger_row,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert result is None
    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM run_ledger_boundary_facts")).all() == []


def test_callback_write_rolls_back_when_database_suppresses_success_update(ledger_setup) -> None:
    engine, ledger = ledger_setup
    claim = _claimed(ledger, "success-update-ignored")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TRIGGER ignore_run_success
            BEFORE UPDATE OF status ON od_run_ledger
            WHEN NEW.status = 'succeeded'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )

    def write_fact(connection) -> str:
        connection.execute(insert(FACT_TABLE).values(value=72))
        return "must-roll-back"

    result = ledger.commit_result(
        claim.run_key,
        worker="worker-a",
        fencing_token=claim.fencing_token,
        now=NOW + timedelta(seconds=1),
        operation=write_fact,
        clock=lambda: NOW + timedelta(seconds=2),
    )

    assert result is None
    assert ledger.get(claim.run_key) == claim
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM run_ledger_boundary_facts")).all() == []


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("fencing_token", "not-an-integer", "invalid fencing_token"),
        ("status", "corrupt-status", "invalid status"),
        ("worker", b"not-text", "invalid worker"),
    ],
)
def test_read_fails_closed_when_a_ledger_row_has_corrupt_storage(
    ledger_setup, column: str, value: object, message: str
) -> None:
    engine, ledger = ledger_setup
    claim = ledger.enqueue("corrupt-storage", SNAPSHOT)
    columns = {
        "fencing_token": RUN_LEDGER_TABLE.c.fencing_token,
        "status": RUN_LEDGER_TABLE.c.status,
        "worker": RUN_LEDGER_TABLE.c.worker,
    }

    with engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            update(RUN_LEDGER_TABLE)
            .where(RUN_LEDGER_TABLE.c.run_key == claim.run_key)
            .values({columns[column]: value})
        )

    with pytest.raises(RuntimeError, match=message):
        ledger.get(claim.run_key)
