"""
Task execution API endpoints.

Provides endpoints for viewing and managing task executions.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import get_current_admin_user, get_current_user
from opendata.api.schemas import APIResponse, ErrorResponse

COMMON_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    403: {"model": ErrorResponse, "description": "Insufficient permissions"},
}
from opendata.core.database import get_db
from opendata.models.task import TaskExecution, TaskStatus
from opendata.models.user import User
from opendata.services.execution_service import ExecutionService

router = APIRouter()


class ExecutionListResponse(BaseModel):
    """Response model for execution list."""

    items: list[dict[str, Any]]
    total: int
    page: int
    page_size: int


class ExecutionStatsResponse(BaseModel):
    """Response model for execution statistics."""

    total_count: int
    success_count: int
    failed_count: int
    success_rate: float
    avg_duration: float
    today_executions: int


@router.get("/", responses=COMMON_RESPONSES)
async def get_executions(
    task_id: int | None = None,
    script_id: str | None = None,
    status: TaskStatus | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """
    Get list of task executions.

    Supports filtering by task, script, status, and date range.
    """
    service = ExecutionService(db)
    skip = (page - 1) * page_size

    executions, total = await service.get_executions(
        task_id=task_id,
        script_id=script_id,
        status=status,
        start_date=start_date,
        end_date=end_date,
        skip=skip,
        limit=page_size,
    )

    items = [_execution_to_dict(execution) for execution in executions]

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


@router.get("/stats")
async def get_execution_stats(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get execution statistics."""
    service = ExecutionService(db)
    stats = await service.get_execution_stats(start_date=start_date, end_date=end_date)
    return APIResponse(success=True, message="success", data=stats)


@router.get("/recent")
async def get_recent_executions(
    limit: int = Query(50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get recent executions."""
    service = ExecutionService(db)
    executions = await service.get_recent_executions(limit=limit)
    return APIResponse(
        success=True,
        message="success",
        data=[_execution_to_dict(execution) for execution in executions],
    )


@router.get("/running")
async def get_running_executions(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get currently running executions."""
    service = ExecutionService(db)
    executions = await service.get_running_executions()
    return APIResponse(
        success=True,
        message="success",
        data=[_execution_to_dict(execution) for execution in executions],
    )


@router.get("/failed")
async def get_failed_executions(
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get failed executions."""
    service = ExecutionService(db)
    executions = await service.get_failed_executions(limit=limit)
    return APIResponse(
        success=True,
        message="success",
        data=[_execution_to_dict(execution) for execution in executions],
    )


@router.get(
    "/{execution_id}",
    responses={
        **COMMON_RESPONSES,
        404: {"model": ErrorResponse, "description": "Execution not found"},
    },
)
async def get_execution(
    execution_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get execution details by ID."""
    service = ExecutionService(db)
    execution = await service.get_by_execution_id(execution_id)

    if not execution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")

    return APIResponse(success=True, message="success", data=_execution_to_dict(execution))


@router.delete(
    "/",
    responses={
        **COMMON_RESPONSES,
        400: {"model": ErrorResponse, "description": "Must provide execution_ids or status"},
    },
)
async def delete_executions(
    execution_ids: list[str] | None = None,
    status_filter: TaskStatus | None = Query(None, alias="status"),
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Delete executions by IDs or status.

    Requires admin privileges.
    """
    service = ExecutionService(db)

    if execution_ids:
        count = await service.delete_executions_by_ids(execution_ids)
    elif status_filter is not None:
        count = await service.delete_executions_by_status(status_filter)
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either execution_ids or status",
        )

    return APIResponse(
        success=True, message=f"Deleted {count} execution(s)", data={"deleted_count": count}
    )


def _execution_to_dict(execution: TaskExecution) -> dict[str, Any]:
    """Convert execution model to dictionary."""
    return execution.to_dict()
