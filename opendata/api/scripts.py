"""
Data script management API endpoints.

Provides endpoints for managing data acquisition scripts.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.api.dependencies import get_current_admin_user, get_current_user
from opendata.api.schemas import APIResponse, ErrorResponse, ScriptCreateRequest, ScriptUpdateRequest
from opendata.core.database import get_db
from opendata.models.data_script import DataScript, ScriptFrequency
from opendata.models.user import User
from opendata.services.script_service import ScriptService

router = APIRouter()


# Note: ScriptCreateRequest, ScriptUpdateRequest are imported from
# app.api.schemas (canonical location).


def _script_to_dict(script: DataScript) -> dict[str, Any]:
    """Convert script model to dictionary."""
    return script.to_dict()


@router.get("/")
async def get_scripts(
    category: str | None = None,
    frequency: ScriptFrequency | None = None,
    is_active: bool | None = None,
    keyword: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=2000),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """
    Get list of data scripts.

    Supports filtering by category, frequency, status, and keyword search.
    """
    service = ScriptService(db)
    skip = (page - 1) * page_size

    scripts, total = await service.get_scripts(
        category=category,
        frequency=frequency,
        is_active=is_active,
        keyword=keyword,
        skip=skip,
        limit=page_size,
    )

    items = [_script_to_dict(script) for script in scripts]

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
async def get_script_stats(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get script statistics."""
    service = ScriptService(db)
    stats = await service.get_script_stats()
    return APIResponse(success=True, message="success", data=stats)


@router.post("/scan")
async def scan_scripts(
    db: AsyncSession = Depends(get_db),
    current_admin: User = Depends(get_current_admin_user),
) -> APIResponse:
    """
    Scan and register scripts from filesystem.

    Requires admin privileges.
    """
    service = ScriptService(db)
    result = await service.scan_and_register_scripts()
    return APIResponse(success=True, message="Scan completed", data=result)


@router.get("/categories")
async def get_script_categories(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get list of script categories."""
    from opendata.utils.cache import api_cache

    cached = api_cache.get("script_categories")
    if cached is not None:
        return APIResponse(success=True, message="success", data=cached)

    service = ScriptService(db)
    categories = await service.get_categories()
    api_cache.set("script_categories", categories, ttl=300)  # 5 min cache
    return APIResponse(success=True, message="success", data=categories)


@router.get(
    "/{script_id}",
    responses={
        401: {"model": ErrorResponse, "description": "Not authenticated"},
        404: {"model": ErrorResponse, "description": "Script not found"},
    },
)
async def get_script(
    script_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> APIResponse:
    """Get script details by ID."""
    service = ScriptService(db)
    script = await service.get_script(script_id)

    if not script:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")

    return APIResponse(success=True, message="success", data=_script_to_dict(script))


@router.put("/{script_id}/toggle")
async def toggle_script(
    script_id: str,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Toggle script active status.

    Requires admin privileges.
    """
    service = ScriptService(db)
    script = await service.get_script(script_id)

    if not script:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")

    updated = await service.update_script(script_id, is_active=not script.is_active)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Script not found")
    return APIResponse(success=True, message="Script status updated", data=_script_to_dict(updated))


# Admin-only endpoints for custom script management
@router.post(
    "/admin/scripts",
    status_code=status.HTTP_201_CREATED,
    responses={
        400: {"model": ErrorResponse, "description": "Script ID already exists"},
        403: {"model": ErrorResponse, "description": "Admin required"},
    },
)
async def create_custom_script(
    request: ScriptCreateRequest,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Create a new custom data script.

    Requires admin privileges.
    """
    # Check if script_id already exists
    result = await db.execute(select(DataScript).where(DataScript.script_id == request.script_id))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Script ID already exists",
        )

    # Create new custom script
    script = DataScript(
        script_id=request.script_id,
        script_name=request.script_name,
        category=request.category,
        sub_category=request.sub_category,
        description=request.description,
        source=request.source,
        target_table=request.target_table,
        module_path=request.module_path,
        function_name=request.function_name,
        dependencies=request.parameters,
        estimated_duration=request.estimated_duration,
        timeout=request.timeout,
        is_custom=True,
        is_active=True,
    )

    db.add(script)
    await db.commit()
    await db.refresh(script)

    return APIResponse(
        success=True, message="Script created successfully", data=_script_to_dict(script)
    )


@router.put("/admin/scripts/{script_id}")
async def update_script(
    script_id: str,
    request: ScriptUpdateRequest,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Update a data script.

    Requires admin privileges.
    System scripts (is_custom=False) have restrictions on category changes.
    """
    result = await db.execute(select(DataScript).where(DataScript.script_id == script_id))
    script = result.scalar_one_or_none()

    if script is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found",
        )

    # Update allowed fields
    if request.script_name is not None:
        script.script_name = request.script_name
    if request.description is not None:
        script.description = request.description
    if request.sub_category is not None:
        script.sub_category = request.sub_category
    if request.parameters is not None:
        script.dependencies = request.parameters
    if request.estimated_duration is not None:
        script.estimated_duration = request.estimated_duration
    if request.timeout is not None:
        script.timeout = request.timeout

    await db.commit()
    await db.refresh(script)

    return APIResponse(
        success=True, message="Script updated successfully", data=_script_to_dict(script)
    )


@router.delete("/admin/scripts/{script_id}")
async def delete_script(
    script_id: str,
    current_admin: User = Depends(get_current_admin_user),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Delete a custom data script.

    Requires admin privileges.
    System scripts (is_custom=False) cannot be deleted.
    """
    result = await db.execute(select(DataScript).where(DataScript.script_id == script_id))
    script = result.scalar_one_or_none()

    if script is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Script not found",
        )

    # Prevent deletion of system scripts
    if not script.is_custom:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete system built-in scripts",
        )

    await db.delete(script)
    await db.commit()

    return APIResponse(
        success=True, message="Custom script deleted successfully", data={"script_id": script_id}
    )
