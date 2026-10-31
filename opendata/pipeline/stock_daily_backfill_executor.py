"""Offline, resumable control plane for reviewed stock-daily backfill chunks.

This module turns an immutable :class:`StockDailyBackfillPlan` into a bounded
sequence of ledger-backed runs. It never loads a universe, reads ``.env``,
contacts a provider, registers an APScheduler job, enables the existing
disabled stock-daily task, or certifies full history. Execution requires an
explicit execution-only job and an explicit authorization token; dry-run is the
default and only inspects the ledger.

The executor is intentionally a single-invocation loop, not a scheduler. A
caller decides when to call :meth:`StockDailyBackfillExecutor.execute_once`.
Progress lives in the existing run ledger, keyed by the planner's deterministic
request identity. A rerun skips terminal successes and stops at the first
terminal failure, cancelled run, live lease owned by another worker, or
uncertain expired lease. It never invents a new identity for a failed chunk.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final, Literal, Protocol

from opendata.data.platform_contracts import DatasetContract, FieldContract, SourceIdentity
from opendata.data.providers.akshare._source import SOURCE
from opendata.pipeline.reviewed_snapshot_backfill import (
    build_stock_daily_backfill_plan_from_snapshot,
)
from opendata.pipeline.run_dispatch import RunDispatcher
from opendata.pipeline.run_identity import TaskDefinition, build_run_identity
from opendata.pipeline.run_ledger import RunLedger, RunRecord
from opendata.pipeline.run_service import ReviewedJobRegistry, RunOutcome
from opendata.pipeline.stock_daily_backfill_plan import (
    MAX_STOCK_DAILY_WINDOW_DAYS,
    StockDailyBackfillChunk,
    StockDailyBackfillPlan,
    StockDailyBackfillPlanIntegrityError,
    stock_daily_task_sha256,
    verify_stock_daily_backfill_plan,
)

EXECUTION_AUTHORIZATION: Final[str] = "execute-reviewed-stock-daily-backfill-v1"
STOCK_DAILY_EXECUTION_TARGET: Final[str] = "stock-daily-akshare"
STOCK_DAILY_EXECUTION_JOB_ID: Final[str] = "cn-stock-daily-backfill-execution"
STOCK_DAILY_EXECUTION_JOB_VERSION: Final[str] = "v1"
STOCK_DAILY_EXECUTION_DATASET_ID: Final[str] = "cn-equity-stock-daily"
STOCK_DAILY_EXECUTION_SOURCE_ID: Final[str] = "akshare.daily"
STOCK_DAILY_EXECUTION_SCHEMA_VERSION: Final[str] = "1.0.0"
STOCK_DAILY_EXECUTION_SEMANTIC_VERSION: Final[str] = "stock-daily-raw-v1"
MAX_CHUNKS_PER_INVOCATION: Final[int] = 1_000

ChunkStatus = Literal[
    "succeeded",
    "queued",
    "failed",
    "cancelled",
    "running",
    "uncertain",
    "missing",
]
ChunkAction = Literal[
    "executed",
    "observed_succeeded",
    "would_dispatch",
    "dispatched",
    "preview",
    "stopped",
]
StopReason = Literal[
    "plan_finished",
    "dry_run",
    "max_chunks_reached",
    "chunk_failed",
    "chunk_cancelled",
    "chunk_leased",
    "chunk_uncertain",
    "execution_unclaimed",
    "execution_error",
    "unexpected_run_claimed",
    "dispatch_rejected",
    "dispatch_not_persisted",
    "ledger_state_invalid",
]


class StockDailyBackfillExecutionError(ValueError):
    """Report one stable, path-free execution refusal code."""

    def __init__(self, code: str) -> None:
        """Store the machine-readable refusal code."""
        self.code = code
        super().__init__(code)


class RunOnceWorker(Protocol):
    """The single-run execution primitive accepted from a persistent worker."""

    def run_once(self) -> RunOutcome | None:
        """Claim and execute at most one queued ledger run."""
        ...  # pragma: no cover


@dataclass(frozen=True, slots=True)
class StockDailyBackfillChunkReceipt:
    """One per-chunk ledger observation with no request payload or secret data."""

    instrument_id: str
    chunk_index: int
    window_start: date
    window_end: date
    run_key: str
    status: ChunkStatus
    action: ChunkAction
    detail: str


@dataclass(frozen=True, slots=True)
class StockDailyBackfillExecutionReceipt:
    """Deterministic, resumable outcome for one bounded executor invocation.

    ``execution_authorized`` and ``full_history_ready`` are always false. The
    receipt describes local ledger progress only; it does not certify provider
    data, production MySQL behavior, full history, or a natural scheduling run.
    """

    plan_sha256: str
    snapshot_sha256: str | None
    dry_run: bool
    chunks_total: int
    chunks_considered: int
    chunks_executed: int
    chunks_skipped: int
    chunks_failed: int
    chunks_remaining: int
    stop_reason: StopReason
    chunks: tuple[StockDailyBackfillChunkReceipt, ...]

    @property
    def execution_authorized(self) -> bool:
        """Return false because a plan or receipt never authorizes live sends."""
        return False

    @property
    def full_history_ready(self) -> bool:
        """Return false because this control plane makes no completeness claim."""
        return False

    def to_dict(self) -> dict[str, object]:
        """Return a stable JSON-compatible receipt representation."""
        return {
            "plan_sha256": self.plan_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "dry_run": self.dry_run,
            "execution_authorized": self.execution_authorized,
            "full_history_ready": self.full_history_ready,
            "chunks_total": self.chunks_total,
            "chunks_considered": self.chunks_considered,
            "chunks_executed": self.chunks_executed,
            "chunks_skipped": self.chunks_skipped,
            "chunks_failed": self.chunks_failed,
            "chunks_remaining": self.chunks_remaining,
            "stop_reason": self.stop_reason,
            "chunks": [
                {
                    "instrument_id": chunk.instrument_id,
                    "chunk_index": chunk.chunk_index,
                    "window_start": chunk.window_start.isoformat(),
                    "window_end": chunk.window_end.isoformat(),
                    "run_key": chunk.run_key,
                    "status": chunk.status,
                    "action": chunk.action,
                    "detail": chunk.detail,
                }
                for chunk in self.chunks
            ],
        }

    def to_json(self) -> str:
        """Serialize the receipt as compact canonical JSON without local paths."""
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


class StockDailyBackfillExecutor:
    """Execute one immutable stock-daily plan through an execution-only job.

    The registry must contain the exact plan task marked ``reviewed=True`` and
    ``execution_only=True`` with the task still disabled and schedule-free. The
    dispatcher and ledger must target the same durable run queue; the executor
    verifies every submitted run key and snapshot against its own recomputation
    and stops instead of claiming success when they differ.
    """

    def __init__(
        self,
        *,
        task: TaskDefinition,
        registry: ReviewedJobRegistry,
        dispatcher: RunDispatcher,
        ledger: RunLedger,
        worker: RunOnceWorker,
        max_chunks_per_invocation: int = 1,
        max_failures: int = 1,
        dry_run: bool = True,
        execution_authorization: str | None = None,
    ) -> None:
        """Bind explicit job, queue, worker, limits, and authorization state.

        Args:
            task: Exact disabled task declaration used by the plan.
            registry: Registry containing the reviewed execution-only job.
            dispatcher: Dispatcher that owns execution-only backfill submission.
            ledger: Same durable run ledger the worker continues to use.
            worker: Object exposing ``run_once()``, normally a persistent worker.
            max_chunks_per_invocation: Maximum chunks dispatched/executed now.
            max_failures: Fixed at 1 by the fail-closed contract.
            dry_run: Preview ledger state without dispatch or execution.
            execution_authorization: Explicit token required when not dry-running.

        Raises:
            TypeError: If a dependency has the wrong explicit type.
            ValueError: If the job shape or a limit violates the contract.
            StockDailyBackfillExecutionError: If an authorization value is
                supplied but does not equal the reviewed execution token.
        """
        if not isinstance(task, TaskDefinition):
            raise TypeError("task must be a TaskDefinition")
        if not isinstance(registry, ReviewedJobRegistry):
            raise TypeError("registry must be a ReviewedJobRegistry")
        if not isinstance(dispatcher, RunDispatcher):
            raise TypeError("dispatcher must be a RunDispatcher")
        if not isinstance(ledger, RunLedger):
            raise TypeError("ledger must be a RunLedger")
        if not callable(getattr(worker, "run_once", None)):
            raise TypeError("worker must expose a callable run_once() method")
        if type(max_chunks_per_invocation) is not int or not (
            1 <= max_chunks_per_invocation <= MAX_CHUNKS_PER_INVOCATION
        ):
            raise ValueError(
                "max_chunks_per_invocation must be an integer between 1 and "
                f"{MAX_CHUNKS_PER_INVOCATION}"
            )
        if type(max_failures) is not int or max_failures != 1:
            raise ValueError("max_failures must be 1 for the fail-closed execution contract")
        if type(dry_run) is not bool:
            raise ValueError("dry_run must be a boolean")
        if execution_authorization is not None:
            if not isinstance(execution_authorization, str):
                raise TypeError("execution_authorization must be a string or None")
            if not hmac.compare_digest(execution_authorization, EXECUTION_AUTHORIZATION):
                raise StockDailyBackfillExecutionError("execution_authorization_invalid")

        job = registry.resolve(task.job_id, task.version)
        if job is None:
            raise ValueError("execution-only job version is not registered")
        if job.reviewed is not True or job.execution_only is not True:
            raise ValueError("job version is not a reviewed execution-only job")
        if job.task != task:
            raise ValueError("execution-only job task does not match the plan task")
        if task.enabled is not False or task.schedule is not None:
            raise ValueError("execution-only task must remain disabled and unscheduled")

        self._task = task
        self._registry = registry
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._worker = worker
        self._max_chunks = max_chunks_per_invocation
        self._dry_run = dry_run
        self._execution_authorization = execution_authorization

    @property
    def dry_run(self) -> bool:
        """Return whether this executor only previews ledger state."""
        return self._dry_run

    def execute_once(
        self,
        plan: StockDailyBackfillPlan,
        *,
        snapshot_sha256: str | None = None,
    ) -> StockDailyBackfillExecutionReceipt:
        """Inspect, dispatch, and execute a bounded prefix of one plan.

        The plan is independently verified before any ledger lookup. For each
        chunk in deterministic planner order the executor computes the run
        identity from the exact task and request, skips a terminal success,
        stops before any terminal failure, cancellation, live foreign lease,
        expired/uncertain lease, or unexpected ledger state, and otherwise
        dispatches a missing run and invokes ``worker.run_once()`` once.

        Args:
            plan: Immutable plan returned by the reviewed planner.
            snapshot_sha256: Optional exact snapshot byte digest reported in the
                receipt. Call :meth:`execute_snapshot_once` to bind those bytes.

        Returns:
            A deterministic receipt with per-chunk run keys and statuses.
            ``execution_authorized`` and ``full_history_ready`` are false.

        Raises:
            StockDailyBackfillPlanIntegrityError: If the plan or a chunk digest,
                request, identity, window, or containment check fails.
            StockDailyBackfillExecutionError: If execution is requested without
                the exact authorization token.
        """
        self._require_execution_authorization()
        if snapshot_sha256 is not None and not _is_sha256(snapshot_sha256):
            raise StockDailyBackfillPlanIntegrityError("snapshot_digest_invalid")
        verify_stock_daily_backfill_plan(plan)
        self._verify_plan_binding(plan)

        chunks = plan.candidate_chunks
        records: list[StockDailyBackfillChunkReceipt] = []
        considered = 0
        executed = 0
        skipped = 0
        failed = 0
        would_execute = 0
        stop_reason: StopReason = "plan_finished"

        for chunk in chunks:
            if self._dry_run:
                if would_execute >= self._max_chunks:
                    stop_reason = "max_chunks_reached"
                    break
            elif executed >= self._max_chunks:
                stop_reason = "max_chunks_reached"
                break

            identity = build_run_identity(self._task, chunk.request)
            run_key = identity.idempotency_key
            record = self._ledger.get(run_key)
            considered += 1
            if record is not None and record.job_snapshot_json != identity.canonical_json:
                raise StockDailyBackfillPlanIntegrityError("ledger_snapshot_mismatch")

            if record is not None and record.status == "succeeded":
                skipped += 1
                records.append(
                    _chunk_receipt(
                        chunk,
                        run_key,
                        "succeeded",
                        "observed_succeeded",
                        "terminal_succeeded",
                    )
                )
                continue
            if record is not None and record.status == "failed":
                failed += 1
                records.append(
                    _chunk_receipt(chunk, run_key, "failed", "stopped", "terminal_failed")
                )
                stop_reason = "chunk_failed"
                break
            if record is not None and record.status == "cancelled":
                records.append(
                    _chunk_receipt(chunk, run_key, "cancelled", "stopped", "terminal_cancelled")
                )
                stop_reason = "chunk_cancelled"
                break
            if record is not None and record.status == "running":
                if _lease_is_live(record):
                    records.append(
                        _chunk_receipt(chunk, run_key, "running", "stopped", "leased_by_other")
                    )
                    stop_reason = "chunk_leased"
                else:
                    records.append(
                        _chunk_receipt(
                            chunk,
                            run_key,
                            "uncertain",
                            "stopped",
                            "expired_or_missing_lease",
                        )
                    )
                    stop_reason = "chunk_uncertain"
                break
            if record is not None and record.status not in {"queued"}:
                records.append(
                    _chunk_receipt(chunk, run_key, "uncertain", "stopped", "ledger_state_invalid")
                )
                stop_reason = "ledger_state_invalid"
                break

            if self._dry_run:
                would_execute += 1
                records.append(
                    _chunk_receipt(
                        chunk,
                        run_key,
                        "queued" if record is not None else "missing",
                        "would_dispatch" if record is None else "preview",
                        "dry_run",
                    )
                )
                continue

            if record is None:
                dispatched = self._dispatch(chunk, run_key, identity.canonical_json)
                if dispatched is not None:
                    records.append(dispatched)
                    failed += 1
                    stop_reason = _dispatch_stop_reason(dispatched.detail)
                    break
                record = self._ledger.get(run_key)
                if record is None:
                    records.append(
                        _chunk_receipt(
                            chunk,
                            run_key,
                            "missing",
                            "stopped",
                            "dispatch_not_persisted",
                        )
                    )
                    failed += 1
                    stop_reason = "dispatch_not_persisted"
                    break
                if record.job_snapshot_json != identity.canonical_json:
                    raise StockDailyBackfillPlanIntegrityError("ledger_snapshot_mismatch")

            try:
                outcome = self._worker.run_once()
            except Exception:
                current = self._ledger.get(run_key)
                status = _receipt_status(current)
                records.append(_chunk_receipt(chunk, run_key, status, "stopped", "execution_error"))
                if status in {"failed", "uncertain"}:
                    failed += 1
                stop_reason = "chunk_failed" if status == "failed" else "execution_error"
                break
            if outcome is None or outcome.record is None:
                current = self._ledger.get(run_key)
                if current is not None and current.status == "succeeded":
                    skipped += 1
                    records.append(
                        _chunk_receipt(
                            chunk,
                            run_key,
                            "succeeded",
                            "observed_succeeded",
                            "concurrent_success",
                        )
                    )
                    continue
                records.append(
                    _chunk_receipt(
                        chunk,
                        run_key,
                        _receipt_status(current),
                        "stopped",
                        "execution_unclaimed",
                    )
                )
                stop_reason = "execution_unclaimed"
                break
            if outcome.record.run_key != run_key:
                current = self._ledger.get(run_key)
                records.append(
                    _chunk_receipt(
                        chunk,
                        run_key,
                        _receipt_status(current),
                        "stopped",
                        "unexpected_run_claimed",
                    )
                )
                stop_reason = "unexpected_run_claimed"
                break
            if outcome.record.status == "succeeded":
                executed += 1
                records.append(
                    _chunk_receipt(chunk, run_key, "succeeded", "executed", "run_succeeded")
                )
                continue
            if outcome.record.status == "failed":
                failed += 1
                records.append(_chunk_receipt(chunk, run_key, "failed", "stopped", "run_failed"))
                stop_reason = "chunk_failed"
                break
            if outcome.record.status == "cancelled":
                records.append(
                    _chunk_receipt(chunk, run_key, "cancelled", "stopped", "run_cancelled")
                )
                stop_reason = "chunk_cancelled"
                break
            records.append(
                _chunk_receipt(chunk, run_key, "uncertain", "stopped", "execution_not_terminal")
            )
            stop_reason = "chunk_uncertain"
            break

        if stop_reason == "plan_finished" and self._dry_run:
            stop_reason = "dry_run"
        return StockDailyBackfillExecutionReceipt(
            plan_sha256=plan.plan_sha256,
            snapshot_sha256=snapshot_sha256,
            dry_run=self._dry_run,
            chunks_total=len(chunks),
            chunks_considered=considered,
            chunks_executed=executed,
            chunks_skipped=skipped,
            chunks_failed=failed,
            chunks_remaining=len(chunks) - considered,
            stop_reason=stop_reason,
            chunks=tuple(records),
        )

    def execute_snapshot_once(
        self,
        snapshot_bytes: bytes,
        *,
        expected_snapshot_sha256: str,
        contract: DatasetContract,
        as_of: date,
        max_window_days: int = MAX_STOCK_DAILY_WINDOW_DAYS,
    ) -> StockDailyBackfillExecutionReceipt:
        """Verify pinned snapshot bytes, build the plan, and execute one prefix.

        The exact byte digest is checked before decoding. The existing
        snapshot planner then verifies its embedded content digest and produces
        the same plan the executor independently verifies again.

        Args:
            snapshot_bytes: Exact immutable snapshot bytes supplied by the caller.
            expected_snapshot_sha256: Lowercase SHA-256 expected for those bytes.
            contract: Exact stock-daily dataset contract.
            as_of: Explicit business-date cutoff used to build the plan.
            max_window_days: Inclusive window cap accepted by the planner.

        Returns:
            The same deterministic receipt returned by :meth:`execute_once`.

        Raises:
            StockDailyBackfillPlanIntegrityError: If the byte digest is invalid
                or mismatched, or the snapshot/plan cannot be verified.
        """
        if type(snapshot_bytes) is not bytes:
            raise TypeError("snapshot_bytes must be immutable bytes")
        if not _is_sha256(expected_snapshot_sha256):
            raise StockDailyBackfillPlanIntegrityError("snapshot_digest_invalid")
        actual_sha256 = hashlib.sha256(snapshot_bytes).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_snapshot_sha256):
            raise StockDailyBackfillPlanIntegrityError("snapshot_digest_mismatch")
        try:
            candidate = build_stock_daily_backfill_plan_from_snapshot(
                snapshot_bytes,
                task=self._task,
                contract=contract,
                as_of=as_of,
                max_window_days=max_window_days,
            )
        except StockDailyBackfillPlanIntegrityError:
            raise
        except Exception:
            raise StockDailyBackfillPlanIntegrityError("snapshot_or_plan_invalid") from None
        if not hmac.compare_digest(candidate.snapshot_bytes_sha256, actual_sha256):
            raise StockDailyBackfillPlanIntegrityError("snapshot_digest_mismatch")
        return self.execute_once(candidate.plan, snapshot_sha256=actual_sha256)

    def _require_execution_authorization(self) -> None:
        """Allow dry-run by default and require the exact token for execution."""
        if self._dry_run:
            return
        authorization = self._execution_authorization
        if authorization is None or not hmac.compare_digest(authorization, EXECUTION_AUTHORIZATION):
            raise StockDailyBackfillExecutionError("execution_not_authorized")

    def _verify_plan_binding(self, plan: StockDailyBackfillPlan) -> None:
        """Bind the verified plan to this exact task and execution-only job."""
        if plan.task_sha256 != stock_daily_task_sha256(self._task):
            raise StockDailyBackfillPlanIntegrityError("plan_task_mismatch")
        if (
            plan.job_id,
            plan.job_version,
            plan.dataset_id,
            plan.provider,
            plan.model,
        ) != (
            self._task.job_id,
            self._task.version,
            self._task.dataset_id,
            self._task.provider,
            self._task.model,
        ):
            raise StockDailyBackfillPlanIntegrityError("plan_task_mismatch")

    def _dispatch(
        self,
        chunk: StockDailyBackfillChunk,
        run_key: str,
        canonical_json: str,
    ) -> StockDailyBackfillChunkReceipt | None:
        """Submit one missing run and return a stop receipt on refusal."""
        payload = {
            "query": chunk.request.query,
            "partition": chunk.request.partition,
            "shard": chunk.request.shard,
            "idempotency_key": chunk.request.idempotency_key,
            "backfill_start": chunk.request.backfill_start,
            "backfill_end": chunk.request.backfill_end,
        }
        try:
            receipt = self._dispatcher.dispatch_backfill_execution(
                self._task.job_id,
                self._task.version,
                payload,
                dry_run=False,
            )
        except Exception:
            return _chunk_receipt(
                chunk,
                run_key,
                "failed",
                "stopped",
                "dispatch_rejected",
            )
        if receipt.identity.run_key != run_key:
            raise StockDailyBackfillPlanIntegrityError("dispatch_identity_mismatch")
        persisted = self._ledger.get(run_key)
        if persisted is None:
            return _chunk_receipt(
                chunk,
                run_key,
                "missing",
                "stopped",
                "dispatch_not_persisted",
            )
        if persisted.job_snapshot_json != canonical_json:
            raise StockDailyBackfillPlanIntegrityError("ledger_snapshot_mismatch")
        if persisted.status not in {"queued", "running", "succeeded"}:
            return _chunk_receipt(
                chunk,
                run_key,
                _receipt_status(persisted),
                "stopped",
                "ledger_state_invalid",
            )
        return None


def _dispatch_stop_reason(detail: str) -> StopReason:
    """Translate a dispatch refusal detail into the receipt stop reason."""
    if detail == "dispatch_not_persisted":
        return "dispatch_not_persisted"
    if detail == "ledger_state_invalid":
        return "ledger_state_invalid"
    return "dispatch_rejected"


def build_stock_daily_execution_task() -> TaskDefinition:
    """Return the fixed, disabled, unscheduled stock-daily execution task."""
    return TaskDefinition(
        job_id=STOCK_DAILY_EXECUTION_JOB_ID,
        version=STOCK_DAILY_EXECUTION_JOB_VERSION,
        dataset_id=STOCK_DAILY_EXECUTION_DATASET_ID,
        provider=SOURCE,
        model="stock_daily",
        timezone="Asia/Shanghai",
        schedule=None,
        enabled=False,
    )


def build_stock_daily_execution_contract() -> DatasetContract:
    """Return the exact raw daily-bar contract accepted by the stock-daily job."""
    return DatasetContract(
        dataset_id=STOCK_DAILY_EXECUTION_DATASET_ID,
        schema_version=STOCK_DAILY_EXECUTION_SCHEMA_VERSION,
        semantic_version=STOCK_DAILY_EXECUTION_SEMANTIC_VERSION,
        asset_class="equity",
        frequency="1d",
        record_kind="time_series",
        fields=(
            FieldContract(name="market", data_type="string"),
            FieldContract(name="symbol", data_type="string"),
            FieldContract(name="trade_date", data_type="date"),
            FieldContract(name="open", data_type="number"),
            FieldContract(name="high", data_type="number"),
            FieldContract(name="low", data_type="number"),
            FieldContract(name="close", data_type="number"),
            FieldContract(name="volume", data_type="number"),
            FieldContract(name="amount", data_type="number"),
        ),
        business_key=("market", "symbol", "trade_date"),
        ordering=("trade_date", "market", "symbol"),
        filter_fields=("market", "symbol"),
        time_field="trade_date",
        temporal_semantics="business_date",
        source=SourceIdentity(
            source_id=STOCK_DAILY_EXECUTION_SOURCE_ID,
            source=SOURCE,
        ),
        field_units={
            "open": "CNY/share",
            "high": "CNY/share",
            "low": "CNY/share",
            "close": "CNY/share",
            "volume": "share",
            "amount": "CNY",
        },
        field_currencies={
            "open": "CNY",
            "high": "CNY",
            "low": "CNY",
            "close": "CNY",
            "amount": "CNY",
        },
        currency="CNY",
        adjustment_basis="raw",
        field_adjustments={
            "open": "raw",
            "high": "raw",
            "low": "raw",
            "close": "raw",
            "volume": "not_applicable",
            "amount": "not_applicable",
        },
        calendar_id="SSE",
        timezone="Asia/Shanghai",
        pit_policy="unsupported",
    )


def _chunk_receipt(
    chunk: StockDailyBackfillChunk,
    run_key: str,
    status: ChunkStatus,
    action: ChunkAction,
    detail: str,
) -> StockDailyBackfillChunkReceipt:
    """Project one chunk observation onto safe receipt fields."""
    return StockDailyBackfillChunkReceipt(
        instrument_id=chunk.instrument_id,
        chunk_index=chunk.chunk_index,
        window_start=chunk.window_start,
        window_end=chunk.window_end,
        run_key=run_key,
        status=status,
        action=action,
        detail=detail,
    )


def _receipt_status(record: RunRecord | None) -> ChunkStatus:
    """Map an optional ledger record to the receipt's closed status set."""
    if record is None:
        return "missing"
    if record.status == "succeeded":
        return "succeeded"
    if record.status == "queued":
        return "queued"
    if record.status == "failed":
        return "failed"
    if record.status == "cancelled":
        return "cancelled"
    if record.status == "running":
        return "running"
    return "uncertain"


