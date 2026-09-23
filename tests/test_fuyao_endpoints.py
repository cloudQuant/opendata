"""fuyao P0 端点测试（A3.2）。

mock 层覆盖请求构造（毫秒戳/闭区间/参数边界/失败关闭）、响应归一化
（契约模型、字段映射、排序、缺字段 fail-closed）与窗口切块；真机层（``e2e``）
在配置了 ``FUYAO_API_KEY`` 时对上游冒烟。
"""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from opendata.data.models import Bar, CorporateAction, Instrument, TradingCalendar
from opendata_fuyao import FuyaoCredentials, FuyaoError, FuyaoHttpClient
from opendata_fuyao.endpoints import (
    ADJUSTMENT_FACTORS_ENDPOINT,
    CALENDAR_ENDPOINT,
    MAX_LIST_LIMIT,
    MAX_SEARCH_LIMIT,
    PRICES_ENDPOINT,
    TICKERS_LIST_ENDPOINT,
    TICKERS_SEARCH_ENDPOINT,
    build_adjustment_factors_request,
    build_prices_request,
    build_tickers_list_request,
    build_tickers_search_request,
    fetch_adjustment_factors,
    fetch_daily_bars,
    fetch_trading_calendar,
    list_instruments,
    millis_to_trading_date,
    normalize_adjustment_factors,
    normalize_bars,
    normalize_calendar,
    normalize_instruments,
    search_instruments,
    shanghai_midnight_millis,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

SHANGHAI = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def _ms(day: str) -> int:
    """交易日 → 上海零点毫秒戳（测试直算，不复用被测实现）。"""
    parsed = date.fromisoformat(day)
    return int(datetime.combine(parsed, time.min, tzinfo=SHANGHAI).timestamp() * 1000)


def _envelope(items: Sequence[Mapping[str, Any]]) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {"timestamp": None, "item": list(items)},
        }
    ).encode()


def _parse(raw: bytes):
    from opendata_fuyao import parse_envelope

    return parse_envelope(json.loads(raw))


def _client(handler, **kwargs) -> FuyaoHttpClient:
    return FuyaoHttpClient(
        credentials=FuyaoCredentials("fuyao-test-key", base_url="https://fuyao.test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
        **kwargs,
    )


def _bar_row(day: str, *, close: float = 10.0) -> dict[str, Any]:
    return {
        "date_ms": _ms(day),
        "volume": 1000.0,
        "turnover": 10500.0,
        "open_price": 10.0,
        "high_price": close + 0.5,
        "low_price": 9.5,
        "close_price": close,
    }


class TestRequestBuilders:
    def test_prices_request_is_closed_interval_millis(self):
        params = build_prices_request(
            symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 5)
        )

        assert params["thscode"] == "600519.SH"
        assert params["interval"] == "1d"
        assert params["start"] == _ms("2024-01-02")
        assert params["end"] == _ms("2024-01-05") - 1  # 闭区间：半开 end 减 1ms
        assert params["adjust"] == "none"

    @pytest.mark.parametrize(
        ("adjust", "expected"),
        [("unadjusted", "none"), ("qfq", "forward"), ("hfq", "backward")],
    )
    def test_adjustment_mapping(self, adjust, expected):
        params = build_prices_request(
            symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 3), adjust=adjust
        )

        assert params["adjust"] == expected

    def test_unknown_adjustment_and_bad_window_fail_closed(self):
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_adjust"):
            build_prices_request(
                symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 3), adjust="split"
            )
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_window"):
            build_prices_request(symbol="600519.SH", start=date(2024, 1, 3), end=date(2024, 1, 3))
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_symbol"):
            build_prices_request(symbol="  ", start=date(2024, 1, 2), end=date(2024, 1, 3))

    def test_tickers_search_bounds(self):
        assert build_tickers_search_request(query="茅台", limit=5)["q"] == "茅台"

        with pytest.raises(FuyaoError, match="search_limit"):
            build_tickers_search_request(query="茅台", limit=MAX_SEARCH_LIMIT + 1)
        with pytest.raises(FuyaoError, match="query"):
            build_tickers_search_request(query="")

    def test_tickers_list_bounds(self):
        params = build_tickers_list_request(limit=100, offset=200, asset_type="a-share")

        assert params == {"limit": 100, "offset": 200, "asset_type": "a-share"}
        with pytest.raises(FuyaoError, match="list_limit"):
            build_tickers_list_request(limit=MAX_LIST_LIMIT + 1)
        with pytest.raises(FuyaoError, match="offset"):
            build_tickers_list_request(offset=-1)

    def test_adjustment_factors_window_uses_from_to_dates(self):
        params = build_adjustment_factors_request(
            symbol="600519.SH", start=date(2024, 1, 1), end=date(2024, 12, 31)
        )

        assert params == {"thscode": "600519.SH", "from": "2024-01-01", "to": "2024-12-31"}

    def test_adjustment_factors_rejects_half_open_window(self):
        with pytest.raises(FuyaoError, match="adjustment_window"):
            build_adjustment_factors_request(symbol="600519.SH", start=date(2024, 1, 1))
        with pytest.raises(FuyaoError, match="adjustment_window"):
            build_adjustment_factors_request(
                symbol="600519.SH", start=date(2024, 2, 1), end=date(2024, 1, 1)
            )

    def test_millis_round_trip_uses_shanghai_dates(self):
        assert millis_to_trading_date(_ms("2024-01-02")) == date(2024, 1, 2)
        assert shanghai_midnight_millis(date(2024, 1, 2)) == _ms("2024-01-02")

    def test_millis_rejects_non_integer(self):
        with pytest.raises(FuyaoError, match="date_ms"):
            millis_to_trading_date("1704124800000")


