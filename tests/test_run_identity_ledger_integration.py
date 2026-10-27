"""Exercise the run contract and persistent ledger together, without a scheduler."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine

from opendata.pipeline.run_identity import RunRequest, TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunLedger,
    RunLedgerConflictError,
)


def task_definition():
    return TaskDefinition(
        job_id="stock-daily",
        version="1",
        dataset_id="stock_daily",
        provider="akshare",
        model="EquityHistorical",
    )


@pytest.fixture
def ledger_engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'owned-runs.db'}")
    # Table creation is explicit and confined to this owned test database.
    RUN_LEDGER_METADATA.create_all(engine)
    yield engine
    engine.dispose()


def test_same_scheduled_slot_with_changed_query_is_rejected(ledger_engine):
    occurrence = datetime(2026, 10, 26, 8, tzinfo=timezone.utc)
    original = build_run_identity(
        task_definition(),
        RunRequest(run_type="scheduled", occurrence=occurrence, query={"symbol": "600519"}),
    )
    changed = build_run_identity(
        task_definition(),
        RunRequest(run_type="scheduled", occurrence=occurrence, query={"symbol": "000001"}),
    )
    assert original.idempotency_key == changed.idempotency_key
    assert original.payload_hash != changed.payload_hash
    ledger = RunLedger(ledger_engine)
    ledger.enqueue(original.idempotency_key, original.canonical_json)

    with pytest.raises(RunLedgerConflictError):
        ledger.enqueue(changed.idempotency_key, changed.canonical_json)

    stored = ledger.get(original.idempotency_key)
    assert stored is not None
    assert stored.job_snapshot_json == original.canonical_json
    assert stored.status == "queued"


def test_same_instant_retry_survives_repository_restart(ledger_engine):
    first = build_run_identity(
        task_definition(),
        RunRequest(
            run_type="scheduled",
            occurrence=datetime.fromisoformat("2026-10-26T08:00:00+00:00"),
            query={"symbol": "600519", "adjust": "none"},
        ),
    )
    retry = build_run_identity(
        task_definition(),
        RunRequest(
            run_type="scheduled",
            occurrence=datetime.fromisoformat("2026-10-26T16:00:00+08:00"),
            query={"adjust": "none", "symbol": "600519"},
        ),
    )
    RunLedger(ledger_engine).enqueue(first.idempotency_key, first.canonical_json)
    restarted = RunLedger(ledger_engine)
    stored = restarted.enqueue(retry.idempotency_key, retry.canonical_json)
    now = datetime.now(timezone.utc)
    claim = restarted.claim("worker-a", now, timedelta(minutes=1))

    assert stored.run_key == first.idempotency_key
    assert claim is not None
    assert claim.run_key == first.idempotency_key
    assert restarted.claim("worker-b", now, timedelta(minutes=1)) is None
    assert restarted.finish(claim.run_key, "worker-a", claim.fencing_token, now) is not None


def test_manual_and_scheduled_requests_do_not_occupy_each_others_slot(ledger_engine):
    scheduled = build_run_identity(
        task_definition(),
        RunRequest(
            run_type="scheduled",
            occurrence=datetime(2026, 10, 26, 8, tzinfo=timezone.utc),
        ),
    )
    manual = build_run_identity(
        task_definition(),
        RunRequest(run_type="manual", idempotency_key="explicit-request-1"),
    )
    ledger = RunLedger(ledger_engine)
    ledger.enqueue(scheduled.idempotency_key, scheduled.canonical_json)
    ledger.enqueue(manual.idempotency_key, manual.canonical_json)
    now = datetime.now(timezone.utc)
    first = ledger.claim("worker-a", now, timedelta(minutes=1))
    second = ledger.claim("worker-b", now, timedelta(minutes=1))

    assert first is not None and second is not None
    assert {first.run_key, second.run_key} == {scheduled.idempotency_key, manual.idempotency_key}
    assert ledger.claim("worker-c", now, timedelta(minutes=1)) is None


def test_reused_backfill_request_key_rejects_changed_range(ledger_engine):
    from datetime import date

    first = build_run_identity(
        task_definition(),
        RunRequest(
            run_type="backfill",
            idempotency_key="backfill-request-1",
            backfill_start=date(2026, 1, 1),
            backfill_end=date(2026, 1, 31),
        ),
    )
    changed = build_run_identity(
        task_definition(),
        RunRequest(
            run_type="backfill",
            idempotency_key="backfill-request-1",
            backfill_start=date(2026, 1, 1),
            backfill_end=date(2026, 2, 28),
        ),
    )
    ledger = RunLedger(ledger_engine)
    ledger.enqueue(first.idempotency_key, first.canonical_json)
    with pytest.raises(RunLedgerConflictError):
        ledger.enqueue(changed.idempotency_key, changed.canonical_json)
