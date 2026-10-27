"""Behavioral tests for reviewed job execution and fenced publication."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection

from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_ledger import RunLedgerConflictError, RunRecord
from opendata.pipeline.run_service import (
    MAX_RESULT_BYTES,
    MAX_RESULT_RECORDS,
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunExecutionContext,
    RunService,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 26, 2, 0, tzinfo=UTC)
TTL = timedelta(minutes=5)


class FakeLedger:
    """Small deterministic ledger double that models lease and transaction rules."""

    def __init__(self) -> None:
        self.records: dict[str, RunRecord] = {}
        self.committed_publications: list[str] = []
        self.deny_heartbeat = False
        self.expire_during_commit = False
        self.cancel_before_commit = False
        self.fail_raises = False

    def enqueue(self, run_key: str, jobsnapshot_json: str) -> RunRecord:
        existing = self.records.get(run_key)
        if existing is not None:
            if existing.job_snapshot_json != jobsnapshot_json:
                raise RunLedgerConflictError("run_key already exists with different content")
            return existing
        row = RunRecord(
            run_key=run_key,
            job_snapshot_json=jobsnapshot_json,
            status="queued",
            worker=None,
            lease_id=None,
            fencing_token=0,
            lease_expires_at=None,
            created_at=NOW,
            updated_at=NOW,
            started_at=None,
            heartbeat_at=None,
            finished_at=None,
            error_text=None,
        )
        self.records[run_key] = row
        return row

    def claim(self, worker: str, now: datetime, ttl: timedelta) -> RunRecord | None:
        for key, row in self.records.items():
            if row.status == "queued":
                claimed = replace(
                    row,
                    status="running",
                    worker=worker,
                    lease_id="lease-1",
                    fencing_token=row.fencing_token + 1,
                    lease_expires_at=now + ttl,
                    updated_at=now,
                    started_at=now,
                    heartbeat_at=now,
                )
                self.records[key] = claimed
                return claimed
        return None

    def heartbeat(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> RunRecord | None:
        row = self.records[run_key]
        if (
            self.deny_heartbeat
            or row.status != "running"
            or row.worker != worker
            or row.fencing_token != fencing_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
        ):
            return None
        renewed = replace(
            row,
            lease_expires_at=now + ttl,
            updated_at=now,
            heartbeat_at=now,
        )
        self.records[run_key] = renewed
        return renewed

    def fail(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        error_text: str,
    ) -> RunRecord | None:
        if self.fail_raises:
            raise RuntimeError("error storage failure contains credential hidden-value")
        row = self.records[run_key]
        if (
            row.status != "running"
            or row.worker != worker
            or row.fencing_token != fencing_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
        ):
            return None
        failed = replace(
            row,
            status="failed",
            worker=None,
            lease_id=None,
            lease_expires_at=None,
            updated_at=now,
            finished_at=now,
            error_text=error_text,
        )
        self.records[run_key] = failed
        return failed

    def get(self, run_key: str) -> RunRecord | None:
        return self.records.get(run_key)

    def commit_result(
        self,
        run_key: str,
        *,
        worker: str,
        fencing_token: int,
        now: datetime,
        operation: Callable[[Connection], PublicationReceipt],
        clock: Callable[[], datetime] | None = None,
    ) -> tuple[RunRecord, PublicationReceipt] | None:
        row = self.records[run_key]
        if self.cancel_before_commit:
            cancelled = replace(
                row,
                status="cancelled",
                worker=None,
                lease_id=None,
                fencing_token=row.fencing_token + 1,
                lease_expires_at=None,
            )
            self.records[run_key] = cancelled
            return None
        if (
            row.status != "running"
            or row.worker != worker
            or row.fencing_token != fencing_token
            or row.lease_expires_at is None
            or row.lease_expires_at <= now
        ):
            return None
        before = row
        connection = _UnusedConnection()
        result = operation(connection)  # type: ignore[arg-type]
        commit_time = clock() if clock is not None else now
        if self.expire_during_commit and row.lease_expires_at is not None:
            commit_time = row.lease_expires_at
        if row.lease_expires_at is None or commit_time >= row.lease_expires_at:
            self.records[run_key] = replace(before, lease_expires_at=commit_time)
            raise RuntimeError("internal lease-expired error with a secret marker")
        self.committed_publications.extend(connection.staged_publications)
        succeeded = replace(
            row,
            status="succeeded",
            worker=None,
            lease_id=None,
            lease_expires_at=None,
            updated_at=commit_time,
            finished_at=commit_time,
            error_text=None,
        )
        self.records[run_key] = succeeded
        return succeeded, result


class _UnusedConnection:
    """Marker passed to publisher callbacks that do not issue SQL in unit tests."""

    def __init__(self) -> None:
        self.staged_publications: list[str] = []


def make_task(
    *, enabled: bool = True, job_id: str = "stock-daily", version: str = "v1"
) -> TaskDefinition:
    """Return a versioned task with explicit schedule metadata."""
    return TaskDefinition(
        job_id=job_id,
        version=version,
        dataset_id="cn.stock.daily",
        provider="provider-cn",
        model="stock-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * 1-5"),
        enabled=enabled,
    )


def make_batch(*, rows: int = 1, payload_value: object = 7) -> NormalizedBatch:
    """Construct normalized stock observations for service tests."""
    return NormalizedBatch(
        records=tuple(
            NormalizedRecord(
                business_key=f"000001.SZ:{index}",
                payload={"close": payload_value},  # type: ignore[dict-item]
                business_date=date(2026, 10, 23),
            )
            for index in range(rows)
        ),
        schema_version="1",
        semantic_version="1",
        unit="CNY/share",
        adjustment_basis="none",
        quality={"rejected_rows": 0},
        provenance={"provider": "provider-cn"},
    )


def make_job(
    *,
    reviewed: bool = True,
    enabled: bool = True,
    fetch: Callable[[RunRequest, RunExecutionContext], object] | None = None,
    normalize: Callable[[object, RunRequest, RunExecutionContext], NormalizedBatch] | None = None,
    publish: Callable[
        [Connection, TaskDefinition, RunRequest, NormalizedBatch, str], PublicationReceipt
    ]
    | None = None,
) -> ReviewedJob:
    """Create an explicitly wired test job."""
    return ReviewedJob(
        task=make_task(enabled=enabled),
        fetch=fetch or (lambda _request, _context: {"bars": 1}),
        normalize=normalize or (lambda _raw, _request, _context: make_batch()),
        publish=publish
        or (
            lambda _connection, task, _request, batch, _key: PublicationReceipt(
                publication_id="pub-1",
                dataset_id=task.dataset_id,
                record_count=len(batch.records),
            )
        ),
        reviewed=reviewed,
    )


def make_request(*, query: dict[str, object] | None = None, key: str = "manual-1") -> RunRequest:
    """Create a valid manual invocation with explicit idempotency."""
    return RunRequest(
        run_type="manual",
        query=query or {"symbol": "000001.SZ"},
        idempotency_key=key,
    )


def make_service(
    job: ReviewedJob | None = None,
    ledger: FakeLedger | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    **kwargs: object,
) -> tuple[RunService, FakeLedger, ReviewedJob]:
    """Bind a service to deterministic test dependencies."""
    selected_job = job or make_job()
    selected_ledger = ledger or FakeLedger()
    service = RunService(
        selected_ledger,
        ReviewedJobRegistry([selected_job]),
        clock=clock or (lambda: NOW),
        **kwargs,  # type: ignore[arg-type]
    )
    return service, selected_ledger, selected_job


def test_submit_conflicts_on_changed_request_and_retry_is_idempotent() -> None:
    """A repeated key returns the existing run but cannot hide changed query data."""
    service, ledger, _ = make_service()
    first = service.submit("stock-daily", "v1", make_request())
    replay = service.submit("stock-daily", "v1", make_request())

    assert replay == first
    assert len(ledger.records) == 1
    with pytest.raises(RunLedgerConflictError):
        service.submit("stock-daily", "v1", make_request(query={"symbol": "600000.SH"}))

    with pytest.raises(ValueError, match="not registered"):
        service.submit("unknown-job", "v1", make_request())


@pytest.mark.parametrize(
    ("reviewed", "enabled"),
    [(False, True), (True, False)],
)
def test_unreviewed_or_disabled_job_cannot_be_enqueued(reviewed: bool, enabled: bool) -> None:
    """Jobs fail closed until both explicit review and enablement are present."""
    service, ledger, _ = make_service(make_job(reviewed=reviewed, enabled=enabled))

    with pytest.raises(ValueError):
        service.submit("stock-daily", "v1", make_request())

    assert ledger.records == {}


def test_success_publishes_once_and_replay_does_not_execute_again() -> None:
    """A successful publication and terminal run prevent duplicate execution."""
    published: list[str] = []

    def publish(
        _connection: Connection,
        task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        key: str,
    ) -> PublicationReceipt:
        published.append(key)
        return PublicationReceipt("pub-1", task.dataset_id, len(batch.records))

    service, ledger, _ = make_service(make_job(publish=publish))
    queued = service.submit("stock-daily", "v1", make_request())

    outcome = service.run_once("worker-a", TTL)
    replay = service.submit("stock-daily", "v1", make_request())
    second_execution = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert outcome.publication == PublicationReceipt("pub-1", "cn.stock.daily", 1)
    assert replay.run_key == queued.run_key
    assert second_execution is None
    assert published == [queued.run_key]
    assert ledger.get(queued.run_key).status == "succeeded"  # type: ignore[union-attr]


def test_snapshot_must_match_registered_job_version_and_provider() -> None:
    """Tampered routing metadata never reaches provider or publisher handlers."""
    fetched: list[bool] = []
    published: list[bool] = []
    job = make_job(
        fetch=lambda _request, _context: fetched.append(True),
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("p", "cn.stock.daily", 1),
        )[1],
    )
    service, ledger, _ = make_service(job)
    queued = service.submit("stock-daily", "v1", make_request())
    altered = json.loads(queued.job_snapshot_json)
    altered["job"]["provider"] = "unregistered-provider"
    ledger.records[queued.run_key] = replace(
        queued,
        job_snapshot_json=json.dumps(altered, sort_keys=True, separators=(",", ":")),
    )

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_SnapshotInvalidError"
    assert fetched == []
    assert published == []


def test_cancel_before_commit_never_calls_publisher() -> None:
    """A stale/cancelled lease is rejected before transactional publication."""
    published: list[bool] = []
    job = make_job(
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("pub-1", "cn.stock.daily", 1),
        )[1]
    )
    ledger = FakeLedger()
    service, _, _ = make_service(job, ledger)
    service.submit("stock-daily", "v1", make_request())
    ledger.cancel_before_commit = True

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None
    assert outcome.publication is None
    assert published == []
    assert outcome.record is not None and outcome.record.status == "cancelled"


def test_lease_renewal_failure_stops_pipeline_before_publish() -> None:
    """A handler checkpoint aborts immediately when lease renewal fails."""
    published: list[bool] = []

    def fetch(_request: RunRequest, context: RunExecutionContext) -> object:
        context.checkpoint()
        return {"bars": 1}

    job = make_job(
        fetch=fetch,
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("pub-1", "cn.stock.daily", 1),
        )[1],
    )
    ledger = FakeLedger()
    service, _, _ = make_service(job, ledger)
    service.submit("stock-daily", "v1", make_request())
    ledger.deny_heartbeat = True

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_LeaseLostError"
    assert published == []


def test_expired_fence_during_publish_does_not_expose_exception_secret() -> None:
    """Commit-time lease loss yields no publication receipt or raw error text."""
    published: list[bool] = []

    def publish(
        connection: Connection,
        task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        _key: str,
    ) -> PublicationReceipt:
        connection.staged_publications.append("pub-1")  # type: ignore[attr-defined]
        published.append(True)
        return PublicationReceipt("pub-1", task.dataset_id, len(batch.records))

    ledger = FakeLedger()
    times = iter([NOW] * 5 + [NOW + TTL] * 5)
    service, _, _ = make_service(make_job(publish=publish), ledger, clock=lambda: next(times))
    service.submit("stock-daily", "v1", make_request())
    ledger.expire_during_commit = True

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.publication is None
    assert outcome.record is not None
    assert "secret marker" not in (outcome.record.error_text or "")
    assert outcome.record.status == "running"
    assert published == [True]
    assert ledger.committed_publications == []


def test_result_size_limit_and_credentials_fail_closed_without_leak() -> None:
    """Oversized or credential-bearing output is rejected with a stable safe code."""
    published: list[bool] = []
    job = make_job(
        normalize=lambda _raw, _request, _context: make_batch(rows=3),
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("pub-1", "cn.stock.daily", 3),
        )[1],
    )
    service, _, _ = make_service(job, max_records=2)
    service.submit("stock-daily", "v1", make_request())

    oversized = service.run_once("worker-a", TTL)
    assert oversized is not None and oversized.record is not None
    assert oversized.record.error_text == "execution_failed:_ResultInvalidError"

    secret_job = make_job(
        normalize=lambda _raw, _request, _context: make_batch(
            payload_value="Bearer hidden-token-value"
        ),
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("pub-1", "cn.stock.daily", 1),
        )[1],
    )
    secret_service, _, _ = make_service(secret_job)
    secret_service.submit("stock-daily", "v1", make_request())
    secret_result = secret_service.run_once("worker-a", TTL)

    assert secret_result is not None and secret_result.record is not None
    assert secret_result.record.error_text == "execution_failed:_ResultInvalidError"
    assert "hidden-token-value" not in (secret_result.record.error_text or "")
    assert published == []


def test_typed_backfill_snapshot_reconstructs_full_history_request() -> None:
    """The worker receives exact inclusive dates and a stable redacted key."""
    seen: list[RunRequest] = []
    request = RunRequest(
        run_type="backfill",
        query={"symbol": "000001.SZ"},
        idempotency_key="backfill-2026-01",
        backfill_start=date(2026, 1, 1),
        backfill_end=date(2026, 10, 1),
    )
    job = make_job(fetch=lambda current, _context: seen.append(current))
    service, _, _ = make_service(job)
    service.submit("stock-daily", "v1", request)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert len(seen) == 1
    assert seen[0].run_type == "backfill"
    assert seen[0].backfill_start == date(2026, 1, 1)
    assert seen[0].backfill_end == date(2026, 10, 1)
    assert seen[0].idempotency_key.startswith("sha256-")


def test_repeated_dst_wall_time_uses_distinct_utc_occurrence_slots() -> None:
    """Two folded local times remain distinct scheduled natural occurrences."""
    task = TaskDefinition(
        job_id="stock-daily",
        version="v1",
        dataset_id="cn.stock.daily",
        provider="provider-cn",
        model="stock-daily-v1",
        timezone="America/New_York",
        schedule=ScheduleMetadata(kind="cron", expression="30 1 * * *"),
        enabled=True,
    )
    job = ReviewedJob(
        task=task,
        fetch=lambda _request, _context: {"bars": 1},
        normalize=lambda _raw, _request, _context: make_batch(),
        publish=lambda _connection, current_task, _request, batch, _key: PublicationReceipt(
            "pub-1", current_task.dataset_id, len(batch.records)
        ),
        reviewed=True,
    )
    service, ledger, _ = make_service(job)
    new_york = ZoneInfo("America/New_York")
    first_fold = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=0)
    second_fold = datetime(2026, 11, 1, 1, 30, tzinfo=new_york, fold=1)

    first = service.submit(
        "stock-daily", "v1", RunRequest(run_type="scheduled", occurrence=first_fold)
    )
    second = service.submit(
        "stock-daily", "v1", RunRequest(run_type="scheduled", occurrence=second_fold)
    )

    assert first.run_key != second.run_key
    assert len(ledger.records) == 2


def test_scheduled_request_executes_the_canonical_occurrence() -> None:
    """Scheduled snapshots validate their natural UTC slot before publishing."""
    seen: list[RunRequest] = []
    job = make_job(fetch=lambda request, _context: seen.append(request))
    service, _, _ = make_service(job)
    service.submit(
        "stock-daily",
        "v1",
        RunRequest(run_type="scheduled", occurrence=NOW),
    )

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert seen[0].occurrence == NOW


@pytest.mark.parametrize(
    "snapshot_json",
    [
        "not-json",
        "[]",
        "{}",
        '{"contract_version":1,"contract_version":1,"job":{},"run":{}}',
        '{ "contract_version": 1, "job": {}, "run": {} }',
        '{"contract_version":NaN,"job":{},"run":{}}',
        " " * 65_537,
        "\ud800",
        json.dumps(
            {
                "contract_version": True,
                "job": {},
                "run": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    ],
)
def test_malformed_snapshot_is_failed_without_running_handlers(snapshot_json: str) -> None:
    """Malformed, duplicate-key, noncanonical, and bool-version snapshots fail closed."""
    fetched: list[bool] = []
    job = make_job(fetch=lambda _request, _context: fetched.append(True))
    ledger = FakeLedger()
    key = "a" * 64
    ledger.enqueue(key, snapshot_json)
    service, _, _ = make_service(job, ledger)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text is not None
    assert fetched == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot.update(extra="field"),
        lambda snapshot: snapshot.update(contract_version=2),
        lambda snapshot: snapshot.update(job="bad"),
        lambda snapshot: snapshot["job"].pop("provider"),
        lambda snapshot: snapshot.update(run="bad"),
        lambda snapshot: snapshot["run"].pop("shard"),
        lambda snapshot: snapshot["run"].update(business_request="bad"),
        lambda snapshot: snapshot["run"]["business_request"].update(query=[]),
        lambda snapshot: snapshot["run"]["business_request"].update(partition=[]),
    ],
)
def test_snapshot_shape_mutations_fail_before_fetch(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    """Each required envelope section is checked before handler dispatch."""
    task = make_task()
    identity = build_run_identity(task, make_request())
    altered = json.loads(identity.canonical_json)
    mutation(altered)
    encoded = json.dumps(altered, sort_keys=True, separators=(",", ":"))
    ledger = FakeLedger()
    ledger.enqueue(identity.idempotency_key, encoded)
    fetched: list[bool] = []
    service, _, _ = make_service(make_job(fetch=lambda *_args: fetched.append(True)), ledger)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert fetched == []


@pytest.mark.parametrize(
    ("reviewed", "enabled"),
    [(False, True), (True, False)],
)
def test_already_queued_job_is_rechecked_before_execution(reviewed: bool, enabled: bool) -> None:
    """Review and enabled state are checked again after a queued version is claimed."""
    task = make_task(enabled=enabled)
    identity = build_run_identity(task, make_request())
    ledger = FakeLedger()
    ledger.enqueue(identity.idempotency_key, identity.canonical_json)
    fetched: list[bool] = []
    job = make_job(
        reviewed=reviewed,
        enabled=enabled,
        fetch=lambda *_args: fetched.append(True),
    )
    service, _, _ = make_service(job, ledger)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert fetched == []


@pytest.mark.parametrize(
    "batch",
    [
        None,
        replace(make_batch(), records=[NormalizedRecord("k", {})]),
        replace(make_batch(), records=(object(),)),
        replace(
            make_batch(),
            records=(NormalizedRecord("same", {}), NormalizedRecord("same", {})),
        ),
        replace(make_batch(), records=(NormalizedRecord("k", {}, business_date=NOW),)),
        replace(
            make_batch(),
            records=(NormalizedRecord("k", {}, observed_at=datetime(2026, 1, 1)),),
        ),
        replace(make_batch(), records=(NormalizedRecord("k", []),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"api_key": "sensitive"}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": float("nan")}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": object()}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": 10**5000}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": "\x00"}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": "x" * 4097}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": "\ud800"}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"nested": {"": 1}}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"nested": {"a\n": 1}}),)),
        replace(make_batch(), records=(NormalizedRecord("k", {"value": [None] * 50_001}),)),
        replace(
            make_batch(),
            records=(NormalizedRecord("k", {"value": {str(i): i for i in range(50_001)}}),),
        ),
        replace(
            make_batch(),
            records=(NormalizedRecord("k", {"value": [[[[[[[[[[1]]]]]]]]]]}),),
        ),
        replace(make_batch(), schema_version="\t"),
        replace(make_batch(), quality=[]),
        replace(make_batch(), watermark=[]),
        replace(make_batch(), schema_version="Bearer hidden-value"),
    ],
)
def test_invalid_normalized_batches_never_publish(batch: object) -> None:
    """Boundaries reject mutable, duplicate, malformed, or credential-like output."""
    published: list[bool] = []
    job = make_job(
        normalize=lambda *_args: batch,  # type: ignore[return-value]
        publish=lambda *_args: (
            published.append(True),
            PublicationReceipt("pub-1", "cn.stock.daily", 1),
        )[1],
    )
    service, _, _ = make_service(job)
    service.submit("stock-daily", "v1", make_request())

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert published == []


@pytest.mark.parametrize(
    "receipt",
    [
        None,
        PublicationReceipt("pub-1", "wrong-dataset", 1),
        PublicationReceipt("pub-1", "cn.stock.daily", True),
    ],
)
def test_invalid_publication_receipt_rolls_back_and_marks_run_failed(receipt: object) -> None:
    """A publisher receipt must match registered dataset and normalized row count."""
    job = make_job(publish=lambda *_args: receipt)  # type: ignore[arg-type,return-value]
    service, ledger, _ = make_service(job)
    service.submit("stock-daily", "v1", make_request())

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.publication is None
    assert ledger.committed_publications == []


def test_run_once_without_claim_is_a_noop_and_request_type_is_checked() -> None:
    """An empty queue is idle and untyped input cannot create a run."""
    service, _, _ = make_service()
    assert service.run_once("worker-a", TTL) is None
    with pytest.raises(TypeError, match="validated RunRequest"):
        service.submit("stock-daily", "v1", object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_records": True},
        {"max_records": 0},
        {"max_records": MAX_RESULT_RECORDS + 1},
        {"max_result_bytes": True},
        {"max_result_bytes": 0},
        {"max_result_bytes": MAX_RESULT_BYTES + 1},
    ],
)
def test_result_caps_are_validated_on_service_construction(kwargs: dict[str, object]) -> None:
    """Resource limits cannot be disabled or expanded without an explicit review."""
    job = make_job()
    with pytest.raises(ValueError):
        RunService(FakeLedger(), ReviewedJobRegistry([job]), **kwargs)  # type: ignore[arg-type]


def test_failed_error_persistence_does_not_surface_the_original_secret() -> None:
    """A secondary ledger failure cannot leak handler or storage exception text."""
    job = make_job(fetch=lambda *_args: (_ for _ in ()).throw(RuntimeError("secret hidden-value")))
    ledger = FakeLedger()
    service, _, _ = make_service(job, ledger)
    service.submit("stock-daily", "v1", make_request())
    ledger.fail_raises = True

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "running"
    assert "hidden-value" not in (outcome.record.error_text or "")


def test_invalid_clock_is_rejected_before_claiming() -> None:
    """A naive injected clock cannot produce a lease timestamp."""
    service, ledger, _ = make_service(clock=lambda: datetime(2026, 1, 1))

    with pytest.raises(ValueError, match="aware UTC datetime"):
        service.run_once("worker-a", TTL)

    assert ledger.records == {}


def test_registry_and_runner_reject_invalid_dependency_registration() -> None:
    """The execution boundary rejects malformed registries and job entries."""
    valid_job = make_job()
    with pytest.raises(TypeError, match="ReviewedJob instances"):
        ReviewedJobRegistry([object()])  # type: ignore[list-item]
    with pytest.raises(TypeError, match="TaskDefinition"):
        ReviewedJobRegistry([replace(valid_job, task=object())])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="explicit callables"):
        ReviewedJobRegistry([replace(valid_job, fetch=None)])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicate"):
        ReviewedJobRegistry([valid_job, valid_job])
    with pytest.raises(TypeError, match="RunLedger contract"):
        RunService(object(), ReviewedJobRegistry([valid_job]))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="ReviewedJobRegistry"):
        RunService(FakeLedger(), object())  # type: ignore[arg-type]


def test_unknown_job_version_and_invalid_snapshot_identifier_fail_closed() -> None:
    """Unknown versions and malformed identifiers never dispatch a handler."""
    task = make_task()
    identity = build_run_identity(task, make_request())
    snapshots: list[dict[str, object]] = []
    unknown_job = json.loads(identity.canonical_json)
    unknown_job["job"]["version"] = "v2"
    snapshots.append(unknown_job)
    invalid_identifier = json.loads(identity.canonical_json)
    invalid_identifier["job"]["job_id"] = "../unsafe"
    snapshots.append(invalid_identifier)
    fetched: list[bool] = []

    for altered in snapshots:
        ledger = FakeLedger()
        ledger.enqueue(
            identity.idempotency_key,
            json.dumps(altered, sort_keys=True, separators=(",", ":")),
        )
        service, _, _ = make_service(make_job(fetch=lambda *_args: fetched.append(True)), ledger)
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"

    assert fetched == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda snapshot: snapshot["run"].update(run_type="unknown"),
        lambda snapshot: snapshot["run"].update(occurrence="2026-10-26T02:00:00"),
        lambda snapshot: snapshot["run"].update(request_key_sha256="short"),
        lambda snapshot: snapshot["run"].update(occurrence="2026-10-26T02:00:00Z"),
    ],
)
def test_invalid_manual_run_fields_are_rejected(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    """Manual mode cannot smuggle scheduled or malformed backfill metadata."""
    task = make_task()
    identity = build_run_identity(task, make_request())
    altered = json.loads(identity.canonical_json)
    mutation(altered)
    ledger = FakeLedger()
    ledger.enqueue(
        identity.idempotency_key,
        json.dumps(altered, sort_keys=True, separators=(",", ":")),
    )
    service, _, _ = make_service(ledger=ledger)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"


def test_invalid_backfill_dates_are_rejected() -> None:
    """Backfill range strings must be exact calendar dates in ascending order."""
    task = make_task()
    request = RunRequest(
        run_type="backfill",
        idempotency_key="backfill-a",
        backfill_start=date(2026, 1, 1),
        backfill_end=date(2026, 2, 1),
    )
    identity = build_run_identity(task, request)
    for start, end in (("20260101", "2026-02-01"), ("2026-03-01", "2026-02-01")):
        altered = json.loads(identity.canonical_json)
        altered["run"]["backfill_range"] = {"start": start, "end": end}
        ledger = FakeLedger()
        ledger.enqueue(
            identity.idempotency_key,
            json.dumps(altered, sort_keys=True, separators=(",", ":")),
        )
        service, _, _ = make_service(ledger=ledger)
        outcome = service.run_once("worker-a", TTL)
        assert outcome is not None and outcome.record is not None
        assert outcome.record.status == "failed"


def test_result_byte_cap_and_nonfinite_nested_metadata_fail_closed() -> None:
    """Encoded output and nested JSON values remain bounded and finite."""
    oversized_service, _, _ = make_service(max_result_bytes=1)
    oversized_service.submit("stock-daily", "v1", make_request())
    too_large = oversized_service.run_once("worker-a", TTL)
    assert too_large is not None and too_large.record is not None
    assert too_large.record.status == "failed"

    invalid_service, _, _ = make_service(
        make_job(
            normalize=lambda *_args: replace(
                make_batch(), provenance={"nested": {"secret_token": "x"}}
            )
        )
    )
    invalid_service.submit("stock-daily", "v1", make_request())
    invalid = invalid_service.run_once("worker-a", TTL)
    assert invalid is not None and invalid.record is not None
    assert invalid.record.status == "failed"


def test_valid_json_scalar_and_timestamp_values_survive_bounded_copy() -> None:
    """The safe copy preserves supported JSON scalars and aware observations."""
    observed = datetime(2026, 10, 23, 7, 0, tzinfo=UTC)
    seen: list[NormalizedBatch] = []
    batch = replace(
        make_batch(),
        records=(
            NormalizedRecord(
                "000001.SZ:2026-10-23",
                {"price": 7.25, "valid": True, "missing": None, "tags": ["a", "b"]},
                business_date=date(2026, 10, 23),
                event_at=observed,
                observed_at=observed,
                available_at=observed,
            ),
        ),
    )
    job = make_job(
        normalize=lambda *_args: batch,
        publish=lambda _connection, task, _request, safe_batch, _key: (
            seen.append(safe_batch),
            PublicationReceipt("pub-1", task.dataset_id, len(safe_batch.records)),
        )[1],
    )
    service, _, _ = make_service(job)
    service.submit("stock-daily", "v1", make_request())

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert seen[0].records[0].payload == {
        "price": 7.25,
        "valid": True,
        "missing": None,
        "tags": ["a", "b"],
    }
    assert seen[0].records[0].observed_at == observed
    assert seen[0].records[0].event_at == observed


def test_nonstandard_exception_class_is_replaced_by_safe_error_code() -> None:
    """An invalid exception class name cannot inject text into run status."""
    unusual_exception = type("bad exception name", (RuntimeError,), {})
    job = make_job(fetch=lambda *_args: (_ for _ in ()).throw(unusual_exception("do not expose")))
    service, _, _ = make_service(job)
    service.submit("stock-daily", "v1", make_request())

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.error_text == "execution_failed:UnhandledError"
