"""Durable claims and cross-worker limits for AkShare executions.

This module re-implements the extracted contract of the ghost-code plane
preserved at ``~/afi204-evidence/2026-11-06-ghost-code-plane`` (only the
compiled bytecode of ``run_coordinator.py`` and its models survived; no
git ref ever held the sources).

Enablement pre-check (required before this coordinator is wired into the
live execution path): inspect how many ``ak_task_run_claims`` rows are
non-terminal and what ``recoverable_claims()`` returns on the running
system first. The legacy plane left thousands of ``failed`` rows whose
``next_retry_at`` may still gate collection; claims and cooldowns are
never reset or cleared to "unblock" work — ``recoverable_claims()`` is
the only sanctioned startup-recovery read path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError

from opendata.afi_port.runtime.persistence import cursor_rowcount
from opendata.core import database
from opendata.core.config import get_settings
from opendata.models.data_script import DataScript
from opendata.models.run_claim import SchedulerControl, TaskRunClaim
from opendata.models.task import ScheduledTask, TaskExecution, TriggeredBy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

RUNNING = "running"
RETRY_WAIT = "retry_wait"
COMPLETED = "completed"
FAILED = "failed"

LIVE_TASK_DUE_LOOKAHEAD_SECONDS = 30
LIVE_TASK_MISFIRE_GRACE_SECONDS = 3600

# The three settings below are read through ``getattr`` with explicit fallbacks
# because ``app/config.py`` is frozen at its large-file ratchet baseline and
# cannot gain fields in this PR. Enablement should introduce the real Settings
# fields in a change that also carries the reviewed baseline update; until then
# these defaults are the documented coordination budget.
DEFAULT_PER_SOURCE_CONCURRENT_TASKS = 1
DEFAULT_LEASE_SECONDS = 1800
DEFAULT_SOURCE_MIN_INTERVAL_SECONDS = 0.0


def utcnow() -> datetime:
    """Return UTC as a timezone-naive value for the project's database columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class RunLease:
    """Fenced execution authority returned by a successful claim."""

    claim_key: str
    owner_id: str
    fencing_token: int
    lease_expires_at: datetime
    attempt_count: int


@dataclass(frozen=True)
class ClaimOutcome:
    """Result of an atomic execution claim attempt."""

    state: str
    lease: RunLease | None = None
    execution_id: str | None = None
    wait_seconds: float = 0.0
    reason: str | None = None


