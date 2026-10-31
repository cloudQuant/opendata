"""Controlled, dependency-injected execution of reviewed pipeline jobs.

Jobs are executable only when an application explicitly registers reviewed
Python callables. This module never imports job paths, reads process settings,
opens provider connections, or executes legacy scripts. Fetch and normalization
run outside the warehouse transaction; publication and the succeeded ledger
transition are delegated to one fenced transaction callback.
An optional exact-bytes sidecar is validated or decoded before that callback;
any decoded transaction context is carried only by the in-memory normalized
batch for the current run.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from types import FunctionType, MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypeAlias, TypeGuard, cast

from pydantic import JsonValue

from opendata.pipeline.run_identity import RunRequest, TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import RunLedger
from opendata.utils.cooperative_checkpoint import _CooperativeCheckpoint

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.engine import Connection

    from opendata.pipeline.run_ledger import RunRecord

MAX_RESULT_RECORDS = 50_000
MAX_RESULT_BYTES = 16 * 1024 * 1024
MAX_COMMIT_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_RESULT_DEPTH = 8
MAX_RESULT_NODES = 1_000_000
MAX_RESULT_TEXT = 4_096
_RUN_LEDGER_ENQUEUE_METHOD = RunLedger.enqueue
_RUN_LEDGER_CLAIM_RUN_METHOD = RunLedger.claim_run
_UNSET_COMMIT_PAYLOAD = object()
_RUN_SNAPSHOT_IDENTITY_ISSUER = object()
_ERROR_NAME = re.compile(r"^_?[A-Za-z][A-Za-z0-9_]{0,79}$")
_SECRET_KEY_MARKERS = (
    "accesskey",
    "apikey",
    "authorization",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_BEARER_VALUE = re.compile(r"^\s*bearer\s+\S+", re.IGNORECASE)
_IDENTIFIER_CHARACTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/+-"
_COMMON_SECRET_VALUE = re.compile(
    r"^(?:sk-[A-Za-z0-9]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,})$"
)

JsonObject: TypeAlias = Mapping[str, JsonValue]


class FetchHandler(Protocol):  # pragma: no cover
    """A reviewed source adapter called outside the warehouse transaction."""

    def __call__(self, request: RunRequest, context: RunExecutionContext) -> object:
        """Fetch raw source data for one validated request."""
        ...


class NormalizeHandler(Protocol):  # pragma: no cover
    """A reviewed pure or bounded normalizer for one source response."""

    def __call__(
        self, raw: object, request: RunRequest, context: RunExecutionContext
    ) -> NormalizedBatch:
        """Convert source data into a bounded publication batch."""
        ...


class PublishHandler(Protocol):  # pragma: no cover
    """A reviewed warehouse writer that uses only the supplied connection."""

    def __call__(
        self,
        connection: Connection,
        task: TaskDefinition,
        request: RunRequest,
        batch: NormalizedBatch,
        idempotency_key: str,
    ) -> PublicationReceipt:
        """Publish facts and return a non-sensitive publication receipt."""
        ...


class ResultCommittingLedger(Protocol):  # pragma: no cover
    """The explicit ledger operations required by the controlled runner."""

    def enqueue(self, run_key: str, jobsnapshot_json: str) -> RunRecord:
        """Insert an immutable run snapshot idempotently."""
        ...

    def claim(self, worker: str, now: datetime, ttl: timedelta) -> RunRecord | None:
        """Claim the next queued run using a fencing token."""
        ...

    def heartbeat(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        ttl: timedelta,
    ) -> RunRecord | None:
        """Renew the current run lease."""
        ...

    def fail(
        self,
        run_key: str,
        worker: str,
        fencing_token: int,
        now: datetime,
        error_text: str,
    ) -> RunRecord | None:
        """Fail a run while the same lease remains valid."""
        ...

    def get(self, run_key: str) -> RunRecord | None:
        """Read a run record by idempotency key."""
        ...

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
        """Publish and mark success atomically under the live fencing token."""
        ...


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    """One complete replacement value for a publication business key."""

    business_key: str
    payload: JsonObject
    business_date: date | None = None
    event_at: datetime | None = None
    observed_at: datetime | None = None
    source_published_at: datetime | None = None
    available_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class NormalizedBatch:
    """Normalized facts plus optional, in-memory commit sidecars.

    ``commit_payload`` and ``commit_context`` are omitted from representations
    and ledger snapshots. Jobs may set only ``commit_payload``. A registered
    sidecar job must provide exact ``bytes`` on every run; the runner validates
    its declared size and decodes or validates it outside the transaction.
    ``commit_context`` is reserved for the runner to attach a decoded,
    allowlisted immutable object to the publisher's in-memory batch.
    """

    records: tuple[NormalizedRecord, ...]
    schema_version: str
    semantic_version: str
    unit: str
    adjustment_basis: str
    quality: JsonObject
    provenance: JsonObject
    watermark: JsonObject | None = None
    commit_payload: bytes | None = field(default=None, repr=False)
    commit_context: object | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Bounded, non-sensitive summary returned after a committed publication."""

    publication_id: str
    dataset_id: str
    record_count: int


@dataclass(frozen=True, slots=True)
class ReviewedJob:
    """A reviewed job and an optional bounded sidecar contract.

    Sidecar support uses exactly one of two all-or-nothing contracts: exact
    ``bytes`` plus a synchronous validator returning literal ``True``, or
    exact ``bytes`` plus the allowlisted prepared-capture decoder and output
    type. Both require a per-job byte limit. Once declared, every run must
    return a payload. Jobs without these fields reject non-``None`` payloads
    and remain compatible with batches whose payload is ``None``.
    ``one_shot_only`` jobs must be disabled and schedule-free; their only
    accepted request shape is a reviewed manual run with a one-shot ledger row.
    ``execution_only`` jobs are an explicitly reviewed control-plane exception:
    they stay disabled and schedule-free, are never accepted by ordinary
    submission or scheduled dispatch, and may execute only the exact backfill
    request enqueued through :meth:`RunService.submit_execution`.
    """

    task: TaskDefinition
    fetch: FetchHandler
    normalize: NormalizeHandler
    publish: PublishHandler
    reviewed: bool = False
    commit_payload_type: type[bytes] | None = None
    validate_commit_payload: Callable[[bytes], bool] | None = None
    max_commit_payload_bytes: int | None = None
    decode_commit_payload: Callable[[bytes], object] | None = None
    commit_context_type: type[object] | None = None
    one_shot_only: bool = False
    execution_only: bool = False


