"""Behavioral tests for scheduled Sina FZB fanout and recovery."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from apscheduler.events import EVENT_JOB_SUBMITTED, JobSubmissionEvent
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Connection, Engine

import opendata.pipeline.sina_fzb_fanout as fanout_module
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.sina_fzb_fanout import (
    SINA_FZB_FANOUT_JOB_ID,
    SinaFzbFanoutCoordinator,
    SinaFzbFanoutScheduleError,
    SinaFzbInstrumentManifest,
)
from opendata.pipeline.sina_fzb_fanout_journal import (
    ENQUEUED,
    SINA_FZB_FANOUT_METADATA,
    FanoutGroupRecord,
    FanoutManifestConflictError,
    SinaFzbFanoutJournal,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

FanoutSetup = tuple[Engine, SinaFzbFanoutJournal, RunDispatcher, TaskDefinition, list[datetime]]

UTC = timezone.utc
OCCURRENCE = datetime(2026, 10, 26, 8, 30, tzinfo=UTC)
SYMBOLS = ("000001.SZ", "000002.SZ", "600000.SH")


def _unexpected_fetch(request: RunRequest, context: RunExecutionContext) -> object:
    """Prove scheduler submission never invokes the source adapter inline."""
    raise AssertionError("fanout must only enqueue reviewed run requests")


def _unexpected_normalize(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Prove submission never invokes normalization inline."""
    raise AssertionError("fanout must not normalize source data")


