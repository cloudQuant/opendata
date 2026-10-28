"""Exercise the reviewed runtime adapters for isolated one-shot roles."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event

from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from opendata.pipeline.reviewed_worker import (
    PersistentRunWorker,
    ProcessFetchHandler,
    create_worker_engine,
)
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
from opendata.pipeline.run_service import NormalizedBatch, PublicationReceipt, ReviewedJob
from opendata.pipeline.runtime_roles import (
    RuntimeRole,
    RuntimeRolePlan,
    build_runtime_role_plan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

    from opendata.reviewed_cli import ReviewedCLIApplication

DATASET_ID = "tests.reviewed-runtime-one-shot-cli"
ONE_SHOT_JOB_ID = "tests-reviewed-one-shot"
WORKER_ID = "tests-reviewed-one-shot-executor"
LEASE_TTL = timedelta(seconds=30)
HEARTBEAT_INTERVAL_SECONDS = 0.1
DATABASE_IO_TIMEOUT_SECONDS = 0.2


def _fixture_process_operation(parameters: JsonValue) -> JsonValue:
    """Return one harmless normalized JSON value from the isolated child."""
    if not isinstance(parameters, dict):
        raise ValueError("fixture_process_parameters_invalid")
    symbol = parameters.get("symbol")
    if not isinstance(symbol, str):
        raise ValueError("fixture_process_symbol_invalid")
    return {"symbol": symbol}


def _build_process_parameters(request: RunRequest) -> Mapping[str, JsonValue]:
    """Copy one checked fixture symbol into the process protocol payload."""
    symbol = request.query.get("symbol")
    if not isinstance(symbol, str):
        raise ValueError("fixture_request_symbol_invalid")
    return {"symbol": symbol}


def _decode_process_result(value: JsonValue) -> object:
    """Pass the bounded synthetic child JSON to the task normalizer."""
    return value


def _process_fetch_handler() -> ProcessFetchHandler:
    """Build the process-bounded fetch handler required by persistent workers."""
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"fixture-one-shot-runtime": _fixture_process_operation}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    return ProcessFetchHandler(
        executor,
        "fixture-one-shot-runtime",
        _build_process_parameters,
        _decode_process_result,
        timeout_seconds=2.0,
    )


def _plan(role: RuntimeRole, *, sink_id: str = DATASET_ID) -> RuntimeRolePlan:
    """Build one dedicated reviewed one-shot process plan."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles={role},
        reviewed_config_id="tests.reviewed-runtime-one-shot-cli",
        reviewed_sink_ids={sink_id},
    )


def _job(dataset_id: str = DATASET_ID) -> ReviewedJob:
    """Create a disabled, schedule-free synthetic one-shot task."""
    task = TaskDefinition(
        job_id=ONE_SHOT_JOB_ID,
        version="v1",
        dataset_id=dataset_id,
        provider="fixture-provider",
        model="fixture-model",
        enabled=False,
        schedule=None,
    )

    def normalize(_raw: object, _request: RunRequest, _context: object) -> NormalizedBatch:
        raise AssertionError("runtime construction and submission must not normalize data")

    def publish(
        _connection: Connection,
        _task: TaskDefinition,
        _request: RunRequest,
        _batch: NormalizedBatch,
        _idempotency_key: str,
    ) -> PublicationReceipt:
        raise AssertionError("runtime construction and submission must not publish data")

    return ReviewedJob(
        task=task,
        fetch=_process_fetch_handler(),
        normalize=normalize,
        publish=publish,
        reviewed=True,
        one_shot_only=True,
    )


def _runtime(
    engine: Engine,
    role: RuntimeRole,
    *,
    dataset_id: str = DATASET_ID,
) -> ReviewedRuntime:
    """Compose a synthetic runtime against a caller-owned Engine."""
    return build_reviewed_runtime(
        metadata_engine=engine,
        catalog=ReviewedTaskCatalog([_job(dataset_id)]),
        runtime_plan=_plan(role, sink_id=DATASET_ID),
    )


def _worker_engine(tmp_path: Path) -> Engine:
    """Create a verified worker Engine with only a private temporary ledger."""
    database_path = tmp_path / "one-shot-runtime.sqlite"
    bootstrap_engine = create_engine(f"sqlite:///{database_path}")
    try:
        RUN_LEDGER_METADATA.create_all(bootstrap_engine)
    finally:
        bootstrap_engine.dispose()
    return create_worker_engine(
        database_path,
        database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
    )


def _worker_application(runtime: ReviewedRuntime, engine: Engine) -> ReviewedCLIApplication:
    """Bind one worker through fixed and explicit runtime options."""
    return runtime.to_cli_application(
        worker_engine=engine,
        worker_id=WORKER_ID,
        lease_ttl=LEASE_TTL,
        heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
        database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
    )


