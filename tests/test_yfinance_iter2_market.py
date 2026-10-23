"""Independent yfinance SDK-shaped fixtures for Iteration 2 market identities."""

from __future__ import annotations

import asyncio
import json
import types
from contextlib import nullcontext
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models.yfinance_iter2 import (
    YfinanceKeyExecutiveObservation,
    YfinancePayloadObservation,
    YfinancePriceTargetConsensus,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.yfinance.models import _sdk, _session
from opendata.data.providers.yfinance.models.futures_curve_iter2 import YfinanceFuturesCurveFetcher
from opendata.data.providers.yfinance.models.market_iter2 import (
    MAX_EQUITY_HISTORICAL_SYMBOLS,
    YfinanceCompanyNewsFetcher,
    YfinanceCryptoHistoricalFetcher,
    YfinanceCurrencyHistoricalFetcher,
    YfinanceEquityActiveFetcher,
    YfinanceEquityAggressiveSmallCapsFetcher,
    YfinanceEquityGainersFetcher,
    YfinanceEquityHistoricalFetcher,
    YfinanceEquityHistoricalQuery,
    YfinanceEquityInfoFetcher,
    YfinanceEquityLosersFetcher,
    YfinanceEquityQuoteFetcher,
    YfinanceEquityScreenerFetcher,
    YfinanceEquityUndervaluedGrowthFetcher,
    YfinanceEquityUndervaluedLargeCapsFetcher,
    YfinanceEtfHistoricalFetcher,
    YfinanceEtfInfoFetcher,
    YfinanceFuturesHistoricalFetcher,
    YfinanceGrowthTechEquitiesFetcher,
    YfinanceIndexHistoricalFetcher,
    YfinanceKeyExecutivesFetcher,
    YfinanceKeyMetricsFetcher,
    YfinanceOptionsChainsFetcher,
    YfinancePriceTargetFetcher,
    YfinanceShareStatisticsFetcher,
)
from opendata.data.providers.yfinance.models.stock_daily import YfinanceProviderError
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    RequestBudget,
    RequestBudgetProfile,
    RequestGrant,
    RequestOperation,
)
from opendata.services import provider_model_query
from scripts.quality.iter2_witnesses._parts.yfinance.rows_w34_futures_curve import _chain_document


def _sdk_context() -> Any:
    from opendata.data.protocol import FetchContext

    return FetchContext()


@pytest.mark.parametrize(
    ("split_values", "expected"),
    [
        (pd.Series([2.0], index=[pd.Timestamp("2020-08-31")]), [2.0]),
        (
            pd.DataFrame({"Stock Splits": [2.0]}, index=[pd.Timestamp("2020-08-31")]),
            [2.0],
        ),
        ([2.0, 3.0], [2.0, 3.0]),
    ],
)
def test_split_history_preserves_supported_sized_shapes(
    split_values: object,
    expected: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Ticker:
        def __init__(self, _symbol: str) -> None:
            self.splits = split_values

    fake_sdk = types.SimpleNamespace(__version__="1.7.0", Ticker=Ticker)
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: fake_sdk)
    monkeypatch.setattr(
        _sdk,
        "governed_yfinance_session",
        lambda _fake_sdk: nullcontext(None),
    )

    result = _sdk.require_split_history("AAPL")

    assert result.tolist() == expected


def test_split_history_rejects_non_sized_sdk_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    class Ticker:
        def __init__(self, _symbol: str) -> None:
            pass

        @property
        def splits(self) -> object:
            return object()

    fake_sdk = types.SimpleNamespace(__version__="1.7.0", Ticker=Ticker)
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: fake_sdk)
    monkeypatch.setattr(
        _sdk,
        "governed_yfinance_session",
        lambda _fake_sdk: nullcontext(None),
    )

    with pytest.raises(YfinanceProviderError) as error:
        _sdk.require_split_history("AAPL")

    assert error.value.code == "YFINANCE_SPLITS_SHAPE_INVALID"


@pytest.mark.parametrize(
    ("offset_minutes", "is_utc"),
    [(0, True), (1, False), (-1, False), (None, False)],
)
def test_yfinance_retrieval_timestamps_accept_only_utc_offsets(
    offset_minutes: int | None, is_utc: bool
) -> None:
    timestamp = datetime(2026, 1, 2, 3, 4, 5)
    if offset_minutes is not None:
        timestamp = timestamp.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))

    cases = (
        (
            YfinancePayloadObservation,
            {
                "model_id": "EquityInfo",
                "symbol": "AAPL",
                "raw_payload_json": "{}",
            },
        ),
        (
            YfinanceKeyExecutiveObservation,
            {
                "symbol": "AAPL",
                "title": "Chief Executive Officer",
                "name": "Ada Example",
                "ordinal": 0,
                "raw_payload_json": "{}",
            },
        ),
        (
            YfinancePriceTargetConsensus,
            {
                "symbol": "AAPL",
                "target_high": None,
                "target_low": None,
                "target_consensus": None,
                "target_median": None,
                "current_price": None,
            },
        ),
    )
    for model_type, fields in cases:
        payload = {**fields, "retrieved_at_utc": timestamp}
        if is_utc:
            row = model_type.model_validate(payload)
            assert row.retrieved_at_utc == timestamp
        else:
            with pytest.raises(ValidationError):
                model_type.model_validate(payload)


@pytest.fixture
def crypto_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [61000.0],
            "High": [62000.0],
            "Low": [60500.0],
            "Close": [61500.0],
            "Adj Close": [61500.0],
            "Volume": [123.5],
            "Dividends": [0.0],
            "Stock Splits": [0.0],
        },
        index=pd.DatetimeIndex(["2024-01-02"], name="Date"),
    )


@pytest.fixture
def currency_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [1.09], "High": [1.10], "Low": [1.08], "Close": [1.095], "Volume": [0.0]},
        index=pd.DatetimeIndex(["2024-01-03"], name="Date"),
    )


@pytest.fixture
def equity_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [185.0], "High": [188.0], "Low": [184.0], "Close": [187.0], "Volume": [1000]},
        index=pd.DatetimeIndex(["2024-01-04"], name="Date"),
    )


@pytest.fixture
def etf_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [470.0], "High": [472.0], "Low": [469.0], "Close": [471.0], "Volume": [500]},
        index=pd.DatetimeIndex(["2024-01-05"], name="Date"),
    )


@pytest.fixture
def futures_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [4800.0], "High": [4820.0], "Low": [4790.0], "Close": [4810.0], "Volume": [900]},
        index=pd.DatetimeIndex(["2024-01-08"], name="Date"),
    )


@pytest.fixture
def index_history_response() -> pd.DataFrame:
    return pd.DataFrame(
        {"Open": [4700.0], "High": [4725.0], "Low": [4680.0], "Close": [4715.0]},
        index=pd.DatetimeIndex(["2024-01-09"], name="Date"),
    )


@pytest.mark.parametrize(
    ("fetcher_type", "fixture_name", "symbol", "session_date", "model_id"),
    [
        (
            YfinanceCryptoHistoricalFetcher,
            "crypto_history_response",
            "BTC-USD",
            "2024-01-02",
            "CryptoHistorical",
        ),
        (
            YfinanceCurrencyHistoricalFetcher,
            "currency_history_response",
            "EURUSD=X",
            "2024-01-03",
            "CurrencyHistorical",
        ),
        (
            YfinanceEquityHistoricalFetcher,
            "equity_history_response",
            "AAPL",
            "2024-01-04",
            "EquityHistorical",
        ),
        (
            YfinanceEtfHistoricalFetcher,
            "etf_history_response",
            "SPY",
            "2024-01-05",
            "EtfHistorical",
        ),
        (
            YfinanceFuturesHistoricalFetcher,
            "futures_history_response",
            "ES=F",
            "2024-01-08",
            "FuturesHistorical",
        ),
        (
            YfinanceIndexHistoricalFetcher,
            "index_history_response",
            "^GSPC",
            "2024-01-09",
            "IndexHistorical",
        ),
    ],
)
def test_each_historical_identity_uses_its_own_handwritten_response(
    fetcher_type: type,
    fixture_name: str,
    symbol: str,
    session_date: str,
    model_id: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = request.getfixturevalue(fixture_name)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        _sdk,
        "require_history_snapshot",
        lambda *args, **kwargs: calls.append((args, kwargs)) or response,
    )
    fetcher = fetcher_type()
    query = fetcher.transform_query(symbol=symbol, start_date="2024-01-01", end_date="2024-01-31")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

    assert len(rows) == 1
    assert rows[0].model_id == model_id
    assert rows[0].symbol == symbol
    assert rows[0].session_date == date.fromisoformat(session_date)
    assert rows[0].interval == "1d"
    assert rows[0].price_adjustment == (
        "splits_only" if model_id in {"EquityHistorical", "EtfHistorical"} else "not_applicable"
    )
    assert rows[0].currency_status == "not_provided_by_sdk"
    assert calls[0][0] == (symbol, date(2024, 1, 1), date(2024, 1, 31))


def test_history_rejects_bad_arguments_before_io_and_preserves_date_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        _sdk, "require_history_snapshot", lambda *args, **kwargs: calls.append(args)
    )
    fetcher = YfinanceEquityHistoricalFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", start_date="2024-02-02", end_date="2024-02-01")
    assert calls == []

    query = fetcher.transform_query(symbol="AAPL", start_date="2024-02-01", end_date="2024-02-02")
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data(pd.DataFrame({"Close": [1.0]}), query)
    assert error.value.code == "YFINANCE_HISTORY_SHAPE_INVALID"


