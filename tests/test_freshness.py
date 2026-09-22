"""Freshness and alert-matrix tests (A4.8).

Freshness answers "does each domain hold the latest trading day?" by
querying the warehouse for the domain's date column (derived from its
contract model: ``trade_date`` for bars, ``ex_date`` for corporate
actions, ``report_period`` for financials, ``as_of`` for index
membership). A missing table or a lag beyond the threshold raises an
alert - that is the design's "模拟数据缺失触发告警" case.

The alert matrix covers the four operational signals from the
requirements: pipeline failures, consecutive failures, partition
gaps (reusing the A4.3 horizon planner) and disk water level.
"""

from datetime import date

import pytest
from sqlalchemy import create_engine, pool, text

from opendata.pipeline.freshness import (
    AlertSettings,
    DiskUsage,
    FreshnessReport,
    PipelineFailure,
    evaluate_alerts,
    freshness_field,
    ods_freshness,
)
from opendata.pipeline.partitions import PartitionState

EXPECTED = date(2024, 1, 31)


class TestFreshnessField:
    def test_bar_domains_use_trade_date(self):
        assert freshness_field("stock_daily") == "trade_date"
        assert freshness_field("futures_daily") == "trade_date"

    def test_event_and_report_domains_use_their_own_date_column(self):
        assert freshness_field("stock_action") == "ex_date"
        assert freshness_field("financial_statement") == "report_period"
        assert freshness_field("index_constituent") == "as_of"
        assert freshness_field("trading_calendar") == "date"

    def test_unknown_domain_fails_closed(self):
        with pytest.raises(LookupError, match="unknown domain"):
            freshness_field("not_a_domain")


class TestCheckFreshness:
    @pytest.fixture
    def warehouse(self):
        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `ods_stock_daily_akshare` WHERE 股票代码 = 'FRESHNESS_PROBE'")
            )
        engine.dispose()

    def test_reports_the_lag_against_the_expected_date(self, warehouse):
        with warehouse.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO `ods_stock_daily_akshare` "
                    "(`日期`, `股票代码`, `开盘`, `收盘`, `最高`, `最低`, `成交量`, `成交额`, "
                    "`_source`, `_fetched_at`, `_batch_id`) VALUES "
                    "('2024-01-05', 'FRESHNESS_PROBE', 1, 1, 1, 1, 1, 1, "
                    "'akshare', NOW(), '11111111-2222-4333-8444-555555555555')"
                )
            )

        report = ods_freshness(
            warehouse,
            "stock_daily",
            "akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
        )

        assert report.field == "日期"  # the ods layer keeps source naming
        assert report.latest == date(2024, 1, 5)
        assert report.lag_days == 26
        assert report.status == "stale"

    def test_fresh_when_the_latest_row_reaches_the_expected_date(self, warehouse):
        with warehouse.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO `ods_stock_daily_akshare` "
                    "(`日期`, `股票代码`, `开盘`, `收盘`, `最高`, `最低`, `成交量`, `成交额`, "
                    "`_source`, `_fetched_at`, `_batch_id`) VALUES "
                    "(:day, 'FRESHNESS_PROBE', 1, 1, 1, 1, 1, 1, "
                    "'akshare', NOW(), '11111111-2222-4333-8444-555555555555')"
                ),
                {"day": EXPECTED},
            )

        report = ods_freshness(
            warehouse,
            "stock_daily",
            "akshare",
            table="ods_stock_daily_akshare",
            expected=EXPECTED,
        )

        assert report.status == "fresh"
        assert report.lag_days == 0

    def test_missing_table_is_reported_not_raised(self, warehouse):
        """模拟数据缺失: an absent table must alert, never crash the check."""
        report = ods_freshness(
            warehouse,
            "stock_daily",
            "akshare",
            table="_missing_table_probe",
            expected=EXPECTED,
        )

        assert report.status == "missing"
        assert report.latest is None
        assert report.lag_days is None


