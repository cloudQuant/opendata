"""
Scheduled task API routes.

Provides endpoints for managing scheduled data acquisition tasks.
"""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import get_current_user
from opendata.api.schemas import (
    APIResponse,
    ScheduleTemplate,
    TaskCreateRequest,
    TaskUpdateRequest,
)
from opendata.core.database import get_db
from opendata.models.data_script import DataScript
from opendata.models.task import ScheduledTask, ScheduleType
from opendata.models.user import User
from opendata.services.scheduler import task_scheduler

router = APIRouter()


# Preset schedule templates (from cloud_quant)
SCHEDULE_TEMPLATES = [
    ScheduleTemplate(
        value="every_hour",
        label="每小时",
        description="每小时执行一次",
        cron_expression="0 * * * *",
    ),
    ScheduleTemplate(
        value="every_day_at_8",
        label="每天8点",
        description="每天早上8点执行",
        cron_expression="0 8 * * *",
    ),
    ScheduleTemplate(
        value="every_day_at_18",
        label="每天18点",
        description="每天下午18点执行",
        cron_expression="0 18 * * *",
    ),
    ScheduleTemplate(
        value="every_monday_at_8",
        label="每周一8点",
        description="每周一早上8点执行",
        cron_expression="0 8 * * 1",
    ),
    ScheduleTemplate(
        value="every_month_1st",
        label="每月1号",
        description="每月1号早上8点执行",
        cron_expression="0 8 1 * *",
    ),
    ScheduleTemplate(
        value="workdays_at_8",
        label="工作日8点",
        description="周一到周五早上8点执行",
        cron_expression="0 8 * * 1-5",
    ),
    ScheduleTemplate(
        value="workdays_at_18",
        label="工作日18点",
        description="周一到周五下午18点执行",
        cron_expression="0 18 * * 1-5",
    ),
    ScheduleTemplate(
        value="every_30_minutes",
        label="每30分钟",
        description="每30分钟执行一次",
        cron_expression="*/30 * * * *",
    ),
    ScheduleTemplate(
        value="every_10_minutes",
        label="每10分钟",
        description="每10分钟执行一次",
        cron_expression="*/10 * * * *",
    ),
]


def _task_to_dict(task: ScheduledTask, script_name: str | None = None) -> dict[str, Any]:
    """Convert task model to dictionary."""
    return task.to_dict(script_name=script_name)


# Note: TaskCreateRequest, TaskUpdateRequest, TaskResponse, PaginatedTaskResponse
# are imported from opendata.api.schemas (canonical location).


async def _can_access_task(user_id: int, task: ScheduledTask, is_admin: bool) -> bool:
    """Check if user can access task."""
    from opendata.core.security import PermissionChecker

    return PermissionChecker.can_access_owned_resource(user_id, task.user_id, is_admin)


def _apply_task_update(task: ScheduledTask, request: TaskUpdateRequest) -> None:
    """Apply TaskUpdateRequest fields to ScheduledTask model."""
    if request.name is not None:
        task.name = request.name
    if request.description is not None:
        task.description = request.description
    if request.schedule_type is not None:
        task.schedule_type = ScheduleType(request.schedule_type)
    if request.schedule_expression is not None:
        task.schedule_expression = request.schedule_expression
    if request.parameters is not None:
        task.parameters = request.parameters
    if request.is_active is not None:
        task.is_active = request.is_active
    if request.retry_on_failure is not None:
        task.retry_on_failure = request.retry_on_failure
    if request.max_retries is not None:
        task.max_retries = request.max_retries
    if request.timeout is not None:
        task.timeout = request.timeout


