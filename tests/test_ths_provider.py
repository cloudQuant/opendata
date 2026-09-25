"""fuyao (ths) provider tests (A3.4).

Registration and routing facts (capability, verified flag, authority order),
query validation, symbol resolution rules and the adapter's fail-closed paths;
the live class exercises the adapter through the registry against the real
API when ``FUYAO_API_KEY`` is configured.
"""

from __future__ import annotations

import json
from datetime import date
from typing import TYPE_CHECKING

import httpx
import pytest
from pydantic import ValidationError

from opendata.data.models import (
    Bar,
    CorporateAction,
    FinancialStatement,
    IndexConstituent,
    Instrument,
    TradingCalendar,
)
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    credentials,
    resolve_code,
    resolve_fund_code,
    resolve_futures_code,
    resolve_index_code,
    resolve_option_code,
)
from opendata.data.providers.ths.models.financial_statement import ThsFinancialStatementFetcher
from opendata.data.providers.ths.models.fund_action import ThsFundActionFetcher
from opendata.data.providers.ths.models.futures_daily import ThsFuturesDailyFetcher
from opendata.data.providers.ths.models.index_constituent import ThsIndexConstituentFetcher
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.providers.ths.models.instrument import ThsInstrumentFetcher
from opendata.data.providers.ths.models.option_daily import ThsOptionDailyFetcher
from opendata.data.providers.ths.models.stock_action import ThsStockActionFetcher
from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
from opendata.data.providers.ths.models.trading_calendar import ThsTradingCalendarFetcher
from opendata.data.providers.ths.registration import FETCHERS, register
from opendata.data.registry import ProviderRegistry, authority_baseline, get_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opendata_fuyao import FuyaoHttpClient

DAY_MS = 1704124800000  # 2024-01-02 00:00 +08:00

#: 快照端点的 ``data.timestamp`` 是**请求时刻**（实测带毫秒），不是上海零点。
SNAPSHOT_MS = DAY_MS + 4_000_000  # 2024-01-02 01:06:40 +08:00


def _envelope(items: list[dict]) -> bytes:
    return _envelope_at(items, timestamp=None)


def _envelope_at(items: list[dict], timestamp: int | None) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {"timestamp": timestamp, "item": items},
        }
    ).encode()


def _bar_row() -> dict:
    return {
        "date_ms": DAY_MS,
        "volume": 1000.0,
        "turnover": 10500.0,
        "open_price": 10.0,
        "high_price": 10.5,
        "low_price": 9.5,
        "close_price": 10.2,
    }


def _period_row() -> dict:
    """期货/期权日 K 行：日期字段是 ``timestamp``,与日线的 ``date_ms`` 不同."""
    row = {key: value for key, value in _bar_row().items() if key != "date_ms"}
    row["timestamp"] = DAY_MS
    return row


def _instrument_row(code: str, **overrides) -> dict:
    """目录行：``ticker`` 是 ``thscode`` 去后缀（除期权外必须一致，见 C14）."""
    row = {
        "thscode": code,
        "ticker": code.partition(".")[0],
        "name": f"标的{code}",
        "exchange": code.partition(".")[2],
        "asset_type": "a-share",
        "currency": "CNY",
        "list_date": "2001-08-27",
        "end_date": None,
    }
    row.update(overrides)
    return row


def _event_row() -> dict:
    return {
        "ticker": "600519",
        "ex_date_ms": DAY_MS,
        "dividend_per_share": 30.876,
        "per_share_bonus": 0.0,
    }


def _fund_dividend_row(ms: int, *, per_ten: float = 1.23) -> dict:
    """一笔 ETF 分红：字段集与 2026-09-25 实测的 510300.SH 响应一致."""
    return {
        "per_ten_cash_before_tax": per_ten,
        "per_ten_cash_after_tax": per_ten,
        "progress": "2",
        "publish_date_ms": ms - 604_800_000,
        "registration_date_ms": ms - 267_840_000,
        "ex_dividend_date_ms": ms,
        "payment_date_ms": ms + 691_200_000,
        "reinvestment_date_ms": None,
        "profit_base_date_ms": ms - 1_642_560_000,
        "in_dividend_date_ms": ms,
    }


def _dividend_envelope(items: list[dict], *, count: int | None = None) -> bytes:
    """分红信封：``data`` 里带上游自校验的 ``dividend_count`` / ``dividend_total``."""
    amounts = [
        row["per_ten_cash_before_tax"] for row in items if row.get("per_ten_cash_before_tax")
    ]
    total = round(sum(value / 10.0 for value in amounts), 10)
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {
                "timestamp": None,
                "dividend_count": len(items) if count is None else count,
                "dividend_total": total,
                "item": items,
            },
        }
    ).encode()


#: 财务报表的宽行：一行一个报告期，日期同样是上海零点毫秒戳。
PERIOD_END_MS = 1703952000000  # 2023-12-31
DISCLOSE_MS = 1713196800000  # 2024-04-16


def _statement_row(statement: str = "income", **overrides) -> dict:
    """一张报表的一个报告期（科目值全为 1.0，只验形状与身份）."""
    from opendata_fuyao.endpoints import FINANCIAL_STATEMENT_ITEMS

    row = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "period": "annual",
        "fiscal_year": 2023,
        "fiscal_period": "FY",
        "period_end_ms": PERIOD_END_MS,
        "report_date_ms": DISCLOSE_MS,
        "currency": "CNY",
        **dict.fromkeys(FINANCIAL_STATEMENT_ITEMS[statement], 1.0),
    }
    row.update(overrides)
    return row


def _factory(handler):  # httpx handler
    """Build the context-manager factory the fetchers call per fetch."""
    import contextlib

    @contextlib.contextmanager
    def fake_client(*, timeout_seconds: float | None = None) -> Iterator[object]:
        active = _mock_client(handler)
        try:
            yield active
        finally:
            active.close()

    return fake_client


