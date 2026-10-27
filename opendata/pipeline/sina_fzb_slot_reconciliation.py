"""Detect missing UTC cron slots for the Sina FZB fanout journal.

This module is an offline reconciliation capability. It enumerates cron
occurrences and asks the durable journal to compare them with already
registered fanout groups. It does not create groups, dispatch runs, contact a
provider, or claim that an enqueued group completed data collection.

Cron expressions use APScheduler ``CronTrigger.from_crontab`` semantics. All
specified fields are combined with AND: when both day-of-month and day-of-week
are restricted, both must match. For example, ``0 0 1 * mon`` runs only when
the first day of a month is Monday; it does not use the traditional cron OR
interpretation of either condition matching.

Version 1 accepts one immutable UTC cron schedule generation per job. A
schedule or job-version change is a contract conflict; generation cutover is a
separate operational action. Slots without contemporaneous group evidence
retain unknown selection evidence even if a group appears later.

The fanout journal's natural key has no schedule fingerprint. A matching
job-version group proves only that a group is registered for that occurrence;
it does not prove which schedule generation created it. Its opaque selection
reference is not an independently verified historical universe. The 120-
second grace delays the missing observation after the fanout scheduler's
one-second misfire grace, but cannot recreate an event suppressed during
downtime. On first initialization the journal rejects a lower boundary later
than its selected clock; an exact replay of an existing contract remains
valid after a clock rollback. Late recovery timestamps use the observed audit
clock and do not move forward artificially, so recovery waits until that clock
catches up to the original missing observation.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Final, Protocol

from opendata.pipeline.run_identity import ScheduleMetadata, TaskDefinition
from opendata.pipeline.sina_fzb_fanout_journal import SINA_FZB_JOB_ID

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from opendata.pipeline.sina_fzb_slot_reconciliation_journal import (
        ReconciliationBatch,
        ReconciliationCursor,
    )

UTC = timezone.utc
RECONCILIATION_CONTRACT_VERSION: Final[int] = 1
SLOT_GRACE_SECONDS: Final[int] = 120
MAX_SLOTS_PER_BATCH: Final[int] = 128
MAX_RECOVERY_SLOTS_PER_BATCH: Final[int] = 128
MAX_INITIAL_FROM_BASIS_LENGTH: Final[int] = 255

_WEEKDAY_FIELD = re.compile(
    r"^(?:mon|tue|wed|thu|fri|sat|sun)(?:-(?:mon|tue|wed|thu|fri|sat|sun))?"
    r"(?:/\d+)?(?:,(?:mon|tue|wed|thu|fri|sat|sun)"
    r"(?:-(?:mon|tue|wed|thu|fri|sat|sun))?(?:/\d+)?)*$",
    re.IGNORECASE,
)
_SCHEDULE_SNAPSHOT_KEYS = frozenset(
    {
        "calendar_policy",
        "contract_version",
        "crontab",
        "grace_seconds",
        "job_id",
        "job_version",
        "max_recovery_slots_per_batch",
        "max_slots_per_batch",
        "schedule_kind",
        "selection_policy",
        "timezone",
    }
)
_INITIAL_FROM_BASIS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,254}$", re.ASCII)


class UnsupportedSinaFzbScheduleError(ValueError):
    """Raised when a schedule cannot be reconciled under the UTC cron policy."""


class _CronTrigger(Protocol):
    """Narrow APScheduler trigger contract used by the offline enumerator."""

    def get_next_fire_time(
        self,
        previous_fire_time: datetime | None,
        now: datetime,
    ) -> datetime | None:
        """Return the next matching UTC cron instant."""


class _ReconciliationJournal(Protocol):
    """Minimal journal API consumed by the schedule reconciler."""

    def database_now(self) -> datetime:
        """Return the database UTC clock for a multi-host cutoff."""

    def initialize(
        self,
        *,
        job_id: str,
        job_version: str,
        schedule_snapshot_json: str,
        schedule_fingerprint: str,
        initial_from: datetime,
        initial_from_basis: str,
        now: datetime,
    ) -> ReconciliationCursor:
        """Create or validate the immutable schedule cursor."""

    def reconcile_batch(
        self,
        *,
        job_id: str,
        job_version: str,
        expected_schedule_fingerprint: str,
        expected_cursor_revision: int,
        expected_from: datetime,
        checked_at: datetime,
        due_through: datetime,
        new_cursor: datetime,
        expected_occurrences: tuple[datetime, ...],
    ) -> ReconciliationBatch:
        """Atomically persist bounded slot observations and advance the cursor."""


class SinaFzbSlotReconciler:
    """Enumerate and persist bounded missing-slot observations for one job.

    Args:
        journal: Caller-owned durable reconciliation journal. The journal must
            already have its migration applied.
        task: Immutable task definition for the reviewed Sina FZB capture job.
        initial_from: Explicit exclusive lower boundary for the first
            reconciliation window. No history before this instant is inferred.
            On first initialization it must not be later than the selected
            clock; an existing exact journal contract can be replayed after a
            clock rollback.
        initial_from_basis: Bounded caller-declared reason for the boundary.
            It is not independent evidence, a listing date, or a completeness
            claim. Everything before ``initial_from`` remains NOT_ASSESSED.
        clock: Optional explicit UTC clock for single-host/caller-clock use and
            deterministic tests. The default reads the journal's database UTC
            clock. Recovery uses this observed time without clamping, so a
            rollback can delay recovery until the clock catches up.

    The task must have a five-field cron schedule, the exact ``UTC`` timezone,
    and no trading-calendar identifier. Cron day-of-month and day-of-week
    conditions follow APScheduler's conjunctive matching semantics. The
    ``enabled`` flag is intentionally irrelevant because this object does not
    activate a scheduler.
    """

    def __init__(
        self,
        journal: _ReconciliationJournal,
        task: TaskDefinition,
        *,
        initial_from: datetime,
        initial_from_basis: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """Validate the inert schedule contract and freeze its fingerprint."""
        if not callable(getattr(journal, "initialize", None)) or not callable(
            getattr(journal, "reconcile_batch", None)
        ):
            raise TypeError("journal must implement initialize() and reconcile_batch()")
        if not isinstance(task, TaskDefinition):
            raise TypeError("task must be a TaskDefinition")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        if clock is None and not callable(getattr(journal, "database_now", None)):
            raise TypeError("journal must implement database_now() when no clock is supplied")
        schedule = _validate_task(task)
        self._journal = journal
        self._task = task
        self._clock = journal.database_now if clock is None else clock
        self._initial_from = _require_utc(initial_from, "initial_from")
        self._initial_from_basis = _validate_initial_from_basis(initial_from_basis)
        self._crontab = schedule.expression
        self._schedule_snapshot_json = _canonical_schedule_snapshot(task)
        self._schedule_fingerprint = hashlib.sha256(
            self._schedule_snapshot_json.encode("utf-8")
        ).hexdigest()

    @property
    def schedule_fingerprint(self) -> str:
        """Return the immutable fingerprint used to fence this schedule."""
        return self._schedule_fingerprint

    @property
    def schedule_snapshot_json(self) -> str:
        """Return the canonical immutable schedule snapshot."""
        return self._schedule_snapshot_json

    def reconcile(self) -> ReconciliationBatch:
        """Persist one bounded occurrence window and late-group recovery pass.

        The configured clock must return an aware UTC instant. By default, the
        journal's database clock determines the cutoff. Occurrences newer than
        the fixed 120-second grace are not examined. If more than 128 due slots
        remain, only the first 128 are committed and the watermark stops at
        the last processed slot. Otherwise the complete window advances to the
        safe ``checked_at - grace`` boundary, including when it contains no
        cron slot.
        """
        checked_at = _require_utc(self._clock(), "reconciliation clock")
        due_through = checked_at - timedelta(seconds=SLOT_GRACE_SECONDS)
        cursor = self._journal.initialize(
            job_id=self._task.job_id,
            job_version=self._task.version,
            schedule_snapshot_json=self._schedule_snapshot_json,
            schedule_fingerprint=self._schedule_fingerprint,
            initial_from=self._initial_from,
            initial_from_basis=self._initial_from_basis,
            now=checked_at,
        )
        _validate_cursor(
            cursor,
            job_id=self._task.job_id,
            job_version=self._task.version,
            fingerprint=self._schedule_fingerprint,
            schedule_snapshot_json=self._schedule_snapshot_json,
            initial_from=self._initial_from,
            initial_from_basis=self._initial_from_basis,
        )

        if due_through <= cursor.reconciled_through:
            # A clock rewind or a call inside the already-reconciled grace
            # window must not move the cursor backward. The journal still runs
            # its bounded late-group recovery scan in this no-progress batch.
            return _reconcile_batch(
                self._journal,
                job_id=self._task.job_id,
                job_version=self._task.version,
                expected_schedule_fingerprint=self._schedule_fingerprint,
                expected_cursor_revision=cursor.cursor_revision,
                expected_from=cursor.reconciled_through,
                checked_at=checked_at,
                due_through=due_through,
                new_cursor=cursor.reconciled_through,
                expected_occurrences=(),
                previous_cursor=cursor,
                schedule_snapshot_json=self._schedule_snapshot_json,
                initial_from=self._initial_from,
                initial_from_basis=self._initial_from_basis,
            )

        occurrences = _enumerate_occurrences(
            self._crontab,
            cursor.reconciled_through,
            due_through,
            limit=MAX_SLOTS_PER_BATCH,
        )
        if len(occurrences) > MAX_SLOTS_PER_BATCH:
            committed_occurrences = occurrences[:MAX_SLOTS_PER_BATCH]
            new_cursor = committed_occurrences[-1]
        else:
            committed_occurrences = occurrences
            new_cursor = due_through

        return _reconcile_batch(
            self._journal,
            job_id=self._task.job_id,
            job_version=self._task.version,
            expected_schedule_fingerprint=self._schedule_fingerprint,
            expected_cursor_revision=cursor.cursor_revision,
            expected_from=cursor.reconciled_through,
            checked_at=checked_at,
            due_through=due_through,
            new_cursor=new_cursor,
            expected_occurrences=tuple(committed_occurrences),
            previous_cursor=cursor,
            schedule_snapshot_json=self._schedule_snapshot_json,
            initial_from=self._initial_from,
            initial_from_basis=self._initial_from_basis,
        )


def _reconcile_batch(
    journal: _ReconciliationJournal,
    *,
    job_id: str,
    job_version: str,
    expected_schedule_fingerprint: str,
    expected_cursor_revision: int,
    expected_from: datetime,
    checked_at: datetime,
    due_through: datetime,
    new_cursor: datetime,
    expected_occurrences: tuple[datetime, ...],
    previous_cursor: ReconciliationCursor,
    schedule_snapshot_json: str,
    initial_from: datetime,
    initial_from_basis: str,
) -> ReconciliationBatch:
    """Persist one batch and fail closed if its returned cursor is inconsistent."""
    batch = journal.reconcile_batch(
        job_id=job_id,
        job_version=job_version,
        expected_schedule_fingerprint=expected_schedule_fingerprint,
        expected_cursor_revision=expected_cursor_revision,
        expected_from=expected_from,
        checked_at=checked_at,
        due_through=due_through,
        new_cursor=new_cursor,
        expected_occurrences=expected_occurrences,
    )
    _validate_cursor(
        batch.cursor,
        job_id=job_id,
        job_version=job_version,
        fingerprint=expected_schedule_fingerprint,
        schedule_snapshot_json=schedule_snapshot_json,
        initial_from=initial_from,
        initial_from_basis=initial_from_basis,
    )
    if batch.cursor.reconciled_through != new_cursor:
        raise RuntimeError("journal batch returned an unexpected reconciliation watermark")
    if batch.cursor.cursor_revision != previous_cursor.cursor_revision + 1:
        raise RuntimeError("journal batch did not advance the cursor revision exactly once")
    return batch


def _validate_task(task: TaskDefinition) -> ScheduleMetadata:
    """Require the exact supported job, cron dialect, and calendar policy."""
    if task.job_id != SINA_FZB_JOB_ID:
        raise UnsupportedSinaFzbScheduleError("only the reviewed Sina FZB job is supported")
    if task.timezone != "UTC":
        raise UnsupportedSinaFzbScheduleError("slot reconciliation supports timezone='UTC' only")
    if task.calendar is not None:
        raise UnsupportedSinaFzbScheduleError(
            "slot reconciliation is cron-only and rejects trading calendars"
        )
    schedule = task.schedule
    if schedule is None or schedule.kind != "cron":
        raise UnsupportedSinaFzbScheduleError("slot reconciliation requires explicit cron metadata")
    fields = schedule.expression.split()
    if len(fields) != 5:
        raise UnsupportedSinaFzbScheduleError("cron expression must contain five fields")
    weekday = fields[4]
    if weekday != "*" and _WEEKDAY_FIELD.fullmatch(weekday) is None:
        if re.search(r"\d", weekday):
            raise UnsupportedSinaFzbScheduleError("numeric weekday cron fields are ambiguous")
        raise UnsupportedSinaFzbScheduleError(
            "weekday cron field must be '*' or use named weekdays"
        )
    _cron_trigger(schedule.expression)
    return schedule


def _canonical_schedule_snapshot(task: TaskDefinition) -> str:
    """Serialize the exact v1 cron-only contract used by the journal."""
    schedule = task.schedule
    if schedule is None:
        raise UnsupportedSinaFzbScheduleError("slot reconciliation requires explicit cron metadata")
    snapshot = {
        "calendar_policy": "cron_only",
        "contract_version": RECONCILIATION_CONTRACT_VERSION,
        "crontab": schedule.expression,
        "grace_seconds": SLOT_GRACE_SECONDS,
        "job_id": task.job_id,
        "job_version": task.version,
        "max_recovery_slots_per_batch": MAX_RECOVERY_SLOTS_PER_BATCH,
        "max_slots_per_batch": MAX_SLOTS_PER_BATCH,
        "schedule_kind": "cron",
        "selection_policy": "unknown_if_no_slot_evidence",
        "timezone": "UTC",
    }
    if frozenset(snapshot) != _SCHEDULE_SNAPSHOT_KEYS:
        raise RuntimeError("schedule snapshot schema changed without a contract version update")
    return json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_initial_from_basis(value: str) -> str:
    """Require a bounded caller-declared boundary reference without prose or controls."""
    if type(value) is not str:
        raise ValueError("initial_from_basis must be a string")
    if not _INITIAL_FROM_BASIS_PATTERN.fullmatch(value):
        raise ValueError("initial_from_basis must be a bounded identifier without whitespace")
    return value


def _enumerate_occurrences(
    expression: str,
    after: datetime,
    through: datetime,
    *,
    limit: int,
) -> list[datetime]:
    """Return at most ``limit + 1`` strictly bounded UTC cron occurrences."""
    if limit <= 0:
        raise ValueError("limit must be positive")
    if through <= after:
        return []
    trigger = _cron_trigger(expression)
    occurrences: list[datetime] = []
    previous_fire_time: datetime | None = after
    next_fire_time = trigger.get_next_fire_time(previous_fire_time, after)
    while next_fire_time is not None:
        occurrence = _require_utc(next_fire_time, "cron occurrence")
        if occurrence <= after:
            raise RuntimeError("cron trigger returned an occurrence outside the exclusive window")
        if occurrence > through:
            break
        occurrences.append(occurrence)
        if len(occurrences) > limit:
            break
        previous_fire_time = next_fire_time
        next_fire_time = trigger.get_next_fire_time(previous_fire_time, previous_fire_time)
    return occurrences


def _cron_trigger(expression: str) -> _CronTrigger:
    """Create the APScheduler trigger lazily so APScheduler stays optional."""
    try:
        from apscheduler.triggers.cron import CronTrigger
    except ImportError as exc:
        raise RuntimeError("APScheduler is required for UTC cron reconciliation") from exc
    try:
        trigger: _CronTrigger = CronTrigger.from_crontab(expression, timezone="UTC")
        return trigger
    except (TypeError, ValueError) as exc:
        raise UnsupportedSinaFzbScheduleError("invalid APScheduler cron expression") from exc


def _require_utc(value: datetime, label: str) -> datetime:
    """Require an aware zero-offset instant and normalize it to UTC."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be a timezone-aware UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC)


