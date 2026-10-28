"""Exercise durable reviewed workers against an owned SQLite warehouse."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import (
    BigInteger,
    Column,
    MetaData,
    String,
    Table,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.pool import QueuePool, StaticPool

from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    create_worker_engine,
)
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
    from collections.abc import Callable, Iterator

    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
DATASET = "fixture_worker_daily"
FACT_METADATA = MetaData()
WORKER_FACTS = Table(
    "fixture_worker_facts",
    FACT_METADATA,
    Column("business_key", String(255), primary_key=True),
    Column("close_value", BigInteger, nullable=False),
)


@pytest.fixture(autouse=True)
def _dispose_test_engines(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Dispose every Engine created by this test module after each test."""
    engines: list[Engine] = []
    sqlalchemy_factory: Callable[..., Engine] = create_engine
    worker_factory: Callable[..., Engine] = create_worker_engine

    def tracked_sqlalchemy_factory(*args: object, **kwargs: object) -> Engine:
        engine = sqlalchemy_factory(*args, **kwargs)
        engines.append(engine)
        return engine

    def tracked_worker_factory(*args: object, **kwargs: object) -> Engine:
        engine = worker_factory(*args, **kwargs)
        engines.append(engine)
        return engine

    # Track only the constructor names used in this test module. Engines from
    # worker subprocesses are disposed in _run_worker_process itself.
    monkeypatch.setitem(globals(), "create_engine", tracked_sqlalchemy_factory)
    monkeypatch.setitem(globals(), "create_worker_engine", tracked_worker_factory)
    try:
        yield
    finally:
        for engine in reversed(engines):
            engine.dispose()


def fixture_worker_fetch(parameters: dict[str, JsonValue]) -> JsonValue:
    """Return deterministic data or exercise process lifetime controls."""
    marker = parameters.get("started_path")
    if isinstance(marker, str):
        marker_path = Path(marker)
        temporary_path = marker_path.with_name(f".{marker_path.name}.{os.getpid()}.tmp")
        temporary_path.write_text(str(os.getpid()), encoding="ascii")
        os.replace(temporary_path, marker_path)
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
    database_io_timeout_seconds: float = 0.05,
) -> PersistentRunWorker:
    """Construct an isolated worker with a reviewed process-backed fetcher."""
    engine = create_worker_engine(
        database_path,
        database_io_timeout_seconds=database_io_timeout_seconds,
    )
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
        database_io_timeout_seconds=database_io_timeout_seconds,
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
    worker = _make_worker(
        database_path,
        worker_id,
        timeout_seconds=timeout_seconds,
        database_io_timeout_seconds=0.5,
    )
    try:
        start_event.wait(10)
        outcome = worker.run_once()
        result_queue.put(None if outcome is None else outcome.record.status)
    finally:
        worker.ledger._engine.dispose()


def _expect_inherited_engine_rejected(engine: object) -> None:
    """Assert a worker cannot adopt a parent-process Engine after ``fork``."""
    try:
        PersistentRunWorker(
            engine,  # type: ignore[arg-type]
            ReviewedJobRegistry(),
            worker_id="fork-child-worker",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )
    except ValueError as exc:
        if "cannot be reused across process boundaries" not in str(exc):
            raise
        return
    raise AssertionError("parent-process worker Engine was accepted after fork")


def _expect_inherited_worker_run_rejected(worker: PersistentRunWorker) -> None:
    """Assert an already-bound facade stops before touching its inherited pool."""
    try:
        worker.run_once()
    except ValueError as exc:
        if "cannot be reused across process boundaries" not in str(exc):
            raise
        return
    raise AssertionError("parent-process worker was allowed to run after fork")


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


