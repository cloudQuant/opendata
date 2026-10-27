"""Fail-closed tests for the Eastmoney stock_daily migration adapter.

Every wire response and frame below is synthetic and is used only to pin
parsing and contract behavior. They are not source evidence. The bounded live
source check was unavailable, so these tests cannot verify upstream behavior.
"""

import json
from datetime import date
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest
from pydantic import ValidationError

from opendata.data.models import Bar
from opendata.data.protocol import FetchContext
from opendata.data.providers.akshare import stock_daily_wire
from opendata.data.providers.akshare.models.stock_daily import AkshareStockDailyFetcher


def _synthetic_eastmoney_wire(
    *,
    code: str = "600519",
    market: int = 1,
    klines: list[str] | None = None,
    rc: int = 0,
    extra: dict[str, object] | None = None,
) -> bytes:
    """Return explicit synthetic Eastmoney-shaped JSON, never a captured response."""
    payload: dict[str, object] = {
        "rc": rc,
        "data": {
            "code": code,
            "market": market,
            "klines": klines
            if klines is not None
            else ["2024-01-02,1685,1688,1690,1680,1.23,2076240,0,0,0,0"],
        },
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, separators=(",", ":")).encode()


def _fake_http_client(
    content: bytes,
    *,
    status_code: int = 200,
    seen: dict[str, Any] | None = None,
) -> object:
    """Return a synthetic governed-client seam that records request arguments."""
    captured = seen if seen is not None else {}

    class FakeHttpClient:
        def get(self, url: str, **kwargs: object) -> SimpleNamespace:
            captured["url"] = url
            captured["call_count"] = int(captured.get("call_count", 0)) + 1
            captured.update(kwargs)
            return SimpleNamespace(status_code=status_code, content=content)

    return FakeHttpClient()


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
    client = _fake_http_client(_synthetic_eastmoney_wire(), seen=seen)
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)
    result = AkshareStockDailyFetcher().fetch(symbol="600519", adjust=adjust)

    assert len(result) == 1
    assert seen["url"] == stock_daily_wire.EASTMONEY_KLINE_URL
    assert set(seen["params"]) == {
        "fields1",
        "fields2",
        "klt",
        "fqt",
        "secid",
        "beg",
        "end",
    }
    assert seen["params"]["fqt"] == "0"
    assert seen["params"]["klt"] == "101"
    assert seen["params"]["secid"] == "1.600519"
    assert seen["params"]["beg"] == "19700101"
    assert seen["params"]["end"] == "20500101"
    assert seen["source"] == "akshare"


def test_query_date_window_is_inclusive_and_passed_to_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both requested endpoint dates are retained and sent without drift."""
    seen: dict[str, object] = {}

    wire = _synthetic_eastmoney_wire(
        klines=[
            "2024-01-02,1685,1688,1690,1680,1.23,2076240,0,0,0,0",
            "2024-01-03,1688,1690,1695,1685,2.5,4225000,0,0,0,0",
        ]
    )
    client = _fake_http_client(wire, seen=seen)
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)
    result = AkshareStockDailyFetcher().fetch(
        symbol="600519",
        start_date="20240102",
        end_date="2024-01-03",
        source="akshare",
        market="cn",
    )

    assert [bar.trade_date for bar in result] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert seen["params"]["beg"] == "20240102"
    assert seen["params"]["end"] == "20240103"


def test_timeout_context_is_forwarded_to_governed_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preserve caller timeout through the direct wire request."""
    seen: dict[str, object] = {}
    client = _fake_http_client(_synthetic_eastmoney_wire(), seen=seen)
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    AkshareStockDailyFetcher().fetch(symbol="600519", ctx=FetchContext(timeout=12.5))

    assert seen["timeout"] == 12.5


