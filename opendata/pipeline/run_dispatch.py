"""Validate reviewed run submissions and enqueue them through one code path.

This module accepts only statically injected reviewed job definitions. Manual,
backfill, and scheduled requests all use ``RunService.submit``; no source
adapter, normalizer, publisher, legacy script, or arbitrary import runs here.
The APScheduler bridge is an event listener for a dedicated no-op tick job.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal, NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from opendata.pipeline.run_identity import (
    RunIdentity,
    RunRequest,
    TaskDefinition,
    build_run_identity,
)
from opendata.pipeline.run_service import (
    ResultCommittingLedger,
    ReviewedJob,
    ReviewedJobRegistry,
    RunService,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from sqlalchemy.engine import Engine

_MAX_EVENT_SLOTS = 128
_EXCEPTION_CODE = re.compile(r"^_?[A-Za-z][A-Za-z0-9_]{0,79}$")
_WEEKDAY_NAME = r"(?:mon|tue|wed|thu|fri|sat|sun)"
_NAMED_WEEKDAY_ITEM = rf"{_WEEKDAY_NAME}(?:-{_WEEKDAY_NAME})?(?:/\d+)?"
_NAMED_WEEKDAY_FIELD = re.compile(rf"^{_NAMED_WEEKDAY_ITEM}(?:,{_NAMED_WEEKDAY_ITEM})*$", re.I)
_PAYLOAD_FIELDS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "scheduled": frozenset({"query", "partition", "shard"}),
        "manual": frozenset({"query", "partition", "shard", "idempotency_key"}),
        "backfill": frozenset(
            {
                "query",
                "partition",
                "shard",
                "idempotency_key",
                "backfill_start",
                "backfill_end",
            }
        ),
    }
)


@dataclass(frozen=True, slots=True)
class DispatchIdentity:
    """A safe identity summary that never includes query or partition values."""

    run_key: str
    run_type: Literal["scheduled", "manual", "backfill"]
    job_id: str
    version: str
    request_bytes: int


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    """A bounded receipt for a dry-run preview or one queued run."""

    identity: DispatchIdentity
    status: Literal["dry_run", "queued", "running", "succeeded", "failed", "cancelled"]
    created_at: datetime | None


@dataclass(frozen=True, slots=True)
class SlotDispatchFailure:
    """A sanitized failure for one explicitly scheduled occurrence."""

    occurrence: datetime | None
    code: str


@dataclass(frozen=True, slots=True)
class APSchedulerBatchReceipt:
    """Bounded receipts for requests submitted from one APScheduler event.

    An accepted receipt proves the run request was submitted to the opendata
    ledger. It does not prove that the no-op scheduler tick executed or that
    the queued pipeline run completed.
    """

    accepted: tuple[DispatchReceipt, ...]
    failures: tuple[SlotDispatchFailure, ...]
    global_error: str | None = None


class APSchedulerDispatchError(RuntimeError):
    """Report partial scheduler-event results without exposing request data."""

    def __init__(
        self,
        receipt: APSchedulerBatchReceipt,
    ) -> None:
        """Create a safe error containing each accepted key and rejected slot."""
        self.receipt = receipt
        self.accepted = receipt.accepted
        self.failures = receipt.failures
        super().__init__(
            "scheduled_event_dispatch_failed: "
            f"accepted={len(receipt.accepted)} failed={len(receipt.failures)} "
            f"global_error={receipt.global_error or 'none'}"
        )


class ReviewedTaskCatalog:
    """Immutable catalog of explicitly injected, reviewed task definitions."""

    def __init__(self, jobs: Iterable[ReviewedJob]) -> None:
        """Build the catalog from static reviewed job entries.

        Args:
            jobs: Reviewed jobs whose Python handlers are supplied by the
                application. This constructor never imports handler paths.

        Raises:
            TypeError: If an entry is not a reviewed job.
            ValueError: If a job is unreviewed or an identity is duplicated.
        """
        entries: list[ReviewedJob] = []
        for job in jobs:
            if not isinstance(job, ReviewedJob):
                raise TypeError("catalog entries must be ReviewedJob instances")
            if not job.reviewed:
                raise ValueError("catalog accepts only reviewed jobs")
            entries.append(job)
        self._registry = ReviewedJobRegistry(entries)
        self._tasks: Mapping[tuple[str, str], TaskDefinition] = MappingProxyType(
            {(job.task.job_id, job.task.version): job.task for job in entries}
        )

    @property
    def registry(self) -> ReviewedJobRegistry:
        """Return the frozen handler registry used by the run service."""
        return self._registry

    def resolve(self, job_id: str, version: str) -> TaskDefinition | None:
        """Return one exact reviewed task version, if it was injected."""
        return self._tasks.get((job_id, version))

    def tasks(self) -> tuple[TaskDefinition, ...]:
        """Return task definitions in their deterministic insertion order."""
        return tuple(self._tasks.values())


class RunDispatcher:
    """Validate all run modes and submit them to one reviewed run service.

    The current ``RunService.submit`` policy requires every run mode to use an
    enabled and reviewed job version; manual and backfill dispatch preserve
    that same policy. Jobs explicitly marked one-shot-only use a separate
    manual entry point and are never accepted by ordinary dispatch methods.
    Reviewed execution-only jobs use a separate backfill entry point that keeps
    the task disabled and unscheduled.
    """

    def __init__(
        self,
        ledger: ResultCommittingLedger,
        catalog: ReviewedTaskCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Bind an explicit ledger and one static reviewed task catalog."""
        if not isinstance(catalog, ReviewedTaskCatalog):
            raise TypeError("catalog must be a ReviewedTaskCatalog")
        self._catalog = catalog
        self._service = RunService(ledger, catalog.registry, clock=clock)

    @classmethod
    def from_engine(
        cls,
        engine: Engine,
        catalog: ReviewedTaskCatalog,
        *,
        clock: Callable[[], datetime] | None = None,
        clock_policy: Literal["caller", "database"] = "database",
    ) -> RunDispatcher:
        """Create a dispatcher around a caller-owned synchronous ledger engine.

        The run ledger table must already exist. No database schema is created
        by this factory.
        """
        from opendata.pipeline.run_ledger import RunLedger

        return cls(RunLedger(engine, clock_policy=clock_policy), catalog, clock=clock)

    def dispatch_scheduled(
        self,
        job_id: str,
        version: str,
        *,
        occurrence: datetime,
        payload: Mapping[str, object] | None = None,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Validate an exact UTC cron occurrence and enqueue its immutable slot."""
        task = self._require_task(job_id, version)
        _validate_scheduled_occurrence(task, occurrence)
        request = _build_request("scheduled", occurrence, payload)
        return self._submit(task, request, dry_run=dry_run)

    def dispatch_manual(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Validate and enqueue one manually requested run."""
        task = self._require_task(job_id, version)
        request = _build_request("manual", None, payload)
        return self._submit(task, request, dry_run=dry_run)

    def dispatch_one_shot(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Preview or enqueue one manual request for a reviewed one-shot-only job.

        One-shot jobs must be explicitly marked in the reviewed registry and
        remain disabled and unscheduled. A dry run builds the same validated
        request and safe identity but never calls the service or writes a row.
        """
        task = self._require_one_shot_task(job_id, version)
        request = _build_request("manual", None, payload)
        return self._submit_one_shot(task, request, dry_run=dry_run)

    def dispatch_backfill(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Validate and enqueue one explicit historical range request."""
        task = self._require_task(job_id, version)
        request = _build_request("backfill", None, payload)
        return self._submit(task, request, dry_run=dry_run)

    def dispatch_backfill_execution(
        self,
        job_id: str,
        version: str,
        payload: Mapping[str, object],
        *,
        dry_run: bool = False,
    ) -> DispatchReceipt:
        """Preview or enqueue one backfill request for an execution-only job.

        The job must be reviewed, explicitly marked ``execution_only``, and
        remain disabled and schedule-free. This entry point does not enable
        ordinary dispatch, scheduling, or a one-shot manual run.
        """
        task = self._require_execution_task(job_id, version)
        request = _build_request("backfill", None, payload)
        return self._submit_execution(task, request, dry_run=dry_run)

    def apscheduler_callback(
        self,
        job_id: str,
        version: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> APSchedulerSubmissionCallback:
        """Create a reviewed no-op tick binding for APScheduler 3.x."""
        task = self._require_task(job_id, version, scheduled=True)
        request_payload = _snapshot_scheduled_payload(payload)
        return APSchedulerSubmissionCallback(self, task, request_payload)

    def _require_task(
        self,
        job_id: str,
        version: str,
        *,
        scheduled: bool = False,
    ) -> TaskDefinition:
        """Resolve reviewed/enabled metadata before validating a request."""
        job = self._catalog.registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not in the reviewed catalog")
        if job.one_shot_only is True:
            raise ValueError("one-shot-only jobs require the dedicated one-shot dispatcher")
        task = job.task
        if not task.enabled:
            raise ValueError("job version is disabled")
        if scheduled and task.schedule is None:
            raise ValueError("scheduled runs require explicit schedule metadata")
        return task

    def _require_one_shot_task(self, job_id: str, version: str) -> TaskDefinition:
        """Resolve a reviewed one-shot job without enabling ordinary dispatch."""
        job = self._catalog.registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not in the reviewed catalog")
        if job.reviewed is not True or job.one_shot_only is not True:
            raise ValueError("job version is not approved for one-shot dispatch")
        if job.task.enabled is not False or job.task.schedule is not None:
            raise ValueError("one-shot-only jobs must be disabled and unscheduled")
        return job.task

    def _require_execution_task(self, job_id: str, version: str) -> TaskDefinition:
        """Resolve a reviewed execution-only job without enabling ordinary dispatch."""
        job = self._catalog.registry.resolve(job_id, version)
        if job is None:
            raise ValueError("job version is not in the reviewed catalog")
        if job.reviewed is not True or job.execution_only is not True:
            raise ValueError("job version is not approved for execution-only dispatch")
        if job.task.enabled is not False or job.task.schedule is not None:
            raise ValueError("execution-only jobs must be disabled and unscheduled")
        return job.task

    def _submit(
        self,
        task: TaskDefinition,
        request: RunRequest,
        *,
        dry_run: bool,
    ) -> DispatchReceipt:
        """Build one canonical identity and optionally persist it through RunService."""
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        identity = build_run_identity(task, request)
        safe_identity = _safe_identity(identity, task)
        if dry_run:
            return DispatchReceipt(safe_identity, "dry_run", None)
        record = self._service.submit(task.job_id, task.version, request)
        return DispatchReceipt(safe_identity, record.status, record.created_at)

    def _submit_one_shot(
        self,
        task: TaskDefinition,
        request: RunRequest,
        *,
        dry_run: bool,
    ) -> DispatchReceipt:
        """Submit only through the service's persisted one-shot policy."""
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        identity = build_run_identity(task, request)
        safe_identity = _safe_identity(identity, task)
        if dry_run:
            return DispatchReceipt(safe_identity, "dry_run", None)
        record = self._service.submit_one_shot(task.job_id, task.version, request)
        return DispatchReceipt(safe_identity, record.status, record.created_at)

    def _submit_execution(
        self,
        task: TaskDefinition,
        request: RunRequest,
        *,
        dry_run: bool,
    ) -> DispatchReceipt:
        """Submit only through the service's persisted execution-only policy."""
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean")
        identity = build_run_identity(task, request)
        safe_identity = _safe_identity(identity, task)
        if dry_run:
            return DispatchReceipt(safe_identity, "dry_run", None)
        record = self._service.submit_execution(task.job_id, task.version, request)
        return DispatchReceipt(safe_identity, record.status, record.created_at)


class APSchedulerSubmissionCallback:
    """Bridge APScheduler submission events through their actual scheduled slots.

    ``register()`` installs only this class's no-op tick function. APScheduler
    3.x does not pass a scheduled time to ordinary job functions, so a caller
    must not attach this listener to an existing data-fetch or collection job.
    The listener consumes ``JobSubmissionEvent.scheduled_run_times`` and never
    derives an occurrence from local wall time. The submission event is emitted
    after the executor accepts its slot list, before the executor checks each
    slot's misfire grace period. Consequently, a slot can be queued here and
    later produce ``EVENT_JOB_MISSED`` for the no-op tick. That later event does
    not cancel the durable request: opendata's queue owns pipeline execution,
    and an accepted receipt means submitted, not completed. Coalescing is
    disabled so each slot APScheduler actually submits can be queued
    independently. A ``max_instances`` rejection emits no submission event, so
    those slots are not synthesized. Operators must monitor ``last_receipt``
    and the run ledger; APScheduler logs but does not propagate listener
    exceptions to the scheduler caller. Cron expressions use APScheduler 3's
    five-field dialect; weekday fields must be ``*`` or named weekdays
    (``mon`` through ``sun``). Prefer ``mon-fri`` for business days. Numeric
    weekday fields are rejected because source dialects disagree about their
    numbering; occurrence inputs remain explicit UTC instants matched against
    the task's configured IANA timezone.
    """

    def __init__(
        self,
        dispatcher: RunDispatcher,
        task: TaskDefinition,
        payload: Mapping[str, object],
    ) -> None:
        """Bind a dispatcher, exact task version, and bounded static payload."""
        if not isinstance(dispatcher, RunDispatcher):
            raise TypeError("dispatcher must be a RunDispatcher")
        if not isinstance(task, TaskDefinition):
            raise TypeError("task must be a TaskDefinition")
        reviewed_job = dispatcher._catalog.registry.resolve(task.job_id, task.version)
        if reviewed_job is not None and reviewed_job.one_shot_only is True:
            raise ValueError("one-shot-only jobs cannot use the APScheduler callback")
        if not isinstance(payload, Mapping):
            raise ValueError("run payload must be a JSON object")
        _validate_payload_shape("scheduled", payload)
        self._dispatcher = dispatcher
        self._task = task
        self._payload = dict(payload)
        digest = hashlib.sha256(f"{task.job_id}\0{task.version}".encode()).hexdigest()[:32]
        self._scheduler_job_id = f"od-reviewed-{digest}"
        self._registered = False
        self._last_receipt: APSchedulerBatchReceipt | None = None

    @property
    def scheduler_job_id(self) -> str:
        """Return the deterministic id used for the dedicated no-op job."""
        return self._scheduler_job_id

    @property
    def last_receipt(self) -> APSchedulerBatchReceipt | None:
        """Return the latest bounded event receipt for operator monitoring.

        APScheduler catches and logs listener exceptions. The latest receipt
        therefore records partial/global failures that callers might not see;
        durable ledger rows remain the audit source for accepted requests.
        """
        return self._last_receipt

    def register(self, scheduler: object) -> str:
        """Install a dedicated no-op cron tick and this submission listener.

        The scheduler receives no business callable. Only actual scheduled
        run times from its submission event can create queued requests. Do not
        attach this listener to a legacy collection job: it is safe only when
        the bound scheduler job is the dedicated no-op installed here.

        ``misfire_grace_time`` controls execution of the no-op tick, not
        submission to the opendata ledger. ``coalesce=False`` preserves all
        run times included in a submission event. ``max_instances=1`` can
        suppress a submission event while another tick is active; suppressed
        times are not fabricated or marked successful.

        Args:
            scheduler: An unstarted or running APScheduler 3.x instance.

        Returns:
            The deterministic APScheduler job id.

        Raises:
            TypeError: If required APScheduler methods are missing.
            ValueError: If this binding has already been registered.
        """
        if self._registered:
            raise ValueError("APScheduler callback is already registered")
        _validate_cron_dialect(self._task)
        schedule = self._task.schedule
        if schedule is None:
            raise ValueError("scheduled runs require explicit schedule metadata")
        try:
            from apscheduler.events import EVENT_JOB_SUBMITTED
            from apscheduler.triggers.cron import CronTrigger

            add_job = getattr(scheduler, "add_job", None)
            add_listener = getattr(scheduler, "add_listener", None)
            remove_listener = getattr(scheduler, "remove_listener", None)
            if not callable(add_job) or not callable(add_listener) or not callable(remove_listener):
                raise TypeError("scheduler must provide add_job, add_listener, and remove_listener")
            trigger = CronTrigger.from_crontab(
                schedule.expression,
                timezone=self._task.timezone,
            )
            add_listener(self, EVENT_JOB_SUBMITTED)
            try:
                add_job(
                    self._noop_tick,
                    trigger=trigger,
                    id=self._scheduler_job_id,
                    replace_existing=False,
                    coalesce=False,
                    misfire_grace_time=1,
                    max_instances=1,
                )
            except Exception:
                remove_listener(self)
                raise
        except (ImportError, ValueError, TypeError):
            raise
        except Exception:
            raise ValueError("APScheduler binding registration failed") from None
        self._registered = True
        return self._scheduler_job_id

    def __call__(self, event: object) -> tuple[DispatchReceipt, ...]:
        """Enqueue each real APScheduler submission slot independently.

        Non-submission events and other job ids are ignored. Partial failures
        raise :class:`APSchedulerDispatchError` with safe accepted receipts
        and sanitized occurrence/error codes.
        """
        from apscheduler.events import EVENT_JOB_SUBMITTED

        if getattr(event, "code", None) != EVENT_JOB_SUBMITTED:
            return ()
        if getattr(event, "job_id", None) != self._scheduler_job_id:
            return ()
        slots = getattr(event, "scheduled_run_times", None)
        if not isinstance(slots, (list, tuple)) or not slots:
            self._reject_event("empty_slots")
        if len(slots) > _MAX_EVENT_SLOTS:
            self._reject_event("slot_limit_exceeded")

        accepted: list[DispatchReceipt] = []
        failures: list[SlotDispatchFailure] = []
        for slot in slots:
            receipt, failure = _dispatch_scheduler_slot(
                self._dispatcher,
                self._task,
                self._payload,
                slot,
            )
            if failure is not None:
                failures.append(failure)
            if receipt is not None:
                accepted.append(receipt)
        batch_receipt = APSchedulerBatchReceipt(tuple(accepted), tuple(failures))
        self._last_receipt = batch_receipt
        if failures:
            raise APSchedulerDispatchError(batch_receipt)
        return tuple(accepted)

    def _reject_event(self, code: str) -> NoReturn:
        """Record a safe preflight failure before reporting it to APScheduler."""
        receipt = APSchedulerBatchReceipt((), (), global_error=code)
        self._last_receipt = receipt
        raise APSchedulerDispatchError(receipt)

    @staticmethod
    def _noop_tick() -> None:
        """Provide the side-effect-free APScheduler job function."""
        return None


def _build_request(
    run_type: Literal["scheduled", "manual", "backfill"],
    occurrence: datetime | None,
    payload: Mapping[str, object] | None,
) -> RunRequest:
    """Build a strict request while hiding raw validation details from errors."""
    if payload is None:
        values: dict[str, object] = {}
    elif isinstance(payload, Mapping):
        values = dict(payload)
    else:
        raise ValueError("run payload must be a JSON object")
    _validate_payload_shape(run_type, values)
    values["run_type"] = run_type
    if run_type == "scheduled":
        values["occurrence"] = occurrence
    try:
        return RunRequest.model_validate(values)
    except (ValidationError, TypeError, ValueError):
        raise ValueError("run payload is invalid") from None


def _validate_payload_shape(
    run_type: Literal["scheduled", "manual", "backfill"],
    payload: Mapping[str, object],
) -> None:
    """Reject unknown mode fields before they reach RunRequest or the ledger."""
    allowed = _PAYLOAD_FIELDS[run_type]
    if any(not isinstance(key, str) or key not in allowed for key in payload):
        raise ValueError("run payload contains unsupported fields")


def _validate_scheduled_occurrence(task: TaskDefinition, occurrence: datetime) -> None:
    """Require an explicit UTC instant that exactly matches a valid cron slot."""
    if not isinstance(occurrence, datetime):
        raise ValueError("scheduled occurrence must be an explicit datetime")
    if not _is_explicit_utc(occurrence):
        raise ValueError("scheduled occurrence must use an explicit UTC timezone")
    _validate_cron_dialect(task)
    schedule = task.schedule
    if schedule is None:
        raise ValueError("scheduled runs require explicit schedule metadata")
    try:
        zone = ZoneInfo(task.timezone)
        local = occurrence.astimezone(zone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("task timezone is invalid") from None

    local_wall_time = local.replace(tzinfo=None)
    valid_instants: set[datetime] = set()
    for fold in (0, 1):
        candidate = local_wall_time.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == local_wall_time and round_trip.fold == fold:
            valid_instants.add(candidate.astimezone(timezone.utc))
    if not valid_instants:
        raise ValueError("scheduled occurrence maps to a nonexistent local time")
    if len(valid_instants) > 1:
        raise ValueError("scheduled occurrence maps to an ambiguous DST time")

    try:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(schedule.expression, timezone=zone)
        previous_microsecond = local - timedelta(microseconds=1)
        next_slot = trigger.get_next_fire_time(None, previous_microsecond)
    except (ImportError, TypeError, ValueError):
        raise ValueError("cron schedule expression is invalid") from None
    if next_slot is None or next_slot.astimezone(timezone.utc) != occurrence.astimezone(
        timezone.utc
    ):
        raise ValueError("scheduled occurrence is outside the configured cron slot")


def _validate_cron_dialect(task: TaskDefinition) -> None:
    """Require APScheduler 3 five-field cron with named or wildcard weekdays.

    APScheduler 3 treats numeric weekday zero as Monday, while common POSIX
    cron uses one for Monday. Reject numeric weekday syntax instead of silently
    accepting a schedule that may move its collection days. Numeric step values
    remain valid only when attached to named weekday expressions, such as
    ``mon-fri/2``.
    """
    if task.schedule is None:
        raise ValueError("scheduled runs require explicit schedule metadata")
    if task.schedule.kind != "cron":
        raise ValueError("scheduled dispatch and APScheduler bridge require cron schedules")
    fields = task.schedule.expression.split()
    if len(fields) != 5:
        raise ValueError("APScheduler cron schedule must contain five fields")
    weekday_field = fields[4]
    if weekday_field == "*" or _NAMED_WEEKDAY_FIELD.fullmatch(weekday_field):
        return
    if re.search(r"\d", weekday_field):
        raise ValueError(
            "numeric weekday cron fields are ambiguous; manually map the original "
            "schedule dialect to named weekdays or '*'"
        )
    raise ValueError("cron weekday field must be '*' or use named weekdays mon through sun")


def _utc_from_scheduler_slot(value: object, task: TaskDefinition) -> datetime:
    """Validate APScheduler's configured-zone time and convert its instant to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("APScheduler slot must be timezone-aware")
    zone_name = getattr(value.tzinfo, "key", None) or str(value.tzinfo)
    if zone_name != task.timezone:
        raise ValueError("APScheduler slot timezone does not match the task")
    return value.astimezone(timezone.utc)


def _dispatch_scheduler_slot(
    dispatcher: RunDispatcher,
    task: TaskDefinition,
    payload: Mapping[str, object],
    slot: object,
) -> tuple[DispatchReceipt | None, SlotDispatchFailure | None]:
    """Dispatch one event slot and return only a safe receipt or failure code."""
    occurrence: datetime | None = None
    try:
        occurrence = _utc_from_scheduler_slot(slot, task)
        receipt = dispatcher.dispatch_scheduled(
            task.job_id,
            task.version,
            occurrence=occurrence,
            payload=payload,
        )
    except Exception as exc:
        return None, SlotDispatchFailure(occurrence, _safe_error_code(exc))
    return receipt, None


def _snapshot_scheduled_payload(
    payload: Mapping[str, object] | None,
) -> Mapping[str, object]:
    """Validate and detach a static scheduled payload from caller mutation."""
    if payload is None:
        values: Mapping[str, object] = {}
    elif isinstance(payload, Mapping):
        values = payload
    else:
        raise ValueError("run payload must be a JSON object")
    request = _build_request("scheduled", datetime(2000, 1, 1, tzinfo=timezone.utc), values)
    return MappingProxyType(
        {
            "query": request.query,
            "partition": request.partition,
            "shard": request.shard,
        }
    )


def _is_explicit_utc(value: datetime) -> bool:
    """Accept UTC timezone objects and reject other zones at zero offset."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        return False
    key = getattr(value.tzinfo, "key", None)
    if key is not None:
        return key in {"UTC", "Etc/UTC"}
    return value.tzinfo is timezone.utc or str(value.tzinfo) == "UTC"


def _safe_identity(identity: RunIdentity, task: TaskDefinition) -> DispatchIdentity:
    """Project full canonical identity onto fields safe to display to operators."""
    request_bytes = len(identity.canonical_json.encode("utf-8"))
    return DispatchIdentity(
        run_key=identity.idempotency_key,
        run_type=identity.run_type,
        job_id=task.job_id,
        version=task.version,
        request_bytes=request_bytes,
    )


def _safe_error_code(error: BaseException) -> str:
    """Return a sanitized type code without exception messages or payload values."""
    name = type(error).__name__
    if not _EXCEPTION_CODE.fullmatch(name):
        name = "UnhandledError"
    return name


__all__ = [
    "APSchedulerBatchReceipt",
    "APSchedulerDispatchError",
    "APSchedulerSubmissionCallback",
    "DispatchIdentity",
    "DispatchReceipt",
    "RunDispatcher",
    "ReviewedTaskCatalog",
    "SlotDispatchFailure",
]