def _unexpected_publish(
    connection: Connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    """Prove submission never publishes warehouse facts inline."""
    raise AssertionError("fanout must not publish data")


@pytest.fixture
def fanout_setup(tmp_path: Path) -> Iterator[FanoutSetup]:
    """Bind run ledger and fanout journal to one explicitly owned SQLite file."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sina-fzb-fanout.sqlite'}")
    try:
        RUN_LEDGER_METADATA.create_all(engine)
        SINA_FZB_FANOUT_METADATA.create_all(engine)
        clock_value = [OCCURRENCE]
        task = TaskDefinition(
            job_id=SINA_FZB_FANOUT_JOB_ID,
            version="process-v1",
            dataset_id="sina-fzb-observations",
            provider="sina",
            model="financial-statement",
            timezone="UTC",
            schedule=ScheduleMetadata(kind="cron", expression="30 8 * * *"),
            enabled=True,
        )
        job = ReviewedJob(
            task=task,
            fetch=_unexpected_fetch,
            normalize=_unexpected_normalize,
            publish=_unexpected_publish,
            reviewed=True,
        )
        dispatcher = RunDispatcher(
            RunLedger(engine, clock_policy="caller"),
            ReviewedTaskCatalog((job,)),
            clock=lambda: clock_value[0],
        )
        journal = SinaFzbFanoutJournal(engine, clock_policy="caller")
        yield engine, journal, dispatcher, task, clock_value
    finally:
        engine.dispose()


def _manifest(symbols: tuple[str, ...] = SYMBOLS) -> SinaFzbInstrumentManifest:
    """Build a caller-resolved immutable selection fixture."""
    return SinaFzbInstrumentManifest.from_symbols(
        manifest_id="sha256:manifest-001",
        selection_snapshot_ref="universe:2026-10-26",
        symbols=symbols,
    )


def _coordinator(
    fanout_setup: FanoutSetup,
    manifest: SinaFzbInstrumentManifest | None = None,
) -> SinaFzbFanoutCoordinator:
    """Build one bounded coordinator with deterministic UTC lease time."""
    _, journal, dispatcher, task, clock_value = fanout_setup
    return SinaFzbFanoutCoordinator(
        dispatcher,
        journal,
        task,
        manifest or _manifest(),
        worker_id="test-fanout-worker",
        lease_ttl=timedelta(seconds=60),
        retry_backoff_base=timedelta(seconds=2),
        retry_backoff_cap=timedelta(seconds=30),
        pump_interval_seconds=30,
        max_chunks_per_pump=2,
        chunk_size=2,
        clock=lambda: clock_value[0],
    )


def _coordinator_options(fanout_setup: FanoutSetup) -> dict[str, object]:
    """Return the explicit valid options used by constructor guard tests."""
    _, _, _, _, clock_value = fanout_setup
    return {
        "worker_id": "test-fanout-worker",
        "lease_ttl": timedelta(seconds=60),
        "retry_backoff_base": timedelta(seconds=2),
        "retry_backoff_cap": timedelta(seconds=30),
        "pump_interval_seconds": 30,
        "max_chunks_per_pump": 2,
        "chunk_size": 2,
        "clock": lambda: clock_value[0],
    }


def _claimed_group(fanout_setup: FanoutSetup) -> FanoutGroupRecord:
    """Create one durable row and claim it for direct record validation tests."""
    _, journal, _, _, _ = fanout_setup
    coordinator = _coordinator(fanout_setup)
    group = coordinator.create_scheduled_group(OCCURRENCE)
    claimed = journal.claim_next_group(
        worker="test-fanout-worker",
        now=OCCURRENCE,
        ttl=timedelta(seconds=60),
    )
    assert claimed is not None
    assert claimed.fanout_key == group.fanout_key
    return claimed


def _rehashed_record(record: FanoutGroupRecord, payload: str) -> FanoutGroupRecord:
    """Return a deliberately edited snapshot with its matching content digest."""
    return replace(
        record,
        manifest_json=payload,
        manifest_sha256=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def _ledger_count(engine: Engine) -> int:
    """Count durable child requests without depending on source execution."""
    with engine.connect() as connection:
        result = connection.execute(select(func.count()).select_from(RUN_LEDGER_TABLE))
    return int(result.scalar_one())


def test_manifest_sorts_symbols_and_rejects_duplicates_and_noncanonical_codes() -> None:
    """Manifest canonicalization is deterministic and never silently dedupes."""
    manifest = _manifest(("600000.SH", "000001.SZ"))
    assert manifest.symbols == ("000001.SZ", "600000.SH")
    canonical = manifest.canonical_json(
        job_id=SINA_FZB_FANOUT_JOB_ID,
        job_version="process-v1",
        occurrence=OCCURRENCE,
    )
    assert json.loads(canonical)["symbols"] == ["000001.SZ", "600000.SH"]

    with pytest.raises(ValueError, match="unique"):
        _manifest(("000001.SZ", "000001.SZ"))
    with pytest.raises(ValueError, match="SH or SZ suffix"):
        _manifest(("SZ000001",))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("worker_id", "bad worker", "worker_id is invalid"),
        ("lease_ttl", timedelta(0), "lease_ttl must be between"),
        ("retry_backoff_base", timedelta(0), "retry_backoff_base must be between"),
        ("retry_backoff_cap", timedelta(days=2), "retry_backoff_cap must be between"),
        ("pump_interval_seconds", True, "pump_interval_seconds must be between"),
        ("pump_interval_seconds", 0, "pump_interval_seconds must be between"),
        ("max_chunks_per_pump", True, "max_chunks_per_pump must be an integer"),
        ("max_chunks_per_pump", 0, "max_chunks_per_pump must be between"),
        ("max_chunks_per_pump", 33, "max_chunks_per_pump must be between"),
        ("chunk_size", True, "chunk_size must be an integer"),
        ("chunk_size", 0, "chunk_size must be between"),
        ("chunk_size", 101, "chunk_size must be between"),
    ],
)
def test_coordinator_rejects_invalid_operational_bounds(
    fanout_setup: FanoutSetup,
    field: str,
    value: object,
    message: str,
) -> None:
    """Every configured worker bound fails closed before any scheduler side effect."""
    _, journal, dispatcher, task, _ = fanout_setup
    options = _coordinator_options(fanout_setup)
    options[field] = value
    with pytest.raises(ValueError, match=message):
        SinaFzbFanoutCoordinator(dispatcher, journal, task, _manifest(), **options)


def test_coordinator_rejects_invalid_task_and_journal_contracts(
    fanout_setup: FanoutSetup,
) -> None:
    """Only the exact enabled reviewed cron job and full journal API are accepted."""
    _, journal, dispatcher, task, _ = fanout_setup
    options = _coordinator_options(fanout_setup)
    with pytest.raises(TypeError, match="dispatcher"):
        SinaFzbFanoutCoordinator(None, journal, task, _manifest(), **options)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="task"):
        SinaFzbFanoutCoordinator(dispatcher, journal, None, _manifest(), **options)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only accepts"):
        SinaFzbFanoutCoordinator(
            dispatcher,
            journal,
            task.model_copy(update={"job_id": "other-job"}),
            _manifest(),
            **options,
        )
    with pytest.raises(ValueError, match="explicit cron"):
        SinaFzbFanoutCoordinator(
            dispatcher,
            journal,
            task.model_copy(update={"schedule": None}),
            _manifest(),
            **options,
        )
    with pytest.raises(ValueError, match="numeric weekday"):
        SinaFzbFanoutCoordinator(
            dispatcher,
            journal,
            task.model_copy(
                update={"schedule": ScheduleMetadata(kind="cron", expression="30 8 * * 1")}
            ),
            _manifest(),
            **options,
        )
    with pytest.raises(ValueError, match="enabled reviewed job"):
        SinaFzbFanoutCoordinator(
            dispatcher,
            journal,
            task.model_copy(update={"enabled": False}),
            _manifest(),
            **options,
        )
    with pytest.raises(TypeError, match="manifest"):
        SinaFzbFanoutCoordinator(dispatcher, journal, task, None, **options)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="journal"):
        SinaFzbFanoutCoordinator(
            dispatcher,
            object(),
            task,
            _manifest(),
            **options,
        )


def test_scheduler_listener_persists_group_then_pump_enqueues_exact_partitions(
    fanout_setup: FanoutSetup,
) -> None:
    """Actual submission slots persist first; recovery submits query-free children."""
    engine, journal, _, task, _ = fanout_setup
    coordinator = _coordinator(fanout_setup)
    scheduler = BackgroundScheduler(timezone=UTC)
    cron_job_id, pump_job_id = coordinator.register(scheduler)

    cron_job = scheduler.get_job(cron_job_id)
    pump_job = scheduler.get_job(pump_job_id)
    assert cron_job is not None and cron_job.func == coordinator._noop_tick
    assert pump_job is not None and pump_job.func == coordinator._pump_tick
    assert cron_job.coalesce is False
    assert pump_job.coalesce is True
    with pytest.raises(ValueError, match="already registered"):
        coordinator.register(scheduler)
    local_slot = OCCURRENCE.astimezone(ZoneInfo(task.timezone))
    event = JobSubmissionEvent(EVENT_JOB_SUBMITTED, cron_job_id, "default", [local_slot])
    receipts = coordinator(event)
    assert len(receipts) == 1
    assert receipts[0].status == "PENDING"
    assert receipts[0].member_count == len(SYMBOLS)
    assert _ledger_count(engine) == 0

    pending = journal.get(receipts[0].fanout_key)
    assert pending is not None and pending.next_index == 0
    first_chunk = coordinator.run_pending(max_chunks=1)
    assert first_chunk.claimed_chunks == 1
    assert first_chunk.accepted_runs == 2
    assert first_chunk.pending_chunks == 1
    intermediate = journal.get(receipts[0].fanout_key)
    assert intermediate is not None
    assert intermediate.status == "PENDING"
    assert intermediate.next_index == 2
    assert intermediate.lease_owner is None
    assert intermediate.attempt_count == 0
    assert _ledger_count(engine) == 2

    final_chunk = coordinator.run_pending(max_chunks=1)
    assert final_chunk.claimed_chunks == 1
    assert final_chunk.accepted_runs == 1
    assert final_chunk.enqueued_groups == 1
    completed = journal.get(receipts[0].fanout_key)
    assert completed is not None
    assert completed.status == ENQUEUED
    assert completed.next_index == len(SYMBOLS)
    assert completed.enqueued_at is not None
    assert _ledger_count(engine) == len(SYMBOLS)

    with engine.connect() as connection:
        rows = connection.execute(select(RUN_LEDGER_TABLE)).mappings().all()
    requests = [json.loads(row["job_snapshot_json"]) for row in rows]
    assert {
        request["run"]["business_request"]["partition"]["instrument_id"] for request in requests
    } == set(SYMBOLS)
    assert all(request["run"]["business_request"]["query"] == {} for request in requests)
    assert all(request["run"]["shard"] is None for request in requests)
    assert all(
        request["run"]["occurrence"] == "2026-10-26T08:30:00.000000Z" for request in requests
    )


def test_partial_chunk_failure_replays_same_child_ids_and_honors_retry_delay(
    fanout_setup: FanoutSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient child failure leaves the cursor and safely replays accepted rows."""
    engine, journal, dispatcher, _, clock_value = fanout_setup
    coordinator = _coordinator(fanout_setup)
    group = coordinator.create_scheduled_group(OCCURRENCE)
    original_dispatch = dispatcher.dispatch_scheduled
    attempts = 0

    def fail_once(*args, **kwargs):
        nonlocal attempts
        if not kwargs.get("dry_run", False):
            attempts += 1
            if attempts == 2:
                raise RuntimeError("private provider response")
        return original_dispatch(*args, **kwargs)

    monkeypatch.setattr(dispatcher, "dispatch_scheduled", fail_once)
    failed = coordinator.run_pending()
    assert failed.retry_groups == 1
    assert failed.accepted_runs == 1
    assert failed.outcomes[0].error_code == "RUNTIME_ERROR"
    assert _ledger_count(engine) == 1
    waiting = journal.get(group.fanout_key)
    assert waiting is not None
    assert waiting.status == "PENDING"
    assert waiting.next_index == 0
    assert waiting.attempt_count == 1
    assert waiting.lease_owner is None
    assert waiting.last_error_code == "RUNTIME_ERROR"
    assert waiting.next_attempt_at == OCCURRENCE + timedelta(seconds=2)

    clock_value[0] = OCCURRENCE + timedelta(seconds=3)
    replayed = coordinator.run_pending()
    assert replayed.enqueued_groups == 1
    assert replayed.accepted_runs == len(SYMBOLS)
    assert _ledger_count(engine) == len(SYMBOLS)
    completed = journal.get(group.fanout_key)
    assert completed is not None and completed.status == ENQUEUED
    assert completed.next_index == len(SYMBOLS)
    assert completed.attempt_count == 1
    assert "private provider response" not in repr(replayed)


def test_crash_after_child_acceptance_replays_chunk_after_lease_expiry(
    fanout_setup: FanoutSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost cursor transaction replays deterministic child keys after expiry."""
    engine, journal, _, _, clock_value = fanout_setup
    coordinator = _coordinator(fanout_setup)
    group = coordinator.create_scheduled_group(OCCURRENCE)
    original_advance = journal.advance_cursor

    def fail_first_advance(**kwargs):
        raise RuntimeError("simulated process loss after child acceptance")

    monkeypatch.setattr(journal, "advance_cursor", fail_first_advance)
    interrupted = coordinator.run_pending(max_chunks=1)
    assert interrupted.claimed_chunks == 1
    assert interrupted.global_error == "RUNTIME_ERROR"
    assert interrupted.outcomes == ()
    assert _ledger_count(engine) == 2
    leased = journal.get(group.fanout_key)
    assert leased is not None
    assert leased.next_index == 0
    assert leased.lease_owner == "test-fanout-worker"
    first_fence = leased.fencing_token

    monkeypatch.setattr(journal, "advance_cursor", original_advance)
    clock_value[0] = OCCURRENCE + timedelta(seconds=61)
    replayed = coordinator.run_pending(max_chunks=1)
    assert replayed.claimed_chunks == 1
    assert replayed.pending_chunks == 1
    assert replayed.accepted_runs == 2
    assert _ledger_count(engine) == 2
    resumed = journal.get(group.fanout_key)
    assert resumed is not None
    assert resumed.next_index == 2
    assert resumed.lease_owner is None
    assert resumed.fencing_token > first_fence
    assert resumed.attempt_count == 0


def test_manifest_drift_fails_before_any_child_request(fanout_setup: FanoutSetup) -> None:
    """A different selection snapshot cannot reuse a durable occurrence slot."""
    engine, _, _, _, _ = fanout_setup
    first = _coordinator(fanout_setup)
    first.create_scheduled_group(OCCURRENCE)
    changed = _manifest(("000001.SZ", "600000.SH"))
    second = _coordinator(fanout_setup, changed)

    with pytest.raises(FanoutManifestConflictError):
        second.create_scheduled_group(OCCURRENCE)
    assert _ledger_count(engine) == 0


def test_listener_records_partial_slot_failure_without_leaking_details(
    fanout_setup: FanoutSetup,
) -> None:
    """Scheduler-facing errors contain only bounded codes and accepted receipts."""
    _, journal, _, _, _ = fanout_setup
    coordinator = _coordinator(fanout_setup)
    scheduler = BackgroundScheduler(timezone=UTC)
    cron_job_id, _ = coordinator.register(scheduler)
    valid_slot = OCCURRENCE.astimezone(ZoneInfo("UTC"))
    wrong_zone = OCCURRENCE.astimezone(ZoneInfo("Asia/Shanghai"))
    event = JobSubmissionEvent(
        EVENT_JOB_SUBMITTED,
        cron_job_id,
        "default",
        [valid_slot, wrong_zone],
    )

    with pytest.raises(SinaFzbFanoutScheduleError) as error:
        coordinator(event)

    receipt = error.value.receipt
    assert len(receipt.accepted) == 1
    assert len(receipt.failures) == 1
    assert receipt.failures[0].code == "VALUE_ERROR"
    assert "Asia/Shanghai" not in str(error.value)
    assert journal.get(receipt.accepted[0].fanout_key) is not None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fanout_key", "invalid"),
        ("manifest_sha256", "invalid"),
        ("manifest_json", None),
        ("job_id", "other-job"),
        ("job_version", "other-version"),
        ("occurrence", datetime(2026, 10, 26, 8, 30)),
        ("symbols", ()),
        ("next_index", True),
        ("next_index", -1),
        ("next_index", len(SYMBOLS)),
        ("status", "UNKNOWN"),
        ("attempt_count", True),
        ("attempt_count", -1),
        ("fencing_token", True),
        ("fencing_token", -1),
        ("enqueued_at", OCCURRENCE),
    ],
)
def test_record_validation_rejects_corrupt_persisted_row_fields(
    fanout_setup: FanoutSetup,
    field: str,
    value: object,
) -> None:
    """Recovery never dispatches rows with mismatched status or manifest fields."""
    record = _claimed_group(fanout_setup)
    corrupted = replace(record, **{field: value})
    with pytest.raises(ValueError):
        fanout_module._validate_record(corrupted)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"contract_version":1,"job_id":"sina-fzb-capture","job_id":"sina-fzb-capture"}',
        "NaN",
    ],
)
def test_record_validation_rejects_malformed_json_and_duplicate_keys(
    fanout_setup: FanoutSetup,
    payload: str,
) -> None:
    """Snapshot parsing rejects malformed JSON, duplicate keys, and constants."""
    record = _claimed_group(fanout_setup)
    with pytest.raises(ValueError):
        fanout_module._validate_record(_rehashed_record(record, payload))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("contract_version", True),
        ("contract_version", 2),
        ("job_id", "bad id"),
        ("job_id", "different-job"),
        ("job_version", "bad version"),
        ("job_version", "different-version"),
        ("manifest_id", ""),
        ("selection_snapshot_ref", "has space"),
        ("symbols", "000001.SZ"),
        ("symbols", ["600000.SH", "000001.SZ"]),
        ("symbols", ["000001.SZ", "000001.SZ"]),
        ("occurrence", "2026-10-26T08:30:00+00:00"),
        ("occurrence", "not-a-timeZ"),
    ],
)
def test_record_validation_rejects_noncanonical_manifest_document_fields(
    fanout_setup: FanoutSetup,
    field: str,
    value: object,
) -> None:
    """The persisted manifest must retain its exact seven-field canonical schema."""
    record = _claimed_group(fanout_setup)
    document = json.loads(record.manifest_json)
    document[field] = value
    payload = fanout_module._canonical_json(document)
    with pytest.raises(ValueError):
        fanout_module._validate_record(_rehashed_record(record, payload))


def test_record_validation_rejects_oversized_digest_mismatch_and_noncanonical_json(
    fanout_setup: FanoutSetup,
) -> None:
    """Persisted snapshot byte bounds and digest/canonical checks fail closed."""
    record = _claimed_group(fanout_setup)
    oversized = replace(
        record,
        manifest_json=" " * (fanout_module.MAX_FANOUT_MANIFEST_BYTES + 1),
    )
    with pytest.raises(ValueError, match="byte limit"):
        fanout_module._validate_record(oversized)
    with pytest.raises(ValueError, match="digest"):
        fanout_module._validate_record(replace(record, manifest_sha256="0" * 64))
    noncanonical = _rehashed_record(record, json.dumps(json.loads(record.manifest_json), indent=2))
    with pytest.raises(ValueError, match="canonical encoding"):
        fanout_module._validate_record(noncanonical)


@pytest.mark.parametrize(
    ("input_symbols", "message"),
    [
        ("000001.SZ", "iterable"),
        (None, "iterable"),
        ((), "nonempty"),
        (("600000.SH", "000001.SZ"), "strictly sorted"),
        (("000001.SZ", "000001.SZ"), "unique"),
        (("SZ000001",), "noncanonical"),
    ],
)
def test_manifest_rejects_invalid_iterables_and_direct_tuple_inputs(
    input_symbols: object,
    message: str,
) -> None:
    """Caller selections are bounded, immutable, canonical, and duplicate-free."""
    if isinstance(input_symbols, tuple):
        with pytest.raises(ValueError, match=message):
            SinaFzbInstrumentManifest(
                manifest_id="manifest-001",
                selection_snapshot_ref="snapshot-001",
                symbols=input_symbols,
            )
    else:
        with pytest.raises(ValueError, match=message):
            SinaFzbInstrumentManifest.from_symbols(
                manifest_id="manifest-001",
                selection_snapshot_ref="snapshot-001",
                symbols=input_symbols,  # type: ignore[arg-type]
            )


def test_manifest_and_schedule_helper_boundaries() -> None:
    """Identifier, reference, UTC-slot, and cron dialect checks stay explicit."""
    valid_task = TaskDefinition(
        job_id=SINA_FZB_FANOUT_JOB_ID,
        version="process-v1",
        dataset_id="sina-fzb-observations",
        provider="sina",
        model="financial-statement",
        timezone="UTC",
        schedule=ScheduleMetadata(kind="cron", expression="30 8 * * mon-fri"),
        enabled=True,
    )
    fanout_module._validate_cron_dialect(valid_task)
    for expression, message in (
        ("30 8 * * * *", "five fields"),
        ("30 8 * * 1", "numeric weekday"),
        ("30 8 * * nonsense", "named weekdays"),
    ):
        invalid_task = valid_task.model_copy(
            update={
                "schedule": ScheduleMetadata(kind="cron", expression=expression),
            }
        )
        with pytest.raises(ValueError, match=message):
            fanout_module._validate_cron_dialect(invalid_task)
    with pytest.raises(ValueError, match="cron"):
        fanout_module._validate_cron_dialect(valid_task.model_copy(update={"schedule": None}))
    with pytest.raises(ValueError, match="invalid"):
        fanout_module._validate_identifier("bad identifier", "job_id")
    with pytest.raises(ValueError, match="invalid"):
        fanout_module._validate_opaque_ref("bad\nref", "manifest_id")
    with pytest.raises(ValueError, match="nonempty"):
        fanout_module._validate_symbols((), require_sorted=True)
    with pytest.raises(ValueError, match="limit"):
        fanout_module._validate_symbols(
            ("000001.SZ",) * (fanout_module.MAX_FANOUT_SYMBOLS + 1),
            require_sorted=True,
        )
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        fanout_module._require_utc(datetime(2026, 10, 26, 8, 30))
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        fanout_module._require_utc(datetime(2026, 10, 26, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai")))
    with pytest.raises(ValueError, match="timezone-aware"):
        fanout_module._utc_from_scheduler_slot(datetime(2026, 10, 26, 8, 30), valid_task)
    with pytest.raises(ValueError, match="timezone does not match"):
        fanout_module._utc_from_scheduler_slot(
            OCCURRENCE.astimezone(ZoneInfo("Asia/Shanghai")),
            valid_task,
        )
    assert fanout_module._utc_from_scheduler_slot(OCCURRENCE, valid_task) == OCCURRENCE
    assert fanout_module._parse_occurrence("2026-10-26T08:30:00.000000Z") == OCCURRENCE
    for invalid_occurrence in (
        "2026-10-26T08:30:00+00:00",
        "2026-02-30T08:30:00.000000Z",
        "2026-10-26T08:30:00Z",
    ):
        with pytest.raises(ValueError):
            fanout_module._parse_occurrence(invalid_occurrence)


def test_json_and_error_code_helpers_are_strict_and_sanitized() -> None:
    """Canonical serialization rejects unsupported values and error text is hidden."""
    with pytest.raises(ValueError, match="canonical JSON"):
        fanout_module._canonical_json({"not_json": object()})
    with pytest.raises(ValueError, match="canonical JSON"):
        fanout_module._canonical_json({"not_finite": float("nan")})

    class PrivateProviderError(RuntimeError):
        """Distinct exception type carrying a secret message."""

    odd_name_error = type("bad-name", (RuntimeError,), {})()
    assert (
        fanout_module._safe_error_code(PrivateProviderError("secret")) == "PRIVATE_PROVIDER_ERROR"
    )
    assert fanout_module._safe_error_code(odd_name_error) == "UNHANDLED_ERROR"
    assert fanout_module._retry_delay(timedelta(seconds=2), timedelta(seconds=30), 0) == timedelta(
        seconds=2
    )
    assert fanout_module._retry_delay(timedelta(seconds=2), timedelta(seconds=30), 1) == timedelta(
        seconds=4
    )
    assert fanout_module._retry_delay(timedelta(seconds=2), timedelta(seconds=30), 20) == timedelta(
        seconds=30
    )
