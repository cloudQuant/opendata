"""Source health patrol tests (B3.4 / AC-4 / AC-19).

Probes run against stub fetchers in a private registry - no real
network - covering the healthy/failing/verified-skipping branches, the
key configuration report and the HTTP surface.

The C16 guard cases are the load-bearing ones: they read the *real*
registry, so a newly verified leg without a working probe fails the
suite here instead of quietly weakening ``auto`` routing in production.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.registry import ProviderRegistry
from opendata.pipeline import patrol as patrol_module
from opendata.pipeline.patrol import (
    PROBE_PARAMS,
    PROBE_RESOLVERS,
    PatrolProbeConfigError,
    PatrolProbeError,
    first_live_contract,
    key_status,
    patrol,
    probe_params,
    rolling_fund_etf_params,
    rolling_futures_params,
    rolling_option_params,
)
from opendata_fuyao.endpoints import FUND_ETF_DEPTH_DAYS


class StubQuery(QueryParams):
    symbol: str = ""
    asset_type: str = ""


class StubFetcher(Fetcher[StubQuery, object]):
    """Fetcher whose behaviour the test wires explicitly."""

    def __init__(
        self,
        capability: Capability,
        *,
        raise_on: Exception | None = None,
        returns: object = "ok",
    ):
        self.capability = capability
        self.raise_on = raise_on
        self.returns = returns
        self.calls = 0
        self.seen: dict[str, object] = {}

    def transform_query(self, **kwargs: object) -> StubQuery:
        return StubQuery(**kwargs)

    def extract_data(self, params: StubQuery, ctx: FetchContext) -> object:
        self.calls += 1
        self.seen = params.model_dump(exclude_defaults=True)
        if self.raise_on is not None:
            raise self.raise_on
        return "raw"

    def transform_data(self, raw: object, params: StubQuery) -> object:
        return self.returns


class StubCatalog:
    """Instrument-catalog stub the option probe reads instead of the net."""

    def __init__(self, rows: object):
        self.rows = rows

    def fetch(self, **kwargs: object) -> object:
        assert kwargs == {"asset_type": "options"}
        return self.rows


def _capability(domain: str, source: str = "ths", verified: bool = True) -> Capability:
    return Capability(
        asset_class="equity",
        domain=domain,
        period="1D",
        market="cn",
        source=source,
        verified=verified,
    )


def _registry(*fetchers: StubFetcher) -> ProviderRegistry:
    registry = ProviderRegistry()
    for fetcher in fetchers:
        registry.register(fetcher)
    return registry


@pytest.fixture
def real_registry() -> ProviderRegistry:
    """The process registry with every bundled provider registered."""
    from opendata.data.providers import register_providers
    from opendata.data.registry import get_registry

    register_providers()
    return get_registry()


@pytest.fixture
def offline_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the option probe a stub catalog so no test hits the network."""
    rows = [
        _instrument("10011425.SH", date(2026, 12, 23)),
        _instrument("10011424.SH", date(2026, 10, 1)),
        _instrument("10011423.SH", None),
    ]
    monkeypatch.setattr(patrol_module, "_catalog_fetcher", lambda registry: StubCatalog(rows))


def _instrument(symbol: str, delist_date: date | None) -> object:
    class _Row:
        pass

    row = _Row()
    row.symbol = symbol
    row.delist_date = delist_date
    return row


