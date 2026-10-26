"""Fail-closed tests for the Eastmoney stock_daily migration adapter.

Every frame below is synthetic and is used only to pin parsing and contract
behavior. The available Eastmoney upstream fixture metadata is pending because
its attempted capture returned HTTP 502, so these tests are not source evidence.
"""

from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

import opendata.data.providers.akshare._vendor as opendata_http
from opendata.data.models import Bar
from opendata.data.providers.akshare.models.stock_daily import AkshareStockDailyFetcher


def _synthetic_eastmoney_frame() -> pd.DataFrame:
    """Return a synthetic Eastmoney-shaped row with native source units."""
    return pd.DataFrame(
        {
            "日期": ["2024-01-02"],
            "股票代码": ["600519"],
            "开盘": [1685.0],
            "收盘": [1688.0],
            "最高": [1690.0],
            "最低": [1680.0],
            "成交量": [1.23],
            "成交额": [2_076_240.0],
        }
    )


def test_normalizes_synthetic_eastmoney_golden_row_with_documented_units() -> None:
    """Pin the exact Bar fields, including lots-to-shares conversion."""
    fetcher = AkshareStockDailyFetcher()
    params = fetcher.transform_query(symbol="600519.SH", adjust="raw")

    rows = fetcher.transform_data(_synthetic_eastmoney_frame(), params)

    assert rows == [
        Bar(
            symbol="600519",
            trade_date=date(2024, 1, 2),
            open=1685.0,
            high=1690.0,
            low=1680.0,
            close=1688.0,
            volume=123.0,
            amount=2_076_240.0,
        )
    ]


@pytest.mark.parametrize("symbol", ["600519.SZ", "sz600519", "100001", "60051"])
def test_query_rejects_invalid_or_conflicting_instrument_symbols(symbol: str) -> None:
    """Reject non-A-share codes and suffixes that disagree with the code."""
    with pytest.raises(ValidationError, match="symbol"):
        AkshareStockDailyFetcher().transform_query(symbol=symbol)


def test_normalizer_rejects_response_for_another_instrument() -> None:
    """Do not relabel a provider row to the requested symbol."""
    frame = _synthetic_eastmoney_frame()
    frame.loc[0, "股票代码"] = "000001"
    params = AkshareStockDailyFetcher().transform_query(symbol="600519")

    with pytest.raises(ValueError, match="does not match requested symbol"):
        AkshareStockDailyFetcher().transform_data(frame, params)


@pytest.mark.parametrize("bad_date", ["2024-02-30", "not-a-date", pd.NaT, 20240102])
def test_normalizer_rejects_invalid_source_business_dates(bad_date: object) -> None:
    """Reject invalid values instead of dropping rows or epoch-coercing numbers."""
    frame = _synthetic_eastmoney_frame()
    frame.loc[0, "日期"] = bad_date
    params = AkshareStockDailyFetcher().transform_query(symbol="600519")

    with pytest.raises(ValueError, match="invalid business date"):
        AkshareStockDailyFetcher().transform_data(frame, params)


@pytest.mark.parametrize("adjust", ["qfq", "hfq"])
def test_adjusted_prices_are_not_mislabeled_as_unadjusted_bars(adjust: str) -> None:
    """D10 Bar data rejects adjusted values until adjustment has its own identity."""
    with pytest.raises(ValidationError, match="unadjusted Bar contract"):
        AkshareStockDailyFetcher().fetch(symbol="600519", adjust=adjust)


@pytest.mark.parametrize("adjust", ["", "raw", "none", "unadjusted"])
def test_supported_unadjusted_aliases_always_request_raw_source_prices(
    monkeypatch: pytest.MonkeyPatch, adjust: str
) -> None:
    """The only supported Bar basis maps to Eastmoney's unadjusted flag."""
    seen: dict[str, object] = {}

    def fake_hist(**kwargs: object) -> pd.DataFrame:
        seen.update(kwargs)
        return _synthetic_eastmoney_frame()

    monkeypatch.setattr(opendata_http, "stock_zh_a_hist", fake_hist)
    result = AkshareStockDailyFetcher().fetch(symbol="600519", adjust=adjust)

    assert len(result) == 1
    assert seen["adjust"] == ""
    assert seen["symbol"] == "600519"


