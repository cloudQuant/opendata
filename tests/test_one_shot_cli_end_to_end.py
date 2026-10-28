"""Exercise the dedicated one-shot CLI roles against owned SQLite state."""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, cast

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, func, select

from opendata.pipeline.process_executor import ReviewedProcessExecutor, ReviewedProcessRegistry
from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from opendata.pipeline.reviewed_worker import ProcessFetchHandler, create_worker_engine
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
from opendata.pipeline.run_identity import RunRequest, TaskDefinition
from opendata.pipeline.run_ledger import (
    RUN_LEDGER_METADATA,
    RUN_LEDGER_TABLE,
    RunLedger,
)
from opendata.pipeline.run_service import (
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
    RunOutcome,
)
from opendata.pipeline.runtime_roles import (
    RuntimeRole,
    RuntimeRolePlan,
    build_runtime_role_plan,
)
from opendata.reviewed_cli import ReviewedCLIApplication, run_reviewed_cli

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from pathlib import Path

    from pydantic import JsonValue
    from sqlalchemy.engine import Connection, Engine

UTC = timezone.utc
DATASET_ID = "fixture.one-shot-cli"
JOB_ID = "fixture-one-shot-cli"
TRADE_DATE = date(2026, 10, 28)
LEASE_TTL = timedelta(seconds=15)
HEARTBEAT_INTERVAL_SECONDS = 0.05
DATABASE_IO_TIMEOUT_SECONDS = 0.1
PUBLICATION_METADATA = MetaData()
SYNTHETIC_PUBLICATIONS = Table(
    "fixture_one_shot_cli_publications",
    PUBLICATION_METADATA,
    Column("run_key", String(64), primary_key=True),
    Column("publication_id", String(128), nullable=False),
    Column("dataset_id", String(128), nullable=False),
    Column("record_count", Integer, nullable=False),
)


def _synthetic_fetch(parameters: Mapping[str, JsonValue]) -> JsonValue:
    """Return deterministic local data from the registered child operation."""
    instrument_id = parameters.get("instrument_id")
    if not isinstance(instrument_id, str):
        raise ValueError("synthetic instrument is invalid")
    return {"instrument_id": instrument_id, "close": 12.5}


@dataclass(slots=True)
class _HandlerCalls:
    """Record observable synthetic handler activity for exact-once assertions."""

    fetch: list[str] = field(default_factory=list)
    publish: list[str] = field(default_factory=list)


def _make_job(calls: _HandlerCalls) -> ReviewedJob:
    """Build one disabled, unscheduled one-shot job with local handlers only."""
    task = TaskDefinition(
        job_id=JOB_ID,
        version="v1",
        dataset_id=DATASET_ID,
        provider="synthetic-provider",
        model="synthetic-daily",
        timezone="UTC",
        schedule=None,
        enabled=False,
    )
    executor = ReviewedProcessExecutor(
        ReviewedProcessRegistry({"synthetic-one-shot": _synthetic_fetch}),
        poll_interval_seconds=0.01,
        checkpoint_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
    )
    fetch = ProcessFetchHandler(
        executor,
        "synthetic-one-shot",
        lambda request: dict(request.query),
        lambda value: value,
        timeout_seconds=5.0,
    )

    def normalize(
        raw: object,
        _request: RunRequest,
        context: RunExecutionContext,
    ) -> NormalizedBatch:
        if not isinstance(raw, dict):
            raise ValueError("synthetic result is invalid")
        instrument_id = raw.get("instrument_id")
        close = raw.get("close")
        if not isinstance(instrument_id, str) or not isinstance(close, float | int):
            raise ValueError("synthetic result is invalid")
        calls.fetch.append(context.run_key)
        return NormalizedBatch(
            records=(
                NormalizedRecord(
                    business_key=f"{instrument_id}:{TRADE_DATE.isoformat()}",
                    payload={"instrument_id": instrument_id, "close": close},
                    business_date=TRADE_DATE,
                ),
            ),
            schema_version="1",
            semantic_version="synthetic-v1",
            unit="CNY/share",
            adjustment_basis="raw",
            quality={"synthetic": True, "rejected_rows": 0},
            provenance={"source": "synthetic-test"},
        )

    def publish(
        connection: Connection,
        current_task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        run_key: str,
    ) -> PublicationReceipt:
        calls.publish.append(run_key)
        publication_id = f"synthetic-publication-{run_key[:16]}"
        connection.execute(
            SYNTHETIC_PUBLICATIONS.insert().values(
                run_key=run_key,
                publication_id=publication_id,
                dataset_id=current_task.dataset_id,
                record_count=len(batch.records),
            )
        )
        return PublicationReceipt(
            publication_id=publication_id,
            dataset_id=current_task.dataset_id,
            record_count=len(batch.records),
        )

    return ReviewedJob(
        task=task,
        fetch=fetch,
        normalize=normalize,
        publish=publish,
        reviewed=True,
        one_shot_only=True,
    )