def _mock_client(handler) -> FuyaoHttpClient:  # httpx handler
    from opendata_fuyao import FuyaoCredentials, FuyaoHttpClient

    return FuyaoHttpClient(
        credentials=FuyaoCredentials("ths-test-key", base_url="https://fuyao.test"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda seconds: None,
    )


class TestRegistration:
    def test_registers_the_verified_fuyao_capabilities(self):
        registry = ProviderRegistry()

        registered = register(registry)

        assert {(capability.domain, capability.source) for capability in registered} == {
            ("stock_daily", "ths"),
            ("stock_action", "ths"),
            ("index_daily", "ths"),
            ("index_constituent", "ths"),
            ("financial_statement", "ths"),
            ("futures_daily", "ths"),
            ("option_daily", "ths"),
            ("trading_calendar", "ths"),
            ("instrument", "ths"),
            ("fund_action", "ths"),
        }
        assert all(capability.verified for capability in registered)

    def test_registration_is_idempotent(self):
        registry = ProviderRegistry()
        register(registry)

        assert register(registry) == []
        assert len(registry.capabilities()) == len(FETCHERS)

    def test_fetchers_declare_the_registered_domains(self):
        capabilities = [fetcher.capability for fetcher in FETCHERS]

        assert {(cap.asset_class, cap.domain, cap.period, cap.market) for cap in capabilities} == {
            ("equity", "stock_daily", "1D", "cn"),
            ("equity", "stock_action", "1D", "cn"),
            ("index", "index_daily", "1D", "cn"),
            ("index", "index_constituent", "snapshot", "cn"),
            ("equity", "financial_statement", "Q", "cn"),
            ("futures", "futures_daily", "1D", "cn"),
            ("option", "option_daily", "1D", "cn"),
            ("metadata", "trading_calendar", "snapshot", "cn"),
            ("metadata", "instrument", "snapshot", "cn"),
            ("fund", "fund_action", "1D", "cn"),
        }

    def test_every_verified_domain_auto_routes_to_ths(self):
        """A verified ths capability must win ``source=auto`` for its domain."""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        registry = get_registry()

        for fetcher in FETCHERS:
            capability = fetcher.capability
            resolved = registry.resolve(
                capability.asset_class, capability.domain, source="auto"
            ).capability
            assert resolved.source == "ths"
            assert (resolved.asset_class, resolved.domain) == (
                capability.asset_class,
                capability.domain,
            )

    def test_ths_is_the_declared_domestic_authority(self):
        assert authority_baseline()["stock_daily"][0] == "ths"
        assert authority_baseline()["stock_action"][0] == "ths"
        assert authority_baseline()["index_daily"][0] == "ths"
        assert authority_baseline()["index_constituent"][0] == "ths"
        assert authority_baseline()["financial_statement"][0] == "ths"
        assert authority_baseline()["futures_daily"][0] == "ths"
        assert authority_baseline()["option_daily"][0] == "ths"

    def test_bundled_registration_includes_the_fuyao_source(self):
        from opendata.data.providers import register_providers

        register_providers()

        sources = {capability.source for capability in get_registry().capabilities()}
        assert "ths" in sources


class TestQueryValidation:
    def test_unknown_fields_are_rejected(self):
        with pytest.raises(ValidationError):
            ThsStockDailyFetcher().transform_query(symbol="600519", period="daily")

    def test_daily_query_requires_a_symbol(self):
        with pytest.raises(ValidationError):
            ThsStockDailyFetcher().transform_query()

    def test_adjust_defaults_to_empty(self):
        query = ThsStockDailyFetcher().transform_query(symbol="600519")

        assert query.adjust == ""


class TestSymbolResolution:
    def test_qualified_codes_pass_through_without_a_lookup(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a qualified code must not trigger a lookup")

        with _mock_client(handler) as active:
            assert resolve_code(active, "600519.SH") == "600519.SH"
            assert resolve_code(active, " 000001.sz ") == "000001.SZ"

    def test_plain_codes_are_resolved_from_the_search(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        {"thscode": "883970.TI", "ticker": "883970", "name": "指数"},
                        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
                    ]
                ),
            )

        with _mock_client(handler) as active:
            assert resolve_code(active, "600519") == "600519.SH"

    def test_index_only_matches_are_refused(self):
        """同为 600519 前缀的指数代码不参与解析（后缀不是股票交易所）."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_envelope([{"thscode": "600519.TI", "ticker": "600519"}])
            )

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_UNRESOLVED"),
        ):
            resolve_code(active, "600519")

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            [{"thscode": "600520.SH", "ticker": "600520"}],
            [
                {"thscode": "600519.SH", "ticker": "600519"},
                {"thscode": "600519.SZ", "ticker": "600519"},
            ],
        ],
    )
    def test_unresolved_or_ambiguous_codes_fail_closed(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope(payload))

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_UNRESOLVED"),
        ):
            resolve_code(active, "600519")

    def test_blank_symbol_is_refused(self):
        with (
            _mock_client(lambda request: httpx.Response(200, content=_envelope([]))) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_INVALID"),
        ):
            resolve_code(active, "   ")

    def test_qualified_index_codes_pass_through(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a qualified code must not trigger a lookup")

        with _mock_client(handler) as active:
            assert resolve_index_code(active, "000300.SH") == "000300.SH"
            assert resolve_index_code(active, " 886042.ti ") == "886042.TI"

    def test_plain_index_code_resolves_from_the_index_universe(self):
        """指数走列表解析：search 端点是按名称检索的，数字代码查不到.

        行按上游真实形状给（带 ``ticker``/``asset_type``）——旧夹具省掉了
        ``ticker``，于是 C18 那条「整表因对账失败而拒」的缺陷在这条用例里是绿的。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["asset_type"] == "a-share-index"
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        {
                            "thscode": "886042.TI",
                            "ticker": "886042",
                            "asset_type": "a-share-index",
                            "name": "存储芯片",
                        },
                        {
                            "thscode": "000300.SH",
                            "ticker": "000300",
                            "asset_type": "a-share-index",
                            "name": "沪深300",
                        },
                    ]
                ),
            )

        with _mock_client(handler) as active:
            assert resolve_index_code(active, "000300") == "000300.SH"

    def test_index_display_codes_do_not_break_the_bare_code_lookup(self):
        """C18: 同一页里的展示码行（``000001.SH`` ↔ ``1A0001``）不能吃掉解析.

        真实指数目录 1,431 行里有 206 行是这种写法，旧实现对账一不等就整表抛，
        于是主系列（上证综指自己也在其中）连同 ``000300`` 一起查不出来。
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        {
                            "thscode": "000001.SH",
                            "ticker": "1A0001",
                            "asset_type": "a-share-index",
                            "name": "上证指数",
                        },
                        {
                            "thscode": "970006.SZ",
                            "ticker": "988006",
                            "asset_type": "a-share-index",
                            "name": "创业板指(港币)(CNH)",
                        },
                        {
                            "thscode": "000300.SH",
                            "ticker": "000300",
                            "asset_type": "a-share-index",
                            "name": "沪深300",
                        },
                    ]
                ),
            )

        with _mock_client(handler) as active:
            assert resolve_index_code(active, "000300") == "000300.SH"
            assert resolve_index_code(active, "000001") == "000001.SH"

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            [{"thscode": "000301.SH", "ticker": "000301", "asset_type": "a-share-index"}],
            [
                {"thscode": "000300.SH", "ticker": "000300", "asset_type": "a-share-index"},
                {"thscode": "000300.CSI", "ticker": "000300", "asset_type": "a-share-index"},
            ],
        ],
    )
    def test_unresolved_or_ambiguous_index_codes_fail_closed(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope(payload))

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_INDEX_SYMBOL_UNRESOLVED"),
        ):
            resolve_index_code(active, "000300")

    def test_blank_index_symbol_is_refused(self):
        with (
            _mock_client(lambda request: httpx.Response(200, content=_envelope([]))) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_INVALID"),
        ):
            resolve_index_code(active, "  ")

    def test_qualified_derivative_codes_pass_through(self):
        """C6: 带后缀的合约代码直接用,不查目录."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a qualified code must not trigger a lookup")

        with _mock_client(handler) as active:
            assert resolve_futures_code(active, "rb2610.shf") == "RB2610.SHF"
            assert resolve_option_code(active, "MO2612-c-7600.CFE") == "MO2612-C-7600.CFE"
            assert resolve_futures_code(active, "850002.TI") == "850002.TI"

    def test_plain_futures_code_resolves_from_the_futures_catalog(self):
        """C6: 期货裸码按 ``asset_type=futures`` 列表解析,和股票检索不是一条路."""
        asset_types: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            asset_types.append(str(request.url.params["asset_type"]))
            return httpx.Response(
                200, content=_envelope([{"thscode": "CU2610.SHF", "name": "沪铜2610"}])
            )

        with _mock_client(handler) as active:
            assert resolve_futures_code(active, "cu2610") == "CU2610.SHF"

        assert asset_types == ["futures"]

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            [{"thscode": "CU2611.SHF", "name": "沪铜2611"}],
            [
                {"thscode": "CU2610.SHF", "name": "沪铜2610"},
                {"thscode": "CU2610.INE", "name": "国际铜2610"},
            ],
        ],
    )
    def test_unresolved_or_ambiguous_futures_codes_fail_closed(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope(payload))

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_FUTURES_SYMBOL_UNRESOLVED"),
        ):
            resolve_futures_code(active, "CU2610")

    def test_blank_futures_symbol_is_refused(self):
        with (
            _mock_client(lambda request: httpx.Response(200, content=_envelope([]))) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_INVALID"),
        ):
            resolve_futures_code(active, " ")

    def test_bare_option_contract_is_refused_without_a_lookup(self):
        """C6: 期权目录超过单次列举上限,裸码解析不做,必须带交易所后缀."""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("option codes are never looked up")

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_OPTION_SYMBOL_QUALIFIED_REQUIRED"),
        ):
            resolve_option_code(active, "10011514")

    def test_qualified_fund_codes_pass_through(self):
        """C15：带后缀的基金代码直接用；裸码才查 ETF 目录。"""

        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("a qualified code must not trigger a lookup")

        with _mock_client(handler) as active:
            assert resolve_fund_code(active, " 510300.sh ") == "510300.SH"

    def test_plain_fund_code_resolves_from_the_etf_listing(self):
        """C15：场内基金走 ``asset_type=fund-etf`` 目录（实测 1,696 行、裸码在沪深间唯一）。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.params["asset_type"]))
            return httpx.Response(
                200, content=_envelope([{"thscode": "510300.SH", "name": "沪深300ETF"}])
            )

        with _mock_client(handler) as active:
            assert resolve_fund_code(active, "510300") == "510300.SH"

        assert seen == ["fund-etf"]

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            [{"thscode": "510330.SH", "name": "沪深300ETF"}],
            [
                {"thscode": "510300.SH", "name": "沪深300ETF"},
                {"thscode": "510300.SZ", "name": "别的300"},
            ],
        ],
    )
    def test_unresolved_or_ambiguous_fund_codes_fail_closed(self, payload):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope(payload))

        with (
            _mock_client(handler) as active,
            pytest.raises(ThsProviderError, match="THS_FUND_SYMBOL_UNRESOLVED"),
        ):
            resolve_fund_code(active, "510300")

    def test_blank_fund_symbol_is_refused(self):
        with (
            _mock_client(lambda request: httpx.Response(200, content=_envelope([]))) as active,
            pytest.raises(ThsProviderError, match="THS_SYMBOL_INVALID"),
        ):
            resolve_fund_code(active, " ")


class TestCredentials:
    def test_missing_key_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FUYAO_API_KEY", raising=False)
        monkeypatch.setattr("opendata.core.config.settings.fuyao_api_key", None, raising=False)

        with pytest.raises(ThsProviderError, match="THS_NOT_CONFIGURED"):
            credentials()

    def test_environment_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("FUYAO_API_KEY", "env-key")

        assert credentials().api_key == "env-key"

    def test_settings_fill_in_from_dotenv(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("FUYAO_API_KEY", raising=False)
        monkeypatch.setattr(
            "opendata.core.config.settings.fuyao_api_key", "settings-key", raising=False
        )

        assert credentials().api_key == "settings-key"

    @pytest.mark.parametrize("timeout", [None, 3.0])
    def test_client_builds_with_the_resolved_key_and_always_closes(
        self, monkeypatch: pytest.MonkeyPatch, timeout: float | None
    ):
        """每次 fetch 一个短生命周期客户端，退出时必须关闭."""
        from opendata.data.providers.ths.models import _client as client_module
        from opendata_fuyao import FuyaoCredentials

        expected = FuyaoCredentials("ths-test-key", base_url="https://fuyao.test")
        built: dict[str, object] = {}

        class SpyClient:
            def __init__(self, *, credentials, timeout_seconds=None):
                built["credentials"] = credentials
                built["timeout"] = timeout_seconds
                built["closed"] = False

            def close(self) -> None:
                built["closed"] = True

        monkeypatch.setattr(client_module, "credentials", lambda: expected)
        monkeypatch.setattr(client_module, "FuyaoHttpClient", SpyClient)

        with client(timeout_seconds=timeout):
            assert built["credentials"] is expected
            assert built["timeout"] == timeout
            assert built["closed"] is False

        assert built["closed"] is True


class TestFetchStages:
    def test_daily_fetch_returns_contract_rows(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope([_bar_row()]))

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.stock_daily.client", _factory(handler)
        )
        fetcher = ThsStockDailyFetcher()
        rows = fetcher.fetch(
            symbol="600519.SH", start_date=date(2024, 1, 2), end_date=date(2024, 1, 5)
        )

        assert isinstance(rows, tuple)
        assert all(isinstance(row, Bar) for row in rows)
        assert rows[0].symbol == "600519.SH"
        assert rows[0].trade_date == date(2024, 1, 2)
        assert rows[0].amount == 10500.0

    def test_daily_fetch_rejects_adjusted_series(self, monkeypatch: pytest.MonkeyPatch):
        fetcher = ThsStockDailyFetcher()
        query = fetcher.transform_query(symbol="600519.SH", adjust="qfq")
        rows = (
            Bar(
                symbol="600519.SH",
                trade_date=date(2024, 1, 2),
                open=1.0,
                high=1.0,
                low=1.0,
                close=1.0,
                volume=1.0,
                amount=1.0,
            ),
        )

        with pytest.raises(ThsProviderError, match="THS_ADJUST_UNSUPPORTED"):
            fetcher.transform_data(rows, query)

    def test_daily_empty_response_fails_closed(self):
        fetcher = ThsStockDailyFetcher()
        query = fetcher.transform_query(symbol="600519.SH")

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.transform_data((), query)

    def test_action_fetch_returns_events_newest_first(self, monkeypatch: pytest.MonkeyPatch):
        rows = [_event_row(), {**_event_row(), "ex_date_ms": DAY_MS + 86_400_000}]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope(rows))

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.stock_action.client", _factory(handler)
        )
        fetcher = ThsStockActionFetcher()
        events = fetcher.fetch(symbol="600519.SH")

        assert all(isinstance(event, CorporateAction) for event in events)
        assert [event.ex_date for event in events] == [date(2024, 1, 3), date(2024, 1, 2)]

    def test_index_fetch_returns_contract_rows(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(
                200,
                content=_envelope([_bar_row(), {**_bar_row(), "date_ms": DAY_MS + 86_400_000}]),
            )

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.index_daily.client", _factory(handler)
        )
        rows = ThsIndexDailyFetcher().fetch(
            symbol="000300.SH", start_date=date(2024, 1, 2), end_date=date(2024, 1, 3)
        )

        assert seen["path"] == "/api/a-share-index/prices/historical"
        assert "adjust" not in seen["params"]
        assert all(isinstance(row, Bar) for row in rows)
        assert [row.trade_date for row in rows] == [date(2024, 1, 2), date(2024, 1, 3)]
        assert rows[0].symbol == "000300.SH"

    def test_index_query_rejects_adjust(self):
        """指数没有复权口径，请求里出现 adjust 应当直接拒掉."""

        with pytest.raises(ValidationError):
            ThsIndexDailyFetcher().transform_query(symbol="000300.SH", adjust="qfq")

    def test_index_empty_response_fails_closed(self):
        fetcher = ThsIndexDailyFetcher()
        query = fetcher.transform_query(symbol="000300.SH")

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.transform_data((), query)

    def test_constituent_fetch_stores_plain_codes_for_both_sides(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """C9: 成分股是合并键，两侧都存裸码（bar 域存限定写法，这里是另一条口径）."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(
                200,
                content=_envelope_at(
                    [
                        {"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"},
                        {"thscode": "000001.SZ", "ticker": "000001", "name": "平安银行"},
                    ],
                    SNAPSHOT_MS,
                ),
            )

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.index_constituent.client", _factory(handler)
        )
        rows = ThsIndexConstituentFetcher().fetch(symbol="000300.SH")

        assert seen["path"] == "/api/a-share-index/constituents/ths-stock-list"
        assert "thscode=000300.SH" in seen["params"]  # 上游只认带后缀的写法
        assert all(isinstance(row, IndexConstituent) for row in rows)
        assert [row.symbol for row in rows] == ["000001", "600519"]
        assert {row.index_symbol for row in rows} == {"000300"}
        assert {row.as_of for row in rows} == {date(2024, 1, 2)}
        assert all(row.weight is None for row in rows)  # 上游不发布权重，不补算

    def test_constituent_fetch_resolves_a_bare_index_code(self, monkeypatch: pytest.MonkeyPatch):
        """裸指数码先按指数全集解析，再把解析出的 thscode 写进请求."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            seen.append(path)
            if path.endswith("/list"):
                return httpx.Response(
                    200, content=_envelope([{"thscode": "000300.SH", "name": "沪深300"}])
                )
            return httpx.Response(
                200,
                content=_envelope_at(
                    [{"thscode": "600519.SH", "ticker": "600519", "name": "贵州茅台"}], SNAPSHOT_MS
                ),
            )

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.index_constituent.client", _factory(handler)
        )
        rows = ThsIndexConstituentFetcher().fetch(symbol="000300")

        assert rows[0].index_symbol == "000300"
        assert "/api/meta/tickers/list" in seen

    def test_constituent_query_rejects_a_date_range(self):
        """端点只有当前清单：给了窗口就不能假装给的是历史快照."""
        fetcher = ThsIndexConstituentFetcher()

        with pytest.raises(ThsProviderError, match="THS_CONSTITUENTS_SNAPSHOT_ONLY"):
            fetcher.transform_query(symbol="000300.SH", start_date=date(2024, 1, 2))
        with pytest.raises(ThsProviderError, match="THS_CONSTITUENTS_SNAPSHOT_ONLY"):
            fetcher.transform_query(symbol="000300.SH", end_date=date(2024, 1, 2))

    def test_constituent_empty_response_fails_closed(self):
        fetcher = ThsIndexConstituentFetcher()
        query = fetcher.transform_query(symbol="000300.SH")

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.transform_data((), query)

    @pytest.mark.parametrize(
        ("module", "fetcher", "endpoint"),
        [
            ("futures_daily", ThsFuturesDailyFetcher(), "/api/futures/prices/daily"),
            ("option_daily", ThsOptionDailyFetcher(), "/api/options/prices/daily"),
        ],
    )
    def test_derivative_fetch_reads_the_timestamp_field(
        self, monkeypatch: pytest.MonkeyPatch, module: str, fetcher, endpoint: str
    ):
        """C6: 期货/期权腿走 ``time_period=day_1``,行内日期是 ``timestamp``."""
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(
                200,
                content=_envelope(
                    [{**_period_row(), "timestamp": DAY_MS + 86_400_000}, _period_row()]
                ),
            )

        monkeypatch.setattr(
            f"opendata.data.providers.ths.models.{module}.client", _factory(handler)
        )
        rows = fetcher.fetch(
            symbol="CU2610.SHF" if module == "futures_daily" else "10011514.SH",
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 3),
        )

        assert seen["path"] == endpoint
        assert "time_period=day_1" in seen["params"]
        assert "interval" not in seen["params"] and "adjust" not in seen["params"]
        assert [row.trade_date for row in rows] == [date(2024, 1, 2), date(2024, 1, 3)]
        assert all(isinstance(row, Bar) for row in rows)
        assert {row.symbol for row in rows} == {rows[0].symbol}

    @pytest.mark.parametrize("fetcher", [ThsFuturesDailyFetcher(), ThsOptionDailyFetcher()])
    def test_derivative_query_rejects_adjust(self, fetcher):
        with pytest.raises(ValidationError):
            fetcher.transform_query(symbol="CU2610.SHF", adjust="qfq")

    @pytest.mark.parametrize("fetcher", [ThsFuturesDailyFetcher(), ThsOptionDailyFetcher()])
    def test_derivative_empty_response_fails_closed(self, fetcher):
        query = fetcher.transform_query(symbol="CU2610.SHF")

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.transform_data((), query)

    def test_context_timeout_is_accepted(self):
        fetcher = ThsStockDailyFetcher()
        query = fetcher.transform_query(symbol="600519.SH")

        assert query.symbol == "600519.SH"
        assert FetchContext(timeout=5.0).timeout == 5.0


class TestFinancialStatementAdapter:
    """C10: 财务报表腿的查询词表、宽行摊长与行身份校验."""

    def test_chinese_disclosure_name_normalizes_to_the_contract_value(self):
        """新浪那条腿用中文类型：这里接受中文入参，但入库只写契约取值."""
        fetcher = ThsFinancialStatementFetcher()

        assert fetcher.transform_query(symbol="600519.SH").statement_type == "income"
        for alias, value in (
            ("利润表", "income"),
            ("资产负债表", "balance"),
            ("现金流量表", "cashflow"),
        ):
            query = fetcher.transform_query(symbol="600519.SH", statement_type=alias)
            assert query.statement_type == value

    def test_unknown_vocabulary_fails_closed(self):
        fetcher = ThsFinancialStatementFetcher()

        with pytest.raises(ThsProviderError, match="THS_STATEMENT_TYPE_UNSUPPORTED"):
            fetcher.transform_query(symbol="600519.SH", statement_type="所有者权益变动表")
        with pytest.raises(ThsProviderError, match="THS_FINANCIAL_PERIOD_UNSUPPORTED"):
            fetcher.transform_query(symbol="600519.SH", period="monthly")
        with pytest.raises(ThsProviderError, match="THS_FINANCIAL_WINDOW_INCOMPLETE"):
            fetcher.transform_query(symbol="600519.SH", start_date=date(2024, 1, 1))
        with pytest.raises(ValidationError):  # 财务数没有复权口径
            fetcher.transform_query(symbol="600519.SH", adjust="qfq")

    def test_fetch_melts_the_wide_response_and_orders_it_ascending(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """上游一行一个报告期（新→旧），契约要一科目一行、按报告期升序."""
        from opendata_fuyao.endpoints import FINANCIAL_STATEMENT_ITEMS

        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        _statement_row("income"),
                        _statement_row("income", fiscal_year=2022, period_end_ms=1672416000000),
                    ]
                ),
            )

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.financial_statement.client", _factory(handler)
        )
        rows = ThsFinancialStatementFetcher().fetch(symbol="600519.SH", statement_type="income")

        assert seen["path"] == "/api/a-share/financials/income-statements"
        assert "thscode=600519.SH" in seen["params"]
        assert "period=annual" in seen["params"]
        assert all(isinstance(row, FinancialStatement) for row in rows)
        assert len(rows) == 2 * len(FINANCIAL_STATEMENT_ITEMS["income"])
        assert [row.report_period for row in rows] == sorted(r.report_period for r in rows)
        assert rows[0].report_period == date(2022, 12, 31)
        assert {row.symbol for row in rows} == {"600519"}  # 长表用裸码（合并键）
        assert {row.revision for row in rows} == {1}

    def test_fetch_resolves_a_bare_code_before_asking_for_a_statement(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """裸码先经 search 解析成 thscode：财务报表端点对裸码回 1002."""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/search"):
                return httpx.Response(
                    200,
                    content=_envelope([{"thscode": "600519.SH", "ticker": "600519", "name": "x"}]),
                )
            return httpx.Response(200, content=_envelope([_statement_row("balance")]))

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.financial_statement.client", _factory(handler)
        )
        rows = ThsFinancialStatementFetcher().fetch(symbol="600519", statement_type="资产负债表")

        assert seen == ["/api/meta/tickers/search", "/api/a-share/financials/balance-sheets"]
        assert {row.statement_type for row in rows} == {"balance"}

    def test_fetch_passes_the_report_period_window(self, monkeypatch: pytest.MonkeyPatch):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url.params))
            return httpx.Response(
                200,
                content=_envelope(
                    [_statement_row("cashflow", thscode="300750.SZ", ticker="300750")]
                ),
            )

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.financial_statement.client", _factory(handler)
        )
        ThsFinancialStatementFetcher().fetch(
            symbol="300750.SZ",
            statement_type="现金流量表",
            period="quarterly",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 12, 31),
        )

        assert "period=quarterly" in seen[0]
        assert "start=1704038400000" in seen[0]  # 2024-01-01 上海零点
        assert "end=1735574400000" in seen[0]  # 2024-12-31 上海零点

    def test_financial_empty_response_fails_closed(self):
        fetcher = ThsFinancialStatementFetcher()
        query = fetcher.transform_query(symbol="600519.SH")

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.transform_data((), query)

    def test_financial_rows_from_another_issuer_fail_closed(self):
        """上游按 thscode 取数，回显别的发行人只能是接错标的."""
        fetcher = ThsFinancialStatementFetcher()
        query = fetcher.transform_query(symbol="600519.SH")
        row = FinancialStatement(
            symbol="600520",
            statement_type="income",
            report_period=date(2023, 12, 31),
            announce_date=date(2024, 4, 16),
            item="net_profit",
            value=1.0,
        )

        with pytest.raises(ThsProviderError, match="THS_FINANCIAL_SYMBOL_MISMATCH"):
            fetcher.transform_data((row,), query)


class TestTradingCalendarAdapter:
    """A4.7 producer 侧：日历行的邻接交易日由**相邻行**推出，coverage 之外不外推。"""

    @staticmethod
    def _day(offset_days: int) -> dict:
        """一个交易日行；上游只给 ``date_ms``，且只给交易日。"""
        return {"date_ms": DAY_MS + offset_days * 86_400_000}

    def _fetcher(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows: list[dict],
        seen: list[str] | None = None,
    ) -> ThsTradingCalendarFetcher:
        def handler(request: httpx.Request) -> httpx.Response:
            if seen is not None:
                seen.append(str(request.url))
            return httpx.Response(200, content=_envelope(rows))

        monkeypatch.setattr(
            "opendata.data.providers.ths.models.trading_calendar.client", _factory(handler)
        )
        return ThsTradingCalendarFetcher()

    def test_adjacent_trade_dates_come_from_the_neighbouring_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """01-02 / 01-03 / 01-05 三行 ⇒ 01-03 的下一交易日是 01-05，中间那个空档不解释。"""
        days = self._fetcher(monkeypatch, [self._day(0), self._day(1), self._day(3)]).fetch()

        assert [day.date for day in days] == [
            date(2024, 1, 2),
            date(2024, 1, 3),
            date(2024, 1, 5),
        ]
        assert all(isinstance(day, TradingCalendar) and day.is_open for day in days)
        assert days[0].prev_trade_date is None
        assert days[0].next_trade_date == date(2024, 1, 3)
        assert days[1].prev_trade_date == date(2024, 1, 2)
        assert days[1].next_trade_date == date(2024, 1, 5)
        # coverage 右端之外没有事实来源，宁可留 None 也不按工作日猜一个。
        assert days[-1].next_trade_date is None

    def test_no_window_parameter_is_sent(self, monkeypatch: pytest.MonkeyPatch):
        """实测六个候选窗口参数全被忽略 ⇒ 不发参数，不假装能定窗。"""
        seen: list[str] = []
        self._fetcher(monkeypatch, [self._day(0)], seen).fetch(
            start_date=date(2024, 1, 2), end_date=date(2024, 1, 3)
        )

        assert "?" not in seen[0]

    def test_query_defaults_to_the_published_exchange(self, monkeypatch: pytest.MonkeyPatch):
        fetcher = self._fetcher(monkeypatch, [self._day(0)])

        assert fetcher.transform_query().exchange == "CN-SSE"

    def test_another_exchange_label_is_refused(self, monkeypatch: pytest.MonkeyPatch):
        """上游 A 股日历不按交易所拆分，答别的标签只能是接错了源。"""
        fetcher = self._fetcher(monkeypatch, [self._day(0)])

        with pytest.raises(ThsProviderError, match="THS_CALENDAR_EXCHANGE_UNSUPPORTED"):
            fetcher.transform_query(exchange="CN-SZSE")

    def test_requested_window_slices_the_published_coverage(self, monkeypatch: pytest.MonkeyPatch):
        fetcher = self._fetcher(monkeypatch, [self._day(0), self._day(1), self._day(3)])

        rows = fetcher.fetch(start_date=date(2024, 1, 3), end_date=date(2024, 1, 5))

        assert [row.date for row in rows] == [date(2024, 1, 3), date(2024, 1, 5)]

    def test_a_window_outside_coverage_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """未来窗口不是「没有数据」，是问了一个上游没发布的东西 —— 空元组会被读成休市。"""
        fetcher = self._fetcher(monkeypatch, [self._day(0), self._day(1)])

        with pytest.raises(ThsProviderError, match="THS_CALENDAR_OUT_OF_COVERAGE"):
            fetcher.fetch(start_date=date(2030, 1, 1), end_date=date(2030, 1, 31))

    def test_empty_calendar_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        fetcher = self._fetcher(monkeypatch, [])

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            fetcher.fetch()

    def test_a_window_ending_before_coverage_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """只给 `end_date` 也可能整段在 coverage 之外 —— 两侧都得拦，不能只拦一侧。"""
        fetcher = self._fetcher(monkeypatch, [self._day(1), self._day(3)])

        with pytest.raises(ThsProviderError, match="THS_CALENDAR_OUT_OF_COVERAGE"):
            fetcher.fetch(end_date=date(2023, 12, 31))

    def test_a_half_open_window_slices_from_either_edge(self, monkeypatch: pytest.MonkeyPatch):
        """单端窗口照样要切对：上游不收窗口，切片发生在本层。"""
        fetcher = self._fetcher(monkeypatch, [self._day(0), self._day(1), self._day(3)])

        assert [row.date for row in fetcher.fetch(start_date=date(2024, 1, 3))] == [
            date(2024, 1, 3),
            date(2024, 1, 5),
        ]
        # 先推邻接再切片 ⇒ 被切掉的行仍参与 prev/next，右端那一行的下一交易日
        # 是真实交易日，不是切口。
        assert [
            (row.date, row.next_trade_date) for row in fetcher.fetch(end_date=date(2024, 1, 3))
        ] == [
            (date(2024, 1, 2), date(2024, 1, 3)),
            (date(2024, 1, 3), date(2024, 1, 5)),
        ]


class TestInstrumentCatalogAdapter:
    """C14：标的目录腿 —— 一页一资产类型、分页到尽、快照语义与失败关闭路径。"""

    def _patch(self, monkeypatch: pytest.MonkeyPatch, handler) -> ThsInstrumentFetcher:
        monkeypatch.setattr(
            "opendata.data.providers.ths.models.instrument.client", _factory(handler)
        )
        return ThsInstrumentFetcher()

    def test_pages_are_read_until_one_comes_back_short(self, monkeypatch: pytest.MonkeyPatch):
        """短页即止：目录长度不写死，靠「本页不满」收敛。"""
        from opendata.data.providers.ths.models import instrument as module

        monkeypatch.setattr(module, "PAGE_LIMIT", 2)
        seen: list[str] = []
        pages = {
            "0": [_instrument_row("600519.SH"), _instrument_row("600000.SH")],
            "2": [_instrument_row("000001.SZ")],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            offset = request.url.params["offset"]
            seen.append(offset)
            return httpx.Response(200, content=_envelope_at(pages[offset], SNAPSHOT_MS))

        rows = self._patch(monkeypatch, handler).fetch(asset_type="a-share")

        assert seen == ["0", "2"]
        assert all(isinstance(row, Instrument) for row in rows)
        assert [row.symbol for row in rows] == ["000001.SZ", "600000.SH", "600519.SH"]
        assert [row.status for row in rows] == ["active"] * 3

    def test_the_asset_type_filter_is_sent_as_asked(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            rows = [_instrument_row("600519.SH")]
            return httpx.Response(200, content=_envelope_at(rows, SNAPSHOT_MS))

        self._patch(monkeypatch, handler).fetch(asset_type="futures")

        assert seen["path"] == "/api/meta/tickers/list"
        assert "asset_type=futures" in seen["params"]
        assert "limit=10000" in seen["params"]  # 单页取上游上限，小目录一次拿完

    def test_pagination_that_never_ends_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """offset 被上游忽略时页页都满：到顶即失败，而不是无限翻页。"""
        from opendata.data.providers.ths.models import instrument as module

        monkeypatch.setattr(module, "PAGE_LIMIT", 2)
        monkeypatch.setattr(module, "MAX_PAGES", 4)
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.params["offset"])
            return httpx.Response(
                200,
                content=_envelope_at(
                    [_instrument_row("600519.SH"), _instrument_row("600000.SH")], SNAPSHOT_MS
                ),
            )

        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_PAGINATION_STUCK"):
            self._patch(monkeypatch, handler).fetch(asset_type="a-share")

        assert len(calls) == 4

    def test_a_repeated_code_across_pages_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """分页期间快照重载 ⇒ 同一 thscode 出现两次；不去重，直接关在门外。"""
        from opendata.data.providers.ths.models import instrument as module

        monkeypatch.setattr(module, "PAGE_LIMIT", 2)
        pages = {
            "0": [_instrument_row("600519.SH"), _instrument_row("600000.SH")],
            "2": [_instrument_row("600519.SH")],
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_envelope_at(pages[request.url.params["offset"]], SNAPSHOT_MS)
            )

        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_DUPLICATE_SYMBOL"):
            self._patch(monkeypatch, handler).fetch(asset_type="a-share")

    def test_query_requires_a_documented_asset_type(self):
        fetcher = ThsInstrumentFetcher()

        assert fetcher.transform_query(asset_type="a-share").asset_type == "a-share"
        with pytest.raises(ValidationError):
            fetcher.transform_query()
        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_ASSET_TYPE_UNSUPPORTED"):
            fetcher.transform_query(asset_type="everything")

    def test_query_rejects_a_date_range(self):
        """目录只有当前清单：给了窗口就假装给的是历史快照，不接。"""
        fetcher = ThsInstrumentFetcher()

        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_SNAPSHOT_ONLY"):
            fetcher.transform_query(asset_type="a-share", start_date=date(2024, 1, 2))
        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_SNAPSHOT_ONLY"):
            fetcher.transform_query(asset_type="a-share", end_date=date(2024, 1, 2))

    def test_empty_catalog_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope_at([], SNAPSHOT_MS))

        with pytest.raises(ThsProviderError, match="THS_EMPTY_RESPONSE"):
            self._patch(monkeypatch, handler).fetch(asset_type="a-share")

    def test_a_catalog_without_a_snapshot_date_fails_closed(self, monkeypatch: pytest.MonkeyPatch):
        """status 由快照日推导：信封不给基准日，这一页就不可用。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_envelope([_instrument_row("600519.SH")]))

        with pytest.raises(ThsProviderError, match="THS_INSTRUMENT_SNAPSHOT_DATE_MISSING"):
            self._patch(monkeypatch, handler).fetch(asset_type="a-share")

    def test_an_in_market_contract_is_not_labelled_delisted(self, monkeypatch: pytest.MonkeyPatch):
        """旧判据（``end_date`` 非空即退市）把 874 个在市月份合约全标 delisted。"""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_envelope_at(
                    [
                        _instrument_row(
                            "IC2612.CFE",
                            exchange="CFFEX",
                            asset_type="futures",
                            currency=None,
                            list_date="2026-04-20",
                            end_date="2026-12-21",
                            last_trade_date="2026-12-18",
                        )
                    ],
                    SNAPSHOT_MS,
                ),
            )

        rows = self._patch(monkeypatch, handler).fetch(asset_type="futures")

        assert [row.status for row in rows] == ["active"]
        assert rows[0].delist_date == date(2026, 12, 18)  # 最后交易日，不是 12-21 交割日
        assert rows[0].currency == "CNY"  # 上游为 null 的本地补值


