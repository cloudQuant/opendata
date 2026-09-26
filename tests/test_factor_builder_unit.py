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

from datetime import date, timedelta
from typing import Any

import pandas as pd
import pytest

from opendata.pipeline.factor_builder import (
    LOOKBACK_DAYS,
    FactorBuilder,
    FactorBuildResult,
    _lookback,
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
        assert traced.iloc[0]["source"] == "ths"
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

    def __init__(self, *, closes: list[tuple], events: list[tuple]) -> None:
        self.closes = closes
        self.events = events
        self.calls: list[tuple[str, dict[str, object]]] = []

    def connect(self) -> _Connection:
        return _Connection(self)

    def rows_for(self, sql: str) -> list[tuple]:
        if f"FROM `{DAILY_TABLE}`" in sql:
            return self.closes
        if f"FROM `{ACTION_TABLE}`" in sql:
            return self.events
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
EVENT_ROWS = [("600519.SH", date(2026, 9, 2), 1.5, 0.0, 0.0, 0.0)]


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


class TestLookback:
    def test_a_window_start_is_widened_by_the_documented_days(self):
        assert _lookback(date(2026, 10, 1)) == date(2026, 10, 1) - timedelta(days=LOOKBACK_DAYS)

    def test_an_open_start_stays_open(self):
        assert _lookback(None) is None


class TestOdsReads:
    def test_the_daily_read_is_filtered_to_the_unadjusted_series(self, writer_spy):
        """The factor itself carries the adjustment, so its input must be the
        unadjusted series - reading adjusted closes would double it."""
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build()

        assert "`adjusted` = 'none'" in engine.sql_for(DAILY_TABLE)
        assert "`adjusted` = 'none'" not in engine.sql_for(ACTION_TABLE)

    def test_only_the_close_window_is_widened(self, writer_spy):
        """Pricing the first event needs a close before it; the event window
        must not shift, or a build would re-price events outside its scope."""
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build(start=date(2026, 9, 2), end=date(2026, 9, 30))

        assert engine.params_for(DAILY_TABLE) == {
            "start": date(2026, 9, 2) - timedelta(days=LOOKBACK_DAYS),
            "end": date(2026, 9, 30),
        }
        assert engine.params_for(ACTION_TABLE) == {
            "start": date(2026, 9, 2),
            "end": date(2026, 9, 30),
        }

    def test_an_unbounded_build_reads_every_event_and_keeps_the_filter(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build()

        assert "WHERE" not in engine.sql_for(ACTION_TABLE)
        assert engine.params_for(ACTION_TABLE) == {}
        assert "WHERE" in engine.sql_for(DAILY_TABLE)
        assert ":start" not in engine.sql_for(DAILY_TABLE)

    def test_a_start_without_an_end_bounds_only_the_lower_side(self, writer_spy):
        engine = FakeWarehouse(closes=CLOSE_ROWS, events=EVENT_ROWS)

        _builder(engine).build(start=date(2026, 9, 2))

        assert "`trade_date` >= :start" in engine.sql_for(DAILY_TABLE)
        assert "`trade_date` <=" not in engine.sql_for(DAILY_TABLE)
        assert engine.params_for(DAILY_TABLE) == {"start": date(2026, 8, 18)}
        assert "`ex_date` >= :start" in engine.sql_for(ACTION_TABLE)

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
            events=[("600519.SH", date(2026, 9, 2), 1.5, None, None, None)],
        )

        events = _builder(engine)._read_events(None, None)

        assert (events[0].cash_dividend, events[0].bonus) == (1.5, 0.0)
        assert (events[0].allotment_ratio, events[0].allotment_price) == (0.0, 0.0)

    def test_an_event_that_pays_nothing_is_dropped(self):
        """A zero row cumulates to a no-op at best; the contract forbids it."""
        engine = FakeWarehouse(
            closes=CLOSE_ROWS,
            events=[
                ("600519.SH", date(2026, 9, 2), 0.0, 0.0, 0.0, 0.0),
                ("600519.SH", date(2026, 9, 3), 0.0, 0.0, 0.4, 8.0),
            ],
        )

        events = _builder(engine)._read_events(None, None)

        assert [event.allotment_ratio for event in events] == [0.4]


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
            "source",
            "_merged_at",
            "_diff_flag",
            "_as_of",
        ]
        assert result.rows_written == len(frame)
        assert result.symbols == 2

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

    def test_an_unpriceable_event_writes_nothing(self, writer_spy):
        """The ex-date precedes every close: fail closed, leave the table alone."""
        engine = FakeWarehouse(
            closes=[("600519.SH", date(2026, 9, 5), 100.0)],
            events=[("600519.SH", date(2026, 9, 2), 1.5, 0.0, 0.0, 0.0)],
        )

        with pytest.raises(ValueError, match="no close before ex-date"):
            _builder(engine).build()

        assert writer_spy == []


def _write_call(seen: list[tuple]) -> tuple[str, tuple[str, ...], pd.DataFrame]:
    """The single dwd write a build issued."""
    calls = [entry for entry in seen if entry[0] == "write"]
    assert len(calls) == 1, f"expected exactly one dwd write, saw {seen}"
    return calls[0][2], calls[0][3], calls[0][1]
