"""Verify FMP ETF fetchers are exposed through the normal provider descriptor."""

from __future__ import annotations

from opendata.data.providers.fmp.provider import PROVIDER

_ETF_BINDINGS = {
    "EtfCountries": ("EtfCountriesFetcher", "fmp_etf_countries"),
    "EtfEquityExposure": ("EtfEquityExposureFetcher", "fmp_etf_equity_exposure"),
    "EtfHoldings": ("EtfHoldingsFetcher", "fmp_etf_holdings"),
    "EtfInfo": ("EtfInfoFetcher", "fmp_etf_info"),
    "EtfPricePerformance": ("EtfPricePerformanceFetcher", "fmp_etf_price_performance"),
    "EtfSearch": ("EtfSearchFetcher", "fmp_etf_search"),
    "EtfSectors": ("EtfSectorsFetcher", "fmp_etf_sectors"),
    "EtfHistorical": ("EtfHistoricalFetcher", "fmp_etf_historical"),
}


def test_fmp_etf_fetchers_are_lazy_provider_bindings() -> None:
    """Resolve each ETF identity through FMP's descriptor and retain legacy price bindings."""
    bindings_by_model = {
        model_id: binding
        for binding in PROVIDER.fetcher_bindings
        for model_id in binding.canonical_model_ids
    }

    assert {model_id for model_id in bindings_by_model if model_id.startswith("Etf")} == set(
        _ETF_BINDINGS
    )
    assert {"EquityHistorical", "EquityQuote"} <= set(bindings_by_model)

    for model_id, (class_name, domain) in _ETF_BINDINGS.items():
        binding = bindings_by_model[model_id]
        assert binding.module == "opendata.data.providers.fmp.models.iter2_etf"
        assert binding.class_name == class_name

        fetcher = binding.load(PROVIDER.source)
        assert fetcher.capability.source == "fmp"
        assert fetcher.capability.domain == domain
        assert fetcher.canonical_model == model_id