class TestPatrol:
    async def test_healthy_fetcher_is_reported_ok(self):
        registry = _registry(StubFetcher(_capability("stock_daily")))

        results = await patrol(registry)

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].domain == "stock_daily"
        assert results[0].error is None
        assert results[0].latency_ms >= 0
        assert results[0].rows == 2

    async def test_failing_fetcher_marks_the_source_unhealthy(self):
        fetcher = StubFetcher(_capability("stock_daily"), raise_on=RuntimeError("boom"))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "boom" in (results[0].error or "")
        # auto routing must skip the broken source now
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_unverified_capabilities_are_not_probed(self):
        registry = _registry(
            StubFetcher(_capability("stock_daily", verified=False), raise_on=RuntimeError("x"))
        )

        results = await patrol(registry)

        assert results == []

    async def test_probe_uses_the_minimal_params(self):
        fetcher = StubFetcher(_capability("stock_daily"))
        registry = _registry(fetcher)

        await patrol(registry)

        assert fetcher.calls == 1
        assert fetcher.seen["symbol"] == "600519"

    async def test_empty_probe_counts_as_unhealthy(self):
        fetcher = StubFetcher(_capability("stock_daily"), returns=())
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "no rows" in (results[0].error or "")
        with pytest.raises(LookupError):
            registry.resolve("equity", "stock_daily", source="auto")

    async def test_missing_probe_is_reported_without_blaming_the_source(self):
        """An unconfigured probe is the patrol's gap, not a broken source."""
        fetcher = StubFetcher(_capability("not_a_real_domain"))
        registry = _registry(fetcher)

        results = await patrol(registry)

        assert results[0].ok is False
        assert "PatrolProbeConfigError" in (results[0].error or "")
        assert fetcher.calls == 0
        # routing health untouched: the source is still reachable
        assert registry.resolve("equity", "not_a_real_domain", source="auto") is fetcher


class TestProbeParamCoverage:
    """C16 guards: probe params are keyed by (domain, source) and complete."""

    def test_probe_tables_match_the_verified_capabilities_exactly(
        self, real_registry: ProviderRegistry
    ):
        verified = {(c.domain, c.source) for c in real_registry.capabilities() if c.verified}

        assert set(PROBE_PARAMS) | set(PROBE_RESOLVERS) == verified
        assert set(PROBE_PARAMS) & set(PROBE_RESOLVERS) == set()

    def test_every_probe_validates_offline(
        self,
        real_registry: ProviderRegistry,
        offline_catalog: None,
    ):
        """Each verified capability must accept its own probe params.

        ``transform_query`` is the same validate stage the real probe runs,
        so a leg that lands verified without a usable probe fails here.
        """
        for capability in real_registry.capabilities():
            if not capability.verified:
                continue
            fetcher = real_registry.resolve(
                capability.asset_class, capability.domain, source=capability.source
            )
            params = probe_params(capability, real_registry, today=date(2026, 9, 25))

            query = fetcher.transform_query(**params)

            assert set(params) <= set(query.model_fields_set)


class TestRollingProbes:
    """Legs whose instrument expires - or whose window does - are named anew."""

    def test_futures_contract_is_the_lead_month_after_today(self):
        params = rolling_futures_params(date(2026, 9, 25))

        assert params["symbol"] == "CU2611.SHF"
        assert params["start_date"] == date(2026, 9, 4)
        assert params["end_date"] == date(2026, 9, 25)

    def test_futures_contract_rolls_over_the_year(self):
        assert rolling_futures_params(date(2026, 12, 30))["symbol"] == "CU2702.SHF"

    def test_option_contract_comes_from_the_catalog(self, offline_catalog: None):
        params = rolling_option_params(date(2026, 9, 25), _registry())

        assert params["symbol"] == "10011425.SH"
        assert params["end_date"] == date(2026, 9, 25)

    def test_the_etf_probe_window_rolls_with_the_probe_date(self):
        """标的固定、窗口不固定：这条通道的可答深度本身就是「今天 - 1827 天」。"""
        params = rolling_fund_etf_params(date(2026, 9, 25))

        assert params["symbol"] == "510300"
        assert params["start_date"] == date(2026, 9, 4)
        assert params["end_date"] == date(2026, 9, 25)

    def test_the_etf_probe_never_asks_past_the_rolling_depth(self):
        """判据是相对量：窗口一旦长过深度，腿会在发请求前自己拒答（C20）。"""
        for probe_day in (date(2026, 9, 25), date(2031, 1, 3), date(2038, 7, 20)):
            params = rolling_fund_etf_params(probe_day)

            assert (probe_day - params["start_date"]).days < FUND_ETF_DEPTH_DAYS

    def test_pick_skips_contracts_expiring_inside_the_horizon(self):
        rows = [
            _instrument("10011425.SH", date(2026, 12, 23)),
            _instrument("10011420.SH", date(2026, 10, 1)),
            _instrument("10011421.SH", None),
        ]

        assert first_live_contract(rows, date(2026, 9, 25)) == "10011425.SH"

    def test_pick_raises_when_nothing_lives_on(self):
        rows = [_instrument("10011420.SH", date(2026, 10, 1))]

        with pytest.raises(PatrolProbeError):
            first_live_contract(rows, date(2026, 9, 25))


