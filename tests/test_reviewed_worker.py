"""Exercise durable reviewed workers against an owned SQLite warehouse."""

from __future__ import annotations

import multiprocessing
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import BigInteger, Column, MetaData, String, Table, create_engine, func, select

from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.reviewed_worker import PersistentRunWorker, ProcessFetchHandler
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
)

if TYPE_CHECKING:
    from pydantic import JsonValue
    from sqlalchemy.engine import Connection

UTC = timezone.utc
DATASET = "fixture_worker_daily"
FACT_METADATA = MetaData()
WORKER_FACTS = Table(
    "fixture_worker_facts",
    FACT_METADATA,
    Column("business_key", String(255), primary_key=True),
    Column("close_value", BigInteger, nullable=False),
)


def fixture_worker_fetch(parameters: dict[str, JsonValue]) -> JsonValue:
    """Return deterministic data or exercise process lifetime controls."""
    marker = parameters.get("started_path")
    if isinstance(marker, str):
        Path(marker).write_text(str(os.getpid()), encoding="ascii")
    delay = parameters.get("delay_seconds", 0)
    if isinstance(delay, (int, float)):
        time.sleep(float(delay))
    if parameters.get("mode") == "error":
        raise RuntimeError("upstream token=secret-test-value")
    return {"instrument": "FIXTURE:ONE", "trade_date": "2026-10-26", "close": 12}


def _identity_decoder(value: JsonValue) -> object:
    """Return the JSON value unchanged for adapter validation tests."""
    return value


def _make_engine(
    database_path: str | Path,
    *,
    timeout_seconds: float = 0.05,
    pool_timeout_seconds: float | None = None,
):
    """Open a process-local engine for one test-owned SQLite file."""
    return create_engine(
        f"sqlite:///{database_path}",
        connect_args={"timeout": timeout_seconds},
        pool_timeout=pool_timeout_seconds or timeout_seconds,
    )


def _make_worker(
    database_path: str | Path,
    worker_id: str,
    *,
    timeout_seconds: float = 15.0,
    checkpoint_interval_seconds: float = 0.05,
) -> PersistentRunWorker:
    """Construct an isolated worker with a reviewed process-backed fetcher."""
    engine = _make_engine(database_path)
    fetch = _make_process_fetch_handler(
        timeout_seconds=timeout_seconds,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
    )
    job = ReviewedJob(
        task=_task(),
        fetch=fetch,
        normalize=_normalize,
        publish=_publish,
        reviewed=True,
    )
    return PersistentRunWorker(
        engine,
        ReviewedJobRegistry([job]),
        worker_id=worker_id,
        lease_ttl=timedelta(seconds=2),
        heartbeat_interval_seconds=checkpoint_interval_seconds,
        database_io_timeout_seconds=0.05,
    )


def _make_process_fetch_handler(
    *, timeout_seconds: float, checkpoint_interval_seconds: float = 0.05
) -> ProcessFetchHandler:
    """Bind a fixed source operation to explicit parent JSON conversions."""
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-worker-daily": fixture_worker_fetch}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=checkpoint_interval_seconds,
    )
    return ProcessFetchHandler(
        executor,
        "fixture-worker-daily",
        lambda request: dict(request.query),
        lambda value: value,
        timeout_seconds=timeout_seconds,
    )


def _task() -> TaskDefinition:
    """Create a reviewed fixture task with a valid schedule definition."""
    return TaskDefinition(
        job_id="fixture-worker-daily",
        version="v1",
        dataset_id=DATASET,
        provider="fixture-provider",
        model="fixture-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        enabled=True,
    )


def _normalize(raw: object, _request: RunRequest, _context: RunExecutionContext) -> NormalizedBatch:
    """Convert the fixture provider response into one typed fact row."""
    if not isinstance(raw, dict):
        raise ValueError("fixture_response_invalid")
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key=f"{raw['instrument']}:{raw['trade_date']}",
                payload={"close": raw["close"]},
                business_date=date.fromisoformat(str(raw["trade_date"])),
            ),
        ),
        schema_version="1",
        semantic_version="fixture-v1",
        unit="CNY/share",
        adjustment_basis="raw",
        quality={"rejected_rows": 0},
        provenance={"source": "fixture"},
    )


