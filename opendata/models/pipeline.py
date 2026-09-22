"""Pipeline progress models (design §9.2).

``task_executions`` is the outward-facing execution record; this
table is the internal shard-level checkpoint that lets a restarted
pipeline skip the shards it already completed. One row per
``(pipeline_id, shard)``: the deterministic ``pipeline_id`` (domain,
source and window) makes a re-run of the same window resume, and a
different window start a fresh bookkeeping series.
"""

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base


class ShardStatus(str, enum.Enum):
    """Lifecycle of one pipeline shard."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PipelineProgress(Base):
    """Shard-level checkpoint of one pipeline run."""

    __tablename__ = "pipeline_progress"
    # SQLAlchemy accepts the tuple form for table-level constraints; the
    # declarative base only annotates the dict (dialect kwargs) form.
    __table_args__ = (  # type: ignore[assignment]  # framework accepts the tuple form
        UniqueConstraint("pipeline_id", "shard", name="uq_pipeline_progress_shard"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    shard: Mapped[int] = mapped_column(Integer, nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[ShardStatus] = mapped_column(
        Enum(ShardStatus), default=ShardStatus.PENDING, nullable=False
    )
    rows_written: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )

    def __repr__(self) -> str:
        """Return a debug representation."""
        return (
            f"<PipelineProgress {self.pipeline_id} shard={self.shard} status={self.status.value}>"
        )
