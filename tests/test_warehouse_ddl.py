"""Warehouse DDL generator tests (A4.1).

The data-warehouse DDL is generated from declared column specs (ods)
and contract models (dwd) under the design §8 rules: source-raw
columns plus the ods metadata trio, dwd contract columns plus the
traceability columns, business-key primary keys, and the partition
trio (yearly ``RANGE COLUMNS`` partitions, a ``MAXVALUE`` fallback
partition, and the rule that every unique key must contain the
partition key).
"""

from datetime import date

import pytest

from opendata.pipeline.ddl import (
    Column,
    contract_columns,
    dwd_table_ddl,
    ods_table_ddl,
    year_partitions,
)

STOCK_DAILY_ODS_COLUMNS = [
    Column("symbol", "varchar(64)", nullable=False),
    Column("trade_date", "date", nullable=False),
    Column("open", "double", nullable=False),
    Column("close", "double", nullable=False),
    Column("volume", "bigint"),
    Column("amount", "double"),
    Column("turnover", "double"),
]


class TestOdsDdl:
    def test_contains_source_columns_and_metadata_trio(self):
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            STOCK_DAILY_ODS_COLUMNS,
            key=("symbol", "trade_date"),
            partition_key="trade_date",
            start_year=2024,
            years=3,
        )

        assert "CREATE TABLE IF NOT EXISTS `ods_stock_daily_akshare`" in ddl
        for fragment in (
            "`symbol` varchar(64) NOT NULL",
            "`trade_date` date NOT NULL",
            "`open` double NOT NULL",
            "`volume` bigint NULL",
            "`_source` varchar(32) NOT NULL",
            "`_fetched_at` datetime NOT NULL",
            "`_batch_id` char(36) NOT NULL",
        ):
            assert fragment in ddl
        assert "PRIMARY KEY (`symbol`, `trade_date`)" in ddl
        assert "ENGINE=InnoDB" in ddl

    def test_partition_trio(self):
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            STOCK_DAILY_ODS_COLUMNS,
            key=("symbol", "trade_date"),
            partition_key="trade_date",
            start_year=2024,
            years=3,
        )

        assert "PARTITION BY RANGE COLUMNS(`trade_date`)" in ddl
        assert "PARTITION p2024 VALUES LESS THAN ('2025-01-01')" in ddl
        assert "PARTITION p2025 VALUES LESS THAN ('2026-01-01')" in ddl
        assert "PARTITION p2026 VALUES LESS THAN ('2027-01-01')" in ddl
        assert "PARTITION pmax VALUES LESS THAN (MAXVALUE)" in ddl

    def test_rejects_partition_key_outside_primary_key(self):
        with pytest.raises(ValueError, match="partition key"):
            ods_table_ddl(
                "stock_daily",
                "akshare",
                STOCK_DAILY_ODS_COLUMNS,
                key=("symbol",),
                partition_key="trade_date",
            )

    def test_rejects_unknown_partition_key(self):
        with pytest.raises(ValueError, match="unknown"):
            ods_table_ddl(
                "stock_daily",
                "akshare",
                STOCK_DAILY_ODS_COLUMNS,
                key=("symbol", "trade_date"),
                partition_key="missing_column",
            )

    def test_rejects_unsafe_identifier(self):
        with pytest.raises(ValueError):
            ods_table_ddl(
                "stock_daily",
                "akshare`; DROP TABLE x; --",
                STOCK_DAILY_ODS_COLUMNS,
                key=("symbol", "trade_date"),
            )

    def test_unpartitioned_table_has_no_partition_clause(self):
        ddl = ods_table_ddl(
            "index_constituent",
            "akshare",
            [Column("index_symbol", "varchar(64)", nullable=False)],
            key=("index_symbol",),
        )

        assert "PARTITION BY" not in ddl

    def test_accepts_source_column_names_in_unicode(self):
        """ods keeps source naming (§8.1), and sina/em columns are Chinese."""
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            [
                Column("股票代码", "varchar(64)", nullable=False),
                Column("日期", "date", nullable=False),
                Column("开盘", "double", nullable=False),
            ],
            key=("股票代码", "日期"),
            partition_key="日期",
            start_year=2024,
            years=1,
        )

        assert "`股票代码` varchar(64) NOT NULL" in ddl
        assert "PARTITION BY RANGE COLUMNS(`日期`)" in ddl


class TestDwdDdl:
    def test_derives_contract_columns_and_trace_columns(self):
        ddl = dwd_table_ddl(
            "stock_daily",
            key=("symbol", "trade_date"),
            partition_key="trade_date",
            start_year=2025,
            years=2,
        )

        assert "CREATE TABLE IF NOT EXISTS `dwd_stock_daily`" in ddl
        for fragment in (
            "`symbol` varchar(64) NOT NULL",
            "`trade_date` date NOT NULL",
            "`open` double NOT NULL",
            "`volume` double NOT NULL",
            "`source` varchar(32) NOT NULL",
            "`_merged_at` datetime NOT NULL",
            "`_diff_flag` tinyint(1) NOT NULL DEFAULT 0",
            "`_as_of` date NOT NULL",
        ):
            assert fragment in ddl
        assert "PRIMARY KEY (`symbol`, `trade_date`)" in ddl
        assert "PARTITION pmax VALUES LESS THAN (MAXVALUE)" in ddl

    def test_optional_contract_fields_are_nullable(self):
        columns = {column.name: column for column in contract_columns("index_constituent")}

        assert columns["weight"].nullable is True
        assert columns["symbol"].nullable is False

    def test_invalid_domain_fails_closed(self):
        with pytest.raises(LookupError, match="unknown domain"):
            dwd_table_ddl("not_a_domain", key=("symbol",))


class TestYearPartitions:
    def test_returns_contiguous_upper_bounds(self):
        partitions = year_partitions(2024, 3)

        assert partitions == [
            ("p2024", date(2025, 1, 1)),
            ("p2025", date(2026, 1, 1)),
            ("p2026", date(2027, 1, 1)),
        ]

    def test_rejects_non_positive_years(self):
        with pytest.raises(ValueError, match="years"):
            year_partitions(2024, 0)
