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

from opendata.data.models import Bar, CorporateAction
from opendata.data.protocol import FetchContext
from opendata.data.providers.ths.models._client import (
    ThsProviderError,
    credentials,
    resolve_code,
)
from opendata.data.providers.ths.models.stock_action import ThsStockActionFetcher
from opendata.data.providers.ths.models.stock_daily import ThsStockDailyFetcher
from opendata.data.providers.ths.registration import FETCHERS, register
from opendata.data.registry import ProviderRegistry, authority_baseline, get_registry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from opendata_fuyao import FuyaoHttpClient

DAY_MS = 1704124800000  # 2024-01-02 00:00 +08:00


def _envelope(items: list[dict]) -> bytes:
    return json.dumps(
        {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {"timestamp": None, "item": items},
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
        }

    def test_ths_is_the_declared_domestic_authority(self):
        assert authority_baseline()["stock_daily"][0] == "ths"
        assert authority_baseline()["stock_action"][0] == "ths"

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
