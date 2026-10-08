"""Factor builder unit tests (AC-11, gate-runnable).

The pure helpers (symbol normalization, the bound window clause, the dwd
lineage stamping) come first. The build path is then driven against a
recording fake: what is under test is what the builder *asks the
warehouse* and what it hands the dwd writer - the unadjusted filter, the
widened close window, the all-zero event drop, and the fact that a
failed cumulation writes nothing. How MySQL answers those statements is
the e2e file's job.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from opendata.pipeline.factor_builder import (
    FactorBuilder,
    FactorBuildResult,
    _normalize_symbol,
    _window_clause,
    _with_dwd_trace,
)


class TestNormalizeSymbol:
    def test_strips_the_exchange_suffix(self):
        assert _normalize_symbol("600519.SH") == "600519"
        assert _normalize_symbol("000001.SZ") == "000001"

    def test_bare_symbols_pass_through(self):
        assert _normalize_symbol("600519") == "600519"


class TestWindowClause:
    def test_no_bounds_returns_no_clause(self):
        clause, params = _window_clause(None, None, "trade_date")

        assert clause == ""
        assert params == {}

    def test_start_only_binds_the_lower_bound(self):
        clause, params = _window_clause(date(2026, 1, 1), None, "trade_date")

        assert "`trade_date` >= :start" in clause
        assert params == {"start": date(2026, 1, 1)}

    def test_end_only_binds_the_upper_bound(self):
        clause, params = _window_clause(None, date(2026, 2, 1), "ex_date")

        assert "`ex_date` <= :end" in clause
        assert params == {"end": date(2026, 2, 1)}

    def test_both_bounds_are_bound_parameters(self):
        clause, params = _window_clause(date(2026, 1, 1), date(2026, 2, 1), "trade_date")

        assert "`trade_date` >= :start" in clause
        assert "`trade_date` <= :end" in clause
        assert len(params) == 2


class TestDwdTrace:
    def test_lineage_columns_are_stamped(self):
        frame = pd.DataFrame([{"symbol": "600519", "trade_date": date(2026, 9, 21)}])

        traced = _with_dwd_trace(frame)

        assert list(traced.columns) == [
            "symbol",
            "trade_date",
            "source",
            "_merged_at",
            "_diff_flag",
            "_as_of",
        ]
        assert traced.iloc[0]["source"] == "corporate-action-affine-v1"
        assert traced.iloc[0]["_diff_flag"] == 0
        assert traced.iloc[0]["_as_of"] == traced.iloc[0]["_merged_at"].date()

    def test_original_frame_is_not_mutated(self):
        frame = pd.DataFrame([{"symbol": "600519"}])

        _with_dwd_trace(frame)

        assert list(frame.columns) == ["symbol"]


class TestResult:
    def test_result_shape(self):
        result = FactorBuildResult(rows_written=2, symbols=1)

        assert result.rows_written == 2
        assert result.symbols == 1
        assert result.rows_planned == 0
        assert result.events_outside_basis == 0
        assert result.legacy_rows_preserved == 0


DAILY_TABLE = "ods_stock_daily_ths"
ACTION_TABLE = "ods_stock_action_ths"
TARGET_TABLE = "dwd_stock_adjust"


class _Rows:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple]:
        return self._rows


class _Connection:
    def __init__(self, engine: FakeWarehouse) -> None:
        self._engine = engine

    def __enter__(self) -> _Connection:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, statement: Any, params: dict[str, object] | None = None) -> _Rows:
        sql = str(statement)
        self._engine.calls.append((sql, dict(params or {})))
        return _Rows(self._engine.rows_for(sql))


class FakeWarehouse:
    """Answers the two ods reads from a script and records every statement.

    Attributes:
        calls: ``(sql, params)`` per read, in the order the builder ran them.
    """

    def __init__(
        self,
        *,
        closes: list[tuple],
        events: list[tuple],
        existing: list[tuple] | None = None,
    ) -> None:
        self.closes = closes
        self.events = events
        self.existing = existing or []
        self.calls: list[tuple[str, dict[str, object]]] = []

    def connect(self) -> _Connection:
        return _Connection(self)

    def rows_for(self, sql: str) -> list[tuple]:
        if f"FROM `{DAILY_TABLE}`" in sql:
            return self.closes
        if f"FROM `{ACTION_TABLE}`" in sql:
            return self.events
        if f"FROM `{TARGET_TABLE}`" in sql:
            return self.existing
        raise AssertionError(f"unexpected statement: {sql!r}")

    def _one(self, table: str, position: int) -> tuple[str, dict[str, object]]:
        for sql, params in self.calls:
            if f"FROM `{table}`" in sql:
                return (sql, params)[position]  # type: ignore[return-value]
        raise AssertionError(f"no read of {table}")

    def sql_for(self, table: str) -> str:
        return self._one(table, 0)

    def params_for(self, table: str) -> dict[str, object]:
        return self._one(table, 1)


def _builder(engine: FakeWarehouse) -> FactorBuilder:
    return FactorBuilder(
        engine,
        daily_table=DAILY_TABLE,
        action_table=ACTION_TABLE,
        target_table=TARGET_TABLE,
    )


#: One dividend on 2026-09-02, priced by the close of 2026-09-01.
CLOSE_ROWS = [
    ("600519.SH", date(2026, 9, 1), 100.0),
    ("600519.SH", date(2026, 9, 2), 101.0),
    ("000001.SZ", date(2026, 9, 1), 10.0),
]
EVENT_ROWS = [("600519.SH", date(2026, 9, 2), 1.5, 0.0, 0.0, 0.0, "evt-1")]


@pytest.fixture
def writer_spy(monkeypatch):
    """Captures what the builder hands the derived layer, without writing."""
    seen: list[tuple] = []

    class _Spy:
        def __init__(self, engine: object) -> None:
            seen.append(("constructed", engine))

        def write(self, frame: pd.DataFrame, *, table: str, key: tuple[str, ...]) -> int:
            seen.append(("write", frame, table, key))
            return len(frame)

    monkeypatch.setattr("opendata.pipeline.factor_builder.DwdWriter", _Spy)
    return seen


class TestOdsReads:
    def test_the_daily_read_is_filtered_to_the_unadjusted_series(self, writer_spy):
        """The factor itself carries the adjustment, so its input must be the
        unadjusted series - reading adjusted closes would double it."""
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build()

        assert "`adjusted` = 'none'" in engine.sql_for(DAILY_TABLE)
        assert "`adjusted` = 'none'" not in engine.sql_for(ACTION_TABLE)

    def test_build_reads_full_history_while_write_window_is_requested(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build(start=date(2026, 9, 2), end=date(2026, 9, 30))

        assert "`trade_date` >= :start" not in engine.sql_for(DAILY_TABLE)
        assert "`ex_date` >= :start" not in engine.sql_for(ACTION_TABLE)
        assert engine.params_for(DAILY_TABLE) == {}
        assert engine.params_for(ACTION_TABLE) == {}
        assert engine.params_for(TARGET_TABLE) == {
            "start": date(2026, 9, 2),
            "end": date(2026, 9, 30),
            "symbols": ["600519"],
        }

    def test_an_unbounded_build_reads_every_event_and_keeps_the_filter(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build()

        assert "WHERE" not in engine.sql_for(ACTION_TABLE)
        assert engine.params_for(ACTION_TABLE) == {}
        assert "WHERE" in engine.sql_for(DAILY_TABLE)
        assert ":start" not in engine.sql_for(DAILY_TABLE)

    def test_action_read_includes_event_key(self):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        events = _builder(engine)._read_events(None, None)

        assert events[0].event_key == "evt-1"
        assert "`event_key`" in engine.sql_for(ACTION_TABLE)

    def test_symbols_are_normalized_at_the_warehouse_boundary(self):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)
        builder = _builder(engine)

        closes = builder._read_closes(None, None)
        events = builder._read_events(None, None)

        assert [symbol for symbol, _day, _close in closes] == ["600519", "600519", "000001"]
        assert [event.symbol for event in events] == ["600519"]

    def test_null_payout_fields_read_as_zero(self):
        engine = FakeWarehouse(
            closes=[],
            events=[("600519.SH", date(2026, 9, 2), 1.5, None, None, None, "evt-null")],
        )

        events = _builder(engine)._read_events(None, None)

        assert (events[0].cash_dividend, events[0].bonus) == (1.5, 0.0)
        assert (events[0].allotment_ratio, events[0].allotment_price) == (0.0, 0.0)

    def test_an_event_that_pays_nothing_is_dropped(self):
        """A zero row cumulates to a no-op at best; the contract forbids it."""
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=[
                ("600519.SH", date(2026, 9, 2), 0.0, 0.0, 0.0, 0.0, "evt-zero"),
                ("600519.SH", date(2026, 9, 3), 0.0, 0.0, 0.4, 8.0, "evt-rights"),
            ],
        )

        events = _builder(engine)._read_events(None, None)

        assert [event.allotment_ratio for event in events] == [0.0, 0.4]


class TestBuild:
    def test_factors_reach_the_writer_with_their_key_and_lineage(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        result = _builder(engine).build()

        table, key, frame = _write_call(writer_spy)
        assert table == TARGET_TABLE
        assert key == ("symbol", "trade_date")
        assert list(frame.columns) == [
            "symbol",
            "trade_date",
            "qfq_factor",
            "hfq_factor",
            "qfq_scale",
            "qfq_offset",
            "hfq_scale",
            "hfq_offset",
            "adjustment_version",
            "legacy_source",
            "source",
            "_merged_at",
            "_diff_flag",
            "_as_of",
        ]
        assert result.rows_written == len(frame)
        assert result.rows_planned == len(frame)
        assert result.symbols == 2
        assert set(frame["source"]) == {"corporate-action-affine-v1"}

    def test_the_latest_day_is_qfq_anchored_at_one(self, writer_spy):
        """qfq anchors the newest price at 1.0 - the reader multiplies the
        stored bar by the factor, so the last day must stay untouched."""
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build()

        _table, _key, frame = _write_call(writer_spy)
        quoted = frame[frame["symbol"] == "600519"].sort_values("trade_date")
        assert quoted["qfq_factor"].iloc[-1] == pytest.approx(1.0)
        assert quoted["qfq_factor"].iloc[0] == pytest.approx(98.5 / 100.0)
        assert quoted["hfq_factor"].iloc[-1] == pytest.approx(100.0 / 98.5)

    def test_no_data_writes_nothing_at_all(self, writer_spy):
        engine = FakeWarehouse(closes=[], events=[])

        result = _builder(engine).build()

        assert result == FactorBuildResult(rows_written=0, symbols=0)
        assert writer_spy == []

    def test_out_of_basis_events_are_counted_not_used_to_shift_anchors(self, writer_spy):
        engine = FakeWarehouse(
            closes=[("600519.SH", date(2026, 9, 5), 100.0)],
            events=[
                ("600519.SH", date(1991, 1, 1), 1.0, 0.0, 0.0, 0.0, "old"),
                ("600519.SH", date(2026, 9, 5), 2.0, 0.0, 0.0, 0.0, "first"),
                ("600519.SH", date(2026, 9, 6), 3.0, 0.0, 0.0, 0.0, "future"),
                ("000002.SZ", date(2026, 9, 5), 4.0, 0.0, 0.0, 0.0, "no-bars"),
            ],
        )

        result = _builder(engine).build()

        assert result.events_outside_basis == 4
        _table, _key, frame = _write_call(writer_spy)
        assert (frame["qfq_scale"] == 1.0).all()
        assert (frame["qfq_offset"] == 0.0).all()

    def test_duplicate_actions_within_basis_fail_before_writer_construction(self, writer_spy):
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=[
                ("600519.SH", date(2026, 9, 2), 0.0, 0.0, 0.0, 0.0, "empty"),
                ("600519.SH", date(2026, 9, 2), 1.5, 0.0, 0.0, 0.0, "cash"),
            ],
        )

        with pytest.raises(ValueError, match="ambiguous multiple corporate actions"):
            _builder(engine).build()

        assert writer_spy == []

    @pytest.mark.parametrize("bad_price", [float("nan"), float("inf"), -1.0])
    def test_invalid_zero_event_fields_fail_before_noop_filter_and_write(
        self, writer_spy, bad_price
    ):
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=[("600519.SH", date(2026, 9, 2), 0.0, 0.0, 0.0, bad_price, "bad-zero")],
        )

        with pytest.raises(ValueError, match="allotment_price must be finite and non-negative"):
            _builder(engine).build()

        assert writer_spy == []

    def test_existing_legacy_factors_are_preserved_verbatim(self, writer_spy):
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=EVENT_ROWS,
            existing=[
                ("600519", date(2026, 9, 1), 0.7654321, 1.2345678, "ths-old", None),
            ],
        )

        result = _builder(engine).build()

        _table, _key, frame = _write_call(writer_spy)
        old = frame.loc[
            (frame["symbol"] == "600519") & (frame["trade_date"] == date(2026, 9, 1))
        ].iloc[0]
        new = frame.loc[frame["trade_date"] == date(2026, 9, 2)].iloc[0]
        assert old["qfq_factor"] == 0.7654321
        assert old["hfq_factor"] == 1.2345678
        assert old["legacy_source"] == "ths-old"
        assert new["legacy_source"] == "corporate-action-ratio-v1"
        assert result.legacy_rows_preserved == 1

    def test_existing_invalid_legacy_factors_fail_before_writer_construction(self, writer_spy):
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=EVENT_ROWS,
            existing=[("600519", date(2026, 9, 1), float("nan"), 1.0, "ths", None)],
        )

        with pytest.raises(ValueError, match="must be finite and positive"):
            _builder(engine).build()

        assert writer_spy == []

    def test_dry_run_plans_and_validates_without_constructing_writer(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        result = _builder(engine).build(dry_run=True)

        assert result.rows_written == 0
        assert result.rows_planned == 3
        assert result.symbols == 2
        assert writer_spy == []

    @pytest.mark.parametrize(
        "start, end, message",
        [
            (date(2026, 9, 3), date(2026, 9, 2), "on or before"),
            (datetime(2026, 9, 1), None, "date, not a datetime"),
        ],
    )
    def test_invalid_windows_fail_before_engine_io(self, start, end, message):
        class NoIoEngine:
            def connect(self):
                raise AssertionError("window validation must precede engine I/O")

        with pytest.raises(ValueError, match=message):
            FactorBuilder(
                NoIoEngine(),
                daily_table=DAILY_TABLE,
                action_table=ACTION_TABLE,
            ).build(start=start, end=end)

    def test_database_iso_dates_are_normalized(self):
        engine = FakeWarehouse(
            closes=[("600519.SH", "2026-09-01", 100.0)],
            events=[("600519.SH", "2026-09-02", 1.0, 0.0, 0.0, 0.0, "evt")],
        )

        closes = _builder(engine)._read_closes(None, None)
        events = _builder(engine)._read_events(None, None)

        assert closes[0][1] == date(2026, 9, 1)
        assert events[0].ex_date == date(2026, 9, 2)

    def test_sqlite_reader_and_expanding_symbol_lookup_preserve_legacy_values(self, writer_spy):
        engine = create_engine("sqlite://")
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE ods_stock_daily_ths "
                    "(thscode TEXT, trade_date DATE, close_price FLOAT, adjusted TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE ods_stock_action_ths "
                    "(thscode TEXT, ex_date DATE, dividend_per_share FLOAT, "
                    "per_share_bonus FLOAT, allotment_ratio FLOAT, allotment_price FLOAT, "
                    "event_key TEXT)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE dwd_stock_adjust "
                    "(symbol TEXT, trade_date DATE, qfq_factor FLOAT, hfq_factor FLOAT, "
                    "source TEXT, legacy_source TEXT)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO ods_stock_daily_ths VALUES "
                    "('600519.SH', '2026-09-01', 100, 'none'), "
                    "('600519.SH', '2026-09-02', 101, 'none'), "
                    "('600519.SH', '2026-09-03', 102, 'none')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO ods_stock_action_ths VALUES "
                    "('600519.SH', '2026-09-02', 1.5, 0, 0, 0, 'evt-1')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO dwd_stock_adjust VALUES "
                    "('600519', '2026-09-02', 0.876543, 1.234567, 'historical-ths', NULL)"
                )
            )

        result = FactorBuilder(
            engine,
            daily_table=DAILY_TABLE,
            action_table=ACTION_TABLE,
            target_table=TARGET_TABLE,
        ).build(start=date(2026, 9, 2), end=date(2026, 9, 2))

        assert result.rows_planned == 1
        assert result.rows_written == 1
        assert result.legacy_rows_preserved == 1
        _table, _key, frame = _write_call(writer_spy)
        assert frame.iloc[0]["qfq_factor"] == pytest.approx(0.876543)
        assert frame.iloc[0]["hfq_factor"] == pytest.approx(1.234567)
        assert frame.iloc[0]["legacy_source"] == "historical-ths"
        engine.dispose()


def _write_call(seen: list[tuple]) -> tuple[str, tuple[str, ...], pd.DataFrame]:
    """The single dwd write a build issued."""
    calls = [entry for entry in seen if entry[0] == "write"]
    assert len(calls) == 1, f"expected exactly one dwd write, saw {seen}"
    return calls[0][2], calls[0][3], calls[0][1]
