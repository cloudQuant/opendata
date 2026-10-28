"""Offline lifecycle and role guards for reviewed-runtime one-shot dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, event

from opendata.pipeline.reviewed_runtime import ReviewedRuntime, build_reviewed_runtime
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog
from opendata.pipeline.run_identity import RunRequest, ScheduleMetadata, TaskDefinition
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA
from opendata.pipeline.run_service import (
    NormalizedBatch,
    PublicationReceipt,
    ReviewedJob,
    RunExecutionContext,
)
from opendata.pipeline.runtime_roles import (
    RuntimeRole,
    RuntimeRolePlan,
    build_runtime_role_plan,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Connection, Engine


DATASET_ID = "fixture.reviewed-runtime-one-shot"


def _fetch(request: RunRequest, context: RunExecutionContext) -> object:
    """Fail if queue dispatch invokes a source adapter inline."""
    del request, context
    raise AssertionError("dispatch must not invoke a source adapter")


def _normalize(
    raw: object,
    request: RunRequest,
    context: RunExecutionContext,
) -> NormalizedBatch:
    """Fail if queue dispatch invokes normalization inline."""
    del raw, request, context
    raise AssertionError("dispatch must not invoke a normalizer")


def _publish(
    connection: Connection,
    task: TaskDefinition,
    request: RunRequest,
    batch: NormalizedBatch,
    idempotency_key: str,
) -> PublicationReceipt:
    """Fail if queue dispatch publishes business data inline."""
    del connection, task, request, batch, idempotency_key
    raise AssertionError("dispatch must not publish data")


def _job(job_id: str, *, one_shot_only: bool) -> ReviewedJob:
    """Build a reviewed synthetic task with no source or publication effects."""
    task = TaskDefinition(
        job_id=job_id,
        version="v1",
        dataset_id=DATASET_ID,
        provider="fixture-provider",
        model="fixture-model",
        timezone="UTC",
        schedule=(None if one_shot_only else ScheduleMetadata(kind="cron", expression="0 9 * * *")),
        enabled=not one_shot_only,
    )
    return ReviewedJob(
        task=task,
        fetch=_fetch,
        normalize=_normalize,
        publish=_publish,
        reviewed=True,
        one_shot_only=one_shot_only,
    )


def _plan(*roles: RuntimeRole) -> RuntimeRolePlan:
    """Build a named reviewed runtime plan for the synthetic dataset."""
    return build_runtime_role_plan(
        profile="reviewed",
        roles=roles,
        reviewed_config_id="tests.reviewed-runtime-one-shot-guard",
        reviewed_sink_ids=(DATASET_ID,),
    )


def _capture_write(
    _connection: object,
    _cursor: object,
    statement: str,
    _parameters: object,
    _context: object,
    _executemany: bool,
    writes: list[str],
) -> None:
    """Record DML statements so rejected dispatch can prove it wrote nothing."""
    normalized = statement.lstrip().upper()
    if normalized.startswith(("INSERT ", "UPDATE ", "DELETE ", "REPLACE ")):
        writes.append(statement)


@dataclass(slots=True)
class _RuntimeResources:
    """Own temporary SQLite Engines and composed runtimes for each test."""

    tmp_path: Path
    engines: list[Engine] = field(default_factory=list)
    runtimes: list[ReviewedRuntime] = field(default_factory=list)

    def build(
        self,
        name: str,
        roles: tuple[RuntimeRole, ...],
        jobs: tuple[ReviewedJob, ...],
    ) -> tuple[Engine, ReviewedRuntime, list[str]]:
        """Create a schema-backed runtime and begin capturing writes."""
        engine = create_engine(f"sqlite:///{self.tmp_path / f'{name}.sqlite'}")
        self.engines.append(engine)
        RUN_LEDGER_METADATA.create_all(engine)
        runtime = build_reviewed_runtime(
            metadata_engine=engine,
            catalog=ReviewedTaskCatalog(jobs),
            runtime_plan=_plan(*roles),
        )
        self.runtimes.append(runtime)
        writes: list[str] = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda connection, cursor, statement, parameters, context, executemany: _capture_write(
                connection,
                cursor,
                statement,
                parameters,
                context,
                executemany,
                writes,
            ),
        )
        return engine, runtime, writes

    def close(self) -> None:
        """Close runtime adapters and dispose every test-owned Engine."""
        for runtime in reversed(self.runtimes):
            runtime.close()
        for engine in reversed(self.engines):
            engine.dispose()


@pytest.fixture
def resources(tmp_path: Path) -> Iterator[_RuntimeResources]:
    """Provide only private temporary SQLite resources and unconditional cleanup."""
    owned = _RuntimeResources(tmp_path)
    try:
        yield owned
    finally:
        owned.close()


def _payload(idempotency_key: str) -> dict[str, object]:
    """Return bounded fixture input for a synthetic one-shot request."""
    return {"idempotency_key": idempotency_key, "query": {"symbol": "FIXTURE"}}


def test_closed_runtime_rejects_one_shot_before_ledger_write(
    resources: _RuntimeResources,
) -> None:
    """A closed dispatcher must fail before inserting its one-shot request."""
    _engine, runtime, writes = resources.build(
        "closed",
        ("api",),
        (_job("fixture-one-shot", one_shot_only=True),),
    )
    runtime.close()

    with pytest.raises(RuntimeError, match="reviewed runtime is closed"):
        runtime.dispatcher.dispatch_one_shot("fixture-one-shot", "v1", _payload("closed-request"))

    assert writes == []
    assert runtime.ledger.get("closed-request") is None


def test_runtime_without_api_role_rejects_one_shot_before_ledger_write(
    resources: _RuntimeResources,
) -> None:
    """A scheduler-only runtime cannot enqueue through the API one-shot path."""
    _engine, runtime, writes = resources.build(
        "scheduler-only",
        ("scheduler",),
        (_job("fixture-one-shot", one_shot_only=True),),
    )

    with pytest.raises(ValueError, match="role 'api' is not enabled"):
        runtime.dispatcher.dispatch_one_shot(
            "fixture-one-shot", "v1", _payload("scheduler-request")
        )

    assert writes == []
    assert runtime.ledger.get("scheduler-request") is None


def test_legacy_profile_rejects_one_shot_before_ledger_write(
    resources: _RuntimeResources,
) -> None:
    """A corrupted legacy profile cannot pass the API role check into dispatch."""
    _engine, runtime, writes = resources.build(
        "legacy-profile",
        ("api",),
        (_job("fixture-one-shot", one_shot_only=True),),
    )
    # Simulate a plan corrupted after factory validation while preserving its API role.
    object.__setattr__(runtime.runtime_plan, "profile", "legacy_compatible")

    with pytest.raises(ValueError, match="reviewed runtime profile"):
        runtime.dispatcher.dispatch_one_shot("fixture-one-shot", "v1", _payload("legacy-request"))

    assert writes == []
    assert runtime.ledger.get("legacy-request") is None


def test_reviewed_api_keeps_one_shot_policy_and_ordinary_manual_dispatch(
    resources: _RuntimeResources,
) -> None:
    """Allow reviewed one-shot jobs while preserving the normal manual route."""
    _engine, runtime, writes = resources.build(
        "reviewed-api",
        ("api",),
        (
            _job("fixture-one-shot", one_shot_only=True),
            _job("fixture-ordinary", one_shot_only=False),
        ),
    )

    ordinary = runtime.dispatcher.dispatch_manual(
        "fixture-ordinary", "v1", _payload("ordinary-request")
    )
    one_shot = runtime.dispatcher.dispatch_one_shot(
        "fixture-one-shot", "v1", _payload("one-shot-request")
    )

    ordinary_record = runtime.ledger.get(ordinary.identity.run_key)
    one_shot_record = runtime.ledger.get(one_shot.identity.run_key)
    assert ordinary.status == "queued"
    assert ordinary_record is not None
    assert ordinary_record.claim_policy == "recover_expired"
    assert one_shot.status == "queued"
    assert one_shot_record is not None
    assert one_shot_record.claim_policy == "one_shot"
    assert len(writes) == 2

    with pytest.raises(ValueError, match="one-shot-only"):
        runtime.dispatcher.dispatch_manual(
            "fixture-one-shot", "v1", _payload("ordinary-one-shot-request")
        )
    assert len(writes) == 2
