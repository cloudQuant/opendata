"""Data acquisition API routes.

Provides endpoints for manual data download and progress tracking.
"""

import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import CurrentUser, get_db
from opendata.api.schemas import (
    APIResponse,
    DataDownloadRequest,
    DataDownloadResponse,
    DownloadProgressResponse,
    SourceCatalog,
)
from opendata.data.capability import Capability
from opendata.data.registry import authority_baseline, get_registry
from opendata.models.interface import DataInterface
from opendata.models.task import TaskExecution, TaskStatus
from opendata.services.data_acquisition import DataAcquisitionService
from opendata.utils.constants import ESTIMATED_DOWNLOAD_SECONDS

router = APIRouter()

# Service instance
data_service = DataAcquisitionService()


@router.get("/capabilities")
async def list_capabilities(current_user: CurrentUser) -> list[Capability]:
    """List registered provider capabilities (design §4.3, FR-2).

    Every registered data-source capability with its routing fields
    and verification state; unverified capabilities do not serve
    ``source="auto"`` requests (FR-3).
    """
    return get_registry().capabilities()


@router.get("/sources")
async def list_sources(current_user: CurrentUser) -> SourceCatalog:
    """List the authority baseline and the registered sources (design §4.4).

    ``authority`` is the domain -> ordered-sources table that drives
    ``source="auto"`` routing; ``registered`` inverts the live
    registry into a source -> domains view.
    """
    registered: dict[str, list[str]] = {}
    for capability in get_registry().capabilities():
        registered.setdefault(capability.source, []).append(capability.domain)
    return SourceCatalog(
        authority={domain: list(sources) for domain, sources in authority_baseline().items()},
        registered={source: sorted(domains) for source, domains in registered.items()},
    )


def _log_task_exception(task: asyncio.Task) -> None:
    """Log unhandled exceptions from background asyncio tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error(
            "Unhandled exception in background task: %s: %s",
            type(exc).__name__,
            exc,
        )


@router.post("/download", status_code=status.HTTP_202_ACCEPTED)
async def trigger_download(
    request: DataDownloadRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DataDownloadResponse:
    """Trigger manual data download.

    Initiates data acquisition for the specified interface.
    Returns execution ID immediately for progress tracking.
    The actual download runs as a background task.
    """
    # Get interface
    result = await db.execute(select(DataInterface).where(DataInterface.id == request.interface_id))
    iface = result.scalar_one_or_none()

    if iface is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data interface not found",
        )

    if not iface.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Data interface is not active",
        )

    # Create task execution record
    execution = TaskExecution(
        execution_id=f"dl_{uuid.uuid4().hex[:12]}",
        task_id=None,  # Manual download has no parent task
        script_id=iface.name,
        status=TaskStatus.PENDING,
        retry_count=0,
    )

    db.add(execution)
    await db.commit()
    await db.refresh(execution)

    # Capture IDs before launching background task (request db session
    # will be closed after the response is sent)
    exec_id = execution.id
    iface_id = iface.id
    params = request.parameters

    # Launch download in the background with its own sessions: main db
    # for metadata/execution state, data warehouse for the data table
    # (FR-17 session routing fix).
    async def _bg_download() -> None:
        from opendata.core.database import async_session_maker as _sm
        from opendata.core.database import data_session_maker as _dsm

        async with _sm() as bg_db, _dsm() as bg_data_db:
            try:
                await data_service.execute_download(
                    execution_id=exec_id,
                    interface_id=iface_id,
                    parameters=params,
                    db=bg_db,
                    data_db=bg_data_db,
                )
            except Exception as e:
                logger.error(
                    "Background download failed for execution %s: %s",
                    exec_id,
                    e,
                )

    bg = asyncio.create_task(_bg_download())
    bg.add_done_callback(_log_task_exception)

    return DataDownloadResponse(
        execution_id=execution.id,
        status="pending",
        message="Data download started",
    )


@router.get(
    "/download/{execution_id}/status",
)
async def get_download_progress(
    execution_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> DownloadProgressResponse:
    """Get download progress.

    Returns current status and progress of data download.
    """
    result = await db.execute(select(TaskExecution).where(TaskExecution.id == execution_id))
    execution = result.scalar_one_or_none()

    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found",
        )

    # Calculate progress
    progress = 0.0
    if execution.status == TaskStatus.COMPLETED:
        progress = 100.0
    elif execution.status == TaskStatus.RUNNING:
        if execution.start_time:
            now = datetime.now(timezone.utc)
            st = execution.start_time
            # Handle naive vs aware datetime comparison
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            elapsed = (now - st).total_seconds()
            # Simple progress estimate (could be improved with actual progress tracking)
            progress = min(50.0, elapsed * 2)
    elif execution.status == TaskStatus.FAILED:
        progress = 0.0

    # Estimate completion (rough estimate for running tasks)
    estimated_completion = None
    if execution.status == TaskStatus.RUNNING and execution.start_time:
        estimated_completion = execution.start_time.timestamp() + ESTIMATED_DOWNLOAD_SECONDS

    return DownloadProgressResponse(
        execution_id=execution.id,
        status=execution.status.value,
        progress=progress,
        message=execution.error_message if execution.status == TaskStatus.FAILED else None,
        rows_processed=execution.rows_after,
        started_at=execution.start_time,
        estimated_completion=datetime.fromtimestamp(estimated_completion, timezone.utc)
        if estimated_completion
        else None,
    )


@router.get("/download/{execution_id}/result")
async def get_download_result(
    execution_id: int,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Get download result data.

    Returns the actual data from completed download.
    For successful downloads, returns the data rows.
    """
    result = await db.execute(select(TaskExecution).where(TaskExecution.id == execution_id))
    execution = result.scalar_one_or_none()

    if execution is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Execution not found",
        )

    if execution.status == TaskStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail="Download still in progress",
        )

    if execution.status == TaskStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail="Download queued",
        )

    if execution.status == TaskStatus.FAILED or execution.status == TaskStatus.TIMEOUT:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Download failed: {execution.error_message}",
        )

    # Return success info
    return APIResponse(
        success=True,
        message="Download completed",
        data={
            "execution_id": execution_id,
            "rows_affected": execution.rows_after,
            "duration_ms": execution.duration,
            "completed_at": execution.end_time.isoformat() if execution.end_time else None,
        },
    )