def _validate_cursor(
    cursor: ReconciliationCursor,
    *,
    job_id: str,
    job_version: str,
    fingerprint: str,
    schedule_snapshot_json: str,
    initial_from: datetime,
    initial_from_basis: str,
) -> None:
    """Fail closed if a journal returns a cursor for a different contract."""
    if cursor.job_id != job_id or cursor.job_version != job_version:
        raise RuntimeError("journal cursor does not match the requested job version")
    if cursor.schedule_fingerprint != fingerprint:
        raise RuntimeError("journal cursor schedule fingerprint does not match")
    if cursor.schedule_snapshot_json != schedule_snapshot_json:
        raise RuntimeError("journal cursor schedule snapshot does not match")
    if cursor.calendar_policy != "cron_only":
        raise RuntimeError("journal cursor must retain the cron-only calendar policy")
    if cursor.status != "ACTIVE":
        raise RuntimeError("journal cursor is not active")
    if cursor.initial_from != initial_from:
        raise RuntimeError("journal cursor initial boundary does not match")
    if cursor.initial_from_basis != initial_from_basis:
        raise RuntimeError("journal cursor initial boundary basis does not match")
    if cursor.earlier_history_status != "NOT_ASSESSED":
        raise RuntimeError("journal cursor must retain NOT_ASSESSED before the initial boundary")
    if cursor.grace_seconds != SLOT_GRACE_SECONDS:
        raise RuntimeError("journal cursor grace period does not match the schedule contract")
    _require_utc(cursor.reconciled_through, "journal watermark")
    if type(cursor.cursor_revision) is not int or cursor.cursor_revision < 0:
        raise RuntimeError("journal cursor revision is invalid")
    if cursor.reconciled_through < initial_from:
        raise RuntimeError("journal watermark precedes the initial boundary")
