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
            scenario=(
                "IMF 指标目录（按指定数据流）（economy_imf_available_indicators 域，"
                "列目录或指定数据流的指标代码）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfConsumerPriceIndexFetcher",
            canonical_model_ids=("ConsumerPriceIndex",),
            scenario="IMF 消费者价格指数（economy_imf_cpi 域，取国家与调和 CPI 观察值）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfDirectionOfTradeFetcher",
            canonical_model_ids=("DirectionOfTrade",),
            scenario="IMF 双边货物贸易（economy_imf_direction_of_trade 域，查 IMTS 数据集）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.iter2",
            "ImfEconomicIndicatorFetcher",
            canonical_model_ids=("EconomicIndicators",),
            scenario="IMF 宏观经济指标（economy_imf_indicators 域，按源声明维度查 SDMX 数据流）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfChokePointInfoFetcher",
            canonical_model_ids=("MaritimeChokePointInfo",),
            scenario=(
                "IMF PortWatch 海上咽喉点元数据（imf_maritime_chokepoint_info 域，"
                "读官方图层的长期静态元数据）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfChokePointVolumeFetcher",
            canonical_model_ids=("MaritimeChokePointVolume",),
            scenario=(
                "IMF PortWatch 每日海上咽喉点流量（imf_maritime_chokepoint_volume 域，"
                "读每日通行数与运力估计）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfPortInfoFetcher",
            canonical_model_ids=("PortInfo",),
            scenario="IMF PortWatch 港口元数据（imf_port_info 域，读港口属性或按年吞吐排名）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.portwatch",
            "ImfPortVolumeFetcher",
            canonical_model_ids=("PortVolume",),
            scenario="IMF PortWatch 每日港口活动与贸易量（imf_port_volume 域，读每日停靠与估算量）",
        ),
    ),
)
