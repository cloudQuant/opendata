"""Create the durable per-symbol Sina FZB fanout journal.

Revision ID: 0013_sina_fzb_fanout_groups
Revises: 0012_instrument_capture_scope
Create Date: 2026-10-27

The application contract freezes one manifest for each scheduled
job/occurrence slot and persists a lease-fenced enqueue cursor. The DDL does not
prevent principals with direct UPDATE access from changing a manifest and its
digest; deployments must restrict writes to the service account. ``ENQUEUED``
means every child run was accepted by the dispatcher; it does not mean any
child finished successfully. The table is intentionally specialized to
``sina-fzb-capture``. Group state and child-run enqueue transactions are
separate, so callers rely on the child ledger's deterministic idempotency when
replaying an interrupted chunk.
"""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    String,
    Text,
    select,
    table,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from alembic import op

revision = "0013_sina_fzb_fanout_groups"
down_revision = "0012_instrument_capture_scope"
branch_labels = None
depends_on = None

_TABLE = "od_sina_fzb_fanout_groups"


def _require_supported_database() -> None:
    """Require enforced CHECK constraints on supported MySQL installations."""
    dialect = op.get_bind().dialect
    if dialect.name == "mariadb" or getattr(dialect, "is_mariadb", False):
        raise RuntimeError("This migration supports MySQL 8.0.16+ but not MariaDB.")
    if dialect.name != "mysql":
        return
    version = dialect.server_version_info
    if version is None or len(version) < 3 or version[0] != 8 or version[:3] < (8, 0, 16):
        raise RuntimeError("This migration requires MySQL 8.0.16 or later within MySQL 8.")


def upgrade() -> None:
    """Create the application-frozen manifest journal and its claim indexes."""
    _require_supported_database()
    op.create_table(
        _TABLE,
        Column(
            "fanout_key",
            String(64).with_variant(String(64, collation="utf8mb4_0900_bin"), "mysql"),
            primary_key=True,
            nullable=False,
        ),
        Column(
            "job_id",
            String(128).with_variant(String(128, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=False,
        ),
        Column(
            "job_version",
            String(128).with_variant(String(128, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=False,
        ),
        Column("occurrence_at_us", BigInteger(), nullable=False),
        Column("manifest_sha256", String(64), nullable=False),
        Column("manifest_json", Text().with_variant(MEDIUMTEXT(), "mysql"), nullable=False),
        Column("member_count", BigInteger(), nullable=False),
        Column("next_index", BigInteger(), nullable=False),
        Column(
            "status",
            String(16).with_variant(String(16, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=False,
        ),
        Column(
            "lease_owner",
            String(255).with_variant(String(255, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=True,
        ),
        Column("fencing_token", BigInteger(), nullable=False),
        Column("lease_expires_at_us", BigInteger(), nullable=True),
        Column("attempt_count", BigInteger(), nullable=False),
        Column("next_attempt_at_us", BigInteger(), nullable=True),
        Column(
            "last_error_code",
            String(64).with_variant(String(64, collation="utf8mb4_0900_bin"), "mysql"),
            nullable=True,
        ),
        Column("created_at_us", BigInteger(), nullable=False),
        Column("updated_at_us", BigInteger(), nullable=False),
        Column("enqueued_at_us", BigInteger(), nullable=True),
        CheckConstraint(
            "job_id = 'sina-fzb-capture'",
            name="ck_od_sina_fzb_fanout_job_id",
        ),
        CheckConstraint(
            "status IN ('PENDING', 'ENQUEUED', 'BLOCKED')",
            name="ck_od_sina_fzb_fanout_status",
        ),
        CheckConstraint("member_count > 0", name="ck_od_sina_fzb_fanout_member_count"),
        CheckConstraint(
            "next_index >= 0 AND next_index <= member_count",
            name="ck_od_sina_fzb_fanout_cursor",
        ),
        CheckConstraint("fencing_token >= 0", name="ck_od_sina_fzb_fanout_fence"),
        CheckConstraint("attempt_count >= 0", name="ck_od_sina_fzb_fanout_attempts"),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_expires_at_us IS NULL) OR "
            "(lease_owner IS NOT NULL AND lease_expires_at_us IS NOT NULL)",
            name="ck_od_sina_fzb_fanout_lease_pair",
        ),
        CheckConstraint(
            "status != 'ENQUEUED' OR "
            "(next_index = member_count AND lease_owner IS NULL AND lease_expires_at_us IS NULL)",
            name="ck_od_sina_fzb_fanout_enqueued_complete",
        ),
        CheckConstraint(
            "status != 'PENDING' OR next_index < member_count",
            name="ck_od_sina_fzb_fanout_pending_incomplete",
        ),
        CheckConstraint(
            "status != 'BLOCKED' OR lease_owner IS NULL",
            name="ck_od_sina_fzb_fanout_blocked_unleased",
        ),
        CheckConstraint(
            "status != 'BLOCKED' OR last_error_code IS NOT NULL",
            name="ck_od_sina_fzb_fanout_blocked_error",
        ),
        CheckConstraint(
            "status != 'ENQUEUED' OR last_error_code IS NULL",
            name="ck_od_sina_fzb_fanout_enqueued_no_error",
        ),
        CheckConstraint("length(fanout_key) = 64", name="ck_od_sina_fzb_fanout_key_sha_len"),
        CheckConstraint("length(manifest_sha256) = 64", name="ck_od_sina_fzb_manifest_sha_len"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "uq_od_sina_fzb_fanout_slot",
        _TABLE,
        ["job_id", "occurrence_at_us"],
        unique=True,
    )
    op.create_index(
        "ix_od_sina_fzb_fanout_claim",
        _TABLE,
        [
            "status",
            "next_attempt_at_us",
            "lease_expires_at_us",
            "updated_at_us",
            "created_at_us",
        ],
    )


def downgrade() -> None:
    """Drop the journal only when no manifests or progress remain to archive."""
    _require_supported_database()
    groups = table(_TABLE)
    if op.get_bind().execute(select(1).select_from(groups).limit(1)).first() is not None:
        raise RuntimeError("Sina FZB fanout groups must be archived before schema downgrade.")
    op.drop_index("ix_od_sina_fzb_fanout_claim", table_name=_TABLE)
    op.drop_index("uq_od_sina_fzb_fanout_slot", table_name=_TABLE)
    op.drop_table(_TABLE)


__all__ = ["upgrade", "downgrade", "revision", "down_revision"]