def test_wire_http_error_fails_without_source_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed fixed endpoint does not invoke the old vendor wrapper or another source."""
    seen: dict[str, object] = {}
    client = _fake_http_client(b"", status_code=502, seen=seen)
    monkeypatch.setattr(stock_daily_wire, "get_shared_http_client", lambda: client)

    with pytest.raises(ValueError, match="HTTP 502"):
        AkshareStockDailyFetcher().fetch(symbol="600519")

    assert seen["url"] == stock_daily_wire.EASTMONEY_KLINE_URL
    assert seen["call_count"] == 1


@pytest.mark.parametrize(
    ("code", "market", "message"),
    [
        ("000001", 1, "code does not exactly match"),
        ("600519", 0, "market does not match"),
        ("600519", True, "market does not match"),
    ],
)
def test_wire_identity_must_match_the_requested_symbol_and_secid(
    code: str, market: int, message: str
) -> None:
    """Never infer source identity from the requested symbol or frame rows."""
    with pytest.raises(ValueError, match=message):
        stock_daily_wire.parse_stock_daily_wire(
            _synthetic_eastmoney_wire(code=code, market=market),
            symbol="600519",
            expected_market=1,
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"rc": 0, "data": {"market": 1, "klines": []}},
        {"rc": 0, "data": {"code": "600519", "klines": []}},
        {"rc": 0, "data": {"code": "600519", "market": 1}},
        {"rc": 1, "data": {"code": "600519", "market": 1, "klines": []}},
    ],
)
def test_wire_requires_success_envelope_and_all_identity_fields(payload: dict[str, object]) -> None:
    """Missing source identity, rows, and unsuccessful response codes fail closed."""
    with pytest.raises(ValueError):
        stock_daily_wire.parse_stock_daily_wire(
            json.dumps(payload).encode(), symbol="600519", expected_market=1
        )


def test_wire_rejects_invalid_row_shape_nonfinite_and_duplicate_dates() -> None:
    """Malformed source rows flow to explicit errors instead of disappearing."""
    with pytest.raises(ValueError, match="field count"):
        stock_daily_wire.parse_stock_daily_wire(
            _synthetic_eastmoney_wire(klines=["2024-01-02,1685"]),
            symbol="600519",
            expected_market=1,
        )

    nonfinite = _synthetic_eastmoney_wire(
        klines=["2024-01-02,NaN,1688,1690,1680,1.23,2076240,0,0,0,0"]
    )
    with pytest.raises(ValueError, match="non-finite"):
        AkshareStockDailyFetcher().transform_data(
            stock_daily_wire.parse_stock_daily_wire(nonfinite, symbol="600519", expected_market=1),
            AkshareStockDailyFetcher().transform_query(symbol="600519"),
        )

    duplicated = _synthetic_eastmoney_wire(
        klines=[
            "2024-01-02,1685,1688,1690,1680,1.23,2076240,0,0,0,0",
            "2024-01-02,1685,1688,1690,1680,1.23,2076240,0,0,0,0",
        ]
    )
    with pytest.raises(ValueError, match="duplicate date"):
        AkshareStockDailyFetcher().transform_data(
            stock_daily_wire.parse_stock_daily_wire(duplicated, symbol="600519", expected_market=1),
            AkshareStockDailyFetcher().transform_query(symbol="600519"),
        )


def test_wire_rejects_oversized_body_and_too_many_rows() -> None:
    """Apply explicit parser byte and row limits before frame normalization."""
    with pytest.raises(ValueError, match="post-buffer byte limit"):
        stock_daily_wire.parse_stock_daily_wire(
            b" " * (stock_daily_wire.MAX_RAW_RESPONSE_BYTES + 1),
            symbol="600519",
            expected_market=1,
        )

    row = "2024-01-02,1685,1688,1690,1680,1.23,2076240,0,0,0,0"
    with pytest.raises(ValueError, match="row limit"):
        stock_daily_wire.parse_stock_daily_wire(
            _synthetic_eastmoney_wire(klines=[row] * (stock_daily_wire.MAX_KLINE_ROWS + 1)),
            symbol="600519",
            expected_market=1,
        )


def test_wire_rejects_nonfinite_json_constants_and_duplicate_keys() -> None:
    """Python JSON extensions and ambiguous duplicate keys are not accepted."""
    with pytest.raises(ValueError, match="strict JSON"):
        stock_daily_wire.parse_stock_daily_wire(
            b'{"rc":NaN,"data":{}}', symbol="600519", expected_market=1
        )
    with pytest.raises(ValueError, match="strict JSON"):
        stock_daily_wire.parse_stock_daily_wire(
            b'{"rc":0,"rc":0,"data":{}}', symbol="600519", expected_market=1
        )


def test_wire_window_is_enforced_by_same_normalizer() -> None:
    """A wire observation outside the inclusive query window is rejected."""
    fetcher = AkshareStockDailyFetcher()
    params = fetcher.transform_query(symbol="600519", start_date="20240103", end_date="20240104")
    raw = stock_daily_wire.parse_stock_daily_wire(
        _synthetic_eastmoney_wire(), symbol="600519", expected_market=1
    )

    with pytest.raises(ValueError, match="precedes requested start_date"):
        fetcher.transform_data(raw, params)


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
