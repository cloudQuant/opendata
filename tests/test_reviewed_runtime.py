"""Exercise explicit reviewed runtime composition with owned SQLite fixtures.

These tests use only a synthetic JSON child operation and temporary databases.
They verify role checks, CLI queue/worker wiring, and caller-owned Engine
lifecycle; no provider or production service is contacted.
"""

from __future__ import annotations

import io
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event, select

from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from opendata.pipeline.reviewed_worker import ProcessFetchHandler, create_worker_engine
from opendata.pipeline.run_dispatch import (
    APSchedulerDispatchError,
    ReviewedTaskCatalog,
    RunDispatcher,
)
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RunLedger
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.runtime_roles import RuntimeRole, RuntimeRolePlan, build_runtime_role_plan
from opendata.reviewed_cli import ReviewedCLIApplication, run_reviewed_cli

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

DATASET_ID = "fixture-reviewed-runtime"
JOB_ID = "fixture-reviewed-runtime-daily"
WORKER_ID = "fixture-runtime-worker"
LEASE_TTL = timedelta(seconds=2)
HEARTBEAT_INTERVAL_SECONDS = 0.05
DATABASE_IO_TIMEOUT_SECONDS = 0.1

FACT_METADATA = MetaData()
FIXTURE_FACTS = Table(
    "fixture_runtime_facts",
    FACT_METADATA,
    Column("business_key", String(255), primary_key=True),
    Column("value", Integer, nullable=False),
)


class _TestResources:
    """Dispose every fixture Engine and deactivate runtimes after each test."""

    def __init__(self) -> None:
        """Start with no owned runtimes or Engines."""
        self.engines: list[Engine] = []
        self.runtimes: list[ReviewedRuntime] = []

    def own_engine(self, engine: Engine) -> Engine:
        """Register one test Engine for unconditional teardown."""
        self.engines.append(engine)
        return engine

    def sqlite_engine(self, path: Path) -> Engine:
        """Create and register one SQLite fixture Engine."""
        return self.own_engine(create_engine(f"sqlite:///{path}"))

    def worker_engine(self, path: Path) -> Engine:
        """Create and register one verified temporary SQLite worker Engine."""
        return self.own_engine(
            create_worker_engine(
                path,
                database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
            )
        )

    def runtime(
        self,
        engine: Engine,
        catalog: ReviewedTaskCatalog,
        plan: RuntimeRolePlan,
    ) -> ReviewedRuntime:
        """Build and register one runtime for unconditional deactivation."""
        runtime = build_reviewed_runtime(
            metadata_engine=engine,
            catalog=catalog,
            runtime_plan=plan,
        )
        self.runtimes.append(runtime)
        return runtime

    def close(self) -> None:
        """Close runtime adapters before disposing every caller-owned Engine."""
        for runtime in reversed(self.runtimes):
            runtime.close()
        for engine in reversed(self.engines):
            engine.dispose()


@pytest.fixture
def resources() -> Iterator[_TestResources]:
    """Ensure even failed assertions release temporary Engines and adapters."""
    owned = _TestResources()
    try:
        yield owned
    finally:
        owned.close()


def _plan(*roles: RuntimeRole) -> RuntimeRolePlan:
    """Build one reviewed fixture role plan with explicit sink ownership."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles=roles,
        reviewed_config_id="test.reviewed-runtime",
        reviewed_sink_ids=(DATASET_ID,),
    )


def _fixture_child(parameters: Mapping[str, JsonValue]) -> JsonValue:
    """Echo one checked integer through the real process executor protocol."""
    value = parameters.get("value")
    if type(value) is not int:
        raise ValueError("fixture_value_invalid")
    return {"value": value}


def _build_parameters(request: RunRequest) -> Mapping[str, JsonValue]:
    """Copy only one synthetic integer from the validated request query."""
    value = request.query.get("value")
    if type(value) is not int:
        raise ValueError("fixture_query_value_invalid")
    return {"value": value}


def _decode_result(value: JsonValue) -> object:
    """Pass the child's bounded JSON result to the fixture normalizer."""
    return value


