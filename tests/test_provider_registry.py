"""Provider registry and protocol tests (A1.2 / AC-3).

Covers the A1.2 acceptance set: registration, auto routing by
authority, degradation to the next source, explicit-source failures
and the verified/notes exclusion from auto routing, plus the
three-stage fetcher pipeline.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from opendata.data import registry as registry_module
from opendata.data.capability import ON_DEMAND, UPSTREAM_PENDING, Capability
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.registry import ProviderRegistry, authority_baseline, get_registry


class StubQuery(QueryParams):
    symbol: str


class StubFetcher(Fetcher[StubQuery, object]):
    """Records stage calls and returns empty results."""

    def __init__(self, capability):
        self.capability = capability
        self.calls = []
        self.seen_ctx = None

    def transform_query(self, **kwargs):
        self.calls.append("transform_query")
        return StubQuery(**kwargs)

    def extract_data(self, params, ctx):
        self.calls.append("extract_data")
        self.seen_ctx = ctx
        return "raw"

    def transform_data(self, raw, params):
        self.calls.append("transform_data")
        return []


def make_cap(
    source,
    *,
    domain="stock_daily",
    verified=True,
    notes="",
    period="1D",
    market="cn",
    asset_class="equity",
):
    """Build a capability with sensible defaults for the stubs."""
    return Capability(
        asset_class=asset_class,
        domain=domain,
        period=period,
        market=market,
        source=source,
        verified=verified,
        notes=notes,
    )


def make_registry(*sources, domain="stock_daily", verified=True, notes=""):
    """Registry with one stub fetcher per given source."""
    registry = ProviderRegistry()
    for source in sources:
        registry.register(
            StubFetcher(make_cap(source, domain=domain, verified=verified, notes=notes))
        )
    return registry


class TestRegistration:
    def test_register_lists_capabilities(self):
        registry = make_registry("ths", "akshare")
        assert sorted(cap.source for cap in registry.capabilities()) == ["akshare", "ths"]

    def test_duplicate_capability_rejected(self):
        registry = make_registry("ths")
        with pytest.raises(ValueError, match="already registered"):
            registry.register(StubFetcher(make_cap("ths")))

    def test_get_registry_is_singleton(self):
        assert get_registry() is get_registry()

    def test_authority_baseline_matches_design(self):
        # Design §4.4: ths is authoritative, akshare is the fallback.
        assert authority_baseline()["stock_daily"] == ("ths", "akshare")

    def test_authority_baseline_fails_closed(self, monkeypatch):
        registry_module.authority_baseline.cache_clear()
        monkeypatch.setattr(registry_module, "_AUTHORITY_PATH", Path("/nonexistent.json"))
        with pytest.raises(RuntimeError, match="unreadable"):
            registry_module.authority_baseline()
        registry_module.authority_baseline.cache_clear()


class TestAutoRouting:
    def test_auto_prefers_authority_source(self):
        registry = make_registry("akshare", "ths")
        assert registry.resolve("equity", "stock_daily").capability.source == "ths"

    def test_auto_degrades_to_next_when_unhealthy(self):
        registry = make_registry("ths", "akshare")
        registry.mark_unavailable(registry.resolve("equity", "stock_daily"))
        assert registry.resolve("equity", "stock_daily").capability.source == "akshare"

    def test_auto_fails_when_all_unhealthy(self):
        registry = make_registry("ths", "akshare")
        registry.mark_unavailable(registry.resolve("equity", "stock_daily"))
        registry.mark_unavailable(registry.resolve("equity", "stock_daily"))
        with pytest.raises(LookupError, match="unavailable"):
            registry.resolve("equity", "stock_daily")

    def test_health_restored_after_mark_available(self):
        registry = make_registry("ths")
        fetcher = registry.resolve("equity", "stock_daily")
        registry.mark_unavailable(fetcher)
        with pytest.raises(LookupError, match="unavailable"):
            registry.resolve("equity", "stock_daily")
        registry.mark_available(fetcher)
        assert registry.resolve("equity", "stock_daily") is fetcher

    def test_unlisted_source_ranks_after_baseline(self):
        registry = ProviderRegistry()
        registry.register(StubFetcher(make_cap("yfinance")))
        registry.register(StubFetcher(make_cap("ths")))
        assert registry.resolve("equity", "stock_daily").capability.source == "ths"
        registry.mark_unavailable(registry.resolve("equity", "stock_daily"))
        assert registry.resolve("equity", "stock_daily").capability.source == "yfinance"

    def test_unverified_excluded_from_auto(self):
        registry = make_registry("ths", "akshare", verified=False)
        with pytest.raises(LookupError, match="no verified capability"):
            registry.resolve("equity", "stock_daily")

    @pytest.mark.parametrize("notes", [ON_DEMAND, UPSTREAM_PENDING])
    def test_reserved_notes_excluded_from_auto(self, notes):
        registry = make_registry("ths", notes=notes)
        with pytest.raises(LookupError, match="no verified capability"):
            registry.resolve("equity", "stock_daily")


class TestExplicitRouting:
    def test_explicit_source_routes_directly(self):
        registry = make_registry("ths", "akshare")
        assert (
            registry.resolve("equity", "stock_daily", source="akshare").capability.source
            == "akshare"
        )

    def test_explicit_source_no_fallback(self):
        # The authority source is healthy but was not requested.
        registry = make_registry("ths", "akshare")
        with pytest.raises(LookupError, match="no registered capability"):
            registry.resolve("equity", "stock_daily", source="nope")

    def test_explicit_source_serves_unverified(self):
        registry = make_registry("akshare", verified=False)
        assert (
            registry.resolve("equity", "stock_daily", source="akshare").capability.source
            == "akshare"
        )

    def test_explicit_source_serves_on_demand(self):
        registry = make_registry("akshare", notes=ON_DEMAND)
        assert (
            registry.resolve("equity", "stock_daily", source="akshare").capability.source
            == "akshare"
        )

    def test_period_and_market_filters(self):
        registry = make_registry("ths")
        with pytest.raises(LookupError, match="no registered capability"):
            registry.resolve("equity", "stock_daily", period="1W", source="ths")
        with pytest.raises(LookupError, match="no registered capability"):
            registry.resolve("equity", "stock_daily", market="us", source="ths")


class TestCapability:
    def test_participates_in_auto_requires_verified(self):
        assert make_cap("ths", verified=False).participates_in_auto() is False
        assert make_cap("ths").participates_in_auto() is True

    @pytest.mark.parametrize("notes", [ON_DEMAND, UPSTREAM_PENDING])
    def test_participates_in_auto_rejects_reserved_notes(self, notes):
        assert make_cap("ths", notes=notes).participates_in_auto() is False

    def test_capability_is_strict(self):
        with pytest.raises(ValidationError):
            Capability(
                asset_class="equity",
                domain="stock_daily",
                period="1D",
                market="cn",
                source="ths",
                verified=True,
                typo="x",
            )


class TestFetcherPipeline:
    def test_fetch_runs_stages_in_order(self):
        fetcher = StubFetcher(make_cap("ths"))
        result = fetcher.fetch(symbol="600519.SH")
        assert result == []
        assert fetcher.calls == ["transform_query", "extract_data", "transform_data"]

    def test_fetch_passes_context(self):
        fetcher = StubFetcher(make_cap("ths"))
        fetcher.fetch(symbol="600519.SH", ctx=FetchContext(timeout=5.0))
        assert fetcher.seen_ctx == FetchContext(timeout=5.0)

    def test_fetch_validates_query_params(self):
        fetcher = StubFetcher(make_cap("ths"))
        with pytest.raises(ValidationError):
            fetcher.fetch(symbol="600519.SH", bogus="typo")

    def test_query_params_defaults(self):
        params = StubQuery(symbol="600519.SH")
        assert params.source == "auto"
        assert params.market is None
        assert params.start_date is None
        assert params.end_date is None

    def test_query_params_narrows_symbol(self):
        with pytest.raises(ValidationError):
            StubQuery(symbol=None)