class TestAlertMatrix:
    def _settings(self) -> AlertSettings:
        return AlertSettings()

    def test_all_clear_produces_no_alerts(self):
        alerts = evaluate_alerts(
            settings=self._settings(),
            freshness=[
                FreshnessReport(
                    domain="stock_daily",
                    source="akshare",
                    field="trade_date",
                    latest=EXPECTED,
                    expected=EXPECTED,
                    lag_days=0,
                    status="fresh",
                )
            ],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=10, total_bytes=100),
        )

        assert alerts == []

    def test_stale_freshness_alerts_and_escalates_with_the_lag(self):
        warning = evaluate_alerts(
            settings=self._settings(),
            freshness=[_report(lag=2)],
            failures=[],
            partition_plans={},
            disk=None,
        )
        critical = evaluate_alerts(
            settings=self._settings(),
            freshness=[_report(lag=5)],
            failures=[],
            partition_plans={},
            disk=None,
        )

        assert warning[0].rule == "freshness" and warning[0].severity == "warning"
        assert critical[0].severity == "critical"

    def test_missing_table_alerts_as_critical(self):
        alerts = evaluate_alerts(
            settings=self._settings(),
            freshness=[_report(status="missing")],
            failures=[],
            partition_plans={},
            disk=None,
        )

        assert alerts[0].rule == "freshness"
        assert alerts[0].severity == "critical"
        assert "missing" in alerts[0].detail

    def test_pipeline_failure_alerts_and_consecutive_failures_escalate(self):
        single = evaluate_alerts(
            settings=self._settings(),
            freshness=[],
            failures=[PipelineFailure("stock_daily", "akshare", 1, "boom")],
            partition_plans={},
            disk=None,
        )
        repeated = evaluate_alerts(
            settings=self._settings(),
            freshness=[],
            failures=[PipelineFailure("stock_daily", "akshare", 4, "boom")],
            partition_plans={},
            disk=None,
        )

        assert single[0].rule == "pipeline_failure" and single[0].severity == "warning"
        assert any(alert.rule == "consecutive_failures" for alert in repeated)
        assert all(
            alert.severity == "critical"
            for alert in repeated
            if alert.rule == "consecutive_failures"
        )

    def test_partition_gap_alerts(self):
        alerts = evaluate_alerts(
            settings=self._settings(),
            freshness=[],
            failures=[],
            partition_plans={
                "ods_stock_daily_akshare": [("p2027", date(2028, 1, 1))],
            },
            disk=None,
        )

        assert alerts[0].rule == "partition_missing"
        assert alerts[0].severity == "critical"
        assert "p2027" in alerts[0].detail

    def test_disk_water_level_warns_then_escalates(self):
        warning = evaluate_alerts(
            settings=self._settings(),
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=85, total_bytes=100),
        )
        critical = evaluate_alerts(
            settings=self._settings(),
            freshness=[],
            failures=[],
            partition_plans={},
            disk=DiskUsage(used_bytes=96, total_bytes=100),
        )

        assert warning[0].rule == "disk_water" and warning[0].severity == "warning"
        assert critical[0].severity == "critical"

    def test_report_is_json_ready(self):
        alerts = evaluate_alerts(
            settings=self._settings(),
            freshness=[_report(lag=9)],
            failures=[PipelineFailure("stock_daily", "akshare", 1, "boom")],
            partition_plans={"ods_stock_daily_akshare": [("p2028", date(2029, 1, 1))]},
            disk=DiskUsage(used_bytes=99, total_bytes=100),
        )

        payloads = [alert.as_dict() for alert in alerts]
        assert {payload["rule"] for payload in payloads} == {
            "freshness",
            "pipeline_failure",
            "partition_missing",
            "disk_water",
        }
        assert all(
            set(payload) >= {"rule", "severity", "subject", "detail"} for payload in payloads
        )


def _report(*, lag: int | None = 2, status: str = "stale") -> FreshnessReport:
    return FreshnessReport(
        domain="stock_daily",
        source="akshare",
        field="trade_date",
        latest=None if status == "missing" else EXPECTED,
        expected=EXPECTED,
        lag_days=lag,
        status=status,
    )


class TestPartitionPlanCollector:
    def test_collects_gaps_for_partitioned_tables_only(self):
        from sqlalchemy import create_engine, text

        from opendata.core.config import settings
        from opendata.pipeline.freshness import partition_plans_for

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS `_probe_unpartitioned_fresh`"))
            connection.execute(
                text("CREATE TABLE `_probe_unpartitioned_fresh` (`id` int NOT NULL PRIMARY KEY)")
            )

        try:
            plans = partition_plans_for(
                engine,
                ["ods_stock_daily_akshare", "_probe_unpartitioned_fresh"],
                current_year=2026,
                years_ahead=2,
            )
            # The shipped table already covers 2024-2026 + pmax with
            # years_ahead=2 (horizon 2028) -> a gap must be reported.
            assert "ods_stock_daily_akshare" in plans
            assert plans["ods_stock_daily_akshare"] == [("p2027", date(2028, 1, 1))]
            assert "_probe_unpartitioned_fresh" not in plans
        finally:
            with engine.begin() as connection:
                connection.execute(text("DROP TABLE IF EXISTS `_probe_unpartitioned_fresh`"))
            engine.dispose()


class TestPartitionPlanHelper:
    def test_partition_plans_reuse_the_horizon_planner(self):
        """The matrix consumes plans produced by the A4.3 planner."""
        from opendata.pipeline.partitions import plan_yearly_partitions

        plan = plan_yearly_partitions(
            [PartitionState("p2024", date(2025, 1, 1)), PartitionState("pmax", None)],
            current_year=2026,
            years_ahead=1,
        )

        assert plan == [("p2025", date(2026, 1, 1)), ("p2026", date(2027, 1, 1))]
