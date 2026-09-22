"""ods writer tests (A4.2).

The ods writer replaces the legacy provider path and fixes its two
defects (design §8.1): tables created without a primary key made
``ON DUPLICATE KEY UPDATE`` a no-op, and a content-hash unique key
turned a corrected price into a new row. The writer upserts on the
**business key**, so re-writing a corrected value updates in place.

Unit tests cover the pure SQL/planning layer (no database); the
``e2e`` class proves the real upsert semantics, staging/direct
equivalence and the write benchmark against the warehouse MySQL.
"""

from datetime import datetime, timezone

import pandas as pd
import pytest
from sqlalchemy import create_engine, inspect, pool, text

from opendata.pipeline.ddl import Column, ods_table_ddl
from opendata.pipeline.ods_writer import (
    OdsWriter,
    build_staging_upsert_sql,
    build_upsert_sql,
    plan_frame,
)

BATCH_ID = "8f14e45f-ceea-467e-b1b3-1d0d5b9f2c11"


def _frame(**overrides) -> pd.DataFrame:
    """A two-row source frame shaped like an ods batch."""
    data = {
        "symbol": ["600519", "000001"],
        "trade_date": ["2024-01-02", "2024-01-02"],
        "close": [1688.0, 9.5],
    }
    data.update(overrides)
    return pd.DataFrame(data)


class TestSqlBuilders:
    def test_upsert_uses_business_key_and_alias_form(self):
        sql = build_upsert_sql(
            "ods_x_akshare", ["symbol", "trade_date", "close"], ("symbol", "trade_date")
        )

        assert sql == (
            "INSERT INTO `ods_x_akshare` (`symbol`, `trade_date`, `close`) "
            "VALUES (:symbol, :trade_date, :close) AS new "
            "ON DUPLICATE KEY UPDATE `close` = new.`close`"
        )

    def test_upsert_with_only_key_columns_keeps_a_valid_no_op_update(self):
        sql = build_upsert_sql("ods_x_akshare", ["symbol"], ("symbol",))

        assert sql.endswith("ON DUPLICATE KEY UPDATE `symbol` = new.`symbol`")

    def test_staging_upsert_qualifies_source_columns(self):
        sql = build_staging_upsert_sql(
            "ods_x_akshare",
            "_stg_ods_x_akshare",
            ["symbol", "trade_date", "close"],
            ("symbol", "trade_date"),
        )

        assert sql == (
            "INSERT INTO `ods_x_akshare` (`symbol`, `trade_date`, `close`) "
            "SELECT s.`symbol`, s.`trade_date`, s.`close` FROM `_stg_ods_x_akshare` AS s "
            "ON DUPLICATE KEY UPDATE `close` = s.`close`"
        )

    def test_unsafe_identifier_fails_closed(self):
        with pytest.raises(ValueError):
            build_upsert_sql("ods_x`; DROP TABLE y; --", ["a"], ("a",))


class TestPlanFrame:
    def test_unknown_source_columns_are_ignored_and_reported(self):
        frame = _frame(extra="ignored")

        prepared, plan = plan_frame(
            frame,
            table_columns=["symbol", "trade_date", "close", "_source", "_fetched_at", "_batch_id"],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

        assert plan.ignored == ["extra"]
        assert "extra" not in prepared.columns
        assert list(prepared.columns) == [
            "symbol",
            "trade_date",
            "close",
            "_source",
            "_fetched_at",
            "_batch_id",
        ]

    def test_metadata_columns_are_stamped(self):
        prepared, _ = plan_frame(
            _frame(),
            table_columns=["symbol", "trade_date", "close", "_source", "_fetched_at", "_batch_id"],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, 12, 30, tzinfo=timezone.utc),
        )

        assert set(prepared["_source"]) == {"akshare"}
        assert set(prepared["_batch_id"]) == {BATCH_ID}
        assert prepared["_fetched_at"].iloc[0] == datetime(2026, 9, 23, 12, 30)

    def test_table_owned_columns_absent_from_the_frame_are_filled_nullable(self):
        prepared, _ = plan_frame(
            _frame(),
            table_columns=[
                "symbol",
                "trade_date",
                "close",
                "turnover",
                "_source",
                "_fetched_at",
                "_batch_id",
            ],
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
            fetched_at=datetime(2026, 9, 23, tzinfo=timezone.utc),
        )

        assert "turnover" in prepared.columns
        assert prepared["turnover"].isna().all()

    def test_missing_business_key_fails_closed(self):
        with pytest.raises(ValueError, match="business key"):
            plan_frame(
                _frame().drop(columns=["trade_date"]),
                table_columns=["symbol", "close"],
                key=("symbol", "trade_date"),
                source="akshare",
                batch_id=BATCH_ID,
            )

    def test_invalid_batch_id_fails_closed(self):
        with pytest.raises(ValueError, match="batch_id"):
            plan_frame(
                _frame(),
                table_columns=["symbol", "trade_date", "close"],
                key=("symbol", "trade_date"),
                source="akshare",
                batch_id="short",
            )


