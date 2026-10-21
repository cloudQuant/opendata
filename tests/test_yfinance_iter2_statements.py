"""Independent source-response fixtures for the first yfinance Iteration 2 slice."""

from __future__ import annotations

import datetime
import types
from contextlib import nullcontext
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.providers.yfinance.models import _sdk
from opendata.data.providers.yfinance.models.iter2 import (
    YfinanceBalanceSheetFetcher,
    YfinanceCashFlowStatementFetcher,
    YfinanceHistoricalDividendsFetcher,
    YfinanceIncomeStatementFetcher,
)
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.providers.yfinance.provider import PROVIDER


@pytest.fixture
def balance_sheet_sdk_response() -> pd.DataFrame:
    """Handwritten Ticker.get_balance_sheet response with date columns."""
    return pd.DataFrame(
        {
            pd.Timestamp("2024-12-31"): [1000.0, 250.0],
            pd.Timestamp("2023-12-31"): [900.0, 200.0],
        },
        index=["Total Assets", "Cash And Cash Equivalents"],
    )


@pytest.fixture
def cash_flow_sdk_response() -> pd.DataFrame:
    """Handwritten Ticker.get_cash_flow response, distinct from the other fixtures."""
    return pd.DataFrame(
        {
            pd.Timestamp("2024-09-30"): [120.5, 60.25],
            pd.Timestamp("2023-09-30"): [105.0, None],
        },
        index=["Operating Cash Flow", "Free Cash Flow"],
    )


@pytest.fixture
def income_statement_sdk_response() -> pd.DataFrame:
    """Handwritten Ticker.get_income_stmt response including one provider-side gap."""
    return pd.DataFrame(
        {
            pd.Timestamp("2025-03-31"): [75.0, 12.0],
            pd.Timestamp("2024-03-31"): [68.5, float("nan")],
        },
        index=["Total Revenue", "Net Income"],
    )


@pytest.fixture
def dividends_sdk_response() -> pd.Series:
    """Handwritten Ticker.dividends response with actual historical event dates."""
    return pd.Series(
        [0.24, 0.25, 0.26],
        index=pd.DatetimeIndex(["2024-03-15", "2024-06-14", "2024-09-13"]),
        name="Dividends",
    )


