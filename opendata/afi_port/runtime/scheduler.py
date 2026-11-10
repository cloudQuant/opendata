"""Single-instance APScheduler wrapper for the ported akshare task plane."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from opendata.afi_port.runtime.clock import to_naive_utc, utcnow_naive
from opendata.afi_port.services.script import AkshareScriptService
from opendata.core.config import get_settings
from opendata.core.database import async_session_maker
from opendata.models.task import ScheduledTask, ScheduleType, TriggeredBy

if TYPE_CHECKING:
    from opendata.models.task import TaskExecution

settings = get_settings()

_JOB_ID_PREFIX = "ak_task_"


def _job_id(task_id: int) -> str:
    return f"{_JOB_ID_PREFIX}{task_id}"


def _inactive_job_task_ids(armed_ids: list[int], job_ids: list[str]) -> list[int]:
    """Registered akshare job ids whose task is no longer in the armed set."""
    armed = set(armed_ids)
    stale: list[int] = []
    for job_id in job_ids:
        if not job_id.startswith(_JOB_ID_PREFIX):
            continue
        try:
            task_id = int(job_id[len(_JOB_ID_PREFIX) :])
        except ValueError:
            continue
        if task_id not in armed:
            stale.append(task_id)
    return stale


class AkshareScheduler:
    """In-memory scheduler for akshare tasks."""

    def __init__(self) -> None:
        """Create the wrapper without starting APScheduler or its concurrency gate."""
        self.scheduler = None
        self._run_semaphore: asyncio.Semaphore | None = None
        self._run_semaphore_limit = max(1, int(settings.akshare_scheduler_max_concurrent_tasks))

    def _ensure_run_semaphore(self) -> asyncio.Semaphore:
        if self._run_semaphore is None:
            self._run_semaphore = asyncio.Semaphore(self._run_semaphore_limit)
        return self._run_semaphore

    def _ensure_scheduler(self) -> Any:  # noqa: ANN401
        if self.scheduler is not None:
            return self.scheduler
        try:
            from apscheduler.executors.asyncio import AsyncIOExecutor
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except ImportError as exc:
            raise RuntimeError("APScheduler is not installed") from exc

        self.scheduler = AsyncIOScheduler(
            executors={"default": AsyncIOExecutor()},
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
            timezone=settings.akshare_scheduler_timezone,
        )
        return self.scheduler

    async def start(self) -> None:
        """Start the APScheduler instance and load every armed task."""
        scheduler = self._ensure_scheduler()
        if not scheduler.running:
            scheduler.start()
            await self.reload_active_tasks()

    async def shutdown(self) -> None:
        """Stop the scheduler and drop its in-memory job store."""
        if self.scheduler is not None and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def _build_trigger(self, task: ScheduledTask) -> Any:  # noqa: ANN401
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.date import DateTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        if task.schedule_type == ScheduleType.CRON:
            return CronTrigger.from_crontab(
                task.schedule_expression, timezone=settings.akshare_scheduler_timezone
            )
        if task.schedule_type == ScheduleType.DAILY and ":" in task.schedule_expression:
            hour, minute = task.schedule_expression.split(":")
            return CronTrigger(
                hour=int(hour),
                minute=int(minute),
                timezone=settings.akshare_scheduler_timezone,
            )
        if task.schedule_type in {ScheduleType.DAILY, ScheduleType.WEEKLY, ScheduleType.MONTHLY}:
            return CronTrigger.from_crontab(
                task.schedule_expression, timezone=settings.akshare_scheduler_timezone
            )
        if task.schedule_type == ScheduleType.INTERVAL:
            expression = task.schedule_expression.strip().lower()
            if expression.endswith("m"):
                return IntervalTrigger(minutes=int(expression[:-1]))
            if expression.endswith("h"):
                return IntervalTrigger(hours=int(expression[:-1]))
            if expression.endswith("d"):
                return IntervalTrigger(days=int(expression[:-1]))
            return IntervalTrigger(minutes=int(expression))
        return DateTrigger(run_date=datetime.now())

    async def _run_task_job(self, task_id: int) -> None:
        async with self._ensure_run_semaphore():
            await self.run_task_now(task_id)

    async def add_or_update_task(self, task_id: int) -> None:
        """Register or replace one task's trigger, or remove it when inactive."""
        scheduler = self._ensure_scheduler()
        async with async_session_maker() as session:
            task = await session.get(ScheduledTask, task_id)
            if task is None:
                return
            job_id = _job_id(task.id)
            if not task.is_active:
                if scheduler.get_job(job_id):
                    scheduler.remove_job(job_id)
                task.next_execution_at = None
                await session.commit()
                return
            job = scheduler.add_job(
                self._run_task_job,
                trigger=self._build_trigger(task),
                id=job_id,
                replace_existing=True,
                kwargs={"task_id": task.id},
                name=task.name,
            )
            task.next_execution_at = to_naive_utc(getattr(job, "next_run_time", None))
            await session.commit()

    async def remove_task(self, task_id: int) -> None:
        """Remove one task's scheduled job if present."""
        scheduler = self._ensure_scheduler()
        job_id = _job_id(task_id)
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    async def reload_active_tasks(self) -> None:
        """Reconcile scheduler jobs with the set of armed tasks, dropping stale ones."""
        async with async_session_maker() as session:
            result = await session.execute(
                select(ScheduledTask.id).where(ScheduledTask.is_active.is_(True))
            )
            task_ids = [row[0] for row in result.all()]
        for task_id in task_ids:
            await self.add_or_update_task(task_id)
        # A task deactivated after its job was registered keeps firing unless it is dropped here:
        # add_or_update_task only ever visits active ids, so the stale job would outlive the flag.
        scheduler = self._ensure_scheduler()
        stale = _inactive_job_task_ids(task_ids, [str(job.id) for job in scheduler.get_jobs()])
        for job_task_id in stale:
            await self.add_or_update_task(job_task_id)

    async def run_task_now(self, task_id: int, operator_id: str | None = None) -> TaskExecution:
        """Execute one task immediately and refresh its execution timestamps."""
        async with async_session_maker() as session:
            task = await session.get(ScheduledTask, task_id)
            if task is None:
                raise ValueError("Task not found")
            if not task.script_id:
                raise ValueError(f"Task {task_id} has no script_id and cannot run")
            service = AkshareScriptService(session)
            execution = await service.run_script(
                task.script_id,
                parameters=task.parameters,
                operator_id=operator_id,
                task_id=task.id,
                triggered_by=TriggeredBy.MANUAL if operator_id else TriggeredBy.SCHEDULER,
                timeout_seconds=task.timeout,
            )
            scheduler = self._ensure_scheduler()
            job = scheduler.get_job(_job_id(task.id))
            task.last_execution_at = utcnow_naive()
            task.next_execution_at = (
                to_naive_utc(getattr(job, "next_run_time", None)) if job else None
            )
            await session.commit()
            return execution
