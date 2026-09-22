"""Partition maintenance tests (A4.3).

Design §8.1: a daily task checks ``information_schema.PARTITIONS``
and, when the latest upper bound is behind ``current year + 2``,
reorganizes the ``pmax`` fallback into the missing yearly partitions.
The unit tests cover the pure planning (bounds -> partitions to add,
SQL rendering); the ``e2e`` class proves the cross-year write against
the real warehouse: after maintenance a row for a new year lands in
its own partition instead of piling into ``pmax``.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, pool, text

from opendata.pipeline.partitions import (
    PartitionState,
    plan_yearly_partitions,
    reorganize_sql,
)

SUFFICIENT = [
    PartitionState("p2024", date(2025, 1, 1)),
    PartitionState("p2025", date(2026, 1, 1)),
    PartitionState("p2026", date(2027, 1, 1)),
    PartitionState("pmax", None),
]


class TestPlanYearlyPartitions:
    def test_no_plan_when_the_horizon_is_met(self):
        assert plan_yearly_partitions(SUFFICIENT, current_year=2026, years_ahead=1) == []

    def test_plans_every_missing_year_up_to_the_horizon(self):
        existing = [
            PartitionState("p2024", date(2025, 1, 1)),
            PartitionState("pmax", None),
        ]

        plan = plan_yearly_partitions(existing, current_year=2026, years_ahead=2)

        assert plan == [
            ("p2025", date(2026, 1, 1)),
            ("p2026", date(2027, 1, 1)),
            ("p2027", date(2028, 1, 1)),
        ]

    def test_ignores_the_pmax_bound_as_a_horizon(self):
        # Only the fallback exists: the horizon rule starts from the
        # current year, so years_ahead=1 needs the single p2026 bound.
        existing = [PartitionState("pmax", None)]

        plan = plan_yearly_partitions(existing, current_year=2026, years_ahead=1)

        assert plan == [("p2026", date(2027, 1, 1))]

    def test_rejects_non_positive_years_ahead(self):
        with pytest.raises(ValueError, match="years_ahead"):
            plan_yearly_partitions(SUFFICIENT, current_year=2026, years_ahead=0)


class TestReorganizeSql:
    def test_reorganizes_pmax_into_the_new_partitions_plus_pmax(self):
        sql = reorganize_sql(
            "ods_stock_daily_akshare",
            [("p2025", date(2026, 1, 1)), ("p2026", date(2027, 1, 1))],
        )

        assert sql == (
            "ALTER TABLE `ods_stock_daily_akshare` REORGANIZE PARTITION pmax INTO ("
            "PARTITION p2025 VALUES LESS THAN ('2026-01-01'), "
            "PARTITION p2026 VALUES LESS THAN ('2027-01-01'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        )

    def test_unsafe_table_name_fails_closed(self):
        with pytest.raises(ValueError):
            reorganize_sql("ods`; DROP TABLE x; --", [("p2025", date(2026, 1, 1))])


@pytest.mark.e2e
class TestCrossYearWrite:
    """After maintenance a new year's rows must not pile into pmax."""

    TABLE = "_probe_partition_maintenance"

    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_partition_maintenance`"))
            connection.execute(
                text(
                    "CREATE TABLE `_probe_partition_maintenance` ("
                    "`symbol` varchar(64) NOT NULL, "
                    "`trade_date` date NOT NULL, "
                    "`close` double NOT NULL, "
                    "PRIMARY KEY (`symbol`, `trade_date`)) "
                    "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                    "PARTITION BY RANGE COLUMNS(`trade_date`) ("
                    "PARTITION p2024 VALUES LESS THAN ('2025-01-01'), "
                    "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
                )
            )
        yield engine
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_partition_maintenance`"))
        engine.dispose()

    def test_maintenance_extends_partitions_and_new_year_rows_land_alone(self, warehouse):
        from opendata.pipeline.partitions import PartitionMaintainer

        maintainer = PartitionMaintainer(warehouse)
        applied = maintainer.ensure(
            self.TABLE, partition_key="trade_date", current_year=2026, years_ahead=2
        )

        assert applied == ["p2025", "p2026", "p2027"]
        with warehouse.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO `_probe_partition_maintenance` (symbol, trade_date, close) "
                    "VALUES ('600519', '2027-03-01', 1.0)"
                )
            )
            placements = (
                connection.execute(
                    text(
                        "SELECT PARTITION_NAME FROM information_schema.PARTITIONS "
                        "WHERE TABLE_SCHEMA = DATABASE() "
                        "AND TABLE_NAME = '_probe_partition_maintenance' "
                        "AND PARTITION_NAME IS NOT NULL AND TABLE_ROWS > 0"
                    )
                )
                .scalars()
                .all()
            )

        assert placements == ["p2027"]

    def test_maintenance_is_idempotent(self, warehouse):
        from opendata.pipeline.partitions import PartitionMaintainer

        maintainer = PartitionMaintainer(warehouse)
        maintainer.ensure(self.TABLE, partition_key="trade_date", current_year=2026, years_ahead=2)

        assert (
            maintainer.ensure(
                self.TABLE, partition_key="trade_date", current_year=2026, years_ahead=2
            )
            == []
        )

    def test_unpartitioned_table_is_a_noop(self, warehouse):
        from opendata.pipeline.partitions import PartitionMaintainer

        with warehouse.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_unpartitioned`"))
            connection.execute(
                text("CREATE TABLE `_probe_unpartitioned` (`id` int NOT NULL PRIMARY KEY)")
            )
        try:
            applied = PartitionMaintainer(warehouse).ensure(
                "_probe_unpartitioned", partition_key="id", current_year=2026
            )
            assert applied == []
        finally:
            with warehouse.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS `_probe_unpartitioned`"))
