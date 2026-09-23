"""Retention policy tests (A4.10, design §8.5 / §13).

The policy is a declaration first: every asset must resolve to a
lifetime, and only bounded assets may be purged - the guards are what
keep a maintenance job from deleting daily bars.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from opendata.core.config import settings
from opendata.pipeline.diff_report import REPORT_COLUMNS
from opendata.pipeline.retention import (
    MINUTE_ARCHIVE,
    RAW_RESPONSE_CACHE,
    RetentionError,
    RetentionMode,
    assert_policy_covers,
    export_diff_details,
    purge_diff_report,
    purge_expired_rows,
    purge_minute_archives,
    purge_raw_response_cache,
    retention_policy,
    rule_for,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


#: The tables ``alembic_data`` 0001 creates: ``dwd_<domain>`` for the
#: five warehouse-backed P0 domains plus the source-raw ods table. The
#: e2e class below re-checks completeness against the live warehouse,
#: so this literal list cannot hide a drifted migration.
P0_TABLES = (
    "dwd_stock_daily",
    "dwd_stock_action",
    "dwd_financial_statement",
    "dwd_financial_indicator",
    "dwd_index_constituent",
    "ods_stock_daily_akshare",
)


class TestRetentionPolicy:
    def test_declares_the_design_section_8_5_rows(self):
        modes = {rule.asset: rule.mode for rule in retention_policy()}

        assert RetentionMode.PERMANENT in modes.values()  # 日线/财务/元数据
        assert modes["dq_diff_report 聚合"] is RetentionMode.DAYS
        assert modes["分钟线归档（Parquet）"] is RetentionMode.YEARS
        assert modes["原始响应缓存"] is RetentionMode.TTL

    def test_permanent_rows_have_no_limit(self):
        for rule in retention_policy():
            if rule.mode is RetentionMode.PERMANENT:
                assert rule.limit is None

    def test_limits_come_from_settings(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_diff_report_days", 30)
        monkeypatch.setattr(settings, "retention_minute_years", 3)
        monkeypatch.setattr(settings, "cache_ttl_seconds", 120)

        assert rule_for("dq_diff_report").limit == 30
        assert rule_for(MINUTE_ARCHIVE).limit == 3
        assert rule_for(RAW_RESPONSE_CACHE).limit == 120

    def test_every_rule_explains_itself(self):
        for rule in retention_policy():
            assert rule.note
            assert rule.pattern


class TestRuleResolution:
    @pytest.mark.parametrize(
        "target",
        ["ods_stock_daily_akshare", "dwd_stock_daily", "dq_diff_report", MINUTE_ARCHIVE],
    )
    def test_resolves_known_targets(self, target):
        assert rule_for(target).pattern

    def test_exact_match_wins_over_prefix(self):
        assert rule_for("dq_diff_report").pattern == "dq_diff_report"

    def test_unknown_target_fails_closed(self):
        with pytest.raises(RetentionError, match="no retention rule covers"):
            rule_for("tmp_scratch_table")

    def test_real_p0_tables_are_all_covered(self):
        assert_policy_covers(P0_TABLES)

    def test_uncovered_tables_are_named(self):
        with pytest.raises(RetentionError, match="tmp_one, tmp_two"):
            assert_policy_covers(["tmp_one", "dwd_stock_daily", "tmp_two"])


class TestPurgeGuards:
    def test_permanent_table_is_never_purged(self):
        with pytest.raises(RetentionError, match="declared permanent"):
            purge_expired_rows(
                None,  # type: ignore[arg-type]  # rejected before any SQL
                table="dwd_stock_daily",
                date_column="trade_date",
            )

    def test_ods_table_is_never_purged(self):
        with pytest.raises(RetentionError, match="declared permanent"):
            purge_expired_rows(
                None,  # type: ignore[arg-type]
                table="ods_stock_daily_akshare",
                date_column="日期",
            )

    def test_non_positive_window_is_refused(self):
        with pytest.raises(RetentionError, match="non-positive window"):
            purge_expired_rows(
                None,  # type: ignore[arg-type]
                table="dq_diff_report",
                date_column="checked_at",
                older_than_days=0,
            )

    def test_injected_identifier_is_refused(self, monkeypatch):
        monkeypatch.setattr(settings, "retention_diff_report_days", 30)

        with pytest.raises(RetentionError, match="not a plain SQL identifier"):
            purge_expired_rows(
                None,  # type: ignore[arg-type]
                table="dq_diff_report",
                date_column="checked_at; DROP TABLE dwd_stock_daily",
            )


class TestMinuteArchives:
    def test_removes_years_outside_the_window(self, tmp_path):
        for year in (2015, 2016, 2024, 2026):
            directory = tmp_path / "stock_daily" / "600519" / str(year)
            directory.mkdir(parents=True)
            (directory / "part.parquet").write_bytes(b"x")

        removed = [path.name for path in purge_minute_archives(tmp_path, keep_years=3, today=NOW)]

        assert removed == ["2015", "2016"]  # 2024/2025/2026 retained
        assert (tmp_path / "stock_daily" / "600519" / "2024").exists()
        assert not (tmp_path / "stock_daily" / "600519" / "2015").exists()

    def test_missing_root_is_a_noop(self, tmp_path):
        assert purge_minute_archives(tmp_path / "absent", keep_years=3) == []

    def test_non_positive_window_is_refused(self, tmp_path):
        with pytest.raises(RetentionError, match="refusing to purge"):
            purge_minute_archives(tmp_path, keep_years=0)


class TestRawResponseCache:
    def test_removes_only_expired_files(self, tmp_path):
        stale = tmp_path / "eastmoney" / "kline.json"
        fresh = tmp_path / "eastmoney" / "fresh.json"
        stale.parent.mkdir(parents=True)
        stale.write_text("old", encoding="utf-8")
        fresh.write_text("new", encoding="utf-8")
        old = (NOW - timedelta(seconds=3600)).timestamp()
        os.utime(stale, (old, old))
        os.utime(fresh, ((NOW - timedelta(seconds=10)).timestamp(),) * 2)

        removed = purge_raw_response_cache(root=tmp_path, ttl_seconds=900, now=NOW)

        assert removed == 1
        assert not stale.exists()
        assert fresh.exists()

    def test_missing_root_is_a_noop(self, tmp_path):
        assert purge_raw_response_cache(root=tmp_path / "absent", now=NOW) == 0

    def test_non_positive_ttl_is_refused(self, tmp_path):
        with pytest.raises(RetentionError, match="refusing to purge"):
            purge_raw_response_cache(root=tmp_path, ttl_seconds=0, now=NOW)


@pytest.mark.e2e
class TestRetentionAgainstMysql:
    @pytest.fixture
    def warehouse(self):
        import subprocess
        import sys
        from pathlib import Path as PathLib

        from sqlalchemy import create_engine, pool
        from sqlalchemy import text as sql_text

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(sql_text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        subprocess.run(  # literal argv, no shell
            [sys.executable, "-m", "alembic", "-c", "alembic_data.ini", "upgrade", "head"],
            cwd=PathLib(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
        )
        yield engine
        engine.dispose()

    def _seed(self, engine) -> tuple[str, str]:
        """Insert one old and one fresh report row; return their batches."""
        from opendata.pipeline.diff_report import DiffReportWriter
        from tests.test_diff_report import _summary

        old_batch, fresh_batch = "a4100000-old", "a4100000-fresh"
        writer = DiffReportWriter(engine)
        for batch, checked_at in (
            (old_batch, NOW - timedelta(days=200)),
            (fresh_batch, NOW),
        ):
            summary = _summary()
            writer.write(
                summary.__class__(
                    **{
                        **summary.__dict__,
                        "batch_id": batch,
                        "checked_at": checked_at,
                    }
                )
            )
        return old_batch, fresh_batch

    def test_live_warehouse_tables_are_all_covered(self, warehouse):
        from sqlalchemy import inspect

        with warehouse.connect() as connection:
            tables = [
                name
                for name in inspect(connection).get_table_names()
                if name.startswith(("ods_", "dwd_"))
            ]

        assert set(P0_TABLES).issubset(tables)
        assert_policy_covers(tables)  # nothing migrated may lack a lifetime

    def test_purge_keeps_rows_inside_the_window(self, warehouse):
        from sqlalchemy import text as sql_text

        old_batch, fresh_batch = self._seed(warehouse)
        try:
            deleted = purge_diff_report(warehouse)

            assert deleted >= 1  # the seeded old batch at least
            with warehouse.connect() as connection:
                remaining = set(
                    connection.execute(
                        sql_text(
                            "SELECT batch_id FROM `dq_diff_report` WHERE batch_id IN (:old, :fresh)"
                        ),
                        {"old": old_batch, "fresh": fresh_batch},
                    ).scalars()
                )
            assert fresh_batch in remaining
            assert old_batch not in remaining
        finally:
            with warehouse.begin() as connection:
                connection.execute(
                    sql_text("DELETE FROM `dq_diff_report` WHERE batch_id IN (:old, :fresh)"),
                    {"old": old_batch, "fresh": fresh_batch},
                )

    def test_export_writes_every_column(self, warehouse, tmp_path):
        from sqlalchemy import text as sql_text

        old_batch, fresh_batch = self._seed(warehouse)
        try:
            destination = tmp_path / "diff-details.csv"

            exported = export_diff_details(warehouse, destination)

            assert exported >= 2
            lines = destination.read_text(encoding="utf-8").splitlines()
            assert lines[0].split(",") == list(REPORT_COLUMNS)
            assert any(fresh_batch in line for line in lines[1:])
        finally:
            with warehouse.begin() as connection:
                connection.execute(
                    sql_text("DELETE FROM `dq_diff_report` WHERE batch_id IN (:old, :fresh)"),
                    {"old": old_batch, "fresh": fresh_batch},
                )