def test_query_date_window_is_inclusive_and_passed_to_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both requested endpoint dates are retained and sent without drift."""
    frame = pd.concat(
        [
            _synthetic_eastmoney_frame(),
            _synthetic_eastmoney_frame().assign(日期="2024-01-03"),
        ],
        ignore_index=True,
    )
    seen: dict[str, object] = {}

    def fake_hist(**kwargs: object) -> pd.DataFrame:
        seen.update(kwargs)
        return frame

    monkeypatch.setattr(opendata_http, "stock_zh_a_hist", fake_hist)
    result = AkshareStockDailyFetcher().fetch(
        symbol="600519",
        start_date="20240102",
        end_date="2024-01-03",
        source="akshare",
        market="cn",
    )

    assert [bar.trade_date for bar in result] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert seen["start_date"] == "20240102"
    assert seen["end_date"] == "20240103"


@pytest.mark.parametrize("outside_date", ["2024-01-01", "2024-01-04"])
def test_normalizer_rejects_rows_outside_the_requested_inclusive_window(
    outside_date: str,
) -> None:
    """Unexpected source rows fail the query instead of being silently filtered."""
    frame = _synthetic_eastmoney_frame()
    frame.loc[0, "日期"] = outside_date
    params = AkshareStockDailyFetcher().transform_query(
        symbol="600519", start_date="20240102", end_date="20240103"
    )

    with pytest.raises(ValueError, match="requested (start_date|end_date)"):
        AkshareStockDailyFetcher().transform_data(frame, params)


def test_query_rejects_reversed_date_window() -> None:
    """Reject a nonsensical date interval before source I/O."""
    with pytest.raises(ValidationError, match="on or before"):
        AkshareStockDailyFetcher().transform_query(
            symbol="600519", start_date="20240103", end_date="20240102"
        )


@pytest.mark.parametrize("field", ["开盘", "收盘", "最高", "最低", "成交量", "成交额"])
@pytest.mark.parametrize("bad_value", [None, float("nan"), float("inf"), "bad"])
def test_normalizer_rejects_missing_nonfinite_and_nonnumeric_values(
    field: str, bad_value: object
) -> None:
    """Required source values never disappear through dropna or float coercion."""
    frame = _synthetic_eastmoney_frame()
    frame[field] = frame[field].astype("object")
    frame.at[0, field] = bad_value
    params = AkshareStockDailyFetcher().transform_query(symbol="600519")

    with pytest.raises(ValueError, match="(missing/invalid|invalid|non-finite)"):
        AkshareStockDailyFetcher().transform_data(frame, params)


@pytest.mark.parametrize("source", ["tencent", "eastmoney", "yfinance", ""])
def test_query_rejects_unsupported_source_aliases(source: str) -> None:
    """This adapter never silently switches source or invokes an unregistered client."""
    with pytest.raises(ValidationError, match="source"):
        AkshareStockDailyFetcher().transform_query(symbol="600519", source=source)


def test_normalizer_requires_source_instrument_identity() -> None:
    """An absent response symbol cannot be replaced with the query symbol."""
    frame = _synthetic_eastmoney_frame().drop(columns=["股票代码"])
    params = AkshareStockDailyFetcher().transform_query(symbol="600519")

    with pytest.raises(ValueError, match="missing required columns"):
        AkshareStockDailyFetcher().transform_data(frame, params)


def test_normalizer_rejects_empty_frames_duplicate_dates_and_invalid_ohlc() -> None:
    """Empty or non-unique/incoherent source observations fail closed."""
    fetcher = AkshareStockDailyFetcher()
    params = fetcher.transform_query(symbol="600519")

    with pytest.raises(ValueError, match="no stock history rows"):
        fetcher.transform_data(pd.DataFrame(), params)

    duplicate = pd.concat(
        [_synthetic_eastmoney_frame(), _synthetic_eastmoney_frame()], ignore_index=True
    )
    with pytest.raises(ValueError, match="duplicate date"):
        fetcher.transform_data(duplicate, params)

    invalid_range = _synthetic_eastmoney_frame()
    invalid_range.loc[0, "最高"] = 1670.0
    with pytest.raises(ValueError, match="high is below"):
        fetcher.transform_data(invalid_range, params)