def _publish(
    connection: Connection,
    task: TaskDefinition,
    _request: RunRequest,
    batch: NormalizedBatch,
    _idempotency_key: str,
) -> PublicationReceipt:
    """Write facts only through the fenced connection supplied by the ledger."""
    for row in batch.records:
        connection.execute(
            WORKER_FACTS.insert().values(
                business_key=row.business_key,
                close_value=int(row.payload["close"]),
            )
        )
    return PublicationReceipt(
        publication_id="fixture-publication",
        dataset_id=task.dataset_id,
        record_count=len(batch.records),
    )


def _initialize_database(database_path: str | Path) -> None:
    """Create tables in a test-owned durable file before worker processes start."""
    engine = _make_engine(database_path)
    RUN_LEDGER_METADATA.create_all(engine)
    FACT_METADATA.create_all(engine)
    engine.dispose()


def _run_worker_process(
    database_path: str,
    worker_id: str,
    start_event: multiprocessing.synchronize.Event,
    result_queue: multiprocessing.queues.Queue,
    *,
    timeout_seconds: float = 5.0,
) -> None:
    """Run one worker in its own process and return only its durable status."""
    worker = _make_worker(database_path, worker_id, timeout_seconds=timeout_seconds)
    start_event.wait(10)
    outcome = worker.run_once()
    result_queue.put(None if outcome is None else outcome.record.status)


def _request(query: dict[str, JsonValue] | None = None) -> RunRequest:
    """Build a bounded manual request with stable idempotency."""
    return RunRequest(
        run_type="manual",
        query=query or {"instrument": "FIXTURE:ONE"},
        idempotency_key="fixture-worker-request",
    )


def _setup_database(path: Path) -> None:
    """Initialize an empty durable database for a worker test."""
    _initialize_database(path)


def _count_facts(database_path: str | Path) -> int:
    """Read one fact count through a separately opened test connection."""
    engine = _make_engine(database_path)
    try:
        with engine.connect() as connection:
            return connection.execute(select(func.count()).select_from(WORKER_FACTS)).scalar_one()
    finally:
        engine.dispose()


def test_process_fetch_adapter_builds_and_decodes_only_parent_json(tmp_path: Path) -> None:
    """The registered operation is selected statically and sees JSON values."""
    database_path = tmp_path / "adapter.sqlite"
    _setup_database(database_path)
    request = _request({"instrument": "FIXTURE:ONE", "mode": "normal"})
    context = RunExecutionContext("run-key", "adapter-worker", 1, lambda: None)

    raw = _make_process_fetch_handler(timeout_seconds=15)(request, context)

    assert raw == {"instrument": "FIXTURE:ONE", "trade_date": "2026-10-26", "close": 12}


def test_persisted_worker_reuses_completed_state_after_reconstruction(tmp_path: Path) -> None:
    """A reconstructed worker reads the same durable ledger and does not replay."""
    database_path = tmp_path / "restart.sqlite"
    _setup_database(database_path)
    first = _make_worker(database_path, "worker-before-restart")
    queued = first.submit("fixture-worker-daily", "v1", _request())

    outcome = first.run_once()
    second = _make_worker(database_path, "worker-after-restart")

    assert queued.status == "queued"
    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert outcome.publication == PublicationReceipt("fixture-publication", DATASET, 1)
    assert second.ledger.get(queued.run_key) == outcome.record
    assert second.run_once() is None
    assert _count_facts(database_path) == 1