class TestNormalizers:
    def test_bars_map_to_the_contract(self):
        bars = normalize_bars(
            _parse(_envelope([_bar_row("2024-01-03"), _bar_row("2024-01-02")])),
            symbol="600519.SH",
        )

        assert all(isinstance(bar, Bar) for bar in bars)
        assert [bar.trade_date for bar in bars] == [date(2024, 1, 2), date(2024, 1, 3)]  # 升序
        assert all(bar.symbol == "600519.SH" for bar in bars)
        first = bars[0]
        assert first.open == 10.0
        assert first.close == 10.0
        assert first.amount == 10500.0  # turnover → amount
        assert first.volume == 1000.0

    def test_bars_reject_missing_fields(self):
        row = _bar_row("2024-01-02")
        row.pop("close_price")

        with pytest.raises(FuyaoError, match="bar_close_price"):
            normalize_bars(_parse(_envelope([row])), symbol="600519.SH")

    def test_bars_reject_non_mapping_items(self):
        with pytest.raises(FuyaoError, match="bar_item"):
            normalize_bars(
                _parse(_envelope(["not-a-row"])),  # type: ignore[list-item]
                symbol="600519.SH",
            )

    def test_adjustment_events_map_to_corporate_actions(self):
        events = normalize_adjustment_factors(
            symbol="600519.SH",
            envelope=_parse(
                _envelope(
                    [
                        {
                            "ticker": "600519",
                            "ex_date_ms": _ms("2024-06-19"),
                            "dividend_per_share": 30.876,
                            "per_share_bonus": 0,
                        },
                        {
                            "ticker": "600519",
                            "ex_date_ms": _ms("2023-06-19"),
                            "dividend_per_share": 25.911,
                            "per_share_bonus": 0,
                        },
                    ]
                )
            ),
        )

        assert all(isinstance(event, CorporateAction) for event in events)
        assert [event.ex_date for event in events] == [date(2024, 6, 19), date(2023, 6, 19)]  # 降序
        assert all(event.symbol == "600519.SH" for event in events)
        assert events[0].cash_dividend == 30.876
        assert events[0].stock_dividend == 0.0

    def test_adjustment_events_require_ex_date(self):
        with pytest.raises(FuyaoError, match="adjustment_ex_date_ms"):
            normalize_adjustment_factors(
                _parse(_envelope([{"ticker": "600519"}])), symbol="600519.SH"
            )

    def test_instruments_map_with_derived_status(self):
        instruments = normalize_instruments(
            _parse(
                _envelope(
                    [
                        {
                            "thscode": "600519.SH",
                            "ticker": "600519",
                            "name": "贵州茅台",
                            "exchange": "SSE",
                            "asset_type": "a-share",
                            "currency": "CNY",
                            "list_date": "2001-08-27",
                            "end_date": None,
                        },
                        {
                            "thscode": "000001.SZ",
                            "name": None,
                            "exchange": None,
                            "asset_type": "a-share",
                            "currency": None,
                            "list_date": None,
                            "end_date": "2020-01-01",
                        },
                    ]
                )
            )
        )

        assert all(isinstance(item, Instrument) for item in instruments)
        active, delisted = instruments
        assert active.symbol == "600519.SH"
        assert active.name == "贵州茅台"
        assert active.status == "active"
        assert active.list_date == date(2001, 8, 27)
        assert delisted.status == "delisted"
        assert delisted.delist_date == date(2020, 1, 1)
        assert delisted.currency == "CNY"  # 缺省回落

    def test_instruments_require_thscode(self):
        with pytest.raises(FuyaoError, match="ticker_thscode"):
            normalize_instruments(_parse(_envelope([{"name": "无名"}])))

    def test_calendar_maps_trading_days(self):
        days = normalize_calendar(
            _parse(
                _envelope(
                    [
                        {"date_ms": _ms("2024-01-03"), "date": "20240103"},
                        {"date_ms": _ms("2024-01-02"), "date": "20240102"},
                    ]
                )
            ),
            exchange="CN-SSE",
        )

        assert all(isinstance(day, TradingCalendar) for day in days)
        assert [day.date for day in days] == [date(2024, 1, 2), date(2024, 1, 3)]  # 升序
        assert all(day.is_open for day in days)
        assert days[0].exchange == "CN-SSE"

    def test_calendar_requires_an_exchange(self):
        with pytest.raises(FuyaoError, match="exchange"):
            normalize_calendar(_parse(_envelope([])), exchange="  ")


