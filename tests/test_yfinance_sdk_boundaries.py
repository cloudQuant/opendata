"""Offline behavior tests for the yfinance SDK boundary helpers."""

from __future__ import annotations

import builtins
import sys
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pandas as pd
import pytest
import requests

from opendata.data.providers.yfinance.models import _sdk
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


@pytest.fixture
def sdk_harness(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Provide an isolated SDK-shaped surface and a governed session recorder."""
    harness: dict[str, Any] = {
        "events": [],
        "session": object(),
        "ticker": None,
        "data_client": None,
    }

    class YfData:
        _instances: dict[type, object] = {}

    def make_ticker(symbol: str, **kwargs: object) -> object:
        harness["events"].append(("ticker", symbol, kwargs))
        ticker = harness["ticker"]
        assert ticker is not None
        return ticker

    fake_sdk = SimpleNamespace(
        Ticker=make_ticker,
        data=SimpleNamespace(YfData=YfData),
        const=SimpleNamespace(_BASE_URL_="https://fixture.invalid"),
    )
    harness["sdk"] = fake_sdk
    harness["yf_data_type"] = YfData

    @contextmanager
    def governed(_sdk: object) -> Iterator[object | None]:
        harness["events"].append(("session_enter",))
        try:
            yield harness["session"]
        finally:
            harness["events"].append(("session_exit",))

    def ensure_session(_sdk: object, session: object, *, initialize: bool) -> None:
        harness["events"].append(("ensure_session", session, initialize))
        data_client = harness["data_client"]
        if initialize and data_client is not None:
            YfData._instances[YfData] = data_client

    monkeypatch.setattr(_sdk, "_require_sdk", lambda: fake_sdk)
    monkeypatch.setattr(_sdk, "governed_yfinance_session", governed)
    monkeypatch.setattr(_sdk, "_ensure_yfdata_session", ensure_session)
    return harness


def _provider_code(call: Any, expected_code: str) -> YfinanceProviderError:
    with pytest.raises(YfinanceProviderError) as error:
        call()
    assert error.value.code == expected_code
    return error.value


def _point(as_of: str, raw: object) -> dict[str, object]:
    return {"asOfDate": as_of, "reportedValue": {"raw": raw}}


def test_history_frame_translates_inclusive_dates_and_preserves_raw_options(
    sdk_harness: dict[str, Any],
) -> None:
    frame = pd.DataFrame({"Close": [187.0]}, index=pd.DatetimeIndex(["2024-01-04"]))
    calls: list[dict[str, object]] = []

    class Ticker:
        def history(self, **kwargs: object) -> pd.DataFrame:
            calls.append(kwargs)
            return frame

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_history_frame(
        " AAPL ",
        start=date(2024, 1, 1),
        end=date(2024, 1, 4),
        timeout=4.5,
        interval="1h",
        prepost=True,
        actions=False,
        auto_adjust=False,
    )

    assert result is frame
    assert sdk_harness["events"][:2] == [
        ("session_enter",),
        ("ticker", "AAPL", {"session": sdk_harness["session"]}),
    ]
    assert calls == [
        {
            "interval": "1h",
            "prepost": True,
            "actions": False,
            "auto_adjust": False,
            "start": "2024-01-01",
            "end": "2024-01-05",
            "timeout": 4.5,
        }
    ]
    assert sdk_harness["events"][-1] == ("session_exit",)
    assert _sdk._ACTIVE_SDK.get() is None


@pytest.mark.parametrize(
    ("history", "expected_code"),
    [(None, None), ([{"Close": 1}], "YFINANCE_HISTORY_SHAPE_INVALID")],
)
def test_history_frame_accepts_empty_result_and_rejects_non_frame(
    history: object,
    expected_code: str | None,
    sdk_harness: dict[str, Any],
) -> None:
    class Ticker:
        def history(self, **kwargs: object) -> object:
            assert kwargs == {
                "interval": "1d",
                "prepost": False,
                "actions": True,
                "auto_adjust": False,
            }
            return history

    sdk_harness["ticker"] = Ticker()
    result = (
        _sdk.require_history_frame("MSFT", start=None, end=None, timeout=None)
        if expected_code is None
        else None
    )
    if expected_code is None:
        assert result is None
    else:
        error = _provider_code(
            lambda: _sdk.require_history_frame("MSFT", start=None, end=None, timeout=None),
            expected_code,
        )
        assert error.__cause__ is None


def test_history_snapshot_reuses_governed_context_and_translates_http_error(
    sdk_harness: dict[str, Any],
) -> None:
    transport = requests.exceptions.HTTPError("429 response fixture")

    class Ticker:
        def history(self, **kwargs: object) -> None:
            raise transport

    sdk_harness["ticker"] = Ticker()
    error = _provider_code(
        lambda: _sdk.require_history_snapshot(
            "AAPL", date(2024, 1, 1), date(2024, 1, 2), timeout=2.0
        ),
        "YFINANCE_REQUEST_FAILED",
    )
    assert error.__cause__ is transport
    assert [event for event in sdk_harness["events"] if event[0] == "session_enter"] == [
        ("session_enter",)
    ]
    assert _sdk._ACTIVE_SDK.get() is None


def test_ticker_without_session_uses_the_plain_constructor(
    sdk_harness: dict[str, Any],
) -> None:
    sdk_harness["session"] = None
    history_calls: list[dict[str, object]] = []

    class Ticker:
        def history(self, **kwargs: object) -> None:
            history_calls.append(kwargs)
            return None

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_history_frame("AAPL", start=None, end=None, timeout=None)
    assert result is None
    assert history_calls == [
        {"interval": "1d", "prepost": False, "actions": True, "auto_adjust": False}
    ]
    assert sdk_harness["events"][:2] == [("session_enter",), ("ticker", "AAPL", {})]
    assert not any(event[0] == "ensure_session" for event in sdk_harness["events"])


@pytest.mark.parametrize(
    ("split_payload", "expected"),
    [
        (None, []),
        ([], []),
        (
            pd.DataFrame(
                {"Other": [7.0], "Stock Splits": [4.0]},
                index=pd.DatetimeIndex(["2020-08-31"]),
            ),
            [4.0],
        ),
        (
            pd.DataFrame(
                {"First Provider Field": [0.5]},
                index=pd.DatetimeIndex(["2000-06-30"]),
            ),
            [0.5],
        ),
    ],
)
def test_split_history_normalizes_supported_source_shapes(
    split_payload: object, expected: list[float], sdk_harness: dict[str, Any]
) -> None:
    class Ticker:
        @property
        def splits(self) -> object:
            return split_payload

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_split_history(" AAPL ")
    assert result.tolist() == expected
    assert str(result.dtype) == "float64"


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (RequestBudgetError("budget exhausted"), None),
        (requests.exceptions.HTTPError("HTTP 503 fixture"), "YFINANCE_SPLITS_UNAVAILABLE"),
    ],
)
def test_split_history_keeps_budget_failures_distinct_from_transport_failures(
    failure: Exception, expected_code: str | None, sdk_harness: dict[str, Any]
) -> None:
    class Ticker:
        @property
        def splits(self) -> object:
            raise failure

    sdk_harness["ticker"] = Ticker()
    if expected_code is None:
        with pytest.raises(RequestBudgetError) as error:
            _sdk.require_split_history("AAPL")
        assert error.value is failure
    else:
        error = _provider_code(lambda: _sdk.require_split_history("AAPL"), expected_code)
        assert error.__cause__ is failure


@pytest.mark.parametrize(
    ("statement_type", "method_name"),
    [
        ("balance_sheet", "get_balance_sheet"),
        ("cash_flow_statement", "get_cashflow"),
        ("income_statement", "get_income_stmt"),
    ],
)
def test_statement_frame_calls_the_documented_sdk_method(
    statement_type: str, method_name: str, sdk_harness: dict[str, Any]
) -> None:
    frame = pd.DataFrame({"2024-12-31": [1000.0]}, index=["Total Assets"])
    calls: list[tuple[str, dict[str, object]]] = []

    class Ticker:
        def __getattr__(self, name: str) -> Any:
            if name != method_name:
                raise AssertionError(f"unexpected Ticker method: {name}")

            def fetch(**kwargs: object) -> pd.DataFrame:
                calls.append((name, kwargs))
                return frame

            return fetch

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_statement_frame(" aapl ", statement_type, frequency="quarterly")
    assert result is frame
    assert calls == [(method_name, {"as_dict": False, "pretty": False, "freq": "quarterly"})]
    assert sdk_harness["events"][1] == (
        "ticker",
        "aapl",
        {"session": sdk_harness["session"]},
    )


def test_statement_frame_rejects_unknown_type_before_constructing_ticker(
    sdk_harness: dict[str, Any],
) -> None:
    error = _provider_code(
        lambda: _sdk.require_statement_frame("AAPL", "cash_flow", frequency="yearly"),
        "YFINANCE_BAD_STATEMENT_TYPE",
    )
    assert error.__cause__ is None
    assert not any(event[0] == "ticker" for event in sdk_harness["events"])


@pytest.mark.parametrize(
    ("statement", "expected_code"),
    [(None, None), (["not", "a", "frame"], "YFINANCE_STATEMENT_SHAPE_INVALID")],
)
def test_statement_frame_accepts_absent_data_and_rejects_wrong_container(
    statement: object, expected_code: str | None, sdk_harness: dict[str, Any]
) -> None:
    class Ticker:
        def get_balance_sheet(self, **kwargs: object) -> object:
            return statement

    sdk_harness["ticker"] = Ticker()
    if expected_code is None:
        assert _sdk.require_statement_frame("AAPL", "balance_sheet", frequency="yearly") is None
    else:
        _provider_code(
            lambda: _sdk.require_statement_frame("AAPL", "balance_sheet", frequency="yearly"),
            expected_code,
        )


@pytest.mark.parametrize(
    ("attribute", "call", "empty_value", "bad_value", "shape_code"),
    [
        (
            "dividends",
            lambda: _sdk.require_dividends_series("AAPL"),
            None,
            [0.25],
            "YFINANCE_DIVIDENDS_SHAPE_INVALID",
        ),
        (
            "get_news",
            lambda: _sdk.require_news("AAPL", count=3),
            [],
            {"news": []},
            "YFINANCE_NEWS_SHAPE_INVALID",
        ),
    ],
)
def test_series_and_news_helpers_validate_documented_shapes(
    attribute: str,
    call: Any,
    empty_value: object,
    bad_value: object,
    shape_code: str,
    sdk_harness: dict[str, Any],
) -> None:
    good_value: object
    if attribute == "dividends":
        good_value = pd.Series([0.24], index=pd.DatetimeIndex(["2024-03-15"]))

        class Ticker:
            @property
            def dividends(self) -> object:
                return good_value

    else:
        good_value = [{"id": "story-1", "title": "Fixture headline"}]

        class Ticker:
            def get_news(self, **kwargs: object) -> object:
                assert kwargs == {"count": 3, "tab": "news"}
                return good_value

    sdk_harness["ticker"] = Ticker()
    result = call()
    if attribute == "dividends":
        assert isinstance(result, pd.Series)
        pd.testing.assert_series_equal(result, good_value)
    else:
        assert result == good_value

    if attribute == "dividends":

        class EmptyTicker:
            @property
            def dividends(self) -> object:
                return empty_value

        class BadTicker:
            @property
            def dividends(self) -> object:
                return bad_value

    else:

        class EmptyTicker:
            def get_news(self, **kwargs: object) -> object:
                return empty_value

        class BadTicker:
            def get_news(self, **kwargs: object) -> object:
                return bad_value

    sdk_harness["ticker"] = EmptyTicker()
    assert call() == empty_value
    sdk_harness["ticker"] = BadTicker()
    _provider_code(call, shape_code)


def test_info_requires_nonempty_mapping_and_returns_a_plain_dict(
    sdk_harness: dict[str, Any],
) -> None:
    source = {"longName": "Example Holdings", "marketCap": 123456}

    class Ticker:
        def get_info(self) -> Mapping[str, object]:
            return source

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_info("AAPL")
    assert result == source
    assert result is not source

    class WrongTicker:
        def get_info(self) -> object:
            return [("symbol", "AAPL")]

    class EmptyTicker:
        def get_info(self) -> Mapping[str, object]:
            return {}

    sdk_harness["ticker"] = WrongTicker()
    _provider_code(lambda: _sdk.require_info("AAPL"), "YFINANCE_INFO_SHAPE_INVALID")
    sdk_harness["ticker"] = EmptyTicker()
    _provider_code(lambda: _sdk.require_info("AAPL"), "YFINANCE_EMPTY_RESPONSE")


def test_fast_info_materializes_mapping_and_rejects_empty_or_unreadable_values(
    sdk_harness: dict[str, Any],
) -> None:
    class Ticker:
        @property
        def fast_info(self) -> Mapping[str, object]:
            return {"lastPrice": 10.25, "currency": "USD"}

    sdk_harness["ticker"] = Ticker()
    assert _sdk.require_fast_info("AAPL") == {"lastPrice": 10.25, "currency": "USD"}

    class EmptyTicker:
        @property
        def fast_info(self) -> Mapping[str, object]:
            return {}

    class UnreadableTicker:
        @property
        def fast_info(self) -> object:
            raise requests.exceptions.HTTPError("HTTP 502 fixture")

    sdk_harness["ticker"] = EmptyTicker()
    _provider_code(lambda: _sdk.require_fast_info("AAPL"), "YFINANCE_EMPTY_RESPONSE")
    sdk_harness["ticker"] = UnreadableTicker()
    error = _provider_code(lambda: _sdk.require_fast_info("AAPL"), "YFINANCE_REQUEST_FAILED")
    assert isinstance(error.__cause__, requests.exceptions.HTTPError)


def test_fund_sections_keeps_available_fields_and_rejects_empty_result(
    sdk_harness: dict[str, Any],
) -> None:
    class Funds:
        asset_classes = {"stock": 0.98, "cash": 0.02}
        description = "Fixture fund"
        fund_operations = {"annualReportExpenseRatio": 0.0003}
        fund_overview = {"categoryName": "Large Blend"}
        equity_holdings = [{"symbol": "AAPL", "holdingPercent": 0.07}]
        top_holdings = [{"symbol": "AAPL", "holdingPercent": 0.07}]
        sector_weightings = {"technology": 0.3}
        bond_holdings = None
        bond_ratings = None

    class Ticker:
        funds_data = Funds()

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_fund_sections("SPY")
    assert result["asset_classes"] == {"stock": 0.98, "cash": 0.02}
    assert result["description"] == "Fixture fund"
    assert result["bond_holdings"] is None
    assert set(result) == {
        "asset_classes",
        "description",
        "fund_operations",
        "fund_overview",
        "equity_holdings",
        "top_holdings",
        "sector_weightings",
        "bond_holdings",
        "bond_ratings",
    }

    class EmptyFunds:
        asset_classes = None
        description = None
        fund_operations = None
        fund_overview = None
        equity_holdings = None
        top_holdings = None
        sector_weightings = None
        bond_holdings = None
        bond_ratings = None

    class EmptyTicker:
        funds_data = EmptyFunds()

    sdk_harness["ticker"] = EmptyTicker()
    _provider_code(lambda: _sdk.require_fund_sections("SPY"), "YFINANCE_EMPTY_RESPONSE")


class _FakeEquityQuery:
    def __init__(self, operator: str, operands: list[object]) -> None:
        self.operator = operator
        self.operands = operands


def test_screen_builds_nested_query_and_uses_custom_screen_pagination(
    sdk_harness: dict[str, Any],
) -> None:
    screen_calls: list[tuple[object, dict[str, object]]] = []
    response = {"quotes": [{"symbol": "AAPL", "marketCap": 2_900_000_000_000}]}

    def screen(query: object, **kwargs: object) -> dict[str, object]:
        screen_calls.append((query, kwargs))
        return response

    sdk_harness["sdk"].EquityQuery = _FakeEquityQuery
    sdk_harness["sdk"].screen = screen
    query = {
        "operator": "AND",
        "operands": [
            {"operator": "GT", "operands": ["intradaymarketcap", 1_000_000]},
            {"operator": "EQ", "operands": ["region", "us"]},
        ],
    }
    result = _sdk.require_screen(
        query,
        offset=20,
        limit=10,
        predefined=False,
        sort_field="intradaymarketcap",
        sort_ascending=False,
    )

    built = screen_calls[0][0]
    assert isinstance(built, _FakeEquityQuery)
    assert built.operator == "AND"
    nested_operators = [
        operand.operator for operand in built.operands if isinstance(operand, _FakeEquityQuery)
    ]
    assert nested_operators == [
        "GT",
        "EQ",
    ]
    assert result["quotes"] == [{"symbol": "AAPL", "marketCap": 2_900_000_000_000}]
    assert screen_calls[0][1] == {
        "offset": 20,
        "size": 10,
        "sortField": "intradaymarketcap",
        "sortAsc": False,
        "session": sdk_harness["session"],
    }


def test_screen_predefined_call_and_response_contract(
    sdk_harness: dict[str, Any],
) -> None:
    query = "day_gainers"
    calls: list[tuple[object, dict[str, object]]] = []

    def screen(value: object, **kwargs: object) -> dict[str, object]:
        calls.append((value, kwargs))
        return {"quotes": [{"symbol": "NVDA"}], "total": 1}

    sdk_harness["sdk"].screen = screen
    result = _sdk.require_screen(
        query,
        offset=5,
        limit=25,
        predefined=True,
        sort_field="dayvolume",
        sort_ascending=True,
    )
    assert result == {"quotes": [{"symbol": "NVDA"}], "total": 1}
    assert calls == [
        (
            query,
            {
                "offset": 5,
                "count": 25,
                "sortField": "dayvolume",
                "sortAsc": True,
                "session": sdk_harness["session"],
            },
        )
    ]


def test_screen_without_active_governance_omits_the_session_keyword(
    sdk_harness: dict[str, Any],
) -> None:
    sdk_harness["session"] = None
    calls: list[dict[str, object]] = []

    def screen(query: object, **kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"quotes": [{"symbol": "AAPL"}]}

    sdk_harness["sdk"].screen = screen
    raw_screen = _sdk.require_screen.__wrapped__
    result = raw_screen("day_gainers", offset=0, limit=5, predefined=True)
    assert result == {"quotes": [{"symbol": "AAPL"}]}
    assert calls == [{"offset": 0, "count": 5, "sortField": None, "sortAsc": None}]


@pytest.mark.parametrize(
    ("query", "sdk_query", "expected_code"),
    [
        (
            {"operator": "EQ", "operands": ["region", "us"]},
            False,
            "YFINANCE_SCREEN_QUERY_UNAVAILABLE",
        ),
        ({"operator": 1, "operands": ["region", "us"]}, True, "YFINANCE_SCREEN_QUERY_INVALID"),
        ({"operator": "EQ"}, True, "YFINANCE_SCREEN_QUERY_INVALID"),
    ],
)
def test_screen_rejects_unavailable_and_malformed_query_trees(
    query: dict[str, object],
    sdk_query: bool,
    expected_code: str,
    sdk_harness: dict[str, Any],
) -> None:
    if sdk_query:
        sdk_harness["sdk"].EquityQuery = _FakeEquityQuery
    else:
        sdk_harness["sdk"].EquityQuery = None
    _provider_code(
        lambda: _sdk.require_screen(query, offset=0, limit=5, predefined=False),
        expected_code,
    )
    assert not any(event[0] == "ticker" for event in sdk_harness["events"])


@pytest.mark.parametrize(
    "bad_response",
    [None, [], {}, {"quotes": None}, {"quotes": "AAPL"}, {"quotes": ["AAPL"]}],
)
def test_screen_rejects_malformed_response_envelopes(
    bad_response: object, sdk_harness: dict[str, Any]
) -> None:
    sdk_harness["sdk"].screen = lambda *_args, **_kwargs: bad_response
    _provider_code(
        lambda: _sdk.require_screen("day_gainers", offset=0, limit=5, predefined=True),
        "YFINANCE_SCREEN_SHAPE_INVALID",
    )


def test_valuation_measures_builds_current_and_dated_golden_frame(
    sdk_harness: dict[str, Any],
) -> None:
    payload = {
        "timeseries": {
            "result": [
                {"meta": {"symbol": ["AAPL"]}, "timestamp": [1700000000]},
                {
                    "quarterlyMarketCap": [
                        _point("2024-09-30", 120.0),
                        _point("2024-06-30", 110.0),
                    ],
                    "quarterlyEnterpriseValue": [_point("2024-09-30", 150.0)],
                    "quarterlyPeRatio": [_point("2024-06-30", 29.5)],
                    "trailingMarketCap": [
                        _point("2025-01-02", 130.0),
                        _point("2025-01-04", 140.0),
                    ],
                    "trailingForwardPeRatio": [_point("2025-01-03", 19.25)],
                    "monthlyPriceToUnknownThing": [_point("2024-09-30", 999.0)],
                    "quarterlyPsRatio": [
                        "malformed point",
                        {"asOfDate": "2024-09-30", "reportedValue": {"raw": None}},
                        {"asOfDate": None, "reportedValue": {"raw": 5.0}},
                        {"asOfDate": "2024-09-30", "reportedValue": "missing raw value"},
                    ],
                    "unprefixedMarketCap": [_point("2024-09-30", 999.0)],
                    "quarterlyUnknownMeasure": [_point("2024-09-30", 999.0)],
                    "quarterlyPbRatio": "not a list",
                },
                None,
            ]
        }
    }
    calls: list[tuple[str, dict[str, object], float]] = []

    class DataClient:
        def get_raw_json(self, url: str, *, params: dict[str, object], timeout: float) -> object:
            calls.append((url, params, timeout))
            return payload

    sdk_harness["data_client"] = DataClient()
    result = _sdk.require_valuation_measures("AAPL", frequency="quarterly", periods=2)

    assert result is not None
    expected = pd.DataFrame(
        {
            "Current": [140.0, None, None, 19.25, None, None, None, None, None],
            "9/30/2024": [120.0, 150.0, None, None, None, None, None, None, None],
            "6/30/2024": [110.0, None, 29.5, None, None, None, None, None, None],
        },
        index=[
            "Market Cap",
            "Enterprise Value",
            "Trailing P/E",
            "Forward P/E",
            "PEG Ratio (5yr expected)",
            "Price/Sales",
            "Price/Book",
            "Enterprise Value/Revenue",
            "Enterprise Value/EBITDA",
        ],
        dtype=object,
    )
    pd.testing.assert_frame_equal(result, expected)
    assert calls[0][0] == (
        "https://fixture.invalid/ws/fundamentals-timeseries/v1/finance/timeseries/AAPL"
    )
    assert calls[0][1]["symbol"] == "AAPL"
    assert calls[0][1]["type"] == ",".join(
        name
        for suffix in (
            "MarketCap",
            "EnterpriseValue",
            "PeRatio",
            "ForwardPeRatio",
            "PegRatio",
            "PsRatio",
            "PbRatio",
            "EnterprisesValueRevenueRatio",
            "EnterprisesValueEBITDARatio",
        )
        for name in (f"quarterly{suffix}", f"trailing{suffix}")
    )
    assert calls[0][1]["period1"] == 1483113600
    assert isinstance(calls[0][1]["period2"], int)
    assert calls[0][2] == 30.0
    assert ("ensure_session", sdk_harness["session"], True) in sdk_harness["events"]


@pytest.mark.parametrize(
    ("frequency", "periods", "payload"),
    [
        (
            "yearly",
            -1,
            {"timeseries": {"result": [{"annualMarketCap": [_point("2023-12-31", 80)]}]}},
        ),
        (
            "trailing",
            3,
            {"timeseries": {"result": [{"trailingMarketCap": [_point("2025-02-01", 99)]}]}},
        ),
    ],
)
def test_valuation_frequency_and_period_cap_keep_source_semantics(
    frequency: str,
    periods: int,
    payload: dict[str, object],
    sdk_harness: dict[str, Any],
) -> None:
    class DataClient:
        def get_raw_json(self, url: str, *, params: dict[str, object], timeout: float) -> object:
            assert params["type"].startswith(
                "annualMarketCap" if frequency == "yearly" else "trailingMarketCap"
            )
            return payload

    sdk_harness["data_client"] = DataClient()
    result = _sdk.require_valuation_measures("IBM", frequency=frequency, periods=periods)
    assert result is not None
    if frequency == "yearly":
        assert result.loc["Market Cap"].tolist() == [None, 80]
        assert list(result.columns) == ["Current", "12/31/2023"]
    else:
        assert result.loc["Market Cap"].tolist() == [99]
        assert list(result.columns) == ["Current"]


def test_valuation_empty_result_and_invalid_envelope_are_distinct(
    sdk_harness: dict[str, Any],
) -> None:
    class DataClient:
        payload: object = {"timeseries": {"result": []}}

        def get_raw_json(self, url: str, *, params: dict[str, object], timeout: float) -> object:
            return self.payload

    client = DataClient()
    sdk_harness["data_client"] = client
    assert _sdk.require_valuation_measures("AAPL", frequency="monthly", periods=2) is None

    for invalid in [None, [], {}, {"timeseries": {"result": None}}]:
        client.payload = invalid
        _provider_code(
            lambda: _sdk.require_valuation_measures("AAPL", frequency="monthly", periods=2),
            "YFINANCE_METRICS_SHAPE_INVALID",
        )


def test_valuation_rejects_unsupported_frequency_and_missing_session_singleton(
    sdk_harness: dict[str, Any],
) -> None:
    _provider_code(
        lambda: _sdk.require_valuation_measures("AAPL", frequency="weekly", periods=2),
        "YFINANCE_METRICS_FREQ_UNSUPPORTED",
    )

    sdk_harness["data_client"] = None
    _provider_code(
        lambda: _sdk.require_valuation_measures("AAPL", frequency="quarterly", periods=2),
        "YFINANCE_SESSION_SINGLETON_UNAVAILABLE",
    )


def test_valuation_guard_refuses_to_run_outside_the_governed_wrapper(
    sdk_harness: dict[str, Any],
) -> None:
    original = _sdk.require_valuation_measures.__wrapped__
    error = _provider_code(
        lambda: original("AAPL", frequency="quarterly", periods=1),
        "YFINANCE_SESSION_NOT_GOVERNED",
    )
    assert error.__cause__ is None


def test_valuation_request_preserves_budget_error_and_translates_http_error(
    sdk_harness: dict[str, Any],
) -> None:
    for failure, expected_code in [
        (RequestBudgetError("request denied"), None),
        (requests.exceptions.HTTPError("HTTP 503 fixture"), "YFINANCE_REQUEST_FAILED"),
    ]:

        class DataClient:
            def get_raw_json(
                self,
                url: str,
                *,
                params: dict[str, object],
                timeout: float,
                _failure: Exception = failure,
            ) -> object:
                raise _failure

        sdk_harness["data_client"] = DataClient()
        if expected_code is None:
            with pytest.raises(RequestBudgetError) as error:
                _sdk.require_valuation_measures("AAPL", frequency="quarterly", periods=1)
            assert error.value is failure
        else:
            error = _provider_code(
                lambda: _sdk.require_valuation_measures("AAPL", frequency="quarterly", periods=1),
                expected_code,
            )
            assert error.__cause__ is failure


def test_options_chain_returns_both_frames_and_checks_the_pair(
    sdk_harness: dict[str, Any],
) -> None:
    calls = pd.DataFrame({"contractSymbol": ["AAPL-CALL"], "strike": [100.0]})
    puts = pd.DataFrame({"contractSymbol": ["AAPL-PUT"], "strike": [90.0]})
    seen: list[str] = []

    class Chain:
        def __init__(self) -> None:
            self.calls = calls
            self.puts = puts

    class Ticker:
        def option_chain(self, expiration: str) -> Chain:
            seen.append(expiration)
            return Chain()

    sdk_harness["ticker"] = Ticker()
    actual_calls, actual_puts = _sdk.require_options_chain("AAPL", "2027-01-15")
    pd.testing.assert_frame_equal(actual_calls, calls)
    pd.testing.assert_frame_equal(actual_puts, puts)
    assert seen == ["2027-01-15"]

    class BadChain:
        def __init__(self) -> None:
            self.calls = []
            self.puts = puts

    class BadTicker:
        def option_chain(self, expiration: str) -> BadChain:
            return BadChain()

    sdk_harness["ticker"] = BadTicker()
    _provider_code(
        lambda: _sdk.require_options_chain("AAPL", "2027-01-15"),
        "YFINANCE_OPTIONS_SHAPE_INVALID",
    )


def test_price_targets_requires_all_five_documented_fields(
    sdk_harness: dict[str, Any],
) -> None:
    target_summary = {"current": 190.0, "low": 175.0, "high": 225.0, "mean": 205.0, "median": 200.0}

    class Ticker:
        def get_analyst_price_targets(self) -> Mapping[str, object]:
            return target_summary

    sdk_harness["ticker"] = Ticker()
    result = _sdk.require_price_targets("AAPL")
    assert result == target_summary
    assert result is not target_summary

    class WrongTicker:
        def get_analyst_price_targets(self) -> object:
            return [("current", 190)]

    class IncompleteTicker:
        def get_analyst_price_targets(self) -> Mapping[str, object]:
            return {"current": 190.0, "low": 175.0, "high": 225.0, "mean": 205.0}

    sdk_harness["ticker"] = WrongTicker()
    _provider_code(
        lambda: _sdk.require_price_targets("AAPL"),
        "YFINANCE_PRICE_TARGET_SHAPE_INVALID",
    )
    sdk_harness["ticker"] = IncompleteTicker()
    _provider_code(
        lambda: _sdk.require_price_targets("AAPL"),
        "YFINANCE_PRICE_TARGET_SHAPE_INVALID",
    )


@pytest.mark.parametrize(
    ("attribute", "invoke", "mapped_code"),
    [
        (
            "get_balance_sheet",
            lambda: _sdk.require_statement_frame("AAPL", "balance_sheet", frequency="yearly"),
            "YFINANCE_REQUEST_FAILED",
        ),
        ("dividends", lambda: _sdk.require_dividends_series("AAPL"), "YFINANCE_REQUEST_FAILED"),
        ("get_news", lambda: _sdk.require_news("AAPL", count=2), "YFINANCE_REQUEST_FAILED"),
        ("get_info", lambda: _sdk.require_info("AAPL"), "YFINANCE_REQUEST_FAILED"),
        ("fast_info", lambda: _sdk.require_fast_info("AAPL"), "YFINANCE_REQUEST_FAILED"),
        ("funds_data", lambda: _sdk.require_fund_sections("SPY"), "YFINANCE_REQUEST_FAILED"),
        (
            "option_chain",
            lambda: _sdk.require_options_chain("AAPL", "2027-01-15"),
            "YFINANCE_REQUEST_FAILED",
        ),
        (
            "get_analyst_price_targets",
            lambda: _sdk.require_price_targets("AAPL"),
            "YFINANCE_REQUEST_FAILED",
        ),
    ],
)
@pytest.mark.parametrize(
    ("failure", "preserves_budget"),
    [
        (RequestBudgetError("request denied"), True),
        (requests.exceptions.HTTPError("HTTP 429 fixture"), False),
    ],
)
def test_ticker_failures_keep_budget_and_translate_http_errors(
    attribute: str,
    invoke: Any,
    mapped_code: str,
    failure: Exception,
    preserves_budget: bool,
    sdk_harness: dict[str, Any],
) -> None:
    class Ticker:
        def __getattr__(self, name: str) -> Any:
            if name != attribute:
                raise AssertionError(f"unexpected Ticker access: {name}")
            if name in {"dividends", "fast_info", "funds_data"}:
                raise failure

            def fail(*_args: object, **_kwargs: object) -> object:
                raise failure

            return fail

    sdk_harness["ticker"] = Ticker()
    if preserves_budget:
        with pytest.raises(RequestBudgetError) as error:
            invoke()
        assert error.value is failure
    else:
        error = _provider_code(invoke, mapped_code)
        assert error.__cause__ is failure


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (YfinanceProviderError("YFINANCE_HISTORY_SHAPE_INVALID"), "YFINANCE_HISTORY_SHAPE_INVALID"),
        (RequestBudgetError("history denied"), None),
    ],
)
def test_history_snapshot_preserves_provider_and_budget_failures(
    failure: Exception,
    expected_code: str | None,
    sdk_harness: dict[str, Any],
) -> None:
    class Ticker:
        def history(self, **kwargs: object) -> object:
            raise failure

    sdk_harness["ticker"] = Ticker()

    def call() -> pd.DataFrame | None:
        return _sdk.require_history_snapshot(
            "AAPL", date(2024, 1, 1), date(2024, 1, 2), timeout=2.0
        )

    if expected_code is None:
        with pytest.raises(RequestBudgetError) as error:
            call()
        assert error.value is failure
    else:
        error = _provider_code(call, expected_code)
        assert error is failure


def test_screen_failures_keep_budget_and_translate_http_error(
    sdk_harness: dict[str, Any],
) -> None:
    for failure, preserves_budget in [
        (RequestBudgetError("screen denied"), True),
        (requests.exceptions.HTTPError("HTTP 502 fixture"), False),
    ]:

        def screen(*_args: object, _failure: Exception = failure, **_kwargs: object) -> object:
            raise _failure

        sdk_harness["sdk"].screen = screen
        if preserves_budget:
            with pytest.raises(RequestBudgetError) as error:
                _sdk.require_screen("day_gainers", offset=0, limit=5, predefined=True)
            assert error.value is failure
        else:
            error = _provider_code(
                lambda: _sdk.require_screen("day_gainers", offset=0, limit=5, predefined=True),
                "YFINANCE_REQUEST_FAILED",
            )
            assert error.__cause__ is failure


def test_ensure_config_seam_handles_missing_and_legacy_shaped_optional_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = SimpleNamespace(config=None)
    monkeypatch.setitem(sys.modules, "yfinance", fake_module)
    monkeypatch.setattr(_sdk, "_SDK_IMPORT_UNAVAILABLE", False)

    _sdk._ensure_sdk_config_seam()

    assert fake_module.config.network.retries == 0
    assert fake_module.config.network.proxy is None
    assert _sdk._SDK_IMPORT_UNAVAILABLE is False

    real_import = builtins.__import__

    def reject_yfinance(name: str, *args: object, **kwargs: object) -> object:
        if name == "yfinance":
            raise ImportError("optional yfinance package absent in fixture")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_yfinance)
    _sdk._ensure_sdk_config_seam()
    assert _sdk._SDK_IMPORT_UNAVAILABLE is True
    error = _provider_code(_sdk._require_sdk, "YFINANCE_SDK_MISSING")
    assert error.__cause__ is None


def test_require_sdk_uses_active_module_and_translates_lazy_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_module = SimpleNamespace(
        config=SimpleNamespace(network=SimpleNamespace(retries=0, proxy=None))
    )
    token = _sdk._ACTIVE_SDK.set((fake_module, object()))
    try:
        assert _sdk._require_sdk() is fake_module
    finally:
        _sdk._ACTIVE_SDK.reset(token)

    monkeypatch.setattr(_sdk, "_SDK_IMPORT_UNAVAILABLE", False)
    real_import = builtins.__import__

    def reject_yfinance(name: str, *args: object, **kwargs: object) -> object:
        if name == "yfinance":
            raise ImportError("lazy optional import fixture")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_yfinance)
    error = _provider_code(_sdk._require_sdk, "YFINANCE_SDK_MISSING")
    assert isinstance(error.__cause__, ImportError)
