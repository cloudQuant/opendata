"""C85 unit coverage for ``opendata/pipeline/partitions.py`` (A4.3, design S8.1).

The maintainer is driven by a hand-written fake engine that records the SQL it
is handed and replays canned ``information_schema.PARTITIONS`` rows: no engine
is created, no warehouse connection is opened, and no DDL/DML ever reaches a
database. Covered here are :func:`parse_partition_rows` (dark until now), the
already-present guard and both bounds of the name-collision filter in
:func:`plan_yearly_partitions`, and the three exits of
``PartitionMaintainer.ensure``.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from opendata.pipeline.partitions import (
    FALLBACK_PARTITION,
    PartitionMaintainer,
    PartitionState,
    parse_partition_rows,
    plan_yearly_partitions,
    reorganize_sql,
)


class FakeResult:
    """Stand-in for a SQLAlchemy ``CursorResult`` with canned rows."""

    def __init__(self, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


class FakeConnection:
    """Records ``(mode, sql, parameters)`` and pops the next canned result."""

    def __init__(self, engine, mode):
        self._engine = engine
        self.mode = mode

    def execute(self, statement, parameters=None):
        self._engine.statement_log.append((self.mode, str(statement), parameters))
        if not self._engine.pending_results:
            raise AssertionError("the fake engine ran out of canned results")
        return self._engine.pending_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeEngine:
    """The only thing :class:`PartitionMaintainer` ever talks to."""

    def __init__(self, results=()):
        self.pending_results = list(results)
        self.statement_log = []
        self.opened_modes = []

    @property
    def executed_sql(self):
        return [sql for _mode, sql, _params in self.statement_log]

    def connect(self):
        self.opened_modes.append("connect")
        return FakeConnection(self, "connect")

    def begin(self):
        self.opened_modes.append("begin")
        return FakeConnection(self, "begin")


class TestParsePartitionRows:
    def test_no_rows_means_no_states(self):
        assert parse_partition_rows([]) == []

    def test_quoted_unquoted_and_maxvalue_descriptions_all_parse(self):
        states = parse_partition_rows(
            [
                ("p2024", "'2025-01-01'"),
                ("p2025", "2026-01-01"),
                ("p2026", "  '2027-01-01'  "),
                ("p2099", None),
                ("pmax", "MAXVALUE"),
                ("psilent", "maxvalue"),
            ]
        )

        assert states == [
            PartitionState("p2024", date(2025, 1, 1)),
            PartitionState("p2025", date(2026, 1, 1)),
            PartitionState("p2026", date(2027, 1, 1)),
            PartitionState("p2099", None),
            PartitionState("pmax", None),
            PartitionState("psilent", None),
        ]

    def test_states_keep_the_input_order(self):
        rows = [("p2026", "'2027-01-01'"), ("p2024", "'2025-01-01'"), ("pmax", "MAXVALUE")]

        assert [state.name for state in parse_partition_rows(rows)] == ["p2026", "p2024", "pmax"]
        assert parse_partition_rows(rows)[2].upper_bound is None

    def test_a_malformed_date_description_is_not_silently_dropped(self):
        with pytest.raises(ValueError):
            parse_partition_rows([("p2024", "not-a-date")])


class TestPlanYearlyPartitionsBounds:
    def test_rejects_a_negative_years_ahead(self):
        with pytest.raises(ValueError, match="years_ahead must be positive, got -2"):
            plan_yearly_partitions([], current_year=2026, years_ahead=-2)

    def test_skips_a_year_whose_partition_already_exists(self):
        # The horizon filter has to honour existing names, not only bounds:
        # ``p2025`` is present (with a mid-year bound), so only ``p2026`` lands.
        existing = [
            PartitionState("p2025", date(2025, 6, 1)),
            PartitionState("pmax", None),
        ]

        plan = plan_yearly_partitions(existing, current_year=2025, years_ahead=2)

        assert plan == [("p2026", date(2027, 1, 1))]

    def test_plans_every_year_when_nothing_but_the_fallback_exists(self):
        plan = plan_yearly_partitions(
            [PartitionState(FALLBACK_PARTITION, None)], current_year=2024, years_ahead=2
        )

        assert plan == [
            ("p2024", date(2025, 1, 1)),
            ("p2025", date(2026, 1, 1)),
        ]

    def test_an_empty_partition_list_still_plans_from_the_current_year(self):
        assert plan_yearly_partitions([], current_year=2026, years_ahead=1) == [
            ("p2026", date(2027, 1, 1))
        ]


class TestReorganizeSqlGuards:
    def test_no_additions_is_an_error_not_a_no_op(self):
        with pytest.raises(ValueError, match="no partitions to add"):
            reorganize_sql("ods_stock_daily_akshare", [])

    @pytest.mark.parametrize(
        "table",
        ["", "2ods", "ods daily", "ods`; DROP TABLE x; --", "`ods`"],
        ids=["empty", "leading-digit", "space", "backtick-injection", "prequoted"],
    )
    def test_unsafe_identifier_fails_closed_with_the_name(self, table):
        with pytest.raises(ValueError, match=f"invalid SQL identifier {table!r}") as excinfo:
            reorganize_sql(table, [("p2027", date(2028, 1, 1))])

        assert isinstance(excinfo.value, ValueError)

    def test_a_unicode_word_identifier_is_accepted(self):
        sql = reorganize_sql("分区表", [("p2027", date(2028, 1, 1))])

        assert sql.startswith("ALTER TABLE `分区表` REORGANIZE PARTITION pmax INTO (")

    def test_a_single_addition_still_recreates_the_fallback(self):
        sql = reorganize_sql("dwd_stock_daily", [("p2027", date(2028, 1, 1))])

        assert sql == (
            "ALTER TABLE `dwd_stock_daily` REORGANIZE PARTITION pmax INTO ("
            "PARTITION p2027 VALUES LESS THAN ('2028-01-01'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        )


class TestPartitionMaintainer:
    def test_engine_is_bound_for_reuse(self):
        engine = FakeEngine()

        assert PartitionMaintainer(engine).engine is engine

    def test_is_partitioned_reads_the_count_and_binds_the_table(self):
        engine = FakeEngine([FakeResult(scalar=4)])

        assert PartitionMaintainer(engine).is_partitioned("ods_stock_daily_akshare") is True

        mode, sql, parameters = engine.statement_log[0]
        assert mode == "connect"
        assert engine.opened_modes == ["connect"]
        assert parameters == {"table": "ods_stock_daily_akshare"}
        assert "FROM information_schema.PARTITIONS" in sql
        assert "PARTITION_NAME IS NOT NULL" in sql

    @pytest.mark.parametrize("count", [0, None], ids=["zero", "null"])
    def test_unpartitioned_table_reports_false(self, count):
        engine = FakeEngine([FakeResult(scalar=count)])

        assert PartitionMaintainer(engine).is_partitioned("plain_table") is False

    def test_state_parses_the_rows_it_reads(self):
        engine = FakeEngine(
            [
                FakeResult(
                    rows=[
                        SimpleNamespace(name="p2024", description="'2025-01-01'"),
                        SimpleNamespace(name="pmax", description="MAXVALUE"),
                    ]
                )
            ]
        )

        states = PartitionMaintainer(engine).state("dwd_stock_daily")

        assert states == [
            PartitionState("p2024", date(2025, 1, 1)),
            PartitionState("pmax", None),
        ]
        mode, sql, parameters = engine.statement_log[0]
        assert (mode, parameters) == ("connect", {"table": "dwd_stock_daily"})
        assert "PARTITION_NAME AS name" in sql
        assert "ORDER BY PARTITION_ORDINAL_POSITION" in sql

    def test_state_of_a_missing_table_is_empty(self):
        engine = FakeEngine([FakeResult(rows=[])])

        assert PartitionMaintainer(engine).state("nope") == []

    def test_ensure_no_ops_for_an_unpartitioned_table(self):
        engine = FakeEngine([FakeResult(scalar=0)])

        applied = PartitionMaintainer(engine).ensure("plain_table", current_year=2026)

        assert applied == []
        assert len(engine.statement_log) == 1
        assert engine.opened_modes == ["connect"]

    def test_ensure_no_ops_when_the_horizon_is_already_met(self):
        engine = FakeEngine(
            [
                FakeResult(scalar=4),
                FakeResult(
                    rows=[
                        SimpleNamespace(name="p2025", description="'2026-01-01'"),
                        SimpleNamespace(name="p2026", description="'2027-01-01'"),
                        SimpleNamespace(name="p2027", description="'2028-01-01'"),
                        SimpleNamespace(name="pmax", description="MAXVALUE"),
                    ]
                ),
            ]
        )

        applied = PartitionMaintainer(engine).ensure(
            "ods_stock_daily_akshare", current_year=2026, years_ahead=1
        )

        assert applied == []
        assert engine.opened_modes == ["connect", "connect"]
        assert not any("REORGANIZE" in sql for sql in engine.executed_sql)

    def test_ensure_reorganizes_pmax_inside_a_transaction(self):
        engine = FakeEngine(
            [
                FakeResult(scalar=2),
                FakeResult(
                    rows=[
                        SimpleNamespace(name="p2024", description="'2025-01-01'"),
                        SimpleNamespace(name="pmax", description="MAXVALUE"),
                    ]
                ),
                FakeResult(),
            ]
        )

        applied = PartitionMaintainer(engine).ensure("dwd_stock_daily", current_year=2026)

        assert applied == ["p2025", "p2026", "p2027"]
        assert engine.opened_modes == ["connect", "connect", "begin"]
        mode, ddl, parameters = engine.statement_log[-1]
        assert (mode, parameters) == ("begin", None)
        assert ddl == (
            "ALTER TABLE `dwd_stock_daily` REORGANIZE PARTITION pmax INTO ("
            "PARTITION p2025 VALUES LESS THAN ('2026-01-01'), "
            "PARTITION p2026 VALUES LESS THAN ('2027-01-01'), "
            "PARTITION p2027 VALUES LESS THAN ('2028-01-01'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        )

    def test_ensure_propagates_years_ahead_into_the_plan(self):
        engine = FakeEngine(
            [
                FakeResult(scalar=1),
                FakeResult(rows=[SimpleNamespace(name="pmax", description="MAXVALUE")]),
                FakeResult(),
            ]
        )

        applied = PartitionMaintainer(engine).ensure(
            "ods_minute_bar", current_year=2026, years_ahead=1
        )

        assert applied == ["p2026"]
        assert engine.executed_sql[-1] == (
            "ALTER TABLE `ods_minute_bar` REORGANIZE PARTITION pmax INTO ("
            "PARTITION p2026 VALUES LESS THAN ('2027-01-01'), "
            "PARTITION pmax VALUES LESS THAN (MAXVALUE))"
        )

    def test_ensure_refuses_an_unsafe_table_name_before_any_ddl(self):
        engine = FakeEngine(
            [
                FakeResult(scalar=1),
                FakeResult(rows=[SimpleNamespace(name="pmax", description="MAXVALUE")]),
            ]
        )

        with pytest.raises(ValueError, match="invalid SQL identifier 'bad; name'"):
            PartitionMaintainer(engine).ensure("bad; name", current_year=2026)

        assert len(engine.statement_log) == 2
        assert not any("REORGANIZE" in sql for sql in engine.executed_sql)

    def test_ensure_surfaces_a_bad_horizon_argument(self):
        engine = FakeEngine([FakeResult(scalar=1), FakeResult(rows=[])])

        with pytest.raises(ValueError, match="years_ahead must be positive, got 0"):
            PartitionMaintainer(engine).ensure("dwd_stock_daily", current_year=2026, years_ahead=0)