def _normalize(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Convert one child result to an explicitly typed synthetic record."""
    del request, context
    if not isinstance(raw, dict):
        raise ValueError("fixture_result_invalid")
    value = raw.get("value")
    if type(value) is not int:
        raise ValueError("fixture_result_value_invalid")
    return NormalizedBatch(
        records=(
            NormalizedRecord(
                business_key="fixture:one",
                payload={"value": value},
                business_date=date(2026, 10, 27),
            ),
        ),
        schema_version="1",
        semantic_version="fixture-v1",
        unit="count",
        adjustment_basis="not_applicable",
        quality={"rejected_rows": 0},
        provenance={"source": "fixture"},
    )


def _publish(
    connection: Connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    """Publish only through the fenced connection supplied by RunService."""
    del request, idempotency_key
    record = batch.records[0]
    value = record.payload.get("value")
    if type(value) is not int:
        raise ValueError("fixture_publish_value_invalid")
    connection.execute(FIXTURE_FACTS.insert().values(business_key=record.business_key, value=value))
    return PublicationReceipt(
        publication_id="fixture-runtime-publication",
        dataset_id=task.dataset_id,
        record_count=1,
    )


def _catalog() -> ReviewedTaskCatalog:
    """Create one explicitly reviewed synthetic process-backed job."""
    task = TaskDefinition(
        job_id=JOB_ID,
        version="v1",
        dataset_id=DATASET_ID,
        provider="fixture-provider",
        model="fixture-model-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri"),
        enabled=True,
    )
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-runtime": _fixture_child}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    fetch = ProcessFetchHandler(
        executor,
        "fixture-runtime",
        _build_parameters,
        _decode_result,
        timeout_seconds=3.0,
    )
    return ReviewedTaskCatalog(
        [
            ReviewedJob(
                task=task,
                fetch=fetch,
                normalize=_normalize,
                publish=_publish,
                reviewed=True,
            )
        ]
    )


def _invoke(
    application: ReviewedCLIApplication,
    request: Mapping[str, JsonValue],
) -> tuple[int, dict[str, object], str]:
    """Call the strict CLI adapter and decode its bounded JSON receipt."""
    output = io.StringIO()
    errors = io.StringIO()
    status = run_reviewed_cli(
        ["--request", "-"],
        application=application,
        stdin=io.BytesIO(json.dumps(request, separators=(",", ":")).encode("utf-8")),
        stdout=output,
        stderr=errors,
    )
    receipt_text = output.getvalue() or errors.getvalue()
    parsed: object = json.loads(receipt_text)
    if not isinstance(parsed, dict):
        raise AssertionError("CLI response must be a JSON object")
    receipt: dict[str, object] = {}
    for key in parsed:
        if not isinstance(key, str):
            raise AssertionError("CLI response keys must be strings")
        receipt[key] = parsed[key]
    return status, receipt, receipt_text


def _worker_application(
    runtime: ReviewedRuntime,
    worker_engine: Engine,
    *,
    worker_id: str = WORKER_ID,
    lease_ttl: timedelta = LEASE_TTL,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    database_io_timeout_seconds: float = DATABASE_IO_TIMEOUT_SECONDS,
) -> ReviewedCLIApplication:
    """Adapt one worker runtime with an explicit, fixed test configuration."""
    return runtime.to_cli_application(
        worker_engine=worker_engine,
        worker_id=worker_id,
        lease_ttl=lease_ttl,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        database_io_timeout_seconds=database_io_timeout_seconds,
    )


def test_reviewed_plan_is_rejected_before_engine_access(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Reject the legacy profile without connecting to the injected Engine."""
    database_path = tmp_path / "plan-check.sqlite"
    engine = resources.sqlite_engine(database_path)
    connection_attempts: list[bool] = []
    event.listen(engine, "connect", lambda _connection, _record: connection_attempts.append(True))
    with pytest.raises(ValueError, match="explicit reviewed role plan"):
        build_reviewed_runtime(
            metadata_engine=engine,
            catalog=ReviewedTaskCatalog([]),
            runtime_plan=build_runtime_role_plan(),
        )
    assert connection_attempts == []
    assert not database_path.exists()


def test_factory_does_not_create_schema_or_connect(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Compose a reviewed runtime without bootstrapping its ledger schema."""
    database_path = tmp_path / "no-schema.sqlite"
    engine = resources.sqlite_engine(database_path)
    connection_attempts: list[bool] = []
    event.listen(engine, "connect", lambda _connection, _record: connection_attempts.append(True))
    runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("api"))
    assert isinstance(runtime.ledger, RunLedger)
    assert connection_attempts == []
    assert not database_path.exists()


def test_factory_accepts_reviewed_worker_and_scheduler_only_plans(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Keep valid process roles composable without auto-starting a scheduler."""
    database_path = tmp_path / "role-only.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    worker_engine = resources.worker_engine(database_path)
    worker_runtime = resources.runtime(worker_engine, ReviewedTaskCatalog([]), _plan("worker"))
    worker_application = _worker_application(worker_runtime, worker_engine)
    assert worker_application.runtime_plan is worker_runtime.runtime_plan
    assert worker_application.dispatcher is None
    assert worker_application.worker_once is not None

    scheduler_runtime = resources.runtime(
        worker_engine,
        ReviewedTaskCatalog([]),
        _plan("scheduler"),
    )
    scheduler_application = scheduler_runtime.to_cli_application()
    assert scheduler_application.dispatcher is scheduler_runtime.dispatcher
    assert scheduler_application.worker_once is None
    assert isinstance(worker_runtime, ReviewedRuntime)
    assert isinstance(worker_runtime.ledger, RunLedger)
    assert isinstance(worker_runtime.dispatcher, RunDispatcher)
    with pytest.raises(ValueError, match="role 'api' is not enabled"):
        scheduler_runtime.dispatcher.dispatch_manual(
            JOB_ID,
            "v1",
            {"idempotency_key": "role-gate"},
        )
    with pytest.raises(ValueError, match="different settings"):
        _worker_application(worker_runtime, worker_engine, worker_id="different-worker")
    with pytest.raises(ValueError, match="different settings"):
        _worker_application(worker_runtime, worker_engine, lease_ttl=timedelta(seconds=3))


def test_dispatch_roles_and_closed_callbacks_are_enforced(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Gate API and scheduler operations by role and runtime lifecycle."""
    engine = resources.sqlite_engine(tmp_path / "role-dry-run.sqlite")
    catalog = _catalog()
    api_runtime = resources.runtime(engine, catalog, _plan("api"))
    manual = api_runtime.dispatcher.dispatch_manual(
        JOB_ID,
        "v1",
        {"idempotency_key": "runtime-role-manual"},
        dry_run=True,
    )
    backfill = api_runtime.dispatcher.dispatch_backfill(
        JOB_ID,
        "v1",
        {
            "idempotency_key": "runtime-role-backfill",
            "backfill_start": date(2026, 10, 1),
            "backfill_end": date(2026, 10, 27),
        },
        dry_run=True,
    )
    assert manual.status == "dry_run"
    assert backfill.status == "dry_run"
    with pytest.raises(ValueError, match="role 'scheduler' is not enabled"):
        api_runtime.dispatcher.dispatch_scheduled(
            JOB_ID,
            "v1",
            occurrence=datetime(2026, 10, 27, 16, tzinfo=timezone.utc),
            dry_run=True,
        )
    with pytest.raises(ValueError, match="role 'scheduler' is not enabled"):
        api_runtime.dispatcher.apscheduler_callback(JOB_ID, "v1")

    scheduler_runtime = resources.runtime(engine, catalog, _plan("scheduler"))
    scheduled = scheduler_runtime.dispatcher.dispatch_scheduled(
        JOB_ID,
        "v1",
        occurrence=datetime(2026, 10, 27, 16, tzinfo=timezone.utc),
        dry_run=True,
    )
    callback = scheduler_runtime.dispatcher.apscheduler_callback(JOB_ID, "v1")
    assert scheduled.status == "dry_run"

    api_runtime.close()
    scheduler_runtime.close()
    with pytest.raises(RuntimeError, match="runtime is closed"):
        api_runtime.dispatcher.dispatch_manual(
            JOB_ID,
            "v1",
            {"idempotency_key": "runtime-closed-manual"},
            dry_run=True,
        )
    with pytest.raises(RuntimeError, match="runtime is closed"):
        api_runtime.dispatcher.dispatch_backfill(
            JOB_ID,
            "v1",
            {
                "idempotency_key": "runtime-closed-backfill",
                "backfill_start": date(2026, 10, 1),
                "backfill_end": date(2026, 10, 27),
            },
            dry_run=True,
        )
    with pytest.raises(RuntimeError, match="runtime is closed"):
        scheduler_runtime.dispatcher.dispatch_scheduled(
            JOB_ID,
            "v1",
            occurrence=datetime(2026, 10, 27, 16, tzinfo=timezone.utc),
            dry_run=True,
        )
    with pytest.raises(RuntimeError, match="runtime is closed"):
        scheduler_runtime.dispatcher.apscheduler_callback(JOB_ID, "v1")

    from apscheduler.events import EVENT_JOB_SUBMITTED

    submission = SimpleNamespace(
        code=EVENT_JOB_SUBMITTED,
        job_id=callback.scheduler_job_id,
        scheduled_run_times=[
            datetime(2026, 10, 27, 16, tzinfo=ZoneInfo("UTC")),
        ],
    )
    with pytest.raises(APSchedulerDispatchError, match="scheduled_event_dispatch_failed"):
        callback(submission)


def test_worker_role_rejects_missing_and_unexpected_options(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Require fixed worker settings only when the worker role is enabled."""
    database_path = tmp_path / "worker-options.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    engine = resources.worker_engine(database_path)
    worker_runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("worker"))
    with pytest.raises(ValueError, match="all fixed worker options"):
        worker_runtime.to_cli_application()
    with pytest.raises(ValueError, match="all fixed worker options"):
        worker_runtime.to_cli_application(worker_engine=engine)
    with pytest.raises(ValueError, match="all fixed worker options"):
        worker_runtime.to_cli_application(
            worker_engine=engine,
            worker_id=WORKER_ID,
            lease_ttl=LEASE_TTL,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        )

    api_runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("api"))
    with pytest.raises(ValueError, match="worker options require"):
        api_runtime.to_cli_application(worker_id=WORKER_ID)
    api_runtime.close()
    with pytest.raises(RuntimeError, match="runtime is closed"):
        api_runtime.to_cli_application(worker_id=WORKER_ID)


@pytest.mark.parametrize("worker_id", ["", "worker with spaces", "w" * 256])
def test_worker_identity_is_bounded(
    tmp_path: Path,
    resources: _TestResources,
    worker_id: str,
) -> None:
    """Reject empty, unsafe, and overlong identities before worker binding."""
    database_path = tmp_path / "worker-id.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    engine = resources.worker_engine(database_path)
    runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("worker"))
    with pytest.raises(ValueError, match="CLI-safe identifier"):
        _worker_application(runtime, engine, worker_id=worker_id)


