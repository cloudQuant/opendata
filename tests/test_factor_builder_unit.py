"""Factor builder unit tests (AC-11, gate-runnable).

The warehouse round trip is e2e; these cover the pure helpers that the
build path depends on - symbol normalization, the bound window clause
and the dwd lineage stamping.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from opendata.pipeline.factor_builder import (
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
