"""Provider metadata and lazy bindings for the OECD adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="oecd",
    name="OECD",
    description="OECD SDMX adapter for macroeconomic series.",
    website="https://data-explorer.oecd.org/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.cpi",
            "OecdCpiFetcher",
            scenario="宏观 CPI（AI 研究 / 资产配置）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.unemployment",
            "OecdUnemploymentFetcher",
            scenario="宏观失业率（AI 研究 / 资产配置）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdCompositeLeadingIndicatorFetcher",
            canonical_model_ids=("CompositeLeadingIndicator",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdConsumerPriceIndexFetcher",
            canonical_model_ids=("ConsumerPriceIndex",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdCountryInterestRateFetcher",
            canonical_model_ids=("CountryInterestRates",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpNominalFetcher",
            canonical_model_ids=("GdpNominal",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpRealFetcher",
            canonical_model_ids=("GdpReal",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpForecastFetcher",
            canonical_model_ids=("GdpForecast",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdHousePriceIndexFetcher",
            canonical_model_ids=("HousePriceIndex",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdSharePriceIndexFetcher",
            canonical_model_ids=("SharePriceIndex",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdUnemploymentFetcher",
            canonical_model_ids=("Unemployment",),
        ),
    ),
)