@pytest.mark.parametrize(
    ("public_interval", "sdk_interval"),
    [
        ("1m", "1m"),
        ("2m", "2m"),
        ("5m", "5m"),
        ("15m", "15m"),
        ("30m", "30m"),
        ("60m", "60m"),
        ("90m", "90m"),
        ("1h", "1h"),
        ("1d", "1d"),
        ("5d", "5d"),
        ("1W", "1wk"),
        ("1M", "1mo"),
        ("1Q", "3mo"),
    ],
)
def test_history_interval_mapping_and_intraday_timezone_are_preserved(
    public_interval: str,
    sdk_interval: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = pd.Timestamp("2024-02-01 09:30:00", tz="America/New_York")
    response = pd.DataFrame(
        {"Open": [1.0], "High": [2.0], "Low": [0.5], "Close": [1.5]},
        index=pd.DatetimeIndex([timestamp], name="Date"),
    )
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        _sdk,
        "require_history_snapshot",
        lambda symbol, start, end, **kwargs: calls.append((symbol, start, end, kwargs)) or response,
    )

    fetcher = YfinanceEquityHistoricalFetcher()
    query = fetcher.transform_query(symbol="AAPL", interval=public_interval)
    raw = fetcher.extract_data(query, _sdk_context())
    row = fetcher.transform_data(raw, query)[0]

    assert calls[0][3]["interval"] == sdk_interval
    if public_interval in {"1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"}:
        assert row.session_date == timestamp.to_pydatetime()
        assert row.session_date.tzinfo is not None
    else:
        assert row.session_date == timestamp.date()
    assert row.interval == public_interval


@pytest.mark.parametrize(
    "symbols",
    [" , aapl, MSFT, ,AAPL, ", ["aapl", "MSFT", "AAPL"]],
)
def test_equity_history_fans_out_normalized_unique_symbols_in_first_seen_order(
    equity_history_response: pd.DataFrame,
    symbols: str | list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert YfinanceEquityHistoricalQuery.model_fields["symbol"].annotation == str | list[str]
    calls: list[str] = []
    msft_response = equity_history_response.copy()
    msft_response.loc[:, "Close"] = 410.0
    responses = {"AAPL": equity_history_response, "MSFT": msft_response}

    def history(symbol: str, *_args: object, **_kwargs: object) -> pd.DataFrame:
        calls.append(symbol)
        return responses[symbol]

    monkeypatch.setattr(_sdk, "require_history_snapshot", history)
    fetcher = YfinanceEquityHistoricalFetcher()
    query = fetcher.transform_query(symbol=symbols, start_date="2024-01-01", end_date="2024-01-31")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)

    assert calls == ["AAPL", "MSFT"]
    assert [(row.symbol, row.close) for row in rows] == [("AAPL", 187.0), ("MSFT", 410.0)]


def test_equity_history_rejects_empty_or_over_limit_symbol_inputs_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        _sdk, "require_history_snapshot", lambda *args, **kwargs: calls.append(args)
    )
    fetcher = YfinanceEquityHistoricalFetcher()
    too_many = [f"S{index}" for index in range(MAX_EQUITY_HISTORICAL_SYMBOLS + 1)]
    for symbols in (" , , ", too_many, ["AAPL", 17], ",".join(["AAPL"] * 33)):
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol=symbols, start_date="2024-01-01", end_date="2024-01-31")
    assert calls == []


