"""HTTP routes for the ported data-governance plane."""

import typing

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from opendata.afi_port.api.deps import get_db, require_data_admin_user
from opendata.afi_port.services.data_connectors import DataGovernanceService

router = APIRouter(
    prefix="/data-governance",
    tags=["Data Governance"],
    dependencies=[Depends(require_data_admin_user)],
)


def get_data_governance_service(db: AsyncSession = Depends(get_db)) -> DataGovernanceService:
    """Build the per-request governance service bound to the session."""
    return DataGovernanceService(db)


@router.post("/bootstrap", response_model=None)
async def bootstrap_data_governance(
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """Bootstrap the governance catalog (providers, datasets, endpoints)."""
    return await service.bootstrap()


@router.get("/providers", response_model=None)
async def list_providers(
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """List the registered governance providers."""
    return await service.list_providers()


@router.get("/endpoints", response_model=None)
async def list_endpoints(
    provider_id: str | None = None,
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """List governance endpoints, optionally filtered to one provider."""
    return await service.list_endpoints(provider_id=provider_id)


@router.post("/endpoints/{endpoint_id}/preview", response_model=None)
async def preview_endpoint(
    endpoint_id: str,
    payload: dict | None = None,
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """Run a non-persisting preview of one governance endpoint."""
    result = await service.preview_endpoint(endpoint_id, dict((payload or {}).get("params") or {}))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint_not_found")
    return result


@router.get("/jobs", response_model=None)
async def list_jobs(
    endpoint_id: str | None = None,
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """List governance ingest jobs, optionally filtered to one endpoint."""
    return await service.list_jobs(endpoint_id=endpoint_id)


@router.get("/jobs/{job_id}", response_model=None)
async def get_job(
    job_id: str,
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """Fetch one governance job or 404."""
    result = await service.get_job(job_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job_not_found")
    return result


@router.post(
    "/endpoints/{endpoint_id}/jobs", status_code=status.HTTP_201_CREATED, response_model=None
)
async def create_job(
    endpoint_id: str,
    payload: dict | None = None,
    service: DataGovernanceService = Depends(get_data_governance_service),
) -> typing.Any:
    """Create an ingest job for one governance endpoint."""
    result = await service.create_job(endpoint_id, dict((payload or {}).get("params") or {}))
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="endpoint_not_found")
    return result