def test_two_independent_workers_compete_for_one_persistent_run(tmp_path: Path) -> None:
    """Two OS workers race on one queued slot and only one publishes the fact."""
    database_path = tmp_path / "competition.sqlite"
    _setup_database(database_path)
    submitter = _make_worker(database_path, "submitter")
    submitter.submit(
        "fixture-worker-daily",
        "v1",
        _request({"instrument": "FIXTURE:ONE", "delay_seconds": 0.3}),
    )
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    workers = [
        context.Process(
            target=_run_worker_process,
            args=(database_path.as_posix(), f"competing-worker-{index}", start_event, result_queue),
        )
        for index in (1, 2)
    ]
    for process in workers:
        process.start()
    start_event.set()
    for process in workers:
        process.join(20)
    try:
        assert [process.exitcode for process in workers] == [0, 0]
        outcomes = [result_queue.get(timeout=2), result_queue.get(timeout=2)]
        assert outcomes.count("succeeded") == 1
        assert outcomes.count(None) == 1
        assert _count_facts(database_path) == 1
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(2)
        result_queue.close()
        result_queue.join_thread()


def test_expired_lease_is_reclaimed_after_worker_process_restart(tmp_path: Path) -> None:
    """A new worker reclaims persisted work and the old token cannot publish."""
    database_path = tmp_path / "expired.sqlite"
    _setup_database(database_path)
    abandoned = _make_worker(database_path, "worker-that-crashed")
    queued = abandoned.submit("fixture-worker-daily", "v1", _request())
    old_claim = abandoned.ledger.claim(
        "worker-that-crashed", datetime.now(UTC), timedelta(seconds=1)
    )
    assert old_claim is not None

    time.sleep(1.1)
    restarted = _make_worker(database_path, "worker-after-crash")
    outcome = restarted.run_once()
    stale_callback_calls: list[bool] = []
    stale_result = restarted.ledger.commit_result(
        queued.run_key,
        worker="worker-that-crashed",
        fencing_token=old_claim.fencing_token,
        now=datetime.now(UTC),
        operation=lambda _connection: stale_callback_calls.append(True),  # type: ignore[arg-type,return-value]
    )

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert stale_result is None
    assert stale_callback_calls == []
    assert restarted.ledger.get(queued.run_key).fencing_token == old_claim.fencing_token + 1


def test_database_timing_budget_rejects_unbounded_or_unsafe_values(tmp_path: Path) -> None:
    """Lease timing cannot be configured below the explicit I/O safety budget."""
    database_path = tmp_path / "budget.sqlite"
    _setup_database(database_path)
    engine = _make_engine(database_path)
    registry = ReviewedJobRegistry()

    with pytest.raises(ValueError, match="lease_ttl is too short"):
        PersistentRunWorker(
            engine,
            registry,
            worker_id="short-lease",
            lease_ttl=timedelta(seconds=1),
            heartbeat_interval_seconds=0.5,
            database_io_timeout_seconds=0.3,
        )
    with pytest.raises(ValueError, match="database_io_timeout_seconds"):
        PersistentRunWorker(
            engine,
            registry,
            worker_id="unbounded-io",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=float("inf"),
        )
    for invalid_ttl in (object(), timedelta(milliseconds=500), timedelta(days=2)):
        with pytest.raises((TypeError, ValueError)):
            PersistentRunWorker(
                engine,
                registry,
                worker_id="invalid-ttl",
                lease_ttl=invalid_ttl,  # type: ignore[arg-type]
                heartbeat_interval_seconds=0.05,
                database_io_timeout_seconds=0.05,
            )


