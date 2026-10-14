"""Add immutable financial PIT versions, coverage declarations, and conflicts.

Revision ID: 0008_financial_pit
Revises: 0007_affine_stock_adjust
Create Date: 2026-10-12

The existing latest-value DWD tables are unchanged.  PIT storage is additive;
all timestamps are stored as UTC-naive SQL DATETIME values and tagged source
publication precision/timezone remain explicit columns.
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_financial_pit"
down_revision = "0007_affine_stock_adjust"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the additive PIT tables without migrating or rewriting rows."""
    op.create_table(
        "financial_pit_versions",
        sa.Column("version_id", sa.String(64), primary_key=True),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(128), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("announce_date", sa.Date(), nullable=False),
        sa.Column("statement_type", sa.String(32), nullable=True),
        sa.Column("item", sa.String(128), nullable=True),
        sa.Column("indicator", sa.String(128), nullable=True),
        sa.Column("source_revision", sa.String(255), nullable=False),
        sa.Column("revision_order", sa.Integer(), nullable=False),
        sa.Column("public_time_precision", sa.String(16), nullable=False),
        sa.Column("published_at_utc", sa.DateTime(), nullable=True),
        sa.Column("published_on", sa.Date(), nullable=True),
        sa.Column("visible_at_utc", sa.DateTime(), nullable=True),
        sa.Column("source_timezone", sa.String(64), nullable=False),
        sa.Column("observed_at_utc", sa.DateTime(), nullable=False),
        sa.Column("provenance", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_financial_pit_versions_scope",
        "financial_pit_versions",
        ["domain", "source", "market", "symbol", "report_period"],
    )
    op.create_table(
        "financial_pit_coverage",
        sa.Column("coverage_id", sa.String(64), primary_key=True),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(128), nullable=False),
        sa.Column("report_period_start", sa.Date(), nullable=False),
        sa.Column("report_period_end", sa.Date(), nullable=False),
        sa.Column("public_history_from_utc", sa.DateTime(), nullable=False),
        sa.Column("complete_through_utc", sa.DateTime(), nullable=False),
        sa.Column("source_timezone", sa.String(64), nullable=False),
        sa.Column("evidence_uri", sa.Text(), nullable=False),
        sa.Column("evidence_digest", sa.String(64), nullable=False),
    )
    op.create_index(
        "ix_financial_pit_coverage_scope",
        "financial_pit_coverage",
        ["domain", "source", "market", "symbol", "report_period_start", "report_period_end"],
    )
    op.create_table(
        "financial_pit_conflicts",
        sa.Column("conflict_id", sa.String(64), primary_key=True),
        sa.Column("version_id", sa.String(64), nullable=False),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(128), nullable=False),
        sa.Column("report_period", sa.Date(), nullable=False),
        sa.Column("statement_type", sa.String(32), nullable=True),
        sa.Column("item", sa.String(128), nullable=True),
        sa.Column("indicator", sa.String(128), nullable=True),
        sa.Column("source_revision", sa.String(255), nullable=False),
        sa.Column("stored_digest", sa.String(64), nullable=False),
        sa.Column("incoming_digest", sa.String(64), nullable=False),
        sa.Column("stored_payload_json", sa.Text(), nullable=False),
        sa.Column("incoming_payload_json", sa.Text(), nullable=False),
        sa.Column("stored_provenance", sa.Text(), nullable=False),
        sa.Column("incoming_provenance", sa.Text(), nullable=False),
        sa.Column("detected_at_utc", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_financial_pit_conflicts_scope",
        "financial_pit_conflicts",
        ["domain", "source", "market", "symbol", "report_period"],
    )


def downgrade() -> None:
    """Drop only the additive PIT tables introduced by this revision."""
    op.drop_index("ix_financial_pit_conflicts_scope", table_name="financial_pit_conflicts")
    op.drop_table("financial_pit_conflicts")
    op.drop_index("ix_financial_pit_coverage_scope", table_name="financial_pit_coverage")
    op.drop_table("financial_pit_coverage")
    op.drop_index("ix_financial_pit_versions_scope", table_name="financial_pit_versions")
    op.drop_table("financial_pit_versions")