@pytest.fixture
def sdk_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A recording stand-in for the documented yfinance Ticker methods."""
    calls: dict[str, Any] = {}

    class Ticker:
        def __init__(self, symbol: str) -> None:
            calls["symbol"] = symbol

        def get_balance_sheet(self, **kwargs: object) -> pd.DataFrame:
            calls["balance_sheet_kwargs"] = kwargs
            return balance_sheet_response

        def get_cashflow(self, **kwargs: object) -> pd.DataFrame:
            calls["cash_flow_kwargs"] = kwargs
            return cash_flow_response

        def get_income_stmt(self, **kwargs: object) -> pd.DataFrame:
            calls["income_stmt_kwargs"] = kwargs
            return income_statement_response

        @property
        def dividends(self) -> pd.Series:
            calls["dividends_read"] = True
            return dividends_response

    balance_sheet_response = pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [1000.0]}, index=["Total Assets"]
    )
    cash_flow_response = pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [120.5]}, index=["Operating Cash Flow"]
    )
    income_statement_response = pd.DataFrame(
        {pd.Timestamp("2024-12-31"): [75.0]}, index=["Total Revenue"]
    )
    dividends_response = pd.Series(
        [0.24, 0.25], index=pd.DatetimeIndex(["2024-03-15", "2024-06-14"])
    )
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: types.SimpleNamespace(Ticker=Ticker))
    monkeypatch.setattr(
        _sdk,
        "governed_yfinance_session",
        lambda _fake_sdk: nullcontext(None),
    )
    return calls


class TestStatementResponses:
    def test_balance_sheet_has_an_independent_sdk_fixture(
        self, balance_sheet_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda symbol, statement, *, frequency: balance_sheet_sdk_response,
        )
        fetcher = YfinanceBalanceSheetFetcher()
        query = fetcher.transform_query(symbol=" aapl ")
        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

        assert len(rows) == 4
        assert rows[0].symbol == "AAPL"
        assert rows[0].statement_type == "balance_sheet"
        assert rows[0].report_period == datetime.date(2023, 12, 31)
        assert rows[-1].value == 1000.0
        assert {row.unit_status for row in rows} == {"not_provided_by_sdk"}

    def test_cash_flow_preserves_report_period_and_nullable_line_item(
        self, cash_flow_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda symbol, statement, *, frequency: cash_flow_sdk_response,
        )
        fetcher = YfinanceCashFlowStatementFetcher()
        query = fetcher.transform_query(symbol="MSFT")
        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

        gap = next(row for row in rows if row.line_item == "Free Cash Flow" and row.value is None)
        assert gap.report_period == datetime.date(2023, 9, 30)
        assert gap.frequency == "yearly"
        latest = next(
            row
            for row in rows
            if row.line_item == "Operating Cash Flow"
            and row.report_period == datetime.date(2024, 9, 30)
        )
        assert latest.value == 120.5

    def test_income_statement_keeps_original_labels_and_null_cells(
        self, income_statement_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda symbol, statement, *, frequency: income_statement_sdk_response,
        )
        fetcher = YfinanceIncomeStatementFetcher()
        query = fetcher.transform_query(symbol="nvda")
        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

        gap = next(row for row in rows if row.line_item == "Net Income" and row.value is None)
        assert gap.symbol == "NVDA"
        assert gap.report_period == datetime.date(2024, 3, 31)
        assert gap.frequency == "yearly"
        latest = next(
            row
            for row in rows
            if row.line_item == "Total Revenue" and row.report_period == datetime.date(2025, 3, 31)
        )
        assert latest.value == 75.0

    @pytest.mark.parametrize(
        ("fetcher_type", "bad_frame", "expected_code"),
        [
            (YfinanceBalanceSheetFetcher, pd.DataFrame(), "YFINANCE_EMPTY_RESPONSE"),
            (
                YfinanceCashFlowStatementFetcher,
                pd.DataFrame({"not-a-date": [1.0]}, index=["Operating Cash Flow"]),
                "YFINANCE_REPORT_PERIOD_INVALID",
            ),
            (
                YfinanceIncomeStatementFetcher,
                pd.DataFrame(
                    {pd.Timestamp("2024-12-31"): ["unknown-unit/value"]}, index=["Revenue"]
                ),
                "YFINANCE_VALUE_INVALID",
            ),
        ],
    )
    def test_each_statement_rejects_its_invalid_response_shape(
        self,
        fetcher_type: type,
        bad_frame: pd.DataFrame,
        expected_code: str,
    ) -> None:
        fetcher = fetcher_type()
        query = fetcher.transform_query(symbol="AAPL")
        with pytest.raises(YfinanceProviderError) as error:
            fetcher.transform_data(bad_frame, query)
        assert error.value.code == expected_code

    def test_statement_limit_keeps_the_newest_periods(
        self, balance_sheet_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda symbol, statement, *, frequency: balance_sheet_sdk_response,
        )
        fetcher = YfinanceBalanceSheetFetcher()
        query = fetcher.transform_query(symbol="AAPL", limit=1)

        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
        assert {row.report_period for row in rows} == {datetime.date(2024, 12, 31)}

    def test_statement_period_maps_quarter_and_typed_frequency(
        self, balance_sheet_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda symbol, statement, *, frequency: (
                calls.append(frequency) or balance_sheet_sdk_response
            ),
        )
        fetcher = YfinanceBalanceSheetFetcher()
        query = fetcher.transform_query(symbol="AAPL", period="quarter", limit=2)

        raw = fetcher.extract_data(query, _sdk_context())
        rows = fetcher.transform_data(raw, query)

        assert calls == ["quarterly"]
        assert rows
        assert {row.frequency for row in rows} == {"quarterly"}
        assert query.effective_frequency == "quarterly"

    def test_statement_query_rejects_bad_arguments_before_sdk_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[object] = []
        monkeypatch.setattr(
            _sdk,
            "require_statement_frame",
            lambda *args, **kwargs: calls.append((args, kwargs)),
        )
        fetcher = YfinanceBalanceSheetFetcher()

        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", frequency="monthly")
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", period="monthly")
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", start_date="2025-01-02")
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", unexpected="typo")
        assert calls == []

    def test_documented_method_names_and_freq_are_passed_to_sdk(
        self, sdk_calls: dict[str, Any]
    ) -> None:
        queries = (
            (YfinanceBalanceSheetFetcher(), "balance_sheet"),
            (YfinanceCashFlowStatementFetcher(), "cash_flow_statement"),
            (YfinanceIncomeStatementFetcher(), "income_statement"),
        )
        for fetcher, statement in queries:
            query = fetcher.transform_query(symbol="aapl")
            fetcher.extract_data(query, _sdk_context())
            method_key = {
                "balance_sheet": "balance_sheet_kwargs",
                "cash_flow_statement": "cash_flow_kwargs",
                "income_statement": "income_stmt_kwargs",
            }[statement]
            assert sdk_calls[method_key] == {
                "as_dict": False,
                "pretty": False,
                "freq": "yearly",
            }
        assert sdk_calls["symbol"] == "AAPL"


class TestHistoricalDividends:
    def test_historical_series_becomes_dated_per_share_events(
        self, dividends_sdk_response: pd.Series, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_sdk, "require_dividends_series", lambda symbol: dividends_sdk_response)
        fetcher = YfinanceHistoricalDividendsFetcher()
        query = fetcher.transform_query(symbol="aapl")
        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

        assert [row.ex_dividend_date for row in rows] == [
            datetime.date(2024, 3, 15),
            datetime.date(2024, 6, 14),
            datetime.date(2024, 9, 13),
        ]
        assert rows[1].amount == 0.25
        assert "ex_dividend_date" in rows[1].model_dump(mode="json")
        assert "amount" in rows[1].model_dump(mode="json")
        assert "event_date" not in rows[1].model_dump(mode="json")
        assert "amount_per_share" not in rows[1].model_dump(mode="json")
        assert [row.event_date for row in rows] == [
            datetime.date(2024, 3, 15),
            datetime.date(2024, 6, 14),
            datetime.date(2024, 9, 13),
        ]
        assert rows[1].amount_per_share == 0.25
        assert rows[1].currency is None
        assert rows[1].currency_status == "not_provided_by_sdk"

    def test_date_window_keeps_only_real_events_inclusive(
        self, dividends_sdk_response: pd.Series, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_sdk, "require_dividends_series", lambda symbol: dividends_sdk_response)
        fetcher = YfinanceHistoricalDividendsFetcher()
        query = fetcher.transform_query(
            symbol="AAPL", start_date="2024-06-14", end_date="2024-09-13"
        )

        rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
        assert [row.ex_dividend_date for row in rows] == [
            datetime.date(2024, 6, 14),
            datetime.date(2024, 9, 13),
        ]

    def test_wrong_empty_or_undated_dividend_shapes_fail_closed(self) -> None:
        fetcher = YfinanceHistoricalDividendsFetcher()
        query = fetcher.transform_query(symbol="AAPL")
        for raw, expected_code in (
            (None, "YFINANCE_DIVIDENDS_SHAPE_INVALID"),
            ([], "YFINANCE_DIVIDENDS_SHAPE_INVALID"),
            (pd.Series(dtype="float64"), "YFINANCE_EMPTY_OR_DUPLICATE_RESPONSE"),
        ):
            with pytest.raises(YfinanceProviderError) as error:
                fetcher.transform_data(raw, query)  # type: ignore[arg-type]
            assert error.value.code == expected_code

        invalid_index = pd.Series([0.25], index=["unknown-date"])
        with pytest.raises(YfinanceProviderError) as error:
            fetcher.transform_data(invalid_index, query)
        assert error.value.code == "YFINANCE_EX_DIVIDEND_DATE_INVALID"

    def test_bad_dividend_query_is_rejected_before_sdk_io(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[str] = []
        monkeypatch.setattr(_sdk, "require_dividends_series", lambda symbol: calls.append(symbol))
        fetcher = YfinanceHistoricalDividendsFetcher()

        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", start_date="2024-09-13", end_date="2024-03-15")
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="AAPL", misspelled=True)
        assert calls == []

    def test_sdk_wrapper_rejects_unexpected_response_shape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class Ticker:
            def __init__(self, symbol: str) -> None:
                assert symbol == "AAPL"

            @property
            def dividends(self) -> list[float]:
                return [0.25]

        monkeypatch.setattr(_sdk, "_require_sdk", lambda: types.SimpleNamespace(Ticker=Ticker))
        monkeypatch.setattr(
            _sdk,
            "governed_yfinance_session",
            lambda _fake_sdk: nullcontext(None),
        )
        with pytest.raises(YfinanceProviderError) as error:
            _sdk.require_dividends_series("AAPL")
        assert error.value.code == "YFINANCE_DIVIDENDS_SHAPE_INVALID"

    def test_sdk_wrapper_uses_ticker_dividends_not_quote_snapshot(
        self, sdk_calls: dict[str, Any]
    ) -> None:
        series = _sdk.require_dividends_series("AAPL")
        assert isinstance(series, pd.Series)
        assert sdk_calls["dividends_read"] is True


class TestProviderBindings:
    def test_first_slice_is_reachable_by_fixed_model_identity(self) -> None:
        bindings = {
            model_id: binding
            for binding in PROVIDER.fetcher_bindings
            for model_id in binding.canonical_model_ids
        }
        assert {
            "BalanceSheet",
            "CashFlowStatement",
            "HistoricalDividends",
            "IncomeStatement",
        } <= set(bindings)
        assert (
            bindings["BalanceSheet"].load("yfinance").capability.domain == "yfinance_balance_sheet"
        )

    def test_each_canonical_binding_has_a_consuming_scenario(self) -> None:
        bindings = [binding for binding in PROVIDER.fetcher_bindings if binding.canonical_model_ids]

        assert len(bindings) == 29
        assert all(binding.scenario and binding.scenario.strip() for binding in bindings)


def _sdk_context() -> Any:
    """Avoid an SDK/network dependency while exercising the synchronous stage methods."""
    from opendata.data.protocol import FetchContext

    return FetchContext(timeout=3.0)
