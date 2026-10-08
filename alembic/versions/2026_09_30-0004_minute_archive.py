"""Index immutable local minute Parquet shards.

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the minute archive metadata index in the main database."""
    op.create_table(
        "minute_archive_shards",
        sa.Column("domain", sa.String(length=64), nullable=False),
        sa.Column("symbol", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column(
            "min_timestamp",
            sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column(
            "max_timestamp",
            sa.DateTime(timezone=True).with_variant(mysql.DATETIME(fsp=6), "mysql"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("domain", "symbol", "source", "period", "day"),
    )
    op.create_index(
        "ix_minute_archive_lookup",
        "minute_archive_shards",
        ["domain", "source", "period", "day", "symbol"],
    )


def downgrade() -> None:
    """Drop the minute archive index without touching Parquet files."""
    op.drop_index("ix_minute_archive_lookup", table_name="minute_archive_shards")
    op.drop_table("minute_archive_shards")
