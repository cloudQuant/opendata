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
            scenario="OECD 综合先行指标（economy_oecd_cli 域，按国家取 CLI 指数读数）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdConsumerPriceIndexFetcher",
            canonical_model_ids=("ConsumerPriceIndex",),
            scenario="OECD 消费者价格指数（economy_oecd_cpi 域，取总体与调和 CPI 观察值）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdCountryInterestRateFetcher",
            canonical_model_ids=("CountryInterestRates",),
            scenario="OECD 成员国利率（economy_oecd_interest_rates 域，按期限取即时/短/长期利率）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpNominalFetcher",
            canonical_model_ids=("GdpNominal",),
            scenario="OECD 名义 GDP（economy_oecd_gdp_nominal 域，取现价、指数与人均名义产出）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpRealFetcher",
            canonical_model_ids=("GdpReal",),
            scenario="OECD 实际 GDP（economy_oecd_gdp_real 域，按季/年取不变价产出观察值）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdGdpForecastFetcher",
            canonical_model_ids=("GdpForecast",),
            scenario="OECD GDP 预测（economy_oecd_gdp_forecast 域，按口径取经济展望预测值）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdHousePriceIndexFetcher",
            canonical_model_ids=("HousePriceIndex",),
            scenario="OECD 房价指数（economy_oecd_house_price_index 域，取指数与同比读数）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdSharePriceIndexFetcher",
            canonical_model_ids=("SharePriceIndex",),
            scenario="OECD 股票价格指数（economy_oecd_share_price_index 域，取成员国股价指数）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.iter2",
            "OecdUnemploymentFetcher",
            canonical_model_ids=("Unemployment",),
            scenario="OECD 失业率（economy_oecd_unemployment 域，按性别与年龄取失业率观察值）",
        ),
    ),
)
