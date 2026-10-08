"""Task scheduler service.

Manages scheduled task execution using APScheduler with async support.
Integrates with SchedulerService and ExecutionService.
"""

import asyncio
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.core.database import async_session_maker
from opendata.models.task import ScheduledTask, ScheduleType, TaskStatus, TriggeredBy
from opendata.models.user import User, UserRole
from opendata.services.execution_service import ExecutionService
from opendata.services.scheduler_service import init_scheduler_service
from opendata.services.script_service import ScriptService
from opendata.utils.db_result import get_rowcount

if TYPE_CHECKING:
    from opendata.services.scheduler_service import SchedulerService


class TaskScheduler:
    """Service for managing scheduled task execution.

    Provides async task scheduling, execution, and retry logic.
    """

    def __init__(self) -> None:
        """Start stopped, with no APScheduler service and no running tasks."""
        self._running = False
        self.scheduler_service: SchedulerService | None = None
        self._running_tasks: dict[int, asyncio.Task] = {}  # task_id -> asyncio.Task

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running and self.scheduler_service is not None

    async def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            logger.warning("Scheduler already running")
            return

        logger.info("Starting task scheduler...")
        self.scheduler_service = init_scheduler_service()
        await self.scheduler_service.start()

        # Add jobs for all active tasks
        await self._load_active_tasks()

        # Register housekeeping: cleanup old execution records daily at 3am
        await self.scheduler_service.add_job(
            job_id="__housekeeping_cleanup_executions",
            func=self._cleanup_old_executions,
            trigger_type="cron",
            trigger_args={"cron_expression": "0 3 * * *"},
            job_name="Cleanup old execution records",
        )

        self._running = True
        logger.info("Task scheduler started successfully")

    async def shutdown(self) -> None:
        """Shutdown the scheduler."""
        if not self._running:
            return

        logger.info("Shutting down task scheduler...")
        if self.scheduler_service:
            await self.scheduler_service.shutdown()
            self.scheduler_service = None

        self._running = False
        logger.info("Task scheduler shut down")

    async def _load_active_tasks(self) -> None:
        """Load all active tasks from database and schedule them."""
        from sqlalchemy import select

        async with async_session_maker() as db:
            result = await db.execute(
                select(ScheduledTask).where(ScheduledTask.is_active.is_(True))
            )
            tasks = result.scalars().all()

            for task in tasks:
                try:
                    await self._schedule_task(task, db)
                    logger.info(f"Scheduled task: {task.name} (ID: {task.id})")
                except Exception as e:  # noqa: PERF203  # 一条任务配不上不能拖累其余
                    logger.error(f"Failed to schedule task {task.id}: {e}")

    async def _schedule_task(self, task: ScheduledTask, db: AsyncSession) -> None:
        """Add a task to the scheduler."""
        if self.scheduler_service is None:
            return

        # Determine trigger type and args
        trigger_type, trigger_args = self._get_trigger_config(task)

        # Add job to scheduler
        await self.scheduler_service.add_job(
            job_id=f"task_{task.id}",
            func=self._execute_task_wrapper,
            trigger_type=trigger_type,
            trigger_args=trigger_args,
            job_name=task.name,
            kwargs={"task_id": task.id},
        )

    def _get_trigger_config(self, task: ScheduledTask) -> tuple[str, dict]:
        """Get trigger configuration from task schedule."""
        schedule_expr = task.schedule_expression

        if task.schedule_type == ScheduleType.CRON:
            return "cron", {"cron_expression": schedule_expr}
        if task.schedule_type == ScheduleType.DAILY:
            # Expected format: HH:MM (e.g. 09:30)
            if ":" in schedule_expr:
                hour, minute = schedule_expr.split(":")
                cron_expr = f"{minute} {hour} * * *"
                return "cron", {"cron_expression": cron_expr}
            return "cron", {"cron_expression": schedule_expr}
        if task.schedule_type == ScheduleType.WEEKLY or task.schedule_type == ScheduleType.MONTHLY:
            return "cron", {"cron_expression": schedule_expr}
        if task.schedule_type == ScheduleType.INTERVAL:
            # Parse interval (e.g. 5m, 1h, 30s)
            return "interval", self._parse_interval(schedule_expr)
        return "once", {}

    def _parse_interval(self, interval_str: str) -> dict:
        """Parse interval string to trigger args."""
        interval_str = interval_str.strip().lower()
        args = {}

        if interval_str.endswith("s"):
            args["seconds"] = int(interval_str[:-1])
        elif interval_str.endswith("m"):
            args["minutes"] = int(interval_str[:-1])
        elif interval_str.endswith("h"):
            args["hours"] = int(interval_str[:-1])
        elif interval_str.endswith("d"):
            args["days"] = int(interval_str[:-1])
        else:
            # Default to minutes
            args["minutes"] = int(interval_str)

        return args

    async def _execute_task_wrapper(self, task_id: int) -> None:
        """Execute task with error handling and retry logic."""
        current_task = asyncio.current_task()
        if current_task is not None:
            self._running_tasks[task_id] = current_task
        try:
            async with async_session_maker() as db:
                from sqlalchemy import select

                result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
                task = result.scalar_one_or_none()

                if task is None:
                    logger.error(f"Task {task_id} not found")
                    return

                if not task.is_active:
                    logger.info(f"Task {task_id} is not active, skipping")
                    return

                await self._execute_with_retry(task, db)
        finally:
            if self._running_tasks.get(task_id) is current_task:
                self._running_tasks.pop(task_id, None)

    async def _execute_with_retry(self, task: ScheduledTask, db: AsyncSession) -> None:
        """Execute task with retry mechanism."""
        execution_service = ExecutionService(db)
        script_service = ScriptService(db) if task.task_kind == "script" else None

        max_attempts = task.max_retries if task.retry_on_failure else 1
        timeout = task.timeout if task.timeout > 0 else None

        for attempt in range(max_attempts):
            # Create execution record
            execution = await execution_service.create_execution(
                task_id=task.id,
                script_id=task.script_id,
                params=task.parameters,
                triggered_by=TriggeredBy.SCHEDULER,
            )

            if task.task_kind == "pipeline" and not await self._is_active_pipeline_admin(
                db, task.user_id
            ):
                rejected_at = datetime.now(timezone.utc)
                await execution_service.update_execution(
                    execution_id=execution.execution_id,
                    status=TaskStatus.FAILED,
                    end_time=rejected_at,
                    error_message="Pipeline task owner must be an active administrator",
                )
                task.last_execution_at = rejected_at
                await db.commit()
                await self._broadcast_status(
                    execution.execution_id,
                    task.id,
                    TaskStatus.FAILED,
                    error_message="Pipeline task owner is not authorized",
                )
                return

            try:
                # Update status to running
                await execution_service.update_execution(
                    execution_id=execution.execution_id,
                    status=TaskStatus.RUNNING,
                    start_time=datetime.now(timezone.utc),
                )

                # Broadcast RUNNING status via WebSocket
                await self._broadcast_status(
                    execution.execution_id,
                    task.id,
                    TaskStatus.RUNNING,
                )

                rows_before = None
                provider = None
                rows_after = None
                if task.task_kind == "pipeline":
                    from opendata.api.schemas import validate_pipeline_task_parameters
                    from opendata.pipeline.jobs import run_incremental_job

                    pipeline_parameters = validate_pipeline_task_parameters(task.parameters)
                    pipeline_run = run_incremental_job(**pipeline_parameters)
                    outcome = (
                        await asyncio.wait_for(pipeline_run, timeout=timeout)
                        if timeout is not None
                        else await pipeline_run
                    )
                    result = {
                        "success": outcome.failures == 0,
                        "data": outcome.as_dict(),
                        "error": (
                            f"Pipeline completed with {outcome.failures} failed shard(s)"
                            if outcome.failures
                            else None
                        ),
                    }
                elif task.task_kind == "script":
                    if script_service is None or task.script_id is None:
                        raise RuntimeError("Script task is missing its script executor")
                    script = await script_service.get_script(task.script_id)
                    if script and script.target_table:
                        from opendata.data_fetch.providers.akshare_provider import AkshareProvider

                        provider = AkshareProvider()
                        rows_before = provider.get_table_row_count(script.target_table)

                    result = await script_service.execute_script(
                        script_id=task.script_id,
                        execution_id=execution.execution_id,
                        params=task.parameters,
                        timeout=timeout,
                    )

                    if script and script.target_table:
                        if provider is None:
                            from opendata.data_fetch.providers.akshare_provider import (
                                AkshareProvider,
                            )

                            provider = AkshareProvider()
                        rows_after = provider.get_table_row_count(script.target_table)
                else:
                    raise RuntimeError(f"Unknown task executor: {task.task_kind}")

                # Update execution result
                if result.get("success"):
                    end_time = datetime.now(timezone.utc)
                    await execution_service.update_execution(
                        execution_id=execution.execution_id,
                        status=TaskStatus.COMPLETED,
                        end_time=end_time,
                        result=result,
                        rows_before=rows_before,
                        rows_after=rows_after,
                    )

                    # Update task last execution
                    task.last_execution_at = end_time
                    await db.commit()

                    duration = (end_time - (execution.start_time or end_time)).total_seconds()
                    logger.info(
                        f"Task {task.name} (ID: {task.id}) completed successfully. "
                        f"Rows: {rows_before} -> {rows_after}"
                    )

                    # Broadcast COMPLETED status via WebSocket
                    await self._broadcast_status(
                        execution.execution_id,
                        task.id,
                        TaskStatus.COMPLETED,
                        rows_before=rows_before,
                        rows_after=rows_after,
                        duration=duration,
                    )
                    return  # Success, exit retry loop
                raise Exception(result.get("error", "Unknown error"))

            except Exception as e:
                logger.error(f"Task {task.name} (ID: {task.id}) attempt {attempt + 1} failed: {e}")

                failure_status = (
                    TaskStatus.TIMEOUT if isinstance(e, TimeoutError) else TaskStatus.FAILED
                )
                failure_message = str(e) or (
                    "Task timed out" if failure_status == TaskStatus.TIMEOUT else "Task failed"
                )

                await execution_service.update_execution(
                    execution_id=execution.execution_id,
                    status=failure_status,
                    end_time=datetime.now(timezone.utc),
                    error_message=failure_message,
                )

                await db.commit()

                # Broadcast FAILED status via WebSocket
                await self._broadcast_status(
                    execution.execution_id,
                    task.id,
                    failure_status,
                    error_message=failure_message,
                )

                # Send failure notification (WebSocket + optional email)
                try:
                    from opendata.services.notification_service import NotificationService

                    await NotificationService.notify_task_failed(
                        task_id=task.id,
                        task_name=task.name,
                        execution_id=execution.execution_id,
                        error_message=failure_message,
                        retry_count=attempt + 1,
                        max_retries=max_attempts,
                    )
                except Exception as e:
                    logger.debug("Task failure notification skipped: {}", e)

                # If not last attempt, wait before retry
                if attempt < max_attempts - 1:
                    # Exponential backoff: 60s, 120s, 240s...
                    delay = 60 * (2**attempt)
                    logger.info(f"Retrying task {task.id} in {delay} seconds...")
                    await asyncio.sleep(delay)

    async def add_task(self, task_id: int, db: AsyncSession) -> None:
        """Add a task to the scheduler."""
        from sqlalchemy import select

        result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        task = result.scalar_one_or_none()

        if task is None:
            logger.error(f"Task {task_id} not found")
            return

        await self._schedule_task(task, db)
        logger.info(f"Added task to scheduler: {task.name} (ID: {task.id})")

    async def update_task(self, task_id: int, db: AsyncSession) -> None:
        """Update a task in the scheduler."""
        # Remove and re-add
        await self.remove_task(task_id)
        await self.add_task(task_id, db)

    async def remove_task(self, task_id: int) -> None:
        """Remove a task from the scheduler."""
        if self.scheduler_service is None:
            return

        try:
            await self.scheduler_service.remove_job(f"task_{task_id}")
            logger.info(f"Removed task from scheduler: {task_id}")
        except Exception as e:
            logger.warning(f"Failed to remove task {task_id}: {e}")

    async def trigger_task(self, task_id: int, user_id: int | None = None) -> str:
        """Trigger immediate task execution.

        The background task creates its own database session so we don't
        use a session that gets closed when this method returns.
        """
        from sqlalchemy import select

        # Read task info in a short-lived session
        async with async_session_maker() as db:
            result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
            task = result.scalar_one_or_none()

            if task is None:
                raise ValueError(f"Task {task_id} not found")

            if task.task_kind == "pipeline":
                if user_id is None or not await self._is_active_pipeline_admin(db, user_id):
                    raise PermissionError("Administrator permission is required for pipeline tasks")
                if not await self._is_active_pipeline_admin(db, task.user_id):
                    raise PermissionError("Pipeline task owner is not an active administrator")

            # Snapshot the value we need so we don't touch this session later
            task_id_val = task.id

        # Background coroutine with its own session
        async def _bg_execute() -> None:
            async with async_session_maker() as bg_db:
                bg_result = await bg_db.execute(
                    select(ScheduledTask).where(ScheduledTask.id == task_id_val)
                )
                bg_task = bg_result.scalar_one_or_none()
                if bg_task is None:
                    logger.error(f"Task {task_id_val} disappeared before execution")
                    return
                await self._execute_with_retry(bg_task, bg_db)

        # Generate an execution_id to return immediately
        execution_id = f"exec_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{task_id_val}"

        bg = asyncio.create_task(_bg_execute())
        self._running_tasks[task_id_val] = bg

        def _on_done(t: asyncio.Task, tid: int = task_id_val) -> None:
            self._running_tasks.pop(tid, None)
            if not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    logger.error(f"Unhandled exception in trigger_task bg for task {tid}: {exc}")

        bg.add_done_callback(_on_done)

        return execution_id

    @staticmethod
    async def _is_active_pipeline_admin(db: AsyncSession, user_id: int) -> bool:
        """Read current role state before any scheduled pipeline provider work."""
        result = await db.execute(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )
        user = result.scalar_one_or_none()
        return user is not None and user.is_active and user.role == UserRole.ADMIN

    async def cancel_task(self, task_id: int) -> bool:
        """Cancel a running task execution.

        Returns True if a running task was found and cancelled.
        """
        bg_task = self._running_tasks.pop(task_id, None)
        if bg_task is None or bg_task.done():
            return False

        bg_task.cancel()
        logger.info(f"Cancelled running task {task_id}")

        # Mark the latest running execution as cancelled
        try:
            async with async_session_maker() as db:
                from sqlalchemy import select

                from opendata.models.task import TaskExecution

                result = await db.execute(
                    select(TaskExecution)
                    .where(TaskExecution.task_id == task_id)
                    .where(TaskExecution.status == TaskStatus.RUNNING)
                    .order_by(TaskExecution.created_at.desc())
                    .limit(1)
                )
                execution = result.scalar_one_or_none()
                if execution:
                    execution.status = TaskStatus.CANCELLED
                    execution.end_time = datetime.now(timezone.utc)
                    execution.error_message = "Cancelled by user"
                    await db.commit()
        except Exception as e:
            logger.error(f"Error updating cancelled execution for task {task_id}: {e}")

        return True

    def get_running_task_ids(self) -> list[int]:
        """Get list of currently running task IDs."""
        return [tid for tid, t in self._running_tasks.items() if not t.done()]

    @staticmethod
    async def _broadcast_status(
        execution_id: str,
        task_id: int | None,
        status: TaskStatus,
        *,
        rows_before: int | None = None,
        rows_after: int | None = None,
        error_message: str | None = None,
        duration: float | None = None,
    ) -> None:
        """Broadcast execution status change via WebSocket (best-effort)."""
        try:
            from opendata.api.websocket import broadcast_execution_update

            await broadcast_execution_update(
                execution_id=execution_id,
                task_id=task_id,
                status=status.value,
                rows_before=rows_before,
                rows_after=rows_after,
                error_message=error_message,
                duration=duration,
            )
        except Exception as e:
            logger.debug("WebSocket broadcast skipped: {}", e)

    async def _cleanup_old_executions(self, retention_days: int = 30) -> None:
        """Delete execution records older than retention_days.

        Keeps the most recent records and removes old completed/failed ones.
        Also marks stuck PENDING/RUNNING executions (older than 4 hours) as TIMEOUT.
        """
        from datetime import timedelta

        from sqlalchemy import and_, delete, update

        from opendata.models.task import TaskExecution

        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        stuck_cutoff = datetime.now(timezone.utc) - timedelta(hours=4)

        try:
            async with async_session_maker() as db:
                # 1. Mark stuck PENDING/RUNNING executions as TIMEOUT
                stuck_result = await db.execute(
                    update(TaskExecution)
                    .where(
                        and_(
                            TaskExecution.created_at < stuck_cutoff,
                            TaskExecution.status.in_(
                                [
                                    TaskStatus.PENDING,
                                    TaskStatus.RUNNING,
                                ]
                            ),
                        )
                    )
                    .values(
                        status=TaskStatus.TIMEOUT,
                        end_time=datetime.now(timezone.utc),
                        error_message="Marked as timeout by housekeeping (stuck > 4 hours)",
                    )
                )
                stuck_count = get_rowcount(stuck_result)
                if stuck_count:
                    logger.warning(
                        f"Housekeeping: marked {stuck_count} stuck executions as TIMEOUT"
                    )

                # 2. Delete old terminal-state execution records
                result = await db.execute(
                    delete(TaskExecution).where(
                        and_(
                            TaskExecution.created_at < cutoff,
                            TaskExecution.status.in_(
                                [
                                    TaskStatus.COMPLETED,
                                    TaskStatus.FAILED,
                                    TaskStatus.CANCELLED,
                                    TaskStatus.TIMEOUT,
                                ]
                            ),
                        )
                    )
                )
                await db.commit()
                deleted = get_rowcount(result)
                if deleted:
                    logger.info(
                        f"Housekeeping: deleted {deleted} execution records "
                        f"older than {retention_days} days"
                    )
        except Exception as e:
            logger.error(f"Housekeeping cleanup failed: {e}")


# Global scheduler instance
task_scheduler = TaskScheduler()
