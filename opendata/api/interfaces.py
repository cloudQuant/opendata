"""
Data interface API routes.

Provides endpoints for browsing and managing akshare data interfaces.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from opendata.api.dependencies import CurrentAdmin, get_db
from opendata.api.schemas import (
    APIResponse,
    CategoryResponse,
    InterfaceListResponse,
    InterfaceResponse,
    PaginatedParams,
    PaginatedResponse,
)
from opendata.models.interface import DataInterface, InterfaceCategory

router = APIRouter()


@router.get(
    "/categories",
)
async def list_categories(
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    List all interface categories.

    Returns all available data interface categories with descriptions.
    """
    from opendata.utils.cache import api_cache

    cached = api_cache.get("interface_categories")
    if cached is not None:
        return APIResponse(success=True, message="success", data=cached)

    result = await db.execute(
        select(InterfaceCategory).order_by(InterfaceCategory.sort_order, InterfaceCategory.name)
    )
    categories = result.scalars().all()

    data = [CategoryResponse.model_validate(c).model_dump() for c in categories]
    api_cache.set("interface_categories", data, ttl=300)  # 5 min cache

    return APIResponse(
        success=True,
        message="success",
        data=data,
    )


@router.get(
    "/",
)
async def list_interfaces(
    db: AsyncSession = Depends(get_db),
    params: PaginatedParams = Depends(),
    category_id: int | None = Query(None, description="Filter by category ID"),
    search: str | None = Query(None, description="Search by name or description"),
    is_active: bool | None = Query(None, description="Filter by active status"),
) -> APIResponse:
    """
    List data interfaces with pagination.

    Returns paginated list of data interfaces with optional filtering.
    """
    # Build base query
    query = select(DataInterface)

    # Apply filters
    if category_id is not None:
        query = query.where(DataInterface.category_id == category_id)

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            (DataInterface.name.ilike(search_pattern))
            | (DataInterface.display_name.ilike(search_pattern))
            | (DataInterface.description.ilike(search_pattern))
        )

    if is_active is not None:
        query = query.where(DataInterface.is_active == is_active)

    # Get total count
    count_query = select(func.count()).select_from(query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Get paginated results
    query = (
        query.options(selectinload(DataInterface.category))
        .order_by(DataInterface.id)
        .offset((params.page - 1) * params.page_size)
        .limit(params.page_size)
    )

    result = await db.execute(query)
    interfaces = result.scalars().all()

    # Build response items
    items = [
        InterfaceListResponse(
            id=iface.id,
            name=iface.name,
            display_name=iface.display_name,
            description=iface.description,
            category_name=iface.category.name if iface.category else None,
            is_active=iface.is_active,
        )
        for iface in interfaces
    ]

    return APIResponse(
        success=True,
        message="success",
        data=PaginatedResponse(
            items=items,
            total=total,
            page=params.page,
            page_size=params.page_size,
            total_pages=(total + params.page_size - 1) // params.page_size,
        ).model_dump(),
    )


@router.get(
    "/{interface_id}",
)
async def get_interface(
    interface_id: int,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Get data interface details.

    Returns detailed information about a specific data interface
    including parameters and usage examples.
    """
    result = await db.execute(
        select(DataInterface)
        .options(
            selectinload(DataInterface.category),
            selectinload(DataInterface.params),
        )
        .where(DataInterface.id == interface_id)
    )
    iface = result.scalar_one_or_none()

    if iface is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data interface not found",
        )

    return APIResponse(
        success=True,
        message="success",
        data=InterfaceResponse(
            id=iface.id,
            name=iface.name,
            display_name=iface.display_name,
            description=iface.description,
            category_id=iface.category_id,
            category_name=iface.category.name if iface.category else None,
            module_path=iface.module_path,
            function_name=iface.function_name,
            parameters=iface.parameters,
            return_type=iface.return_type,
            example=iface.example,
            is_active=iface.is_active,
        ).model_dump(),
    )


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_interface(
    current_admin: CurrentAdmin,
    name: str,
    display_name: str,
    category_id: int,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Create a new data interface.

    Admin only endpoint for adding new data interfaces.
    """
    # Verify category exists
    result = await db.execute(select(InterfaceCategory).where(InterfaceCategory.id == category_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Category not found",
        )

    # Create interface
    interface = DataInterface(
        name=name,
        display_name=display_name,
        category_id=category_id,
        parameters={},
        return_type="DataFrame",
    )

    db.add(interface)
    await db.commit()
    await db.refresh(interface)

    return APIResponse(
        success=True,
        message="Data interface created successfully",
        data={"interface_id": interface.id},
    )


@router.put(
    "/{interface_id}",
)
async def update_interface(
    current_admin: CurrentAdmin,
    interface_id: int,
    display_name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Update data interface.

    Admin only endpoint for updating interface details.
    """
    result = await db.execute(select(DataInterface).where(DataInterface.id == interface_id))
    iface = result.scalar_one_or_none()

    if iface is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data interface not found",
        )

    # Update fields
    if display_name is not None:
        iface.display_name = display_name
    if description is not None:
        iface.description = description
    if is_active is not None:
        iface.is_active = is_active

    await db.commit()

    return APIResponse(
        success=True,
        message="Data interface updated successfully",
    )


@router.delete(
    "/{interface_id}",
)
async def delete_interface(
    current_admin: CurrentAdmin,
    interface_id: int,
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """
    Delete data interface.

    Admin only endpoint for deleting interfaces.
    """
    result = await db.execute(select(DataInterface).where(DataInterface.id == interface_id))
    iface = result.scalar_one_or_none()

    if iface is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data interface not found",
        )

    await db.delete(iface)
    await db.commit()

    return APIResponse(
        success=True,
        message="Data interface deleted successfully",
    )
