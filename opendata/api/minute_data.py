"""REST queries over locally archived minute Parquet data."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger

from opendata.api.dependencies import CurrentPrincipal, require_domain_access
from opendata.api.schemas import APIResponse
from opendata.data.minute_archive import (
    MinuteArchiveIndexError,
    MinuteArchiveIntegrityError,
    MinuteArchiveValidationError,
    get_minute_archive_engine,
    get_minute_archive_root,
    query_minute_archive,
    validate_asset_domain,
)

router = APIRouter()

if TYPE_CHECKING:
    from sqlalchemy import Engine


def _default_window(start: date | None, end: date | None) -> tuple[date, date]:
    """Resolve the optional date range to one UTC calendar day by default."""
    effective_end = end or datetime.now(timezone.utc).date()
    return (start or effective_end), effective_end


@router.get("/minute/{asset_class}/{domain}")
async def query_minute_data(
    asset_class: str,
    domain: str,
    principal: CurrentPrincipal,
    symbols: Annotated[list[str], Query(min_length=1, max_length=20)],
    source: Annotated[str, Query(min_length=1, max_length=64)],
    period: Annotated[str, Query(min_length=2, max_length=3)],
    start: date | None = Query(None),
    end: date | None = Query(None),
    fields: list[str] | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=1000),
    engine: Engine = Depends(get_minute_archive_engine),
) -> APIResponse:
    """Read bounded minute records from the local Parquet archive.

    Access uses the existing authenticated domain policy. The endpoint reads
    only the main-database metadata index and local immutable files; it does
    not call a market-data provider or query a minute table in the warehouse.
    """
    require_domain_access(principal, domain)
    try:
        validate_asset_domain(asset_class, domain)
    except MinuteArchiveValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="minute archive route does not match a supported Bar domain",
        ) from exc
    window_start, window_end = _default_window(start, end)
    try:
        result = await asyncio.to_thread(
            query_minute_archive,
            engine,
            get_minute_archive_root(),
            asset_class=asset_class,
            domain=domain,
            symbols=symbols,
            source=source,
            period=period,
            start=window_start,
            end=window_end,
            fields=fields,
            page=page,
            page_size=page_size,
        )
    except MinuteArchiveValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except MinuteArchiveIndexError as exc:
        logger.error(f"minute archive metadata index unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="minute archive metadata index is unavailable",
        ) from exc
    except MinuteArchiveIntegrityError as exc:
        logger.error(f"minute archive integrity check failed: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="minute archive integrity check failed",
        ) from exc
    return APIResponse(success=True, message="success", data=result.as_dict())


__all__ = ["query_minute_data", "router"]
