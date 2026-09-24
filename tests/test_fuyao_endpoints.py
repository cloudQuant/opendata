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

from opendata.data.models import (
    Bar,
    CorporateAction,
    FinancialStatement,
    IndexConstituent,
    Instrument,
    TradingCalendar,
)
from opendata_fuyao import FuyaoCredentials, FuyaoError, FuyaoHttpClient
from opendata_fuyao.endpoints import (
    ADJUSTMENT_FACTORS_ENDPOINT,
    BALANCE_SHEETS_ENDPOINT,
    CALENDAR_ENDPOINT,
    CASH_FLOW_STATEMENTS_ENDPOINT,
    FINANCIAL_STATEMENT_ITEMS,
    FUTURES_PRICES_ENDPOINT,
    INCOME_STATEMENTS_ENDPOINT,
    INDEX_CONSTITUENTS_ENDPOINT,
    INDEX_PRICES_ENDPOINT,
    MAX_LIST_LIMIT,
    MAX_SEARCH_LIMIT,
    OPTIONS_PRICES_ENDPOINT,
    PRICES_ENDPOINT,
    TICKERS_LIST_ENDPOINT,
    TICKERS_SEARCH_ENDPOINT,
    build_adjustment_factors_request,
    build_financial_statements_request,
    build_index_constituents_request,
    build_index_prices_request,
    build_period_daily_request,
    build_prices_request,
    build_tickers_list_request,
    build_tickers_search_request,
    fetch_adjustment_factors,
    fetch_daily_bars,
    fetch_financial_statements,
    fetch_index_constituents,
    fetch_index_daily_bars,
    fetch_period_daily_bars,
    fetch_trading_calendar,
    list_instruments,
    millis_to_trading_date,
    normalize_adjustment_factors,
    normalize_bars,
    normalize_calendar,
    normalize_financial_statements,
    normalize_index_constituents,
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


def _envelope_at(items: Sequence[Mapping[str, Any]], timestamp_ms: int | None) -> bytes:
    """带 ``data.timestamp`` 的信封（成分股等快照端点用它给观测时刻）。"""
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {"timestamp": timestamp_ms, "item": list(items)},
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


def _statement_row(statement: str = "income", **overrides: Any) -> dict[str, Any]:
    """一张报表的一个报告期：**宽行**（一行一个报告期、一列一个科目）。"""
    row: dict[str, Any] = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2024,
        "fiscal_period": "FY",
        "period_end_ms": _ms("2024-12-31"),
        "report_date_ms": _ms("2025-04-17"),
        "currency": "CNY",
        **dict.fromkeys(FINANCIAL_STATEMENT_ITEMS[statement], 1.0),
    }
    row["net_profit"] = 86_228_150_000.0
    row.update(overrides)
    return row


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

    def test_index_request_carries_no_adjust(self):
        params = build_index_prices_request(
            symbol="000300.SH", start=date(2024, 1, 2), end=date(2024, 1, 5)
        )

        assert params == {
            "thscode": "000300.SH",
            "interval": "1d",
            "start": _ms("2024-01-02"),
            "end": _ms("2024-01-05") - 1,
        }
        assert "adjust" not in params  # 指数无复权语义（上游 data.adjust 恒为 null）

    def test_index_request_symbol_and_window_fail_closed(self):
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_symbol"):
            build_index_prices_request(symbol=" ", start=date(2024, 1, 2), end=date(2024, 1, 3))
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_window"):
            build_index_prices_request(
                symbol="000300.SH", start=date(2024, 1, 3), end=date(2024, 1, 3)
            )

    def test_period_request_uses_time_period_and_no_adjust(self):
        params = build_period_daily_request(
            symbol="IF2610.CFE", start=date(2024, 1, 2), end=date(2024, 1, 5)
        )

        assert params == {
            "thscode": "IF2610.CFE",
            "time_period": "day_1",
            "start": _ms("2024-01-02"),
            "end": _ms("2024-01-05") - 1,
        }
        # 期货/期权价是观测值：既无复权，也不能带 interval（上游按字段拒绝）。
        assert "adjust" not in params
        assert "interval" not in params

    def test_period_request_symbol_and_window_fail_closed(self):
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_symbol"):
            build_period_daily_request(symbol="", start=date(2024, 1, 2), end=date(2024, 1, 3))
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_window"):
            build_period_daily_request(
                symbol="IF2610.CFE", start=date(2024, 1, 4), end=date(2024, 1, 3)
            )

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

    def test_constituents_request_is_one_qualified_index(self):
        # 上游把入参 trim().toUpperCase()，故本地只去空白、不改大小写。
        assert build_index_constituents_request(symbol=" 000300.sh ") == {"thscode": "000300.sh"}
        # 裸码在本地即拦：上游对裸码回 1002，而后缀对指数不唯一（000300.SH / 399300.SZ）。
        with pytest.raises(FuyaoError, match="constituents_symbol_qualified"):
            build_index_constituents_request(symbol="000300")
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_symbol"):
            build_index_constituents_request(symbol="  ")

    def test_financial_request_always_sends_period(self):
        """``period`` 文档写有默认值，实测省略即 1001，故本地永远显式发出."""
        assert build_financial_statements_request(symbol=" 600519.sh ") == {
            "thscode": "600519.sh",
            "period": "annual",
        }
        assert build_financial_statements_request(symbol="600519.SH", limit=3) == {
            "thscode": "600519.SH",
            "period": "annual",
            "limit": 3,
        }

    def test_financial_window_is_closed_interval_shanghai_millis(self):
        params = build_financial_statements_request(
            symbol="600519.SH",
            period="quarterly",
            start=date(2024, 1, 1),
            end=date(2025, 12, 31),
        )

        assert params == {
            "thscode": "600519.SH",
            "period": "quarterly",
            "start": _ms("2024-01-01"),
            "end": _ms("2025-12-31"),
        }

    def test_financial_request_modes_and_bounds_fail_closed(self):
        """上游对这两种非法组合回「标的或字段不受支持」，与真实原因无关，本地先拦."""
        cases = [
            ("financial_symbol_qualified", {"symbol": "600519"}),
            ("FUYAO_ENVELOPE_INVALID_symbol", {"symbol": "  "}),
            ("financial_period", {"symbol": "600519.SH", "period": "monthly"}),
            ("financial_window", {"symbol": "600519.SH", "start": date(2024, 1, 1)}),
            ("financial_window", {"symbol": "600519.SH", "end": date(2024, 1, 1)}),
            (
                "financial_window",
                {
                    "symbol": "600519.SH",
                    "start": date(2025, 1, 1),
                    "end": date(2024, 1, 1),
                },
            ),
            (
                "financial_window_span",
                {
                    "symbol": "600519.SH",
                    "start": date(2010, 1, 1),
                    "end": date(2025, 1, 1),
                },
            ),
            (
                "financial_mode",
                {
                    "symbol": "600519.SH",
                    "start": date(2024, 1, 1),
                    "end": date(2024, 6, 30),
                    "limit": 3,
                },
            ),
            ("financial_limit", {"symbol": "600519.SH", "limit": 0}),
            ("financial_limit", {"symbol": "600519.SH", "limit": 21}),
            ("financial_limit_type", {"symbol": "600519.SH", "limit": True}),
        ]
        for detail, kwargs in cases:
            with pytest.raises(FuyaoError, match=detail):
                build_financial_statements_request(**kwargs)


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

    def test_constituents_map_to_plain_codes_sorted_by_symbol(self):
        rows = normalize_index_constituents(
            _parse(
                _envelope_at(
                    [
                        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
                        {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行"},
                    ],
                    _ms("2024-01-02") + 4_000_000,  # 请求时刻：当日 01:06:40 +08:00
                )
            ),
            index_symbol="000300.SH",
        )

        assert all(isinstance(row, IndexConstituent) for row in rows)
        assert [row.symbol for row in rows] == ["000001", "600519"]  # 升序
        assert all(row.index_symbol == "000300.SH" for row in rows)  # 请求写法原样透传
        assert all(row.weight is None for row in rows)  # 该端点不发布权重
        assert {row.as_of for row in rows} == {date(2024, 1, 2)}  # 观测日 = 时间戳的上海日期

    def test_constituents_require_both_identifier_fields_to_agree(self):
        # 落库用裸码做合并键：thscode 去后缀不等于 ticker 时不猜，直接失败关闭。
        with pytest.raises(FuyaoError, match="constituent_symbol_mismatch"):
            normalize_index_constituents(
                _parse(
                    _envelope_at(
                        [{"thscode": "600519.SH", "ticker": "600520", "name": "对不上"}],
                        _ms("2024-01-02"),
                    )
                ),
                index_symbol="000300.SH",
            )
        with pytest.raises(FuyaoError, match="constituent_symbol_type"):
            normalize_index_constituents(
                _parse(_envelope_at([{"thscode": 600519, "ticker": "600519"}], _ms("2024-01-02"))),
                index_symbol="000300.SH",
            )
        with pytest.raises(FuyaoError, match="constituent_ticker"):
            normalize_index_constituents(
                _parse(_envelope_at([{"thscode": "600519.SH"}], _ms("2024-01-02"))),
                index_symbol="000300.SH",
            )

    def test_constituents_require_the_snapshot_timestamp(self):
        with pytest.raises(FuyaoError, match="constituents_timestamp"):
            normalize_index_constituents(
                _parse(_envelope_at([{"thscode": "600519.SH", "ticker": "600519"}], None)),
                index_symbol="000300.SH",
            )

    def test_constituents_require_an_index_symbol(self):
        with pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID_symbol"):
            normalize_index_constituents(
                _parse(
                    _envelope_at([{"thscode": "600519.SH", "ticker": "600519"}], _ms("2024-01-02"))
                ),
                index_symbol=" ",
            )

    def test_financial_statements_melt_a_wide_row_into_long_rows(self):
        """一行一个报告期的宽表 ⇒ 一科目一行；`item` 用上游英文科目名."""
        items = FINANCIAL_STATEMENT_ITEMS["income"]
        rows = normalize_financial_statements(
            _parse(_envelope([_statement_row("income")])), statement_type="income"
        )

        assert all(isinstance(row, FinancialStatement) for row in rows)
        assert len(rows) == len(items)  # 每个登记科目一行
        assert {row.item for row in rows} == set(items)
        assert {row.symbol for row in rows} == {"600519"}  # 落长表用 ticker（裸码）
        assert {row.statement_type for row in rows} == {"income"}
        assert {row.report_period for row in rows} == {date(2024, 12, 31)}
        assert {row.announce_date for row in rows} == {date(2025, 4, 17)}  # report_date_ms
        assert {row.revision for row in rows} == {1}  # 上游无修订序列，不猜
        assert [row.value for row in rows if row.item == "net_profit"] == [86_228_150_000.0]
        assert [row.item for row in rows] == sorted(items)  # 同报告期按科目升序

    def test_financial_statements_sort_by_period_then_item(self):
        rows = normalize_financial_statements(
            _parse(
                _envelope(
                    [
                        _statement_row("income", fiscal_year=2025, period_end_ms=_ms("2025-12-31")),
                        _statement_row("income"),
                    ]
                )
            ),
            statement_type="income",
        )

        assert [row.report_period for row in rows] == sorted(
            [r.report_period for r in rows]
        )  # 上游按新→旧回，本地转升序
        assert rows[0].report_period == date(2024, 12, 31)
        assert rows[-1].report_period == date(2025, 12, 31)

    def test_each_statement_type_reads_its_own_item_set(self):
        balance = normalize_financial_statements(
            _parse(_envelope([_statement_row("balance")])), statement_type="balance"
        )

        assert {row.item for row in balance} == set(FINANCIAL_STATEMENT_ITEMS["balance"])
        assert {row.statement_type for row in balance} == {"balance"}
        with pytest.raises(FuyaoError, match="financial_statement_type"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income")])), statement_type="notes"
            )

    def test_financial_null_value_is_skipped_but_a_missing_key_is_not(self):
        """``null`` 是上游真实缺值；键整列不见是契约漂移，必须失败关闭."""
        items = FINANCIAL_STATEMENT_ITEMS["income"]
        with_null = _statement_row("income", research_and_development_expenses=None)

        rows = normalize_financial_statements(
            _parse(_envelope([with_null])), statement_type="income"
        )

        assert len(rows) == len(items) - 1
        assert "research_and_development_expenses" not in {row.item for row in rows}
        with pytest.raises(FuyaoError, match="financial_income_sales_fee"):
            normalize_financial_statements(
                _parse(_envelope([{k: v for k, v in with_null.items() if k != "sales_fee"}])),
                statement_type="income",
            )

    def test_financial_non_numeric_value_fails_closed(self):
        with pytest.raises(FuyaoError, match="financial_income_net_profit_type"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income", net_profit="862亿")])),
                statement_type="income",
            )
        with pytest.raises(FuyaoError, match="financial_income_basic_eps_type"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income", basic_eps=True)])),
                statement_type="income",
            )

    def test_financial_report_period_reconciles_the_two_fiscal_paths(self):
        """财年+报告期与 ``period_end_ms`` 必须同指一天：按 UTC 读戳会整体早一天."""
        rows = normalize_financial_statements(
            _parse(_envelope([_statement_row("income", fiscal_period="Q4")])),
            statement_type="income",
        )

        assert {row.report_period for row in rows} == {date(2024, 12, 31)}  # Q4 与 FY 同为年末

        for detail, overrides in [
            ("financial_income_period_mismatch", {"fiscal_period": "Q3"}),
            ("financial_income_period_mismatch", {"fiscal_year": 2023}),
            ("financial_income_fiscal_period", {"fiscal_period": "Q5"}),
            ("financial_income_fiscal_type", {"fiscal_year": "2024"}),
            ("financial_income_fiscal_type", {"fiscal_period": 4}),
        ]:
            with pytest.raises(FuyaoError, match=detail):
                normalize_financial_statements(
                    _parse(_envelope([_statement_row("income", **overrides)])),
                    statement_type="income",
                )

    def test_financial_statements_reject_a_non_cny_currency(self):
        """契约没有币种字段：把美元数当人民币入库不会报错，只会算错."""
        with pytest.raises(FuyaoError, match="financial_income_currency"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income", currency="USD")])),
                statement_type="income",
            )

    def test_financial_statements_require_both_symbol_fields_to_agree(self):
        with pytest.raises(FuyaoError, match="financial_income_symbol_mismatch"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income", ticker="600520")])),
                statement_type="income",
            )
        with pytest.raises(FuyaoError, match="financial_balance_symbol_mismatch"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("balance", thscode="600520.SH")])),
                statement_type="balance",
            )
        with pytest.raises(FuyaoError, match="financial_income_symbol_type"):
            normalize_financial_statements(
                _parse(_envelope([_statement_row("income", ticker=None)])),
                statement_type="income",
            )


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

    def test_index_bars_hit_the_index_endpoint(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, content=_envelope([_bar_row("2024-01-02")]))

        with _client(handler) as client:
            bars = fetch_index_daily_bars(
                client, symbol="000300.SH", start=date(2024, 1, 2), end=date(2024, 1, 4)
            )

        assert seen["path"] == INDEX_PRICES_ENDPOINT
        assert "adjust" not in seen["params"]
        assert bars == (
            Bar(
                symbol="000300.SH",
                trade_date=date(2024, 1, 2),
                open=10.0,
                high=10.5,
                low=9.5,
                close=10.0,
                volume=1000.0,
                amount=10500.0,
            ),
        )

    def test_index_long_windows_are_chunked(self):
        windows: list[tuple[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            windows.append((params["start"], params["end"]))
            return httpx.Response(200, content=_envelope([]))

        with _client(handler) as client:
            fetch_index_daily_bars(
                client, symbol="000300.SH", start=date(2005, 1, 1), end=date(2026, 1, 1)
            )

        assert len(windows) == 3  # 21 年 → 三块（各 ≤ 10 年）

    def test_period_bars_read_the_timestamp_field(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            row = {
                "timestamp": _ms("2024-01-02"),
                "open_price": 3500.0,
                "high_price": 3560.0,
                "low_price": 3490.0,
                "close_price": 3520.0,
                "volume": 1200.0,
                "turnover": 4.2e7,
            }
            return httpx.Response(200, content=_envelope([row]))

        with _client(handler) as client:
            bars = fetch_period_daily_bars(
                client,
                endpoint=FUTURES_PRICES_ENDPOINT,
                symbol="IF2610.CFE",
                start=date(2024, 1, 2),
                end=date(2024, 1, 4),
            )

        assert seen["path"] == FUTURES_PRICES_ENDPOINT
        assert seen["params"]["time_period"] == "day_1"
        assert bars == (
            Bar(
                symbol="IF2610.CFE",
                trade_date=date(2024, 1, 2),
                open=3500.0,
                high=3560.0,
                low=3490.0,
                close=3520.0,
                volume=1200.0,
                amount=4.2e7,
            ),
        )

    def test_period_fetch_is_a_single_request_for_21_years(self):
        """期货/期权日 K 实测不受 10 年上限约束，故不切块（少一半请求）。"""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(dict(request.url.params)["start"]))
            return httpx.Response(200, content=_envelope([]))

        with _client(handler) as client:
            fetch_period_daily_bars(
                client,
                endpoint=OPTIONS_PRICES_ENDPOINT,
                symbol="90007464.SZ",
                start=date(2005, 1, 1),
                end=date(2026, 1, 1),
            )

        assert len(calls) == 1

    def test_period_bars_reject_a_null_turnover(self):
        """商品指数类标的 turnover 为 null：契约要求 amount 非空，故失败关闭。"""

        def handler(request: httpx.Request) -> httpx.Response:
            row = {
                "timestamp": _ms("2024-01-02"),
                "open_price": 120.0,
                "high_price": 121.0,
                "low_price": 119.0,
                "close_price": 120.5,
                "volume": 3.0e7,
                "turnover": None,
            }
            return httpx.Response(200, content=_envelope([row]))

        with _client(handler) as client, pytest.raises(FuyaoError, match="FUYAO_ENVELOPE_INVALID"):
            fetch_period_daily_bars(
                client,
                endpoint=FUTURES_PRICES_ENDPOINT,
                symbol="850002.TI",
                start=date(2024, 1, 2),
                end=date(2024, 1, 4),
            )

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

    def test_constituents_endpoint_serves_the_whole_list(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(request.url.params)
            return httpx.Response(
                200,
                content=_envelope_at(
                    [
                        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
                        {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行"},
                    ],
                    _ms("2024-01-02"),
                ),
            )

        with _client(handler) as client:
            rows = fetch_index_constituents(client, symbol="000300.SH")

        assert seen["path"] == INDEX_CONSTITUENTS_ENDPOINT
        assert seen["params"] == {"thscode": "000300.SH"}  # 无分页/窗口参数
        assert [row.symbol for row in rows] == ["000001", "600519"]

    def test_constituents_unregistered_index_fails_closed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=json.dumps(
                    {"code": 1002, "message": "bad thscode", "request_id": "r", "data": None}
                ).encode(),
            )

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            fetch_index_constituents(client, symbol="999999.SH")

        assert exc.value.category == "request"
        assert exc.value.upstream_code == 1002

    @pytest.mark.parametrize(
        ("statement_type", "endpoint"),
        [
            ("income", INCOME_STATEMENTS_ENDPOINT),
            ("balance", BALANCE_SHEETS_ENDPOINT),
            ("cashflow", CASH_FLOW_STATEMENTS_ENDPOINT),
        ],
    )
    def test_each_statement_hits_its_own_endpoint(self, statement_type: str, endpoint: str):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(200, content=_envelope([_statement_row(statement_type)]))

        with _client(handler) as client:
            rows = fetch_financial_statements(
                client, symbol="600519.SH", statement_type=statement_type
            )

        assert seen["path"] == endpoint
        assert "thscode=600519.SH" in seen["params"]
        assert "period=annual" in seen["params"]  # 文档写「默认 annual」，实测省略即 1001
        assert len(rows) == len(FINANCIAL_STATEMENT_ITEMS[statement_type])

    def test_statement_window_and_limit_reach_the_query(self):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.params))
            return httpx.Response(200, content=_envelope([_statement_row("balance")]))

        with _client(handler) as client:
            fetch_financial_statements(
                client,
                symbol="600036.SH",
                statement_type="balance",
                period="quarterly",
                start=date(2024, 1, 1),
                end=date(2024, 12, 31),
            )
            fetch_financial_statements(
                client, symbol="600036.SH", statement_type="balance", limit=3
            )

        assert "period=quarterly" in seen[0]
        assert f"start={_ms('2024-01-01')}" in seen[0]
        assert f"end={_ms('2024-12-31')}" in seen[0]
        assert "limit=3" in seen[1]
        assert "start=" not in seen[1] and "end=" not in seen[1]

    def test_unknown_statement_type_fails_before_any_request(self):
        """三张报表各有端点：给了未知类型不该静默打到其中某一张."""
        requested: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requested.append(request.url.path)
            return httpx.Response(200, content=_envelope([]))

        with _client(handler) as client, pytest.raises(FuyaoError) as exc:
            fetch_financial_statements(client, symbol="600519.SH", statement_type="notes")

        assert "financial_statement_type" in str(exc.value)
        assert requested == []

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
