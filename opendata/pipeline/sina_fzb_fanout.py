"""Persist and recover per-symbol Sina FZB scheduled submissions.

This coordinator turns one reviewed cron occurrence and one caller-supplied,
immutable instrument manifest into durable single-symbol run requests.  The
fanout journal and run ledger commit independently: a cursor advances only
after every request in its chunk has been accepted, and an interrupted chunk
is safe to replay because the run ledger owns deterministic child identities.
The component is inert until a caller explicitly registers it; it does not
activate a source grant or wire itself into the application's production
scheduler composition. It is not a reliable scheduler by itself: APScheduler
emits no submission event while the scheduler is down, ``max_instances`` can
skip submitted slots, and listener failures are retained only in this
process's latest receipt. Calendar reconciliation, durable event-failure
alerting, and operational source-grant review remain prerequisites before
production activation; this component does not guarantee that no slot is lost.
The coordinator also binds one manifest at construction and reuses that exact
selection for every occurrence it handles. Production activation therefore
requires a versioned, evidence-backed manifest resolved and frozen per slot;
one construction-time snapshot cannot establish a current or complete
all-market universe across future slots.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, NoReturn

from opendata.pipeline.run_dispatch import RunDispatcher
from opendata.pipeline.run_identity import TaskDefinition
from opendata.pipeline.run_ledger import MAX_TTL, MIN_TTL, RunLedgerConflictError
from opendata.pipeline.sina_fzb_process_job import build_sina_fzb_scheduled_payload

if TYPE_CHECKING:
    from opendata.pipeline.sina_fzb_fanout_journal import (
        FanoutGroupRecord,
        SinaFzbFanoutJournal,
    )

FanoutStatus = Literal["PENDING", "ENQUEUED", "BLOCKED"]

SINA_FZB_FANOUT_JOB_ID = "sina-fzb-capture"
MAX_FANOUT_SYMBOLS = 20_000
MAX_FANOUT_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_FANOUT_CHUNK_SIZE = 100
MAX_FANOUT_EVENT_SLOTS = 128
MAX_CHUNKS_PER_PUMP = 32

_SYMBOL = re.compile(r"^[0-9]{6}\.(?:SH|SZ)$", re.ASCII)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$", re.ASCII)
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$", re.ASCII)
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+\-]{0,254}$", re.ASCII)
_WEEKDAY_FIELD = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)(?:-(?:mon|tue|wed|thu|fri|sat|sun))?"
    r"(?:/\d+)?(?:,(?:mon|tue|wed|thu|fri|sat|sun)"
    r"(?:-(?:mon|tue|wed|thu|fri|sat|sun))?(?:/\d+)?)*$",
    re.IGNORECASE,
)
_MANIFEST_FIELDS = frozenset(
    {
        "contract_version",
        "job_id",
        "job_version",
        "manifest_id",
        "occurrence",
        "selection_snapshot_ref",
        "symbols",
    }
)
_UTC = timezone.utc


@dataclass(frozen=True, slots=True)
class SinaFzbInstrumentManifest:
    """An immutable, source-resolved selection of canonical Sina symbols.

    ``selection_snapshot_ref`` is an opaque provenance reference supplied by
    the caller.  This class does not infer a universe, fetch a constituent
    list, or assert source rights/completeness.
    """

    manifest_id: str
    selection_snapshot_ref: str
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        """Reject mutable, oversized, duplicate, or noncanonical selections."""
        _validate_opaque_ref(self.manifest_id, "manifest_id")
        _validate_opaque_ref(self.selection_snapshot_ref, "selection_snapshot_ref")
        if type(self.symbols) is not tuple:
            raise ValueError("symbols must be an immutable tuple")
        _validate_symbols(self.symbols, require_sorted=True)

    @classmethod
    def from_symbols(
        cls,
        *,
        manifest_id: str,
        selection_snapshot_ref: str,
        symbols: Iterable[str],
    ) -> SinaFzbInstrumentManifest:
        """Create a canonical manifest from an explicit bounded symbol list.

        Input order is normalized for reproducible identity. Duplicate or
        invalid symbols are rejected rather than silently removed.
        """
        if isinstance(symbols, (str, bytes)) or not isinstance(symbols, Iterable):
            raise ValueError("symbols must be an iterable of canonical Sina symbols")
        collected: list[str] = []
        seen: set[str] = set()
        for symbol in symbols:
            if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
                raise ValueError("symbols must use six digits and an SH or SZ suffix")
            if symbol in seen:
                raise ValueError("manifest symbols must be unique")
            seen.add(symbol)
            collected.append(symbol)
            if len(collected) > MAX_FANOUT_SYMBOLS:
                raise ValueError("manifest symbol limit exceeded")
        return cls(
            manifest_id=manifest_id,
            selection_snapshot_ref=selection_snapshot_ref,
            symbols=tuple(sorted(collected)),
        )

    def canonical_json(
        self,
        *,
        job_id: str,
        job_version: str,
        occurrence: datetime,
    ) -> str:
        """Bind this selection to one immutable job version and UTC slot."""
        _validate_identifier(job_id, "job_id")
        _validate_identifier(job_version, "job_version")
        occurrence_utc = _require_utc(occurrence)
        document = {
            "contract_version": 1,
            "job_id": job_id,
            "job_version": job_version,
            "manifest_id": self.manifest_id,
            "occurrence": _format_occurrence(occurrence_utc),
            "selection_snapshot_ref": self.selection_snapshot_ref,
            "symbols": list(self.symbols),
        }
        encoded = _canonical_json(document)
        if len(encoded.encode("utf-8")) > MAX_FANOUT_MANIFEST_BYTES:
            raise ValueError("manifest byte limit exceeded")
        return encoded


@dataclass(frozen=True, slots=True)
class FanoutGroupReceipt:
    """Safe durable-group status without exposing the manifest contents."""

    fanout_key: str
    occurrence: datetime
    manifest_sha256: str
    member_count: int
    next_index: int
    status: FanoutStatus


@dataclass(frozen=True, slots=True)
class FanoutSlotFailure:
    """Sanitized failure for one scheduler occurrence."""

    occurrence: datetime | None
    code: str


@dataclass(frozen=True, slots=True)
class FanoutScheduleReceipt:
    """Bounded result of handling one APScheduler submission event."""

    accepted: tuple[FanoutGroupReceipt, ...]
    failures: tuple[FanoutSlotFailure, ...]
    global_error: str | None = None


class SinaFzbFanoutScheduleError(RuntimeError):
    """Report partial group persistence without exposing raw input or errors."""

    def __init__(self, receipt: FanoutScheduleReceipt) -> None:
        """Store a safe receipt for scheduler listeners that swallow exceptions."""
        self.receipt = receipt
        super().__init__(
            "sina_fzb_fanout_schedule_failed: "
            f"accepted={len(receipt.accepted)} failed={len(receipt.failures)} "
            f"global_error={receipt.global_error or 'none'}"
        )


@dataclass(frozen=True, slots=True)
class FanoutGroupOutcome:
    """Safe result of one claimed chunk, including partial accepted counts."""

    fanout_key: str
    status: FanoutStatus
    disposition: Literal["pending", "enqueued", "blocked", "retry", "lease_lost"]
    accepted_this_attempt: int
    next_index: int
    member_count: int
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class FanoutPumpReceipt:
    """Bounded summary of one recovery-pump invocation."""

    claimed_chunks: int
    accepted_runs: int
    enqueued_groups: int
    pending_chunks: int
    blocked_groups: int
    retry_groups: int
    lease_lost_groups: int
    outcomes: tuple[FanoutGroupOutcome, ...]
    global_error: str | None = None


class SinaFzbFanoutCoordinator:
    """Persist scheduler slots and recover single-symbol run submissions.

    The fanout journal is scoped to the fixed ``sina-fzb-capture`` job. Each
    worker renews its group fence while submitting one bounded chunk, then
    advances the cursor only after every child dispatch returned. The journal
    releases the lease on every successful cursor advance. A failed or
    interrupted chunk is replayed from its old cursor;
    ``RunLedgerConflictError`` and invalid reviewed job versions block the
    group for operator review. APScheduler downtime and ``max_instances`` can
    suppress slots before a submission event exists; listener failures are
    process-local receipts only. Durable calendar reconciliation and failure
    alerting are not provided, so registering this coordinator alone does not
    guarantee complete or lossless scheduling. The one manifest supplied at
    construction is reused for every slot; production callers must resolve an
    evidence-backed, versioned selection for each occurrence before enabling
    this path. A fixed snapshot does not assert long-term all-market coverage.
    """

    def __init__(
        self,
        dispatcher: RunDispatcher,
        journal: SinaFzbFanoutJournal,
        task: TaskDefinition,
        manifest: SinaFzbInstrumentManifest,
        *,
        worker_id: str,
        lease_ttl: timedelta,
        retry_backoff_base: timedelta,
        retry_backoff_cap: timedelta,
        pump_interval_seconds: float,
        max_chunks_per_pump: int = 1,
        chunk_size: int = MAX_FANOUT_CHUNK_SIZE,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind caller-owned components and explicit operational bounds.

        No engine, schema, scheduler, provider, or source universe is created
        here. ``register`` installs a real APScheduler 3 listener and pump only
        when called explicitly; production wiring and source-grant review stay
        with the application composition owner.
        """
        if not isinstance(dispatcher, RunDispatcher):
            raise TypeError("dispatcher must be a RunDispatcher")
        if not isinstance(task, TaskDefinition):
            raise TypeError("task must be a TaskDefinition")
        if task.job_id != SINA_FZB_FANOUT_JOB_ID:
            raise ValueError("fanout coordinator only accepts the reviewed Sina FZB job")
        if task.schedule is None or task.schedule.kind != "cron":
            raise ValueError("fanout scheduling requires explicit cron metadata")
        _validate_cron_dialect(task)
        if type(task.enabled) is not bool or not task.enabled:
            raise ValueError("fanout scheduling requires an enabled reviewed job")
        if not isinstance(manifest, SinaFzbInstrumentManifest):
            raise TypeError("manifest must be a SinaFzbInstrumentManifest")
        if type(worker_id) is not str or not _WORKER_ID.fullmatch(worker_id):
            raise ValueError("worker_id is invalid")
        _validate_ttl(lease_ttl, "lease_ttl")
        _validate_backoff(retry_backoff_base, "retry_backoff_base")
        _validate_backoff(retry_backoff_cap, "retry_backoff_cap")
        if retry_backoff_base > retry_backoff_cap:
            raise ValueError("retry_backoff_base must not exceed retry_backoff_cap")
        if (
            isinstance(pump_interval_seconds, bool)
            or not isinstance(pump_interval_seconds, (int, float))
            or not 1 <= pump_interval_seconds <= 86_400
        ):
            raise ValueError("pump_interval_seconds must be between 1 and 86400")
        if isinstance(max_chunks_per_pump, bool) or not isinstance(max_chunks_per_pump, int):
            raise ValueError("max_chunks_per_pump must be an integer")
        if not 1 <= max_chunks_per_pump <= MAX_CHUNKS_PER_PUMP:
            raise ValueError(f"max_chunks_per_pump must be between 1 and {MAX_CHUNKS_PER_PUMP}")
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int):
            raise ValueError("chunk_size must be an integer")
        if not 1 <= chunk_size <= MAX_FANOUT_CHUNK_SIZE:
            raise ValueError(f"chunk_size must be between 1 and {MAX_FANOUT_CHUNK_SIZE}")
        _validate_journal(journal)

        self._dispatcher = dispatcher
        self._journal = journal
        self._task = task
        self._manifest = manifest
        self._worker_id = worker_id
        self._lease_ttl = lease_ttl
        self._retry_backoff_base = retry_backoff_base
        self._retry_backoff_cap = retry_backoff_cap
        self._pump_interval_seconds = float(pump_interval_seconds)
        self._max_chunks_per_pump = max_chunks_per_pump
        self._chunk_size = chunk_size
        self._clock = clock or (lambda: datetime.now(_UTC))
        digest = hashlib.sha256(f"{task.job_id}\0{task.version}".encode()).hexdigest()[:32]
        self._scheduler_job_id = f"od-sina-fzb-fanout-{digest}"
        self._pump_job_id = f"{self._scheduler_job_id}-pump"
        self._registered = False
        self._last_schedule_receipt: FanoutScheduleReceipt | None = None
        self._last_pump_receipt: FanoutPumpReceipt | None = None

    @property
    def scheduler_job_id(self) -> str:
        """Return the deterministic no-op cron job id."""
        return self._scheduler_job_id

    @property
    def pump_job_id(self) -> str:
        """Return the deterministic bounded recovery-pump job id."""
        return self._pump_job_id

    @property
    def last_schedule_receipt(self) -> FanoutScheduleReceipt | None:
        """Return the most recent scheduler-event receipt for monitoring."""
        return self._last_schedule_receipt

    @property
    def last_pump_receipt(self) -> FanoutPumpReceipt | None:
        """Return the most recent recovery summary for monitoring."""
        return self._last_pump_receipt

    def register(self, scheduler: object) -> tuple[str, str]:
        """Install a no-op cron listener and a periodic persistent recovery pump.

        APScheduler's submission event supplies the actual cron slot. The
        event listener persists group headers only; the interval pump performs
        bounded, resumable child submissions. Neither job performs source I/O.
        Scheduler downtime and ``max_instances`` can suppress slots, while
        event-listener failures are only reflected in process-local memory.
        A durable calendar reconciliation and alerting layer is required for
        lossless production scheduling and is outside this component.
        """
        if self._registered:
            raise ValueError("Sina FZB fanout is already registered")
        try:
            from apscheduler.events import EVENT_JOB_SUBMITTED
            from apscheduler.triggers.cron import CronTrigger
            from apscheduler.triggers.interval import IntervalTrigger

            add_job = getattr(scheduler, "add_job", None)
            add_listener = getattr(scheduler, "add_listener", None)
            remove_listener = getattr(scheduler, "remove_listener", None)
            remove_job = getattr(scheduler, "remove_job", None)
            if not callable(add_job) or not callable(add_listener) or not callable(remove_listener):
                raise TypeError("scheduler must provide add_job, add_listener, and remove_listener")
            if not callable(remove_job):
                raise TypeError("scheduler must provide remove_job")
            schedule = self._task.schedule
            if schedule is None or schedule.kind != "cron":
                raise ValueError("fanout scheduling requires explicit cron metadata")
            cron_trigger = CronTrigger.from_crontab(
                schedule.expression,
                timezone=self._task.timezone,
            )
            add_listener(self, EVENT_JOB_SUBMITTED)
            installed: list[str] = []
            try:
                add_job(
                    self._noop_tick,
                    trigger=cron_trigger,
                    id=self._scheduler_job_id,
                    replace_existing=False,
                    coalesce=False,
                    misfire_grace_time=1,
                    max_instances=1,
                )
                installed.append(self._scheduler_job_id)
                add_job(
                    self._pump_tick,
                    trigger=IntervalTrigger(seconds=self._pump_interval_seconds),
                    id=self._pump_job_id,
                    replace_existing=False,
                    coalesce=True,
                    misfire_grace_time=1,
                    max_instances=1,
                )
                installed.append(self._pump_job_id)
            except Exception:
                remove_listener(self)
                for job_id in installed:
                    with suppress(Exception):
                        remove_job(job_id)
                raise
        except (ImportError, TypeError, ValueError):
            raise
        except Exception:
            raise ValueError("APScheduler fanout registration failed") from None
        self._registered = True
        return self._scheduler_job_id, self._pump_job_id

    def create_scheduled_group(self, occurrence: datetime) -> FanoutGroupReceipt:
        """Preflight and persist one exact scheduled slot without child writes."""
        occurrence_utc = _require_utc(occurrence)
        manifest_json = self._manifest.canonical_json(
            job_id=self._task.job_id,
            job_version=self._task.version,
            occurrence=occurrence_utc,
        )
        self._dispatcher.dispatch_scheduled(
            self._task.job_id,
            self._task.version,
            occurrence=occurrence_utc,
            payload=build_sina_fzb_scheduled_payload(self._manifest.symbols[0]),
            dry_run=True,
        )
        record = self._journal.create_or_get_group(
            job_id=self._task.job_id,
            job_version=self._task.version,
            occurrence=occurrence_utc,
            manifest_json=manifest_json,
        )
        parsed = _validate_record(record)
        if (
            parsed.job_id != self._task.job_id
            or parsed.job_version != self._task.version
            or parsed.occurrence != occurrence_utc
            or parsed.manifest_json != manifest_json
        ):
            raise ValueError("journal returned a conflicting Sina FZB group")
        return _group_receipt(parsed)

    def __call__(self, event: object) -> tuple[FanoutGroupReceipt, ...]:
        """Persist each actual APScheduler slot and report partial failures safely."""
        from apscheduler.events import EVENT_JOB_SUBMITTED

        if getattr(event, "code", None) != EVENT_JOB_SUBMITTED:
            return ()
        if getattr(event, "job_id", None) != self._scheduler_job_id:
            return ()
        slots = getattr(event, "scheduled_run_times", None)
        if not isinstance(slots, (list, tuple)) or not slots:
            self._reject_event("empty_slots")
        if len(slots) > MAX_FANOUT_EVENT_SLOTS:
            self._reject_event("slot_limit_exceeded")

        accepted: list[FanoutGroupReceipt] = []
        failures: list[FanoutSlotFailure] = []
        for slot in slots:
            occurrence: datetime | None = None
            try:
                occurrence = _utc_from_scheduler_slot(slot, self._task)
                group_receipt = self.create_scheduled_group(occurrence)
            except Exception as exc:
                failures.append(FanoutSlotFailure(occurrence, _safe_error_code(exc)))
            else:
                accepted.append(group_receipt)
        receipt = FanoutScheduleReceipt(tuple(accepted), tuple(failures))
        self._last_schedule_receipt = receipt
        if failures:
            raise SinaFzbFanoutScheduleError(receipt)
        return tuple(accepted)

    def run_pending(self, *, max_chunks: int | None = None) -> FanoutPumpReceipt:
        """Claim and process due chunks within an explicit per-pump bound."""
        limit = self._max_chunks_per_pump if max_chunks is None else max_chunks
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("max_chunks must be an integer")
        if not 1 <= limit <= self._max_chunks_per_pump:
            raise ValueError("max_chunks exceeds the configured pump bound")

        outcomes: list[FanoutGroupOutcome] = []
        claimed_chunks = 0
        accepted_runs = 0
        global_error: str | None = None
        for _ in range(limit):
            try:
                claimed = self._journal.claim_next_group(
                    worker=self._worker_id,
                    now=self._now(),
                    ttl=self._lease_ttl,
                )
            except Exception as exc:
                global_error = _safe_error_code(exc)
                break
            if claimed is None:
                break
            claimed_chunks += 1
            try:
                outcome = self._resume_claimed_group(claimed)
            except Exception as exc:
                global_error = _safe_error_code(exc)
                break
            outcomes.append(outcome)
            accepted_runs += outcome.accepted_this_attempt

        receipt = FanoutPumpReceipt(
            claimed_chunks=claimed_chunks,
            accepted_runs=accepted_runs,
            enqueued_groups=sum(outcome.disposition == "enqueued" for outcome in outcomes),
            pending_chunks=sum(outcome.disposition == "pending" for outcome in outcomes),
            blocked_groups=sum(outcome.disposition == "blocked" for outcome in outcomes),
            retry_groups=sum(outcome.disposition == "retry" for outcome in outcomes),
            lease_lost_groups=sum(outcome.disposition == "lease_lost" for outcome in outcomes),
            outcomes=tuple(outcomes),
            global_error=global_error,
        )
        self._last_pump_receipt = receipt
        return receipt

    def _resume_claimed_group(self, record: FanoutGroupRecord) -> FanoutGroupOutcome:
        """Submit replay-safe chunks until complete, blocked, or lease loss."""
        try:
            current = _validate_record(record)
            if current.job_id != SINA_FZB_FANOUT_JOB_ID:
                raise ValueError("journal contains an unexpected job id")
            if current.status != "PENDING" or current.next_index >= len(current.symbols):
                raise ValueError("journal returned a nonpending or exhausted group")
            self._require_claim(current)
        except (TypeError, ValueError) as exc:
            error_code = _safe_error_code(exc)
            blocked = self._try_block(record, error_code)
            return _outcome(
                record,
                blocked,
                disposition="blocked" if blocked is not None else "lease_lost",
                accepted=0,
                error_code=error_code,
            )

        accepted = 0
        start = current.next_index
        end = min(start + self._chunk_size, len(current.symbols))
        for index in range(start, end):
            if index > start:
                renewed = self._heartbeat(current)
                if renewed is None:
                    return _outcome(
                        current,
                        current,
                        disposition="lease_lost",
                        accepted=accepted,
                    )
                renewed_record = _validate_record(renewed)
                if (
                    renewed_record.fanout_key != current.fanout_key
                    or renewed_record.manifest_sha256 != current.manifest_sha256
                    or renewed_record.next_index != start
                ):
                    return _outcome(
                        current,
                        current,
                        disposition="lease_lost",
                        accepted=accepted,
                        error_code="FANOUT_HEARTBEAT_INVARIANT_ERROR",
                    )
                current = renewed_record
            try:
                self._dispatcher.dispatch_scheduled(
                    current.job_id,
                    current.job_version,
                    occurrence=current.occurrence,
                    payload=build_sina_fzb_scheduled_payload(current.symbols[index]),
                )
            except (RunLedgerConflictError, TypeError, ValueError) as exc:
                code = _safe_error_code(exc)
                blocked = self._try_block(current, code)
                return _outcome(
                    current,
                    blocked,
                    disposition="blocked" if blocked is not None else "lease_lost",
                    accepted=accepted,
                    error_code=code,
                )
            except Exception as exc:
                code = _safe_error_code(exc)
                released = self._try_release_for_retry(current, code)
                return _outcome(
                    current,
                    released,
                    disposition="retry" if released is not None else "lease_lost",
                    accepted=accepted,
                    error_code=code,
                )
            accepted += 1

        advanced = self._journal.advance_cursor(
            fanout_key=current.fanout_key,
            worker=self._worker_id,
            fencing_token=current.fencing_token,
            expected_index=start,
            new_index=end,
            now=self._now(),
        )
        if advanced is None:
            return _outcome(
                current,
                current,
                disposition="lease_lost",
                accepted=accepted,
            )
        current = _validate_record(advanced)
        if current.status == "ENQUEUED":
            return _outcome(current, current, disposition="enqueued", accepted=accepted)
        if (
            current.status != "PENDING"
            or current.next_index != end
            or current.lease_owner is not None
            or current.lease_expires_at is not None
        ):
            code = "FANOUT_CURSOR_INVARIANT_ERROR"
            blocked = self._try_block(current, code)
            return _outcome(
                current,
                blocked,
                disposition="blocked" if blocked is not None else "lease_lost",
                accepted=accepted,
                error_code=code,
            )
        return _outcome(current, current, disposition="pending", accepted=accepted)

    def _heartbeat(self, record: FanoutGroupRecord) -> FanoutGroupRecord | None:
        """Renew the active fence immediately before another child submission."""
        return self._journal.heartbeat(
            fanout_key=record.fanout_key,
            worker=self._worker_id,
            fencing_token=record.fencing_token,
            now=self._now(),
            ttl=self._lease_ttl,
        )

    def _require_claim(self, record: FanoutGroupRecord) -> None:
        """Check that the claim reply is bound to this live coordinator lease."""
        if record.lease_owner != self._worker_id:
            raise ValueError("journal returned a group owned by another worker")
        if isinstance(record.fencing_token, bool) or record.fencing_token < 1:
            raise ValueError("journal returned an invalid fencing token")
        if record.lease_expires_at is None:
            raise ValueError("journal returned a group without a lease expiry")
        _require_utc(record.lease_expires_at)

    def _try_block(
        self,
        record: FanoutGroupRecord,
        error_code: str,
    ) -> FanoutGroupRecord | None:
        """Fence a permanent failure; return the newest durable row if possible."""
        return self._journal.block(
            fanout_key=record.fanout_key,
            worker=self._worker_id,
            fencing_token=record.fencing_token,
            now=self._now(),
            error_code=error_code,
        )

    def _try_release_for_retry(
        self,
        record: FanoutGroupRecord,
        error_code: str,
    ) -> FanoutGroupRecord | None:
        """Release a transient failure with attempt-scaled bounded backoff."""
        delay = _retry_delay(
            self._retry_backoff_base,
            self._retry_backoff_cap,
            record.attempt_count,
        )
        return self._journal.release_for_retry(
            fanout_key=record.fanout_key,
            worker=self._worker_id,
            fencing_token=record.fencing_token,
            now=self._now(),
            retry_delay=delay,
            error_code=error_code,
        )

    def _now(self) -> datetime:
        """Return the injected aware UTC time used by journal transition calls."""
        return _require_utc(self._clock())

    def _reject_event(self, code: str) -> NoReturn:
        """Record a safe whole-event failure before raising to APScheduler."""
        receipt = FanoutScheduleReceipt((), (), global_error=code)
        self._last_schedule_receipt = receipt
        raise SinaFzbFanoutScheduleError(receipt)

    def _pump_tick(self) -> FanoutPumpReceipt:
        """Invoke one bounded persistent recovery pass from APScheduler."""
        return self.run_pending()

    @staticmethod
    def _noop_tick() -> None:
        """Provide a side-effect-free cron function for submission events."""
        return None


