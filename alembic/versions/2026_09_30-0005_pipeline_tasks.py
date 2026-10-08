"""Add the built-in pipeline executor to scheduled tasks.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow a scheduled task to use either a script or pipeline executor."""
    op.add_column(
        "scheduled_tasks",
        sa.Column("task_kind", sa.String(length=16), server_default="script", nullable=False),
    )
    op.alter_column(
        "scheduled_tasks",
        "script_id",
        existing_type=sa.String(length=100),
        nullable=True,
    )
    op.alter_column(
        "task_executions",
        "script_id",
        existing_type=sa.String(length=100),
        nullable=True,
    )


def downgrade() -> None:
    """Return to script-only tasks when no pipeline records remain."""
    connection = op.get_bind()
    pipeline_tasks = connection.scalar(
        sa.text("SELECT COUNT(*) FROM scheduled_tasks WHERE task_kind = 'pipeline'")
    )
    pipeline_executions = connection.scalar(
        sa.text("SELECT COUNT(*) FROM task_executions WHERE script_id IS NULL")
    )
    if pipeline_tasks or pipeline_executions:
        raise RuntimeError("remove pipeline tasks and their executions before downgrading 0005")

    op.alter_column(
        "task_executions",
        "script_id",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.alter_column(
        "scheduled_tasks",
        "script_id",
        existing_type=sa.String(length=100),
        nullable=False,
    )
    op.drop_column("scheduled_tasks", "task_kind")
