"""Data query layer tests (A4.9, design §10.1).

The query layer turns a validated request into a paginated SELECT:
identifiers are validated against the real schema, ``symbols`` go
through bind parameters (never string interpolation), ``fields`` are
whitelisted against the contract model / table columns, a default
time window keeps an unfiltered query from scanning every partition,
and the ``adjust`` option synthesizes qfq/hfq series from Bar +
AdjustFactor (D10) instead of reading pre-adjusted columns.
"""

from datetime import date

import pytest

from opendata.pipeline.query import (
    MAX_PAGE_SIZE,
    DataQuery,
    build_data_select,
    resolve_time_field,
    validate_fields,
    window_bounds,
)

TODAY = date(2026, 9, 23)


class TestWindowBounds:
    def test_default_window_is_applied_when_no_range_is_given(self):
        start, end = window_bounds(DataQuery(domain="stock_daily", layer="dwd"), today=TODAY)

        assert end == TODAY
        assert (end - start).days == 365

    def test_explicit_range_is_honoured(self):
        start, end = window_bounds(
            DataQuery(
                domain="stock_daily",
                layer="dwd",
                start=date(2024, 1, 1),
                end=date(2024, 3, 31),
            ),
            today=TODAY,
        )

        assert (start, end) == (date(2024, 1, 1), date(2024, 3, 31))

    def test_reversed_range_fails_closed(self):
        with pytest.raises(ValueError, match="start"):
            window_bounds(
                DataQuery(
                    domain="stock_daily",
                    layer="dwd",
                    start=date(2024, 3, 31),
                    end=date(2024, 1, 1),
                ),
                today=TODAY,
            )


class TestFieldWhitelist:
    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValueError, match="unknown fields"):
            validate_fields(("open", "evil"), available=("open", "close", "trade_date"))

    def test_known_fields_pass_in_table_order(self):
        fields = validate_fields(
            ("close", "open"),
            available=("symbol", "trade_date", "open", "close"),
            always_include=("symbol", "trade_date"),
        )

        assert fields == ["symbol", "trade_date", "open", "close"]

    def test_always_included_columns_stay_without_being_requested(self):
        fields = validate_fields(
            ("close",), available=("symbol", "trade_date", "close"), always_include=("symbol",)
        )

        assert fields == ["symbol", "close"]  # trade_date was not requested

    def test_no_selection_means_every_column(self):
        assert validate_fields((), available=("symbol", "close")) == ["symbol", "close"]


class TestBuildDataSelect:
    def _query(self, **overrides) -> DataQuery:
        base = {
            "domain": "stock_daily",
            "layer": "dwd",
            "symbols": ("600519", "000001"),
            "start": date(2024, 1, 1),
            "end": date(2024, 1, 31),
        }
        base.update(overrides)
        return DataQuery(**base)

    def test_symbols_and_window_are_bind_parameters(self):
        sql, params = build_data_select(
            self._query(),
            table="dwd_stock_daily",
            columns=("symbol", "trade_date", "close"),
            key=("symbol", "trade_date"),
        )

        assert "`symbol` IN (:symbol_0, :symbol_1)" in sql
        assert params["symbol_0"] == "600519"
        assert params["start"] == date(2024, 1, 1)
        assert "600519" not in sql  # no interpolation anywhere

    def test_injection_attempt_stays_a_literal_parameter(self):
        sql, params = build_data_select(
            self._query(symbols=("600519'); DROP TABLE x; --",)),
            table="dwd_stock_daily",
            columns=("symbol", "trade_date", "close"),
            key=("symbol", "trade_date"),
        )

        assert "DROP TABLE" not in sql
        assert params["symbol_0"] == "600519'); DROP TABLE x; --"

    def test_pagination_is_bounded(self):
        _, params = build_data_select(
            self._query(page=2, page_size=MAX_PAGE_SIZE * 10),
            table="dwd_stock_daily",
            columns=("symbol", "trade_date", "close"),
            key=("symbol", "trade_date"),
        )

        assert params["limit"] == MAX_PAGE_SIZE
        assert params["offset"] == MAX_PAGE_SIZE  # page 2

    def test_invalid_page_fails_closed(self):
        with pytest.raises(ValueError, match="page"):
            build_data_select(
                self._query(page=0),
                table="dwd_stock_daily",
                columns=("symbol", "trade_date", "close"),
                key=("symbol", "trade_date"),
            )

    def test_order_by_business_key(self):
        sql, _ = build_data_select(
            self._query(),
            table="dwd_stock_daily",
            columns=("symbol", "trade_date", "close"),
            key=("symbol", "trade_date"),
        )

        assert "ORDER BY `symbol`, `trade_date`" in sql

    def test_unsafe_table_name_fails_closed(self):
        with pytest.raises(ValueError, match="identifier"):
            build_data_select(
                self._query(),
                table="dwd`; DROP TABLE x; --",
                columns=("symbol", "close"),
                key=("symbol",),
            )

    def test_layer_selects_the_table(self):
        with pytest.raises(ValueError, match="layer"):
            build_data_select(
                self._query(layer="raw"),
                table="dwd_stock_daily",
                columns=("symbol", "close"),
                key=("symbol",),
            )