@dataclass(slots=True)
class _CLIHarness:
    """Own one temporary database, its synthetic catalog, and runtimes."""

    engine: Engine
    catalog: ReviewedTaskCatalog
    calls: _HandlerCalls
    runtimes: list[ReviewedRuntime] = field(default_factory=list)

    def application(
        self,
        role: RuntimeRole,
        *,
        worker_id: str | None = None,
    ) -> tuple[ReviewedRuntime, ReviewedCLIApplication]:
        """Compose one runtime for exactly one role and its strict CLI adapter."""
        plan = build_runtime_role_plan(
            profile="reviewed",
            roles=(role,),
            reviewed_config_id=f"tests.one-shot-cli.{role}",
            reviewed_sink_ids=(DATASET_ID,),
        )
        runtime = build_reviewed_runtime(
            metadata_engine=self.engine,
            catalog=self.catalog,
            runtime_plan=plan,
        )
        self.runtimes.append(runtime)
        if role in {"worker", "one_shot_executor"}:
            if worker_id is None:
                raise ValueError("worker roles require a fixed worker id")
            application = runtime.to_cli_application(
                worker_engine=self.engine,
                worker_id=worker_id,
                lease_ttl=LEASE_TTL,
                heartbeat_interval_seconds=HEARTBEAT_INTERVAL_SECONDS,
                database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
            )
        else:
            application = runtime.to_cli_application()
        return runtime, application

    def close(self) -> None:
        """Deactivate adapters before disposing the fixture-owned Engine."""
        for runtime in reversed(self.runtimes):
            runtime.close()
        self.engine.dispose()


@pytest.fixture
def cli_harness(tmp_path: Path) -> Iterator[_CLIHarness]:
    """Provide a worker-verified temporary SQLite file and synthetic job."""
    database_path = tmp_path / "one-shot-cli.sqlite"
    database_path.touch()
    bootstrap_engine = create_engine(f"sqlite:///{database_path}")
    try:
        RUN_LEDGER_METADATA.create_all(bootstrap_engine)
        PUBLICATION_METADATA.create_all(bootstrap_engine)
    finally:
        bootstrap_engine.dispose()

    engine = create_worker_engine(
        database_path,
        database_io_timeout_seconds=DATABASE_IO_TIMEOUT_SECONDS,
    )
    calls = _HandlerCalls()
    harness = _CLIHarness(
        engine=engine,
        catalog=ReviewedTaskCatalog([_make_job(calls)]),
        calls=calls,
    )
    try:
        yield harness
    finally:
        harness.close()


