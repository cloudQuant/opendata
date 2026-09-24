"""Source field-mapping tests (A4.5, design §8.2).

The mapping table is the per-source contract of the cross-check and
(dwd merge) path: it renames source columns to the contract fields,
converts units and normalizes business keys. Two counterexamples from
the design must fail loudly rather than pass silently:

* a source whose column names do not match the mapping (rejected, not
  ignored);
* a unit that was not converted (the comparison then reports a
  deviation, covered in ``test_cross_check``).
"""

from datetime import date

import pandas as pd
import pytest

from opendata.data.mapping import (
    DEFAULT_TOLERANCE,
    FieldMapping,
    load_mapping,
    normalize_frame,
    require_domain_mapping,
)


class TestLoadMapping:
    def test_loads_the_shipped_akshare_mapping(self):
        mapping = load_mapping("akshare")

        assert mapping.source == "akshare"
        assert "stock_daily" in mapping.domains
        assert "index_constituent" in mapping.domains

    def test_stock_daily_mapping_matches_the_real_em_columns(self):
        domain = require_domain_mapping("akshare", "stock_daily")

        assert domain.key == ("symbol", "trade_date")
        assert domain.fields["close"].source_column == "收盘"
        assert domain.fields["volume"].scale == 100  # 手 -> 股
        assert domain.fields["symbol"].normalize == "plain"

    def test_price_fields_get_a_looser_tolerance(self):
        domain = require_domain_mapping("akshare", "stock_daily")

        assert domain.tolerance("close") == pytest.approx(1e-4)
        assert domain.tolerance("volume") == DEFAULT_TOLERANCE

    def test_unknown_source_fails_closed(self):
        with pytest.raises(LookupError, match="unknown source"):
            load_mapping("not_a_source")

    def test_unknown_domain_fails_closed(self):
        with pytest.raises(LookupError, match="unknown domain"):
            require_domain_mapping("akshare", "not_a_domain")


class TestNormalizeFrame:
    def test_renames_converts_units_and_normalizes_keys(self):
        raw = pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "股票代码": ["600519.SH"],
                "开盘": [1685.0],
                "最高": [1690.0],
                "最低": [1680.0],
                "收盘": [1688.0],
                "成交量": [30000.0],
                "成交额": [5.06e9],
            }
        )

        normalized = normalize_frame(raw, require_domain_mapping("akshare", "stock_daily"))

        assert set(normalized.columns) >= {"symbol", "trade_date", "close", "volume"}
        row = normalized.iloc[0]
        assert row["symbol"] == "600519"  # 600519.SH -> 600519
        assert row["volume"] == 3_000_000.0  # lots -> shares
        assert row["close"] == 1688.0

    def test_missing_source_column_fails_closed(self):
        """Counterexample 1: unmapped column names must not pass silently."""
        raw = pd.DataFrame({"日期": ["2024-01-02"], "股票代码": ["600519"], "开pan": [1.0]})

        with pytest.raises(ValueError, match="收盘"):
            normalize_frame(raw, require_domain_mapping("akshare", "stock_daily"))

    def test_contract_columns_keep_the_mapping_order(self):
        mapping = require_domain_mapping("akshare", "index_constituent")
        raw = pd.DataFrame(
            {
                "日期": [date(2024, 7, 31)],
                "指数代码": ["000300"],
                "成分券代码": ["600519"],
                "权重": [4.15],
            }
        )

        normalized = normalize_frame(raw, mapping)

        assert list(normalized.columns) == ["index_symbol", "symbol", "as_of", "weight"]


class TestFieldMappingModel:
    def test_defaults_are_identity(self):
        field = FieldMapping(source_column="收盘")

        assert field.scale == 1.0
        assert field.normalize is None


class TestDenormalizeFrame:
    """The write-side inverse that lands contract rows in an ods table."""

    def test_source_key_is_the_ods_spelling_of_the_business_key(self):
        ths = require_domain_mapping("ths", "stock_daily")
        akshare = require_domain_mapping("akshare", "stock_daily")
        assert ths.source_key == ("thscode", "trade_date")
        assert akshare.source_key == ("股票代码", "日期")

    def test_contract_rows_return_to_the_source_columns_with_units_undone(self):
        from opendata.data.mapping import denormalize_frame

        mapping = require_domain_mapping("akshare", "stock_daily")
        source = pd.DataFrame(
            {
                "股票代码": ["600519.SH"],
                "日期": [date(2024, 1, 2)],
                "开盘": [1.0],
                "最高": [2.0],
                "最低": [0.5],
                "收盘": [1.5],
                "成交量": [10.0],
                "成交额": [15.0],
            }
        )
        back = denormalize_frame(normalize_frame(source, mapping), mapping)
        assert list(back.columns) == list(source.columns)
        # The 手 -> 股 scale is undone; a plain-normalized key cannot be
        # re-suffixed, which is why this path serves feeds that deliver
        # the source symbol (the fuyao bars), not the akshare writes.
        assert list(back["成交量"]) == [10.0]
        assert list(back["股票代码"]) == ["600519"]

    def test_a_declared_millisecond_column_is_derived_back(self):
        from opendata.data.mapping import denormalize_frame
        from opendata.pipeline.dump_import import shanghai_dates

        mapping = require_domain_mapping("ths", "stock_daily")
        frame = pd.DataFrame(
            {
                "symbol": ["600519.SH"],
                "trade_date": [date(2024, 1, 2)],
                "open": [1.0],
                "high": [2.0],
                "low": [0.5],
                "close": [1.5],
                "volume": [10.0],
                "amount": [15.0],
            }
        )
        projected = denormalize_frame(frame, mapping)
        assert list(shanghai_dates(projected["date_ms"])) == [date(2024, 1, 2)]
        assert projected["date_ms"].dtype == "int64"

    def test_a_missing_contract_field_fails_closed(self):
        from opendata.data.mapping import denormalize_frame

        mapping = require_domain_mapping("ths", "stock_daily")
        with pytest.raises(ValueError, match="fail closed"):
            denormalize_frame(pd.DataFrame([{"symbol": "600519.SH"}]), mapping)
