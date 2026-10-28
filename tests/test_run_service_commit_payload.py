"""Behavioral tests for ephemeral, bounded RunService commit sidecars."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

from opendata.pipeline.run_identity import (
    RunRequest,
    ScheduleMetadata,
    TaskDefinition,
)
from opendata.pipeline.run_ledger import RunLedgerConflictError, RunRecord
from opendata.pipeline.run_service import (
    MAX_COMMIT_PAYLOAD_BYTES,
    NormalizedBatch,
    NormalizedRecord,
    PublicationReceipt,
    ReviewedJob,
    ReviewedJobRegistry,
    RunOutcome,
    RunService,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Connection

NOW = datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc)
TTL = timedelta(minutes=5)
DATASET_ID = "cn.stock.daily"
SECRET_MARKER = "private-sidecar-sentinel"


class _Connection:
    """Opaque marker for a publisher that performs no SQL in unit tests."""


class _BytesSubclass(bytes):
    """A bytes subtype that exposes dynamic length behavior if invoked."""

    len_calls = 0

    def __len__(self) -> int:
        type(self).len_calls += 1
        raise AssertionError("a bytes subtype must be rejected before len()")


class RecordingLedger:
    """In-memory run ledger that records the transaction callback boundary."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.records: dict[str, RunRecord] = {}
        self.events = events if events is not None else []

    def enqueue(self, run_key: str, jobsnapshot_json: str) -> RunRecord:
        existing = self.records.get(run_key)
        if existing is not None:
            if existing.job_snapshot_json != jobsnapshot_json:
                raise RunLedgerConflictError("run key already has another snapshot")
            return existing
        record = RunRecord(
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
        self.records[run_key] = record
        return record

    def claim(self, worker: str, now: datetime, ttl: timedelta) -> RunRecord | None:
        for run_key, record in self.records.items():
            if record.status == "queued":
                claimed = replace(
                    record,
                    status="running",
                    worker=worker,
                    lease_id="test-lease",
                    fencing_token=record.fencing_token + 1,
                    lease_expires_at=now + ttl,
                    updated_at=now,
                    started_at=now,
                    heartbeat_at=now,
                )
                self.records[run_key] = claimed
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
        record = self.records[run_key]
        if (
            record.status != "running"
            or record.worker != worker
            or record.fencing_token != fencing_token
            or record.lease_expires_at is None
            or record.lease_expires_at <= now
        ):
            return None
        renewed = replace(
            record,
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
        record = self.records[run_key]
        if (
            record.status != "running"
            or record.worker != worker
            or record.fencing_token != fencing_token
            or record.lease_expires_at is None
            or record.lease_expires_at <= now
        ):
            return None
        failed = replace(
            record,
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
        record = self.records[run_key]
        if (
            record.status != "running"
            or record.worker != worker
            or record.fencing_token != fencing_token
            or record.lease_expires_at is None
            or record.lease_expires_at <= now
        ):
            return None
        self.events.append("commit_result")
        receipt = operation(_Connection())  # type: ignore[arg-type]
        commit_time = clock() if clock is not None else now
        succeeded = replace(
            record,
            status="succeeded",
            worker=None,
            lease_id=None,
            lease_expires_at=None,
            updated_at=commit_time,
            finished_at=commit_time,
        )
        self.records[run_key] = succeeded
        return succeeded, receipt


def _task() -> TaskDefinition:
    return TaskDefinition(
        job_id="stock-daily",
        version="v1",
        dataset_id=DATASET_ID,
        provider="provider-cn",
        model="stock-daily-v1",
        schedule=ScheduleMetadata(kind="cron", expression="0 16 * * 1-5"),
        enabled=True,
    )


def _batch(payload: bytes | None = None) -> NormalizedBatch:
    return NormalizedBatch(
        records=(NormalizedRecord("000001.SZ:2026-10-27", {"close": 7}),),
        schema_version="1",
        semantic_version="1",
        unit="CNY/share",
        adjustment_basis="none",
        quality={"rejected_rows": 0},
        provenance={"provider": "provider-cn"},
        commit_payload=payload,
    )


def _job(
    *,
    normalize: Callable[..., NormalizedBatch] | None = None,
    publish: Callable[..., PublicationReceipt] | None = None,
    payload_type: type[bytes] | None = None,
    validator: Callable[[bytes], bool] | None = None,
    max_payload_bytes: int | None = None,
) -> ReviewedJob:
    return ReviewedJob(
        task=_task(),
        fetch=lambda _request, _context: None,
        normalize=normalize or (lambda _raw, _request, _context: _batch()),
        publish=publish
        or (
            lambda _connection, task, _request, batch, _key: PublicationReceipt(
                "pub-1", task.dataset_id, len(batch.records)
            )
        ),
        reviewed=True,
        commit_payload_type=payload_type,
        validate_commit_payload=validator,
        max_commit_payload_bytes=max_payload_bytes,
    )


def _service(
    job: ReviewedJob,
    *,
    ledger: RecordingLedger | None = None,
    events: list[str] | None = None,
) -> tuple[RunService, RecordingLedger]:
    selected_ledger = ledger or RecordingLedger(events)
    service = RunService(
        selected_ledger,
        ReviewedJobRegistry([job]),
        clock=lambda: NOW,
    )
    return service, selected_ledger


def _request(key: str = "manual-1", symbol: str = "000001.SZ") -> RunRequest:
    return RunRequest(
        run_type="manual",
        query={"symbol": symbol},
        idempotency_key=key,
    )


def _run(service: RunService, request: RunRequest | None = None) -> RunOutcome | None:
    """Submit one request and execute one claimed run."""
    service.submit("stock-daily", "v1", request or _request())
    return service.run_once("worker-a", TTL)


def test_legacy_job_without_sidecar_remains_compatible() -> None:
    """Existing jobs with default sidecar fields still complete normal batches."""
    service, ledger = _service(_job())

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert outcome.publication == PublicationReceipt("pub-1", DATASET_ID, 1)
    assert ledger.get(outcome.record.run_key) == outcome.record


def test_unregistered_sidecar_fails_before_commit_and_publish() -> None:
    """A legacy job cannot publish even a well-formed bytes sidecar."""
    events: list[str] = []
    job = _job(
        normalize=lambda *_args: _batch(b"unregistered"),
        publish=lambda *_args: (
            events.append("publish"),
            PublicationReceipt("pub-1", DATASET_ID, 1),
        )[1],
    )
    service, ledger = _service(job, events=events)

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_CommitPayloadInvalidError"
    assert events == []
    assert ledger.events == []


def test_registered_sidecar_requires_payload_before_copying_records() -> None:
    """A registered sidecar is required and checked before record traversal."""
    events: list[str] = []
    record_events: list[str] = []

    class TrackingRecords(tuple[NormalizedRecord, ...]):
        def __iter__(self):
            record_events.append("iterate_records")
            return super().__iter__()

    batch = replace(_batch(), records=TrackingRecords(_batch().records))
    job = _job(
        normalize=lambda *_args: batch,
        validator=lambda _payload: events.append("validate") is None,
        payload_type=bytes,
        max_payload_bytes=32,
        publish=lambda *_args: (
            events.append("publish"),
            PublicationReceipt("pub-1", DATASET_ID, 1),
        )[1],
    )
    service, ledger = _service(job, events=events)

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_CommitPayloadInvalidError"
    assert record_events == []
    assert events == []
    assert ledger.events == []


def test_normalized_batch_subclass_with_drifting_payload_is_rejected_without_read() -> None:
    """The runner refuses batch subclasses before a sidecar property can drift."""
    events: list[str] = []
    payload_values = [b"within-limit", b"this-payload-is-too-large"]
    base = _batch()

    class DriftingBatch(NormalizedBatch):
        __slots__ = ()
        payload_reads = 0

        @property
        def commit_payload(self) -> bytes | None:
            type(self).payload_reads += 1
            return payload_values.pop(0)

        @commit_payload.setter
        def commit_payload(self, _value: bytes | None) -> None:
            pass

    batch = DriftingBatch(
        records=base.records,
        schema_version=base.schema_version,
        semantic_version=base.semantic_version,
        unit=base.unit,
        adjustment_basis=base.adjustment_basis,
        quality=base.quality,
        provenance=base.provenance,
        watermark=base.watermark,
        commit_payload=b"constructor-value",
    )
    job = _job(
        normalize=lambda *_args: batch,
        validator=lambda _payload: events.append("validate") is None,
        payload_type=bytes,
        max_payload_bytes=16,
    )
    service, ledger = _service(job, events=events)

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_ResultInvalidError"
    assert DriftingBatch.payload_reads == 0
    assert events == []
    assert ledger.events == []


def test_exact_bytes_type_rejects_subclasses_without_calling_len() -> None:
    """A bytes subclass is refused before its overridable methods can run."""
    events: list[str] = []
    _BytesSubclass.len_calls = 0
    payload = _BytesSubclass(b"dynamic")
    job = _job(
        normalize=lambda *_args: _batch(payload),
        payload_type=bytes,
        validator=lambda _payload: events.append("validate") is None,
        max_payload_bytes=32,
        publish=lambda *_args: (
            events.append("publish"),
            PublicationReceipt("pub-1", DATASET_ID, 1),
        )[1],
    )
    service, ledger = _service(job, events=events)

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_ResultInvalidError"
    assert _BytesSubclass.len_calls == 0
    assert events == []
    assert ledger.events == []


@pytest.mark.parametrize(
    ("payload_type", "validator", "limit", "error_type"),
    [
        (None, lambda _payload: True, 16, TypeError),
        (str, lambda _payload: True, 16, TypeError),
        (_BytesSubclass, lambda _payload: True, 16, TypeError),
        (bytes, None, 16, TypeError),
        (bytes, lambda _payload: True, None, ValueError),
        (bytes, lambda _payload: True, True, ValueError),
        (bytes, lambda _payload: True, 0, ValueError),
        (bytes, lambda _payload: True, MAX_COMMIT_PAYLOAD_BYTES + 1, ValueError),
    ],
)
def test_incomplete_or_unsafe_job_sidecar_declaration_is_rejected(
    payload_type: type[bytes] | None,
    validator: Callable[[bytes], bool] | None,
    limit: int | None,
    error_type: type[Exception],
) -> None:
    """Registration requires exact bytes, a function validator, and a hard cap."""
    with pytest.raises(error_type):
        ReviewedJobRegistry(
            [
                _job(
                    payload_type=payload_type,
                    validator=validator,
                    max_payload_bytes=limit,
                )
            ]
        )


def test_callable_object_is_not_accepted_as_sidecar_validator() -> None:
    """Arbitrary callable objects cannot supply dynamic validator dispatch."""

    class CallableValidator:
        def __call__(self, _payload: bytes) -> bool:
            raise AssertionError("untrusted callable object must never be invoked")

    with pytest.raises(TypeError, match="explicit function"):
        ReviewedJobRegistry(
            [
                _job(
                    payload_type=bytes,
                    validator=CallableValidator(),  # type: ignore[arg-type]
                    max_payload_bytes=32,
                )
            ]
        )


def test_async_validator_is_rejected_at_registration() -> None:
    """The runner never creates an unawaited or deferred validation result."""

    async def validate(_payload: bytes) -> bool:
        return True

    with pytest.raises(TypeError, match="explicit function"):
        ReviewedJobRegistry([_job(payload_type=bytes, validator=validate, max_payload_bytes=32)])


def test_oversized_payload_is_rejected_before_validator_or_transaction() -> None:
    """The runner enforces the declared byte cap before calling job code."""
    events: list[str] = []
    record_events: list[str] = []

    class TrackingRecords(tuple[NormalizedRecord, ...]):
        def __iter__(self):
            record_events.append("iterate_records")
            return super().__iter__()

    batch = replace(_batch(b"12345"), records=TrackingRecords(_batch().records))
    job = _job(
        normalize=lambda *_args: batch,
        payload_type=bytes,
        validator=lambda _payload: events.append("validate") is None,
        max_payload_bytes=4,
        publish=lambda *_args: (
            events.append("publish"),
            PublicationReceipt("pub-1", DATASET_ID, 1),
        )[1],
    )
    service, ledger = _service(job, events=events)

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_CommitPayloadInvalidError"
    assert record_events == []
    assert events == []
    assert ledger.events == []


@pytest.mark.parametrize(
    "validator",
    [
        lambda _payload: False,
        lambda _payload: 1,  # type: ignore[return-value]
        lambda _payload: (_ for _ in ()).throw(RuntimeError(SECRET_MARKER)),
    ],
)
def test_validator_failure_is_sanitized_and_never_publishes(
    validator: Callable[[bytes], bool], caplog: pytest.LogCaptureFixture
) -> None:
    """False, non-bool, and raising validators fail closed without leaking data."""
    payload = SECRET_MARKER.encode()
    events: list[str] = []
    job = _job(
        normalize=lambda *_args: _batch(payload),
        payload_type=bytes,
        validator=validator,
        max_payload_bytes=64,
        publish=lambda *_args: (
            events.append("publish"),
            PublicationReceipt("pub-1", DATASET_ID, 1),
        )[1],
    )
    service, ledger = _service(job, events=events)
    queued = service.submit("stock-daily", "v1", _request())
    batch = _batch(payload)

    outcome = service.run_once("worker-a", TTL)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "failed"
    assert outcome.record.error_text == "execution_failed:_CommitPayloadInvalidError"
    assert payload not in queued.job_snapshot_json.encode()
    assert SECRET_MARKER not in queued.job_snapshot_json
    assert SECRET_MARKER not in repr(batch)
    assert SECRET_MARKER not in repr(outcome.record)
    assert SECRET_MARKER not in caplog.text
    assert events == []
    assert ledger.events == []


def test_valid_sidecar_is_preserved_validated_before_commit_and_hidden_from_repr() -> None:
    """The validated sidecar reaches this run's publisher after external checks."""
    payload = b"capture-v1:bounded-test-payload"
    events: list[str] = []
    validated: list[bytes] = []
    published: list[bytes | None] = []

    def normalize(_raw: object, _request: RunRequest, _context: object) -> NormalizedBatch:
        events.append("normalize")
        return _batch(payload)

    def validate(sidecar: bytes) -> bool:
        events.append("validate")
        validated.append(sidecar)
        return sidecar.startswith(b"capture-v1:")

    def publish(
        _connection: Connection,
        task: TaskDefinition,
        _request: RunRequest,
        batch: NormalizedBatch,
        _key: str,
    ) -> PublicationReceipt:
        events.append("publish")
        published.append(batch.commit_payload)
        return PublicationReceipt("pub-1", task.dataset_id, len(batch.records))

    job = _job(
        normalize=normalize,
        publish=publish,
        payload_type=bytes,
        validator=validate,
        max_payload_bytes=128,
    )
    service, _ = _service(job, events=events)
    assert payload.decode() not in repr(_batch(payload))

    outcome = _run(service)

    assert outcome is not None and outcome.record is not None
    assert outcome.record.status == "succeeded"
    assert published == [payload]
    assert validated[0] is published[0] is payload
    assert events == ["normalize", "validate", "commit_result", "publish"]
    assert payload.decode() not in outcome.record.job_snapshot_json
    assert payload.decode() not in repr(outcome.record)


def test_two_runs_deliver_only_their_own_sidecar_without_ledger_persistence() -> None:
    """Two queued requests retain per-run payload identity without shared caches."""
    payload_by_symbol = {
        "000001.SZ": b"capture:first",
        "600000.SH": b"capture:second",
    }
    published: list[tuple[str, bytes | None]] = []

    def fetch(request: RunRequest, _context: object) -> object:
        return payload_by_symbol[request.query["symbol"]]

    def normalize(raw: object, _request: RunRequest, _context: object) -> NormalizedBatch:
        assert type(raw) is bytes
        return _batch(raw)

    def publish(
        _connection: Connection,
        _task: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        _key: str,
    ) -> PublicationReceipt:
        published.append((str(request.query["symbol"]), batch.commit_payload))
        return PublicationReceipt("pub-1", DATASET_ID, len(batch.records))

    job = _job(
        normalize=normalize,
        publish=publish,
        payload_type=bytes,
        validator=lambda payload: payload.startswith(b"capture:"),
        max_payload_bytes=64,
    )
    job = replace(job, fetch=fetch)
    service, ledger = _service(job)
    first = service.submit("stock-daily", "v1", _request("first", "000001.SZ"))
    second = service.submit("stock-daily", "v1", _request("second", "600000.SH"))

    first_outcome = service.run_once("worker-a", TTL)
    second_outcome = service.run_once("worker-a", TTL)

    assert first_outcome is not None and first_outcome.record is not None
    assert second_outcome is not None and second_outcome.record is not None
    assert first_outcome.record.status == second_outcome.record.status == "succeeded", (
        "runs did not succeed: "
        f"first={first_outcome.record.error_text!r}, "
        f"second={second_outcome.record.error_text!r}"
    )
    assert published == [
        ("000001.SZ", b"capture:first"),
        ("600000.SH", b"capture:second"),
    ]
    assert "capture:first" not in first.job_snapshot_json
    assert "capture:second" not in second.job_snapshot_json
    assert all("capture:" not in repr(record) for record in ledger.records.values())
