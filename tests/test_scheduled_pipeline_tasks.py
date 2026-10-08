"""Pipeline task validation, persistence, scheduling, and execution behavior."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from opendata.api.schemas import TaskCreateRequest, TaskUpdateRequest
from opendata.core.database import Base
from opendata.models.data_script import DataScript, ScriptFrequency
from opendata.models.task import (
    ScheduledTask,
    ScheduleType,
    TaskExecution,
    TaskKind,
    TaskStatus,
)
from opendata.models.user import User, UserRole
from opendata.services.scheduler import TaskScheduler


def _pipeline_payload(**overrides: object) -> dict[str, object]:
    """Build one API request with only the pipeline task configuration."""
    payload: dict[str, object] = {
        "name": "daily-stock-pipeline",
        "task_kind": "pipeline",
        "script_id": None,
        "schedule_type": "cron",
        "schedule_expression": "0 2 * * *",
        "parameters": {"domain": "stock_daily", "source": "ths", "limit": 20},
    }
    payload.update(overrides)
    return payload


async def _pipeline_task(
    db: AsyncSession,
    *,
    owner_role: UserRole = UserRole.ADMIN,
    timeout: int = 0,
    parameters: dict[str, object] | None = None,
) -> ScheduledTask:
    """Persist one pipeline task and owner in the provided isolated session."""
    suffix = uuid4().hex
    user = User(
        username=f"pipeline_{owner_role.value}_{suffix}",
        email=f"pipeline_{owner_role.value}_{suffix}@example.test",
        hashed_password="unused",
        role=owner_role,
        is_active=True,
    )
    db.add(user)
    await db.flush()
    task = ScheduledTask(
        name="temporary pipeline task",
        user_id=user.id,
        task_kind=TaskKind.PIPELINE.value,
        script_id=None,
        schedule_type=ScheduleType.CRON,
        schedule_expression="0 2 * * *",
        parameters=parameters or {"domain": "stock_daily", "symbols": ["600519.SH"]},
        is_active=True,
        retry_on_failure=False,
        max_retries=1,
        timeout=timeout,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


def test_task_schemas_keep_script_default_and_reject_mixed_pipeline_inputs() -> None:
    """Script tasks stay backwards compatible; pipeline input is allowlisted."""
    script_task = TaskCreateRequest(
        name="legacy-script",
        script_id="existing_script",
        schedule_type="daily",
        schedule_expression="02:00",
    )
    assert script_task.task_kind == TaskKind.SCRIPT.value
    assert script_task.script_id == "existing_script"

    with pytest.raises(ValidationError, match="pipeline tasks cannot include script_id"):
        TaskCreateRequest(**_pipeline_payload(script_id="existing_script"))

    with pytest.raises(ValidationError, match="credentials"):
        TaskCreateRequest(
            **_pipeline_payload(parameters={"domain": "stock_daily", "credentials": "x"})
        )

    with pytest.raises(ValidationError, match="unsupported pipeline domain"):
        TaskCreateRequest(**_pipeline_payload(parameters={"domain": "all_domains", "limit": 20}))

    with pytest.raises(ValidationError):
        TaskUpdateRequest(task_kind="pipeline", script_id="script-x")


@pytest.mark.asyncio
async def test_pipeline_task_create_requires_admin_and_list_handles_null_script(
    test_client, test_user_token: str, test_admin_token: str, test_db: AsyncSession
) -> None:
    """Only admins can create pipeline work; listing handles a missing script join key."""
    user_headers = {"Authorization": f"Bearer {test_user_token}"}
    admin_headers = {"Authorization": f"Bearer {test_admin_token}"}
    response = await test_client.post("/api/tasks/", headers=user_headers, json=_pipeline_payload())
    assert response.status_code == 403

    with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
        response = await test_client.post(
            "/api/tasks/", headers=admin_headers, json=_pipeline_payload()
        )
    assert response.status_code == 201
    created = response.json()["data"]
    assert created["task_kind"] == TaskKind.PIPELINE.value
    assert created["script_id"] is None
    assert created["script_name"] is None

    listed = await test_client.get("/api/tasks/", headers=admin_headers)
    assert listed.status_code == 200
    row = next(item for item in listed.json()["data"]["items"] if item["id"] == created["id"])
    assert row["task_kind"] == TaskKind.PIPELINE.value
    assert row["script_id"] is None
    assert row["script_name"] is None


@pytest.mark.asyncio
async def test_pipeline_owner_cannot_edit_parameters_activate_or_trigger_after_demotion(
    test_client, test_admin_token: str, test_db: AsyncSession
) -> None:
    """Owner read access remains; pipeline write operations require current admin role."""
    headers = {"Authorization": f"Bearer {test_admin_token}"}
    with patch("opendata.services.scheduler.task_scheduler.add_task", new_callable=AsyncMock):
        created = await test_client.post("/api/tasks/", headers=headers, json=_pipeline_payload())
    assert created.status_code == 201
    task_id = created.json()["data"]["id"]
    owner = await test_db.get(User, created.json()["data"]["user_id"])
    assert owner is not None
    owner.role = UserRole.USER
    await test_db.commit()

    detail = await test_client.get(f"/api/tasks/{task_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["script_id"] is None

    changed_description = await test_client.put(
        f"/api/tasks/{task_id}", headers=headers, json={"description": "owner write"}
    )
    assert changed_description.status_code == 403

    changed_parameters = await test_client.put(
        f"/api/tasks/{task_id}",
        headers=headers,
        json={"parameters": {"domain": "stock_daily", "limit": 10}},
    )
    assert changed_parameters.status_code == 403

    activate = await test_client.put(
        f"/api/tasks/{task_id}", headers=headers, json={"is_active": True}
    )
    assert activate.status_code == 403

    with patch(
        "opendata.services.scheduler.task_scheduler.trigger_task", new_callable=AsyncMock
    ) as trigger:
        response = await test_client.post(f"/api/tasks/{task_id}/trigger", headers=headers)
    assert response.status_code == 403
    trigger.assert_not_awaited()

    with patch(
        "opendata.services.scheduler.task_scheduler.remove_task", new_callable=AsyncMock
    ) as remove_task:
        deleted = await test_client.delete(f"/api/tasks/{task_id}", headers=headers)
    assert deleted.status_code == 403
    remove_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_restart_reloads_pipeline_task_from_temporary_database(tmp_path) -> None:
    """A fresh scheduler instance restores active pipeline tasks from the database."""
    database_path = tmp_path / "scheduled-tasks.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await _pipeline_task(db)
    await engine.dispose()

    restarted_engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    restarted_factory = async_sessionmaker(
        restarted_engine, class_=AsyncSession, expire_on_commit=False
    )
    scheduler_service = AsyncMock()
    scheduler = TaskScheduler()
    with (
        patch("opendata.services.scheduler.async_session_maker", restarted_factory),
        patch("opendata.services.scheduler.init_scheduler_service", return_value=scheduler_service),
    ):
        await scheduler.start()
        task_calls = [
            call
            for call in scheduler_service.add_job.await_args_list
            if call.kwargs.get("job_id", "").startswith("task_")
        ]
        assert len(task_calls) == 1
        assert task_calls[0].kwargs["kwargs"]["task_id"] == 1
        assert task_calls[0].kwargs["func"].__self__ is scheduler
        await scheduler.shutdown()
    await restarted_engine.dispose()


@pytest.mark.asyncio
async def test_pipeline_executor_dispatches_allowlisted_six_step_arguments(test_db) -> None:
    """Pipeline execution calls the existing runner with validated task arguments."""
    task = await _pipeline_task(
        test_db,
        parameters={
            "domain": "stock_daily",
            "source": "ths",
            "second_source": "akshare",
            "symbols": ["600519.SH"],
            "limit": 20,
            "lookback_days": 3,
            "shard_size": 1,
        },
    )
    outcome = MagicMock()
    outcome.failures = 0
    outcome.as_dict.return_value = {"domain": "stock_daily", "dwd_rows": 1}
    runner = AsyncMock(return_value=outcome)
    scheduler = TaskScheduler()
    with patch("opendata.pipeline.jobs.run_incremental_job", runner):
        await scheduler._execute_with_retry(task, test_db)

    runner.assert_awaited_once_with(
        domain="stock_daily",
        source="ths",
        second_source="akshare",
        symbols=["600519.SH"],
        limit=20,
        lookback_days=3,
        shard_size=1,
    )
    execution = await test_db.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    assert execution is not None
    assert execution.script_id is None
    assert execution.status == TaskStatus.COMPLETED
    assert execution.result == {
        "success": True,
        "data": {"domain": "stock_daily", "dwd_rows": 1},
        "error": None,
    }


@pytest.mark.asyncio
async def test_revoked_pipeline_owner_is_failed_before_runner_call(test_db) -> None:
    """The scheduler refreshes owner state and refuses provider work after demotion."""
    task = await _pipeline_task(test_db, owner_role=UserRole.USER)
    scheduler = TaskScheduler()
    runner = AsyncMock()
    with patch("opendata.pipeline.jobs.run_incremental_job", runner):
        await scheduler._execute_with_retry(task, test_db)

    runner.assert_not_awaited()
    execution = await test_db.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    assert execution is not None
    assert execution.status == TaskStatus.FAILED
    assert "active administrator" in (execution.error_message or "")


@pytest.mark.asyncio
async def test_pipeline_runner_exception_and_timeout_are_not_success(test_db) -> None:
    """Runner failures and deadline expiry both leave terminal non-success records."""
    task = await _pipeline_task(test_db)
    scheduler = TaskScheduler()
    failure = AsyncMock(side_effect=RuntimeError("runner failed"))
    with (
        patch("opendata.pipeline.jobs.run_incremental_job", failure),
        patch(
            "opendata.services.notification_service.NotificationService.notify_task_failed",
            new_callable=AsyncMock,
        ),
    ):
        await scheduler._execute_with_retry(task, test_db)
    failed = await test_db.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    assert failed is not None
    assert failed.status == TaskStatus.FAILED

    timeout_task = await _pipeline_task(test_db, timeout=1)
    timeout_runner = AsyncMock(side_effect=asyncio.TimeoutError())
    with (
        patch("opendata.pipeline.jobs.run_incremental_job", timeout_runner),
        patch(
            "opendata.services.notification_service.NotificationService.notify_task_failed",
            new_callable=AsyncMock,
        ),
    ):
        await scheduler._execute_with_retry(timeout_task, test_db)
    timed_out = await test_db.scalar(
        select(TaskExecution)
        .where(TaskExecution.task_id == timeout_task.id)
        .order_by(TaskExecution.created_at.desc())
    )
    assert timed_out is not None
    assert timed_out.status == TaskStatus.TIMEOUT


@pytest.mark.asyncio
async def test_pipeline_cancellation_marks_execution_cancelled(test_engine, test_db) -> None:
    """A user cancellation propagates through the runner and is not recorded as success."""
    task = await _pipeline_task(test_db)
    session_factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    runner_started = asyncio.Event()

    async def wait_for_cancel(**_: object) -> None:
        runner_started.set()
        await asyncio.Event().wait()

    scheduler = TaskScheduler()
    with (
        patch("opendata.pipeline.jobs.run_incremental_job", side_effect=wait_for_cancel),
        patch("opendata.services.scheduler.async_session_maker", session_factory),
    ):
        running = asyncio.create_task(scheduler._execute_with_retry(task, test_db))
        await asyncio.wait_for(runner_started.wait(), timeout=1)
        scheduler._running_tasks[task.id] = running
        assert await scheduler.cancel_task(task.id) is True
        with pytest.raises(asyncio.CancelledError):
            await running

    execution = await test_db.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    assert execution is not None
    assert execution.status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_script_executor_path_still_runs_and_records_script_id(test_db) -> None:
    """Legacy script tasks retain their original executor and execution identity."""
    user = User(
        username="script_owner",
        email="script_owner@example.test",
        hashed_password="unused",
        role=UserRole.USER,
        is_active=True,
    )
    test_db.add(user)
    script = DataScript(
        script_id="legacy_daily_script",
        script_name="Legacy daily script",
        category="stock",
        frequency=ScriptFrequency.DAILY,
        is_active=True,
    )
    test_db.add(script)
    await test_db.flush()
    task = ScheduledTask(
        name="legacy task",
        user_id=user.id,
        task_kind=TaskKind.SCRIPT.value,
        script_id=script.script_id,
        schedule_type=ScheduleType.DAILY,
        schedule_expression="02:00",
        parameters={"symbol": "600519"},
        is_active=True,
        retry_on_failure=False,
        max_retries=1,
    )
    test_db.add(task)
    await test_db.commit()

    script_service = AsyncMock()
    script_service.get_script.return_value = script
    script_service.execute_script.return_value = {"success": True, "rows": 1}
    with patch("opendata.services.scheduler.ScriptService", return_value=script_service):
        await TaskScheduler()._execute_with_retry(task, test_db)

    script_service.execute_script.assert_awaited_once_with(
        script_id="legacy_daily_script",
        execution_id=script_service.execute_script.await_args.kwargs["execution_id"],
        params={"symbol": "600519"},
        timeout=None,
    )
    execution = await test_db.scalar(select(TaskExecution).where(TaskExecution.task_id == task.id))
    assert execution is not None
    assert execution.script_id == "legacy_daily_script"
    assert execution.status == TaskStatus.COMPLETED
