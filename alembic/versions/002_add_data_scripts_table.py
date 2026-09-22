"""Add data_scripts table with is_custom field.

Revision ID: 002
Revises: 001
Create Date: 2026-02-23
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create data_scripts table."""

    # Create scriptfrequency enum type
    scriptfrequency_enum = sa.Enum(
        "hourly", "daily", "weekly", "monthly", "once", "manual",
        name="scriptfrequency"
    )
    scriptfrequency_enum.create(op.get_bind(), checkfirst=True)

    # Create data_scripts table
    op.create_table(
        "data_scripts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("script_id", sa.String(length=100), nullable=False),
        sa.Column("script_name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("sub_category", sa.String(length=50), nullable=True),
        sa.Column("frequency", sa.Enum("hourly", "daily", "weekly", "monthly", "once", "manual", name="scriptfrequency"), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("target_table", sa.String(length=100), nullable=True),
        sa.Column("module_path", sa.String(length=255), nullable=True),
        sa.Column("function_name", sa.String(length=100), nullable=True),
        sa.Column("dependencies", sa.JSON(), nullable=True),
        sa.Column("estimated_duration", sa.Integer(), nullable=False),
        sa.Column("timeout", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("script_id"),
    )
    op.create_index("ix_data_scripts_id", "data_scripts", ["id"])
    op.create_index("ix_data_scripts_script_id", "data_scripts", ["script_id"], unique=True)
    op.create_index("ix_data_scripts_category", "data_scripts", ["category"])
    op.create_index("ix_data_scripts_sub_category", "data_scripts", ["sub_category"])
    op.create_index("ix_data_scripts_target_table", "data_scripts", ["target_table"])
    op.create_index("ix_data_scripts_is_custom", "data_scripts", ["is_custom"])

    # Add foreign key constraint to scheduled_tasks if table exists
    # This requires scheduled_tasks.interface_id to reference data_scripts instead
    # For now, we'll skip this as it requires a larger schema change


def downgrade() -> None:
    """Drop data_scripts table."""

    op.drop_table("data_scripts")
    op.execute("DROP TYPE IF EXISTS scriptfrequency")
