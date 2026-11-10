"""HTTP routes for the ported data-interface registry and bootstrap."""

import typing

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.afi_port.api.deps import get_db, require_data_admin_user
from opendata.afi_port.schemas.akshare_mgmt import DataInterfaceCreate, DataInterfaceUpdate
from opendata.afi_port.services.interface import AkshareInterfaceService
from opendata.afi_port.services.interface_loader import AkshareInterfaceLoader

router = APIRouter()


def get_akshare_interface_service(
    db: AsyncSession = Depends(get_db),
) -> AkshareInterfaceService:
    """Per-request factory for :class:`AkshareInterfaceService`.

    Bound to the request-scoped DB session, so we cannot ``@lru_cache`` here.
    """
    return AkshareInterfaceService(db)


@router.get("/interfaces/categories", response_model=None)
async def list_interface_categories(
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """List the interface categories."""
    return await service.list_categories()


@router.get("/interfaces", response_model=None)
async def list_interfaces(
    category_id: int | None = None,
    search: str | None = None,
    is_active: bool | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Page over data interfaces with optional filters."""
    return await service.list_interfaces(
        category_id=category_id,
        search=search,
        is_active=is_active,
        page=page,
        page_size=page_size,
    )


@router.get("/interfaces/{interface_id}", response_model=None)
async def get_interface(
    interface_id: int,
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Fetch one interface or 404."""
    interface = await service.get_interface(interface_id)
    if interface is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface not found")
    return interface


@router.post("/interfaces", status_code=status.HTTP_201_CREATED, response_model=None)
async def create_interface(
    payload: DataInterfaceCreate,
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Register a new data interface."""
    return await service.create_interface(payload)


@router.put("/interfaces/{interface_id}", response_model=None)
async def update_interface(
    interface_id: int,
    payload: DataInterfaceUpdate,
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Apply a partial update to one interface or 404."""
    interface = await service.update_interface(interface_id, payload)
    if interface is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface not found")
    return interface


@router.delete(
    "/interfaces/{interface_id}", status_code=status.HTTP_204_NO_CONTENT, response_model=None
)
async def delete_interface(
    interface_id: int,
    service: AkshareInterfaceService = Depends(get_akshare_interface_service),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Delete one interface and return 204, or 404 when absent."""
    if not await service.delete_interface(interface_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interface not found")


@router.post("/interfaces/bootstrap", response_model=None)
async def bootstrap_interfaces(
    refresh: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: typing.Any = Depends(require_data_admin_user),
) -> typing.Any:
    """Bootstrap or refresh the interface registry from the catalog."""
    return await AkshareInterfaceLoader(db).bootstrap(refresh=refresh)