def test_worker_rejects_engines_without_verified_sqlite_timeouts(tmp_path: Path) -> None:
    """Memory, non-SQLite, and over-bound engines fail closed at construction."""
    registry = ReviewedJobRegistry()
    worker_options = {
        "worker_id": "bounded-worker",
        "lease_ttl": timedelta(seconds=2),
        "heartbeat_interval_seconds": 0.05,
        "database_io_timeout_seconds": 0.05,
    }
    memory_engine = create_engine("sqlite:///:memory:")
    with pytest.raises(ValueError, match="file-backed SQLite"):
        PersistentRunWorker(memory_engine, registry, **worker_options)

    oversized_wait_engine = _make_engine(tmp_path / "oversized-wait.sqlite", timeout_seconds=0.1)
    with pytest.raises(ValueError, match="exceeds the configured database I/O bound"):
        PersistentRunWorker(oversized_wait_engine, registry, **worker_options)

    oversized_lock_wait_engine = _make_engine(
        tmp_path / "oversized-lock.sqlite",
        timeout_seconds=0.1,
        pool_timeout_seconds=0.05,
    )
    with pytest.raises(ValueError, match="busy_timeout exceeds"):
        PersistentRunWorker(oversized_lock_wait_engine, registry, **worker_options)

    mysql_engine = create_engine("mysql+pymysql://user:password@localhost/test")
    with pytest.raises(ValueError, match="requires verified file-backed SQLite"):
        PersistentRunWorker(mysql_engine, registry, **worker_options)


def test_worker_rejects_invalid_catalog_and_mismatched_checkpoint_interval(
    tmp_path: Path,
) -> None:
    """Worker identity, catalog type, and real executor cadence are checked."""
    engine = _make_engine(tmp_path / "catalog.sqlite")
    with pytest.raises(TypeError, match="ReviewedJobRegistry"):
        PersistentRunWorker(
            engine,
            object(),  # type: ignore[arg-type]
            worker_id="catalog-worker",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )
    with pytest.raises(ValueError, match="worker_id"):
        PersistentRunWorker(
            engine,
            ReviewedJobRegistry(),
            worker_id=" ",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )

    fetch = _make_process_fetch_handler(timeout_seconds=5, checkpoint_interval_seconds=0.05)
    job = ReviewedJob(_task(), fetch, _normalize, _publish, reviewed=True)
    with pytest.raises(ValueError, match="match every process executor"):
        PersistentRunWorker(
            engine,
            ReviewedJobRegistry([job]),
            worker_id="mismatched-checkpoint",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.1,
            database_io_timeout_seconds=0.05,
        )


def test_fetch_adapter_rejects_invalid_bindings_and_context(tmp_path: Path) -> None:
    """The adapter accepts only explicit dependencies and JSON-object builders."""
    engine = _make_engine(tmp_path / "adapter-errors.sqlite")
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-worker-daily": fixture_worker_fetch}),
        checkpoint_interval_seconds=0.05,
    )
    request = _request()
    context = RunExecutionContext("run-key", "adapter-worker", 1, lambda: None)

    with pytest.raises(TypeError, match="ReviewedProcessExecutor"):
        ProcessFetchHandler(
            object(),  # type: ignore[arg-type]
            "fixture-worker-daily",
            lambda _request: {},
            _identity_decoder,
            timeout_seconds=5,
        )
    with pytest.raises(ValueError, match="operation_id"):
        ProcessFetchHandler(executor, "", lambda _request: {}, _identity_decoder, timeout_seconds=5)
    with pytest.raises(TypeError, match="parameters_builder"):
        ProcessFetchHandler(
            executor,
            "fixture-worker-daily",
            None,  # type: ignore[arg-type]
            _identity_decoder,
            timeout_seconds=5,
        )
    with pytest.raises(TypeError, match="result_decoder"):
        ProcessFetchHandler(
            executor,
            "fixture-worker-daily",
            lambda _request: {},
            None,  # type: ignore[arg-type]
            timeout_seconds=5,
        )
    with pytest.raises(ValueError, match="timeout_seconds"):
        ProcessFetchHandler(
            executor,
            "fixture-worker-daily",
            lambda _request: {},
            _identity_decoder,
            timeout_seconds=True,
        )

    wrong_request = ProcessFetchHandler(
        executor, "fixture-worker-daily", lambda _request: {}, _identity_decoder, timeout_seconds=5
    )
    with pytest.raises(TypeError, match="validated RunRequest"):
        wrong_request(object(), context)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="RunExecutionContext"):
        wrong_request(request, object())  # type: ignore[arg-type]

    non_object_builder = ProcessFetchHandler(
        executor,
        "fixture-worker-daily",
        lambda _request: [],  # type: ignore[arg-type,return-value]
        _identity_decoder,
        timeout_seconds=5,
    )
    with pytest.raises(TypeError, match="JSON object"):
        non_object_builder(request, context)

    with pytest.raises(TypeError, match="synchronous SQLAlchemy Engine"):
        PersistentRunWorker(
            object(),  # type: ignore[arg-type]
            ReviewedJobRegistry(),
            worker_id="invalid-engine",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )

    engine.dispose()


