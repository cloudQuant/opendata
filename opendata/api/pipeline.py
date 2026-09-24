"""Pipeline operations API (B3.2 / AC-13).

Two surfaces:

* the failed-shard list and one-click retry - read the checkpoints an
  earlier run left failed, and reset them to ``pending`` so the next run
  of the same window retries them (the runner skips only ``done``
  shards). Retrying a window is idempotent - resetting a shard that
  succeeded does nothing because the filter is ``status == failed``;
* the manual run trigger - start an incremental batch on demand (the
  fallback 验收文档 §0-4 allows before the cron has fired) and read its
  outcome back by run id. The batch runs as a background task: a full
  universe takes minutes, which an HTTP request must not hold open.
"""

from __future__ import annotations

import asyncio
from datetime import date  # noqa: TC003  # FastAPI reads the annotation at runtime
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status

from opendata.api.dependencies import (
    CurrentUser,  # noqa: TC001  # FastAPI resolves the Annotated alias at runtime
)
from opendata.api.schemas import APIResponse
from opendata.core.database import get_db
from opendata.pipeline.jobs import SUPPORTED_DOMAINS, run_incremental_job
from opendata.pipeline.patrol import key_status, patrol
from opendata.pipeline.retry import list_failed_shards, mark_failed_for_retry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()

#: Manual runs kept in memory (the database checkpoints carry the rest).
_RUNS: dict[str, dict[str, Any]] = {}
#: The background task of each manual run, kept so it is never GC'd mid-flight.
_TASKS: dict[str, asyncio.Task[None]] = {}
#: Keep only the newest N manual runs; this is a trigger, not a history.
_RUNS_KEPT = 20


def _remember(run_id: str, entry: dict[str, Any]) -> None:
    """Store one run entry, dropping the oldest beyond the kept window."""
    _RUNS[run_id] = entry
    for stale in sorted(_RUNS)[:-_RUNS_KEPT]:
        _RUNS.pop(stale, None)
        _TASKS.pop(stale, None)


async def _execute_run(run_id: str, **kwargs: Any) -> None:  # noqa: ANN401 - job parameters
    """Run the batch in the background and record how it ended."""
    entry = _RUNS[run_id]
    try:
        result = await run_incremental_job(**kwargs)
    except Exception as exc:  # reported through the run status, never swallowed
        entry["status"] = "failed"
        entry["error"] = f"{type(exc).__name__}: {exc}"
    else:
        entry["status"] = "succeeded" if result.failures == 0 else "partial"
        entry["result"] = result.as_dict()
        entry["error"] = None


@router.post("/pipeline/run", status_code=status.HTTP_202_ACCEPTED)
async def pipeline_run(
    current_user: CurrentUser,
    domain: str = Query("stock_daily", description="Domain to run"),
    source: str | None = Query(None, description="Authoritative source; defaults to the mapping"),
    second_source: str | None = Query(None, description="Second source; wires cross-check"),
    symbols: list[str] | None = Query(None, description="Explicit universe (repeatable)"),
    limit: int = Query(200, ge=1, le=5000, description="Cap of the derived universe"),
    as_of: date | None = Query(None, description="Window end date; defaults to today"),
    lookback_days: int = Query(0, ge=0, le=30, description="Days to prepend to the window"),
    shard_size: int = Query(50, ge=1, le=500, description="Symbols per shard"),
) -> APIResponse:
    """Start one incremental batch and return immediately with its run id.

    Args:
        current_user: Authenticated user.
        domain: Domain identifier to run.
        source: Authoritative source label.
        second_source: Second source label (cross-check plus dual merge).
        symbols: Explicit symbol universe; defaults to the warehouse list.
        limit: Cap applied to the derived universe.
        as_of: Window end date.
        lookback_days: Days to re-fetch before ``as_of``.
        shard_size: Symbols per shard.

    Returns:
        The run id to poll, plus the echoed request.

    Raises:
        HTTPException: 400 when the domain has no pipeline builder.
    """
    if domain not in SUPPORTED_DOMAINS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"no pipeline builder for domain {domain!r}; "
            f"supported: {sorted(SUPPORTED_DOMAINS)}",
        )
    run_id = uuid4().hex[:12]
    kwargs: dict[str, Any] = {
        "domain": domain,
        "source": source,
        "second_source": second_source,
        "symbols": symbols,
        "limit": limit,
        "as_of": as_of,
        "lookback_days": lookback_days,
        "shard_size": shard_size,
    }
    _remember(run_id, {"run_id": run_id, "status": "running", "request": kwargs})
    # The task reference is kept so the event loop never collects a
    # still-running batch, and so a test/caller can await it.
    _TASKS[run_id] = asyncio.create_task(_execute_run(run_id, **kwargs))
    return APIResponse(
        success=True,
        message="accepted",
        data={"run_id": run_id, "status": "running", "request": kwargs},
    )


@router.get("/pipeline/run/{run_id}")
async def pipeline_run_status(
    current_user: CurrentUser,
    run_id: str,
) -> APIResponse:
    """Read back one manual run (``running`` / ``succeeded`` / ``partial`` / ``failed``).

    Args:
        current_user: Authenticated user.
        run_id: The id returned by the trigger.

    Returns:
        The run entry, including its counters once it finished.

    Raises:
        HTTPException: 404 for a run id this process never started.
    """
    entry = _RUNS.get(run_id)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"unknown run_id {run_id}"
        )
    return APIResponse(success=True, message="success", data=entry)


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