@dataclass(frozen=True, slots=True)
class _CommitPayloadContract:
    """One validated sidecar contract captured for a single run only."""

    validator: Callable[[bytes], bool] | None = None
    decoder: Callable[[bytes], object] | None = None
    context_type: type[object] | None = None


class ReviewedJobRegistry:
    """Immutable registry of explicitly supplied job implementations."""

    def __init__(self, jobs: Iterable[ReviewedJob] = ()) -> None:
        """Create a frozen registry and reject duplicate job/version pairs."""
        indexed: dict[tuple[str, str], ReviewedJob] = {}
        for job in jobs:
            if not isinstance(job, ReviewedJob):
                raise TypeError("registry entries must be ReviewedJob instances")
            if not isinstance(job.task, TaskDefinition):
                raise TypeError("reviewed job task must be a TaskDefinition")
            if not callable(job.fetch) or not callable(job.normalize) or not callable(job.publish):
                raise TypeError("reviewed job handlers must be explicit callables")
            if type(job.reviewed) is not bool:
                raise TypeError("reviewed must be a bool")
            if type(job.one_shot_only) is not bool:
                raise TypeError("one_shot_only must be a bool")
            if type(job.execution_only) is not bool:
                raise TypeError("execution_only must be a bool")
            if job.one_shot_only and job.task.enabled:
                raise ValueError("one-shot-only jobs must be disabled")
            if job.one_shot_only and job.task.schedule is not None:
                raise ValueError("one-shot-only jobs must not declare a schedule")
            if job.execution_only and job.one_shot_only:
                raise ValueError("execution-only and one-shot-only jobs are mutually exclusive")
            if job.execution_only and not job.reviewed:
                raise ValueError("execution-only jobs must be reviewed")
            if job.execution_only and job.task.enabled:
                raise ValueError("execution-only jobs must be disabled")
            if job.execution_only and job.task.schedule is not None:
                raise ValueError("execution-only jobs must not declare a schedule")
            _validate_commit_payload_declaration(job)
            key = (job.task.job_id, job.task.version)
            if key in indexed:
                raise ValueError("duplicate job_id and version in reviewed job registry")
            indexed[key] = job
        self._jobs: Mapping[tuple[str, str], ReviewedJob] = MappingProxyType(indexed)

    def resolve(self, job_id: str, version: str) -> ReviewedJob | None:
        """Return the registered exact job version, if present."""
        return self._jobs.get((job_id, version))


@dataclass(frozen=True, slots=True)
class _VerifiedRunSnapshot:
    """Canonical snapshot provenance issued by RunService for one validated claim.

    The issuer marker prevents accidental construction by callers. It is not a
    security boundary against hostile code in this Python process; the trusted
    boundary remains RunService validation plus RunLedger's fenced commit.
    """

    run_key: str
    payload_hash: str
    canonical_json: str
    _issuer: object = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class RunExecutionContext:
    """Read-only execution context with an explicit lease renewal checkpoint."""

    run_key: str
    worker: str
    fencing_token: int
    _checkpoint: Callable[[], None]
    _cooperative_checkpoint: _CooperativeCheckpoint | None = None
    _snapshot_identity: _VerifiedRunSnapshot | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Bind a local fallback throttle for contexts built outside RunService."""
        if self._cooperative_checkpoint is None:
            # Standalone contexts have no TTL; production contexts inject ttl/5 below.
            object.__setattr__(
                self,
                "_cooperative_checkpoint",
                _CooperativeCheckpoint(self._checkpoint, interval_seconds=0.2, immediate=False),
            )

    def checkpoint(self) -> None:
        """Renew the current lease or stop processing if ownership was lost."""
        self._checkpoint()
        if self._cooperative_checkpoint is not None:
            self._cooperative_checkpoint.mark_renewed()

    def checkpoint_cooperatively(self) -> None:
        """Renew the lease when cooperative CPU work reaches its safe cadence."""
        if self._cooperative_checkpoint is not None:
            self._cooperative_checkpoint()

    def _verified_snapshot_json(self) -> str | None:
        """Return the canonical snapshot only when this is a RunService-issued context.

        The snapshot is reparsed and its run key is revalidated on every read.
        LRB consumers additionally bind its task and business request to their
        arguments. This is provenance for trusted in-process composition, not
        protection from hostile code that can mutate this process.
        """
        identity = self._snapshot_identity
        if identity is None:
            return None
        if (
            type(identity) is not _VerifiedRunSnapshot
            or identity._issuer is not _RUN_SNAPSHOT_IDENTITY_ISSUER
            or identity.run_key != self.run_key
        ):
            raise ValueError("run context snapshot identity is invalid")
        try:
            snapshot = _parse_snapshot(identity.canonical_json)
            _request_from_snapshot(snapshot, identity.run_key)
            snapshot_hash = hashlib.sha256(identity.canonical_json.encode("utf-8")).hexdigest()
        except Exception as exc:
            raise ValueError("run context snapshot identity is invalid") from exc
        if snapshot_hash != identity.payload_hash:
            raise ValueError("run context snapshot identity is invalid")
        return identity.canonical_json


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Current durable run state and its receipt only after atomic success."""

    record: RunRecord | None
    publication: PublicationReceipt | None


