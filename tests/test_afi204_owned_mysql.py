"""Opt-in MySQL acceptance in a freshly owned, disposable database.

The administrative URL is injected into the test process, never committed or
printed. The fixture never selects or mutates an existing application schema.
Every database it creates has a random, validated ``od204_test_`` name and is
dropped in ``finally``. An ordinary test run explicitly skips this suite.
"""

from __future__ import annotations

import importlib.util
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, inspect, select
from sqlalchemy.engine import make_url

from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger


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