@pytest.mark.parametrize(
    "lease_ttl",
    [
        timedelta(0),
        timedelta(milliseconds=500),
        timedelta(seconds=1, microseconds=1),
        timedelta(seconds=86_401),
        timedelta.max,
    ],
)
def test_worker_lease_ttl_is_bounded_and_whole_seconds(
    tmp_path: Path,
    resources: _TestResources,
    lease_ttl: timedelta,
) -> None:
    """Reject leases outside ledger bounds or with fractional seconds."""
    database_path = tmp_path / "worker-ttl.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    engine = resources.worker_engine(database_path)
    runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("worker"))
    with pytest.raises(ValueError, match="whole number of seconds"):
        _worker_application(runtime, engine, lease_ttl=lease_ttl)


@pytest.mark.parametrize(
    "value",
    [True, float("nan"), float("inf"), -0.01, 60.01, 10**1000],
)
def test_worker_heartbeat_interval_rejects_invalid_bounds(
    tmp_path: Path,
    resources: _TestResources,
    value: float,
) -> None:
    """Reject booleans, non-finite, overflow, and out-of-bound heartbeats."""
    database_path = tmp_path / "heartbeat-bound.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    engine = resources.worker_engine(database_path)
    runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("worker"))
    with pytest.raises(ValueError, match="heartbeat_interval_seconds"):
        _worker_application(runtime, engine, heartbeat_interval_seconds=value)


