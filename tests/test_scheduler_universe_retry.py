"""Scheduler retry policy for deterministic pipeline universe failures."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

import pytest

from opendata.models.task import TaskStatus
from opendata.pipeline.universe_errors import (
    EmptySymbolUniverseError,
    UniverseLimitExceededError,
    UniverseQueryError,
)
from opendata.services.scheduler import TaskScheduler


def _scheduled_task(task_kind: str = "pipeline") -> SimpleNamespace:
    """Build the scheduler fields without a mapped task or database."""
    return SimpleNamespace(
        id=41,
        name="daily stock pipeline",
        task_kind=task_kind,
        script_id="script-1" if task_kind == "script" else None,
        parameters={"domain": "stock_daily", "symbols": ["600519.SH"]},
        user_id=12,
        timeout=0,
        retry_on_failure=True,
        max_retries=3,
        last_execution_at=None,
    )


def _execution_service() -> SimpleNamespace:
    """Build asynchronous execution-service substitutes for three attempts."""
    executions = [
        SimpleNamespace(execution_id=f"execution-{index}", start_time=None) for index in range(1, 4)
    ]
    return SimpleNamespace(
        create_execution=AsyncMock(side_effect=executions),
        update_execution=AsyncMock(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "notification_fails", "timeout"),
    [
        (EmptySymbolUniverseError("no eligible symbols"), False, 0),
        (UniverseLimitExceededError("symbol universe exceeds configured limit"), False, 0),
        (EmptySymbolUniverseError("no eligible symbols"), True, 0),
        (UniverseLimitExceededError("symbol universe exceeds configured limit"), False, 5),
    ],
)
async def test_pipeline_universe_domain_errors_fail_once_without_backoff(
    error: Exception, notification_fails: bool, timeout: int
) -> None:
    """Record deterministic universe errors once even if their notification fails."""
    task = _scheduled_task()
    task.timeout = timeout
    db = SimpleNamespace(commit=AsyncMock())
    execution_service = _execution_service()
    runner = AsyncMock(side_effect=error)
    notification = AsyncMock()
    if notification_fails:
        notification.side_effect = RuntimeError("notification unavailable")
    sleep = AsyncMock()
    wait_for = AsyncMock(wraps=asyncio.wait_for)
    scheduler = TaskScheduler()

    with (
        patch(
            "opendata.services.scheduler.ExecutionService",
            return_value=execution_service,
        ),
        patch("opendata.pipeline.jobs.run_incremental_job", runner),
        patch(
            "opendata.services.notification_service.NotificationService.notify_task_failed",
            notification,
        ),
        patch("opendata.services.scheduler.asyncio.sleep", sleep),
        patch("opendata.services.scheduler.asyncio.wait_for", wait_for),
        patch.object(
            scheduler,
            "_is_active_pipeline_admin",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(scheduler, "_broadcast_status", new_callable=AsyncMock) as broadcast,
    ):
        await scheduler._execute_with_retry(task, db)

    execution_service.create_execution.assert_awaited_once()
    assert execution_service.update_execution.await_count == 2
    assert [
        update.kwargs["status"] for update in execution_service.update_execution.await_args_list
    ] == [TaskStatus.RUNNING, TaskStatus.FAILED]
    assert execution_service.update_execution.await_args_list[-1].kwargs["error_message"] == str(
        error
    )
    runner.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert [broadcast_call.args[2] for broadcast_call in broadcast.await_args_list] == [
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
    ]
    notification.assert_awaited_once()
    assert notification.await_args.kwargs["error_message"] == str(error)
    assert notification.await_args.kwargs["retry_count"] == 1
    assert notification.await_args.kwargs["max_retries"] == 1
    sleep.assert_not_awaited()
    if timeout:
        wait_for.assert_awaited_once()
        assert wait_for.await_args.kwargs["timeout"] == timeout
    else:
        wait_for.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("ordinary invalid input"),
        RuntimeError("temporary pipeline error"),
        UniverseQueryError("temporary universe lookup failure"),
    ],
)
async def test_pipeline_transient_or_general_errors_keep_configured_retries(
    error: Exception,
) -> None:
    """Ordinary and potentially transient pipeline errors keep their retry policy."""
    task = _scheduled_task()
    db = SimpleNamespace(commit=AsyncMock())
    execution_service = _execution_service()
    runner = AsyncMock(side_effect=error)
    notification = AsyncMock()
    scheduler = TaskScheduler()

    with (
        patch(
            "opendata.services.scheduler.ExecutionService",
            return_value=execution_service,
        ),
        patch("opendata.pipeline.jobs.run_incremental_job", runner),
        patch(
            "opendata.services.notification_service.NotificationService.notify_task_failed",
            notification,
        ),
        patch("opendata.services.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch.object(
            scheduler,
            "_is_active_pipeline_admin",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(scheduler, "_broadcast_status", new_callable=AsyncMock),
    ):
        await scheduler._execute_with_retry(task, db)

    assert execution_service.create_execution.await_count == 3
    assert execution_service.update_execution.await_count == 6
    assert runner.await_count == 3
    assert db.commit.await_count == 3
    assert notification.await_count == 3
    assert [entry.kwargs["retry_count"] for entry in notification.await_args_list] == [1, 2, 3]
    assert sleep.await_args_list == [call(60), call(120)]


@pytest.mark.asyncio
async def test_script_task_keeps_retrying_even_for_pipeline_domain_exception() -> None:
    """Pipeline-only domain exceptions do not change legacy script task retries."""
    task = _scheduled_task(task_kind="script")
    db = SimpleNamespace(commit=AsyncMock())
    execution_service = _execution_service()
    script_service = SimpleNamespace(
        get_script=AsyncMock(return_value=None),
        execute_script=AsyncMock(side_effect=EmptySymbolUniverseError("script failure")),
    )
    notification = AsyncMock()
    scheduler = TaskScheduler()

    with (
        patch(
            "opendata.services.scheduler.ExecutionService",
            return_value=execution_service,
        ),
        patch("opendata.services.scheduler.ScriptService", return_value=script_service),
        patch(
            "opendata.services.notification_service.NotificationService.notify_task_failed",
            notification,
        ),
        patch("opendata.services.scheduler.asyncio.sleep", new_callable=AsyncMock) as sleep,
        patch.object(scheduler, "_broadcast_status", new_callable=AsyncMock),
    ):
        await scheduler._execute_with_retry(task, db)

    assert execution_service.create_execution.await_count == 3
    assert script_service.execute_script.await_count == 3
    assert notification.await_count == 3
    assert sleep.await_args_list == [call(60), call(120)]
