"""Boundary tests for fail-closed Sina FZB fanout coordination."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from apscheduler.events import EVENT_JOB_SUBMITTED
from sqlalchemy import create_engine, func, select

from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.sina_fzb_fanout import (
    MAX_FANOUT_EVENT_SLOTS,
    SINA_FZB_FANOUT_JOB_ID,
    SinaFzbFanoutCoordinator,
    SinaFzbFanoutScheduleError,
    SinaFzbInstrumentManifest,
)
from opendata.pipeline.sina_fzb_fanout_journal import (
    SINA_FZB_FANOUT_GROUP_TABLE,
    SINA_FZB_FANOUT_METADATA,
    FanoutGroupRecord,
    SinaFzbFanoutJournal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
OCCURRENCE = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")


@dataclass
class FanoutHarness:
    """Own one isolated SQLite engine and deterministic coordinator inputs."""

    engine: Engine
    journal: SinaFzbFanoutJournal
    dispatcher: RunDispatcher
    task: TaskDefinition
    now: list[datetime]
    source_calls: list[str]


def _manifest(symbols: tuple[str, ...] = SYMBOLS) -> SinaFzbInstrumentManifest:
    """Build a bounded, caller-supplied synthetic selection."""
    return SinaFzbInstrumentManifest.from_symbols(
        manifest_id="manifest:boundary-test",
        selection_snapshot_ref="universe:synthetic-2026-10-26",
        symbols=symbols,
    )


def _task(**changes: object) -> TaskDefinition:
    """Create a reviewed test task, allowing deliberate invalid coordinator inputs."""
    task = TaskDefinition.model_validate(
        {
            "job_id": SINA_FZB_FANOUT_JOB_ID,
            "version": "process-v1",
            "dataset_id": "sina-fzb-observations",
            "provider": "sina",
            "model": "financial-statement",
            "timezone": "UTC",
            "schedule": ScheduleMetadata(kind="cron", expression="30 8 * * *"),
            "enabled": True,
        }
    )
    # Deliberately bypass Pydantic validation for negative coordinator tests.
    return task.model_copy(update=changes)


@pytest.fixture
def fanout_harness(tmp_path: Path) -> Iterator[FanoutHarness]:
    """Create both ledgers only inside a fresh pytest-owned SQLite file."""
    engine = create_engine(f"sqlite:///{tmp_path / 'fanout-boundaries.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    SINA_FZB_FANOUT_METADATA.create_all(engine)
    now = [OCCURRENCE]
    task = _task()
    source_calls: list[str] = []

    def fetch(request: RunRequest, context: RunExecutionContext) -> object:
        source_calls.append("fetch")
        raise AssertionError("fanout submission must not call a provider")

    def normalize(
        raw: object,
        request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        source_calls.append("normalize")
        raise AssertionError("fanout submission must not normalize provider data")

    def publish(
        connection: Connection,
        job: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        idempotency_key: str,
    ) -> PublicationReceipt:
        source_calls.append("publish")
        raise AssertionError("fanout submission must not publish warehouse data")

    reviewed_job = ReviewedJob(
        task=task,
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=True,
    )
    dispatcher = RunDispatcher(
        RunLedger(engine, clock_policy="caller"),
        ReviewedTaskCatalog((reviewed_job,)),
        clock=lambda: now[0],
    )
    journal = SinaFzbFanoutJournal(engine, clock_policy="caller")
    yield FanoutHarness(engine, journal, dispatcher, task, now, source_calls)
    engine.dispose()


def _coordinator(
    harness: FanoutHarness,
    *,
    task: TaskDefinition | None = None,
    manifest: SinaFzbInstrumentManifest | None = None,
    **overrides: object,
) -> SinaFzbFanoutCoordinator:
    """Build a deterministic coordinator with small test-only pump bounds."""
    values: dict[str, object] = {
        "worker_id": "boundary-test-worker",
        "lease_ttl": timedelta(seconds=60),
        "retry_backoff_base": timedelta(seconds=2),
        "retry_backoff_cap": timedelta(seconds=30),
        "pump_interval_seconds": 30,
        "max_chunks_per_pump": 2,
        "chunk_size": 2,
        "clock": lambda: harness.now[0],
    }
    values.update(overrides)
    dispatcher = values.pop("dispatcher", harness.dispatcher)
    journal = values.pop("journal", harness.journal)
    return SinaFzbFanoutCoordinator(
        dispatcher,
        journal,
        task or harness.task,
        manifest or _manifest(),
        **values,
    )


def _row_count(engine: Engine, table: object) -> int:
    """Count rows in a test-owned table without depending on implementation state."""
    with engine.connect() as connection:
        result = connection.execute(select(func.count()).select_from(table))
    return int(result.scalar_one())


def test_manifest_rejects_empty_mutable_and_noncanonical_inputs() -> None:
    """A manifest must be immutable, nonempty, and source-compatible."""
    with pytest.raises(ValueError, match="nonempty"):
        _manifest(())
    with pytest.raises(ValueError, match="iterable"):
        SinaFzbInstrumentManifest.from_symbols(
            manifest_id="manifest:bad",
            selection_snapshot_ref="universe:test",
            symbols="600000.SH",
        )
    with pytest.raises(ValueError, match="immutable tuple"):
        SinaFzbInstrumentManifest(
            manifest_id="manifest:bad",
            selection_snapshot_ref="universe:test",
            symbols=["600000.SH"],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="manifest_id"):
        SinaFzbInstrumentManifest.from_symbols(
            manifest_id="contains whitespace",
            selection_snapshot_ref="universe:test",
            symbols=("600000.SH",),
        )


@pytest.mark.parametrize(
    ("task", "message"),
    [
        (_task(job_id="other-job"), "reviewed Sina FZB job"),
        (_task(schedule=None), "explicit cron"),
        (_task(schedule=ScheduleMetadata(kind="interval", expression="30")), "cron"),
        (_task(enabled=False), "enabled"),
        (_task(schedule=ScheduleMetadata(kind="cron", expression="30 8 * * 1")), "weekday"),
        (_task(schedule=ScheduleMetadata(kind="cron", expression="30 8 * * * *")), "five fields"),
    ],
)
def test_coordinator_rejects_unsafe_task_definitions(
    fanout_harness: FanoutHarness,
    task: TaskDefinition,
    message: str,
) -> None:
    """Only the exact enabled cron contract may create scheduled groups."""
    with pytest.raises(ValueError, match=message):
        _coordinator(fanout_harness, task=task)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"worker_id": "worker with spaces"}, "worker_id"),
        ({"lease_ttl": timedelta(milliseconds=500)}, "lease_ttl"),
        ({"retry_backoff_base": timedelta(seconds=31)}, "must not exceed"),
        ({"pump_interval_seconds": 0}, "pump_interval_seconds"),
        ({"pump_interval_seconds": True}, "pump_interval_seconds"),
        ({"max_chunks_per_pump": 0}, "max_chunks_per_pump"),
        ({"max_chunks_per_pump": True}, "max_chunks_per_pump"),
        ({"chunk_size": 101}, "chunk_size"),
        ({"chunk_size": True}, "chunk_size"),
    ],
)
def test_coordinator_rejects_unbounded_operational_settings(
    fanout_harness: FanoutHarness,
    overrides: dict[str, object],
    message: str,
) -> None:
    """Lease, retry, scheduler, and chunk settings fail before any database write."""
    with pytest.raises(ValueError, match=message):
        _coordinator(fanout_harness, **overrides)
    assert _row_count(fanout_harness.engine, SINA_FZB_FANOUT_GROUP_TABLE) == 0


def test_constructor_rejects_untyped_components_and_incomplete_journal(
    fanout_harness: FanoutHarness,
) -> None:
    """The coordinator cannot silently construct substitute persistence or dispatchers."""
    with pytest.raises(TypeError, match="RunDispatcher"):
        SinaFzbFanoutCoordinator(
            object(),
            fanout_harness.journal,
            fanout_harness.task,
            _manifest(),
            worker_id="test-worker",
            lease_ttl=timedelta(seconds=60),
            retry_backoff_base=timedelta(seconds=1),
            retry_backoff_cap=timedelta(seconds=2),
            pump_interval_seconds=10,
        )
    with pytest.raises(TypeError, match="journal"):
        _coordinator(fanout_harness, journal=object())


class _FailingScheduler:
    """Minimal APScheduler surface that fails after its first registered job."""

    def __init__(self) -> None:
        self.listeners: list[object] = []
        self.jobs: dict[str, object] = {}
        self.add_count = 0
        self.fail_on_add: int | None = 2

    def add_listener(self, listener: object, mask: int) -> None:
        self.listeners.append(listener)

    def remove_listener(self, listener: object) -> None:
        self.listeners.remove(listener)

    def add_job(self, function: object, **options: object) -> None:
        self.add_count += 1
        if self.add_count == self.fail_on_add:
            raise RuntimeError("private scheduler detail")
        self.jobs[str(options["id"])] = function

    def remove_job(self, job_id: str) -> None:
        self.jobs.pop(job_id, None)


def test_scheduler_registration_rolls_back_and_can_be_retried(
    fanout_harness: FanoutHarness,
) -> None:
    """A partial APScheduler installation is removed without leaking its error."""
    coordinator = _coordinator(fanout_harness)
    scheduler = _FailingScheduler()
    with pytest.raises(ValueError, match="APScheduler fanout registration failed") as error:
        coordinator.register(scheduler)
    assert "private scheduler detail" not in str(error.value)
    assert scheduler.listeners == []
    assert scheduler.jobs == {}

    scheduler.fail_on_add = None
    cron_job_id, pump_job_id = coordinator.register(scheduler)
    assert cron_job_id in scheduler.jobs
    assert pump_job_id in scheduler.jobs
    assert scheduler.listeners == [coordinator]


@pytest.mark.parametrize("slots", [None, []])
def test_scheduler_rejects_empty_event_slots_without_persisting(
    fanout_harness: FanoutHarness,
    slots: list[datetime] | None,
) -> None:
    """A matching event without submitted times is recorded as a whole-event failure."""
    coordinator = _coordinator(fanout_harness)
    event = SimpleNamespace(
        code=EVENT_JOB_SUBMITTED,
        job_id=coordinator.scheduler_job_id,
        scheduled_run_times=slots,
    )
    with pytest.raises(SinaFzbFanoutScheduleError) as error:
        coordinator(event)
    assert error.value.receipt.global_error == "empty_slots"
    assert coordinator.last_schedule_receipt == error.value.receipt
    assert _row_count(fanout_harness.engine, SINA_FZB_FANOUT_GROUP_TABLE) == 0


def test_scheduler_ignores_unrelated_events_and_bounds_slot_lists(
    fanout_harness: FanoutHarness,
) -> None:
    """Only bounded submission events for the dedicated no-op job are consumed."""
    coordinator = _coordinator(fanout_harness)
    ignored_code_event = SimpleNamespace(
        code=EVENT_JOB_SUBMITTED + 1,
        job_id=coordinator.scheduler_job_id,
    )
    assert coordinator(ignored_code_event) == ()
    assert coordinator(SimpleNamespace(code=EVENT_JOB_SUBMITTED, job_id="legacy-fetch-job")) == ()

    over_limit = [OCCURRENCE] * (MAX_FANOUT_EVENT_SLOTS + 1)
    event = SimpleNamespace(
        code=EVENT_JOB_SUBMITTED,
        job_id=coordinator.scheduler_job_id,
        scheduled_run_times=over_limit,
    )
    with pytest.raises(SinaFzbFanoutScheduleError) as error:
        coordinator(event)
    assert error.value.receipt.global_error == "slot_limit_exceeded"
    assert _row_count(fanout_harness.engine, SINA_FZB_FANOUT_GROUP_TABLE) == 0


def test_scheduled_group_preflight_failure_writes_no_durable_group(
    fanout_harness: FanoutHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatcher dry-run must pass before the immutable slot is persisted."""
    coordinator = _coordinator(fanout_harness)
    called: list[bool] = []

    def reject_preflight(*args: object, **kwargs: object) -> None:
        called.append(bool(kwargs.get("dry_run")))
        raise ValueError("private request payload")

    monkeypatch.setattr(fanout_harness.dispatcher, "dispatch_scheduled", reject_preflight)
    with pytest.raises(ValueError, match="private request payload"):
        coordinator.create_scheduled_group(OCCURRENCE)
    assert called == [True]
    assert _row_count(fanout_harness.engine, SINA_FZB_FANOUT_GROUP_TABLE) == 0