def _lease_is_live(record: RunRecord) -> bool:
    """Return whether a running row is held by another live worker right now."""
    expires_at = record.lease_expires_at
    if not isinstance(expires_at, datetime) or expires_at.tzinfo is None:
        return False
    return expires_at > datetime.now(timezone.utc)


def _is_sha256(value: object) -> bool:
    """Return whether a value is a lowercase hexadecimal SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


__all__ = [
    "EXECUTION_AUTHORIZATION",
    "MAX_CHUNKS_PER_INVOCATION",
    "STOCK_DAILY_EXECUTION_DATASET_ID",
    "STOCK_DAILY_EXECUTION_JOB_ID",
    "STOCK_DAILY_EXECUTION_JOB_VERSION",
    "STOCK_DAILY_EXECUTION_SCHEMA_VERSION",
    "STOCK_DAILY_EXECUTION_SEMANTIC_VERSION",
    "STOCK_DAILY_EXECUTION_SOURCE_ID",
    "STOCK_DAILY_EXECUTION_TARGET",
    "StockDailyBackfillChunkReceipt",
    "StockDailyBackfillExecutionError",
    "StockDailyBackfillExecutionReceipt",
    "StockDailyBackfillExecutor",
    "build_stock_daily_execution_contract",
    "build_stock_daily_execution_task",
]