def test_persistent_worker_executes_one_shot_only_by_explicit_key(tmp_path: Path) -> None:
    """The persistent worker keeps one-shot submission outside its broad queue path."""
    database_path = tmp_path / "one-shot.sqlite"
    _setup_database(database_path)
    worker = _make_worker(database_path, "one-shot-worker")
    queued = worker.submit_one_shot("fixture-worker-daily", "v1", _request())

    assert queued.claim_policy == "one_shot"
    assert worker.run_once() is None
    outcome = worker.run_one_shot(queued.run_key)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert outcome.publication == PublicationReceipt("fixture-publication", DATASET, 1)
    assert worker.run_one_shot(queued.run_key) is None
    assert worker.ledger.get(queued.run_key) == outcome.record
    assert _count_facts(database_path) == 1


def test_two_independent_workers_compete_for_one_persistent_run(tmp_path: Path) -> None:
    """Two OS workers race on one queued slot and only one publishes the fact."""
    database_path = tmp_path / "competition.sqlite"
    _setup_database(database_path)
    submitter = _make_worker(database_path, "submitter", database_io_timeout_seconds=0.5)
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
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(memory_engine, registry, **worker_options)

    oversized_wait_path = tmp_path / "oversized-wait.sqlite"
    oversized_wait_path.touch()
    oversized_wait_engine = create_worker_engine(
        oversized_wait_path, database_io_timeout_seconds=0.05
    )
    oversized_wait_engine.pool._timeout = 0.1
    with pytest.raises(ValueError, match="exceeds the configured database I/O bound"):
        PersistentRunWorker(oversized_wait_engine, registry, **worker_options)

    oversized_lock_path = tmp_path / "oversized-lock.sqlite"
    oversized_lock_path.touch()
    oversized_lock_wait_engine = create_worker_engine(
        oversized_lock_path, database_io_timeout_seconds=0.05
    )
    with oversized_lock_wait_engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=100")
    with pytest.raises(ValueError, match="busy_timeout exceeds"):
        PersistentRunWorker(oversized_lock_wait_engine, registry, **worker_options)

    mysql_engine = create_engine("mysql+pymysql://user:password@localhost/test")
    with pytest.raises(ValueError, match="requires verified file-backed SQLite"):
        PersistentRunWorker(mysql_engine, registry, **worker_options)

    static_pool_path = tmp_path / "static-pool.sqlite"
    static_pool_path.touch()
    static_pool_engine = create_engine(
        f"sqlite:///{static_pool_path}",
        connect_args={"timeout": 0.05},
        poolclass=StaticPool,
    )
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(static_pool_engine, registry, **worker_options)


def test_create_worker_engine_requires_an_existing_canonical_file(tmp_path: Path) -> None:
    """The worker factory does not accept aliases, directories, or missing paths."""
    with pytest.raises(ValueError, match="absolute"):
        create_worker_engine(Path("relative.sqlite"), database_io_timeout_seconds=0.05)
    with pytest.raises(ValueError, match="existing regular file"):
        create_worker_engine(tmp_path / "missing.sqlite", database_io_timeout_seconds=0.05)
    directory = tmp_path / "directory.sqlite"
    directory.mkdir()
    with pytest.raises(ValueError, match="canonical and identify a regular file"):
        create_worker_engine(directory, database_io_timeout_seconds=0.05)
    real_file = tmp_path / "real.sqlite"
    real_file.touch()
    symlink = tmp_path / "alias.sqlite"
    symlink.symlink_to(real_file)
    with pytest.raises(ValueError, match="canonical and identify a regular file"):
        create_worker_engine(symlink, database_io_timeout_seconds=0.05)