@pytest.mark.parametrize(
    "value",
    [True, float("nan"), float("inf"), -0.01, 30.01, 10**1000],
)
def test_worker_database_io_timeout_rejects_invalid_bounds(
    tmp_path: Path,
    resources: _TestResources,
    value: float,
) -> None:
    """Reject booleans, non-finite, overflow, and unbounded I/O timeouts."""
    database_path = tmp_path / "io-timeout-bound.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    engine = resources.worker_engine(database_path)
    runtime = resources.runtime(engine, ReviewedTaskCatalog([]), _plan("worker"))
    with pytest.raises(ValueError, match="database_io_timeout_seconds"):
        _worker_application(runtime, engine, database_io_timeout_seconds=value)


def test_worker_engine_must_be_the_exact_ledger_engine(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Reject a worker target mismatch before constructing the worker SPI."""
    metadata_engine = resources.sqlite_engine(tmp_path / "runtime-ledger.sqlite")
    other_engine = resources.sqlite_engine(tmp_path / "other-ledger.sqlite")
    connection_attempts: list[bool] = []
    event.listen(
        metadata_engine,
        "connect",
        lambda _connection, _record: connection_attempts.append(True),
    )
    event.listen(
        other_engine,
        "connect",
        lambda _connection, _record: connection_attempts.append(True),
    )
    runtime = resources.runtime(
        metadata_engine,
        ReviewedTaskCatalog([]),
        _plan("worker"),
    )
    with pytest.raises(ValueError, match="exact Engine bound to the run ledger"):
        runtime.to_cli_application(
            worker_engine=other_engine,
            worker_id=WORKER_ID,
            lease_ttl=LEASE_TTL,
            heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
            database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
        )
    assert connection_attempts == []


def test_cli_queues_and_executes_one_reviewed_run_with_borrowed_engines(
    tmp_path: Path,
    resources: _TestResources,
) -> None:
    """Prove strict CLI, dispatcher, durable ledger, process worker, and write."""
    database_path = tmp_path / "reviewed-runtime.sqlite"
    bootstrap_engine = resources.sqlite_engine(database_path)
    RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    FACT_METADATA.create_all(bootstrap_engine)
    metadata_engine = resources.worker_engine(database_path)
    worker_engine = metadata_engine
    runtime = resources.runtime(metadata_engine, _catalog(), _plan("api", "worker"))
    application = _worker_application(runtime, worker_engine)
    reused_application = _worker_application(runtime, worker_engine)
    manual_request: dict[str, JsonValue] = {
        "schema_version": 1,
        "operation": "manual",
        "job_id": JOB_ID,
        "version": "v1",
        "payload": {"idempotency_key": "runtime-run-001", "query": {"value": 37}},
    }
    worker_request: dict[str, JsonValue] = {
        "schema_version": 1,
        "operation": "worker_once",
        "worker": WORKER_ID,
        "lease_seconds": int(LEASE_TTL.total_seconds()),
    }

    submit_code, submitted, _submit_text = _invoke(application, manual_request)
    assert submit_code == 0
    assert submitted["status"] == "queued"
    run_key = submitted.get("run_key")
    assert isinstance(run_key, str)

    worker_code, completed, _worker_text = _invoke(reused_application, worker_request)
    assert worker_code == 0
    assert completed == {"status": "succeeded", "run_key": run_key}

    record = runtime.ledger.get(run_key)
    assert record is not None and record.status == "succeeded"
    with metadata_engine.connect() as connection:
        published_value = connection.execute(select(FIXTURE_FACTS.c.value)).scalar_one()
    assert published_value == 37

    idle_code, idle, _idle_text = _invoke(application, worker_request)
    assert idle_code == 0 and idle == {"status": "idle"}
    with pytest.raises(ValueError, match="different settings"):
        _worker_application(runtime, worker_engine, worker_id="different-worker")
    assert application.worker_once is not None
    with pytest.raises(ValueError, match="fixed runtime settings"):
        application.worker_once("different-worker", LEASE_TTL)
    with pytest.raises(ValueError, match="fixed runtime settings"):
        application.worker_once(WORKER_ID, timedelta(seconds=3))

    runtime.close()
    assert runtime.closed is True
    with pytest.raises(RuntimeError, match="runtime is closed"):
        _worker_application(runtime, worker_engine)
    assert application.worker_once is not None
    with pytest.raises(ValueError, match="runtime is closed"):
        application.worker_once(WORKER_ID, LEASE_TTL)
    with pytest.raises(RuntimeError, match="runtime is closed"):
        runtime.dispatcher.dispatch_manual(
            JOB_ID,
            "v1",
            {"idempotency_key": "runtime-run-after-close"},
        )
    with metadata_engine.connect() as connection:
        assert connection.execute(select(1)).scalar_one() == 1
    with worker_engine.connect() as connection:
        assert connection.execute(select(1)).scalar_one() == 1
