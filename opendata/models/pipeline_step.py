"""Durable checkpoints for pipeline hooks and per-symbol fetch windows."""

from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Enum, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from opendata.core.database import Base


class PipelineStepStatus(str, enum.Enum):
    """Lifecycle of one post-ODS pipeline step."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class PipelineStepCheckpoint(Base):
    """Checkpoint for a cross-check, merge, or notification hook."""

    __tablename__ = "pipeline_step_checkpoints"
    __table_args__ = (  # type: ignore[assignment]  # SQLAlchemy accepts tuple table constraints
        UniqueConstraint("pipeline_id", "step", name="uq_pipeline_step_checkpoint"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    step: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[PipelineStepStatus] = mapped_column(
        Enum(
            PipelineStepStatus,
            name="pipeline_step_status",
            values_callable=lambda values: [value.value for value in values],
        ),
        default=PipelineStepStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PipelineSymbolWindow(Base):
    """Persist the bounded source/symbol windows of one pipeline run."""

    __tablename__ = "pipeline_symbol_windows"
    __table_args__ = (  # type: ignore[assignment]  # SQLAlchemy accepts tuple table constraints
        UniqueConstraint("pipeline_id", "source", "symbol", name="uq_pipeline_symbol_window"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    window_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(Date, nullable=True)
