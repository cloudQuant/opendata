"""Behavioral tests for the offline, resumable stock-daily backfill executor."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import create_engine, func, select

from opendata.data.dataset_contract_registry import DatasetContractRegistry
from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
    LifecycleStatus,
)
from opendata.data.instrument_reference_snapshot import encode_instrument_reference_snapshot
from opendata.pipeline import stock_daily_backfill_plan as planner
from opendata.pipeline.publication_store import (
    PUBLICATION_METADATA,
    PUBLICATIONS,
    PUBLISHED_FACT_VERSIONS,
)
from opendata.pipeline.raw_evidence import RawResponseArtifactWriter
from opendata.pipeline.run_dispatch import ReviewedTaskCatalog, RunDispatcher
from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import RUN_LEDGER_METADATA, RUN_LEDGER_TABLE, RunLedger
from opendata.pipeline.run_service import (
    ReviewedJob,
    ReviewedJobRegistry,
    RunOutcome,
    RunService,
)
from opendata.pipeline.stock_daily_backfill_executor import (
    EXECUTION_AUTHORIZATION,
    StockDailyBackfillExecutionError,
    StockDailyBackfillExecutor,
    build_stock_daily_execution_contract,
    build_stock_daily_execution_task,
)
from opendata.pipeline.stock_daily_backfill_plan import (
    StockDailyBackfillChunk,
    StockDailyBackfillPlan,
    StockDailyBackfillPlanIntegrityError,
)
from opendata.pipeline.stock_daily_job import StockDailyJsonResponse, build_stock_daily_job

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.engine import Engine

    from opendata.data.platform_contracts import DatasetContract
    from opendata.data.providers.akshare.models.stock_daily import StockDailyQuery

UTC = timezone.utc
OBSERVED_AT = datetime(2020, 1, 3, 8, 15, tzinfo=UTC)
START = date(2020, 1, 1)
END = date(2020, 1, 2)
LEASE_TTL = timedelta(seconds=30)


@dataclass(slots=True)
class _Harness:
    """Owned SQLite ledger, publication schema, reviewed job, and exact plan."""

    engine: Engine
    ledger: RunLedger
    dispatcher: RunDispatcher
    service: RunService
    registry: ReviewedJobRegistry
    job: ReviewedJob
    reference_registry: InstrumentReferenceRegistry
    plan: StockDailyBackfillPlan
    task: TaskDefinition
    source_calls: list[date]

    def executor(
        self,
        *,
        max_chunks: int = 2,
        max_failures: int = 1,
        dry_run: bool = False,
        authorization: str | None = EXECUTION_AUTHORIZATION,
    ) -> StockDailyBackfillExecutor:
        """Build one bounded executor over this harness."""
        return StockDailyBackfillExecutor(
            task=self.task,
            registry=self.registry,
            dispatcher=self.dispatcher,
            ledger=self.ledger,
            worker=_RunOnceWorker(self.service),
            max_chunks_per_invocation=max_chunks,
            max_failures=max_failures,
            dry_run=dry_run,
            execution_authorization=authorization,
        )

    def counts(self) -> tuple[int, int, int]:
        """Return ledger rows, publication rows, and published fact versions."""
        with self.engine.connect() as connection:
            return (
                int(connection.scalar(select(func.count()).select_from(RUN_LEDGER_TABLE))),
                int(connection.scalar(select(func.count()).select_from(PUBLICATIONS))),
                int(connection.scalar(select(func.count()).select_from(PUBLISHED_FACT_VERSIONS))),
            )


class _RunOnceWorker:
    """Adapt the generic run service to the executor's single-run protocol."""

    def __init__(self, service: RunService) -> None:
        """Bind the exact service used by the harness ledger."""
        self._service = service

    def run_once(self) -> RunOutcome | None:
        """Claim and execute at most one queued run."""
        return self._service.run_once("executor-test-worker", LEASE_TTL)


def _lifecycle(instrument_id: str, *, as_of: date = END) -> InstrumentLifecycle:
    """Build one active lifecycle with an explicit start boundary."""
    dataset_id = build_stock_daily_execution_contract().dataset_id
    source_id = build_stock_daily_execution_contract().source.source_id
    return InstrumentLifecycle(
        dataset_id=dataset_id,
        source_id=source_id,
        instrument_id=instrument_id,
        lifecycle_version="life-v1",
        status=LifecycleStatus.ACTIVE,
        status_as_of=as_of,
        status_evidence_ref="review:status-v1",
        known_start_date=START,
        start_boundary_unknown=False,
        start_evidence_ref="exchange:listing-v1",
    )