def test_worker_checks_actual_database_file_identity_and_rejects_memory_aliases(
    tmp_path: Path,
) -> None:
    """URL claims cannot disguise an in-memory or different SQLite connection."""
    worker_options = {
        "worker_id": "identity-worker",
        "lease_ttl": timedelta(seconds=2),
        "heartbeat_interval_seconds": 0.05,
        "database_io_timeout_seconds": 0.05,
    }
    registry = ReviewedJobRegistry()
    claimed_path = tmp_path / "claimed.sqlite"
    claimed_path.touch()
    uri_memory_engine = create_engine(
        "sqlite:///file:review-memory?mode=memory&cache=shared&uri=true",
        connect_args={"timeout": 0.05, "uri": True, "check_same_thread": False},
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(uri_memory_engine, registry, **worker_options)

    creator_memory_engine = create_engine(
        f"sqlite:///{claimed_path}",
        creator=lambda: sqlite3.connect(":memory:", timeout=0.05, check_same_thread=False),
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(creator_memory_engine, registry, **worker_options)

    other_path = tmp_path / "other.sqlite"
    other_path.touch()
    mismatched_creator_engine = create_engine(
        f"sqlite:///{claimed_path}",
        creator=lambda: sqlite3.connect(other_path.as_posix(), timeout=0.05),
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.05,
    )
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(mismatched_creator_engine, registry, **worker_options)

    symlink_path = tmp_path / "claimed-alias.sqlite"
    symlink_path.symlink_to(claimed_path)
    symlink_engine = create_engine(
        f"sqlite:///{symlink_path}",
        connect_args={"timeout": 0.05},
        pool_timeout=0.05,
    )
    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(symlink_engine, registry, **worker_options)


def test_factory_warmed_pool_rejects_path_replacement_before_worker_creation(
    tmp_path: Path,
) -> None:
    """A warmed factory Engine remains pinned to its original physical file."""
    database_path = tmp_path / "factory-warmed.sqlite"
    _setup_database(database_path)
    engine = create_worker_engine(database_path, database_io_timeout_seconds=0.05)
    replacement_path = tmp_path / "factory-replacement.sqlite"
    _setup_database(replacement_path)
    os.replace(replacement_path, database_path)

    with pytest.raises(ValueError, match="identity changed after worker Engine creation"):
        PersistentRunWorker(
            engine,
            ReviewedJobRegistry(),
            worker_id="replaced-before-worker",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )


def test_external_warmed_engine_is_rejected_even_after_path_replacement(
    tmp_path: Path,
) -> None:
    """A caller-owned pool cannot be adopted after its pathname is replaced."""
    database_path = tmp_path / "external-warmed.sqlite"
    _setup_database(database_path)
    external_engine = _make_engine(database_path)
    with external_engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA database_list").first() is not None
    replacement_path = tmp_path / "external-replacement.sqlite"
    _setup_database(replacement_path)
    os.replace(replacement_path, database_path)

    with pytest.raises(ValueError, match="must come from create_worker_engine"):
        PersistentRunWorker(
            external_engine,
            ReviewedJobRegistry(),
            worker_id="external-engine",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_factory_engine_cannot_be_reused_by_child_process(tmp_path: Path) -> None:
    """Engine registration is process-local and inherited pools fail closed."""
    database_path = tmp_path / "forked-engine.sqlite"
    _setup_database(database_path)
    engine = create_worker_engine(database_path, database_io_timeout_seconds=0.05)
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_expect_inherited_engine_rejected, args=(engine,))
    process.start()
    process.join(10)

    assert process.exitcode == 0


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires POSIX fork semantics")
def test_persistent_worker_cannot_run_after_fork(tmp_path: Path) -> None:
    """An inherited facade rejects work before using its parent-owned pool."""
    database_path = tmp_path / "forked-worker.sqlite"
    _setup_database(database_path)
    worker = _make_worker(database_path, "fork-parent-worker")
    context = multiprocessing.get_context("fork")
    process = context.Process(target=_expect_inherited_worker_run_rejected, args=(worker,))
    process.start()
    process.join(10)

    assert process.exitcode == 0


def test_worker_rechecks_busy_timeout_on_each_engine_checkout(tmp_path: Path) -> None:
    """A later DBAPI checkout cannot silently widen the verified lock wait."""
    database_path = tmp_path / "changing-timeout.sqlite"
    _setup_database(database_path)
    engine = create_worker_engine(database_path, database_io_timeout_seconds=0.05)
    checkout_count = 0

    def change_busy_timeout(
        dbapi_connection: sqlite3.Connection, _record: object, _proxy: object
    ) -> None:
        nonlocal checkout_count
        checkout_count += 1
        timeout_ms = 50 if checkout_count == 1 else 60_000
        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={timeout_ms}")
        cursor.close()

    event.listen(engine.pool, "checkout", change_busy_timeout)
    fetch = _make_process_fetch_handler(timeout_seconds=5, checkpoint_interval_seconds=0.05)
    job = ReviewedJob(_task(), fetch, _normalize, _publish, reviewed=True)
    worker = PersistentRunWorker(
        engine,
        ReviewedJobRegistry([job]),
        worker_id="changing-timeout-worker",
        lease_ttl=timedelta(seconds=2),
        heartbeat_interval_seconds=0.05,
        database_io_timeout_seconds=0.05,
    )
    with pytest.raises(ValueError, match="busy_timeout exceeds the configured database I/O bound"):
        worker.run_once()
    assert checkout_count == 2


def test_worker_rejects_pool_file_identity_change_after_construction(tmp_path: Path) -> None:
    """A pathname replaced after verification fails before a ledger operation."""
    database_path = tmp_path / "replaced.sqlite"
    _setup_database(database_path)
    worker = _make_worker(database_path, "replaced-file-worker")
    replacement_path = tmp_path / "replacement.sqlite"
    _setup_database(replacement_path)
    os.replace(replacement_path, database_path)

    with pytest.raises(ValueError, match="file identity changed"):
        worker.run_once()


def test_worker_checks_file_identity_again_before_database_commit(tmp_path: Path) -> None:
    """A file replaced after checkout cannot receive a stale-connection commit."""
    database_path = tmp_path / "commit-replaced.sqlite"
    replacement_path = tmp_path / "commit-replacement.sqlite"
    _setup_database(database_path)
    _setup_database(replacement_path)
    worker = _make_worker(database_path, "commit-replaced-worker")
    engine = worker.ledger._engine

    with pytest.raises(ValueError, match="file identity changed"), engine.begin() as connection:
        connection.execute(
            WORKER_FACTS.insert().values(business_key="FIXTURE:STALE", close_value=99)
        )
        os.replace(replacement_path, database_path)

    assert _count_facts(database_path) == 0


def test_worker_rejects_invalid_catalog_and_mismatched_checkpoint_interval(
    tmp_path: Path,
) -> None:
    """Worker identity, catalog type, and real executor cadence are checked."""
    catalog_path = tmp_path / "catalog.sqlite"
    catalog_path.touch()
    engine = create_worker_engine(catalog_path, database_io_timeout_seconds=0.05)
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


def test_persistent_worker_rejects_unbounded_fetch_handler(tmp_path: Path) -> None:
    """Ordinary in-process fetch handlers cannot run outside the lease boundary."""
    database_path = tmp_path / "unbounded-fetch.sqlite"
    database_path.touch()
    engine = create_worker_engine(database_path, database_io_timeout_seconds=0.05)
    fetch_calls = 0

    def ordinary_fetch(_request: object, _context: object) -> object:
        nonlocal fetch_calls
        fetch_calls += 1
        return {"data": "unbounded"}

    job = ReviewedJob(_task(), ordinary_fetch, _normalize, _publish, reviewed=True)
    with pytest.raises(ValueError, match="requires process-bounded reviewed fetch handlers"):
        PersistentRunWorker(
            engine,
            ReviewedJobRegistry([job]),
            worker_id="unbounded-fetch",
            lease_ttl=timedelta(seconds=2),
            heartbeat_interval_seconds=0.05,
            database_io_timeout_seconds=0.05,
        )
    assert fetch_calls == 0


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