def test_timeout_kills_child_and_error_record_does_not_leak_exception_text(
    tmp_path: Path,
) -> None:
    """A fetch deadline terminates its child and ledger stores only a safe code."""
    database_path = tmp_path / "timeout.sqlite"
    _setup_database(database_path)
    worker = _make_worker(database_path, "timeout-worker", timeout_seconds=3)
    marker = tmp_path / "timeout-child.pid"
    queued = worker.submit(
        "fixture-worker-daily",
        "v1",
        _request(
            {
                "instrument": "FIXTURE:ONE",
                "delay_seconds": 10,
                "started_path": marker.as_posix(),
            }
        ),
    )

    outcome = worker.run_once()

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:ProcessExecutionTimeoutError"
    assert "secret-test-value" not in (outcome.record.error_text or "")
    assert worker.ledger.get(queued.run_key).status == "failed"
    assert marker.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text(encoding="ascii")), 0)
    assert _count_facts(database_path) == 0


def test_child_exception_text_is_suppressed_before_ledger_persistence(tmp_path: Path) -> None:
    """Provider exception messages and credentials never enter run error text."""
    database_path = tmp_path / "error.sqlite"
    _setup_database(database_path)
    worker = _make_worker(database_path, "error-worker")
    queued = worker.submit(
        "fixture-worker-daily",
        "v1",
        _request({"instrument": "FIXTURE:ONE", "mode": "error"}),
    )

    outcome = worker.run_once()

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:ProcessExecutionHandlerError"
    assert "token" not in (outcome.record.error_text or "").lower()
    assert "secret-test-value" not in (outcome.record.error_text or "")
    assert worker.ledger.get(queued.run_key).status == "failed"


def test_cancelled_lease_stops_running_child_without_publication(tmp_path: Path) -> None:
    """A database cancellation is observed by the heartbeat and kills the child."""
    database_path = tmp_path / "cancel.sqlite"
    _setup_database(database_path)
    marker = tmp_path / "child.pid"
    submitter = _make_worker(database_path, "cancel-submitter")
    queued = submitter.submit(
        "fixture-worker-daily",
        "v1",
        _request(
            {
                "instrument": "FIXTURE:ONE",
                "delay_seconds": 10,
                "started_path": marker.as_posix(),
            }
        ),
    )
    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    # The worker target waits on its start event. Reuse a set event to avoid a
    # test-only background thread or a second cancellation control channel.
    start_event = context.Event()
    start_event.set()
    process = context.Process(
        target=_run_worker_process,
        args=(database_path.as_posix(), "cancelled-worker", start_event, result_queue),
    )
    process.start()
    try:
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), "fixture child did not start"
        child_pid = int(marker.read_text(encoding="ascii"))
        cancel_engine = _make_engine(database_path)
        cancel_ledger = RunLedger(cancel_engine, clock_policy="database")
        cancelled = cancel_ledger.cancel(queued.run_key, datetime.now(UTC))
        assert cancelled is not None and cancelled.status == "cancelled"
        process.join(10)
        assert process.exitcode == 0
        assert result_queue.get(timeout=2) == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
        assert _count_facts(database_path) == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(2)
        result_queue.close()
        result_queue.join_thread()
