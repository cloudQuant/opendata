"""Verify FMP market fetchers are exposed through the normal provider descriptor."""

from __future__ import annotations

from opendata.data.providers.fmp.provider import PROVIDER

_MARKET_BINDINGS = {
    "CryptoHistorical": ("CryptoHistoricalFetcher", "fmp_crypto_historical", "global"),
    "CryptoSearch": ("CryptoSearchFetcher", "fmp_crypto_search", "global"),
    "CurrencyHistorical": ("CurrencyHistoricalFetcher", "fmp_currency_historical", "global"),
    "CurrencyPairs": ("CurrencyPairsFetcher", "fmp_currency_pairs", "global"),
    "CurrencySnapshots": ("CurrencySnapshotsFetcher", "fmp_currency_snapshots", "global"),
    "EquityActive": ("EquityActiveFetcher", "fmp_equity_active", "us"),
    "EquityPeers": ("EquityPeersFetcher", "fmp_equity_peers", "global"),
    "EquityInfo": ("EquityInfoFetcher", "fmp_equity_info", "global"),
    "EquityGainers": ("EquityGainersFetcher", "fmp_equity_gainers", "us"),
    "EquityLosers": ("EquityLosersFetcher", "fmp_equity_losers", "us"),
    "EquityScreener": ("EquityScreenerFetcher", "fmp_equity_screener", "global"),
    "MarketSnapshots": ("MarketSnapshotsFetcher", "fmp_market_snapshots", "global"),
    "PricePerformance": ("PricePerformanceFetcher", "fmp_price_performance", "global"),
    "ShareStatistics": ("ShareStatisticsFetcher", "fmp_share_statistics", "global"),
    "IndexConstituents": ("IndexConstituentsFetcher", "fmp_index_constituents", "us"),
    "IndexHistorical": ("IndexHistoricalFetcher", "fmp_index_historical", "global"),
}


def test_fmp_market_fetchers_are_lazy_provider_bindings() -> None:
    """Resolve market identities through the provider descriptor with declared markets."""
    bindings_by_model = {
        model_id: binding
        for binding in PROVIDER.fetcher_bindings
        for model_id in binding.canonical_model_ids
    }

    assert {model_id for model_id in bindings_by_model if model_id in _MARKET_BINDINGS} == set(
        _MARKET_BINDINGS
    )

    for model_id, (class_name, domain, market) in _MARKET_BINDINGS.items():
        binding = bindings_by_model[model_id]
        assert binding.module == "opendata.data.providers.fmp.models.iter2_market"
        assert binding.class_name == class_name

        fetcher = binding.load(PROVIDER.source)
        assert fetcher.capability.source == "fmp"
        assert fetcher.capability.domain == domain
        assert fetcher.capability.market == market
        assert fetcher.canonical_model == model_id