class RunService:
    """Enqueue and execute only reviewed jobs through an injected run ledger."""

    def __init__(
        self,
        ledger: ResultCommittingLedger,
        registry: ReviewedJobRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        max_records: int = MAX_RESULT_RECORDS,
        max_result_bytes: int = MAX_RESULT_BYTES,
    ) -> None:
        """Bind explicit storage, reviewed code, a UTC clock, and result caps."""
        if not _supports_ledger_protocol(ledger):
            raise TypeError("ledger must implement the explicit RunLedger contract")
        if not isinstance(registry, ReviewedJobRegistry):
            raise TypeError("registry must be a ReviewedJobRegistry")
        if isinstance(max_records, bool) or not isinstance(max_records, int):
            raise ValueError("max_records must be an integer")
        if max_records < 1 or max_records > MAX_RESULT_RECORDS:
            raise ValueError(f"max_records must be between 1 and {MAX_RESULT_RECORDS}")
        if isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int):
            raise ValueError("max_result_bytes must be an integer")
        if max_result_bytes < 1 or max_result_bytes > MAX_RESULT_BYTES:
            raise ValueError(f"max_result_bytes must be between 1 and {MAX_RESULT_BYTES}")
        self._ledger: ResultCommittingLedger = ledger
        self._registry = registry
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._commit_clock = clock
        self._max_records = max_records
        self._max_result_bytes = max_result_bytes

    def submit(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Build and enqueue one immutable request for a reviewed enabled job."""
        job = self._require_runnable(job_id, version)
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a validated RunRequest")
        identity = build_run_identity(job.task, request)
        return self._ledger.enqueue(identity.idempotency_key, identity.canonical_json)

    def submit_one_shot(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Enqueue an explicitly requested reviewed run with a no-retry claim policy.

        Legacy ledger doubles remain valid for :meth:`submit`, but this API
        requires the exact :class:`RunLedger` and checks its captured original
        ``enqueue`` and ``claim_run`` methods before the one-shot policy is
        written. Execution-time ``heartbeat``, ``commit_result``, ``fail`` and
        ``get`` calls still use the trusted injected ledger object; this is not
        protection against malicious code that mutates the running process.
        """
        job = self._registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not registered")
        if not job.reviewed:
            raise ValueError("job version has not been reviewed")
        if job.one_shot_only:
            if not isinstance(request, RunRequest):
                raise TypeError("request must be a validated RunRequest")
            if job.task.enabled or job.task.schedule is not None:
                raise ValueError("one-shot-only job must be disabled and unscheduled")
            if request.run_type != "manual":
                raise ValueError("one-shot-only jobs require a manual run request")
        else:
            if not job.task.enabled:
                raise ValueError("job version is disabled")
            if not isinstance(request, RunRequest):
                raise TypeError("request must be a validated RunRequest")
        identity = build_run_identity(job.task, request)
        ledger = _require_unmodified_run_ledger(self._ledger)
        record = _RUN_LEDGER_ENQUEUE_METHOD(
            ledger,
            identity.idempotency_key,
            identity.canonical_json,
            claim_policy="one_shot",
        )
        if (
            getattr(record, "run_key", None) != identity.idempotency_key
            or getattr(record, "job_snapshot_json", None) != identity.canonical_json
            or getattr(record, "claim_policy", None) != "one_shot"
        ):
            raise RuntimeError("ledger did not persist the requested one-shot run contract")
        return record

    def submit_execution(self, job_id: str, version: str, request: RunRequest) -> RunRecord:
        """Enqueue one exact backfill request for a reviewed execution-only job.

        Execution-only jobs remain disabled and unscheduled, so this method is
        the only submission path that can reach their handlers. The row uses
        the ledger's recoverable claim policy because the persistent worker
        claims queued work through :meth:`run_once`; callers must treat an
        expired ``running`` row as uncertain rather than reclaiming it.
        """
        job = self._registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not registered")
        if not job.reviewed:
            raise ValueError("job version has not been reviewed")
        if not job.execution_only:
            raise ValueError("job version is not execution-only")
        if job.task.enabled or job.task.schedule is not None:
            raise ValueError("execution-only job must be disabled and unscheduled")
        if not isinstance(request, RunRequest):
            raise TypeError("request must be a validated RunRequest")
        if request.run_type != "backfill":
            raise ValueError("execution-only jobs require a backfill run request")
        identity = build_run_identity(job.task, request)
        ledger = _require_unmodified_run_ledger(self._ledger)
        record = _RUN_LEDGER_ENQUEUE_METHOD(
            ledger,
            identity.idempotency_key,
            identity.canonical_json,
            claim_policy="recover_expired",
        )
        if (
            getattr(record, "run_key", None) != identity.idempotency_key
            or getattr(record, "job_snapshot_json", None) != identity.canonical_json
            or getattr(record, "claim_policy", None) != "recover_expired"
        ):
            raise RuntimeError("ledger did not persist the requested execution-only run contract")
        return record

    def run_once(self, worker: str, ttl: timedelta) -> RunOutcome | None:
        """Claim and execute at most one queued request."""
        claim_now = self._now()
        record = self._ledger.claim(worker, claim_now, ttl)
        if record is None:
            return None
        return self._execute_claimed(record, worker, ttl)

    def run_one_shot(self, run_key: str, worker: str, ttl: timedelta) -> RunOutcome | None:
        """Claim and execute only the exact initial one-shot ledger row.

        This path never falls back to the broad queue claim. An expired
        one-shot lease raises the ledger's uncertainty error unchanged because
        its external effect may already have occurred.
        """
        ledger = _require_unmodified_run_ledger(self._ledger)
        record = _RUN_LEDGER_CLAIM_RUN_METHOD(
            ledger,
            run_key,
            worker,
            self._now(),
            ttl,
        )
        if record is None:
            return None
        lease_id = getattr(record, "lease_id", None)
        if (
            getattr(record, "run_key", None) != run_key
            or getattr(record, "claim_policy", None) != "one_shot"
            or getattr(record, "status", None) != "running"
            or getattr(record, "worker", None) != worker
            or getattr(record, "fencing_token", None) != 1
            or not isinstance(lease_id, str)
            or re.fullmatch(r"[0-9a-f]{32}", lease_id) is None
            or getattr(record, "started_at", None) is None
            or getattr(record, "heartbeat_at", None) is None
            or getattr(record, "lease_expires_at", None) is None
            or getattr(record, "finished_at", None) is not None
            or getattr(record, "error_text", None) is not None
        ):
            raise RuntimeError("ledger returned an invalid exact one-shot claim")
        return self._execute_claimed(record, worker, ttl)

    def _execute_claimed(self, record: RunRecord, worker: str, ttl: timedelta) -> RunOutcome:
        """Validate a stored snapshot, run handlers, then publish atomically."""
        try:
            job, request, snapshot_identity = self._resolve_snapshot(record)
            if not job.reviewed:
                raise _JobNotRunnableError("job_not_reviewed")
            if job.execution_only:
                if (
                    job.task.enabled
                    or job.task.schedule is not None
                    or request.run_type != "backfill"
                    or record.claim_policy not in {"recover_expired", "one_shot"}
                ):
                    raise _JobNotRunnableError("execution_only_contract_invalid")
            elif job.one_shot_only:
                if (
                    job.task.enabled
                    or job.task.schedule is not None
                    or request.run_type != "manual"
                    or record.claim_policy != "one_shot"
                ):
                    raise _JobNotRunnableError("one_shot_only_contract_invalid")
            elif not job.task.enabled:
                raise _JobNotRunnableError("job_disabled")
            context = self._execution_context(record, worker, ttl, snapshot_identity)
            context.checkpoint()
            raw = job.fetch(request, context)
            context.checkpoint()
            batch = job.normalize(raw, request, context)
            context.checkpoint()
            if type(batch) is not NormalizedBatch:
                raise _ResultInvalidError("normalized_batch_invalid")
            if batch.commit_context is not None:
                raise _CommitPayloadInvalidError("commit_context_prepopulated")
            commit_payload = batch.commit_payload
            commit_contract = self._preflight_commit_payload(job, commit_payload)
            decoded_context: object | None = None
            if commit_contract.validator is not None:
                self._validate_commit_payload(commit_payload, commit_contract.validator)
            elif commit_contract.decoder is not None:
                decoded_context = self._decode_commit_payload(commit_payload, commit_contract)
            batch = self._validate_batch(
                batch,
                checkpoint=context.checkpoint_cooperatively,
                commit_payload=commit_payload,
            )
            if decoded_context is not None:
                batch = replace(batch, commit_payload=None, commit_context=decoded_context)
            context.checkpoint()

            def publish(connection: Connection) -> PublicationReceipt:
                receipt = job.publish(
                    connection,
                    job.task,
                    request,
                    batch,
                    record.run_key,
                )
                return _validate_receipt(receipt, job.task.dataset_id, len(batch.records))

            committed = self._ledger.commit_result(
                record.run_key,
                worker=worker,
                fencing_token=record.fencing_token,
                now=self._now(),
                operation=publish,
                clock=self._commit_clock,
            )
            if committed is None:
                return RunOutcome(self._ledger.get(record.run_key), None)
            succeeded_record, receipt = committed
            return RunOutcome(succeeded_record, receipt)
        except Exception as exc:
            error_text = _safe_error_code(exc)
            failed_record: RunRecord | None
            try:
                failed_record = self._ledger.fail(
                    record.run_key,
                    worker,
                    record.fencing_token,
                    self._now(),
                    error_text,
                )
            except Exception:
                failed_record = None
            if failed_record is None:
                failed_record = self._ledger.get(record.run_key)
            return RunOutcome(failed_record, None)

    def _require_runnable(self, job_id: str, version: str) -> ReviewedJob:
        """Resolve exact version and fail closed for unreviewed or disabled jobs."""
        job = self._registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not registered")
        if not job.reviewed:
            raise ValueError("job version has not been reviewed")
        if job.execution_only:
            raise ValueError("job version is execution-only")
        if job.one_shot_only:
            raise ValueError("job version is one-shot-only")
        if not job.task.enabled:
            raise ValueError("job version is disabled")
        return job

    def _execution_context(
        self,
        record: RunRecord,
        worker: str,
        ttl: timedelta,
        snapshot_identity: _VerifiedRunSnapshot,
    ) -> RunExecutionContext:
        """Bind lease renewals to one claimed record and fencing token."""
        if (
            type(snapshot_identity) is not _VerifiedRunSnapshot
            or snapshot_identity._issuer is not _RUN_SNAPSHOT_IDENTITY_ISSUER
            or snapshot_identity.run_key != record.run_key
        ):
            raise ValueError("run snapshot identity is invalid")

        def checkpoint() -> None:
            renewed = self._ledger.heartbeat(
                record.run_key,
                worker,
                record.fencing_token,
                self._now(),
                ttl,
            )
            if renewed is None:
                raise _LeaseLostError("run lease is no longer active")

        return RunExecutionContext(
            run_key=record.run_key,
            worker=worker,
            fencing_token=record.fencing_token,
            _checkpoint=checkpoint,
            _cooperative_checkpoint=_CooperativeCheckpoint(
                checkpoint,
                # Keep heartbeat work to at most one fifth of the active lease.
                interval_seconds=min(0.25, ttl.total_seconds() / 5),
                immediate=False,
            ),
            _snapshot_identity=snapshot_identity,
        )

    def _resolve_snapshot(
        self, record: RunRecord
    ) -> tuple[ReviewedJob, RunRequest, _VerifiedRunSnapshot]:
        """Parse and verify a bounded canonical snapshot against the registry."""
        snapshot = _parse_snapshot(record.job_snapshot_json)
        job_data = snapshot["job"]
        run_data = snapshot["run"]
        if not isinstance(job_data, dict) or not isinstance(run_data, dict):
            raise _SnapshotInvalidError("snapshot_sections_invalid")
        job_id = _bounded_identifier(job_data.get("job_id"))
        version = _bounded_identifier(job_data.get("version"))
        job = self._registry.resolve(job_id, version)
        if job is None:
            raise _SnapshotInvalidError("job_version_not_registered")
        expected_job = {
            "job_id": job.task.job_id,
            "version": job.task.version,
            "dataset_id": job.task.dataset_id,
            "provider": job.task.provider,
            "model": job.task.model,
        }
        if job_data != expected_job:
            raise _SnapshotInvalidError("job_snapshot_mismatch")
        request = _request_from_snapshot(snapshot, record.run_key)
        snapshot_identity = _VerifiedRunSnapshot(
            run_key=record.run_key,
            payload_hash=hashlib.sha256(record.job_snapshot_json.encode("utf-8")).hexdigest(),
            canonical_json=record.job_snapshot_json,
            _issuer=_RUN_SNAPSHOT_IDENTITY_ISSUER,
        )
        return job, request, snapshot_identity

    def _validate_batch(
        self,
        batch: NormalizedBatch,
        *,
        checkpoint: Callable[[], None] | None = None,
        commit_payload: object = _UNSET_COMMIT_PAYLOAD,
    ) -> NormalizedBatch:
        """Copy and enforce record, field, credential, and encoded-size bounds."""
        if type(batch) is not NormalizedBatch:
            raise _ResultInvalidError("normalized_batch_invalid")
        if batch.commit_context is not None:
            raise _CommitPayloadInvalidError("commit_context_prepopulated")
        sidecar = (
            batch.commit_payload if commit_payload is _UNSET_COMMIT_PAYLOAD else commit_payload
        )
        if sidecar is not None and type(sidecar) is not bytes:
            raise _ResultInvalidError("commit_payload_type_invalid")
        if not isinstance(batch.records, tuple):
            raise _ResultInvalidError("normalized_records_must_be_immutable")
        if len(batch.records) > self._max_records:
            raise _ResultInvalidError("result_record_limit_exceeded")
        _validate_text(batch.schema_version, "schema_version", max_length=64)
        _validate_text(batch.semantic_version, "semantic_version", max_length=64)
        _validate_text(batch.unit, "unit", max_length=128)
        _validate_text(batch.adjustment_basis, "adjustment_basis", max_length=128)
        if not isinstance(batch.quality, Mapping) or not isinstance(batch.provenance, Mapping):
            raise _ResultInvalidError("result_metadata_invalid")
        if batch.watermark is not None and not isinstance(batch.watermark, Mapping):
            raise _ResultInvalidError("result_watermark_invalid")
        node_count = [0]
        safe_quality = _validate_json_object(
            batch.quality, "quality", node_count, checkpoint=checkpoint
        )
        safe_provenance = _validate_json_object(
            batch.provenance, "provenance", node_count, checkpoint=checkpoint
        )
        safe_watermark = (
            _validate_json_object(batch.watermark, "watermark", node_count, checkpoint=checkpoint)
            if batch.watermark is not None
            else None
        )
        seen: set[str] = set()
        result: dict[str, JsonValue] = {
            "schema_version": batch.schema_version,
            "semantic_version": batch.semantic_version,
            "unit": batch.unit,
            "adjustment_basis": batch.adjustment_basis,
            "quality": safe_quality,
            "provenance": safe_provenance,
            "records": [],
        }
        if safe_watermark is not None:
            result["watermark"] = safe_watermark
        encoded_records: list[JsonValue] = []
        safe_records: list[NormalizedRecord] = []
        for index, row in enumerate(batch.records):
            if checkpoint is not None:
                checkpoint()
            if not isinstance(row, NormalizedRecord):
                raise _ResultInvalidError("normalized_record_invalid")
            _validate_text(row.business_key, "business_key", max_length=512)
            if row.business_key in seen:
                raise _ResultInvalidError("duplicate_business_key")
            seen.add(row.business_key)
            _validate_optional_date(row.business_date)
            for field_name in ("event_at", "observed_at", "source_published_at", "available_at"):
                _validate_optional_datetime(getattr(row, field_name), field_name)
            if not isinstance(row.payload, Mapping):
                raise _ResultInvalidError("record_payload_invalid")
            payload = _validate_json_object(
                row.payload,
                f"records[{index}].payload",
                node_count,
                checkpoint=checkpoint,
            )
            encoded_records.append(
                {
                    "business_key": row.business_key,
                    "payload": payload,
                    "business_date": (
                        row.business_date.isoformat() if row.business_date is not None else None
                    ),
                    "event_at": _datetime_json(row.event_at),
                    "observed_at": _datetime_json(row.observed_at),
                    "source_published_at": _datetime_json(row.source_published_at),
                    "available_at": _datetime_json(row.available_at),
                }
            )
            safe_records.append(
                NormalizedRecord(
                    business_key=row.business_key,
                    payload=payload,
                    business_date=row.business_date,
                    event_at=row.event_at,
                    observed_at=row.observed_at,
                    source_published_at=row.source_published_at,
                    available_at=row.available_at,
                )
            )
        result["records"] = encoded_records
        encoded_size = _result_json_size(result, checkpoint=checkpoint)
        if encoded_size > self._max_result_bytes:
            raise _ResultInvalidError("result_size_limit_exceeded")
        return NormalizedBatch(
            records=tuple(safe_records),
            schema_version=batch.schema_version,
            semantic_version=batch.semantic_version,
            unit=batch.unit,
            adjustment_basis=batch.adjustment_basis,
            quality=safe_quality,
            provenance=safe_provenance,
            watermark=safe_watermark,
            commit_payload=cast("bytes | None", sidecar),
        )

    @staticmethod
    def _preflight_commit_payload(
        job: ReviewedJob, payload: bytes | None
    ) -> _CommitPayloadContract:
        """Check payload declaration, presence, and size before record copies."""
        if payload is not None and type(payload) is not bytes:
            raise _ResultInvalidError("commit_payload_type_invalid")

        contract = _validate_commit_payload_declaration(job)
        if contract is None:
            if payload is not None:
                raise _CommitPayloadInvalidError("commit_payload_not_registered")
            return _CommitPayloadContract()
        if payload is None:
            raise _CommitPayloadInvalidError("commit_payload_missing")
        max_bytes = job.max_commit_payload_bytes
        if (
            type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_COMMIT_PAYLOAD_BYTES
            or len(payload) > max_bytes
        ):
            raise _CommitPayloadInvalidError("commit_payload_size_invalid")
        return contract

    @staticmethod
    def _validate_commit_payload(
        payload: bytes | None,
        validator: Callable[[bytes], bool] | None,
    ) -> None:
        """Run the registered semantic validator outside the commit transaction."""
        if validator is None:
            return
        if type(payload) is not bytes:
            raise _CommitPayloadInvalidError("commit_payload_type_invalid")
        try:
            accepted = validator(payload)
        except Exception:
            raise _CommitPayloadInvalidError("commit_payload_validation_failed") from None
        if type(accepted) is not bool or accepted is not True:
            raise _CommitPayloadInvalidError("commit_payload_validation_failed")

    @staticmethod
    def _decode_commit_payload(
        payload: bytes | None,
        contract: _CommitPayloadContract,
    ) -> object:
        """Decode one bounded payload into its exact allowlisted immutable DTO."""
        decoder = contract.decoder
        context_type = contract.context_type
        if type(payload) is not bytes:
            raise _CommitPayloadInvalidError("commit_payload_type_invalid")
        if decoder is None or context_type is None:
            raise _CommitPayloadInvalidError("commit_payload_not_registered")
        try:
            decoded = decoder(payload)
        except Exception:
            raise _CommitPayloadInvalidError("commit_payload_validation_failed") from None
        if type(decoded) is not context_type:
            raise _CommitPayloadInvalidError("commit_payload_validation_failed")
        return decoded

    def _now(self) -> datetime:
        """Require the injected clock to return an aware UTC instant."""
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("runner clock must return an aware UTC datetime")
        return now.astimezone(timezone.utc)


class _JobNotRunnableError(RuntimeError):
    """Internal, sanitized refusal for a job outside its reviewed state."""


class _LeaseLostError(RuntimeError):
    """Internal, sanitized stop after a lease renewal fails."""


class _SnapshotInvalidError(RuntimeError):
    """Internal, sanitized refusal for a malformed or stale snapshot."""


class _ResultInvalidError(RuntimeError):
    """Internal, sanitized refusal for an invalid or oversized result."""


class _CommitPayloadInvalidError(RuntimeError):
    """Internal, sanitized refusal for an invalid ephemeral commit payload."""


def _validate_commit_payload_declaration(job: ReviewedJob) -> _CommitPayloadContract | None:
    """Validate a complete validator-only or allowlisted decoder contract."""
    payload_type = job.commit_payload_type
    validator = job.validate_commit_payload
    max_bytes = job.max_commit_payload_bytes
    decoder = job.decode_commit_payload
    context_type = job.commit_context_type
    if (
        payload_type is None
        and validator is None
        and max_bytes is None
        and decoder is None
        and context_type is None
    ):
        return None
    if payload_type is not bytes:
        raise TypeError("commit payload type must be exactly bytes")
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_COMMIT_PAYLOAD_BYTES:
        raise ValueError(
            f"max_commit_payload_bytes must be between 1 and {MAX_COMMIT_PAYLOAD_BYTES}"
        )
    if validator is not None:
        if decoder is not None or context_type is not None:
            raise TypeError("sidecar validator and decoder modes cannot be combined")
        if not _is_sync_validator(validator):
            raise TypeError("commit payload validator must be an explicit function")
        return _CommitPayloadContract(validator=validator)
    if decoder is None or context_type is None:
        raise TypeError("commit payload decoder and context type must be declared together")
    expected_decoder, expected_context_type = _prepared_capture_decoder_contract()
    if type(decoder) is not FunctionType or decoder is not expected_decoder:
        raise TypeError("commit payload decoder must be the reviewed prepared-capture decoder")
    if context_type is not expected_context_type:
        raise TypeError("commit context type must be the exact PreparedCapture type")
    return _CommitPayloadContract(decoder=decoder, context_type=context_type)


def _prepared_capture_decoder_contract() -> tuple[FunctionType, type[object]]:
    """Lazily load the sole decoder/output pair allowed for transaction context.

    The imports stay inside this helper so importing the generic runner does
    not create a module cycle with the financial archive store. The caller
    compares both declarations by identity and never invokes arbitrary job
    decoders.
    """
    from opendata.pipeline.financial_observation_store import PreparedCapture
    from opendata.pipeline.financial_prepared_codec import decode_prepared_capture

    return decode_prepared_capture, PreparedCapture


def _is_sync_validator(value: object) -> TypeGuard[Callable[[bytes], bool]]:
    """Recognize plain synchronous functions without invoking custom callables."""
    return type(value) is FunctionType and not (
        inspect.iscoroutinefunction(value)
        or inspect.isgeneratorfunction(value)
        or inspect.isasyncgenfunction(value)
    )


def _supports_ledger_protocol(value: object) -> bool:
    """Check required ledger methods without accepting unrelated duck types."""
    return all(
        callable(getattr(value, name, None))
        for name in ("enqueue", "claim", "heartbeat", "fail", "get", "commit_result")
    )


def _require_unmodified_run_ledger(value: object) -> RunLedger:
    """Require the original enqueue and exact-key claim implementations.

    One-shot paths fail before enqueue or exact-claim side effects when given a
    duck type or when ``enqueue`` / ``claim_run`` are overridden. Other ledger
    methods used during execution remain trusted and are not checked here.
    """
    if type(value) is not RunLedger:
        raise TypeError("one-shot operations require the unmodified RunLedger type")
    if (
        "enqueue" in vars(value)
        or "claim_run" in vars(value)
        or RunLedger.enqueue is not _RUN_LEDGER_ENQUEUE_METHOD
        or RunLedger.claim_run is not _RUN_LEDGER_CLAIM_RUN_METHOD
    ):
        raise TypeError("one-shot operations require unmodified RunLedger methods")
    return value


def _parse_snapshot(snapshot_json: str) -> dict[str, object]:
    """Decode a small canonical snapshot with duplicate-key rejection."""
    if not isinstance(snapshot_json, str):
        raise _SnapshotInvalidError("snapshot_size_invalid")
    try:
        snapshot_size = len(snapshot_json.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _SnapshotInvalidError("snapshot_json_invalid") from exc
    if snapshot_size > 65_536:
        raise _SnapshotInvalidError("snapshot_size_invalid")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        values: dict[str, object] = {}
        for key, value in pairs:
            if key in values:
                raise _SnapshotInvalidError("snapshot_duplicate_key")
            values[key] = value
        return values

    def reject_constant(_: str) -> None:
        raise _SnapshotInvalidError("snapshot_nonstandard_number")

    try:
        parsed: object = json.loads(
            snapshot_json,
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeEncodeError) as exc:
        raise _SnapshotInvalidError("snapshot_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise _SnapshotInvalidError("snapshot_root_invalid")
    try:
        canonical = json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _SnapshotInvalidError("snapshot_json_invalid") from exc
    if canonical != snapshot_json:
        raise _SnapshotInvalidError("snapshot_not_canonical")
    if (
        set(parsed) != {"contract_version", "job", "run"}
        or type(parsed["contract_version"]) is not int
        or parsed["contract_version"] != 1
    ):
        raise _SnapshotInvalidError("snapshot_contract_invalid")
    job = parsed.get("job")
    if not isinstance(job, dict) or set(job) != {
        "job_id",
        "version",
        "dataset_id",
        "provider",
        "model",
    }:
        raise _SnapshotInvalidError("snapshot_job_invalid")
    run = parsed.get("run")
    if not isinstance(run, dict) or set(run) != {
        "run_type",
        "occurrence",
        "request_key_sha256",
        "backfill_range",
        "shard",
        "business_request",
    }:
        raise _SnapshotInvalidError("snapshot_run_invalid")
    business_request = run.get("business_request")
    if not isinstance(business_request, dict) or set(business_request) != {"query", "partition"}:
        raise _SnapshotInvalidError("snapshot_request_invalid")
    if not isinstance(business_request.get("query"), dict) or not isinstance(
        business_request.get("partition"), dict
    ):
        raise _SnapshotInvalidError("snapshot_request_invalid")
    return parsed


def _request_from_snapshot(snapshot: dict[str, object], run_key: str) -> RunRequest:
    """Validate snapshot payload and derive a synthetic stable request token."""
    run_value = snapshot["run"]
    if not isinstance(run_value, dict):
        raise _SnapshotInvalidError("snapshot_request_invalid")
    run = run_value
    business_value = run.get("business_request")
    if not isinstance(business_value, dict):
        raise _SnapshotInvalidError("snapshot_request_invalid")
    business = business_value
    run_type = run.get("run_type")
    if run_type not in {"scheduled", "manual", "backfill"}:
        raise _SnapshotInvalidError("snapshot_run_type_invalid")
    occurrence_value = run.get("occurrence")
    request_digest = run.get("request_key_sha256")
    backfill_value = run.get("backfill_range")
    shard_value = run.get("shard")
    if shard_value is not None:
        _bounded_identifier(shard_value)
    if run_type == "scheduled":
        if not isinstance(occurrence_value, str) or request_digest is not None:
            raise _SnapshotInvalidError("snapshot_occurrence_invalid")
        occurrence = _parse_utc_datetime(occurrence_value)
        if backfill_value is not None:
            raise _SnapshotInvalidError("snapshot_backfill_invalid")
        idempotency_key = None
        backfill_start = None
        backfill_end = None
    else:
        if occurrence_value is not None or not isinstance(request_digest, str):
            raise _SnapshotInvalidError("snapshot_request_key_invalid")
        if not re.fullmatch(r"[0-9a-f]{64}", request_digest):
            raise _SnapshotInvalidError("snapshot_request_key_invalid")
        if _expected_run_key(snapshot, request_digest) != run_key:
            raise _SnapshotInvalidError("snapshot_run_key_mismatch")
        idempotency_key = f"sha256-{request_digest}"
        occurrence = None
        if run_type == "manual":
            if backfill_value is not None:
                raise _SnapshotInvalidError("snapshot_backfill_invalid")
            backfill_start = None
            backfill_end = None
        else:
            if (
                not isinstance(backfill_value, dict)
                or set(backfill_value) != {"start", "end"}
                or not isinstance(backfill_value.get("start"), str)
                or not isinstance(backfill_value.get("end"), str)
            ):
                raise _SnapshotInvalidError("snapshot_backfill_invalid")
            try:
                backfill_start = date.fromisoformat(backfill_value["start"])
                backfill_end = date.fromisoformat(backfill_value["end"])
            except ValueError as exc:
                raise _SnapshotInvalidError("snapshot_backfill_invalid") from exc
            if (
                backfill_start.isoformat() != backfill_value["start"]
                or backfill_end.isoformat() != backfill_value["end"]
            ):
                raise _SnapshotInvalidError("snapshot_backfill_invalid")
    if run_type == "scheduled" and _expected_scheduled_key(snapshot) != run_key:
        raise _SnapshotInvalidError("snapshot_run_key_mismatch")
    return RunRequest.model_validate(
        {
            "contract_version": 1,
            "run_type": run_type,
            "query": business["query"],
            "partition": business["partition"],
            "shard": shard_value,
            "occurrence": occurrence,
            "idempotency_key": idempotency_key,
            "backfill_start": backfill_start,
            "backfill_end": backfill_end,
        }
    )


def _expected_scheduled_key(snapshot: dict[str, object]) -> str:
    """Recompute a scheduled natural-slot key from its canonical payload."""
    job_value = snapshot["job"]
    run_value = snapshot["run"]
    if not isinstance(job_value, dict) or not isinstance(run_value, dict):
        raise _SnapshotInvalidError("snapshot_slot_invalid")
    job = job_value
    run = run_value
    business_value = run.get("business_request")
    if not isinstance(business_value, dict):
        raise _SnapshotInvalidError("snapshot_slot_invalid")
    business = business_value
    occurrence = run.get("occurrence")
    if not isinstance(occurrence, str):
        raise _SnapshotInvalidError("snapshot_slot_invalid")
    slot = {
        "contract_version": 1,
        "job_id": job.get("job_id"),
        "version": job.get("version"),
        "run_type": "scheduled",
        "slot": {
            "occurrence": occurrence,
            "partition": business.get("partition"),
            "shard": run.get("shard"),
        },
    }
    return _sha256(_canonical_json(slot).encode("utf-8"))


def _expected_run_key(snapshot: dict[str, object], request_digest: str) -> str:
    """Recompute manual/backfill request identity without retaining the key."""
    job = snapshot["job"]
    run = snapshot["run"]
    if not isinstance(job, dict) or not isinstance(run, dict):
        raise _SnapshotInvalidError("snapshot_slot_invalid")
    slot = {
        "contract_version": 1,
        "job_id": job.get("job_id"),
        "version": job.get("version"),
        "run_type": run.get("run_type"),
        "request_key_sha256": request_digest,
    }
    return _sha256(_canonical_json(slot).encode("utf-8"))


def _validate_json_object(
    value: Mapping[str, object],
    field: str,
    node_count: list[int],
    *,
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, JsonValue]:
    """Copy a bounded JSON object while rejecting likely credentials."""
    if not isinstance(value, Mapping):
        raise _ResultInvalidError(f"{field}_invalid")
    result: dict[str, JsonValue] = {}
    for key, nested in value.items():
        if checkpoint is not None:
            checkpoint()
        if (
            not isinstance(key, str)
            or not key
            or len(key) > MAX_RESULT_TEXT
            or any(ord(character) < 32 for character in key)
        ):
            raise _ResultInvalidError(f"{field}_invalid")
        normalized_key = key.casefold().replace("_", "").replace("-", "")
        if any(marker in normalized_key for marker in _SECRET_KEY_MARKERS):
            raise _ResultInvalidError("credential_field_in_result")
        result[key] = _validate_json_value(
            nested, f"{field}.{key}", 0, node_count, checkpoint=checkpoint
        )
    return result


def _validate_json_value(
    value: object,
    path: str,
    depth: int,
    node_count: list[int],
    *,
    checkpoint: Callable[[], None] | None = None,
) -> JsonValue:
    """Copy JSON values with bounded shape, strings, and credential checks."""
    if checkpoint is not None:
        checkpoint()
    node_count[0] += 1
    if node_count[0] > MAX_RESULT_NODES or depth > MAX_RESULT_DEPTH:
        raise _ResultInvalidError("result_structure_limit_exceeded")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _ResultInvalidError("result_nonfinite_number")
        return value
    if isinstance(value, str):
        if len(value) > MAX_RESULT_TEXT:
            raise _ResultInvalidError("result_text_limit_exceeded")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
            raise _ResultInvalidError("result_control_character")
        if _BEARER_VALUE.match(value) or _COMMON_SECRET_VALUE.fullmatch(value):
            raise _ResultInvalidError("credential_value_in_result")
        return value
    if type(value) is list or isinstance(value, tuple):
        if len(value) > MAX_RESULT_RECORDS:
            raise _ResultInvalidError("result_array_limit_exceeded")
        copied: list[JsonValue] = []
        for index, item in enumerate(value):
            copied.append(
                _validate_json_value(
                    item,
                    f"{path}[{index}]",
                    depth + 1,
                    node_count,
                    checkpoint=checkpoint,
                )
            )
        return copied
    if isinstance(value, Mapping):
        if len(value) > MAX_RESULT_RECORDS:
            raise _ResultInvalidError("result_object_limit_exceeded")
        nested: dict[str, JsonValue] = {}
        for key, item in value.items():
            if checkpoint is not None:
                checkpoint()
            if (
                not isinstance(key, str)
                or not key
                or len(key) > MAX_RESULT_TEXT
                or any(ord(character) < 32 for character in key)
            ):
                raise _ResultInvalidError("result_object_key_invalid")
            normalized_key = key.casefold().replace("_", "").replace("-", "")
            if any(marker in normalized_key for marker in _SECRET_KEY_MARKERS):
                raise _ResultInvalidError("credential_field_in_result")
            nested[key] = _validate_json_value(
                item,
                f"{path}.{key}",
                depth + 1,
                node_count,
                checkpoint=checkpoint,
            )
        return nested
    raise _ResultInvalidError("result_value_type_unsupported")


def _result_json_size(value: object, *, checkpoint: Callable[[], None] | None = None) -> int:
    """Measure canonical result JSON incrementally so large batches can renew leases."""
    encoder = json.JSONEncoder(
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    iterator = encoder.iterencode(value)
    size = 0
    while True:
        try:
            text_chunk = next(iterator)
        except StopIteration:
            break
        except (TypeError, ValueError) as exc:
            raise _ResultInvalidError("result_not_json_serializable") from exc
        try:
            size += len(text_chunk.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise _ResultInvalidError("result_not_json_serializable") from exc
        if checkpoint is not None:
            checkpoint()
    return size


def _validate_text(value: object, field: str, *, max_length: int = 256) -> None:
    """Require nonempty bounded result metadata without control characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise _ResultInvalidError(f"{field}_invalid")
    if any(ord(character) < 32 for character in value):
        raise _ResultInvalidError(f"{field}_invalid")
    if _BEARER_VALUE.match(value) or _COMMON_SECRET_VALUE.fullmatch(value):
        raise _ResultInvalidError("credential_value_in_result")


def _validate_optional_date(value: object) -> None:
    """Validate an optional business date without accepting datetimes."""
    if value is not None and (not isinstance(value, date) or isinstance(value, datetime)):
        raise _ResultInvalidError("business_date_invalid")


def _validate_optional_datetime(value: object, field: str) -> None:
    """Require optional observations to carry an explicit timezone."""
    if value is not None and (
        not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None
    ):
        raise _ResultInvalidError(f"{field}_invalid")


def _datetime_json(value: datetime | None) -> str | None:
    """Serialize optional timestamps in UTC for result-size measurement."""
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _validate_receipt(
    receipt: object, expected_dataset_id: str, expected_record_count: int
) -> PublicationReceipt:
    """Require a typed receipt matching the registered dataset and row count."""
    if not isinstance(receipt, PublicationReceipt):
        raise _ResultInvalidError("publisher_receipt_invalid")
    _validate_text(receipt.publication_id, "publication_id", max_length=128)
    _validate_text(receipt.dataset_id, "publication_dataset_id", max_length=128)
    if receipt.dataset_id != expected_dataset_id:
        raise _ResultInvalidError("publisher_dataset_mismatch")
    if isinstance(receipt.record_count, bool) or receipt.record_count != expected_record_count:
        raise _ResultInvalidError("publisher_record_count_mismatch")
    return receipt


def _bounded_identifier(value: object) -> str:
    """Validate snapshot identifiers using the contract's bounded shape."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value[0].isalnum()
        or any(character not in _IDENTIFIER_CHARACTERS for character in value)
    ):
        raise _SnapshotInvalidError("snapshot_identifier_invalid")
    return value


def _parse_utc_datetime(value: str) -> datetime:
    """Parse only the canonical UTC timestamp form produced by run_identity."""
    if not value.endswith("Z"):
        raise _SnapshotInvalidError("snapshot_occurrence_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise _SnapshotInvalidError("snapshot_occurrence_invalid") from exc
    if (
        parsed.utcoffset() != timedelta(0)
        or parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value
    ):
        raise _SnapshotInvalidError("snapshot_occurrence_invalid")
    return parsed


def _canonical_json(value: object) -> str:
    """Serialize a JSON value with the canonical run identity settings."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: bytes) -> str:
    """Return a lowercase SHA-256 digest."""
    return hashlib.sha256(value).hexdigest()


def _safe_error_code(error: BaseException) -> str:
    """Return an exception class code without message, traceback, or secrets."""
    name = type(error).__name__
    if not _ERROR_NAME.fullmatch(name):
        name = "UnhandledError"
    return f"execution_failed:{name}"


__all__ = [
    "MAX_COMMIT_PAYLOAD_BYTES",
    "MAX_RESULT_BYTES",
    "MAX_RESULT_RECORDS",
    "NormalizedBatch",
    "NormalizedRecord",
    "PublicationReceipt",
    "ReviewedJob",
    "ReviewedJobRegistry",
    "RunExecutionContext",
    "RunOutcome",
    "RunService",
]
