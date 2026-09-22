"""
Retry service for handling failed task executions with exponential backoff.

Implements automatic retry mechanism for failed task executions.
"""

import asyncio
from datetime import UTC, datetime

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.models.task import ScheduledTask, TaskExecution, TaskStatus


class RetryService:
    """
    Service for managing retry logic for failed task executions.

    Implements exponential backoff: delay = 60s * 2^retry_count
    """

    # Base delay for retry (in seconds)
    BASE_RETRY_DELAY = 60

    # Maximum delay between retries (in seconds)
    MAX_RETRY_DELAY = 3600  # 1 hour

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._retry_queue: dict[str, asyncio.Task] = {}

    async def should_retry(self, execution: TaskExecution, task: ScheduledTask) -> bool:
        """
        Determine if an execution should be retried.

        Args:
            execution: The failed execution
            task: The scheduled task

        Returns:
            True if should retry, False otherwise
        """
        # Check if task has retry enabled
        if not task.retry_on_failure:
            return False

        # Check if retry count is within limit
        if execution.retry_count >= task.max_retries:
            return False

        # Check if execution status is failed
        return execution.status == TaskStatus.FAILED

    def calculate_retry_delay(self, retry_count: int) -> int:
        """
        Calculate retry delay using exponential backoff.

        Args:
            retry_count: Current retry attempt number

        Returns:
            Delay in seconds
        """
        delay = self.BASE_RETRY_DELAY * (2**retry_count)
        return int(min(delay, self.MAX_RETRY_DELAY))

    async def schedule_retry(self, execution: TaskExecution, task: ScheduledTask) -> bool:
        """
        Schedule a retry for a failed execution.

        Args:
            execution: The failed execution
            task: The scheduled task

        Returns:
            True if retry scheduled successfully, False otherwise
        """
        if not await self.should_retry(execution, task):
            logger.info(
                f"Execution {execution.execution_id} should not be retried. "
                f"Retry count: {execution.retry_count}, Max retries: {task.max_retries}"
            )
            return False

        retry_count = execution.retry_count + 1
        delay = self.calculate_retry_delay(execution.retry_count)

        logger.info(
            f"Scheduling retry for execution {execution.execution_id}. "
            f"Retry attempt {retry_count}/{task.max_retries}, delay: {delay}s"
        )

        # Schedule retry with delay
        async def _do_retry() -> None:
            await asyncio.sleep(delay)
            await self.execute_retry(execution, task, retry_count)

        # Store the retry task
        task_handle = asyncio.create_task(_do_retry())
        self._retry_queue[execution.execution_id] = task_handle

        return True

    async def execute_retry(
        self, original_execution: TaskExecution, task: ScheduledTask, retry_count: int
    ) -> bool:
        """
        Execute a retry attempt.

        Args:
            original_execution: The original failed execution
            task: The scheduled task
            retry_count: The retry attempt number

        Returns:
            True if retry succeeded, False otherwise
        """
        logger.info(
            f"Executing retry {retry_count} for task {task.id}, "
            f"original execution: {original_execution.execution_id}"
        )

        try:
            # Create new execution record for retry
            from opendata.services.execution_service import ExecutionService

            execution_service = ExecutionService(self.db)

            new_execution = await execution_service.create_execution(
                task_id=task.id,
                script_id=task.script_id,
                params=task.parameters,
                triggered_by=original_execution.triggered_by,
                operator_id=original_execution.operator_id,
            )

            # Set retry count
            new_execution.retry_count = retry_count
            await self.db.commit()

            # Execute the task
            from opendata.services.data_service import DataService

            data_service = DataService(self.db)

            start_time = datetime.now(UTC)
            await execution_service.update_execution(
                new_execution.execution_id,
                status=TaskStatus.RUNNING,
                start_time=start_time,
            )

            try:
                result = await data_service.execute_script(
                    task.script_id,
                    task.parameters or {},
                    task.timeout,
                )

                end_time = datetime.now(UTC)

                # Update execution with success
                await execution_service.update_execution(
                    new_execution.execution_id,
                    status=TaskStatus.COMPLETED,
                    end_time=end_time,
                    result=result,
                    rows_after=result.get("rows_processed"),
                )

                logger.info(
                    f"Retry {retry_count} succeeded for execution {new_execution.execution_id}"
                )

                # Update task last execution time
                task.last_execution_at = end_time
                await self.db.commit()

                return True

            except Exception as e:
                end_time = datetime.now(UTC)
                error_message = str(e)

                # Update execution with failure
                await execution_service.update_execution(
                    new_execution.execution_id,
                    status=TaskStatus.FAILED,
                    end_time=end_time,
                    error_message=error_message,
                )

                logger.warning(
                    f"Retry {retry_count} failed for execution {new_execution.execution_id}: {error_message}"
                )

                # Schedule another retry if needed
                if await self.should_retry(new_execution, task):
                    await self.schedule_retry(new_execution, task)

                return False

        except Exception as e:
            logger.error(f"Failed to execute retry: {e}")
            return False

    async def cancel_pending_retries(self, execution_id: str) -> bool:
        """
        Cancel any pending retries for an execution.

        Args:
            execution_id: The execution ID

        Returns:
            True if cancelled, False if not found
        """
        retry_task = self._retry_queue.get(execution_id)
        if retry_task and not retry_task.done():
            retry_task.cancel()
            del self._retry_queue[execution_id]
            return True
        return False

    async def get_retry_status(self, execution_id: str) -> dict | None:
        """
        Get retry status for an execution.

        Args:
            execution_id: The execution ID

        Returns:
            Dict with retry status or None
        """
        retry_task = self._retry_queue.get(execution_id)
        if retry_task:
            return {
                "execution_id": execution_id,
                "status": "pending" if not retry_task.done() else "completed",
                "cancelled": retry_task.cancelled() if retry_task.done() else False,
            }
        return None

    async def get_all_pending_retries(self) -> list[str]:
        """
        Get all execution IDs with pending retries.

        Returns:
            List of execution IDs
        """
        return [eid for eid, task in self._retry_queue.items() if not task.done()]

    async def cleanup_completed_retries(self) -> int:
        """
        Remove completed retry tasks from the queue.

        Returns:
            Number of tasks removed
        """
        to_remove = [eid for eid, task in self._retry_queue.items() if task.done()]

        for eid in to_remove:
            del self._retry_queue[eid]

        return len(to_remove)
