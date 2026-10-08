"""Create DWD tables for index, futures, and option daily bars.

Revision ID: 0006_dwd_daily_legs
Revises: 0005_dwd_stock_adjust
Create Date: 2026-09-30

These single-source daily domains already have registered providers and
source mappings. Their DWD tables use the Bar contract and the same
``(symbol, trade_date)`` business key and yearly partition shape as
``dwd_stock_daily``.
"""

from alembic import op
from opendata.pipeline.ddl import dwd_table_ddl

revision = "0006_dwd_daily_legs"
down_revision = "0005_dwd_stock_adjust"
branch_labels = None
depends_on = None

_DAILY_DOMAINS = ("index_daily", "futures_daily", "option_daily")
_BUSINESS_KEY = ("symbol", "trade_date")


def upgrade() -> None:
    """Create the missing DWD tables from their contract definitions."""
    for domain in _DAILY_DOMAINS:
        op.execute(
            dwd_table_ddl(
                domain,
                key=_BUSINESS_KEY,
                partition_key="trade_date",
            )
        )


def downgrade() -> None:
    """Drop only the DWD tables introduced by this revision."""
    for domain in reversed(_DAILY_DOMAINS):
        op.execute(f"DROP TABLE IF EXISTS `dwd_{domain}`")
