"""Pipeline operations API (B3.2 / AC-13).

The failed-shard list and one-click retry surface: read the checkpoints
an earlier run left failed, and reset them to ``pending`` so the next
run of the same window retries them (the runner skips only ``done``
shards). Retrying a window is idempotent - resetting a shard that
succeeded does nothing because the filter is ``status == failed``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opendata.api.dependencies import (
    CurrentUser,  # noqa: TC001  # FastAPI resolves the Annotated alias at runtime
)
from opendata.api.schemas import APIResponse
from opendata.core.database import get_db
from opendata.pipeline.patrol import key_status, patrol
from opendata.pipeline.retry import list_failed_shards, mark_failed_for_retry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


@router.get("/health/sources")
async def source_health(
    current_user: CurrentUser,
) -> APIResponse:
    """Report the credential configuration of every source (B3.4/AC-19).

    Args:
        current_user: Authenticated user.

    Returns:
        Per-source required/configured status and endpoint.
    """
    return APIResponse(success=True, message="success", data={"sources": key_status()})


@router.post("/health/patrol")
async def run_source_patrol(
    current_user: CurrentUser,
) -> APIResponse:
    """Probe every verified capability and refresh routing health (AC-4).

    The patrol marks failing sources unhealthy so ``source=auto`` stops
    routing to them; a broken source is reported, not hidden.

    Args:
        current_user: Authenticated user.

    Returns:
        Per-capability probe results plus the key status.
    """
    results = list(await patrol())
    return APIResponse(
        success=True,
        message="success",
        data={
            "count": len(results),
            "healthy": sum(1 for result in results if result.ok),
            "results": [
                {
                    "domain": result.domain,
                    "source": result.source,
                    "ok": result.ok,
                    "error": result.error,
                    "latency_ms": round(result.latency_ms, 1),
                    "verified": result.verified,
                }
                for result in results
            ],
            "keys": key_status(),
        },
    )


@router.get("/pipeline/failures")
async def pipeline_failures(
    current_user: CurrentUser,
    domain: str | None = Query(None),
    source: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """List the shards an earlier pipeline run left failed.

    Args:
        current_user: Authenticated user (admin or any authenticated
            user - pipeline observability is operational, not domain
            data).
        domain: Restrict to one domain.
        source: Restrict to one source.
        limit: Maximum rows.
        db: Main-database session.

    Returns:
        The failure records, newest first.
    """
    rows = list(await list_failed_shards(db))
    if domain is not None:
        rows = [row for row in rows if row.domain == domain]
    if source is not None:
        rows = [row for row in rows if row.source == source]
    rows = rows[:limit]
    return APIResponse(
        success=True,
        message="success",
        data={
            "count": len(rows),
            "failures": [
                {
                    "pipeline_id": row.pipeline_id,
                    "domain": row.domain,
                    "source": row.source,
                    "shard": row.shard,
                    "window": {
                        "start": row.window_start.isoformat(),
                        "end": row.window_end.isoformat(),
                    },
                    "error": row.error,
                }
                for row in rows
            ],
        },
    )


@router.post("/pipeline/retry-failed", status_code=status.HTTP_200_OK)
async def pipeline_retry_failed(
    current_user: CurrentUser,
    domain: str | None = Query(None, description="Restrict the reset to one domain"),
    source: str | None = Query(None, description="Restrict the reset to one source"),
    layer: Literal["auto"] = Query(  # long but one parameter
        "auto", description="Reserved; retries run through the registry"
    ),
    db: AsyncSession = Depends(get_db),
) -> APIResponse:
    """Reset every failed shard so the next run of its window retries it.

    Args:
        current_user: Authenticated user.
        domain: Restrict to one domain.
        source: Restrict to one source.
        layer: Reserved for symmetry with the data API.
        db: Main-database session.

    Returns:
        How many shards were reset, and the affected windows (for a
        caller that wants to trigger those runs).
    """
    try:
        count = await mark_failed_for_retry(db, domain=domain, source=source)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return APIResponse(
        success=True,
        message="success",
        data={"reset": count},
    )