class AkshareRunCoordinator:
    """Persist run ownership and serialize shared execution/source budgets."""

    def __init__(self) -> None:
        """Bind the coordinator to the process settings and its own session factory."""
        self.settings = get_settings()

    def _budget_setting(self, name: str, default: int | float) -> int | float:
        return getattr(self.settings, name, default)

    @staticmethod
    def _session_factory() -> AsyncSession:
        return database.async_session_maker()

    async def try_claim(
        self,
        *,
        claim_key: str,
        owner_id: str | None = None,
        task_id: int | None,
        script_id: str,
        source: str,
        period_key: str,
        parameter_hash: str,
        parameters: dict[str, Any],
        max_attempts: int,
        allow_manual_retry: bool = False,
        defer_for_live_tasks: bool = False,
    ) -> ClaimOutcome:
        """Claim a business period if it is due and has shared capacity.

        Identity follows the project invariant: a true cron occurrence is
        ``cron:<UTC ISO>`` while a manual/direct run is ``day:<Shanghai
        date>``. Distinct period identities therefore occupy distinct
        claim slots, and a manual run never consumes a cron occurrence.
        """
        for attempt in range(2):
            try:
                return await self._try_claim_once(
                    claim_key=claim_key,
                    task_id=task_id,
                    script_id=script_id,
                    source=source,
                    period_key=period_key,
                    parameter_hash=parameter_hash,
                    parameters=parameters,
                    max_attempts=max_attempts,
                    allow_manual_retry=allow_manual_retry,
                    defer_for_live_tasks=defer_for_live_tasks,
                    owner_id=owner_id,
                )
            except IntegrityError:  # noqa: PERF203  # bounded single-retry on a claim race
                # A competing worker inserted the same business period between our
                # locked read and the INSERT. Retry once so the second pass reads
                # the committed winner instead of racing the primary key again; a
                # second collision is a real anomaly and must surface.
                if attempt:
                    raise
        raise RuntimeError("Unable to claim AkShare run")

    async def _try_claim_once(
        self,
        *,
        claim_key: str,
        task_id: int | None,
        script_id: str,
        source: str,
        period_key: str,
        parameter_hash: str,
        parameters: dict[str, Any],
        max_attempts: int,
        allow_manual_retry: bool,
        defer_for_live_tasks: bool,
        owner_id: str | None,
    ) -> ClaimOutcome:
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            control = await self._locked_control(session)
            claim = await session.get(TaskRunClaim, claim_key, with_for_update=True)

            if claim is not None:
                if claim.status == COMPLETED:
                    return ClaimOutcome("completed", execution_id=claim.execution_id)
                if claim.status == RUNNING and claim.lease_expires_at is not None:
                    expires_at = _as_naive_utc(claim.lease_expires_at)
                    if expires_at > now:
                        return ClaimOutcome(
                            "in_progress",
                            execution_id=claim.execution_id,
                            wait_seconds=max(0.25, (expires_at - now).total_seconds()),
                        )
                if claim.status == RETRY_WAIT and claim.next_retry_at is not None:
                    retry_at = _as_naive_utc(claim.next_retry_at)
                    if retry_at > now:
                        return ClaimOutcome(
                            "retry_wait",
                            execution_id=claim.execution_id,
                            wait_seconds=max(0.25, (retry_at - now).total_seconds()),
                        )
                if claim.status == FAILED and not allow_manual_retry:
                    return ClaimOutcome("failed", execution_id=claim.execution_id)
                if (
                    claim.attempt_count >= max_attempts
                    and not allow_manual_retry
                    and claim.status != RUNNING
                ):
                    if claim.status == RETRY_WAIT:
                        claim.status = FAILED
                        claim.lease_expires_at = None
                        claim.next_retry_at = None
                        claim.updated_at = now
                        await session.flush()
                    return ClaimOutcome("exhausted", execution_id=claim.execution_id)

            global_limit = max(1, int(self.settings.akshare_scheduler_max_concurrent_tasks))
            source_limit = max(
                1,
                int(
                    self._budget_setting(
                        "akshare_scheduler_per_source_concurrent_tasks",
                        DEFAULT_PER_SOURCE_CONCURRENT_TASKS,
                    )
                ),
            )

            global_count = await self._active_count(session, now)
            source_count = await self._active_count(session, now, source=source)
            if global_count >= global_limit or source_count >= source_limit:
                expiry = await self._earliest_expiry(
                    session,
                    now,
                    source=source if source_count >= source_limit else None,
                )
                wait_seconds = max(0.25, (expiry - now).total_seconds()) if expiry else 1.0
                return ClaimOutcome("busy", wait_seconds=wait_seconds)

            if defer_for_live_tasks and task_id is None:
                due_by_source = await self._due_scheduled_task_counts(session, now)
                due_global = sum(due_by_source.values())
                due_for_source = due_by_source.get(source, 0)
                if (
                    global_count + due_global >= global_limit
                    or source_count + due_for_source >= source_limit
                ):
                    return ClaimOutcome("busy", wait_seconds=1.0, reason="live_task_due")

            source_gates = dict(control.source_next_allowed_at or {})
            source_next_allowed = source_gates.get(source)
            if source_next_allowed:
                next_allowed = _as_naive_utc(datetime.fromisoformat(source_next_allowed))
                if next_allowed > now:
                    return ClaimOutcome(
                        "busy",
                        wait_seconds=max(0.25, (next_allowed - now).total_seconds()),
                    )

            lease_seconds = max(
                int(self._budget_setting("akshare_scheduler_lease_seconds", DEFAULT_LEASE_SECONDS)),
                30,
            )
            lease_expires_at = now + timedelta(seconds=lease_seconds)
            owner_id = owner_id or uuid4().hex
            fencing_token = (claim.fencing_token if claim is not None else 0) + 1
            attempt_count = (claim.attempt_count if claim is not None else 0) + 1
            if claim is None:
                claim = TaskRunClaim(
                    claim_key=claim_key,
                    task_id=task_id,
                    script_id=script_id,
                    source=source,
                    period_key=period_key,
                    parameter_hash=parameter_hash,
                    parameters=_json_parameters(parameters),
                    status=RUNNING,
                    owner_id=owner_id,
                    fencing_token=fencing_token,
                    lease_expires_at=lease_expires_at,
                    attempt_count=attempt_count,
                    created_at=now,
                    updated_at=now,
                )
                session.add(claim)
            else:
                claim.task_id = task_id
                claim.script_id = script_id
                claim.source = source
                claim.period_key = period_key
                claim.parameter_hash = parameter_hash
                claim.parameters = _json_parameters(parameters)
                claim.status = RUNNING
                claim.owner_id = owner_id
                claim.fencing_token = fencing_token
                claim.lease_expires_at = lease_expires_at
                claim.attempt_count = attempt_count
                claim.next_retry_at = None
                claim.execution_id = None
                claim.last_error = None
                claim.completed_at = None
                claim.updated_at = now

            source_interval = max(
                0.0,
                float(
                    self._budget_setting(
                        "akshare_scheduler_source_min_interval_seconds",
                        DEFAULT_SOURCE_MIN_INTERVAL_SECONDS,
                    )
                ),
            )
            if source_interval:
                source_gates[source] = (now + timedelta(seconds=source_interval)).isoformat()
                control.source_next_allowed_at = source_gates
                control.updated_at = now
            await session.flush()

            lease = RunLease(
                claim_key=claim_key,
                owner_id=owner_id,
                fencing_token=fencing_token,
                lease_expires_at=lease_expires_at,
                attempt_count=attempt_count,
            )
            return ClaimOutcome("acquired", lease=lease)

    async def renew(self, lease: RunLease, lease_seconds: int) -> bool:
        """Extend a still-current owner lease without changing its fence."""
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            result = await session.execute(
                update(TaskRunClaim)
                .where(
                    TaskRunClaim.claim_key == lease.claim_key,
                    TaskRunClaim.owner_id == lease.owner_id,
                    TaskRunClaim.fencing_token == lease.fencing_token,
                    TaskRunClaim.status == RUNNING,
                    TaskRunClaim.lease_expires_at > now,
                )
                .values(
                    lease_expires_at=now + timedelta(seconds=max(30, lease_seconds)),
                    updated_at=now,
                )
            )
            return cursor_rowcount(result) == 1

    async def set_execution_id(self, lease: RunLease, execution_id: str) -> bool:
        """Associate an execution record with its current claim owner."""
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            result = await session.execute(
                update(TaskRunClaim)
                .where(
                    TaskRunClaim.claim_key == lease.claim_key,
                    TaskRunClaim.owner_id == lease.owner_id,
                    TaskRunClaim.fencing_token == lease.fencing_token,
                    TaskRunClaim.status == RUNNING,
                    TaskRunClaim.lease_expires_at > now,
                )
                .values(execution_id=execution_id, updated_at=now)
            )
            return cursor_rowcount(result) == 1

    async def complete(self, lease: RunLease, execution_id: str) -> bool:
        """Commit successful completion only for the live fenced owner.

        A stale fencing token (or an expired/lost lease) simply matches zero
        rows: the conditional UPDATE refuses the write and this returns
        ``False`` without mutating the current owner's claim.
        """
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            result = await session.execute(
                update(TaskRunClaim)
                .where(
                    TaskRunClaim.claim_key == lease.claim_key,
                    TaskRunClaim.owner_id == lease.owner_id,
                    TaskRunClaim.fencing_token == lease.fencing_token,
                    TaskRunClaim.status == RUNNING,
                    TaskRunClaim.lease_expires_at > now,
                )
                .values(
                    status=COMPLETED,
                    execution_id=execution_id,
                    lease_expires_at=None,
                    next_retry_at=None,
                    completed_at=now,
                    updated_at=now,
                )
            )
            return cursor_rowcount(result) == 1

    async def fail(
        self,
        lease: RunLease,
        error: str,
        execution_id: str | None,
        retry_at: datetime | None,
    ) -> bool:
        """Release the run slot and persist retry or terminal failure state."""
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            result = await session.execute(
                update(TaskRunClaim)
                .where(
                    TaskRunClaim.claim_key == lease.claim_key,
                    TaskRunClaim.owner_id == lease.owner_id,
                    TaskRunClaim.fencing_token == lease.fencing_token,
                    TaskRunClaim.status == RUNNING,
                )
                .values(
                    status=RETRY_WAIT if retry_at is not None else FAILED,
                    execution_id=execution_id,
                    lease_expires_at=None,
                    next_retry_at=retry_at,
                    last_error=error[:4000],
                    updated_at=now,
                )
            )
            return cursor_rowcount(result) == 1

    async def cancel(self, lease: RunLease, error: str, execution_id: str | None) -> bool:
        """Revoke a cancelled owner's lease, including a just-completed claim.

        The owner and fence must still match. A completion racing with request
        cancellation is therefore recorded as cancelled rather than left as a
        reusable completed result.
        """
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            result = await session.execute(
                update(TaskRunClaim)
                .where(
                    TaskRunClaim.claim_key == lease.claim_key,
                    TaskRunClaim.owner_id == lease.owner_id,
                    TaskRunClaim.fencing_token == lease.fencing_token,
                    TaskRunClaim.status.in_(
                        (
                            RUNNING,
                            COMPLETED,
                        )
                    ),
                )
                .values(
                    status=FAILED,
                    execution_id=execution_id,
                    lease_expires_at=None,
                    next_retry_at=None,
                    completed_at=None,
                    last_error=error[:4000],
                    updated_at=now,
                )
            )
            return cursor_rowcount(result) == 1

    async def cancel_unreturned_claim(self, claim_key: str, owner_id: str, error: str) -> bool:
        """Revoke an acquisition interrupted before the scheduler received its lease.

        ``owner_id`` is generated by the acquiring call and unique to it, so a
        cancellation can safely clean up the narrow commit/return race without
        touching an existing or replacement owner.
        """
        now = utcnow()
        async with self._session_factory() as session, session.begin():
            await self._locked_control(session)
            claim = await session.get(TaskRunClaim, claim_key, with_for_update=True)
            if claim is None or claim.owner_id != owner_id or claim.status != RUNNING:
                return False
            claim.status = FAILED
            claim.lease_expires_at = None
            claim.next_retry_at = None
            claim.last_error = error[:4000]
            claim.updated_at = now
            await session.flush()
            return True

    async def recoverable_claims(self) -> list[TaskRunClaim]:
        """Return delayed retries and all leases that need startup recovery."""
        async with self._session_factory() as session:
            result = await session.execute(
                select(TaskRunClaim)
                .where((TaskRunClaim.status == RETRY_WAIT) | (TaskRunClaim.status == RUNNING))
                .order_by(TaskRunClaim.updated_at.asc())
            )
            return list(result.scalars().all())

    async def get_claim(self, claim_key: str) -> TaskRunClaim | None:
        """Fetch one claim row by its business-period key."""
        async with self._session_factory() as session:
            return await session.get(TaskRunClaim, claim_key)

    async def _locked_control(self, session: AsyncSession) -> SchedulerControl:
        result = await session.execute(
            select(SchedulerControl)
            .where(SchedulerControl.control_id == 1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        control = result.scalar_one_or_none()
        if control is None:
            control = SchedulerControl(
                control_id=1,
                source_next_allowed_at={},
                updated_at=utcnow(),
            )
            session.add(control)
            await session.flush()
            result = await session.execute(
                select(SchedulerControl)
                .where(SchedulerControl.control_id == 1)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            control = result.scalar_one()
        return control

    @staticmethod
    async def _active_count(
        session: AsyncSession,
        now: datetime,
        *,
        source: str | None = None,
    ) -> int:
        statement = (
            select(func.count())
            .select_from(TaskRunClaim)
            .where(
                TaskRunClaim.status == RUNNING,
                TaskRunClaim.lease_expires_at > now,
            )
        )
        if source is not None:
            statement = statement.where(TaskRunClaim.source == source)
        return int(await session.scalar(statement) or 0)

    @staticmethod
    async def _due_scheduled_task_counts(session: AsyncSession, now: datetime) -> dict[str, int]:
        """Count runnable, unclaimed task periods due within the priority window.

        This query is read-only and runs after the shared control row has been
        locked. Claim writers take that control lock before claim rows, so this
        introduces no reverse lock order and sees claims committed by competing
        coordinator instances before deciding whether a backfill may proceed.
        The scheduler-execution exclusion is correlated by ``task_id``, whose
        existing index keeps the due check bounded to each candidate's history.
        """
        active_claim = and_(
            TaskRunClaim.task_id == ScheduledTask.id,
            TaskRunClaim.status == RUNNING,
            TaskRunClaim.lease_expires_at > now,
        )
        scheduled_execution_consumed_due = (
            select(TaskExecution.id)
            .where(
                TaskExecution.task_id == ScheduledTask.id,
                TaskExecution.triggered_by == TriggeredBy.SCHEDULER,
                TaskExecution.start_time >= ScheduledTask.next_execution_at,
            )
            .exists()
        )
        statement = (
            select(DataScript.source, func.count(ScheduledTask.id))
            .select_from(ScheduledTask)
            .join(DataScript, DataScript.script_id == ScheduledTask.script_id)
            .outerjoin(TaskRunClaim, active_claim)
            .where(
                ScheduledTask.is_active.is_(True),
                DataScript.is_active.is_(True),
                ScheduledTask.next_execution_at.is_not(None),
                ScheduledTask.next_execution_at
                <= now + timedelta(seconds=LIVE_TASK_DUE_LOOKAHEAD_SECONDS),
                ScheduledTask.next_execution_at
                >= now - timedelta(seconds=LIVE_TASK_MISFIRE_GRACE_SECONDS),
                ~scheduled_execution_consumed_due,
                TaskRunClaim.claim_key.is_(None),
            )
            .group_by(DataScript.source)
        )
        rows = (await session.execute(statement)).all()
        return {str(source or "akshare"): int(count) for source, count in rows}

    @staticmethod
    async def _earliest_expiry(
        session: AsyncSession,
        now: datetime,
        *,
        source: str | None = None,
    ) -> datetime | None:
        statement = select(func.min(TaskRunClaim.lease_expires_at)).where(
            TaskRunClaim.status == RUNNING,
            TaskRunClaim.lease_expires_at > now,
        )
        if source is not None:
            statement = statement.where(TaskRunClaim.source == source)
        expiry = await session.scalar(statement)
        return _as_naive_utc(expiry) if expiry is not None else None


def _as_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _json_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy so a direct run can be recovered after restart."""
    safe_copy: dict[str, Any] = json.loads(
        json.dumps(parameters, ensure_ascii=False, default=str)
    )
    return safe_copy
