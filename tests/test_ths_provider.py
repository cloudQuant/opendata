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

from opendata.data.models import Bar, CorporateAction, IndexConstituent
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    client,
    credentials,
    resolve_code,
    resolve_futures_code,
    resolve_index_code,
    resolve_option_code,
)
from opendata.data.providers.ths.models.futures_daily import ThsFuturesDailyFetcher
from opendata.data.providers.ths.models.index_constituent import ThsIndexConstituentFetcher
from opendata.data.providers.ths.models.index_daily import ThsIndexDailyFetcher
from opendata.data.providers.ths.models.option_daily import ThsOptionDailyFetcher
from opendata.data.providers.ths.models.stock_action import ThsStockActionFetcher
from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
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


def _event_row() -> dict:
    return {
        "ticker": "600519",
        "ex_date_ms": DAY_MS,
        "dividend_per_share": 30.876,
        "per_share_bonus": 0.0,
    }


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
            ("futures_daily", "ths"),
            ("option_daily", "ths"),
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
            ("futures", "futures_daily", "1D", "cn"),
            ("option", "option_daily", "1D", "cn"),
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
        """指数走列表解析：search 端点是按名称检索的，数字代码查不到."""

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["asset_type"] == "a-share-index"
            return httpx.Response(
                200,
                content=_envelope(
                    [
                        {"thscode": "886042.TI", "name": "存储芯片"},
                        {"thscode": "000300.SH", "name": "沪深300"},
                    ]
                ),
            )

        with _mock_client(handler) as active:
            assert resolve_index_code(active, "000300") == "000300.SH"

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            [{"thscode": "000301.SH", "name": "别的指数"}],
            [
                {"thscode": "000300.SH", "name": "沪深300"},
                {"thscode": "000300.CSI", "name": "沪深300（中证）"},
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


@pytest.mark.e2e
class TestAgainstTheLiveFuyaoApi:
    """真机：经注册表路由取数（需要 FUYAO_API_KEY）。"""

    @pytest.fixture(autouse=True)
    def require_key(self):
        from opendata_fuyao import FuyaoCredentials

        if FuyaoCredentials.from_environment() is None:
            pytest.skip("FUYAO_API_KEY is not configured")

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
