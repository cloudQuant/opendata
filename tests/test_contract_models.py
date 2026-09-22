"""Contract model tests (A1.1 / AC-2).

Dual-form serialization (DataFrame <-> pydantic), field-set guard rails
and fail-closed behaviour of the contract base.
"""

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models import (
    AdjustFactor,
    Bar,
    ContractModel,
    CorporateAction,
    FinancialIndicator,
    FinancialStatement,
    FuturesFundamentals,
    IndexConstituent,
    Instrument,
    TradingCalendar,
)

ALL_MODELS = [
    Bar,
    AdjustFactor,
    CorporateAction,
    FinancialStatement,
    FinancialIndicator,
    IndexConstituent,
    Instrument,
    TradingCalendar,
    FuturesFundamentals,
]


def sample(model, **overrides):
    """Build one representative row per model."""
    data = {
        Bar: {
            "symbol": "600519.SH",
            "trade_date": date(2024, 6, 3),
            "open": 1700.0,
            "high": 1720.5,
            "low": 1695.0,
            "close": 1710.2,
            "volume": 3_200_000.0,
            "amount": 547_264_000.0,
        },
        AdjustFactor: {
            "symbol": "600519.SH",
            "trade_date": date(2024, 6, 3),
            "qfq_factor": 1.0,
            "hfq_factor": 13.5,
        },
        CorporateAction: {
            "symbol": "600519.SH",
            "ex_date": date(2024, 6, 27),
            "cash_dividend": 25.911,
            "stock_dividend": 0.0,
        },
        FinancialStatement: {
            "symbol": "600519.SH",
            "statement_type": "income",
            "report_period": date(2024, 3, 31),
            "announce_date": date(2024, 4, 26),
            "item": "total_revenue",
            "value": 46_400_000_000.0,
        },
        FinancialIndicator: {
            "symbol": "600519.SH",
            "report_period": date(2024, 3, 31),
            "announce_date": date(2024, 4, 26),
            "indicator": "eps",
            "value": 23.02,
            "unit": "元",
        },
        IndexConstituent: {
            "index_symbol": "000300.SH",
            "symbol": "600519.SH",
            "as_of": date(2024, 5, 31),
            "weight": 4.85,
        },
        Instrument: {
            "symbol": "600519.SH",
            "exchange": "SSE",
            "name": "贵州茅台",
            "status": "active",
            "currency": "CNY",
            "list_date": date(2001, 8, 27),
            "board": "主板",
        },
        TradingCalendar: {
            "exchange": "SSE",
            "date": date(2024, 6, 8),
            "is_open": False,
            "prev_trade_date": date(2024, 6, 7),
            "next_trade_date": date(2024, 6, 11),
        },
        FuturesFundamentals: {
            "symbol": "RB2410",
            "exchange": "SHFE",
            "trade_date": date(2024, 6, 3),
            "open_interest": 1_850_000.0,
            "open_interest_change": -12_500.0,
            "settlement": 3_612.0,
        },
    }
    return model(**{**data[model], **overrides})


class TestRoundTrip:
    @pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
    def test_frame_round_trip(self, model):
        rows = [sample(model), sample(model)]
        restored = model.from_frame(model.to_frame(rows))
        assert restored == rows

    @pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
    def test_empty_frame_round_trip(self, model):
        df = model.to_frame([])
        assert list(df.columns) == list(model.model_fields)
        assert model.from_frame(df) == []
        assert model.from_frame(pd.DataFrame()) == []

    @pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
    def test_extra_columns_are_ignored(self, model):
        df = model.to_frame([sample(model)])
        df["source"] = "akshare"
        df["_merged_at"] = pd.Timestamp("2026-09-22")
        assert model.from_frame(df) == [sample(model)]

    @pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
    def test_timestamps_coerced_to_date(self, model):
        df = model.to_frame([sample(model)])
        for column in df.columns:
            if df[column].dtype == object:
                df[column] = df[column].map(lambda v: pd.Timestamp(v) if isinstance(v, date) else v)
        rows = model.from_frame(df)
        assert rows == [sample(model)]
        assert all(
            not isinstance(value, pd.Timestamp)
            for row in rows
            for value in row.model_dump().values()
        )


class TestFailClosed:
    @pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
    def test_missing_required_column_raises(self, model):
        df = model.to_frame([sample(model)])
        first_required = next(
            name for name, field in model.model_fields.items() if field.is_required()
        )
        df = df.drop(columns=[first_required])
        with pytest.raises(ValueError, match="missing required columns"):
            model.from_frame(df)

    def test_unknown_field_rejected(self):
        with pytest.raises(ValidationError):
            Bar(symbol="600519.SH", trade_date=date(2024, 6, 3), close=1.0, bogus=1.0)

    def test_nan_in_nullable_becomes_none(self):
        df = Instrument.to_frame([sample(Instrument)])
        df["list_date"] = [float("nan")]
        df["board"] = [None]
        rows = Instrument.from_frame(df)
        assert rows[0].list_date is None
        assert rows[0].board is None

    def test_nan_in_required_still_fails(self):
        df = Bar.to_frame([sample(Bar)])
        df["close"] = [float("nan")]
        with pytest.raises(ValidationError):
            Bar.from_frame(df)

    def test_missing_optional_column_uses_default(self):
        df = CorporateAction.to_frame([sample(CorporateAction, cash_dividend=1.0)])
        df = df.drop(columns=["rights_price"])
        rows = CorporateAction.from_frame(df)
        assert rows[0].rights_price == 0.0


class TestFieldSets:
    def test_bar_is_unadjusted_ohlcv_only(self):
        # D10: no adjusted-price field may sneak into the contract.
        assert set(Bar.model_fields) == {
            "symbol",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "amount",
        }

    def test_instrument_field_set_matches_design(self):
        assert set(Instrument.model_fields) == {
            "symbol",
            "exchange",
            "name",
            "list_date",
            "delist_date",
            "board",
            "status",
            "currency",
        }

    def test_trading_calendar_field_set_matches_design(self):
        assert set(TradingCalendar.model_fields) == {
            "exchange",
            "date",
            "is_open",
            "prev_trade_date",
            "next_trade_date",
        }

    def test_financial_models_have_three_time_dimensions(self):
        for model in (FinancialStatement, FinancialIndicator):
            assert {"report_period", "announce_date", "revision"} <= set(model.model_fields)

    def test_index_constituent_is_snapshot_dated(self):
        assert "as_of" in IndexConstituent.model_fields


class TestSemantics:
    def test_statement_revision_defaults_to_first_release(self):
        row = sample(FinancialStatement)
        assert row.revision == 1

    def test_calendar_non_trading_day(self):
        row = sample(TradingCalendar, is_open=False)
        assert row.prev_trade_date < row.date < row.next_trade_date

    def test_contract_model_is_strict_base(self):
        assert issubclass(Bar, ContractModel)
        assert Bar.model_config["extra"] == "forbid"