@router.get("/schedule/templates")
async def get_schedule_templates(
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get preset schedule templates."""
    return APIResponse(success=True, message="success", data={"templates": SCHEDULE_TEMPLATES})


@router.get("/")
async def list_tasks(
    current_user: User = Depends(get_current_user),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """List scheduled tasks."""
    from opendata.models.user import UserRole

    is_admin = current_user.role == UserRole.ADMIN

    # Build base query
    if is_admin:
        query = select(ScheduledTask)
    else:
        query = select(ScheduledTask).where(ScheduledTask.user_id == current_user.id)

    # Apply filters
    if is_active is not None:
        query = query.where(ScheduledTask.is_active == is_active)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = (
        query.order_by(ScheduledTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )

    result = await db.execute(query)
    tasks = result.scalars().all()

    # Batch-load script names to avoid N+1 queries
    script_ids = list({t.script_id for t in tasks})
    script_name_map: dict[str, str | None] = {}
    if script_ids:
        scripts_result = await db.execute(
            select(DataScript.script_id, DataScript.script_name).where(
                DataScript.script_id.in_(script_ids)
            )
        )
        script_name_map = {row.script_id: row.script_name for row in scripts_result}

    items = []
    for task in tasks:
        script_name = script_name_map.get(task.script_id)
        items.append(task.to_dict(script_name=script_name))

    return APIResponse(
        success=True,
        message="success",
        data={
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
        },
    )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_task(
    request: TaskCreateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Create a new scheduled task."""
    # Verify script exists
    script_result = await db.execute(
        select(DataScript).where(DataScript.script_id == request.script_id)
    )
    script = script_result.scalar_one_or_none()

    if script is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Data script not found",
        )

    if not script.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Data script is not active",
        )

    # Validate schedule type
    try:
        schedule_type = ScheduleType(request.schedule_type)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid schedule type",
        ) from None

    # Create task
    task = ScheduledTask(
        name=request.name,
        description=request.description,
        user_id=current_user.id,
        script_id=request.script_id,
        schedule_type=schedule_type,
        schedule_expression=request.schedule_expression,
        parameters=request.parameters or {},
        is_active=request.is_active,
        retry_on_failure=request.retry_on_failure,
        max_retries=request.max_retries,
        timeout=request.timeout,
    )

    db.add(task)
    await db.commit()
    await db.refresh(task)

    # Schedule the task if active
    if task.is_active:
        await task_scheduler.add_task(task.id, db)

    return APIResponse(
        success=True,
        message="Task created successfully",
        data=_task_to_dict(task, script.script_name),
    )


@router.get("/{task_id}")
async def get_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Get scheduled task details."""
    from opendata.models.user import UserRole

    result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled task not found",
        )

    # Check access
    if not await _can_access_task(current_user.id, task, current_user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Get script info
    script_result = await db.execute(
        select(DataScript).where(DataScript.script_id == task.script_id)
    )
    script = script_result.scalar_one_or_none()
    script_name = script.script_name if script else None

    return APIResponse(
        success=True,
        message="success",
        data=_task_to_dict(task, script_name),
    )


@router.put("/{task_id}")
async def update_task(
    task_id: int,
    request: TaskUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Update scheduled task."""
    from opendata.models.user import UserRole

    result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled task not found",
        )

    if not await _can_access_task(current_user.id, task, current_user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    try:
        _apply_task_update(task, request)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid schedule type",
        ) from None

    task.updated_at = datetime.now(UTC)

    await db.commit()
    await db.refresh(task)

    if task.is_active:
        await task_scheduler.update_task(task.id, db)
    else:
        await task_scheduler.remove_task(task.id)

    script_result = await db.execute(
        select(DataScript).where(DataScript.script_id == task.script_id)
    )
    script = script_result.scalar_one_or_none()
    script_name = script.script_name if script else None

    return APIResponse(
        success=True,
        message="Task updated successfully",
        data=_task_to_dict(task, script_name),
    )


@router.delete("/{task_id}")
async def delete_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Delete scheduled task."""
    from opendata.models.user import UserRole

    result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled task not found",
        )

    # Check access
    if not await _can_access_task(current_user.id, task, current_user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Remove from scheduler
    await task_scheduler.remove_task(task_id)

    # Delete task
    await db.delete(task)
    await db.commit()

    return APIResponse(
        success=True,
        message="Scheduled task deleted successfully",
    )


@router.post("/{task_id}/trigger")
async def trigger_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Trigger immediate task execution."""
    from opendata.models.user import UserRole

    result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled task not found",
        )

    # Check access
    if not await _can_access_task(current_user.id, task, current_user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    # Trigger execution
    execution_id = await task_scheduler.trigger_task(task_id, current_user.id)

    return APIResponse(
        success=True,
        message="Task triggered successfully",
        data={"execution_id": execution_id},
    )


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Cancel a running task execution."""
    from opendata.models.user import UserRole

    result = await db.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
    task = result.scalar_one_or_none()

    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Scheduled task not found",
        )

    if not await _can_access_task(current_user.id, task, current_user.role == UserRole.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    cancelled = await task_scheduler.cancel_task(task_id)

    if not cancelled:
        return APIResponse(
            success=False,
            message="No running execution found for this task",
        )

    return APIResponse(
        success=True,
        message="Task execution cancelled",
    )