class TestFundActionAdapter:
    """C15：ETF 分红腿 —— 每份口径换算、裸码解析、无分红回空、窗口只切片。"""

    LAST_YEAR_MS = DAY_MS - 86_400_000 * 365

    def _patch(self, monkeypatch: pytest.MonkeyPatch, handler) -> ThsFundActionFetcher:
        monkeypatch.setattr(
            "opendata.data.providers.ths.models.fund_action.client", _factory(handler)
        )
        return ThsFundActionFetcher()

    def test_events_land_per_unit_newest_first(self, monkeypatch: pytest.MonkeyPatch):
        seen: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = str(request.url.params)
            return httpx.Response(
                200,
                content=_dividend_envelope(
                    [
                        _fund_dividend_row(DAY_MS, per_ten=1.23),
                        _fund_dividend_row(self.LAST_YEAR_MS, per_ten=0.88),
                    ]
                ),
            )

        events = self._patch(monkeypatch, handler).fetch(symbol="510300.SH")

        assert seen["path"] == "/api/fund/corporate-actions/dividends"
        assert "thscode=510300.SH" in seen["params"]
        assert all(isinstance(event, CorporateAction) for event in events)
        assert [event.cash_dividend for event in events] == [0.123, 0.088]
        assert events[0].ex_date > events[1].ex_date

    def test_a_bare_code_is_resolved_before_the_history_call(self, monkeypatch: pytest.MonkeyPatch):
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.path)
            if request.url.path.endswith("/tickers/list"):
                return httpx.Response(
                    200, content=_envelope([{"thscode": "510300.SH", "name": "沪深300ETF"}])
                )
            assert dict(request.url.params)["thscode"] == "510300.SH"
            return httpx.Response(200, content=_dividend_envelope([_fund_dividend_row(DAY_MS)]))

        events = self._patch(monkeypatch, handler).fetch(symbol="510300")

        assert len(events) == 1
        assert events[0].symbol == "510300.SH"
        assert len(seen) == 2  # 先解析、再取历史

    def test_a_fund_that_never_distributed_comes_back_empty(self, monkeypatch: pytest.MonkeyPatch):
        """上游把「从未分红」发成一整行 null，而不是空数组——那不是事件，也不该报错。"""
        placeholder = dict.fromkeys(_fund_dividend_row(DAY_MS))

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=_dividend_envelope([placeholder], count=0))

        assert self._patch(monkeypatch, handler).fetch(symbol="159915.SZ") == ()

    def test_a_window_slices_the_history_without_a_second_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """上游没有窗口参数：一次全量返回，窗口靠切片，不该重跑网络。"""
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            return httpx.Response(
                200,
                content=_dividend_envelope(
                    [
                        _fund_dividend_row(DAY_MS, per_ten=1.23),
                        _fund_dividend_row(self.LAST_YEAR_MS, per_ten=0.88),
                    ]
                ),
            )

        events = self._patch(monkeypatch, handler).fetch(
            symbol="510300.SH", start_date=date(2024, 1, 1), end_date=date(2024, 1, 2)
        )

        assert [event.cash_dividend for event in events] == [0.123]  # 2023 那笔在窗口之外
        assert len(calls) == 1

    def test_a_window_with_no_distribution_is_a_fact_not_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, content=_dividend_envelope([_fund_dividend_row(DAY_MS, per_ten=1.23)])
            )

        assert (
            self._patch(monkeypatch, handler).fetch(symbol="510300.SH", start_date=date(2030, 1, 1))
            == ()
        )

    def test_rows_the_upstream_cannot_reconcile_fail_closed(self, monkeypatch: pytest.MonkeyPatch):
        """自带的 ``dividend_count`` 与逐笔对不上时不发事件流：宁可挂，不发半截分红。"""
        from opendata_fuyao import FuyaoError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=_dividend_envelope(
                    [_fund_dividend_row(DAY_MS), _fund_dividend_row(self.LAST_YEAR_MS)], count=1
                ),
            )

        with pytest.raises(FuyaoError, match="fund_dividend_count_mismatch"):
            self._patch(monkeypatch, handler).fetch(symbol="510300.SH")

    def test_query_requires_a_symbol(self):
        fetcher = ThsFundActionFetcher()

        with pytest.raises(ValidationError):
            fetcher.transform_query()
        with pytest.raises(ValidationError):
            fetcher.transform_query(symbol="510300.SH", adjust="qfq")


