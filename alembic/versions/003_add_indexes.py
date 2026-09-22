"""Add composite indexes for frequent queries

Revision ID: 003
Revises: 002
Create Date: 2025-02-25
"""

from alembic import op

# revision identifiers
revision = "003"
down_revision = "002_add_data_scripts_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ScheduledTask: user_id + is_active (used in list_tasks)
    op.create_index(
        "ix_scheduled_tasks_user_active",
        "scheduled_tasks",
        ["user_id", "is_active"],
        if_not_exists=True,
    )

    # TaskExecution: task_id + status (used in cancel, running queries)
    op.create_index(
        "ix_task_executions_task_status",
        "task_executions",
        ["task_id", "status"],
        if_not_exists=True,
    )

    # TaskExecution: start_time (used in ORDER BY, date range filters)
    op.create_index(
        "ix_task_executions_start_time",
        "task_executions",
        ["start_time"],
        if_not_exists=True,
    )

    # TaskExecution: script_id (used in execution queries by script)
    op.create_index(
        "ix_task_executions_script_id",
        "task_executions",
        ["script_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_task_executions_script_id", table_name="task_executions")
    op.drop_index("ix_task_executions_start_time", table_name="task_executions")
    op.drop_index("ix_task_executions_task_status", table_name="task_executions")
    op.drop_index("ix_scheduled_tasks_user_active", table_name="scheduled_tasks")