def test_submitter_runtime_exposes_only_one_shot_dispatch(tmp_path: Path) -> None:
    """A submitter can enqueue one-shot work without gaining broad API routes."""
    engine = create_engine(f"sqlite:///{tmp_path / 'submitter.sqlite'}")
    RUN_LEDGER_METADATA.create_all(engine)
    runtime = _runtime(engine, "one_shot_submitter")
    try:
        application = runtime.to_cli_application()
        dispatcher = application.dispatcher
        assert dispatcher is runtime.dispatcher
        assert dispatcher is not None
        assert application.worker_once is None
        assert application.one_shot_run is None

        receipt = dispatcher.dispatch_one_shot(
            ONE_SHOT_JOB_ID,
            "v1",
            {"idempotency_key": "submitter-runtime-request", "query": {"symbol": "FIXTURE"}},
        )
        record = runtime.ledger.get(receipt.identity.run_key)
        assert receipt.status == "queued"
        assert record is not None
        assert record.claim_policy == "one_shot"

        with pytest.raises(ValueError, match="role 'api' is not enabled"):
            dispatcher.dispatch_manual(
                ONE_SHOT_JOB_ID,
                "v1",
                {"idempotency_key": "submitter-manual-request"},
            )
        with pytest.raises(ValueError, match="role 'scheduler' is not enabled"):
            dispatcher.dispatch_scheduled(
                ONE_SHOT_JOB_ID,
                "v1",
                occurrence=datetime(2026, 10, 29, tzinfo=timezone.utc),
            )
        with pytest.raises(ValueError, match="role 'api' is not enabled"):
            dispatcher.dispatch_backfill(
                ONE_SHOT_JOB_ID,
                "v1",
                {
                    "idempotency_key": "submitter-backfill-request",
                    "backfill_start": "2026-10-01",
                    "backfill_end": "2026-10-29",
                },
            )
        with pytest.raises(ValueError, match="role 'scheduler' is not enabled"):
            dispatcher.apscheduler_callback(ONE_SHOT_JOB_ID, "v1")
        assert runtime.ledger.get("submitter-manual-request") is None
        assert runtime.ledger.get("submitter-backfill-request") is None
    finally:
        runtime.close()
        engine.dispose()


def test_submitter_runtime_validates_catalog_sink_before_engine_access(tmp_path: Path) -> None:
    """Reject a one-shot submitter catalog outside its declared reviewed sinks."""
    engine = create_engine(f"sqlite:///{tmp_path / 'sink-mismatch.sqlite'}")
    connection_attempts: list[bool] = []
    event.listen(engine, "connect", lambda *_args: connection_attempts.append(True))
    try:
        with pytest.raises(ValueError, match="sink ids not declared"):
            _runtime(engine, "one_shot_submitter", dataset_id="unreviewed.sink")
        assert connection_attempts == []
    finally:
        engine.dispose()


def test_executor_adapter_calls_only_exact_key_worker_method(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bind exact-key execution and reject attempts to call broad run_once."""
    engine = _worker_engine(tmp_path)
    runtime = _runtime(engine, "one_shot_executor")
    requested_keys: list[str] = []

    def run_one_shot(_worker: PersistentRunWorker, run_key: str) -> None:
        requested_keys.append(run_key)
        return None

    def run_once(_worker: PersistentRunWorker) -> None:
        raise AssertionError("one-shot executor must never claim the broad queue")

    monkeypatch.setattr(PersistentRunWorker, "run_one_shot", run_one_shot)
    monkeypatch.setattr(PersistentRunWorker, "run_once", run_once)
    try:
        application = _worker_application(runtime, engine)
        assert application.dispatcher is None
        assert application.worker_once is None
        assert callable(application.one_shot_run)
        run_key = "a" * 64
        assert application.one_shot_run(run_key) is None
        assert requested_keys == [run_key]
        with pytest.raises(ValueError, match="64 lowercase hexadecimal"):
            application.one_shot_run("A" * 64)
        assert requested_keys == [run_key]

        same_binding = runtime._lifecycle.worker
        assert same_binding is not None
        reused = _worker_application(runtime, engine)
        assert reused.one_shot_run is not application.one_shot_run
        assert runtime._lifecycle.worker is same_binding
        with pytest.raises(ValueError, match="different settings"):
            runtime.to_cli_application(
                worker_engine=engine,
                worker_id="different-worker",
                lease_ttl=LEASE_TTL,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
            )
    finally:
        runtime.close()
        engine.dispose()


def test_executor_adapter_rejects_reentry_and_closed_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep one-shot execution serialized and deactivate its adapter on close."""
    engine = _worker_engine(tmp_path)
    runtime = _runtime(engine, "one_shot_executor")
    application_holder: list[ReviewedCLIApplication] = []
    nested_errors: list[Exception] = []

    def run_one_shot(_worker: PersistentRunWorker, _run_key: str) -> None:
        application = application_holder[0]
        try:
            if application.one_shot_run is None:
                raise AssertionError("one-shot executor SPI was not injected")
            application.one_shot_run("b" * 64)
        except Exception as error:
            nested_errors.append(error)
        runtime.close()
        return None

    monkeypatch.setattr(PersistentRunWorker, "run_one_shot", run_one_shot)
    try:
        application = _worker_application(runtime, engine)
        application_holder.append(application)
        assert application.one_shot_run("c" * 64) is None
        assert len(nested_errors) == 1
        assert isinstance(nested_errors[0], RuntimeError)
        assert str(nested_errors[0]) == "reviewed runtime worker is already running"
        assert runtime.closed is True
        with pytest.raises(ValueError, match="runtime is closed"):
            application.one_shot_run("d" * 64)
    finally:
        runtime.close()
        engine.dispose()


def test_executor_requires_the_exact_ledger_engine(tmp_path: Path) -> None:
    """Do not let an exact-key adapter read a different reviewed ledger."""
    engine = _worker_engine(tmp_path)
    other_engine = create_engine(f"sqlite:///{tmp_path / 'other-ledger.sqlite'}")
    runtime = _runtime(engine, "one_shot_executor")
    try:
        with pytest.raises(ValueError, match="exact Engine bound to the run ledger"):
            runtime.to_cli_application(
                worker_engine=other_engine,
                worker_id=WORKER_ID,
                lease_ttl=LEASE_TTL,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
            )
    finally:
        runtime.close()
        engine.dispose()
        other_engine.dispose()
