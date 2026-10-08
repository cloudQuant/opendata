"""Persist pipeline hook checkpoints and per-symbol windows.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add durable post-ODS step status and source/symbol window rows."""
    op.create_table(
        "pipeline_step_checkpoints",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("step", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            sa.Enum("pending", "running", "done", "failed", name="pipeline_step_status"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "step", name="uq_pipeline_step_checkpoint"),
    )
    op.create_index(
        "ix_pipeline_step_checkpoints_pipeline_id",
        "pipeline_step_checkpoints",
        ["pipeline_id"],
    )
    op.create_table(
        "pipeline_symbol_windows",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("pipeline_id", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("symbol", sa.String(length=64), nullable=False),
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("pipeline_id", "source", "symbol", name="uq_pipeline_symbol_window"),
    )
    op.create_index(
        "ix_pipeline_symbol_windows_pipeline_id",
        "pipeline_symbol_windows",
        ["pipeline_id"],
    )


def downgrade() -> None:
    """Remove the additive pipeline checkpoint tables."""
    op.drop_index("ix_pipeline_symbol_windows_pipeline_id", table_name="pipeline_symbol_windows")
    op.drop_table("pipeline_symbol_windows")
    op.drop_index(
        "ix_pipeline_step_checkpoints_pipeline_id", table_name="pipeline_step_checkpoints"
    )
    op.drop_table("pipeline_step_checkpoints")
