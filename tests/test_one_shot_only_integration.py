"""Exercise the reviewed one-shot-only job contract with owned SQLite state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine

from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RunExecutionUncertainError,
    RunLedger,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
    RunService,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
NOW = datetime(2026, 10, 28, 9, 30, tzinfo=UTC)
LEASE_TTL = timedelta(seconds=10)
TRADE_DATE = date(2026, 10, 27)


@dataclass(slots=True)
class OneShotRuntime:
    """Hold the temporary database and explicitly injected synthetic handlers."""

    engine: Engine
    ledger: RunLedger
    dispatcher: RunDispatcher
    service: RunService
    fetch_calls: list[str]
    publish_calls: list[str]
    clock_value: list[datetime]
    task: TaskDefinition


def _task(
    *,
    enabled: bool = False,
    schedule: ScheduleMetadata | None = None,
    job_id: str = "fixture-one-shot",
) -> TaskDefinition:
    """Return a synthetic task with only the requested execution metadata."""
    return TaskDefinition(
        job_id=job_id,
        version="v1",
        dataset_id="fixture.one-shot",
        provider="fixture",
        model="fixture-bars",
        schedule=schedule,
        enabled=enabled,
    )


def _job(
    task: TaskDefinition,
    fetch_calls: list[str],
    *,
    publish_calls: list[str] | None = None,
    one_shot_only: bool = True,
) -> ReviewedJob:
    """Build a reviewed synthetic job whose only observable source action is local."""

    def fetch(_request: RunRequest, context: RunExecutionContext) -> object:
        fetch_calls.append(context.run_key)
        return {"instrument_id": "FIXTURE:ONE", "close": 12.5}

    def normalize(
        raw: object,
        _request: RunRequest,
        _context: RunExecutionContext,
    ) -> NormalizedBatch:
        assert raw == {"instrument_id": "FIXTURE:ONE", "close": 12.5}
        return NormalizedBatch(
            records=(
                NormalizedRecord(
                    business_key='["FIXTURE:ONE","2026-10-27"]',
                    payload={"instrument_id": "FIXTURE:ONE", "close": 12.5},
                    business_date=TRADE_DATE,
                ),
            ),
            schema_version="1.0.0",
            semantic_version="fixture-v1",
            unit="CNY/share",
            adjustment_basis="raw",
            quality={"status": "passed", "synthetic": True},
            provenance={"source": "fixture", "synthetic": True},
        )

    def publish(
        _connection: Connection,
        job_task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        run_key: str,
    ) -> PublicationReceipt:
        if publish_calls is not None:
            publish_calls.append(run_key)
        return PublicationReceipt(
            publication_id=f"fixture-publication-{run_key[:12]}",
            dataset_id=job_task.dataset_id,
            record_count=len(batch.records),
        )

    return ReviewedJob(
        task=task,
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=True,
        one_shot_only=one_shot_only,
    )


@pytest.fixture
def one_shot_runtime(tmp_path: Path) -> Iterator[OneShotRuntime]:
    """Provide private SQLite state and synthetic source and publisher functions."""
    engine = create_engine(f"sqlite:///{tmp_path / 'one-shot-only.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine, clock_policy="caller")
    fetch_calls: list[str] = []
    publish_calls: list[str] = []
    clock_value = [NOW]
    task = _task()
    job = _job(task, fetch_calls, publish_calls=publish_calls)
    catalog = ReviewedTaskCatalog([job])

    def clock() -> datetime:
        return clock_value[0]

    runtime = OneShotRuntime(
        engine=engine,
        ledger=ledger,
        dispatcher=RunDispatcher(ledger, catalog, clock=clock),
        service=RunService(ledger, catalog.registry, clock=clock),
        fetch_calls=fetch_calls,
        publish_calls=publish_calls,
        clock_value=clock_value,
        task=task,
    )
    try:
        yield runtime
    finally:
        engine.dispose()


def _payload(idempotency_key: str) -> dict[str, object]:
    """Return a bounded synthetic one-shot request payload."""
    return {"query": {"instrument_id": "FIXTURE:ONE"}, "idempotency_key": idempotency_key}


def test_one_shot_only_job_requires_disabled_unscheduled_task() -> None:
    """Registry validation binds the marker to an inactive, unscheduled task."""
    enabled_task = _task(
        enabled=True,
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
    )
    scheduled_task = _task(
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        job_id="fixture-scheduled-one-shot",
    )

    with pytest.raises(ValueError, match="one-shot-only jobs must be disabled"):
        ReviewedJobRegistry([_job(enabled_task, [], one_shot_only=True)])
    with pytest.raises(ValueError, match="one-shot-only jobs must not declare a schedule"):
        ReviewedJobRegistry([_job(scheduled_task, [], one_shot_only=True)])


def test_one_shot_dispatch_persists_policy_and_only_exact_key_fetches(
    one_shot_runtime: OneShotRuntime,
) -> None:
    """The dedicated dispatcher writes a one-shot row and exact execution is isolated."""
    runtime = one_shot_runtime
    assert runtime.task.enabled is False
    assert runtime.task.schedule is None

    first = runtime.dispatcher.dispatch_one_shot(
        "fixture-one-shot", "v1", _payload("first-request")
    )
    second = runtime.dispatcher.dispatch_one_shot(
        "fixture-one-shot", "v1", _payload("second-request")
    )
    first_record = runtime.ledger.get(first.identity.run_key)
    second_record = runtime.ledger.get(second.identity.run_key)

    assert first.status == "queued"
    assert first_record is not None and first_record.claim_policy == "one_shot"
    assert second_record is not None and second_record.claim_policy == "one_shot"
    assert runtime.service.run_once("ordinary-worker", LEASE_TTL) is None
    assert runtime.fetch_calls == []

    outcome = runtime.service.run_one_shot(first.identity.run_key, "one-shot-worker", LEASE_TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert outcome.publication == PublicationReceipt(
        publication_id=f"fixture-publication-{first.identity.run_key[:12]}",
        dataset_id=runtime.task.dataset_id,
        record_count=1,
    )
    assert runtime.fetch_calls == [first.identity.run_key]
    assert runtime.publish_calls == [first.identity.run_key]
    assert runtime.ledger.get(second.identity.run_key) == second_record
    repeated = runtime.service.run_one_shot(first.identity.run_key, "one-shot-worker", LEASE_TTL)
    assert repeated is None
    assert runtime.fetch_calls == [first.identity.run_key]


def test_normal_dispatch_and_manually_enqueued_normal_claim_never_fetch_one_shot_job(
    one_shot_runtime: OneShotRuntime,
) -> None:
    """Ordinary APIs and ordinary ledger claims fail before a one-shot fetch."""
    runtime = one_shot_runtime
    request = RunRequest(
        run_type="manual",
        query={"instrument_id": "FIXTURE:ONE"},
        idempotency_key="ordinary-request",
    )
    identity = build_run_identity(runtime.task, request)

    with pytest.raises(ValueError):
        runtime.dispatcher.dispatch_manual("fixture-one-shot", "v1", _payload("ordinary-request"))
    assert runtime.ledger.get(identity.idempotency_key) is None

    ordinary_record = runtime.ledger.enqueue(
        identity.idempotency_key,
        identity.canonical_json,
    )
    assert ordinary_record.claim_policy == "recover_expired"

    outcome = runtime.service.run_once("ordinary-worker", LEASE_TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.run_key == identity.idempotency_key
    assert outcome.record.status == "failed"
    assert runtime.fetch_calls == []
    assert runtime.publish_calls == []


def test_expired_one_shot_claim_is_uncertain_and_never_retried(
    one_shot_runtime: OneShotRuntime,
) -> None:
    """An expired claim stays uncertain and neither broad nor exact replay fetches."""
    runtime = one_shot_runtime
    receipt = runtime.dispatcher.dispatch_one_shot(
        "fixture-one-shot", "v1", _payload("expired-request")
    )
    first_claim = runtime.ledger.claim_run(
        receipt.identity.run_key,
        "interrupted-worker",
        NOW,
        LEASE_TTL,
    )
    assert first_claim is not None

    runtime.clock_value[0] = NOW + LEASE_TTL
    assert runtime.service.run_once("ordinary-worker", LEASE_TTL) is None
    with pytest.raises(RunExecutionUncertainError) as first_retry:
        runtime.service.run_one_shot(receipt.identity.run_key, "retry-worker", LEASE_TTL)

    runtime.clock_value[0] = NOW + timedelta(days=1)
    with pytest.raises(RunExecutionUncertainError) as later_retry:
        runtime.service.run_one_shot(receipt.identity.run_key, "later-worker", LEASE_TTL)

    assert first_retry.value.run_key == receipt.identity.run_key
    assert later_retry.value.run_key == receipt.identity.run_key
    assert runtime.ledger.get(receipt.identity.run_key) == first_claim
    assert runtime.fetch_calls == []
    assert runtime.publish_calls == []


def test_one_shot_dispatch_rejects_an_ordinary_reviewed_job(
    one_shot_runtime: OneShotRuntime,
) -> None:
    """The dedicated route cannot turn an ordinary reviewed task into one-shot."""
    runtime = one_shot_runtime
    normal_task = _task(
        enabled=True,
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        job_id="fixture-normal",
    )
    normal_job = _job(normal_task, runtime.fetch_calls, one_shot_only=False)
    normal_dispatcher = RunDispatcher(
        runtime.ledger,
        ReviewedTaskCatalog([normal_job]),
        clock=lambda: runtime.clock_value[0],
    )

    with pytest.raises(ValueError):
        normal_dispatcher.dispatch_one_shot("fixture-normal", "v1", _payload("wrong-route"))

    assert runtime.fetch_calls == []
