"""pipeline progress

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-23

Design §9.2: ``pipeline_progress`` is the shard-level resume
checkpoint for the six-step pipeline (internal bookkeeping), kept
separate from ``task_executions`` (the outward-facing run record).
One row per ``(pipeline_id, shard)``; the deterministic pipeline_id
(domain, source, window) makes a restart of the same window skip the
shards an earlier run completed.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the pipeline progress table."""
    op.create_table(
        "pipeline_progress",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("shard", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=False),
        sa.Column("window_end", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "DONE", "FAILED", name="shardstatus"),
            nullable=False,
        ),
        sa.Column("rows_written", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "shard", name="uq_pipeline_progress_shard"),
    )
    op.create_index(
        op.f("ix_pipeline_progress_pipeline_id"),
        "pipeline_progress",
        ["pipeline_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the pipeline progress table."""
    op.drop_index(op.f("ix_pipeline_progress_pipeline_id"), table_name="pipeline_progress")
    op.drop_table("pipeline_progress")
