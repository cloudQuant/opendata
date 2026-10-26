"""Behavioral tests for the reviewed run submission dispatcher."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Event
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from apscheduler.events import (
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
    JobExecutionEvent,
    JobSubmissionEvent,
)
from apscheduler.executors.pool import ThreadPoolExecutor as APSchedulerThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, func, select

from opendata.pipeline.run_dispatch import (
    APSchedulerDispatchError,
    APSchedulerSubmissionCallback,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
    RunLedgerConflictError,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)
SCHEDULED = datetime(2026, 10, 26, 9, 30, tzinfo=UTC)


@pytest.fixture
def dispatch_setup(tmp_path) -> tuple[Engine, RunLedger, RunDispatcher]:
    """Use a real temporary SQLite run ledger with explicit reviewed handlers."""
    engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    jobs = (make_job(), make_job(version="v2"))
    catalog = ReviewedTaskCatalog(jobs)
    ledger = RunLedger(engine)
    dispatcher = RunDispatcher(ledger, catalog, clock=lambda: NOW)
    yield engine, ledger, dispatcher
    engine.dispose()


def make_task(
    *,
    version: str = "v1",
    timezone_name: str = "Asia/Shanghai",
    expression: str = "30 17 * * *",
    kind: str = "cron",
    enabled: bool = True,
) -> TaskDefinition:
    """Build a task with an explicit reviewed daily schedule."""
    return TaskDefinition(
        job_id="dispatch-daily",
        version=version,
        dataset_id="market.daily",
        provider="reviewed-provider",
        model="daily-bars",
        timezone=timezone_name,
        schedule=ScheduleMetadata(kind=kind, expression=expression),
        enabled=enabled,
    )


def _fetch(request: RunRequest, context: RunExecutionContext) -> object:
    """Fail if dispatch unexpectedly runs the source adapter inline."""
    raise AssertionError("dispatch must not fetch source data")


def _normalize(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Fail if dispatch unexpectedly invokes a normalizer."""
    raise AssertionError("dispatch must not normalize source data")


