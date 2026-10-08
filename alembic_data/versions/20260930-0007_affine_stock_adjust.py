"""Add affine adjustment coefficients to stock-adjust rows.

Revision ID: 0007_affine_stock_adjust
Revises: 0006_dwd_daily_legs
Create Date: 2026-09-30

Existing qfq/hfq multiplication factors remain intact and continue to support
legacy consumers. Six nullable fields carry affine coefficients and lineage.
"""

from sqlalchemy import Column, Double, String

from alembic import op

revision = "0007_affine_stock_adjust"
down_revision = "0006_dwd_daily_legs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable affine and legacy-lineage columns without rewriting rows."""
    op.add_column("dwd_stock_adjust", Column("qfq_scale", Double(), nullable=True))
    op.add_column("dwd_stock_adjust", Column("qfq_offset", Double(), nullable=True))
    op.add_column("dwd_stock_adjust", Column("hfq_scale", Double(), nullable=True))
    op.add_column("dwd_stock_adjust", Column("hfq_offset", Double(), nullable=True))
    op.add_column("dwd_stock_adjust", Column("adjustment_version", String(255), nullable=True))
    op.add_column("dwd_stock_adjust", Column("legacy_source", String(255), nullable=True))


def downgrade() -> None:
    """Remove only columns introduced by this revision."""
    op.drop_column("dwd_stock_adjust", "legacy_source")
    op.drop_column("dwd_stock_adjust", "adjustment_version")
    op.drop_column("dwd_stock_adjust", "hfq_offset")
    op.drop_column("dwd_stock_adjust", "hfq_scale")
    op.drop_column("dwd_stock_adjust", "qfq_offset")
    op.drop_column("dwd_stock_adjust", "qfq_scale")
