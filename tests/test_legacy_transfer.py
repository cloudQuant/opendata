"""Legacy transfer tool tests (AC-15, gate-runnable).

The live drill runs against the real warehouse (see
``docs/evidence/C3/``); these cover the pure surface - the mapping
registry, the assessment model and the SQL shapes that the migrate and
verify statements produce.
"""

from __future__ import annotations

import pytest

from scripts.codemod.legacy_transfer import (
    MAPPINGS,
    Assessment,
    TransferError,
    migrate,
)


@pytest.fixture
def engine():
    """A dummy engine - the tested paths are dry-run only."""
    return object()  # type: ignore[return-value]


class TestMappings:
    def test_stock_daily_history_has_a_p0_mapping(self):
        mapping = MAPPINGS["STOCK_ZH_A_HIST"]

        assert mapping["to"] == "ods_stock_daily_akshare"
        assert mapping["source"] == "akshare"
        assert "股票代码" in mapping["columns"]
        assert "日期" in mapping["date_columns"]

    def test_mapping_covers_the_metadata_columns(self):
        mapping = MAPPINGS["STOCK_ZH_A_HIST"]
        # the ods writer requires _source/_fetched_at/_batch_id, which
        # migrate adds on top of the mapped value columns
        assert len(mapping["columns"]) == 12  # the value columns of the ods table


class TestAssessment:
    def test_assessment_shape(self):
        assessment = Assessment(table="T", rows=5, mapping=True)

        assert assessment.table == "T"
        assert assessment.rows == 5
        assert assessment.mapping is True


class TestMigrateSql:
    def test_unknown_table_fails_closed(self, engine):
        with pytest.raises(TransferError, match="no migration mapping"):
            migrate(engine, "NO_SUCH_TABLE", dry_run=True)

    def test_migrate_uses_insert_ignore_and_stamps_lineage(self, engine, capsys):
        migrate(engine, "STOCK_ZH_A_HIST", dry_run=True, limit=10)
        sql = capsys.readouterr().out

        assert "INSERT IGNORE INTO `ods_stock_daily_akshare`" in sql
        assert "`_source`, `_fetched_at`, `_batch_id`" in sql
        assert "STR_TO_DATE(`日期`, '%Y-%m-%d')" in sql
        assert "FROM `akshare_data`.`STOCK_ZH_A_HIST`" in sql
        assert "LIMIT 10" in sql

    def test_dry_run_affects_nothing(self, engine, capsys):
        affected = migrate(engine, "STOCK_ZH_A_HIST", dry_run=True, limit=10)

        assert affected == 0
        assert "INSERT IGNORE" in capsys.readouterr().out