def _invoke(
    application: ReviewedCLIApplication,
    request: Mapping[str, object],
) -> tuple[int, dict[str, object], str]:
    """Call the injected CLI in memory and return its sanitized JSON result."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    request_bytes = json.dumps(dict(request), separators=(",", ":")).encode("utf-8")
    exit_code = run_reviewed_cli(
        ["--request", "-"],
        application=application,
        stdin=io.BytesIO(request_bytes),
        stdout=stdout,
        stderr=stderr,
    )
    output = stdout.getvalue() or stderr.getvalue()
    decoded = json.loads(output)
    if not isinstance(decoded, dict):
        raise AssertionError("reviewed CLI did not return a JSON object")
    return exit_code, cast("dict[str, object]", decoded), output


def _submission_request(idempotency_key: str, *, dry_run: bool = False) -> dict[str, object]:
    """Return one bounded synthetic manual submission request."""
    return {
        "schema_version": 1,
        "operation": "one_shot_submit",
        "job_id": JOB_ID,
        "version": "v1",
        "payload": {
            "idempotency_key": idempotency_key,
            "query": {"instrument_id": "SYNTHETIC:DO_NOT_ECHO"},
            "partition": {},
        },
        "dry_run": dry_run,
    }


def _run_request(run_key: str, *, dry_run: bool = False) -> dict[str, object]:
    """Return one exact-key execution request without worker-controlled values."""
    return {
        "schema_version": 1,
        "operation": "one_shot_run",
        "run_key": run_key,
        "dry_run": dry_run,
    }


def _row_count(engine: Engine) -> int:
    """Count rows in the private run ledger used by this fixture."""
    with engine.connect() as connection:
        return int(
            connection.execute(select(func.count()).select_from(RUN_LEDGER_TABLE)).scalar_one()
        )


def test_one_shot_cli_roles_are_strictly_mutually_exclusive() -> None:
    """Prevent dedicated submitter and executor roles from overlapping roles."""
    for roles in (
        {"one_shot_submitter", "one_shot_executor"},
        {"one_shot_submitter", "api"},
        {"one_shot_executor", "worker"},
    ):
        with pytest.raises(
            ValueError,
            match="one-shot runtime roles must run as a dedicated process role",
        ):
            build_runtime_role_plan(
                profile="reviewed",
                roles=roles,
                reviewed_config_id="tests.one-shot-cli.invalid-role-combination",
                reviewed_sink_ids=(DATASET_ID,),
            )


def test_one_shot_cli_dry_run_ordinary_worker_and_exact_execution(
    cli_harness: _CLIHarness,
) -> None:
    """Submit, leave broad workers idle, then publish one exact run once."""
    submit_runtime, submitter = cli_harness.application("one_shot_submitter")
    assert submitter.worker_once is None
    assert submitter.one_shot_run is None
    assert submitter.dispatcher is not None
    dry_exit, dry_result, dry_output = _invoke(
        submitter,
        _submission_request("dry-run-request", dry_run=True),
    )
    dry_run_key = dry_result.get("run_key")
    assert dry_exit == 0
    assert dry_result["status"] == "dry_run"
    assert isinstance(dry_run_key, str) and len(dry_run_key) == 64
    assert all(character in "0123456789abcdef" for character in dry_run_key)
    assert _row_count(cli_harness.engine) == 0
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    assert "SYNTHETIC:DO_NOT_ECHO" not in dry_output

    request = _submission_request("fixture-one-shot-request")
    submit_exit, submit_result, submit_output = _invoke(submitter, request)
    run_key = submit_result.get("run_key")
    assert submit_exit == 0
    assert submit_result["status"] == "queued"
    assert isinstance(run_key, str) and len(run_key) == 64
    assert all(character in "0123456789abcdef" for character in run_key)
    queued = submit_runtime.ledger.get(run_key)
    assert queued is not None
    assert queued.status == "queued"
    assert queued.claim_policy == "one_shot"
    assert _row_count(cli_harness.engine) == 1
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    assert "SYNTHETIC:DO_NOT_ECHO" not in submit_output

    replay_exit, replay_result, _replay_output = _invoke(submitter, request)
    assert replay_exit == 0
    assert replay_result["run_key"] == run_key
    assert _row_count(cli_harness.engine) == 1

    ordinary_runtime, ordinary_worker = cli_harness.application(
        "worker",
        worker_id="synthetic-ordinary-worker",
    )
    ordinary_exit, ordinary_result, _ordinary_output = _invoke(
        ordinary_worker,
        {
            "schema_version": 1,
            "operation": "worker_once",
            "worker": "synthetic-ordinary-worker",
            "lease_seconds": int(LEASE_TTL.total_seconds()),
        },
    )
    assert ordinary_exit == 0
    assert ordinary_result == {"status": "idle"}
    assert submit_runtime.ledger.get(run_key) == queued
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    ordinary_runtime.close()

    _executor_runtime, executor = cli_harness.application(
        "one_shot_executor",
        worker_id="synthetic-one-shot-worker",
    )
    assert executor.worker_once is None
    assert callable(executor.one_shot_run)
    assert executor.dispatcher is None
    dry_run_exit, dry_run_result, _dry_run_output = _invoke(
        executor,
        _run_request(run_key, dry_run=True),
    )
    assert dry_run_exit == 0
    assert dry_run_result == {"status": "dry_run", "run_key": run_key}
    assert submit_runtime.ledger.get(run_key) == queued
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []

    execute_exit, execute_result, execute_output = _invoke(executor, _run_request(run_key))
    assert execute_exit == 0
    assert execute_result == {"status": "succeeded", "run_key": run_key}
    assert "SYNTHETIC:DO_NOT_ECHO" not in execute_output
    completed = submit_runtime.ledger.get(run_key)
    assert completed is not None and completed.status == "succeeded"
    assert cli_harness.calls.fetch == [run_key]
    assert cli_harness.calls.publish == [run_key]

    with cli_harness.engine.connect() as connection:
        publication = connection.execute(
            select(
                SYNTHETIC_PUBLICATIONS.c.run_key,
                SYNTHETIC_PUBLICATIONS.c.publication_id,
                SYNTHETIC_PUBLICATIONS.c.dataset_id,
                SYNTHETIC_PUBLICATIONS.c.record_count,
            )
        ).one()
    assert publication == (
        run_key,
        f"synthetic-publication-{run_key[:16]}",
        DATASET_ID,
        1,
    )

    repeated_exit, repeated_result, _repeated_output = _invoke(
        executor,
        _run_request(run_key),
    )
    assert repeated_exit == 1
    assert repeated_result == {
        "code": "one_shot_not_claimed",
        "run_key": run_key,
        "status": "not_claimed",
    }
    assert submit_runtime.ledger.get(run_key) == completed
    assert cli_harness.calls.fetch == [run_key]
    assert cli_harness.calls.publish == [run_key]


def test_one_shot_cli_rejects_wrong_roles_and_malformed_run_keys(
    cli_harness: _CLIHarness,
) -> None:
    """Reject cross-role commands and malformed keys without leaking payloads."""
    _submit_runtime, submitter = cli_harness.application("one_shot_submitter")
    _executor_runtime, executor = cli_harness.application(
        "one_shot_executor",
        worker_id="synthetic-one-shot-worker",
    )
    _submit_code, submitted, _submit_output = _invoke(
        submitter,
        _submission_request("wrong-role-fixture"),
    )
    run_key = submitted.get("run_key")
    assert isinstance(run_key, str)
    before_rows = _row_count(cli_harness.engine)

    run_exit, run_result, run_output = _invoke(submitter, _run_request(run_key))
    submit_exit, submit_result, submit_output = _invoke(
        executor,
        _submission_request("must-not-submit-fixture"),
    )
    worker_exit, worker_result, worker_output = _invoke(
        executor,
        {
            "schema_version": 1,
            "operation": "worker_once",
            "worker": "synthetic-one-shot-worker",
            "lease_seconds": int(LEASE_TTL.total_seconds()),
        },
    )
    malformed_exit, malformed_result, malformed_output = _invoke(
        executor,
        _run_request("A" * 64),
    )
    worker_override_exit, worker_override_result, worker_override_output = _invoke(
        executor,
        {
            **_run_request(run_key),
            "worker": "attacker-selected-worker",
            "lease_seconds": int(LEASE_TTL.total_seconds()),
        },
    )

    assert run_exit == 78 and run_result == {"code": "role_not_enabled", "status": "rejected"}
    assert submit_exit == 78
    assert submit_result == {"code": "role_not_enabled", "status": "rejected"}
    assert worker_exit == 78
    assert worker_result == {"code": "role_not_enabled", "status": "rejected"}
    assert malformed_exit == 2
    assert malformed_result == {"code": "invalid_request", "status": "rejected"}
    assert worker_override_exit == 2
    assert worker_override_result == {"code": "invalid_request", "status": "rejected"}
    assert _row_count(cli_harness.engine) == before_rows
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    for output in (
        run_output,
        submit_output,
        worker_output,
        malformed_output,
        worker_override_output,
    ):
        assert "SYNTHETIC:DO_NOT_ECHO" not in output
        assert "FUYAO_API_KEY" not in output


def test_one_shot_submit_cli_rejects_sensitive_payload_without_enqueue(
    cli_harness: _CLIHarness,
) -> None:
    """Reject a secret-shaped query at the real dispatcher boundary without echoing it."""
    _submit_runtime, submitter = cli_harness.application("one_shot_submitter")
    canary = "synthetic-api-key-canary-204"
    request = {
        "schema_version": 1,
        "operation": "one_shot_submit",
        "job_id": JOB_ID,
        "version": "v1",
        "payload": {
            "idempotency_key": "sensitive-payload-fixture",
            "query": {"api_key": canary, "instrument_id": "SYNTHETIC:ONE"},
            "partition": {},
        },
    }

    exit_code, result, output = _invoke(submitter, request)

    assert exit_code == 2
    assert result == {"code": "request_rejected", "status": "rejected"}
    assert _row_count(cli_harness.engine) == 0
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    assert canary not in output


def test_one_shot_cli_uncertain_run_is_not_reclaimed_or_retried(
    cli_harness: _CLIHarness,
) -> None:
    """Return a fixed uncertainty receipt and preserve an expired run unchanged."""
    submit_runtime, submitter = cli_harness.application("one_shot_submitter")
    submit_exit, submission, _submit_output = _invoke(
        submitter,
        _submission_request("expired-one-shot-fixture"),
    )
    run_key = submission.get("run_key")
    assert submit_exit == 0
    assert isinstance(run_key, str)

    caller_clock_ledger = RunLedger(cli_harness.engine, clock_policy="caller")
    stale_claim = caller_clock_ledger.claim_run(
        run_key,
        "interrupted-synthetic-worker",
        datetime(2020, 1, 1, tzinfo=UTC),
        timedelta(seconds=1),
    )
    assert stale_claim is not None and stale_claim.status == "running"
    executor_runtime, executor = cli_harness.application(
        "one_shot_executor",
        worker_id="synthetic-one-shot-worker",
    )

    first_exit, first_result, first_output = _invoke(executor, _run_request(run_key))
    first_after = executor_runtime.ledger.get(run_key)
    second_exit, second_result, second_output = _invoke(executor, _run_request(run_key))
    second_after = executor_runtime.ledger.get(run_key)

    expected = {
        "code": "one_shot_outcome_uncertain",
        "run_key": run_key,
        "status": "uncertain",
    }
    assert first_exit == 1 and first_result == expected
    assert second_exit == 1 and second_result == expected
    assert first_after == stale_claim
    assert second_after == stale_claim
    assert cli_harness.calls.fetch == []
    assert cli_harness.calls.publish == []
    assert "SYNTHETIC:DO_NOT_ECHO" not in first_output
    assert "SYNTHETIC:DO_NOT_ECHO" not in second_output


def test_one_shot_cli_sanitizes_worker_exceptions_without_retrying() -> None:
    """Map one SPI exception to uncertainty without returning its private text."""
    calls: list[str] = []

    def fail_after_entry(run_key: str) -> RunOutcome | None:
        calls.append(run_key)
        raise RuntimeError("synthetic-private-provider-marker")

    plan: RuntimeRolePlan = build_runtime_role_plan(
        profile="reviewed",
        roles=("one_shot_executor",),
        reviewed_config_id="tests.one-shot-cli.synthetic-failure",
        reviewed_sink_ids=(DATASET_ID,),
    )
    application = ReviewedCLIApplication(runtime_plan=plan, one_shot_run=fail_after_entry)
    run_key = "a" * 64
    exit_code, result, output = _invoke(application, _run_request(run_key))

    assert exit_code == 1
    assert result == {
        "code": "one_shot_outcome_uncertain",
        "run_key": run_key,
        "status": "uncertain",
    }
    assert calls == [run_key]
    assert "synthetic-private-provider-marker" not in output


def test_one_shot_run_key_validator_rejects_non_sha256_values() -> None:
    """Accept only lowercase 64-character SHA-256 keys at the CLI boundary."""
    application = ReviewedCLIApplication(
        runtime_plan=build_runtime_role_plan(
            profile="reviewed",
            roles=("one_shot_executor",),
            reviewed_config_id="tests.one-shot-cli.run-key-validation",
            reviewed_sink_ids=(DATASET_ID,),
        ),
        one_shot_run=lambda _run_key: None,
    )
    for invalid_key in ("f" * 63, "f" * 65, "g" * 64, "F" * 64, "0x" + "f" * 62):
        exit_code, result, _output = _invoke(application, _run_request(invalid_key))
        assert exit_code == 2
        assert result == {"code": "invalid_request", "status": "rejected"}
