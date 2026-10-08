"""dwd table for the stock-adjust domain (AC-11 adjust, design D10).

Revision ID: 0005_dwd_stock_adjust
Revises: 0004_batch_watermark
Create Date: 2026-09-24

``adjust=qfq|hfq`` is synthesized at query time from ``Bar x
AdjustFactor``; the factor rows live here, one per (symbol, trade_date),
with the cumulative qfq/hfq factors cumulated from the corporate-action
events (``opendata/pipeline/factors.py``). The table is keyed by the
business key and partitioned by trade_date like every dwd table, and it
carries the dwd lineage columns so merges and queries treat it like the
rest of the layer.
"""

from alembic import op
from opendata.pipeline.ddl import DWD_TRACE_COLUMNS, Column, _table_ddl

revision = "0005_dwd_stock_adjust"
down_revision = "0004_batch_watermark"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the original multiplication-only stock-adjust table.

    Keep this revision's column set stable for databases that have already
    applied it; affine fields arrive in the additive 0007 revision.
    """
    legacy_columns = [
        Column("symbol", "varchar(64)", nullable=False),
        Column("trade_date", "date", nullable=False),
        Column("qfq_factor", "double", nullable=False),
        Column("hfq_factor", "double", nullable=False),
        *DWD_TRACE_COLUMNS,
    ]
    op.execute(
        _table_ddl(
            "dwd_stock_adjust",
            legacy_columns,
            key=("symbol", "trade_date"),
            partition_key="trade_date",
            start_year=None,
            years=3,
        )
    )


def downgrade() -> None:
    """Drop the stock-adjust dwd table."""
    op.execute("DROP TABLE IF EXISTS `dwd_stock_adjust`")