def _alias(
    symbol: str,
    instrument_id: str,
    *,
    valid_from: date = START,
    valid_through: date = END,
) -> InstrumentAlias:
    """Build one reviewed source alias covering the fixture window."""
    dataset_id = build_stock_daily_execution_contract().dataset_id
    source_id = build_stock_daily_execution_contract().source.source_id
    return InstrumentAlias(
        dataset_id=dataset_id,
        source_id=source_id,
        alias=symbol,
        instrument_id=instrument_id,
        alias_version="ticker-v1",
        evidence_ref=f"review:{symbol}",
        valid_from=valid_from,
        valid_through=valid_through,
    )


def _reference_registry(*, two_instruments: bool = False) -> InstrumentReferenceRegistry:
    """Freeze one or two lifecycle/alias pairs for planner fixtures."""
    lifecycles = (_lifecycle("CN:600519"),)
    aliases = (_alias("600519.SH", "CN:600519"),)
    if two_instruments:
        lifecycles = (_lifecycle("CN:000001"), _lifecycle("CN:600519"))
        aliases = (
            _alias("000001.SZ", "CN:000001"),
            _alias("600519.SH", "CN:600519"),
        )
    return InstrumentReferenceRegistry(
        registry_version="executor-registry-v1",
        evidence_ref="review:executor-registry-v1",
        lifecycles=lifecycles,
        aliases=aliases,
    )


def _wire_body(day: date, *, symbol: str = "600519") -> bytes:
    """Return one synthetic, window-matching Eastmoney envelope."""
    return json.dumps(
        {
            "rc": 0,
            "data": {
                "code": symbol.split(".")[0],
                "market": 1,
                "klines": [f"{day.isoformat()},10.0,11.0,11.5,9.5,12,134,20,10,1,2"],
            },
        },
        separators=(",", ":"),
    ).encode()


def _make_job(
    *,
    task: TaskDefinition,
    contract: DatasetContract,
    artifact_root: Path,
    source_calls: list[date],
    fail_second: bool,
) -> ReviewedJob:
    """Build the reviewed execution-only stock-daily adapter."""
    contract_registry = DatasetContractRegistry([contract])
    writer = RawResponseArtifactWriter(artifact_root)

    def source(query: StockDailyQuery) -> StockDailyJsonResponse:
        day = query.start_date
        symbol = query.symbol
        source_calls.append(day)
        if fail_second and day == END:
            raise RuntimeError("fixture source failure")
        return StockDailyJsonResponse(_wire_body(day, symbol=symbol), 200, OBSERVED_AT)

    unreviewed = build_stock_daily_job(
        task,
        schema_version=contract.schema_version,
        semantic_version=contract.semantic_version,
        contract_registry=contract_registry,
        artifact_writer=writer,
        response_source=source,
    )
    return replace(unreviewed, reviewed=True, execution_only=True)


