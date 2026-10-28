"""Behavioral boundaries for jobs that may run only through exact one-shot claims."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
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
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
NOW = datetime(2026, 10, 29, 2, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)


def _task(*, enabled: bool = False, schedule: ScheduleMetadata | None = None) -> TaskDefinition:
    """Build a stable task definition for isolated one-shot contract checks."""
    return TaskDefinition(
        job_id="fuyao-financial-test",
        version="v1",
        dataset_id="cn.stock.financial.test",
        provider="fuyao",
        model="financial-statement-v1",
        schedule=schedule,
        enabled=enabled,
    )


def _request(*, run_type: str = "manual", key: str = "manual-1") -> RunRequest:
    """Build a validated request, including required fields for each run kind."""
    if run_type == "scheduled":
        return RunRequest(run_type="scheduled", occurrence=NOW)
    if run_type == "backfill":
        return RunRequest(
            run_type="backfill",
            idempotency_key=key,
            backfill_start=date(2025, 1, 1),
            backfill_end=date(2025, 1, 2),
        )
    return RunRequest(run_type="manual", idempotency_key=key)


def _batch() -> NormalizedBatch:
    """Return one bounded synthetic fact for publication-path coverage."""
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key="000001.SZ:2025-01-01",
                payload={"revenue": 10},
                business_date=date(2025, 1, 1),
            ),
        ),
        schema_version="1",
        semantic_version="1",
        unit="CNY",
        adjustment_basis="none",
        quality={"rejected_rows": 0},
        provenance={"provider": "fuyao"},
    )


def _job(
    *,
    enabled: bool = False,
    schedule: ScheduleMetadata | None = None,
    one_shot_only: bool = True,
    reviewed: bool = True,
    fetch_calls: list[str] | None = None,
) -> ReviewedJob:
    """Create a reviewed synthetic job and record calls to its fetch boundary."""
    calls = fetch_calls if fetch_calls is not None else []

    def fetch(request: RunRequest, _context: RunExecutionContext) -> object:
        calls.append(request.run_type)
        return {"rows": 1}

    def normalize(
        _raw: object, _request: RunRequest, _context: RunExecutionContext
    ) -> NormalizedBatch:
        return _batch()

    def publish(
        _connection: Connection,
        task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        _idempotency_key: str,
    ) -> PublicationReceipt:
        return PublicationReceipt("publication-1", task.dataset_id, len(batch.records))

    return ReviewedJob(
        task=_task(enabled=enabled, schedule=schedule),
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=reviewed,
        one_shot_only=one_shot_only,
    )


def _sqlite_service(path: Path, job: ReviewedJob) -> tuple[RunService, RunLedger, Engine]:
    """Bind a service to a fresh SQLite ledger owned by this test."""
    engine = create_engine(f"sqlite:///{path}")
    RUN_LEDGER_METADATA.create_all(engine)
    ledger = RunLedger(engine)
    service = RunService(ledger, ReviewedJobRegistry([job]), clock=lambda: NOW)
    return service, ledger, engine


def test_registry_requires_literal_bool_and_disabled_unscheduled_shape() -> None:
    """The marker cannot be truthy-coerced or attached to a schedulable task."""
    with pytest.raises(TypeError, match="one_shot_only must be a bool"):
        ReviewedJobRegistry([replace(_job(), one_shot_only=1)])

    daily_schedule = ScheduleMetadata(kind="cron", expression="0 16 * * 1-5")
    with pytest.raises(ValueError, match="one-shot-only jobs must be disabled"):
        ReviewedJobRegistry([_job(enabled=True, schedule=daily_schedule)])
    with pytest.raises(ValueError, match="one-shot-only jobs must not declare a schedule"):
        ReviewedJobRegistry([_job(schedule=daily_schedule)])

    assert ReviewedJobRegistry([_job()]).resolve("fuyao-financial-test", "v1") is not None


@pytest.mark.parametrize("reviewed", [1, 0, "false", None])
def test_registry_rejects_non_boolean_reviewed_before_enqueue_or_fetch(reviewed: object) -> None:
    """Truthy and falsey non-bools cannot alter the reviewed-job gate."""
    fetch_calls: list[str] = []
    job = replace(_job(fetch_calls=fetch_calls), reviewed=reviewed)
    ledger = Mock()

    with pytest.raises(TypeError, match="reviewed must be a bool"):
        RunService(ledger, ReviewedJobRegistry([job]))

    ledger.enqueue.assert_not_called()
    assert fetch_calls == []


@pytest.mark.parametrize("reviewed", [True, False])
def test_registry_preserves_literal_reviewed_boolean_values(reviewed: bool) -> None:
    """Both supported review states remain valid and retain their exact value."""
    job = _job(reviewed=reviewed)

    registered = ReviewedJobRegistry([job]).resolve("fuyao-financial-test", "v1")

    assert registered is job
    assert registered.reviewed is reviewed


def test_one_shot_only_submit_requires_reviewed_manual_job_and_blocks_regular_submit(
    tmp_path: Path,
) -> None:
    """Only explicit manual submission is accepted and ordinary submit stays closed."""
    service, ledger, engine = _sqlite_service(tmp_path / "one-shot-only-submit.sqlite", _job())
    try:
        with pytest.raises(ValueError, match="one-shot-only"):
            service.submit("fuyao-financial-test", "v1", _request())
        with pytest.raises(ValueError, match="manual run request"):
            service.submit_one_shot("fuyao-financial-test", "v1", _request(run_type="scheduled"))
        with pytest.raises(ValueError, match="manual run request"):
            service.submit_one_shot("fuyao-financial-test", "v1", _request(run_type="backfill"))

        queued = service.submit_one_shot("fuyao-financial-test", "v1", _request())
        assert queued.claim_policy == "one_shot"
        assert queued.status == "queued"
        assert ledger.get(queued.run_key) == queued
    finally:
        engine.dispose()


def test_unreviewed_one_shot_only_job_is_rejected_before_ledger_write(tmp_path: Path) -> None:
    """An unreviewed dedicated job cannot create a durable run row."""
    job = _job(reviewed=False)
    service, ledger, engine = _sqlite_service(tmp_path / "one-shot-only-unreviewed.sqlite", job)
    try:
        request = _request()
        identity = build_run_identity(job.task, request)
        with pytest.raises(ValueError, match="has not been reviewed"):
            service.submit_one_shot("fuyao-financial-test", "v1", request)

        assert ledger.get(identity.idempotency_key) is None
    finally:
        engine.dispose()


def test_one_shot_only_executes_only_by_explicit_key_in_sqlite(tmp_path: Path) -> None:
    """Broad claim skips the dedicated row; exact manual claim publishes it once."""
    fetch_calls: list[str] = []
    service, ledger, engine = _sqlite_service(
        tmp_path / "one-shot-only-execution.sqlite", _job(fetch_calls=fetch_calls)
    )
    try:
        queued = service.submit_one_shot("fuyao-financial-test", "v1", _request())

        assert service.run_once("ordinary-worker", TTL) is None
        assert fetch_calls == []

        outcome = service.run_one_shot(queued.run_key, "one-shot-worker", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "succeeded"
        assert outcome.publication == PublicationReceipt(
            "publication-1", "cn.stock.financial.test", 1
        )
        assert fetch_calls == ["manual"]
        assert service.run_one_shot(queued.run_key, "one-shot-worker", TTL) is None
        assert fetch_calls == ["manual"]
        assert ledger.get(queued.run_key) == outcome.record
    finally:
        engine.dispose()


def test_recoverable_row_for_one_shot_only_job_fails_before_fetch(tmp_path: Path) -> None:
    """A manually injected ordinary claim policy cannot invoke a dedicated job."""
    fetch_calls: list[str] = []
    job = _job(fetch_calls=fetch_calls)
    service, ledger, engine = _sqlite_service(
        tmp_path / "one-shot-only-injected-recoverable.sqlite", job
    )
    try:
        request = _request()
        identity = build_run_identity(job.task, request)
        injected = ledger.enqueue(identity.idempotency_key, identity.canonical_json)
        assert injected.claim_policy == "recover_expired"

        outcome = service.run_once("ordinary-worker", TTL)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        assert outcome.record.error_text == "execution_failed:_JobNotRunnableError"
        assert fetch_calls == []
    finally:
        engine.dispose()


def test_one_shot_claim_with_nonmanual_snapshot_fails_before_fetch(tmp_path: Path) -> None:
    """The execution boundary rechecks manual shape even for a one-shot ledger row."""
    fetch_calls: list[str] = []
    job = _job(fetch_calls=fetch_calls)
    service, ledger, engine = _sqlite_service(
        tmp_path / "one-shot-only-injected-backfill.sqlite", job
    )
    try:
        request = _request(run_type="backfill", key="forged-backfill")
        identity = build_run_identity(job.task, request)
        injected = ledger.enqueue(
            identity.idempotency_key,
            identity.canonical_json,
            claim_policy="one_shot",
        )
        assert injected.claim_policy == "one_shot"

        outcome = service.run_one_shot(injected.run_key, "one-shot-worker", TTL)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        assert outcome.record.error_text == "execution_failed:_JobNotRunnableError"
        assert fetch_calls == []
    finally:
        engine.dispose()


def test_existing_enabled_reviewed_job_keeps_one_shot_api_compatibility(
    tmp_path: Path,
) -> None:
    """Existing reviewed enabled jobs retain the previous explicit one-shot path."""
    fetch_calls: list[str] = []
    schedule = ScheduleMetadata(kind="cron", expression="0 16 * * 1-5")
    ordinary_job = _job(
        enabled=True,
        schedule=schedule,
        one_shot_only=False,
        fetch_calls=fetch_calls,
    )
    service, _ledger, engine = _sqlite_service(
        tmp_path / "ordinary-job-one-shot-compat.sqlite", ordinary_job
    )
    try:
        queued = service.submit_one_shot(
            "fuyao-financial-test", "v1", _request(key="legacy-one-shot")
        )
        assert queued.claim_policy == "one_shot"
        outcome = service.run_one_shot(queued.run_key, "one-shot-worker", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "succeeded"
        assert fetch_calls == ["manual"]
    finally:
        engine.dispose()
