"""Create the fenced pipeline run ledger in the warehouse database.

Revision ID: 0010_run_ledger
Revises: 0009_corporate_event_versions
Create Date: 2026-10-26

This DDL is intentionally literal and independent of the runtime SQLAlchemy
table declaration so the historical migration remains stable if that model
changes later.
"""

from sqlalchemy import BigInteger, CheckConstraint, Column, String, Text

from alembic import op

revision = "0010_run_ledger"
down_revision = "0009_corporate_event_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the warehouse-local run ledger and its claim-order index."""
    op.create_table(
        "od_run_ledger",
        Column("run_key", String(255), primary_key=True, nullable=False),
        Column("job_snapshot_json", Text(), nullable=False),
        Column("status", String(16), nullable=False),
        Column("worker", String(255), nullable=True),
        Column("lease_id", String(32), nullable=True),
        Column("fencing_token", BigInteger(), nullable=False),
        Column("lease_expires_at_us", BigInteger(), nullable=True),
        Column("created_at_us", BigInteger(), nullable=False),
        Column("updated_at_us", BigInteger(), nullable=False),
        Column("started_at_us", BigInteger(), nullable=True),
        Column("heartbeat_at_us", BigInteger(), nullable=True),
        Column("finished_at_us", BigInteger(), nullable=True),
        Column("error_text", Text(), nullable=True),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_od_run_ledger_status",
        ),
        CheckConstraint("fencing_token >= 0", name="ck_od_run_ledger_fencing_token"),
    )
    op.create_index(
        "ix_od_run_ledger_claim",
        "od_run_ledger",
        ["status", "lease_expires_at_us", "created_at_us"],
    )


def downgrade() -> None:
    """Drop only the run-ledger table introduced by this revision."""
    op.drop_index("ix_od_run_ledger_claim", table_name="od_run_ledger")
    op.drop_table("od_run_ledger")


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