def _publish(
    connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    """Fail if dispatch unexpectedly publishes records inline."""
    raise AssertionError("dispatch must not publish data")


def make_job(
    *,
    version: str = "v1",
    timezone_name: str = "Asia/Shanghai",
    expression: str = "30 17 * * *",
    kind: str = "cron",
    enabled: bool = True,
    reviewed: bool = True,
) -> ReviewedJob:
    """Build an injected reviewed job whose handlers must not run at submit."""
    return ReviewedJob(
        task=make_task(
            version=version,
            timezone_name=timezone_name,
            expression=expression,
            kind=kind,
            enabled=enabled,
        ),
        fetch=_fetch,
        normalize=_normalize,
        publish=_publish,
        reviewed=reviewed,
    )


def ledger_rows(engine: Engine) -> list[dict[str, object]]:
    """Read queued snapshots from the real ledger for persistence assertions."""
    with engine.connect() as connection:
        rows = connection.execute(select(RUN_LEDGER_TABLE)).mappings().all()
    return [dict(row) for row in rows]


def row_count(engine: Engine) -> int:
    """Return the durable run count without inspecting business payloads."""
    with engine.connect() as connection:
        result = connection.execute(select(func.count()).select_from(RUN_LEDGER_TABLE))
        return int(result.scalar_one())


def test_scheduled_manual_and_backfill_share_submission_and_are_idempotent(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Queue all modes without running handlers and preserve each identity mode."""
    engine, _, dispatcher = dispatch_setup
    scheduled = dispatcher.dispatch_scheduled(
        "dispatch-daily",
        "v1",
        occurrence=SCHEDULED,
        payload={"query": {"symbol": "XYZ"}, "partition": {"trade_date": "2026-10-26"}},
    )
    scheduled_retry = dispatcher.dispatch_scheduled(
        "dispatch-daily",
        "v1",
        occurrence=SCHEDULED,
        payload={"query": {"symbol": "XYZ"}, "partition": {"trade_date": "2026-10-26"}},
    )
    manual = dispatcher.dispatch_manual(
        "dispatch-daily",
        "v1",
        {"idempotency_key": "manual-oct-26", "query": {"symbol": "XYZ"}},
    )
    backfill = dispatcher.dispatch_backfill(
        "dispatch-daily",
        "v1",
        {
            "idempotency_key": "backfill-oct-26",
            "backfill_start": date(2026, 10, 1),
            "backfill_end": date(2026, 10, 26),
            "query": {"symbol": "XYZ"},
        },
    )

    assert scheduled.identity == scheduled_retry.identity
    assert (
        len(
            {
                scheduled.identity.run_key,
                manual.identity.run_key,
                backfill.identity.run_key,
            }
        )
        == 3
    )
    assert {scheduled.status, manual.status, backfill.status} == {"queued"}
    assert row_count(engine) == 3
    assert all("XYZ" not in receipt.identity.run_key for receipt in (scheduled, manual, backfill))


def test_same_scheduled_slot_with_parameter_drift_conflicts_without_second_row(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject payload changes under an existing occurrence slot."""
    engine, _, dispatcher = dispatch_setup
    dispatcher.dispatch_scheduled(
        "dispatch-daily", "v1", occurrence=SCHEDULED, payload={"query": {"symbol": "AAA"}}
    )
    with pytest.raises(RunLedgerConflictError):
        dispatcher.dispatch_scheduled(
            "dispatch-daily",
            "v1",
            occurrence=SCHEDULED,
            payload={"query": {"symbol": "BBB"}},
        )
    assert row_count(engine) == 1


def test_job_version_change_creates_a_distinct_scheduled_identity(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Keep a new reviewed job version separate from the previous revision."""
    _, _, dispatcher = dispatch_setup
    first = dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=SCHEDULED)
    second = dispatcher.dispatch_scheduled("dispatch-daily", "v2", occurrence=SCHEDULED)
    assert first.identity.run_key != second.identity.run_key


def test_dry_run_returns_only_safe_identity_fields_and_writes_nothing(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Preview and submit share validation while the preview omits raw parameters."""
    engine, _, dispatcher = dispatch_setup
    payload = {"query": {"private_label": "do-not-echo"}, "partition": {"k": "v"}}
    preview = dispatcher.dispatch_scheduled(
        "dispatch-daily", "v1", occurrence=SCHEDULED, payload=payload, dry_run=True
    )
    submitted = dispatcher.dispatch_scheduled(
        "dispatch-daily", "v1", occurrence=SCHEDULED, payload=payload
    )
    assert preview.identity.run_key == submitted.identity.run_key
    assert preview.status == "dry_run" and preview.created_at is None
    assert "do-not-echo" not in repr(preview)
    assert not hasattr(preview.identity, "canonical_json")
    assert row_count(engine) == 1


@pytest.mark.parametrize(
    ("occurrence", "expression", "timezone_name"),
    [
        (datetime(2026, 10, 26, 17, 30), "30 17 * * *", "Asia/Shanghai"),
        (
            datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            "30 17 * * *",
            "Asia/Shanghai",
        ),
        (
            datetime(2026, 10, 26, 9, 30, tzinfo=ZoneInfo("Europe/London")),
            "30 17 * * *",
            "Asia/Shanghai",
        ),
        (datetime(2026, 10, 26, 9, 31, tzinfo=UTC), "30 17 * * *", "Asia/Shanghai"),
        (datetime(2025, 11, 2, 5, 30, tzinfo=UTC), "30 1 * * *", "America/New_York"),
        (datetime(2025, 3, 9, 7, 30, tzinfo=UTC), "30 2 * * *", "America/New_York"),
    ],
)
def test_invalid_timezone_dst_and_out_of_slot_occurrences_are_not_queued(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
    occurrence: datetime,
    expression: str,
    timezone_name: str,
) -> None:
    """Reject missing UTC context, non-UTC input, DST ambiguity, and wrong slots."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name=timezone_name, expression=expression),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)
    with pytest.raises(ValueError):
        dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=occurrence)
    assert row_count(engine) == 0


def test_bad_payload_disabled_job_and_non_cron_schedule_fail_before_enqueue(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Fail closed on payload shape and unsupported or disabled task metadata."""
    engine, _, dispatcher = dispatch_setup
    with pytest.raises(ValueError, match="unsupported fields"):
        dispatcher.dispatch_scheduled(
            "dispatch-daily", "v1", occurrence=SCHEDULED, payload={"run_type": "manual"}
        )
    with pytest.raises(ValueError, match="invalid"):
        dispatcher.dispatch_manual(
            "dispatch-daily", "v1", {"idempotency_key": "manual-bad", "query": []}
        )

    interval_catalog = ReviewedTaskCatalog((make_job(kind="interval", expression="1 hour"),))
    interval_dispatcher = RunDispatcher(RunLedger(engine), interval_catalog, clock=lambda: NOW)
    with pytest.raises(ValueError, match="cron schedule"):
        interval_dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=SCHEDULED)

    disabled_catalog = ReviewedTaskCatalog((make_job(enabled=False),))
    disabled_dispatcher = RunDispatcher(RunLedger(engine), disabled_catalog, clock=lambda: NOW)
    with pytest.raises(ValueError, match="disabled"):
        disabled_dispatcher.dispatch_manual(
            "dispatch-daily", "v1", {"idempotency_key": "manual-disabled"}
        )
    assert row_count(engine) == 0


def test_invalid_cron_expression_fails_before_enqueue(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject cron text that APScheduler cannot parse."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(expression="not-a-five-field-cron"),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)
    with pytest.raises(ValueError, match="five fields"):
        dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=SCHEDULED)
    assert row_count(engine) == 0


@pytest.mark.parametrize(
    "occurrence",
    [
        datetime(2026, 10, 26, 16, tzinfo=UTC),
        datetime(2026, 10, 30, 16, tzinfo=UTC),
    ],
)
def test_named_workday_cron_accepts_monday_and_friday(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
    occurrence: datetime,
) -> None:
    """Accept explicit Monday and Friday slots using the named weekday dialect."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name="UTC", expression="0 16 * * mon-fri"),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)

    receipt = dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=occurrence)

    assert receipt.status == "queued"
    assert row_count(engine) == 1


def test_named_workday_cron_rejects_saturday_without_enqueue(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject a weekend slot under an explicitly named Monday-to-Friday schedule."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name="UTC", expression="0 16 * * mon-fri"),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)

    with pytest.raises(ValueError, match="outside the configured cron slot"):
        dispatcher.dispatch_scheduled(
            "dispatch-daily", "v1", occurrence=datetime(2026, 10, 31, 16, tzinfo=UTC)
        )
    assert row_count(engine) == 0


@pytest.mark.parametrize(
    ("expression", "occurrence"),
    [
        ("0 16 * * mon,wed,fri", datetime(2026, 10, 28, 16, tzinfo=UTC)),
        ("0 16 * * mon-fri/2", datetime(2026, 10, 30, 16, tzinfo=UTC)),
    ],
)
def test_named_weekday_lists_and_steps_are_accepted(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
    expression: str,
    occurrence: datetime,
) -> None:
    """Allow APScheduler-validated lists and steps when weekday values are named."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name="UTC", expression=expression),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)

    assert dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=occurrence).status == (
        "queued"
    )
    assert row_count(engine) == 1


def test_numeric_weekday_cron_is_rejected_by_dispatch_and_registration(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Require manual dialect mapping rather than silently shifting numeric weekdays."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name="UTC", expression="0 16 * * 1-5"),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)

    with pytest.raises(ValueError, match="manually map the original schedule dialect"):
        dispatcher.dispatch_scheduled(
            "dispatch-daily", "v1", occurrence=datetime(2026, 10, 26, 16, tzinfo=UTC)
        )
    with pytest.raises(ValueError, match="manually map the original schedule dialect"):
        dispatcher.apscheduler_callback("dispatch-daily", "v1").register(
            BackgroundScheduler(timezone="UTC")
        )
    assert row_count(engine) == 0


def test_unreviewed_jobs_cannot_enter_the_static_catalog() -> None:
    """Require explicit review before a job can be made dispatchable."""
    with pytest.raises(ValueError, match="only reviewed"):
        ReviewedTaskCatalog((make_job(reviewed=False),))


def test_catalog_and_engine_factory_expose_only_reviewed_static_versions(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Resolve immutable task metadata and construct a dispatcher without DDL."""
    engine, _, _ = dispatch_setup
    job = make_job()
    catalog = ReviewedTaskCatalog((job,))
    assert catalog.registry.resolve("dispatch-daily", "v1") is job
    assert catalog.resolve("dispatch-daily", "v1") == job.task
    assert catalog.resolve("missing", "v1") is None
    assert catalog.tasks() == (job.task,)

    dispatcher = RunDispatcher.from_engine(
        engine,
        catalog,
        clock=lambda: NOW,
        clock_policy="database",
    )
    receipt = dispatcher.dispatch_manual(
        "dispatch-daily", "v1", {"idempotency_key": "engine-factory"}
    )
    assert receipt.status == "queued"
    assert row_count(engine) == 1


def test_invalid_catalog_types_unknown_jobs_and_schedule_metadata_fail_closed(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject malformed DI/catalog pairs and absent scheduled metadata."""
    engine, ledger, dispatcher = dispatch_setup
    with pytest.raises(TypeError, match="ReviewedTaskCatalog"):
        RunDispatcher(ledger, object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ReviewedJob instances"):
        ReviewedTaskCatalog((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reviewed catalog"):
        dispatcher.dispatch_manual("missing-job", "v1", {"idempotency_key": "unknown"})

    base_job = make_job()
    malformed_task = base_job.task.model_copy(update={"schedule": None})
    malformed_catalog = ReviewedTaskCatalog((replace(base_job, task=malformed_task),))
    malformed_dispatcher = RunDispatcher(RunLedger(engine), malformed_catalog, clock=lambda: NOW)
    with pytest.raises(ValueError, match="schedule metadata"):
        malformed_dispatcher.apscheduler_callback("dispatch-daily", "v1")
    with pytest.raises(ValueError, match="schedule metadata"):
        malformed_dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=SCHEDULED)
    assert row_count(engine) == 0


def test_dry_run_uses_the_same_validation_and_does_not_create_ledger_rows(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject bad dry-run payloads just as the persistent submit path does."""
    engine, _, dispatcher = dispatch_setup
    with pytest.raises(ValueError, match="unsupported fields"):
        dispatcher.dispatch_manual(
            "dispatch-daily",
            "v1",
            {"idempotency_key": "manual-dry", "extra": "secret"},
            dry_run=True,
        )
    assert row_count(engine) == 0


def test_dry_run_requires_a_boolean_flag(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject truthy strings and integers instead of coercing preview policy."""
    engine, _, dispatcher = dispatch_setup
    with pytest.raises(ValueError, match="dry_run must be a boolean"):
        dispatcher.dispatch_manual(
            "dispatch-daily",
            "v1",
            {"idempotency_key": "manual-flag"},
            dry_run=1,  # type: ignore[arg-type]
        )
    assert row_count(engine) == 0


def test_real_apscheduler_submission_event_queues_each_explicit_slot(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Bridge actual APScheduler event objects without accepting business callbacks."""
    engine, _, dispatcher = dispatch_setup
    binding = dispatcher.apscheduler_callback(
        "dispatch-daily", "v1", payload={"partition": {"source": "daily"}}
    )
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler_job_id = binding.register(scheduler)
    job = scheduler.get_job(scheduler_job_id)
    assert job is not None
    assert job.func is APSchedulerSubmissionCallback._noop_tick
    assert job.coalesce is False
    assert job.misfire_grace_time == 1
    assert job.max_instances == 1
    with pytest.raises(ValueError, match="already registered"):
        binding.register(scheduler)

    first_local = datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai"))
    second_local = first_local + timedelta(days=1)
    event = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [first_local, second_local],
    )
    receipts = binding(event)
    assert len(receipts) == 2
    assert binding.last_receipt is not None
    assert binding.last_receipt.accepted == receipts
    assert binding.last_receipt.failures == ()
    assert receipts[0].identity.run_key != receipts[1].identity.run_key
    assert row_count(engine) == 2
    snapshots = ledger_rows(engine)
    assert '"occurrence":"2026-10-26T09:30:00.000000Z"' in snapshots[0]["job_snapshot_json"]
    assert '"occurrence":"2026-10-27T09:30:00.000000Z"' in snapshots[1]["job_snapshot_json"]

    retry_event = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [first_local],
    )
    retry_receipt = binding(retry_event)[0]
    assert retry_receipt.identity.run_key == receipts[0].identity.run_key
    assert row_count(engine) == 2

    missed = JobExecutionEvent(
        EVENT_JOB_MISSED,
        scheduler_job_id,
        "default",
        first_local,
    )
    assert binding(missed) == ()
    assert row_count(engine) == 2
    assert binding.last_receipt is not None
    assert binding.last_receipt.accepted[0].identity == receipts[0].identity


def test_submitted_slot_remains_queued_when_real_executor_later_misfires(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Prove a delayed APScheduler executor event cannot erase queue submission."""
    engine, _, _ = dispatch_setup
    catalog = ReviewedTaskCatalog((make_job(timezone_name="UTC", expression="* * * * *"),))
    dispatcher = RunDispatcher(RunLedger(engine), catalog, clock=lambda: NOW)
    binding = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    executor = APSchedulerThreadPoolExecutor(max_workers=1)
    scheduler = BackgroundScheduler(executors={"default": executor}, timezone="UTC")
    job_id = binding.register(scheduler)
    observed_submissions: list[JobSubmissionEvent] = []
    observed_misfires: list[JobExecutionEvent] = []
    got_submission = Event()
    got_misfire = Event()
    blocker_started = Event()
    release_executor = Event()

    def occupy_executor() -> None:
        """Hold the real APScheduler worker until submission is durably handled."""
        blocker_started.set()
        release_executor.wait(timeout=10)

    def record_scheduler_events(event: object) -> None:
        """Record actual scheduler-dispatched event objects for this job."""
        if getattr(event, "job_id", None) != job_id:
            return
        if getattr(event, "code", None) == EVENT_JOB_SUBMITTED:
            assert isinstance(event, JobSubmissionEvent)
            observed_submissions.append(event)
            got_submission.set()
        elif getattr(event, "code", None) == EVENT_JOB_MISSED:
            assert isinstance(event, JobExecutionEvent)
            observed_misfires.append(event)
            got_misfire.set()

    scheduler.add_listener(record_scheduler_events, EVENT_JOB_SUBMITTED | EVENT_JOB_MISSED)
    scheduler.start(paused=True)
    try:
        pool = executor._pool
        assert pool is not None
        pool.submit(occupy_executor)
        assert blocker_started.wait(timeout=10)
        now = datetime.now(UTC)
        late_slot = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
        scheduler.modify_job(job_id, next_run_time=late_slot)
        scheduler.resume()
        assert got_submission.wait(timeout=10)
        assert binding.last_receipt is not None
        assert row_count(engine) == len(binding.last_receipt.accepted)
        release_executor.set()
        assert got_misfire.wait(timeout=10)
    finally:
        release_executor.set()
        scheduler.shutdown(wait=True)

    assert len(observed_submissions) == 1
    submitted_slots = observed_submissions[0].scheduled_run_times
    assert late_slot in submitted_slots
    assert any(event.scheduled_run_time == late_slot for event in observed_misfires)
    assert binding.last_receipt is not None
    assert len(binding.last_receipt.accepted) == len(submitted_slots)
    assert binding.last_receipt.failures == ()
    assert row_count(engine) == len(submitted_slots)
    assert all(receipt.status == "queued" for receipt in binding.last_receipt.accepted)


def test_scheduler_event_mixed_success_and_failure_reports_partial_receipts(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Expose accepted keys and failed slots when one event has bad timezone data."""
    engine, _, dispatcher = dispatch_setup
    binding = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    scheduler = BackgroundScheduler()
    scheduler_job_id = binding.register(scheduler)
    event = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [
            datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 10, 27, 17, 30, tzinfo=ZoneInfo("America/New_York")),
        ],
    )
    with pytest.raises(APSchedulerDispatchError) as captured:
        binding(event)
    assert len(captured.value.accepted) == 1
    assert len(captured.value.failures) == 1
    assert binding.last_receipt is not None
    assert binding.last_receipt.accepted == captured.value.accepted
    assert binding.last_receipt.failures == captured.value.failures
    assert captured.value.failures[0].code == "ValueError"
    assert row_count(engine) == 1
    assert "query" not in str(captured.value)


def test_ap_scheduler_callback_constructor_and_interval_registration_errors(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject invalid callback bindings and unsupported trigger kinds."""
    _, ledger, dispatcher = dispatch_setup
    task = make_task()
    with pytest.raises(TypeError, match="RunDispatcher"):
        APSchedulerSubmissionCallback(object(), task, {})  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="TaskDefinition"):
        APSchedulerSubmissionCallback(dispatcher, object(), {})  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="JSON object"):
        APSchedulerSubmissionCallback(dispatcher, task, [])  # type: ignore[arg-type]

    interval_catalog = ReviewedTaskCatalog((make_job(kind="interval", expression="1 hour"),))
    interval_dispatcher = RunDispatcher(ledger, interval_catalog)
    interval_callback = interval_dispatcher.apscheduler_callback("dispatch-daily", "v1")
    with pytest.raises(ValueError, match="require cron schedules"):
        interval_callback.register(BackgroundScheduler())


def test_apscheduler_registration_validation_and_listener_cleanup(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Validate the scheduler surface and undo listener registration on failure."""
    _, _, dispatcher = dispatch_setup
    callback = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    with pytest.raises(TypeError, match="add_job"):
        callback.register(object())

    class SchedulerStub:
        """Capture listener lifecycle while forcing the job addition to fail."""

        def __init__(self) -> None:
            self.listeners: list[object] = []

        def add_listener(self, listener: object, mask: int) -> None:
            self.listeners.append(listener)

        def remove_listener(self, listener: object) -> None:
            self.listeners.remove(listener)

        def add_job(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("private-query-must-not-escape")

    scheduler = SchedulerStub()
    with pytest.raises(ValueError, match="registration failed") as captured:
        callback.register(scheduler)
    assert scheduler.listeners == []
    assert "private-query" not in str(captured.value)


def test_scheduler_listener_rejects_unrelated_empty_and_oversized_events(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Ignore other APScheduler events and reject invalid run-time batches."""
    engine, _, dispatcher = dispatch_setup
    callback = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    scheduler = BackgroundScheduler()
    scheduler_job_id = callback.register(scheduler)
    assert callback.scheduler_job_id == scheduler_job_id
    assert APSchedulerSubmissionCallback._noop_tick() is None

    unrelated = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        "another-job",
        "default",
        [datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai"))],
    )
    assert callback(unrelated) == ()
    empty = JobSubmissionEvent(EVENT_JOB_SUBMITTED, scheduler_job_id, "default", [])
    with pytest.raises(APSchedulerDispatchError, match="empty_slots"):
        callback(empty)
    assert callback.last_receipt is not None
    assert callback.last_receipt.global_error == "empty_slots"
    oversized = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [datetime(2026, 10, 26, 17, 30, tzinfo=ZoneInfo("Asia/Shanghai"))] * 129,
    )
    with pytest.raises(APSchedulerDispatchError, match="slot_limit_exceeded"):
        callback(oversized)
    assert callback.last_receipt is not None
    assert callback.last_receipt.global_error == "slot_limit_exceeded"

    naive_slot = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        scheduler_job_id,
        "default",
        [datetime(2026, 10, 26, 17, 30)],
    )
    with pytest.raises(APSchedulerDispatchError) as captured:
        callback(naive_slot)
    assert captured.value.failures[0].occurrence is None
    assert callback.last_receipt is not None
    assert callback.last_receipt.failures == captured.value.failures
    assert callback.last_receipt.global_error is None
    assert row_count(engine) == 0


def test_last_receipt_preserves_listener_failure_swallowed_by_apscheduler(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Keep a visible receipt when APScheduler logs and swallows listener errors."""
    engine, _, dispatcher = dispatch_setup
    callback = dispatcher.apscheduler_callback("dispatch-daily", "v1")
    scheduler = BackgroundScheduler()
    job_id = callback.register(scheduler)
    empty = JobSubmissionEvent(EVENT_JOB_SUBMITTED, job_id, "default", [])

    caplog.set_level("ERROR", logger="apscheduler.scheduler")
    scheduler._dispatch_event(empty)

    assert "Error notifying listener" in caplog.text
    assert callback.last_receipt is not None
    assert callback.last_receipt.global_error == "empty_slots"
    assert callback.last_receipt.accepted == ()
    assert row_count(engine) == 0


def test_non_mapping_payload_and_invalid_task_timezone_never_enqueue(
    dispatch_setup: tuple[Engine, RunLedger, RunDispatcher],
) -> None:
    """Reject payloads that are not objects and invalid mutated task metadata."""
    engine, _, dispatcher = dispatch_setup
    with pytest.raises(ValueError, match="JSON object"):
        dispatcher.dispatch_manual(
            "dispatch-daily",
            "v1",
            [],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="explicit datetime"):
        dispatcher.dispatch_scheduled(
            "dispatch-daily",
            "v1",
            occurrence=object(),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="JSON object"):
        dispatcher.apscheduler_callback(
            "dispatch-daily",
            "v1",
            payload=[],  # type: ignore[arg-type]
        )
    base_job = make_job()
    invalid_task = base_job.task.model_copy(update={"timezone": "No/Such_Zone"})
    invalid_catalog = ReviewedTaskCatalog((replace(base_job, task=invalid_task),))
    invalid_dispatcher = RunDispatcher(RunLedger(engine), invalid_catalog, clock=lambda: NOW)
    with pytest.raises(ValueError, match="task timezone"):
        invalid_dispatcher.dispatch_scheduled("dispatch-daily", "v1", occurrence=SCHEDULED)
    assert row_count(engine) == 0
