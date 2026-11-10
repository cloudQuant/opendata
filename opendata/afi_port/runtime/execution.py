"""Execution service for the ported AkShare task runtime.

Ported from AFI's ``services/akshare/execution.py``; rebound onto OpenData's
own model plane (:mod:`opendata.models.task`).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select

from opendata.afi_port.runtime.clock import utcnow_naive
from opendata.models.task import ScheduledTask, TaskExecution, TaskStatus, TriggeredBy

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession


def failure_frame_trace(exc: BaseException, *, limit: int = 24) -> str:
    """Where the exception was raised and through which frames, without locals or messages.

    A traceback's text carries whatever the raising code put in it -- DSNs, tokens,
    request payloads -- so the ledger stores the frame chain instead: it answers
    "where did it die" without copying values that must not be persisted.
    """
    lines: list[str] = []
    tb = exc.__traceback__
    seen = 0
    while tb is not None and seen < limit:
        code = tb.tb_frame.f_code
        lines.append(f"{code.co_filename}:{tb.tb_lineno} in {code.co_name}")
        tb = tb.tb_next
        seen += 1
    if tb is not None:
        lines.append(f"... truncated after {limit} frames")
    return "\n".join(lines)


class AkshareExecutionService:
    """Service for creating and querying akshare execution records."""

    def __init__(self, db: AsyncSession) -> None:
        """Bind the service to one request-scoped async session."""
        self.db = db

    @staticmethod
    def _utcnow_naive() -> datetime:
        """Return the runtime clock's naive-UTC now."""
        return utcnow_naive()

    @staticmethod
    def _duration_seconds(start_time: datetime | None, end_time: datetime) -> float | None:
        """Clamped elapsed seconds between two timestamps."""
        if start_time is None:
            return None
        return max((end_time - start_time).total_seconds(), 0)

    async def create_execution(
        self,
        script_id: str,
        task_id: int | None = None,
        params: dict | None = None,
        triggered_by: TriggeredBy = TriggeredBy.SCHEDULER,
        operator_id: int | None = None,
    ) -> TaskExecution:
        """Open a pending execution ledger row and commit it."""
        execution = TaskExecution(
            execution_id=f"ak_exec_{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            script_id=script_id,
            params=params,
            status=TaskStatus.PENDING,
            triggered_by=triggered_by,
            operator_id=operator_id,
        )
        self.db.add(execution)
        await self.db.commit()
        await self.db.refresh(execution)
        return execution

    async def mark_running(self, execution: TaskExecution) -> TaskExecution:
        """Move an execution to running and stamp its start time."""
        execution_row: Any = execution
        execution_row.status = TaskStatus.RUNNING
        execution_row.start_time = self._utcnow_naive()
        await self.db.commit()
        await self.db.refresh(execution)
        return execution

    async def mark_completed(
        self,
        execution: TaskExecution,
        result: dict | None = None,
        rows_before: int | None = None,
        rows_after: int | None = None,
    ) -> TaskExecution:
        """Close an execution as completed and refresh the parent task timestamp."""
        end_time = self._utcnow_naive()
        execution_row: Any = execution
        execution_row.status = TaskStatus.COMPLETED
        execution_row.end_time = end_time
        execution_row.duration = self._duration_seconds(execution.start_time, end_time)
        execution_row.result = result
        execution_row.rows_before = rows_before
        execution_row.rows_after = rows_after
        await self.db.commit()
        await self.db.refresh(execution)
        if execution.task_id:
            task = await self.db.get(ScheduledTask, execution.task_id)
            if task is not None:
                task_row: Any = task
                task_row.last_execution_at = end_time
                await self.db.commit()
        return execution

    async def mark_failed(
        self,
        execution: TaskExecution,
        error_message: str,
        error_trace: str | None = None,
        rows_before: int | None = None,
        rows_after: int | None = None,
    ) -> TaskExecution:
        """Record a failed run, including any row deltas the caller observed.

        ``rows_before``/``rows_after`` carry the same meaning as in
        :meth:`mark_completed`: a failed run can still have committed warehouse
        rows, and the ledger must be able to say so.  A caller that cannot
        observe the counts (e.g. the worker was SIGKILLed before anything could
        be re-read) leaves them ``None`` so the columns stay NULL rather than
        recording a fabricated zero.
        """
        end_time = self._utcnow_naive()
        execution_row: Any = execution
        execution_row.status = TaskStatus.FAILED
        execution_row.end_time = end_time
        execution_row.duration = self._duration_seconds(execution.start_time, end_time)
        execution_row.error_message = error_message
        execution_row.error_trace = error_trace
        execution_row.rows_before = rows_before
        execution_row.rows_after = rows_after
        await self.db.commit()
        await self.db.refresh(execution)
        return execution

    async def get_execution(self, execution_id: str) -> TaskExecution | None:
        """Fetch one execution by its public execution id."""
        result = await self.db.execute(
            select(TaskExecution).where(TaskExecution.execution_id == execution_id)
        )
        return result.scalar_one_or_none()

    async def list_executions(
        self,
        task_id: int | None = None,
        script_id: str | None = None,
        status: str | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[TaskExecution], int]:
        """Page over executions filtered by task, script, status and window."""
        filters = []
        if task_id is not None:
            filters.append(TaskExecution.task_id == task_id)
        if script_id:
            filters.append(TaskExecution.script_id == script_id)
        if status:
            filters.append(TaskExecution.status == TaskStatus(status))
        if start_date:
            filters.append(TaskExecution.start_time >= start_date)
        if end_date:
            filters.append(TaskExecution.start_time <= end_date)

        stmt = select(TaskExecution)
        count_stmt = select(func.count()).select_from(TaskExecution)
        if filters:
            stmt = stmt.where(and_(*filters))
            count_stmt = count_stmt.where(and_(*filters))

        total = int((await self.db.execute(count_stmt)).scalar() or 0)
        stmt = (
            stmt.order_by(TaskExecution.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        result = await self.db.execute(stmt)
        return list(result.scalars().all()), total

    async def get_stats(self) -> dict[str, float | int]:
        """Aggregate execution counts, success rate and average duration."""
        total = int((await self.db.execute(select(func.count(TaskExecution.id)))).scalar() or 0)
        success = int(
            (
                await self.db.execute(
                    select(func.count(TaskExecution.id)).where(
                        TaskExecution.status == TaskStatus.COMPLETED
                    )
                )
            ).scalar()
            or 0
        )
        failed = int(
            (
                await self.db.execute(
                    select(func.count(TaskExecution.id)).where(
                        TaskExecution.status == TaskStatus.FAILED
                    )
                )
            ).scalar()
            or 0
        )
        running = int(
            (
                await self.db.execute(
                    select(func.count(TaskExecution.id)).where(
                        TaskExecution.status == TaskStatus.RUNNING
                    )
                )
            ).scalar()
            or 0
        )
        avg_duration = float(
            (
                await self.db.execute(
                    select(func.avg(TaskExecution.duration)).where(
                        TaskExecution.status == TaskStatus.COMPLETED
                    )
                )
            ).scalar()
            or 0
        )
        return {
            "total_count": total,
            "success_count": success,
            "failed_count": failed,
            "running_count": running,
            "success_rate": (success / total * 100) if total else 0,
            "avg_duration": avg_duration,
        }

    async def get_recent(self, limit: int = 20) -> list[TaskExecution]:
        """Return the most recent executions."""
        result = await self.db.execute(
            select(TaskExecution).order_by(TaskExecution.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def get_running(self) -> list[TaskExecution]:
        """Return every execution still in the running state."""
        result = await self.db.execute(
            select(TaskExecution)
            .where(TaskExecution.status == TaskStatus.RUNNING)
            .order_by(TaskExecution.created_at.desc())
        )
        return list(result.scalars().all())

    async def retry_execution(
        self,
        execution_id: str,
        operator_id: int | None = None,
    ) -> TaskExecution:
        """Re-run the parent task of a past execution and return the new execution."""
        execution = await self.get_execution(execution_id)
        if execution is None:
            raise ValueError("Execution not found")
        if execution.task_id is None:
            raise ValueError("Only task-linked executions can be retried")

        from opendata.afi_port.runtime.scheduler_service import get_akshare_scheduler_service

        service = get_akshare_scheduler_service()
        new_execution: TaskExecution = await service.run_task_now(
            execution.task_id, operator_id=operator_id
        )
        return new_execution