class _NotRoutable(ProviderRegistry):
    """Registry that advertises a verified capability it cannot route to."""

    def resolve(self, *args: object, **kwargs: object) -> Fetcher[Any, Any]:
        raise LookupError("no registered capability")


class TestProbePlumbing:
    """The code that assembles a probe, not only the tables it reads."""

    def test_the_option_probe_uses_the_registered_catalog_leg(self, real_registry):
        catalog = next(
            c
            for c in real_registry.capabilities()
            if c.domain == "instrument" and c.source == "ths"
        )
        expected = real_registry.resolve(catalog.asset_class, "instrument", source="ths")

        assert patrol_module._catalog_fetcher(real_registry) is expected

    def test_a_missing_catalog_leg_is_the_patrols_own_gap(self):
        with pytest.raises(PatrolProbeConfigError):
            patrol_module._catalog_fetcher(ProviderRegistry())

    def test_the_option_resolver_falls_back_to_the_process_registry(
        self, offline_catalog: None, real_registry: ProviderRegistry
    ):
        params = rolling_option_params(date(2026, 9, 25))

        assert params["symbol"] == "10011425.SH"

    async def test_an_unroutable_capability_is_skipped_not_blamed(self):
        registry = _NotRoutable()
        registry.register(StubFetcher(_capability("stock_daily")))

        assert await patrol(registry) == []

    def test_run_patrol_is_the_sync_entry_a_cron_calls(self, monkeypatch):
        async def fake_patrol(*args: object) -> list[str]:
            return ["probed"]

        monkeypatch.setattr(patrol_module, "patrol", fake_patrol)

        assert patrol_module.run_patrol() == ["probed"]


class TestKeyStatus:
    def test_ths_requires_a_key(self, monkeypatch):
        keys = key_status(_settings(ths="", fred=""))

        assert keys["ths"]["required"] is True
        assert keys["ths"]["configured"] is False

    def test_configured_keys_are_reported(self, monkeypatch):
        keys = key_status(_settings(ths="t", fred="f"))

        assert keys["ths"]["configured"] is True
        assert keys["fred"]["configured"] is True


def _settings(ths: str, fred: str):
    class _S:
        fuyao_api_key = ths
        fred_api_key = fred

    return _S()


@pytest.mark.asyncio
class TestHealthApi:
    async def test_sources_endpoint_requires_auth(self, test_client):
        response = await test_client.get("/api/v1/health/sources")

        assert response.status_code == 401

    async def test_sources_endpoint_lists_credentials(
        self, test_client, test_user_token, monkeypatch
    ):
        monkeypatch.setattr("opendata.api.pipeline.key_status", lambda: _status("t", "f"))
        response = await test_client.get(
            "/api/v1/health/sources",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        sources = response.json()["data"]["sources"]
        assert sources["ths"]["configured"] is True

    async def test_patrol_endpoint_reports_probe_results(
        self, test_client, test_user_token, monkeypatch
    ):
        registry = _registry(StubFetcher(_capability("stock_daily")))
        monkeypatch.setattr("opendata.api.pipeline.patrol", lambda: _patrol_done(registry))
        response = await test_client.post(
            "/api/v1/health/patrol",
            headers={"Authorization": f"Bearer {test_user_token}"},
        )

        assert response.status_code == 200
        data = response.json()["data"]
        assert data["count"] == 1
        assert data["healthy"] == 1


async def _patrol_done(registry: ProviderRegistry):
    return await patrol(registry)


def _status(ths: str, fred: str):
    return {
        "ths": {"required": True, "configured": bool(ths), "endpoint": "fuyao.aicubes.cn"},
        "fred": {"required": True, "configured": bool(fred), "endpoint": "api.stlouisfed.org"},
    }
