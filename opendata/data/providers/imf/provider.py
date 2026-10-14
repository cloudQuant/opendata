"""Provider metadata and lazy bindings for the IMF adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="imf",
    name="IMF",
    description="IMF SDMX and DataMapper adapters for international macroeconomic series.",
    website="https://www.imf.org/en/Data",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.cpi",
            "ImfCpiFetcher",
            scenario="宏观 CPI（AI 研究 / 资产配置）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.gdp",
            "ImfGdpFetcher",
            scenario="宏观 GDP（AI 研究 / 资产配置）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.unemployment",
            "ImfUnemploymentFetcher",
            scenario="宏观失业率（AI 研究 / 资产配置）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfAvailableIndicatorsFetcher",
            canonical_model_ids=("AvailableIndicators",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfConsumerPriceIndexFetcher",
            canonical_model_ids=("ConsumerPriceIndex",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfDirectionOfTradeFetcher",
            canonical_model_ids=("DirectionOfTrade",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfEconomicIndicatorFetcher",
            canonical_model_ids=("EconomicIndicators",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfChokePointInfoFetcher",
            canonical_model_ids=("MaritimeChokePointInfo",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfChokePointVolumeFetcher",
            canonical_model_ids=("MaritimeChokePointVolume",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfPortInfoFetcher",
            canonical_model_ids=("PortInfo",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfPortVolumeFetcher",
            canonical_model_ids=("PortVolume",),
        ),
    ),
)
