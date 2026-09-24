"""Failed-pipeline retry (B3.2 / AC-13 "失败清单一键重试").

A pipeline run checkpoints every shard in ``pipeline_progress``; a
failed shard stays ``failed`` with its error text. Re-running the same
deterministic ``pipeline_id`` (domain:source:window) skips the ``done``
shards and re-processes everything else, so the retry primitive is:
*list* the failed shards, then *reset* them to ``pending`` so the next
run of that window picks them up. The module owns those two queries;
the HTTP surface and the scheduler decide when a run happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select, update

from opendata.models.pipeline import PipelineProgress, ShardStatus

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class FailedShard:
    """One failed shard of a pipeline run.

    Attributes:
        pipeline_id: Deterministic run id (domain:source:window).
        domain: Domain identifier.
        source: Source identifier.
        shard: Shard index.
        window_start: Window start.
        window_end: Window end.
        error: Failure description recorded at the checkpoint.
    """

    pipeline_id: str
    domain: str
    source: str
    shard: int
    window_start: date
    window_end: date
    error: str | None


async def list_failed_shards(db: AsyncSession) -> Sequence[FailedShard]:
    """List every shard an earlier run left failed.

    Args:
        db: Main-database session (progress rows live there).

    Returns:
        The failed shards, newest first by update time.
    """
    rows = (
        (
            await db.execute(
                select(PipelineProgress)
                .where(PipelineProgress.status == ShardStatus.FAILED)
                .order_by(PipelineProgress.updated_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_to_failed(row) for row in rows]


async def mark_failed_for_retry(
    db: AsyncSession,
    *,
    domain: str | None = None,
    source: str | None = None,
    window: tuple[date, date] | None = None,
) -> int:
    """Reset failed shards to pending so the next run retries them.

    Args:
        db: Main-database session.
        domain: Restrict to one domain (None resets every domain).
        source: Restrict to one source.
        window: Restrict to one ``(start, end)`` window.

    Returns:
        Number of shards reset.

    Raises:
        ValueError: A window is partially specified.
    """
    if window is not None and (window[0] is None or window[1] is None):
        raise ValueError("window must be a (start, end) pair")
    statement = update(PipelineProgress).where(PipelineProgress.status == ShardStatus.FAILED)
    if domain is not None:
        statement = statement.where(PipelineProgress.domain == domain)
    if source is not None:
        statement = statement.where(PipelineProgress.source == source)
    if window is not None:
        statement = statement.where(
            PipelineProgress.window_start == window[0],
            PipelineProgress.window_end == window[1],
        )
    result = await db.execute(statement.values(status=ShardStatus.PENDING, error=None))
    await db.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]  # CursorResult at runtime


def _to_failed(row: PipelineProgress) -> FailedShard:
    """Project one progress row onto the failure record.

    Args:
        row: The progress row.

    Returns:
        The failure record.
    """
    return FailedShard(
        pipeline_id=row.pipeline_id,
        domain=row.domain,
        source=row.source,
        shard=row.shard,
        window_start=row.window_start,
        window_end=row.window_end,
        error=row.error,
    )
