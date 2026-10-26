"""Add the isolated declarative job-definition metadata table.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the immutable job-definition metadata table and identity index."""
    op.create_table(
        "od_job_definitions",
        sa.Column(
            "legacy_task_id",
            sa.String(length=255).with_variant(
                sa.String(length=255, collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column(
            "job_id",
            sa.String(length=128).with_variant(
                sa.String(length=128, collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.String(length=128).with_variant(
                sa.String(length=128, collation="utf8mb4_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column(
            "definition_json",
            sa.Text().with_variant(
                mysql.MEDIUMTEXT(),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("identity_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "provenance_json",
            sa.Text().with_variant(
                mysql.MEDIUMTEXT(),
                "mysql",
            ),
            nullable=False,
        ),
        sa.Column("provenance_sha256", sa.String(length=64), nullable=False),
        sa.Column("receipt_sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint(
            "length(identity_sha256) = 64",
            name="ck_od_job_def_identity_sha_len",
        ),
        sa.CheckConstraint(
            "length(provenance_sha256) = 64",
            name="ck_od_job_def_provenance_sha_len",
        ),
        sa.CheckConstraint(
            "length(receipt_sha256) = 64",
            name="ck_od_job_def_receipt_sha_len",
        ),
        sa.PrimaryKeyConstraint("legacy_task_id"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ux_od_job_definitions_job_version",
        "od_job_definitions",
        ["job_id", "version"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the job-definition identity index and metadata table."""
    op.drop_index(
        "ux_od_job_definitions_job_version",
        table_name="od_job_definitions",
    )
    op.drop_table("od_job_definitions")