def test_equity_history_validates_batch_symbol_attribution_and_discards_partial_failure(
    equity_history_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = YfinanceEquityHistoricalFetcher()
    query = fetcher.transform_query(
        symbol="AAPL,MSFT", start_date="2024-01-01", end_date="2024-01-31"
    )
    with pytest.raises(YfinanceProviderError) as mismatch:
        fetcher.transform_data(
            (("MSFT", equity_history_response), ("AAPL", equity_history_response)), query
        )
    assert mismatch.value.code == "YFINANCE_HISTORY_SYMBOL_MISMATCH"

    calls: list[str] = []

    def fail_on_second(symbol: str, *_args: object, **_kwargs: object) -> pd.DataFrame:
        calls.append(symbol)
        if symbol == "MSFT":
            raise YfinanceProviderError("YFINANCE_REQUEST_FAILED")
        return equity_history_response

    monkeypatch.setattr(_sdk, "require_history_snapshot", fail_on_second)
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.extract_data(query, _sdk_context())
    assert error.value.code == "YFINANCE_REQUEST_FAILED"
    assert calls == ["AAPL", "MSFT"]


def test_equity_history_each_symbol_consumes_the_shared_request_budget(
    equity_history_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yfinance

    fetcher = YfinanceEquityHistoricalFetcher()
    fetcher.canonical_model = "EquityHistorical"
    calls: list[str] = []
    sent: list[str] = []

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def close(self) -> None:
            return None

    monkeypatch.setattr(_session, "_SESSION", None)
    monkeypatch.setattr(yfinance.data.YfData, "_instances", {})
    with _session.governed_yfinance_session(yfinance) as session:
        assert session is not None

        def send_once(
            _session_obj: object,
            _method: str,
            url: str,
            *_args: object,
            **_kwargs: object,
        ) -> Response:
            sent.append(url)
            return Response()

        monkeypatch.setattr(_session, "_send_once", send_once)

        def history(symbol: str, *_args: object, **_kwargs: object) -> pd.DataFrame:
            calls.append(symbol)
            session.get("https://query1.finance.yahoo.com/v1/history", timeout=2)
            return equity_history_response

        monkeypatch.setattr(_sdk, "require_history_snapshot", history)
    grant = RequestGrant(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-sdk",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({"query1.finance.yahoo.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )

    with pytest.raises(RequestAttemptLimitError):
        fetcher.fetch(
            ctx=FetchContext(timeout=2.0, request_budget=budget),
            symbol="AAPL,MSFT",
            start_date="2024-01-01",
            end_date="2024-01-31",
        )
    assert calls == ["AAPL", "MSFT"]
    assert sent == ["https://query1.finance.yahoo.com/v1/history"]
    assert budget.attempts_used == 1


@pytest.mark.parametrize(
    ("fetcher_type", "fixture_name", "sdk_method", "symbols", "expected_symbols", "query_fields"),
    [
        (
            YfinanceCryptoHistoricalFetcher,
            "crypto_history_response",
            "history",
            ["BTC-USD", "ETH-USD"],
            ["BTC-USD", "ETH-USD"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceCurrencyHistoricalFetcher,
            "currency_history_response",
            "history",
            ["eur-usd", "GBPJPY"],
            ["EURUSD=X", "GBPJPY=X"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceEquityHistoricalFetcher,
            "equity_history_response",
            "history",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceEtfHistoricalFetcher,
            "etf_history_response",
            "history",
            ["SPY", "QQQ"],
            ["SPY", "QQQ"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceFuturesHistoricalFetcher,
            "futures_history_response",
            "history",
            ["ES=F", "NQ=F"],
            ["ES=F", "NQ=F"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceIndexHistoricalFetcher,
            "index_history_response",
            "history",
            ["^GSPC", "^DJI"],
            ["^GSPC", "^DJI"],
            {"start_date": "2024-01-01", "end_date": "2024-01-31"},
        ),
        (
            YfinanceCompanyNewsFetcher,
            "company_news_sdk_response",
            "news",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {"limit": 20},
        ),
        (
            YfinanceEquityInfoFetcher,
            "equity_info_sdk_response",
            "info",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {},
        ),
        (
            YfinanceEquityQuoteFetcher,
            "equity_quote_sdk_response",
            "fast_info",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {},
        ),
        (
            YfinanceEtfInfoFetcher,
            "etf_info_sdk_response",
            "fund_sections",
            ["SPY", "QQQ"],
            ["SPY", "QQQ"],
            {},
        ),
        (
            YfinanceKeyMetricsFetcher,
            "key_metrics_sdk_response",
            "metrics",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {"frequency": "yearly", "periods": 3},
        ),
        (YfinancePriceTargetFetcher, None, "price_targets", ["AAPL", "MSFT"], ["AAPL", "MSFT"], {}),
        (
            YfinanceShareStatisticsFetcher,
            "share_statistics_sdk_response",
            "info",
            ["AAPL", "MSFT"],
            ["AAPL", "MSFT"],
            {},
        ),
    ],
)
def test_all_public_multisymbol_models_fan_out_and_attribute_rows(
    fetcher_type: type,
    fixture_name: str | None,
    sdk_method: str,
    symbols: list[str],
    expected_symbols: list[str],
    query_fields: dict[str, object],
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = request.getfixturevalue(fixture_name) if fixture_name else None
    calls: list[str] = []

    if sdk_method == "history":

        def call(symbol: str, *_args: object, **_kwargs: object) -> pd.DataFrame:
            calls.append(symbol)
            return response

        monkeypatch.setattr(_sdk, "require_history_snapshot", call)
    elif sdk_method == "news":

        def call(symbol: str, *, count: int) -> list[dict[str, object]]:
            calls.append(symbol)
            return response

        monkeypatch.setattr(_sdk, "require_news", call)
    elif sdk_method == "info":

        def call(symbol: str) -> dict[str, object]:
            calls.append(symbol)
            return {**response, "symbol": symbol}

        monkeypatch.setattr(_sdk, "require_info", call)
    elif sdk_method == "fast_info":

        def call(symbol: str) -> dict[str, object]:
            calls.append(symbol)
            return response

        monkeypatch.setattr(_sdk, "require_fast_info", call)
    elif sdk_method == "fund_sections":

        def call(symbol: str) -> dict[str, object]:
            calls.append(symbol)
            return response

        monkeypatch.setattr(_sdk, "require_fund_sections", call)
    elif sdk_method == "metrics":

        def call(symbol: str, *, frequency: str, periods: int) -> pd.DataFrame:
            calls.append(symbol)
            return response

        monkeypatch.setattr(_sdk, "require_valuation_measures", call)
    else:

        def call(symbol: str) -> dict[str, object]:
            calls.append(symbol)
            return {"current": 187.0, "low": 180.0, "high": 240.0, "mean": 215.0, "median": 220.0}

        monkeypatch.setattr(_sdk, "require_price_targets", call)

    fetcher = fetcher_type()
    query = fetcher.transform_query(symbol=symbols, **query_fields)
    raw = fetcher.extract_data(query, _sdk_context())
    rows = fetcher.transform_data(raw, query)

    assert calls == expected_symbols
    assert list(dict.fromkeys(row.symbol for row in rows)) == expected_symbols


def test_multisymbol_normalization_is_stable_bounded_and_currency_pair_aware(
    currency_history_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        _sdk,
        "require_history_snapshot",
        lambda symbol, *_args, **_kwargs: calls.append(symbol) or currency_history_response,
    )
    fetcher = YfinanceCurrencyHistoricalFetcher()
    query = fetcher.transform_query(
        symbol=["eur-usd", "GBPJPY=X", "EURUSD", " gbp-jpy "],
        start_date="2024-01-01",
        end_date="2024-01-31",
    )
    assert query.symbols == ("EURUSD=X", "GBPJPY=X")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert calls == ["EURUSD=X", "GBPJPY=X"]
    assert [row.symbol for row in rows] == ["EURUSD=X", "GBPJPY=X"]


@pytest.mark.parametrize(
    ("fetcher_type", "query_fields"),
    [
        (YfinanceCryptoHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceCurrencyHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceEquityHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceEtfHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceFuturesHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceIndexHistoricalFetcher, {"start_date": "2024-01-01", "end_date": "2024-01-02"}),
        (YfinanceCompanyNewsFetcher, {}),
        (YfinanceEquityInfoFetcher, {}),
        (YfinanceEquityQuoteFetcher, {}),
        (YfinanceEtfInfoFetcher, {}),
        (YfinanceKeyMetricsFetcher, {}),
        (YfinancePriceTargetFetcher, {}),
        (YfinanceShareStatisticsFetcher, {}),
    ],
)
def test_every_multisymbol_model_rejects_over_limit_before_io(
    fetcher_type: type,
    query_fields: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    for name in (
        "require_history_snapshot",
        "require_news",
        "require_info",
        "require_fast_info",
        "require_fund_sections",
        "require_valuation_measures",
        "require_price_targets",
    ):
        monkeypatch.setattr(_sdk, name, lambda *args, _calls=calls, **kwargs: _calls.append(args))
    fetcher = fetcher_type()
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol=[f"SYM{index}" for index in range(9)], **query_fields)
    assert calls == []


def test_futures_expiration_mapping_gap_is_rejected_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        _sdk, "require_history_snapshot", lambda *args, **kwargs: calls.append(args)
    )
    fetcher = YfinanceFuturesHistoricalFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(
            symbol="ES=F",
            start_date="2024-01-01",
            end_date="2024-01-31",
            expiration="2024-06",
        )
    assert calls == []


@pytest.mark.parametrize(
    ("fetcher_type", "query_fields", "first_symbol"),
    [
        (
            YfinanceCryptoHistoricalFetcher,
            {
                "symbol": ["BTC-USD", "ETH-USD"],
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
            },
            "BTC-USD",
        ),
        (
            YfinanceCurrencyHistoricalFetcher,
            {
                "symbol": ["EUR-USD", "GBP-JPY"],
                "start_date": "2024-01-01",
                "end_date": "2024-01-02",
            },
            "EURUSD=X",
        ),
        (
            YfinanceEquityHistoricalFetcher,
            {"symbol": ["AAPL", "MSFT"], "start_date": "2024-01-01", "end_date": "2024-01-02"},
            "AAPL",
        ),
        (
            YfinanceEtfHistoricalFetcher,
            {"symbol": ["SPY", "QQQ"], "start_date": "2024-01-01", "end_date": "2024-01-02"},
            "SPY",
        ),
        (
            YfinanceFuturesHistoricalFetcher,
            {"symbol": ["ES=F", "NQ=F"], "start_date": "2024-01-01", "end_date": "2024-01-02"},
            "ES=F",
        ),
        (
            YfinanceIndexHistoricalFetcher,
            {"symbol": ["^GSPC", "^DJI"], "start_date": "2024-01-01", "end_date": "2024-01-02"},
            "^GSPC",
        ),
        (YfinanceCompanyNewsFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
        (YfinanceEquityInfoFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
        (YfinanceEquityQuoteFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
        (YfinanceEtfInfoFetcher, {"symbol": ["SPY", "QQQ"]}, "SPY"),
        (YfinanceKeyMetricsFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
        (YfinancePriceTargetFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
        (YfinanceShareStatisticsFetcher, {"symbol": ["AAPL", "MSFT"]}, "AAPL"),
    ],
)
def test_multisymbol_fetchers_do_not_charge_without_a_transport_send(
    fetcher_type: type,
    query_fields: dict[str, object],
    first_symbol: str,
    equity_history_response: pd.DataFrame,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def record(symbol: str) -> str:
        calls.append(symbol)
        return symbol

    monkeypatch.setattr(
        _sdk,
        "require_history_snapshot",
        lambda symbol, *_args, **_kwargs: record(symbol) and equity_history_response,
    )
    monkeypatch.setattr(
        _sdk,
        "require_news",
        lambda symbol, *, count: record(symbol) and [],
    )
    monkeypatch.setattr(
        _sdk,
        "require_info",
        lambda symbol: record(symbol) and {"symbol": symbol, "sharesOutstanding": 100},
    )
    monkeypatch.setattr(
        _sdk,
        "require_fast_info",
        lambda symbol: record(symbol) and {"lastPrice": 10.0},
    )
    monkeypatch.setattr(
        _sdk,
        "require_fund_sections",
        lambda symbol: record(symbol) and {"description": symbol},
    )
    monkeypatch.setattr(
        _sdk,
        "require_valuation_measures",
        lambda symbol, **kwargs: record(symbol) and pd.DataFrame({"Current": [1.0]}, index=["P/E"]),
    )
    monkeypatch.setattr(
        _sdk,
        "require_price_targets",
        lambda symbol: (
            record(symbol) and {"current": 1.0, "low": 0.5, "high": 2.0, "mean": 1.5, "median": 1.4}
        ),
    )

    fetcher = fetcher_type()
    model = fetcher.canonical_model
    grant = RequestGrant(
        source="yfinance",
        canonical_model=model,
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-sdk",
        task_attempts=1,
        source_attempts=1,
        allowed_hosts=frozenset({"query1.finance.yahoo.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=1,
        source_attempts=1,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )

    context = FetchContext(timeout=2.0, request_budget=budget)
    params = fetcher.transform_query(**query_fields)
    fetcher.extract_data(params, context)
    assert calls[0] == first_symbol
    assert len(calls) == 2
    assert budget.attempts_used == 0


def test_provider_model_query_injects_identity_and_serializes_yfinance_rows(
    equity_history_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opendata.data.providers.catalog import register_provider
    from opendata.data.registry import get_registry

    calls: list[tuple[object, ...]] = []

    msft_response = equity_history_response.copy()
    msft_response.loc[:, "Close"] = 410.0
    response_by_symbol = {"AAPL": equity_history_response, "MSFT": msft_response}

    def history(symbol: str, *args: object, **kwargs: object) -> pd.DataFrame:
        calls.append(((symbol, *args), kwargs))
        return response_by_symbol[symbol]

    monkeypatch.setattr(
        _sdk,
        "require_history_snapshot",
        history,
    )
    registry = get_registry()
    register_provider("yfinance", registry)
    fetcher = registry.resolve_model("yfinance", "EquityHistorical")
    capability = fetcher.capability
    query_fields: dict[str, object] = {}
    original_transform_query = fetcher.transform_query

    def capture_query(**kwargs: object) -> Any:
        query_fields.update(kwargs)
        normalized = original_transform_query(**kwargs)
        query_fields["normalized_symbol"] = normalized.symbol
        return normalized

    monkeypatch.setattr(fetcher, "transform_query", capture_query)

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain == capability.domain

    grant = RequestGrant(
        source="yfinance",
        canonical_model="EquityHistorical",
        operation=RequestOperation.QUERY,
        decision=GrantDecision.ALLOWED,
        rights_evidence="test-fixture:offline-only:fake-sdk",
        task_attempts=2,
        source_attempts=2,
        allowed_hosts=frozenset({"query1.finance.yahoo.com"}),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    budget = RequestBudget(
        task_attempts=2,
        source_attempts=2,
        grants=(grant,),
        profile=RequestBudgetProfile.BOUNDED_REQUEST,
    )
    response = asyncio.run(
        provider_model_query.query_provider_model(
            registry=registry,
            source="yfinance",
            model="EquityHistorical",
            query={
                "symbol": ["AAPL", "MSFT"],
                "start_date": "2024-01-01",
                "end_date": "2024-01-31",
            },
            principal=Principal(),
            ctx=FetchContext(timeout=2.0, request_budget=budget),
        )
    )

    assert query_fields["source"] == "yfinance"
    assert query_fields["market"] == "global"
    assert query_fields["symbol"] == ["AAPL", "MSFT"]
    assert query_fields["normalized_symbol"] == "AAPL,MSFT"
    assert response["source"] == "yfinance"
    assert response["model"] == "EquityHistorical"
    assert response["domain"] == capability.domain
    assert json.dumps(response, allow_nan=False)
    assert response["results"] == [
        {
            "model_id": "EquityHistorical",
            "symbol": "AAPL",
            "session_date": "2024-01-04",
            "open": 185.0,
            "high": 188.0,
            "low": 184.0,
            "close": 187.0,
            "adjusted_close": None,
            "volume": 1000.0,
            "dividends": None,
            "stock_splits": None,
            "currency": None,
            "currency_status": "not_provided_by_sdk",
            "interval": "1d",
            "price_adjustment": "splits_only",
        },
        {
            "model_id": "EquityHistorical",
            "symbol": "MSFT",
            "session_date": "2024-01-04",
            "open": 185.0,
            "high": 188.0,
            "low": 184.0,
            "close": 410.0,
            "adjusted_close": None,
            "volume": 1000.0,
            "dividends": None,
            "stock_splits": None,
            "currency": None,
            "currency_status": "not_provided_by_sdk",
            "interval": "1d",
            "price_adjustment": "splits_only",
        },
    ]
    assert [call[0][0] for call in calls] == ["AAPL", "MSFT"]
    assert all(call[0][1:] == (date(2024, 1, 1), date(2024, 1, 31)) for call in calls)


def test_provider_model_query_serializes_four_public_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise canonical routing, injected identity, and exact typed row fields."""
    from opendata.data.providers.catalog import register_provider
    from opendata.data.registry import get_registry

    registry = get_registry()
    register_provider("yfinance", registry)
    dividends = pd.Series(
        # This local date is the previous UTC day at 00:30 in UTC+14; a UTC
        # conversion before extracting the date would corrupt the public field.
        [0.25],
        index=pd.DatetimeIndex(["2024-06-14 00:30"], tz="Pacific/Kiritimati"),
    )
    officers = [
        {
            "name": "Ada Example",
            "title": "Chief Executive Officer",
            "totalPay": 12_345_678,
            "yearBorn": 1970,
            "fiscalYear": 2024,
            "exercisedValue": 2_000_001,
            "unexercisedValue": 3_000_002,
        }
    ]
    metric_frames = {
        "AAPL": pd.DataFrame(
            {
                "Current": [100_000_000, 200_000_000, 10.0],
                "12/31/2024": [90_000_000, 180_000_000, 9.0],
            },
            index=["Market Cap", "Enterprise Value", "Trailing P/E"],
        ),
        "MSFT": pd.DataFrame(
            {
                "Current": [400_000_000, 600_000_000, 20.0],
                "12/31/2024": [360_000_000, 540_000_000, 18.0],
            },
            index=["Market Cap", "Enterprise Value", "Trailing P/E"],
        ),
    }
    target_values = {
        "AAPL": {"current": 190.0, "low": 180.0, "high": 240.0, "mean": 215.0, "median": 220.0},
        "MSFT": {"current": 410.0, "low": 390.0, "high": 500.0, "mean": 450.0, "median": 455.0},
    }
    monkeypatch.setattr(_sdk, "require_dividends_series", lambda _symbol: dividends)
    monkeypatch.setattr(
        _sdk,
        "require_info",
        lambda symbol: {"symbol": symbol, "companyOfficers": officers},
    )
    monkeypatch.setattr(
        _sdk,
        "require_valuation_measures",
        lambda symbol, *, frequency, periods: metric_frames[symbol],
    )
    monkeypatch.setattr(_sdk, "require_price_targets", lambda symbol: target_values[symbol])

    class Principal:
        def allows_domain(self, domain: str) -> bool:
            return domain.startswith("yfinance_")

    def query(model_id: str, query_fields: dict[str, object]) -> list[dict[str, object]]:
        fetcher = registry.resolve_model("yfinance", model_id)
        capability = fetcher.capability
        seen_query: dict[str, object] = {}
        original_transform = fetcher.transform_query

        def capture_query(**kwargs: object) -> Any:
            seen_query.update(kwargs)
            return original_transform(**kwargs)

        monkeypatch.setattr(fetcher, "transform_query", capture_query)
        symbol_value = query_fields["symbol"]
        attempt_count = len(symbol_value) if isinstance(symbol_value, list) else 1
        grant = RequestGrant(
            source="yfinance",
            canonical_model=model_id,
            operation=RequestOperation.QUERY,
            decision=GrantDecision.ALLOWED,
            rights_evidence="test-fixture:offline-only:fake-sdk",
            task_attempts=attempt_count,
            source_attempts=attempt_count,
            allowed_hosts=frozenset({"query1.finance.yahoo.com"}),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
        budget = RequestBudget(
            task_attempts=attempt_count,
            source_attempts=attempt_count,
            grants=(grant,),
            profile=RequestBudgetProfile.BOUNDED_REQUEST,
        )
        response = asyncio.run(
            provider_model_query.query_provider_model(
                registry=registry,
                source="yfinance",
                model=model_id,
                query=query_fields,
                principal=Principal(),
                ctx=FetchContext(timeout=2.0, request_budget=budget),
            )
        )
        assert seen_query["source"] == "yfinance"
        assert seen_query["market"] == "global"
        assert response["source"] == "yfinance"
        assert response["model"] == model_id
        json.dumps(response, allow_nan=False)
        assert all(isinstance(row, dict) for row in response["results"])
        assert capability.domain == response["domain"]
        return response["results"]

    dividend_rows = query("HistoricalDividends", {"symbol": "AAPL"})
    assert dividend_rows[0]["ex_dividend_date"] == "2024-06-14"
    assert dividend_rows[0]["amount"] == 0.25
    assert "event_date" not in dividend_rows[0]
    assert "amount_per_share" not in dividend_rows[0]

    executive_rows = query("KeyExecutives", {"symbol": "AAPL"})
    assert executive_rows[0]["name"] == "Ada Example"
    assert executive_rows[0]["pay"] == 12_345_678
    assert executive_rows[0]["year_born"] == 1970
    assert "totalPay" not in executive_rows[0]

    metric_rows = query("KeyMetrics", {"symbol": ["AAPL", "MSFT"], "ttm": "include", "limit": 1})
    assert {(row["symbol"], row["period_kind"]) for row in metric_rows} == {
        ("AAPL", "current"),
        ("AAPL", "historical"),
        ("MSFT", "current"),
        ("MSFT", "historical"),
    }
    aapl_current = next(
        row for row in metric_rows if row["symbol"] == "AAPL" and row["period_kind"] == "current"
    )
    msft_current = next(
        row for row in metric_rows if row["symbol"] == "MSFT" and row["period_kind"] == "current"
    )
    assert aapl_current["market_cap"] == 100_000_000
    assert msft_current["market_cap"] == 400_000_000
    assert aapl_current["enterprise_value"] == 200_000_000
    assert msft_current["enterprise_value"] == 600_000_000
    assert aapl_current["period_ending"] is None
    assert aapl_current["period_date"] is None
    assert aapl_current["price_to_sales"] is None

    target_rows = query("PriceTargetConsensus", {"symbol": ["AAPL", "MSFT"]})
    assert [row["target_consensus"] for row in target_rows] == [215.0, 450.0]
    assert [row["current_price"] for row in target_rows] == [190.0, 410.0]
    assert all("mean" not in row and "current" not in row for row in target_rows)


@pytest.fixture
def company_news_sdk_response() -> list[dict[str, object]]:
    return [
        {
            "id": "news-a1",
            "content": {
                "title": "Quarterly results",
                "pubDate": "2024-03-15T12:30:00Z",
                "provider": {"displayName": "Example Wire"},
            },
        }
    ]


@pytest.fixture
def equity_info_sdk_response() -> dict[str, object]:
    return {
        "symbol": "AAPL",
        "longName": "Example Computer Co.",
        "currency": "USD",
        "marketCap": 2_900_000_000_000,
        "companyOfficers": [{"name": "Ada Example", "title": "CEO"}],
    }


@pytest.fixture
def equity_quote_sdk_response() -> dict[str, object]:
    return {"currency": "USD", "lastPrice": 187.0, "dayHigh": 188.0, "dayLow": 184.0}


@pytest.fixture
def etf_info_sdk_response() -> dict[str, object]:
    return {
        "asset_classes": {"stock": 0.92, "bond": 0.08},
        "description": "Broad-market ETF",
        "top_holdings": [{"symbol": "AAPL", "holding_percent": 0.07}],
        "sector_weightings": [{"technology": 0.31}],
    }


@pytest.fixture
def executives_sdk_response() -> list[dict[str, object]]:
    return [
        {
            "name": "Ada Example",
            "title": "Chief Executive Officer",
            "totalPay": 12_345_678,
            "yearBorn": 1970,
            "fiscalYear": 2024,
            "exercisedValue": 2_000_001,
            "unexercisedValue": 3_000_002,
        },
        {
            "name": "Lin Example",
            "title": "Chief Financial Officer",
            "totalPay": 4_567_890,
            "yearBorn": 1975,
            "fiscalYear": 2023,
            "exercisedValue": 1_000_003,
            "unexercisedValue": 2_000_004,
        },
    ]


@pytest.fixture
def share_statistics_sdk_response() -> dict[str, object]:
    return {
        "sharesOutstanding": 15_000_000_000,
        "floatShares": 14_800_000_000,
        "sharesShort": 90_000_000,
        "heldPercentInsiders": 0.001,
        "heldPercentInstitutions": 0.62,
        "regularMarketPrice": 187.0,
    }


def test_company_news_limit_maps_to_sdk_count_and_retains_provider_fields(
    company_news_sdk_response: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        _sdk,
        "require_news",
        lambda symbol, *, count: calls.append((symbol, count)) or company_news_sdk_response,
    )
    fetcher = YfinanceCompanyNewsFetcher()
    query = fetcher.transform_query(symbol="aapl", limit=15)
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert calls == [("AAPL", 15)]
    assert rows[0].published_at_utc == datetime(2024, 3, 15, 12, 30, tzinfo=timezone.utc)
    assert json.loads(rows[0].raw_payload_json)["content"]["title"] == "Quarterly results"


def test_company_news_limit_bounds_merged_multi_symbol_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, int]] = []

    def news(symbol: str, *, count: int) -> list[dict[str, object]]:
        calls.append((symbol, count))
        days = [11, 13, 15] if symbol == "AAPL" else [12, 14, 16]
        return [
            {
                "content": {
                    "pubDate": f"2024-03-{day:02d}T12:00:00Z",
                    "title": f"{symbol}-{day}",
                }
            }
            for day in days
        ]

    monkeypatch.setattr(_sdk, "require_news", news)
    fetcher = YfinanceCompanyNewsFetcher()
    query = fetcher.transform_query(symbol=["AAPL", "MSFT"], limit=3)
    raw = fetcher.extract_data(query, _sdk_context())
    rows = fetcher.transform_data(raw, query)

    assert calls == [("AAPL", 3), ("MSFT", 3)]
    assert len(rows) == 3
    assert [(row.symbol, row.published_at_utc.day) for row in rows] == [
        ("MSFT", 16),
        ("AAPL", 15),
        ("MSFT", 14),
    ]


def test_news_date_window_and_conflicting_limit_alias_reject_before_io(
    company_news_sdk_response: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(_sdk, "require_news", lambda *args, **kwargs: calls.append(args) or [])
    fetcher = YfinanceCompanyNewsFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", count=251)
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", start_date="2024-03-01")
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", limit=10, count=15)
    assert calls == []

    snapshot_query = fetcher.transform_query(symbol="AAPL")
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data(
            (("AAPL", [{"content": {"title": "No timestamp"}}]),), snapshot_query
        )
    assert error.value.code == "YFINANCE_NEWS_DATE_MISSING"


def test_equity_info_keeps_unmodeled_provider_fields(
    equity_info_sdk_response: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_sdk, "require_info", lambda symbol: equity_info_sdk_response)
    fetcher = YfinanceEquityInfoFetcher()
    query = fetcher.transform_query(symbol="aapl")
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    payload = json.loads(row.raw_payload_json)
    assert row.model_id == "EquityInfo"
    assert payload["marketCap"] == 2_900_000_000_000
    assert payload["companyOfficers"][0]["name"] == "Ada Example"
    assert row.retrieved_at_utc.utcoffset().total_seconds() == 0


def test_equity_quote_is_fast_info_not_historical_close(
    equity_quote_sdk_response: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        _sdk,
        "require_fast_info",
        lambda symbol: calls.append(symbol) or equity_quote_sdk_response,
    )
    fetcher = YfinanceEquityQuoteFetcher()
    query = fetcher.transform_query(symbol="aapl")
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    assert calls == ["AAPL"]
    assert json.loads(row.raw_payload_json)["lastPrice"] == 187.0


def test_etf_info_retains_fund_sections(
    etf_info_sdk_response: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_sdk, "require_fund_sections", lambda symbol: etf_info_sdk_response)
    fetcher = YfinanceEtfInfoFetcher()
    query = fetcher.transform_query(symbol="spy")
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    payload = json.loads(row.raw_payload_json)
    assert payload["asset_classes"]["stock"] == pytest.approx(0.92)
    assert payload["top_holdings"][0]["symbol"] == "AAPL"


def test_key_executives_gets_only_officer_records(
    equity_info_sdk_response: dict[str, object],
    executives_sdk_response: list[dict[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sdk, "require_info", lambda symbol: equity_info_sdk_response)
    fetcher = YfinanceKeyExecutivesFetcher()
    query = fetcher.transform_query(symbol="AAPL")
    raw = {**equity_info_sdk_response, "companyOfficers": executives_sdk_response}
    monkeypatch.setattr(_sdk, "require_info", lambda symbol: raw)
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert len(rows) == 2
    assert rows[0].symbol == "AAPL"
    assert rows[0].name == "Ada Example"
    assert rows[0].title == "Chief Executive Officer"
    assert rows[0].pay == 12_345_678
    assert rows[0].year_born == 1970
    assert rows[0].fiscal_year == 2024
    assert rows[0].exercised_value == 2_000_001
    assert rows[0].unexercised_value == 3_000_002
    assert rows[0].currency_pay is None and rows[0].gender is None
    serialized = rows[0].model_dump(mode="json")
    assert serialized["pay"] == 12_345_678
    assert serialized["year_born"] == 1970
    assert "totalPay" not in serialized
    assert json.loads(rows[1].raw_payload_json)["title"] == "Chief Financial Officer"


@pytest.mark.parametrize(
    "invalid_officer",
    [
        {"name": "Ada Example", "yearBorn": 1970},
        {"title": "Chief Executive Officer", "yearBorn": 1970},
        {
            "name": "Ada Example",
            "title": "Chief Executive Officer",
            "totalPay": 12.5,
        },
        {
            "name": "Ada Example",
            "title": "Chief Executive Officer",
            "totalPay": True,
        },
    ],
)
def test_key_executives_rejects_missing_identity_and_noninteger_pay(
    invalid_officer: dict[str, object],
) -> None:
    fetcher = YfinanceKeyExecutivesFetcher()
    query = fetcher.transform_query(symbol="AAPL")
    with pytest.raises(YfinanceProviderError):
        fetcher.transform_data(("AAPL", [invalid_officer]), query)


def test_share_statistics_selects_share_fields_without_using_quote_values(
    share_statistics_sdk_response: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_sdk, "require_info", lambda symbol: share_statistics_sdk_response)
    fetcher = YfinanceShareStatisticsFetcher()
    query = fetcher.transform_query(symbol="AAPL")
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    payload = json.loads(row.raw_payload_json)
    assert payload["sharesOutstanding"] == 15_000_000_000
    assert "regularMarketPrice" not in payload


def test_executives_and_share_statistics_fail_closed_on_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_sdk, "require_info", lambda symbol: {"symbol": symbol})
    executives = YfinanceKeyExecutivesFetcher()
    with pytest.raises(YfinanceProviderError) as error:
        executives.extract_data(executives.transform_query(symbol="AAPL"), _sdk_context())
    assert error.value.code == "YFINANCE_EXECUTIVES_SHAPE_INVALID"
    shares = YfinanceShareStatisticsFetcher()
    with pytest.raises(YfinanceProviderError) as error:
        shares.extract_data(shares.transform_query(symbol="AAPL"), _sdk_context())
    assert error.value.code == "YFINANCE_SHARE_STATISTICS_MISSING"


def test_key_executives_rejects_info_for_a_different_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _sdk,
        "require_info",
        lambda _symbol: {
            "symbol": "MSFT",
            "companyOfficers": [{"name": "Ada Example", "title": "Chief Executive Officer"}],
        },
    )
    fetcher = YfinanceKeyExecutivesFetcher()
    query = fetcher.transform_query(symbol="AAPL")
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.extract_data(query, _sdk_context())
    assert error.value.code == "YFINANCE_EXECUTIVES_SYMBOL_MISMATCH"


@pytest.fixture
def key_metrics_sdk_response() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Current": [31.2, 4.5],
            pd.Timestamp("2024-09-30"): [29.8, None],
            pd.Timestamp("2023-09-30"): [27.1, 3.8],
        },
        index=["Trailing P/E", "Price/Sales"],
    )


def test_key_metrics_separates_current_from_dated_history(
    key_metrics_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        _sdk,
        "require_valuation_measures",
        lambda symbol, *, frequency, periods: (
            calls.append((symbol, frequency, periods)) or key_metrics_sdk_response
        ),
    )
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="aapl", frequency="yearly", periods=3)
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert calls == [("AAPL", "yearly", 3)]
    current = next(row for row in rows if row.period_kind == "current")
    missing = next(row for row in rows if row.period_ending == date(2024, 9, 30))
    historical = next(row for row in rows if row.period_ending == date(2023, 9, 30))
    assert current.period_ending is None and current.period_date is None
    assert current.pe_ratio == pytest.approx(31.2)
    assert current.price_to_sales == pytest.approx(4.5)
    assert current.metric_availability["pe_ratio"] == "reported"
    assert current.metric_availability["dividend_yield"] == "not_returned_by_sdk_method"
    assert missing.period_kind == "historical"
    assert missing.period_ending == missing.period_date == date(2024, 9, 30)
    assert missing.price_to_sales is None
    assert missing.metric_availability["price_to_sales"] == "source_missing"
    assert historical.price_to_sales == pytest.approx(3.8)


def test_key_metrics_preserves_missing_sdk_scalars_as_null() -> None:
    frame = pd.DataFrame(
        {
            "Current": [float("nan"), pd.NA],
            "12/31/2024": [pd.NA, float("nan")],
        },
        index=["Trailing P/E", "Price/Sales"],
    )
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="AAPL", ttm="include", limit=1)
    rows = fetcher.transform_data((("AAPL", frame),), query)

    for row in rows:
        if row.period_kind == "current":
            assert row.pe_ratio is None
            assert row.price_to_sales is None
        else:
            assert row.pe_ratio is None
            assert row.price_to_sales is None
        assert row.metric_availability["pe_ratio"] == "source_missing"
        assert row.metric_availability["price_to_sales"] == "source_missing"


def test_key_metrics_parses_official_yfinance_m_d_yyyy_period_columns() -> None:
    """The official valuation-measures shape labels periods as M/D/YYYY strings."""
    frame = pd.DataFrame({"Current": [31.2], "12/31/2024": [30.4]}, index=["Trailing P/E"])
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="AAPL", ttm="include", limit=1)
    rows = fetcher.transform_data((("AAPL", frame),), query)
    historical = next(row for row in rows if row.period_kind == "historical")
    assert historical.period_ending == historical.period_date == date(2024, 12, 31)
    assert historical.pe_ratio == pytest.approx(30.4)
    assert historical.metric_availability["pe_ratio"] == "reported"
    assert historical.metric_availability["enterprise_value"] == "not_returned_by_sdk_method"


def test_key_metrics_rejects_unknown_or_duplicate_period_identity() -> None:
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="AAPL", ttm="include")
    unknown = pd.DataFrame({"Current": [1.0]}, index=["Unknown Measure"])
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((("AAPL", unknown),), query)
    assert error.value.code == "YFINANCE_METRIC_NAME_UNMAPPED"

    duplicate = pd.DataFrame(
        [[1.0, 2.0]],
        columns=["12/31/2024", "12/31/2024"],
        index=["Trailing P/E"],
    )
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((("AAPL", duplicate),), query)
    assert error.value.code == "YFINANCE_METRICS_SHAPE_INVALID"

    fractional_enterprise_value = pd.DataFrame({"Current": [10.5]}, index=["Enterprise Value"])
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((("AAPL", fractional_enterprise_value),), query)
    assert error.value.code == "YFINANCE_METRIC_INTEGER_INVALID"


def test_key_metrics_historical_query_does_not_fall_back_to_current(
    key_metrics_sdk_response: pd.DataFrame, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        _sdk, "require_valuation_measures", lambda *args, **kwargs: key_metrics_sdk_response
    )
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="AAPL", start_date="2024-01-01", end_date="2024-12-31")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert rows
    assert all(row.period_kind == "historical" for row in rows)
    with pytest.raises(ValidationError):
        fetcher.transform_query(symbol="AAPL", period="q1")


@pytest.mark.parametrize(
    ("query_fields", "expected_frequency", "expected_periods", "expected_kinds"),
    [
        ({"ttm": "only", "period": "annual"}, "yearly", 0, {"current"}),
        (
            {"ttm": "include", "period": "annual", "limit": 2},
            "yearly",
            2,
            {"current", "historical"},
        ),
        ({"ttm": "exclude", "period": "quarter", "limit": 3}, "quarterly", 3, {"historical"}),
    ],
)
def test_key_metrics_public_ttm_period_and_limit_mapping(
    key_metrics_sdk_response: pd.DataFrame,
    query_fields: dict[str, object],
    expected_frequency: str,
    expected_periods: int,
    expected_kinds: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        _sdk,
        "require_valuation_measures",
        lambda symbol, *, frequency, periods: (
            calls.append((symbol, frequency, periods)) or key_metrics_sdk_response
        ),
    )
    fetcher = YfinanceKeyMetricsFetcher()
    query = fetcher.transform_query(symbol="AAPL", **query_fields)
    raw = fetcher.extract_data(query, _sdk_context())
    rows = fetcher.transform_data(raw, query)
    assert calls == [("AAPL", expected_frequency, expected_periods)]
    assert {row.period_kind for row in rows} == expected_kinds


def test_key_metrics_rejects_unmappable_fiscal_quarter_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(
        _sdk,
        "require_valuation_measures",
        lambda *args, **kwargs: calls.append(args),
    )
    with pytest.raises(ValidationError):
        YfinanceKeyMetricsFetcher().fetch(symbol="AAPL", ttm="exclude", period="q1")
    with pytest.raises(ValidationError):
        YfinanceKeyMetricsFetcher().fetch(symbol="AAPL", limit=5)
    assert calls == []


@pytest.fixture
def options_sdk_response() -> tuple[pd.DataFrame, pd.DataFrame]:
    calls = pd.DataFrame(
        {
            "contractSymbol": ["AAPL270115C00100000"],
            "strike": [100.0],
            "lastPrice": [90.0],
            "bid": [89.5],
            "ask": [90.5],
            "volume": [100],
            "openInterest": [400],
            "impliedVolatility": [0.22],
            "inTheMoney": [True],
        }
    )
    puts = pd.DataFrame(
        {
            "contractSymbol": ["AAPL270115P00100000"],
            "strike": [100.0],
            "lastPrice": [1.25],
            "bid": [1.20],
            "ask": [1.30],
            "volume": [70],
            "openInterest": [250],
            "impliedVolatility": [0.27],
            "inTheMoney": [False],
        }
    )
    return calls, puts


def test_options_chain_keeps_call_put_and_requested_expiration(
    options_sdk_response: tuple[pd.DataFrame, pd.DataFrame], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        _sdk,
        "require_options_chain",
        lambda symbol, expiration: calls.append((symbol, expiration)) or options_sdk_response,
    )
    fetcher = YfinanceOptionsChainsFetcher()
    query = fetcher.transform_query(symbol="aapl", expiration_date="2027-01-15")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    assert calls == [("AAPL", "2027-01-15")]
    assert [row.option_type for row in rows] == ["call", "put"]
    assert all(row.expiration_date == date(2027, 1, 15) for row in rows)
    assert json.loads(rows[0].raw_payload_json)["contractSymbol"] == "AAPL270115C00100000"


def test_options_chain_rejects_bad_shape_and_bad_expiration_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(_sdk, "require_options_chain", lambda *args: calls.append(args))
    fetcher = YfinanceOptionsChainsFetcher()
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", expiration_date="2020-01-17")
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL", date="2027-01-15")
    with pytest.raises(ValidationError):
        fetcher.fetch(symbol="AAPL")
    assert calls == []
    query = fetcher.transform_query(symbol="AAPL", expiration_date=date.today() + timedelta(days=5))
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((pd.DataFrame(), pd.DataFrame()), query)
    assert error.value.code == "YFINANCE_OPTIONS_SHAPE_INVALID"


def test_price_target_consensus_requires_the_documented_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {"current": 187.0, "low": 180.0, "high": 240.0, "mean": 215.0, "median": 220.0}
    calls: list[str] = []
    monkeypatch.setattr(
        _sdk,
        "require_price_targets",
        lambda symbol: calls.append(symbol) or response,
    )
    fetcher = YfinancePriceTargetFetcher()
    query = fetcher.transform_query(symbol="aapl")
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    assert calls == ["AAPL"]
    assert row.symbol == "AAPL"
    assert row.current_price == pytest.approx(187.0)
    assert row.target_low == pytest.approx(180.0)
    assert row.target_high == pytest.approx(240.0)
    assert row.target_consensus == pytest.approx(215.0)
    assert row.target_median == pytest.approx(220.0)
    serialized = row.model_dump(mode="json")
    assert serialized["target_consensus"] == pytest.approx(215.0)
    assert "mean" not in serialized
    assert row.mean == 215.0 and row.median == 220.0
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((("AAPL", {"current": 187.0}),), query)
    assert error.value.code == "YFINANCE_PRICE_TARGET_SHAPE_INVALID"
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data((("MSFT", response),), query)
    assert error.value.code == "YFINANCE_PRICE_TARGET_SYMBOL_MISMATCH"


@pytest.fixture
def screener_active_response() -> dict[str, object]:
    return {
        "quotes": [{"symbol": "AAA", "regularMarketPrice": 10.5, "dayVolume": 9000}],
        "count": 1,
    }


@pytest.fixture
def screener_small_caps_response() -> dict[str, object]:
    return {"quotes": [{"symbol": "BBB", "marketCap": 800_000_000, "exchange": "NMS"}], "count": 1}


@pytest.fixture
def screener_gainers_response() -> dict[str, object]:
    return {"quotes": [{"symbol": "CCC", "regularMarketChangePercent": 0.08}], "count": 1}


@pytest.fixture
def screener_losers_response() -> dict[str, object]:
    return {"quotes": [{"symbol": "DDD", "regularMarketChangePercent": -0.06}], "count": 1}


@pytest.fixture
def screener_undervalued_growth_response() -> dict[str, object]:
    return {"quotes": [{"symbol": "EEE", "trailingPE": 12.0, "pegRatio": 0.8}], "count": 1}


@pytest.fixture
def screener_large_caps_response() -> dict[str, object]:
    return {"quotes": [{"symbol": "FFF", "marketCap": 50_000_000_000}], "count": 1}


@pytest.fixture
def screener_growth_tech_response() -> dict[str, object]:
    return {
        "quotes": [{"symbol": "GGG", "sector": "Technology", "quarterlyRevenueGrowth": 0.30}],
        "count": 1,
    }


@pytest.mark.parametrize(
    ("fetcher_type", "fixture_name", "query_name"),
    [
        (YfinanceEquityActiveFetcher, "screener_active_response", "most_actives"),
        (
            YfinanceEquityAggressiveSmallCapsFetcher,
            "screener_small_caps_response",
            "aggressive_small_caps",
        ),
        (YfinanceEquityGainersFetcher, "screener_gainers_response", "day_gainers"),
        (YfinanceEquityLosersFetcher, "screener_losers_response", "day_losers"),
        (
            YfinanceEquityUndervaluedGrowthFetcher,
            "screener_undervalued_growth_response",
            "undervalued_growth_stocks",
        ),
        (
            YfinanceEquityUndervaluedLargeCapsFetcher,
            "screener_large_caps_response",
            "undervalued_large_caps",
        ),
        (
            YfinanceGrowthTechEquitiesFetcher,
            "screener_growth_tech_response",
            "growth_technology_stocks",
        ),
    ],
)
def test_each_predefined_screener_uses_its_own_sdk_fixture(
    fetcher_type: type,
    fixture_name: str,
    query_name: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = request.getfixturevalue(fixture_name)
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        _sdk,
        "require_screen",
        lambda query, *, offset, limit, predefined, sort_field, sort_ascending: (
            calls.append((query, offset, limit, predefined, sort_field, sort_ascending)) or response
        ),
    )
    fetcher = fetcher_type()
    query = fetcher.transform_query(limit=12, sort="asc")
    rows = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)
    expected_sort_field = {
        "most_actives": "dayvolume",
        "aggressive_small_caps": "eodvolume",
        "day_gainers": "percentchange",
        "day_losers": "percentchange",
        "undervalued_growth_stocks": "eodvolume",
        "undervalued_large_caps": "eodvolume",
        "growth_technology_stocks": "eodvolume",
    }[query_name]
    assert calls == [(query_name, 0, 12, True, expected_sort_field, True)]
    assert rows[0].ordinal == 0
    assert rows[0].symbol == response["quotes"][0]["symbol"]


def test_custom_equity_screener_maps_public_filters_and_rejects_bad_values_before_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = {"quotes": [{"symbol": "AAA", "exchange": "NMS"}], "count": 1}
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        _sdk,
        "require_screen",
        lambda query, *, offset, limit, predefined, sort_field=None, sort_ascending=None: (
            calls.append((query, offset, limit, predefined, sort_field, sort_ascending)) or response
        ),
    )
    fetcher = YfinanceEquityScreenerFetcher()
    query = fetcher.transform_query(
        country="USA",
        exchange="nms",
        sector="technology",
        industry="Software—Infrastructure",
        mktcap_min=1_000_000_000,
        mktcap_max=5_000_000_000,
        price_min=10,
        volume_min=100_000,
        beta_max=2.5,
        limit=20,
    )
    row = fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)[0]
    assert row.symbol == "AAA"
    assert calls[0][0] == {
        "operator": "and",
        "operands": [
            {"operator": "eq", "operands": ["region", "us"]},
            {"operator": "eq", "operands": ["exchange", "NMS"]},
            {"operator": "eq", "operands": ["sector", "Technology"]},
            {"operator": "eq", "operands": ["industry", "Software—Infrastructure"]},
            {"operator": "gte", "operands": ["intradaymarketcap", 1_000_000_000]},
            {"operator": "lte", "operands": ["intradaymarketcap", 5_000_000_000]},
            {"operator": "gte", "operands": ["intradayprice", 10.0]},
            {"operator": "gte", "operands": ["dayvolume", 100_000]},
            {"operator": "lte", "operands": ["beta", 2.5]},
        ],
    }
    assert calls[0][1:] == (0, 20, False, None, None)
    with pytest.raises(ValidationError):
        fetcher.fetch(exchange="not-a-yfinance-exchange")
    assert len(calls) == 1


def test_screener_response_missing_quotes_fails_closed() -> None:
    fetcher = YfinanceEquityActiveFetcher()
    query = fetcher.transform_query()
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data({"count": 0}, query)
    assert error.value.code == "YFINANCE_SCREEN_SHAPE_INVALID"


def test_yfinance_query_wrappers_call_documented_methods_and_normalize_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class Funds:
        asset_classes = {"stock": 1.0}
        description = "Fixture fund"
        fund_operations = {}
        fund_overview = {}
        equity_holdings = {}
        top_holdings = []
        sector_weightings = {}
        bond_holdings = []
        bond_ratings = {}

    class Chain:
        calls = pd.DataFrame()
        puts = pd.DataFrame()

    class Ticker:
        def __init__(self, symbol: str) -> None:
            calls["symbol"] = symbol

        def get_news(self, **kwargs: object) -> list[dict[str, object]]:
            calls["news"] = kwargs
            return []

        def get_info(self) -> dict[str, object]:
            calls["info"] = True
            return {"longName": "Fixture"}

        @property
        def fast_info(self) -> dict[str, object]:
            calls["fast_info"] = True
            return {"lastPrice": 10.0}

        @property
        def funds_data(self) -> Funds:
            calls["funds_data"] = True
            return Funds()

        def get_valuation_measures(self, **kwargs: object) -> pd.DataFrame:
            calls["valuation_method"] = kwargs
            return pd.DataFrame({"Current": [1.0]}, index=["P/E"])

        def option_chain(self, expiration: str) -> Chain:
            calls["option_chain"] = expiration
            return Chain()

        def get_analyst_price_targets(self) -> dict[str, object]:
            calls["targets"] = True
            return {"current": 10, "low": 9, "high": 12, "mean": 11, "median": 11}

    class YfData:
        """The data layer the pinned 0.2.66 generation actually offers.

        ``require_valuation_measures`` builds its own fundamentals-timeseries request through
        this object because ``Ticker.get_valuation_measures`` is the newer generation's method;
        the assertion below is that the request carries the documented measure types and that the
        answer is normalized into Current-plus-dated columns, which is what the row mapper needs.
        """

        _instances: dict[type, object] = {}

        def __init__(self, session: object = None, **_kwargs: object) -> None:
            self._session = session
            YfData._instances[YfData] = self

        def get_raw_json(
            self, url: str, params: dict[str, object] | None = None, timeout: float = 30.0
        ) -> dict[str, object]:
            calls["valuation_url"] = url
            calls["valuation"] = params or {}
            return {
                "timeseries": {
                    "result": [
                        {
                            "meta": {"symbol": ["AAPL"]},
                            "trailingMarketCap": [
                                {"asOfDate": "2025-01-02", "reportedValue": {"raw": 1.0}},
                            ],
                            "quarterlyMarketCap": [
                                {"asOfDate": "2024-09-30", "reportedValue": {"raw": 2.0}},
                            ],
                        }
                    ]
                }
            }

    monkeypatch.setattr(
        _sdk,
        "_require_sdk",
        lambda: types.SimpleNamespace(
            Ticker=Ticker,
            data=types.SimpleNamespace(YfData=YfData),
            const=types.SimpleNamespace(_BASE_URL_="https://query1.finance.yahoo.com"),
        ),
    )
    monkeypatch.setattr(
        _sdk,
        "governed_yfinance_session",
        lambda _fake_sdk: nullcontext(None),
    )
    assert _sdk.require_news("AAPL", count=6) == []
    assert _sdk.require_info("AAPL")["longName"] == "Fixture"
    assert _sdk.require_fast_info("AAPL")["lastPrice"] == 10.0
    assert _sdk.require_fund_sections("SPY")["asset_classes"] == {"stock": 1.0}
    measures = _sdk.require_valuation_measures("AAPL", frequency="quarterly", periods=2)
    assert measures is not None
    assert measures.loc["Market Cap", "Current"] == 1.0
    assert measures.loc["Market Cap", "9/30/2024"] == 2.0
    assert "P/E" not in list(measures.index)
    assert _sdk.require_options_chain("AAPL", "2027-01-15")[0].empty
    assert _sdk.require_price_targets("AAPL")["mean"] == 11
    assert calls["news"] == {"count": 6, "tab": "news"}
    assert calls.get("valuation_method") is None
    assert str(calls["valuation_url"]).endswith(
        "/ws/fundamentals-timeseries/v1/finance/timeseries/AAPL"
    )
    requested = str(calls["valuation"]["type"]).split(",")
    assert requested[:2] == ["quarterlyMarketCap", "trailingMarketCap"]
    assert len(requested) == 18
    assert calls["valuation"]["symbol"] == "AAPL"
    assert calls["valuation"]["period1"] == _sdk._VALUATION_PERIOD1
    assert calls["option_chain"] == "2027-01-15"


def _curve_sdk(
    monkeypatch: pytest.MonkeyPatch,
    replies: list[object],
    sdk_origin: object = "https://query1.finance.yahoo.com",
) -> list[tuple[str, dict[str, object]]]:
    """Use the existing fake YfData/nullcontext seam, not require_futures_curve or its parsers."""
    calls: list[tuple[str, dict[str, object]]] = []
    session_token = object()

    class YfData:
        def __init__(self, session: object = None) -> None:
            assert session is session_token

        def get_raw_json(self, *, url: str, params: dict[str, object]) -> object:
            calls.append((url, dict(params)))
            reply = replies[len(calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return reply

    fake_sdk = types.SimpleNamespace(
        data=types.SimpleNamespace(YfData=YfData),
        const=types.SimpleNamespace(_BASE_URL_=sdk_origin),
    )
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: fake_sdk)
    monkeypatch.setattr(_sdk, "governed_yfinance_session", lambda sdk: nullcontext(session_token))
    return calls


def _curve_replies() -> list[dict[str, Any]]:
    """Wire-shaped witness envelopes are synthetic; these are not official market prices."""
    return [response["body"] for response in _chain_document()["responses"][2:]]


def _run_curve(symbol: str = " es ") -> Any:
    fetcher = YfinanceFuturesCurveFetcher()
    query = fetcher.transform_query(symbol=symbol)
    return fetcher.transform_data(fetcher.extract_data(query, _sdk_context()), query)


def test_futures_curve_joins_chain_quotes_encodes_root_and_preserves_each_quote_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _curve_replies()
    replies[0]["quoteSummary"]["result"][0]["futuresChain"]["futures"].extend(["ESU27.CME", ""])
    # Even if quoted, the continuous root has no expiration and must be skipped.
    replies[1]["quoteResponse"]["result"].append({"symbol": "ES=F"})
    calls = _curve_sdk(monkeypatch, replies)
    rows = _run_curve()
    assert [row.model_dump(mode="json") for row in rows] == [
        {"symbol": "ES", "date": "2026-10-16", "expiration": "2026-12-01", "price": 6211.0},
        {"symbol": "ES", "date": "2026-10-15", "expiration": "2027-03-01", "price": 6345.75},
        {"symbol": "ES", "date": "2026-10-15", "expiration": "2027-06-01", "price": None},
    ]
    assert calls == [
        (
            "https://query1.finance.yahoo.com/v10/finance/quoteSummary/ES%3DF",
            {"modules": "futuresChain"},
        ),
        (
            "https://query1.finance.yahoo.com/v7/finance/quote",
            {"symbols": "ES=F,ESZ26.CME,ESH27.CME,ESM27.CME,ESU27.CME"},
        ),
    ]


@pytest.mark.parametrize(
    ("code", "month"),
    [
        ("F", 1),
        ("G", 2),
        ("H", 3),
        ("J", 4),
        ("K", 5),
        ("M", 6),
        ("N", 7),
        ("Q", 8),
        ("U", 9),
        ("V", 10),
        ("X", 11),
        ("Z", 12),
    ],
)
def test_futures_curve_decodes_every_contract_month_through_the_two_leg_request(
    code: str,
    month: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = f"CL{code}28.NYM"
    replies = _curve_replies()
    replies[0]["quoteSummary"]["result"][0]["futuresChain"]["futures"] = [symbol]
    replies[1]["quoteResponse"]["result"] = [
        {"symbol": symbol, "regularMarketTime": 1792161000, "lastMarket": 71.25}
    ]
    calls = _curve_sdk(monkeypatch, replies)
    row = _run_curve(" cl ")[0]
    assert row.model_dump() == {
        "symbol": "CL",
        "date": date(2026, 10, 16),
        "expiration": date(2028, month, 1),
        "price": 71.25,
    }
    assert calls == [
        (
            "https://query1.finance.yahoo.com/v10/finance/quoteSummary/CL%3DF",
            {"modules": "futuresChain"},
        ),
        ("https://query1.finance.yahoo.com/v7/finance/quote", {"symbols": symbol}),
    ]


@pytest.mark.parametrize(
    ("quote", "price"),
    [
        ({"bid": 100.1, "ask": 100.15, "lastMarket": 900}, 100.12),
        ({"bid": 0, "ask": 0, "lastMarket": 900}, 0.0),
        ({"bid": 100, "lastMarket": 101, "regularMarketPrice": 102, "previousClose": 103}, 101.0),
        ({"ask": 100, "lastMarket": "101", "regularMarketPrice": 102, "previousClose": 103}, 102.0),
        (
            {
                "bid": True,
                "ask": 100,
                "lastMarket": float("nan"),
                "regularMarketPrice": False,
                "previousClose": 103,
            },
            103.0,
        ),
        (
            {
                "bid": float("inf"),
                "ask": 100,
                "lastMarket": None,
                "regularMarketPrice": None,
                "previousClose": -1.5,
            },
            -1.5,
        ),
        (
            {
                "bid": "100",
                "ask": 100,
                "lastMarket": {},
                "regularMarketPrice": float("inf"),
                "previousClose": "103",
            },
            None,
        ),
        ({}, None),
    ],
)
def test_futures_curve_midpoint_fallback_precedence_and_null_price(
    quote: dict[str, object],
    price: float | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _curve_replies()
    replies[0]["quoteSummary"]["result"][0]["futuresChain"]["futures"] = ["ESZ26.CME"]
    replies[1]["quoteResponse"]["result"] = [
        {"symbol": "ESZ26.CME", "regularMarketTime": 1792161000, **quote}
    ]
    calls = _curve_sdk(monkeypatch, replies)
    assert _run_curve()[0].model_dump(mode="json") == {
        "symbol": "ES",
        "date": "2026-10-16",
        "expiration": "2026-12-01",
        "price": price,
    }
    assert calls[1][1] == {"symbols": "ESZ26.CME"}


@pytest.mark.parametrize("stamp", [None, 0, -1, True, "1792161000", float("nan"), float("inf")])
def test_futures_curve_refuses_missing_or_invalid_quote_timestamp_instead_of_stamping_now(
    stamp: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _curve_replies()
    replies[1]["quoteResponse"]["result"][0]["regularMarketTime"] = stamp
    calls = _curve_sdk(monkeypatch, replies)
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_FUTURES_CURVE_AS_OF_MISSING"
    assert len(calls) == 2


@pytest.mark.parametrize("symbol", ["ES=F", "X", "ESZXX.CME", "ESA26.CME", "ESU27.CME"])
def test_futures_curve_refuses_when_every_chain_entry_is_undecodable_or_unquoted(
    symbol: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _curve_replies()
    replies[0]["quoteSummary"]["result"][0]["futuresChain"]["futures"] = [symbol]
    replies[1]["quoteResponse"]["result"] = [] if symbol == "ESU27.CME" else [{"symbol": symbol}]
    calls = _curve_sdk(monkeypatch, replies)
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_FUTURES_CURVE_EMPTY"
    assert calls[1][1] == {"symbols": symbol}


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ([], "SHAPE_INVALID"),
        ({}, "SHAPE_INVALID"),
        ({"quoteSummary": []}, "SHAPE_INVALID"),
        ({"quoteSummary": {"result": {}}}, "SHAPE_INVALID"),
        ({"quoteSummary": {"result": []}}, "EMPTY"),
        ({"quoteSummary": {"result": [None]}}, "SHAPE_INVALID"),
        ({"quoteSummary": {"result": [{"futuresChain": []}]}}, "SHAPE_INVALID"),
        ({"quoteSummary": {"result": [{"futuresChain": {"futures": {}}}]}}, "SHAPE_INVALID"),
        ({"quoteSummary": {"result": [{"futuresChain": {"futures": [True]}}]}}, "SHAPE_INVALID"),
    ],
)
def test_futures_curve_invalid_chain_envelope_refuses_before_quote_request(
    body: object,
    code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _curve_sdk(monkeypatch, [body])
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == f"YFINANCE_FUTURES_CHAIN_{code}"
    assert calls == [
        (
            "https://query1.finance.yahoo.com/v10/finance/quoteSummary/ES%3DF",
            {"modules": "futuresChain"},
        )
    ]


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"quoteResponse": []},
        {"quoteResponse": {"result": {}}},
        {"quoteResponse": {"result": [None]}},
    ],
)
def test_futures_curve_invalid_quote_envelope_refuses_after_exactly_two_legs(
    body: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _curve_sdk(monkeypatch, [_curve_replies()[0], body])
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_FUTURES_QUOTES_SHAPE_INVALID"
    assert len(calls) == 2
    assert calls[1] == (
        "https://query1.finance.yahoo.com/v7/finance/quote",
        {"symbols": "ES=F,ESZ26.CME,ESH27.CME,ESM27.CME"},
    )


def test_futures_curve_ignores_quote_rows_without_symbol_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _curve_replies()
    replies[1]["quoteResponse"]["result"] = [{}, {"symbol": True}, {"symbol": ""}]
    calls = _curve_sdk(monkeypatch, replies)
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_FUTURES_CURVE_EMPTY"
    assert len(calls) == 2


@pytest.mark.parametrize("sdk_origin", [None, 42, "http://query1.finance.yahoo.com"])
def test_futures_curve_missing_sdk_query_surface_refuses_without_a_request(
    sdk_origin: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _curve_sdk(monkeypatch, [], sdk_origin)
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_SDK_SURFACE_MISSING"
    assert calls == []


@pytest.mark.parametrize("stage", [0, 1])
def test_futures_curve_wraps_sdk_transport_failure_and_preserves_budget_refusal(
    stage: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies: list[object] = _curve_replies()
    transport_error = OSError("fixture send failed")
    replies[stage] = transport_error
    calls = _curve_sdk(monkeypatch, replies)
    with pytest.raises(YfinanceProviderError) as error:
        _run_curve()
    assert error.value.code == "YFINANCE_REQUEST_FAILED"
    assert error.value.__cause__ is transport_error
    assert len(calls) == stage + 1

    budget_error = RequestAttemptLimitError("fixture budget exhausted")
    replies[stage] = budget_error
    calls = _curve_sdk(monkeypatch, replies)
    with pytest.raises(RequestAttemptLimitError) as error:
        _run_curve()
    assert error.value is budget_error
    assert len(calls) == stage + 1


def test_futures_curve_dated_query_is_refused_before_sdk_or_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sdk_calls: list[str] = []
    monkeypatch.setattr(_sdk, "_require_sdk", lambda: sdk_calls.append("sdk"))
    with pytest.raises(YfinanceProviderError) as error:
        YfinanceFuturesCurveFetcher().fetch(symbol="ES", date="2026-10-16")
    assert error.value.code == "YFINANCE_BAD_QUERY"
    assert sdk_calls == []


@pytest.mark.parametrize("raw", [None, [], (), ([],), ({}, {}), ([], []), ([True], {})])
def test_futures_curve_refuses_invalid_stage_envelope(raw: object) -> None:
    fetcher = YfinanceFuturesCurveFetcher()
    query = fetcher.transform_query(symbol="ES")
    with pytest.raises(YfinanceProviderError) as error:
        fetcher.transform_data(raw, query)
    assert error.value.code == "YFINANCE_FUTURES_CURVE_SHAPE_INVALID"


def test_futures_curve_rejects_the_wrong_query_type_at_both_stage_handoffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _curve_sdk(monkeypatch, [])
    fetcher = YfinanceFuturesCurveFetcher()
    for stage in (
        lambda: fetcher.extract_data(types.SimpleNamespace(symbol="ES"), _sdk_context()),
        lambda: fetcher.transform_data(([], {}), types.SimpleNamespace(symbol="ES")),
    ):
        with pytest.raises(YfinanceProviderError) as error:
            stage()
        assert error.value.code == "YFINANCE_BAD_QUERY"
    assert calls == []