@pytest.mark.e2e
class TestLiveUpsert:
    """Real MySQL semantics: the defects this writer must not repeat."""

    TABLE = "_probe_ods_writer"

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        ddl = ods_table_ddl(
            "stock_daily",
            "akshare",
            [
                Column("symbol", "varchar(64)", nullable=False),
                Column("trade_date", "date", nullable=False),
                Column("close", "double", nullable=False),
                Column("ignored_by_writer", "double"),
            ],
            key=("symbol", "trade_date"),
        ).replace("ods_stock_daily_akshare", self.TABLE)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_ods_writer`"))
            connection.execute(text(ddl))
        yield engine
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_ods_writer`"))
        engine.dispose()

    def _write(self, engine, frame, *, mode="staging"):
        writer = OdsWriter(engine, batch_size=2, mode=mode)
        return writer.write(
            frame,
            table=self.TABLE,
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )

    def test_corrected_value_updates_in_place_without_duplicating(self, warehouse):
        first = _frame()
        # A corrected close for 600519: same business key, new value.
        second = _frame(close=[1700.0, 9.5])

        self._write(warehouse, first)
        self._write(warehouse, second)

        with warehouse.connect() as connection:
            rows = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()
        assert rows == [("000001", 9.5), ("600519", 1700.0)]

    def test_staging_and_direct_paths_agree(self, warehouse):
        self._write(warehouse, _frame(), mode="staging")
        with warehouse.connect() as connection:
            staged = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()
            connection.execute(text("TRUNCATE TABLE `_probe_ods_writer`"))
        self._write(warehouse, _frame(), mode="direct")
        with warehouse.connect() as connection:
            direct = connection.execute(
                text("SELECT symbol, close FROM `_probe_ods_writer` ORDER BY symbol")
            ).all()

        assert staged == direct

    def test_intra_batch_duplicate_keys_end_with_the_last_value(self, warehouse):
        duplicated = pd.DataFrame(
            {
                "symbol": ["600519", "600519"],
                "trade_date": ["2024-01-02", "2024-01-02"],
                "close": [1.0, 2.0],
            }
        )

        self._write(warehouse, duplicated)

        with warehouse.connect() as connection:
            count = connection.execute(text("SELECT COUNT(*) FROM `_probe_ods_writer`")).scalar()
            close = connection.execute(
                text("SELECT close FROM `_probe_ods_writer` WHERE symbol='600519'")
            ).scalar()
        assert count == 1
        assert close == 2.0

    def test_unknown_columns_never_reach_the_table(self, warehouse):
        result = self._write(warehouse, _frame(extra=[1.0, 2.0]))

        with warehouse.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns(self.TABLE)}
        assert "extra" not in columns
        assert result.batches >= 1

    def test_write_benchmark_records_throughput(self, warehouse):
        rows = 20_000
        frame = pd.DataFrame(
            {
                "symbol": [f"{index % 5000:06d}" for index in range(rows)],
                "trade_date": ["2024-01-02"] * rows,
                "close": [float(index) for index in range(rows)],
            }
        )
        writer = OdsWriter(warehouse, batch_size=10_000)

        started = datetime.now(timezone.utc)
        result = writer.write(
            frame,
            table=self.TABLE,
            key=("symbol", "trade_date"),
            source="akshare",
            batch_id=BATCH_ID,
        )
        elapsed = (datetime.now(timezone.utc) - started).total_seconds()

        assert result.rows == rows
        through_put = rows / max(elapsed, 1e-9)
        print(f"\nods write benchmark: {rows} rows in {elapsed:.2f}s = {through_put:,.0f} rows/s")
        assert through_put > 500  # sanity floor, not a performance gate
