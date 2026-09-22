"""dq_diff_report writer tests (A4.5, design §8.2).

The report lives in the data warehouse (``opendata_data``): one row
per sampled difference plus the aggregate counters of the run, so the
dashboard side never has to hold the full detail in memory - the
design keeps only aggregate data plus samples there and exports the
full detail separately.
"""

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, pool, text

from opendata.pipeline.cross_check import DiffSummary, FieldDiff, Verdict
from opendata.pipeline.diff_report import (
    DQ_DIFF_REPORT_TABLE,
    REPORT_COLUMNS,
    build_report_insert_sql,
    summarize_for_report,
)

CHECKED_AT = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
BATCH_ID = "5d2c8a41-9e3b-4c7f-8a1b-6f0d2e9c4b73"


def _summary() -> DiffSummary:
    return DiffSummary(
        domain="stock_daily",
        source_a="akshare",
        source_b="ths",
        checked_at=CHECKED_AT,
        batch_id=BATCH_ID,
        compared_keys=3,
        deviation_count=2,
        missing_count=1,
        per_field={"close": 2},
        samples=[
            FieldDiff(("600519", date(2024, 1, 2)), "close", 1.0, 2.0, 0.5, Verdict.DEVIATION),
            FieldDiff(("000001", date(2024, 1, 2)), "*", None, "present", None, Verdict.MISSING),
        ],
    )


class TestReportRows:
    def test_detail_rows_carry_the_design_columns(self):
        rows = summarize_for_report(_summary())

        assert len(rows) == 2  # sampled details only
        first = rows[0]
        assert first.domain == "stock_daily"
        assert first.source_a == "akshare"
        assert first.source_b == "ths"
        assert first.biz_key == "600519|2024-01-02"
        assert first.field == "close"
        assert first.value_a == "1.0"
        assert first.value_b == "2.0"
        assert first.deviation == pytest.approx(0.5)
        assert first.verdict == Verdict.DEVIATION.value
        assert first.batch_id == BATCH_ID
        assert first.checked_at == CHECKED_AT
        assert set(first.as_params()) == set(REPORT_COLUMNS)

    def test_table_name_derivation(self):
        assert DQ_DIFF_REPORT_TABLE == "dq_diff_report"

    def test_insert_sql_binds_every_column(self):
        sql = build_report_insert_sql()

        assert f"INSERT INTO `{DQ_DIFF_REPORT_TABLE}`" in sql
        assert ":domain" in sql
        assert ":verdict" in sql


@pytest.mark.e2e
class TestReportAgainstMysql:
    @pytest.fixture
    def warehouse(self):
        import subprocess
        import sys
        from pathlib import Path as PathLib

        from opendata.core.config import settings

        engine = create_engine(settings.data_database_url, poolclass=pool.NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception as exc:  # any connection failure means skip
            pytest.skip(f"warehouse database unreachable: {type(exc).__name__}")
        # Self-sufficient: ensure the warehouse schema is at head (the
        # migration suite may have left it elsewhere while testing).
        subprocess.run(  # literal argv, no shell
            [
                sys.executable,
                "-m",
                "alembic",
                "-c",
                "alembic_data.ini",
                "upgrade",
                "head",
            ],
            cwd=PathLib(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
        )
        yield engine
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM `dq_diff_report` WHERE batch_id = :batch"),
                {"batch": BATCH_ID},
            )
        engine.dispose()

    def test_writes_and_reads_back_the_sample_rows(self, warehouse):
        from opendata.pipeline.diff_report import DiffReportWriter

        writer = DiffReportWriter(warehouse)
        written = writer.write(_summary())

        assert written == 2
        with warehouse.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT field, verdict, deviation FROM `dq_diff_report` "
                    "WHERE batch_id = :batch ORDER BY field"
                ),
                {"batch": BATCH_ID},
            ).all()
        assert [(row[0], row[1]) for row in rows] == [("*", "missing"), ("close", "deviation")]

    def test_writing_the_same_batch_twice_does_not_duplicate(self, warehouse):
        from opendata.pipeline.diff_report import DiffReportWriter

        writer = DiffReportWriter(warehouse)
        writer.write(_summary())
        writer.write(_summary())

        with warehouse.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM `dq_diff_report` WHERE batch_id = :batch"),
                {"batch": BATCH_ID},
            ).scalar()
        assert count == 2
