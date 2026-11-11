"""Create the ported run-coordinator claim and scheduler-control tables.

Revision ID: 0011_run_coordinator_claims
Revises: 0010_afi_data_management_plane
Create Date: 2026-11-10

This revision materializes the two coordination tables declared by
``opendata.models.run_claim`` (ported from AFI's ``akshare_scheduler``
models): ``task_run_claims`` (one fenced claim per business period) and
``scheduler_control`` (the single source-rate gate row).

The DDL is fixed and reads no live metadata, so it renders offline. The
29k historical terminal claim rows in AFI are handed over by the stage-4
counted row copy; this revision only creates the empty OpenData tables.
Client-side Python defaults stay client-side: no server default is
declared. SQLite creates the table directly (no batch rebuild is needed
for a new table), and MySQL uses the control plane's utf8mb4 collation.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    Integer,
    String,
    Text,
)

from alembic import op

revision = "0011_run_coordinator_claims"
down_revision = "0010_afi_data_management_plane"
branch_labels = None
depends_on = None

_IndexSpec = tuple[tuple[str, tuple[str, ...], bool], ...]

_TASK_RUN_CLAIMS_INDEXES: _IndexSpec = (
    ("ix_task_run_claims_task_id", ("task_id",), False),
    ("ix_task_run_claims_script_id", ("script_id",), False),
    ("ix_task_run_claims_source", ("source",), False),
    ("ix_task_run_claims_status", ("status",), False),
    ("ix_task_run_claims_lease_expires_at", ("lease_expires_at",), False),
    ("ix_task_run_claims_next_retry_at", ("next_retry_at",), False),
    ("ix_task_run_claims_execution_id", ("execution_id",), False),
)


def _create_indexes(table_name: str, indexes: _IndexSpec) -> None:
    """Create a table's model-declared indexes."""
    for name, columns, unique in indexes:
        op.create_index(name, table_name, list(columns), unique=unique)


def _drop_indexes(table_name: str, indexes: _IndexSpec) -> None:
    """Drop a table's indexes in reverse declaration order."""
    for name, _columns, _unique in reversed(indexes):
        op.drop_index(name, table_name=table_name)


def _create_task_run_claims() -> None:
    """Create the task_run_claims table and its indexes."""
    op.create_table(
        "task_run_claims",
        Column("claim_key", String(64), primary_key=True, nullable=False),
        Column("task_id", Integer(), nullable=True),
        Column("script_id", String(100), nullable=False),
        Column("source", String(50), nullable=False),
        Column("period_key", String(255), nullable=False),
        Column("parameter_hash", String(64), nullable=False),
        Column("parameters", JSON(), nullable=False),
        Column("status", String(20), nullable=False),
        Column("owner_id", String(64), nullable=True),
        Column("fencing_token", BigInteger(), nullable=False),
        Column("lease_expires_at", DateTime(), nullable=True),
        Column("attempt_count", Integer(), nullable=False),
        Column("next_retry_at", DateTime(), nullable=True),
        Column("execution_id", String(100), nullable=True),
        Column("last_error", Text(), nullable=True),
        Column("completed_at", DateTime(), nullable=True),
        Column("created_at", DateTime(), nullable=False),
        Column("updated_at", DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )
    _create_indexes("task_run_claims", _TASK_RUN_CLAIMS_INDEXES)


def _create_scheduler_control() -> None:
    """Create the single-row scheduler_control table."""
    op.create_table(
        "scheduler_control",
        Column("control_id", Integer(), primary_key=True, nullable=False),
        Column("source_next_allowed_at", JSON(), nullable=False),
        Column("updated_at", DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
    )


def upgrade() -> None:
    """Create the two run-coordinator tables."""
    _create_task_run_claims()
    _create_scheduler_control()


def downgrade() -> None:
    """Drop the run-coordinator tables in reverse dependency order."""
    op.drop_table("scheduler_control")
    _drop_indexes("task_run_claims", _TASK_RUN_CLAIMS_INDEXES)
    op.drop_table("task_run_claims")


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