class TestFetchers:
    def test_daily_bars_hits_the_prices_endpoint(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            seen["key"] = request.headers.get("X-api-key")
            return httpx.Response(200, content=_envelope([_bar_row("2024-01-02")]))

        with _client(handler) as client:
            bars = fetch_daily_bars(
                client, symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 4)
            )

        assert seen["path"] == PRICES_ENDPOINT
        assert seen["params"]["thscode"] == "600519.SH"
        assert seen["params"]["adjust"] == "none"
        assert seen["key"] == "fuyao-test-key"
        assert len(bars) == 1

    def test_long_windows_are_chunked(self):
        windows: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            windows.append((params["start"], params["end"]))
            return httpx.Response(200, content=_envelope([]))

        with _client(handler) as client:
            fetch_daily_bars(
                client, symbol="600519.SH", start=date(2010, 1, 1), end=date(2026, 1, 1)
            )

        assert len(windows) == 2  # 16 年 → 两块（各 ≤ 10 年）

    def test_search_and_list_hit_their_endpoints(self):
        paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            return httpx.Response(
                200, content=_envelope([{"thscode": "600519.SH", "name": "贵州茅台"}])
            )

        with _client(handler) as client:
            found = search_instruments(client, query="茅台")
            listed = list_instruments(client, limit=5)

        assert paths == [TICKERS_SEARCH_ENDPOINT, TICKERS_LIST_ENDPOINT]
        assert found[0].symbol == "600519.SH"
        assert listed[0].symbol == "600519.SH"

    def test_adjustment_factors_endpoint(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        {
                            "ticker": "600519",
                            "ex_date_ms": _ms("2024-06-19"),
                            "dividend_per_share": 1.0,
                        }
                    ]
                ),
            )

        with _client(handler) as client:
            events = fetch_adjustment_factors(
                client, symbol="600519.SH", start=date(2024, 1, 1), end=date(2024, 12, 31)
            )

        assert seen["path"] == ADJUSTMENT_FACTORS_ENDPOINT
        assert seen["params"]["from"] == "2024-01-01"
        assert len(events) == 1

    def test_calendar_endpoint(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200, content=_envelope([{"date_ms": _ms("2024-01-02"), "date": "20240102"}])
            )

        with _client(handler) as client:
            days = fetch_trading_calendar(client, exchange="CN-SSE")

        assert seen["path"] == CALENDAR_ENDPOINT
        assert seen["params"] == {}
        assert days[0].date == date(2024, 1, 2)

    def test_upstream_business_error_propagates_with_category(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=json.dumps(
                    {"code": 1003, "message": "window too long", "request_id": "r", "data": None}
                ).encode(),
            )

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            fetch_daily_bars(
                client, symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 4)
            )

        assert exc.value.category == "request"
        assert exc.value.upstream_code == 1003


@pytest.mark.e2e
class TestAgainstTheLiveFuyaoApi:
    """真机冒烟：需要 ``FUYAO_API_KEY``（未配置即 skip）。"""

    @pytest.fixture
    def client(self):
        credentials = FuyaoCredentials.from_environment()
        if credentials is None:
            pytest.skip("FUYAO_API_KEY is not configured")
        with FuyaoHttpClient(credentials=credentials) as live:
            yield live

    def test_daily_bars_come_back_as_contract_rows(self, client):
        bars = fetch_daily_bars(
            client, symbol="600519.SH", start=date(2024, 1, 2), end=date(2024, 1, 6)
        )

        assert len(bars) == 4  # 半开窗口 [01-02, 01-06) → 01-02..01-05
        assert [bar.trade_date.isoformat() for bar in bars] == [
            "2024-01-02",
            "2024-01-03",
            "2024-01-04",
            "2024-01-05",
        ]
        assert all(bar.symbol == "600519.SH" for bar in bars)
        assert all(bar.close > 0 and bar.volume > 0 for bar in bars)

    def test_trading_calendar_comes_back_as_rows(self, client):
        days = fetch_trading_calendar(client, exchange="CN-SSE")

        assert len(days) > 200  # 上游返回整段 A 股日历
        assert all(day.is_open for day in days)

    def test_instrument_search_finds_an_a_share(self, client):
        found = search_instruments(client, query="贵州茅台", limit=5)

        assert found, "上游检索应有结果"
        assert any("600519" in instrument.symbol for instrument in found)

    def test_adjustment_factors_are_events(self, client):
        events = fetch_adjustment_factors(client, symbol="600519.SH")

        assert events, "上游应返回除权除息事件"
        assert all(event.ex_date <= date.today() for event in events)