@pytest.mark.e2e
class TestAgainstTheLiveFuyaoApi:
    """真机：经注册表路由取数（需要 fuyao 凭证，环境变量或 ``.env`` 均可）。"""

    @pytest.fixture(autouse=True)
    def require_key(self):
        """Gate on the same resolution the adapters use.

        ``credentials()`` also reads application settings; checking only the
        process environment used to skip this class on every machine that
        keeps the key in ``.env``, which meant the live legs were never
        actually exercised.
        """
        from opendata.data.providers.ths.models._client import ThsProviderError, credentials

        try:
            credentials()
        except ThsProviderError as exc:
            pytest.skip(f"no fuyao credentials: {exc!s}")

    def test_registry_routes_daily_bars_to_the_fuyao_source(self):
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("stock_daily", source="ths")
        rows = routed.fetch(
            symbol="600519.SH", start_date=date(2024, 1, 2), end_date=date(2024, 1, 5)
        )

        assert len(rows) == 4  # 2024-01-02..01-05（闭区间日）
        assert all(isinstance(row, Bar) and row.close > 0 for row in rows)

    def test_registry_routes_corporate_actions_to_the_fuyao_source(self):
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("stock_action", source="ths")
        events = routed.fetch(symbol="600519.SH")

        assert events and isinstance(events[0], CorporateAction)
        assert events[0].ex_date >= events[-1].ex_date

    def test_registry_routes_the_trading_calendar_to_the_fuyao_source(self):
        """真机：日历腿经注册表路由，行数量级与 coverage 语义都要成立。"""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("trading_calendar", source="ths")
        days = routed.fetch()

        assert len(days) > 200  # 实测一次返回 242 个开市日
        assert all(isinstance(day, TradingCalendar) and day.is_open for day in days)
        assert days[0].date < days[-1].date
        assert days[0].prev_trade_date is None
        assert days[-1].next_trade_date is None  # 上游不含当日，右端之外无从得知

    def test_registry_routes_the_instrument_catalog_to_the_fuyao_source(self):
        """真机：a-share 目录经注册表路由，一分钟内可翻完（实测 5,578 行/1 页）。"""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("instrument", source="ths")
        rows = routed.fetch(asset_type="a-share")

        assert len(rows) > 5000
        assert all(isinstance(row, Instrument) and row.symbol for row in rows)
        assert {row.status for row in rows} == {"active"}  # 上游不发布已退市 A 股
        assert rows[0].symbol < rows[-1].symbol  # 按 thscode 升序
        # 上市区间的左端：实测整表仅 9 行为 null（新股未回填），留冗余不误判
        assert sum(1 for row in rows if row.list_date is None) <= 20

    def test_registry_routes_a_bare_index_code_to_the_fuyao_source(self):
        """C18 真机：裸指数码经指数目录解析取到沪深300.

        旧实现在这一步必抛 ``ticker_code_mismatch``（目录里 206/1,431 行的展示码
        本就与 ``thscode`` 不同 ⇒ 整表被拒），所以这条用例吃的正是「解析器能不能
        从真实目录里拿到候选」，而不是又一个带后缀的写法。
        """
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("index_daily", source="ths")
        rows = routed.fetch(symbol="000300", start_date=date(2024, 9, 2), end_date=date(2024, 9, 6))

        assert len(rows) == 5  # 该周无休市日，半开窗 [09-02, 09-06] 共 5 个交易日
        assert {row.symbol for row in rows} == {"000300.SH"}  # 裸码解析出的限定写法
        assert all(bar.close > 0 for bar in rows)
        assert [rows[0].trade_date, rows[-1].trade_date] == [date(2024, 9, 2), date(2024, 9, 6)]

    def test_registry_routes_fund_distributions_to_the_fuyao_source(self):
        """真机：300ETF 分红经注册表路由，每份口径与量级成立（实测 14 笔、Σ 0.88）。"""
        from opendata.data.providers import register_providers
        from opendata.data.registry import get_registry

        register_providers()
        routed = get_registry().resolve_domain("fund_action", source="ths")
        events = routed.fetch(symbol="510300.SH")

        assert len(events) >= 14
        assert all(isinstance(event, CorporateAction) for event in events)
        assert [event.ex_date for event in events] == sorted(
            (event.ex_date for event in events), reverse=True
        )
        assert all(0.0 < event.cash_dividend < 1.0 for event in events)
        assert sum(event.cash_dividend for event in events) >= 0.88 - 1e-9