def test_run_pending_rejects_per_call_limit_above_configured_bound(
    fanout_harness: FanoutHarness,
) -> None:
    """A caller cannot raise the configured pump work bound at runtime."""
    coordinator = _coordinator(fanout_harness)
    for limit in (0, 3, True, 1.5):
        with pytest.raises(ValueError, match="max_chunks"):
            coordinator.run_pending(max_chunks=limit)  # type: ignore[arg-type]
    assert coordinator.last_pump_receipt is None


def test_claim_failure_is_sanitized_and_does_not_dispatch(
    fanout_harness: FanoutHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Journal failures stop the pump and retain only their exception class."""
    coordinator = _coordinator(fanout_harness)

    def fail_claim(**kwargs: object) -> None:
        raise RuntimeError("credential=must-not-escape")

    monkeypatch.setattr(fanout_harness.journal, "claim_next_group", fail_claim)
    receipt = coordinator.run_pending()
    assert receipt.global_error == "RUNTIME_ERROR"
    assert "must-not-escape" not in repr(receipt)
    assert receipt.claimed_chunks == 0
    assert fanout_harness.source_calls == []


def test_corrupt_claim_reply_blocks_group_before_child_dispatch(
    fanout_harness: FanoutHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An altered persisted-manifest reply is blocked before any child is accepted."""
    coordinator = _coordinator(fanout_harness)
    group = coordinator.create_scheduled_group(OCCURRENCE)
    claim = fanout_harness.journal.claim_next_group(
        worker="boundary-test-worker",
        now=OCCURRENCE,
        ttl=timedelta(seconds=60),
    )
    assert claim is not None
    corrupted = replace(claim, manifest_json="{}")

    def return_corrupt_claim(**kwargs: object) -> FanoutGroupRecord:
        return corrupted

    monkeypatch.setattr(fanout_harness.journal, "claim_next_group", return_corrupt_claim)
    receipt = coordinator.run_pending()
    persisted = fanout_harness.journal.get(group.fanout_key)
    assert persisted is not None and persisted.status == "BLOCKED"
    assert receipt.blocked_groups == 1
    assert receipt.accepted_runs == 0
    assert _row_count(fanout_harness.engine, RUN_LEDGER_TABLE) == 0
    assert fanout_harness.source_calls == []


def test_lease_loss_between_members_keeps_cursor_and_stops_chunk(
    fanout_harness: FanoutHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost heartbeat fence stops the chunk before another symbol is submitted."""
    coordinator = _coordinator(fanout_harness, chunk_size=3)
    group = coordinator.create_scheduled_group(OCCURRENCE)
    dispatch = fanout_harness.dispatcher.dispatch_scheduled
    submitted: list[str] = []

    def count_dispatch(*args: object, **kwargs: object) -> object:
        if not kwargs.get("dry_run", False):
            submitted.append("child")
        return dispatch(*args, **kwargs)

    monkeypatch.setattr(fanout_harness.dispatcher, "dispatch_scheduled", count_dispatch)
    monkeypatch.setattr(fanout_harness.journal, "heartbeat", lambda **kwargs: None)

    receipt = coordinator.run_pending()
    persisted = fanout_harness.journal.get(group.fanout_key)
    assert receipt.lease_lost_groups == 1
    assert receipt.accepted_runs == 1
    assert submitted == ["child"]
    assert persisted is not None
    assert persisted.next_index == 0
    assert persisted.lease_owner == "boundary-test-worker"
    assert _row_count(fanout_harness.engine, RUN_LEDGER_TABLE) == 1
    assert fanout_harness.source_calls == []


def test_permanent_dispatch_rejection_blocks_and_hides_raw_message(
    fanout_harness: FanoutHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deterministic dispatch rejection cannot advance or expose request data."""
    coordinator = _coordinator(fanout_harness)
    group = coordinator.create_scheduled_group(OCCURRENCE)

    def reject_child(*args: object, **kwargs: object) -> None:
        if not kwargs.get("dry_run", False):
            raise ValueError("access_token=private")

    monkeypatch.setattr(fanout_harness.dispatcher, "dispatch_scheduled", reject_child)
    receipt = coordinator.run_pending()
    persisted = fanout_harness.journal.get(group.fanout_key)
    assert receipt.blocked_groups == 1
    assert receipt.outcomes[0].error_code == "VALUE_ERROR"
    assert "private" not in repr(receipt)
    assert persisted is not None and persisted.status == "BLOCKED"
    assert persisted.next_index == 0
    assert _row_count(fanout_harness.engine, RUN_LEDGER_TABLE) == 0