def _validate_journal(journal: object) -> None:
    """Require every persistence operation without creating a default journal."""
    required = (
        "create_or_get_group",
        "claim_next_group",
        "heartbeat",
        "advance_cursor",
        "release_for_retry",
        "block",
        "get",
    )
    if any(not callable(getattr(journal, name, None)) for name in required):
        raise TypeError("journal must implement the Sina FZB fanout contract")


def _validate_identifier(value: object, label: str) -> None:
    """Enforce the bounded identifier grammar used by immutable manifests."""
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _validate_opaque_ref(value: object, label: str) -> None:
    """Match the journal's bounded printable-reference contract."""
    if (
        type(value) is not str
        or not value
        or len(value) > 512
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"{label} is invalid")


def _validate_symbols(symbols: tuple[str, ...], *, require_sorted: bool) -> None:
    """Validate nonempty, unique, bounded symbol tuples."""
    if not symbols or len(symbols) > MAX_FANOUT_SYMBOLS:
        raise ValueError("manifest symbols must be nonempty and within the fanout limit")
    previous: str | None = None
    seen: set[str] = set()
    for symbol in symbols:
        if type(symbol) is not str or not _SYMBOL.fullmatch(symbol):
            raise ValueError("manifest contains a noncanonical Sina symbol")
        if symbol in seen:
            raise ValueError("manifest symbols must be unique")
        if require_sorted and previous is not None and symbol <= previous:
            raise ValueError("manifest symbols must be strictly sorted")
        seen.add(symbol)
        previous = symbol


