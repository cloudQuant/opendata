"""Add immutable corporate-event versions, conflicts, and unresolved evidence.

Revision ID: 0009_corporate_event_versions
Revises: 0008_financial_pit
Create Date: 2026-10-12

Payload digests identify content only. The event identity and revision are
separate key fields, and unresolved source rows are retained as observations
until an event identity is reviewed.
"""

from sqlalchemy import Column, Date, DateTime, Double, Integer, String, Text

from alembic import op

revision = "0009_corporate_event_versions"
down_revision = "0008_financial_pit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create append-only event versions and their explicit conflict records."""
    op.create_table(
        "dwd_corporate_event_versions",
        Column("source", String(64), primary_key=True, nullable=False),
        Column("event_identity", String(191), primary_key=True, nullable=False),
        Column("revision_order", Integer(), primary_key=True, nullable=False),
        Column("content_digest", String(64), primary_key=True, nullable=False),
        Column("identity_basis", String(32), nullable=False),
        Column("economic_event_id", String(191), nullable=True),
        Column("symbol", String(64), nullable=False),
        Column("ex_date", Date(), nullable=False),
        Column("source_revision", String(128), nullable=False),
        Column("pre_event_share_basis", String(191), nullable=True),
        Column("cash_dividend", Double(), nullable=False),
        Column("stock_dividend", Double(), nullable=False),
        Column("rights_shares", Double(), nullable=False),
        Column("rights_price", Double(), nullable=False),
        Column("published_date", Date(), nullable=True),
        Column("published_timestamp", DateTime(timezone=False), nullable=True),
        Column("publication_precision", String(16), nullable=False),
        Column("source_timezone", String(64), nullable=True),
        Column("observed_at", DateTime(timezone=False), nullable=False),
    )
    op.create_index(
        "ix_corporate_event_versions_symbol_date",
        "dwd_corporate_event_versions",
        ["symbol", "ex_date"],
    )
    op.create_table(
        "dwd_corporate_event_conflicts",
        Column("source", String(64), primary_key=True, nullable=False),
        Column("event_identity", String(191), primary_key=True, nullable=False),
        Column("revision_order", Integer(), primary_key=True, nullable=False),
        Column("digest_a", String(64), primary_key=True, nullable=False),
        Column("digest_b", String(64), primary_key=True, nullable=False),
        Column("detected_at", DateTime(timezone=False), nullable=False),
    )
    op.create_table(
        "dwd_corporate_event_candidates",
        Column("observation_id", String(36), primary_key=True, nullable=False),
        Column("source", String(64), nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("raw_payload_json", Text(), nullable=False),
        Column("symbol", String(64), nullable=True),
        Column("ex_date", Date(), nullable=True),
        Column("observed_at", DateTime(timezone=False), nullable=False),
        Column("identity_status", String(32), nullable=False),
    )
    op.create_index(
        "ix_corporate_event_candidates_source_digest",
        "dwd_corporate_event_candidates",
        ["source", "content_digest"],
    )
    op.create_table(
        "dwd_corporate_event_recompute_outbox",
        Column("job_id", String(36), primary_key=True, nullable=False),
        Column("batch_id", String(36), nullable=False, unique=True),
        Column("source", String(64), nullable=False),
        Column("event_identity", String(191), nullable=False),
        Column("revision_order", Integer(), nullable=False),
        Column("content_digest", String(64), nullable=False),
        Column("symbol", String(64), nullable=False),
        Column("ex_date", Date(), nullable=False),
        Column("status", String(16), nullable=False),
        Column("attempt_count", Integer(), nullable=False),
        Column("available_at", DateTime(timezone=False), nullable=False),
        Column("created_at", DateTime(timezone=False), nullable=False),
        Column("lease_token", String(36), nullable=True),
        Column("lease_until", DateTime(timezone=False), nullable=True),
        Column("last_error", Text(), nullable=True),
        Column("completed_at", DateTime(timezone=False), nullable=True),
        Column("factor_row_count", Integer(), nullable=True),
        Column("factor_window_start", Date(), nullable=True),
        Column("factor_window_end", Date(), nullable=True),
    )
    op.create_index(
        "ix_corporate_event_outbox_status_available",
        "dwd_corporate_event_recompute_outbox",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_corporate_event_outbox_symbol_status",
        "dwd_corporate_event_recompute_outbox",
        ["symbol", "status"],
    )


def downgrade() -> None:
    """Drop only the event tables introduced by this revision."""
    op.drop_index(
        "ix_corporate_event_outbox_symbol_status",
        table_name="dwd_corporate_event_recompute_outbox",
    )
    op.drop_index(
        "ix_corporate_event_outbox_status_available",
        table_name="dwd_corporate_event_recompute_outbox",
    )
    op.drop_table("dwd_corporate_event_recompute_outbox")
    op.drop_index(
        "ix_corporate_event_candidates_source_digest",
        table_name="dwd_corporate_event_candidates",
    )
    op.drop_table("dwd_corporate_event_candidates")
    op.drop_table("dwd_corporate_event_conflicts")
    op.drop_index(
        "ix_corporate_event_versions_symbol_date",
        table_name="dwd_corporate_event_versions",
    )
    op.drop_table("dwd_corporate_event_versions")