@contextmanager
def _harness(
    tmp_path: Path,
    *,
    two_instruments: bool = False,
    fail_second: bool = False,
    execution_only: bool = True,
):
    """Create owned SQLite ledger/publication state and one exact plan."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    contract = build_stock_daily_execution_contract()
    task = build_stock_daily_execution_task()
    database_path = tmp_path / "executor.sqlite"
    engine = create_engine(f"sqlite:///{database_path}")
    RUN_LEDGER_METADATA.create_all(engine)
    PUBLICATION_METADATA.create_all(engine)
    source_calls: list[date] = []
    job = _make_job(
        task=task,
        contract=contract,
        artifact_root=tmp_path / "private-raw",
        source_calls=source_calls,
        fail_second=fail_second,
    )
    if not execution_only:
        job = replace(job, execution_only=False)
    registry = ReviewedJobRegistry([job])
    catalog = ReviewedTaskCatalog([job])
    ledger = RunLedger(engine)
    reference_registry = _reference_registry(two_instruments=two_instruments)
    plan = planner.build_stock_daily_backfill_plan(
        reference_registry,
        task=task,
        contract=contract,
        as_of=END,
        max_window_days=1,
    )
    try:
        yield _Harness(
            engine=engine,
            ledger=ledger,
            dispatcher=RunDispatcher(ledger, catalog),
            service=RunService(ledger, registry),
            registry=registry,
            job=job,
            reference_registry=reference_registry,
            plan=plan,
            task=task,
            source_calls=source_calls,
        )
    finally:
        engine.dispose()


def _rehash(plan: StockDailyBackfillPlan) -> StockDailyBackfillPlan:
    """Recompute the plan digest after an intentional structural tamper."""
    values = {
        field.name: getattr(plan, field.name)
        for field in fields(plan)
        if field.name != "plan_sha256"
    }
    return replace(plan, plan_sha256=planner._plan_sha256(**values))


def _chunk_payload(chunk: StockDailyBackfillChunk) -> dict[str, object]:
    """Build the dispatcher payload for one plan chunk."""
    request = chunk.request
    return {
        "query": request.query,
        "partition": request.partition,
        "shard": request.shard,
        "idempotency_key": request.idempotency_key,
        "backfill_start": request.backfill_start,
        "backfill_end": request.backfill_end,
    }


def test_dry_run_is_default_and_writes_no_ledger_or_publication_rows(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        executor = harness.executor(dry_run=True, authorization=None)

        receipt = executor.execute_once(harness.plan)

        assert receipt.dry_run is True
        assert receipt.execution_authorized is False
        assert receipt.full_history_ready is False
        assert receipt.chunks_total == 2
        assert receipt.chunks_considered == 2
        assert receipt.chunks_executed == 0
        assert receipt.chunks_skipped == 0
        assert receipt.chunks_failed == 0
        assert receipt.chunks_remaining == 0
        assert receipt.stop_reason == "dry_run"
        assert [chunk.action for chunk in receipt.chunks] == [
            "would_dispatch",
            "would_dispatch",
        ]
        assert [chunk.status for chunk in receipt.chunks] == ["missing", "missing"]
        assert harness.counts() == (0, 0, 0)
        assert harness.source_calls == []
        encoded = receipt.to_json()
        assert str(tmp_path) not in encoded
        assert "sqlite" not in encoded
        assert "password" not in encoded
        assert executor.execute_once(harness.plan).to_json() == encoded


def test_execution_requires_the_exact_explicit_authorization(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        with pytest.raises(StockDailyBackfillExecutionError) as caught:
            harness.executor(dry_run=False, authorization=None).execute_once(harness.plan)
        assert caught.value.code == "execution_not_authorized"
        with pytest.raises(StockDailyBackfillExecutionError) as caught:
            harness.executor(dry_run=False, authorization="not-the-token")
        assert caught.value.code == "execution_authorization_invalid"
        assert harness.counts() == (0, 0, 0)


def test_execute_two_chunks_then_rerun_skips_them_without_duplicate_publication(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path) as harness:
        executor = harness.executor(max_chunks=2)

        first = executor.execute_once(harness.plan)

        assert first.stop_reason == "plan_finished"
        assert first.chunks_executed == 2
        assert first.chunks_failed == 0
        assert first.chunks_remaining == 0
        assert [chunk.action for chunk in first.chunks] == ["executed", "executed"]
        assert harness.counts() == (2, 2, 2)
        assert harness.source_calls == [START, END]

        second = executor.execute_once(harness.plan)

        assert second.stop_reason == "plan_finished"
        assert second.chunks_executed == 0
        assert second.chunks_skipped == 2
        assert second.chunks_failed == 0
        assert [chunk.action for chunk in second.chunks] == [
            "observed_succeeded",
            "observed_succeeded",
        ]
        assert harness.counts() == (2, 2, 2)
        assert harness.source_calls == [START, END]


def test_interrupted_prefix_resumes_and_skips_the_succeeded_chunk(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        first = harness.executor(max_chunks=1).execute_once(harness.plan)

        assert first.stop_reason == "max_chunks_reached"
        assert first.chunks_executed == 1
        assert first.chunks_remaining == 1
        assert harness.counts() == (1, 1, 1)

        second = harness.executor(max_chunks=2).execute_once(harness.plan)

        assert second.stop_reason == "plan_finished"
        assert second.chunks_skipped == 1
        assert second.chunks_executed == 1
        assert [chunk.action for chunk in second.chunks] == ["observed_succeeded", "executed"]
        assert harness.counts() == (2, 2, 2)


def test_failed_chunk_stops_and_is_never_retried_under_a_new_identity(tmp_path: Path) -> None:
    with _harness(tmp_path, fail_second=True) as harness:
        first = harness.executor(max_chunks=2).execute_once(harness.plan)

        assert first.stop_reason == "chunk_failed"
        assert first.chunks_executed == 1
        assert first.chunks_failed == 1
        assert first.chunks_remaining == 0
        failed_run_key = first.chunks[-1].run_key
        failed_chunk_key = harness.plan.candidate_chunks[1].idempotency_key
        assert harness.counts() == (2, 1, 1)

        second = harness.executor(max_chunks=2).execute_once(harness.plan)

        assert second.stop_reason == "chunk_failed"
        assert second.chunks_skipped == 1
        assert second.chunks_executed == 0
        assert second.chunks_failed == 1
        assert second.chunks[-1].run_key == failed_run_key
        assert harness.plan.candidate_chunks[1].idempotency_key == failed_chunk_key
        assert harness.counts() == (2, 1, 1)


def test_max_chunk_and_failure_caps_reject_loosening_and_bound_dry_run(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path) as harness:
        for invalid in (0, -1, True, 1_001):
            with pytest.raises(ValueError, match="max_chunks_per_invocation"):
                harness.executor(max_chunks=invalid)
        for invalid in (0, 2, True):
            with pytest.raises(ValueError, match="max_failures"):
                harness.executor(max_chunks=1, max_failures=invalid)  # type: ignore[arg-type]

        dry = harness.executor(max_chunks=1, dry_run=True, authorization=None).execute_once(
            harness.plan
        )
        assert dry.stop_reason == "max_chunks_reached"
        assert dry.chunks_considered == 1
        assert dry.chunks_remaining == 1
        assert harness.counts() == (0, 0, 0)


def test_leased_by_another_worker_and_expired_uncertain_lease_both_stop(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        identity = build_run_identity(harness.task, harness.plan.candidate_chunks[0].request)
        harness.ledger.enqueue(identity.idempotency_key, identity.canonical_json)
        claimed = harness.ledger.claim(
            "other-worker", datetime.now(timezone.utc), timedelta(seconds=60)
        )
        assert claimed is not None

        leased = harness.executor(max_chunks=2).execute_once(harness.plan)
        assert leased.stop_reason == "chunk_leased"
        assert leased.chunks[0].status == "running"
        assert leased.chunks_executed == 0
        assert harness.counts() == (1, 0, 0)

    with _harness(tmp_path / "expired") as harness:
        identity = build_run_identity(harness.task, harness.plan.candidate_chunks[0].request)
        harness.ledger.enqueue(identity.idempotency_key, identity.canonical_json)
        expired_now = datetime.now(timezone.utc) - timedelta(minutes=1)
        claimed = harness.ledger.claim("other-worker", expired_now, timedelta(seconds=1))
        assert claimed is not None

        uncertain = harness.executor(max_chunks=2).execute_once(harness.plan)
        assert uncertain.stop_reason == "chunk_uncertain"
        assert uncertain.chunks[0].status == "uncertain"
        assert uncertain.chunks_executed == 0
        assert harness.counts() == (1, 0, 0)


def test_tampered_plan_digest_is_rejected_before_any_ledger_write(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        tampered = replace(harness.plan, plan_sha256="0" * 64)
        with pytest.raises(StockDailyBackfillPlanIntegrityError) as caught:
            harness.executor(max_chunks=2).execute_once(tampered)
        assert caught.value.code == "plan_digest_mismatch"
        assert harness.counts() == (0, 0, 0)


def test_chunk_idempotency_mismatch_is_rejected_even_with_recomputed_plan_digest(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path) as harness:
        original = harness.plan.candidate_chunks[0]
        tampered_chunk = replace(original, idempotency_key="bf-" + "0" * 64)
        result = harness.plan.instruments[0]
        tampered_result = replace(
            result,
            candidate_chunks=(tampered_chunk, *result.candidate_chunks[1:]),
        )
        forged = _rehash(replace(harness.plan, instruments=(tampered_result,)))

        with pytest.raises(StockDailyBackfillPlanIntegrityError) as caught:
            harness.executor(max_chunks=2).execute_once(forged)
        assert caught.value.code == "chunk_idempotency_mismatch"
        assert harness.counts() == (0, 0, 0)


def test_chunk_moved_outside_its_plan_instrument_is_rejected(tmp_path: Path) -> None:
    with _harness(tmp_path, two_instruments=True) as harness:
        first, second = harness.plan.instruments
        moved = replace(first, candidate_chunks=(second.candidate_chunks[0],))
        emptied = replace(second, candidate_chunks=())
        forged = _rehash(replace(harness.plan, instruments=(moved, emptied)))

        with pytest.raises(StockDailyBackfillPlanIntegrityError) as caught:
            harness.executor(max_chunks=2).execute_once(forged)
        assert caught.value.code == "chunk_outside_plan"
        assert harness.counts() == (0, 0, 0)


def test_disabled_job_does_not_execute_without_the_execution_only_marker(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path, execution_only=False) as harness:
        with pytest.raises(ValueError, match="disabled"):
            harness.dispatcher.dispatch_backfill(
                harness.task.job_id,
                harness.task.version,
                _chunk_payload(harness.plan.candidate_chunks[0]),
            )
        with pytest.raises(ValueError, match="not approved for execution-only"):
            harness.dispatcher.dispatch_backfill_execution(
                harness.task.job_id,
                harness.task.version,
                _chunk_payload(harness.plan.candidate_chunks[0]),
            )
        with pytest.raises(ValueError, match="disabled"):
            harness.service.submit(
                harness.task.job_id,
                harness.task.version,
                harness.plan.candidate_chunks[0].request,
            )

        identity = build_run_identity(harness.task, harness.plan.candidate_chunks[0].request)
        harness.ledger.enqueue(identity.idempotency_key, identity.canonical_json)
        outcome = harness.service.run_once("ordinary-worker", LEASE_TTL)

        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"
        assert outcome.record.error_text == "execution_failed:_JobNotRunnableError"
        assert harness.counts() == (1, 0, 0)
        assert harness.source_calls == []


def test_execution_only_registry_rejects_enabled_scheduled_or_unreviewed_shapes(
    tmp_path: Path,
) -> None:
    with _harness(tmp_path) as harness:
        schedule = ScheduleMetadata(kind="cron", expression="0 16 * * mon-fri")
        enabled_task = harness.task.model_copy(update={"enabled": True, "schedule": schedule})
        scheduled_task = harness.task.model_copy(update={"schedule": schedule})

        with pytest.raises(ValueError, match="execution-only jobs must be disabled"):
            ReviewedJobRegistry([replace(harness.job, task=enabled_task)])
        with pytest.raises(ValueError, match="must not declare a schedule"):
            ReviewedJobRegistry([replace(harness.job, task=scheduled_task)])
        with pytest.raises(ValueError, match="execution-only jobs must be reviewed"):
            ReviewedJobRegistry([replace(harness.job, reviewed=False)])


def test_snapshot_bytes_are_digest_pinned_before_planning(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        snapshot = encode_instrument_reference_snapshot(harness.reference_registry)
        snapshot_sha256 = hashlib.sha256(snapshot).hexdigest()
        contract = build_stock_daily_execution_contract()

        with pytest.raises(StockDailyBackfillPlanIntegrityError) as caught:
            harness.executor(max_chunks=1, dry_run=True, authorization=None).execute_snapshot_once(
                snapshot,
                expected_snapshot_sha256="0" * 64,
                contract=contract,
                as_of=END,
                max_window_days=1,
            )
        assert caught.value.code == "snapshot_digest_mismatch"
        assert harness.counts() == (0, 0, 0)

        receipt = harness.executor(
            max_chunks=1, dry_run=True, authorization=None
        ).execute_snapshot_once(
            snapshot,
            expected_snapshot_sha256=snapshot_sha256,
            contract=contract,
            as_of=END,
            max_window_days=1,
        )
        assert receipt.plan_sha256 == harness.plan.plan_sha256
        assert receipt.snapshot_sha256 == snapshot_sha256
        assert receipt.stop_reason == "max_chunks_reached"


def test_receipt_is_deterministic_across_fresh_ledgers(tmp_path: Path) -> None:
    with _harness(tmp_path / "one") as first_harness:
        first = first_harness.executor(max_chunks=2).execute_once(first_harness.plan)
    with _harness(tmp_path / "two") as second_harness:
        second = second_harness.executor(max_chunks=2).execute_once(second_harness.plan)

    assert first.to_json() == second.to_json()
    assert first.plan_sha256 == second.plan_sha256
    assert [chunk.run_key for chunk in first.chunks] == [chunk.run_key for chunk in second.chunks]


def test_plan_must_match_the_execution_task_and_remain_unauthorized(tmp_path: Path) -> None:
    with _harness(tmp_path) as harness:
        assert harness.plan.execution_authorized is False
        assert harness.plan.full_history_ready is False
        other_task = harness.task.model_copy(update={"job_id": "other-execution-job"})
        job = replace(harness.job, task=other_task)
        registry = ReviewedJobRegistry([job])
        executor = StockDailyBackfillExecutor(
            task=other_task,
            registry=registry,
            dispatcher=RunDispatcher(harness.ledger, ReviewedTaskCatalog([job])),
            ledger=harness.ledger,
            worker=_RunOnceWorker(harness.service),
            dry_run=True,
        )

        with pytest.raises(StockDailyBackfillPlanIntegrityError) as caught:
            executor.execute_once(harness.plan)
        assert caught.value.code == "plan_task_mismatch"