def _canonical_json(document: Mapping[str, object]) -> str:
    """Encode deterministic compact JSON without NaN or non-string keys."""
    try:
        return json.dumps(
            document,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        raise ValueError("manifest is not canonical JSON") from None


def _validate_record(record: FanoutGroupRecord) -> FanoutGroupRecord:
    """Verify the persisted canonical manifest and row fields before dispatch."""
    if type(record.fanout_key) is not str or not _SHA256.fullmatch(record.fanout_key):
        raise ValueError("journal group key is invalid")
    if type(record.manifest_sha256) is not str or not _SHA256.fullmatch(record.manifest_sha256):
        raise ValueError("journal manifest digest is invalid")
    if type(record.manifest_json) is not str:
        raise ValueError("journal manifest is invalid")
    encoded = record.manifest_json.encode("utf-8")
    if len(encoded) > MAX_FANOUT_MANIFEST_BYTES:
        raise ValueError("journal manifest exceeds the byte limit")
    if hashlib.sha256(encoded).hexdigest() != record.manifest_sha256:
        raise ValueError("journal manifest digest does not match")
    try:
        document = json.loads(
            record.manifest_json,
            object_pairs_hook=_unique_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise ValueError("journal manifest is not valid canonical JSON") from None
    if type(document) is not dict or frozenset(document) != _MANIFEST_FIELDS:
        raise ValueError("journal manifest fields are invalid")
    if type(document["contract_version"]) is not int or document["contract_version"] != 1:
        raise ValueError("journal manifest version is invalid")
    _validate_identifier(document["job_id"], "job_id")
    _validate_identifier(document["job_version"], "job_version")
    _validate_opaque_ref(document["manifest_id"], "manifest_id")
    _validate_opaque_ref(document["selection_snapshot_ref"], "selection_snapshot_ref")
    symbols_value = document["symbols"]
    if type(symbols_value) is not list:
        raise ValueError("journal manifest symbols are invalid")
    symbols = tuple(symbols_value)
    _validate_symbols(symbols, require_sorted=True)
    occurrence = _parse_occurrence(document["occurrence"])
    if (
        document["job_id"] != record.job_id
        or document["job_version"] != record.job_version
        or occurrence != _require_utc(record.occurrence)
        or symbols != record.symbols
        or record.status not in {"PENDING", "ENQUEUED", "BLOCKED"}
        or isinstance(record.next_index, bool)
        or not isinstance(record.next_index, int)
        or not 0 <= record.next_index <= len(symbols)
        or isinstance(record.attempt_count, bool)
        or not isinstance(record.attempt_count, int)
        or record.attempt_count < 0
        or isinstance(record.fencing_token, bool)
        or not isinstance(record.fencing_token, int)
        or record.fencing_token < 0
        or (record.status == "ENQUEUED" and record.next_index != len(symbols))
        or (record.status == "PENDING" and record.next_index >= len(symbols))
        or (
            record.status == "ENQUEUED"
            and (
                record.lease_owner is not None
                or record.lease_expires_at is not None
                or record.enqueued_at is None
            )
        )
        or (record.status != "ENQUEUED" and record.enqueued_at is not None)
        or (record.status == "BLOCKED" and record.lease_owner is not None)
    ):
        raise ValueError("journal row does not match its immutable manifest")
    if _canonical_json(document) != record.manifest_json:
        raise ValueError("journal manifest is not in canonical encoding")
    return record


def _unique_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON object keys during recovery validation."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    """Reject nonstandard JSON constants such as NaN and Infinity."""
    raise ValueError(f"invalid JSON constant: {value}")


def _parse_occurrence(value: object) -> datetime:
    """Parse only the manifest's exact UTC ISO representation."""
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("journal occurrence is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError("journal occurrence is invalid") from None
    occurrence = _require_utc(parsed)
    if _format_occurrence(occurrence) != value:
        raise ValueError("journal occurrence is not canonical UTC")
    return occurrence


def _format_occurrence(value: datetime) -> str:
    """Format a UTC instant at fixed microsecond precision."""
    return value.astimezone(_UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _require_utc(value: object) -> datetime:
    """Require an aware zero-offset datetime and normalize it to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("datetime must be timezone-aware UTC")
    return value.astimezone(_UTC)


def _utc_from_scheduler_slot(value: object, task: TaskDefinition) -> datetime:
    """Validate APScheduler's task-zone slot before canonicalizing its instant."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("APScheduler slot must be timezone-aware")
    zone_name = getattr(value.tzinfo, "key", None) or str(value.tzinfo)
    if zone_name != task.timezone:
        raise ValueError("APScheduler slot timezone does not match the task")
    return value.astimezone(_UTC)


def _validate_cron_dialect(task: TaskDefinition) -> None:
    """Reject ambiguous numeric weekday fields before registering a job."""
    schedule = task.schedule
    if schedule is None or schedule.kind != "cron":
        raise ValueError("fanout requires cron schedule metadata")
    fields = schedule.expression.split()
    if len(fields) != 5:
        raise ValueError("APScheduler cron expression must contain five fields")
    weekday = fields[4]
    if weekday == "*" or _WEEKDAY_FIELD.fullmatch(weekday):
        return
    if re.search(r"\d", weekday):
        raise ValueError("numeric weekday cron fields are ambiguous")
    raise ValueError("weekday cron field must be '*' or named weekdays")


def _validate_ttl(value: object, label: str) -> None:
    """Enforce the run ledger's established lease TTL bounds."""
    if not isinstance(value, timedelta) or value < MIN_TTL or value > MAX_TTL:
        raise ValueError(f"{label} must be between {MIN_TTL} and {MAX_TTL}")


def _validate_backoff(value: object, label: str) -> None:
    """Require positive, bounded retry backoff durations."""
    if not isinstance(value, timedelta) or value < timedelta(seconds=1) or value > MAX_TTL:
        raise ValueError(f"{label} must be between one second and {MAX_TTL}")


def _retry_delay(base: timedelta, cap: timedelta, attempt_count: int) -> timedelta:
    """Return capped exponential backoff using prior failed attempts."""
    exponent = min(max(attempt_count, 0), 16)
    delay = base
    for _ in range(exponent):
        if delay >= cap:
            return cap
        doubled = delay + delay
        delay = cap if doubled > cap else doubled
    return cap if delay > cap else delay


def _group_receipt(record: FanoutGroupRecord) -> FanoutGroupReceipt:
    """Project a journal record to its bounded operator receipt."""
    return FanoutGroupReceipt(
        fanout_key=record.fanout_key,
        occurrence=_require_utc(record.occurrence),
        manifest_sha256=record.manifest_sha256,
        member_count=len(record.symbols),
        next_index=record.next_index,
        status=record.status,
    )


def _outcome(
    before: FanoutGroupRecord,
    after: FanoutGroupRecord | None,
    *,
    disposition: Literal["pending", "enqueued", "blocked", "retry", "lease_lost"],
    accepted: int,
    error_code: str | None = None,
) -> FanoutGroupOutcome:
    """Create a safe group outcome even when a transition result is absent."""
    selected = after or before
    return FanoutGroupOutcome(
        fanout_key=selected.fanout_key,
        status=selected.status,
        disposition=disposition,
        accepted_this_attempt=accepted,
        next_index=selected.next_index,
        member_count=len(selected.symbols),
        error_code=error_code,
    )


def _safe_error_code(error: BaseException) -> str:
    """Return a sanitized exception class without including its message."""
    name = type(error).__name__
    code = re.sub(r"(?<!^)(?=[A-Z])", "_", name).upper()
    return code if _ERROR_CODE.fullmatch(code) else "UNHANDLED_ERROR"


__all__ = [
    "FanoutGroupOutcome",
    "FanoutGroupReceipt",
    "FanoutPumpReceipt",
    "FanoutScheduleReceipt",
    "FanoutSlotFailure",
    "MAX_FANOUT_CHUNK_SIZE",
    "MAX_FANOUT_MANIFEST_BYTES",
    "MAX_FANOUT_SYMBOLS",
    "MAX_CHUNKS_PER_PUMP",
    "SINA_FZB_FANOUT_JOB_ID",
    "SinaFzbFanoutCoordinator",
    "SinaFzbFanoutScheduleError",
    "SinaFzbInstrumentManifest",
]
