"""Table pagination tests (A4.3, ``/tables`` without an id column).

The legacy endpoint ordered every page by ``id``; ods/dwd tables are
keyed by their business key and have no surrogate id, so the order
clause is derived from the table shape. The integration class proves
it end to end against a SQLite warehouse stand-in (same code path as
the MySQL warehouse, different dialect).
"""

import pytest
from sqlalchemy import create_engine, text

from opendata.pipeline.table_page import order_columns, page_sql, table_shape


class TestOrderColumns:
    def test_prefers_the_surrogate_id_when_present(self):
        assert order_columns(["id", "symbol", "trade_date"], ["symbol"]) == ["id"]

    def test_falls_back_to_the_business_key(self):
        assert order_columns(["symbol", "trade_date", "close"], ["symbol", "trade_date"]) == [
            "symbol",
            "trade_date",
        ]

    def test_falls_back_to_the_first_column_without_a_key(self):
        assert order_columns(["symbol", "close"], []) == ["symbol"]

    def test_rejects_a_key_that_is_not_a_column(self):
        with pytest.raises(ValueError, match="key columns"):
            order_columns(["symbol"], ["trade_date"])


class TestPageSql:
    def test_orders_by_business_key_and_binds_pagination(self):
        sql = page_sql(
            "ods_stock_daily_akshare",
            columns=["symbol", "trade_date", "close"],
            key_columns=["symbol", "trade_date"],
        )

        assert sql == (
            "SELECT * FROM `ods_stock_daily_akshare` "
            "ORDER BY `symbol`, `trade_date` LIMIT :limit OFFSET :offset"
        )

    def test_orders_by_id_when_the_table_has_one(self):
        sql = page_sql("ak_legacy", columns=["id", "close"], key_columns=[])

        assert sql == "SELECT * FROM `ak_legacy` ORDER BY `id` LIMIT :limit OFFSET :offset"

    def test_unsafe_table_name_fails_closed(self):
        with pytest.raises(ValueError):
            page_sql("t`; DROP TABLE x; --", columns=["a"], key_columns=[])

    def test_column_less_table_fails_closed(self):
        with pytest.raises(ValueError, match="no columns"):
            page_sql("empty_table", columns=[], key_columns=[])


class TestTableShapeIntegration:
    """Real queries on a SQLite warehouse stand-in."""

    @pytest.fixture
    def engine(self):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE ods_stock_daily_akshare ("
                    "symbol TEXT NOT NULL, trade_date TEXT NOT NULL, close REAL NOT NULL, "
                    "PRIMARY KEY (symbol, trade_date))"
                )
            )
            connection.execute(text("CREATE TABLE ak_legacy (id INTEGER PRIMARY KEY, close REAL)"))
        yield engine
        engine.dispose()

    def test_shape_of_a_table_without_id(self, engine):
        with engine.connect() as connection:
            columns, keys = table_shape(connection, "ods_stock_daily_akshare")

        assert columns == ["symbol", "trade_date", "close"]
        assert keys == ["symbol", "trade_date"]

    def test_paging_a_table_without_id_returns_every_row_once(self, engine):
        with engine.begin() as connection:
            for symbol in ("600519", "000001", "600000"):
                connection.execute(
                    text(
                        "INSERT INTO ods_stock_daily_akshare (symbol, trade_date, close) "
                        "VALUES (:symbol, '2024-01-02', 1.0)"
                    ),
                    {"symbol": symbol},
                )
        with engine.connect() as connection:
            columns, keys = table_shape(connection, "ods_stock_daily_akshare")
            sql = page_sql("ods_stock_daily_akshare", columns=columns, key_columns=keys)
            first = connection.execute(text(sql), {"limit": 2, "offset": 0}).all()
            second = connection.execute(text(sql), {"limit": 2, "offset": 2}).all()

        assert [row[0] for row in first] == ["000001", "600000"]
        assert [row[0] for row in second] == ["600519"]
