"""A4.1: P0 warehouse tables (dwd x5, ods stock daily x1).

Rendered from ``opendata.pipeline.ddl`` so the migration and the
generator can never drift. Scope is the P0 daily-chain closure of
A2.1:

* ``dwd_<domain>`` for the five P0 domains, derived from the contract
  models (design §8.3);
* ``ods_stock_daily_akshare`` with the source-raw eastmoney kline
  columns (Chinese naming preserved, design §8.1) as the ods
  exemplar: partitioned by ``日期`` with the business key
  ``(股票代码, 日期)``.

The remaining ods tables land with their domain field mappings
(A4.5/A4.7); partitions start at 2024 with three yearly partitions
plus the ``pmax`` fallback that the maintenance task (A4.3)
reorganizes.

Revision ID: 0001_ods_dwd_p0
Revises:
Create Date: 2026-09-23
"""

from alembic import op
from opendata.pipeline.ddl import Column, dwd_table_ddl, ods_table_ddl

revision = "0001_ods_dwd_p0"
down_revision = None
branch_labels = None
depends_on = None

#: (business key, partition key) per P0 domain.
_DWD_TABLES = {
    "stock_daily": (("symbol", "trade_date"), "trade_date"),
    "stock_action": (("symbol", "ex_date"), "ex_date"),
    "financial_statement": (
        ("symbol", "statement_type", "report_period", "item"),
        "report_period",
    ),
    "financial_indicator": (("symbol", "report_period", "indicator"), "report_period"),
    "index_constituent": (("index_symbol", "symbol", "as_of"), "as_of"),
}

#: Source-raw columns of the eastmoney daily kline frame
#: (``stock_zh_a_hist``); the writer injects ``_source``/``_fetched_at``/
#: ``_batch_id`` from ODS_METADATA_COLUMNS.
_STOCK_DAILY_ODS_COLUMNS = [
    Column("日期", "date", nullable=False),
    Column("股票代码", "varchar(64)", nullable=False),
    Column("开盘", "double", nullable=False),
    Column("收盘", "double", nullable=False),
    Column("最高", "double", nullable=False),
    Column("最低", "double", nullable=False),
    Column("成交量", "double", nullable=False),
    Column("成交额", "double", nullable=False),
    Column("振幅", "double"),
    Column("涨跌幅", "double"),
    Column("涨跌额", "double"),
    Column("换手率", "double"),
]

_START_YEAR = 2024
_YEARS = 3


def upgrade() -> None:
    """Create the P0 warehouse tables."""
    for domain, (key, partition_key) in _DWD_TABLES.items():
        op.execute(
            dwd_table_ddl(
                domain,
                key=key,
                partition_key=partition_key,
                start_year=_START_YEAR,
                years=_YEARS,
            )
        )
    op.execute(
        ods_table_ddl(
            "stock_daily",
            "akshare",
            _STOCK_DAILY_ODS_COLUMNS,
            key=("股票代码", "日期"),
            partition_key="日期",
            start_year=_START_YEAR,
            years=_YEARS,
        )
    )


def downgrade() -> None:
    """Drop the P0 warehouse tables (reverse creation order)."""
    op.execute("DROP TABLE IF EXISTS `ods_stock_daily_akshare`")
    for domain in reversed(list(_DWD_TABLES)):
        op.execute(f"DROP TABLE IF EXISTS `dwd_{domain}`")
