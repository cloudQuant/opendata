"""ods tables for the fuyao (THS) source.

Revision ID: 0003_ods_ths
Revises: 0002_dq_diff_report
Create Date: 2026-09-23

A3.3b: the market-dumps channel lands the fuyao source-raw frames in the
warehouse, so the ods layer needs its own tables - ``ods_<domain>_<source>``
for the two domains the dumps carry.

Two derived helper columns are part of the reviewed contract, because the
source carries epoch milliseconds and the ods layer needs a date for
partitioning and a stable idempotency key for events:

* daily K: ``trade_date`` (derived from ``date_ms``, Shanghai trading day)
  drives the yearly partition; the row keeps ``date_ms`` as the raw value.
* adjustment factors: ``ex_date`` (derived from ``ex_date_ms``) plus
  ``event_key``, a digest over the event payload, because the dump contains
  rows that share ``(thscode, ex_date_ms)`` with different event values (and,
  for one symbol, an exact duplicate row). Keying on the digest collapses the
  duplicate on re-import while keeping distinct events.
"""

from collections.abc import Sequence

from alembic import op
from opendata.pipeline.ddl import Column, ods_table_ddl

# revision identifiers, used by Alembic.
revision: str = "0003_ods_ths"
down_revision: str | None = "0002_dq_diff_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: 日 K dump 的源原始列 + ``trade_date`` 派生分区列。
_DAILY_K_COLUMNS = [
    Column("thscode", "varchar(32)", nullable=False),
    Column("trade_date", "date", nullable=False),
    Column("date_ms", "bigint", nullable=False),
    Column("currency", "varchar(8)"),
    Column("interval", "varchar(8)"),
    Column("adjusted", "varchar(16)"),
    Column("open_price", "double"),
    Column("high_price", "double"),
    Column("low_price", "double"),
    Column("close_price", "double"),
    Column("volume", "double"),
    Column("turnover", "double"),
]

#: 复权因子 dump 的源原始列 + 派生幂等键。
_ADJUSTMENT_COLUMNS = [
    Column("thscode", "varchar(32)", nullable=False),
    Column("ticker", "varchar(16)"),
    Column("ex_date", "date", nullable=False),
    Column("ex_date_ms", "bigint", nullable=False),
    Column("event_key", "char(32)", nullable=False),
    Column("dividend_per_share", "double"),
    Column("per_share_bonus", "double"),
    Column("allotment_ratio", "double"),
    Column("allotment_price", "double"),
    Column("currency", "varchar(8)"),
]

# 日 K：10 年 dump，分区从 2015 起预留 12 年（分区维护任务负责扩上界）。
_DAILY_START_YEAR = 2015
_DAILY_YEARS = 12

# 复权因子：全量含 1991 起的早期事件，预留到 2026。
_ADJUSTMENT_START_YEAR = 1991
_ADJUSTMENT_YEARS = 36


def upgrade() -> None:
    """Create the fuyao ods tables."""
    op.execute(
        ods_table_ddl(
            "stock_daily",
            "ths",
            _DAILY_K_COLUMNS,
            key=("thscode", "trade_date"),
            partition_key="trade_date",
            start_year=_DAILY_START_YEAR,
            years=_DAILY_YEARS,
        )
    )
    op.execute(
        ods_table_ddl(
            "stock_action",
            "ths",
            _ADJUSTMENT_COLUMNS,
            key=("thscode", "ex_date", "event_key"),
            partition_key="ex_date",
            start_year=_ADJUSTMENT_START_YEAR,
            years=_ADJUSTMENT_YEARS,
        )
    )


def downgrade() -> None:
    """Drop the fuyao ods tables."""
    op.execute("DROP TABLE IF EXISTS `ods_stock_action_ths`")
    op.execute("DROP TABLE IF EXISTS `ods_stock_daily_ths`")