class TestAdjust:
    def test_bar_domains_resolve_their_date_field(self):
        assert resolve_time_field("stock_daily") == "trade_date"
        assert resolve_time_field("index_constituent") == "as_of"

    def test_no_adjust_passes_the_rows_through(self):
        from opendata.pipeline.query import apply_adjust_to_rows

        rows = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1.0,
                "volume": 100.0,
                "amount": 100.0,
            }
        ]

        adjusted = apply_adjust_to_rows("stock_daily", rows, method="none", factors=[])

        assert adjusted[0]["close"] == 1.0
        assert set(adjusted[0]) == set(rows[0])  # passthrough keeps the row shape

    def test_qfq_scales_by_the_factor(self):
        from opendata.pipeline.query import apply_adjust_to_rows

        rows = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "open": 2.0,
                "high": 2.0,
                "low": 2.0,
                "close": 2.0,
                "volume": 100.0,
                "amount": 100.0,
            }
        ]
        factors = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "qfq_factor": 0.5,
                "hfq_factor": 2.0,
                "qfq_scale": 0.5,
                "qfq_offset": 0.0,
                "hfq_scale": 2.0,
                "hfq_offset": 0.0,
                "adjustment_version": "affine-v1",
            }
        ]

        adjusted = apply_adjust_to_rows("stock_daily", rows, method="qfq", factors=factors)

        assert adjusted[0]["close"] == 1.0
        assert adjusted[0]["volume"] == 100.0  # volume untouched (D10)

    def test_missing_factor_fails_closed(self):
        from opendata.pipeline.query import apply_adjust_to_rows

        rows = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "open": 2.0,
                "high": 2.0,
                "low": 2.0,
                "close": 2.0,
                "volume": 100.0,
                "amount": 100.0,
            }
        ]

        with pytest.raises(ValueError, match="factor"):
            apply_adjust_to_rows("stock_daily", rows, method="qfq", factors=[])

    def test_a_domain_the_mapping_declares_unadjustable_refuses_qfq(self):
        """The price basis comes from the 口径 table, not a hardcoded domain list.

        ``index_daily`` declares ``adjust: not_applicable`` (an index has no
        corporate action to synthesize), so asking for a qfq series of it is a
        parameter mistake the query layer has to refuse rather than multiply
        factors onto a level series.
        """
        from opendata.pipeline.query import apply_adjust_to_rows

        rows = [
            {
                "symbol": "000300",
                "trade_date": date(2026, 9, 24),
                "open": 4618.73,
                "high": 4640.08,
                "low": 4604.77,
                "close": 4611.44,
                "volume": 1.0,
                "amount": 1.0,
            }
        ]
        factors = [{"symbol": "000300", "trade_date": date(2026, 9, 24), "qfq_factor": 0.5}]

        with pytest.raises(RuntimeError, match="adjust='not_applicable'"):
            apply_adjust_to_rows("index_daily", rows, method="qfq", factors=factors)

    def test_the_unadjusted_stock_daily_basis_is_what_allows_synthesis(self):
        from opendata.data.mapping import require_adjust_basis
        from opendata.pipeline.query import apply_adjust_to_rows

        assert require_adjust_basis("stock_daily") == "unadjusted"
        assert require_adjust_basis("index_daily") == "not_applicable"
        # stock_daily is adjustable; the same call on it must not raise.
        rows = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "open": 2.0,
                "high": 2.0,
                "low": 2.0,
                "close": 2.0,
                "volume": 1.0,
                "amount": 1.0,
            }
        ]
        factors = [
            {
                "symbol": "600519",
                "trade_date": date(2024, 1, 2),
                "qfq_factor": 0.5,
                "hfq_factor": 1.0,
            }
        ]

        assert (
            apply_adjust_to_rows("stock_daily", rows, method="qfq", factors=factors)[0]["close"]
            == 1.0
        )
