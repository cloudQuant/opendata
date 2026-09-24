"""Source health patrol tests (B3.4 / AC-4 / AC-19).

Probes run against stub fetchers in a private registry - no real
network - covering the healthy/failing/verified-skipping branches, the
key configuration report and the HTTP surface.
"""

from __future__ import annotations

import pytest

from opendata.data.capability import Capability
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.registry import ProviderRegistry
from opendata.pipeline.patrol import key_status, patrol


class StubQuery(QueryParams):
    symbol: str = ""


class StubFetcher(Fetcher[StubQuery, object]):
    """Fetcher whose behaviour the test wires explicitly."""

    def __init__(self, capability: Capability, *, raise_on: Exception | None = None):
        self.capability = capability
        self.raise_on = raise_on
        self.calls = 0

    def transform_query(self, **kwargs: object) -> StubQuery:
        return StubQuery(**kwargs)

    def extract_data(self, params: StubQuery, ctx: FetchContext) -> object:
        self.calls += 1
        if self.raise_on is not None:
            raise self.raise_on
        return "raw"

    def transform_data(self, raw: object, params: StubQuery) -> object:
        return "ok"


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


class TestPatrol:
    async def test_healthy_fetcher_is_reported_ok(self):
        registry = _registry(StubFetcher(_capability("stock_daily")))

        results = await patrol(registry)

        assert len(results) == 1
        assert results[0].ok is True
        assert results[0].domain == "stock_daily"
        assert results[0].error is None
        assert results[0].latency_ms >= 0

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
