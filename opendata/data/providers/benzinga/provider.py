"""Provider metadata and lazy bindings for benzinga."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="benzinga",
    name="Benzinga",
    description=(
        "Benzinga adapters for documented analyst ratings and news responses. Requests and result "
        "sizes are bounded and every response maps to a typed contract."
    ),
    website="https://www.benzinga.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.benzinga.models.iter2",
            "BenzingaAnalystSearchFetcher",
            canonical_model_ids=("AnalystSearch",),
            scenario="Analyst directory with ratings accuracy metrics",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.benzinga.models.iter2",
            "BenzingaCompanyNewsFetcher",
            canonical_model_ids=("CompanyNews",),
            scenario="News feed filtered by ticker symbols",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.benzinga.models.iter2",
            "BenzingaWorldNewsFetcher",
            canonical_model_ids=("WorldNews",),
            scenario="Unfiltered news feed",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.benzinga.models.price_target",
            "BenzingaPriceTargetFetcher",
            canonical_model_ids=("PriceTarget",),
            scenario="目标价评级：按代码与评级动作取回 benzinga 分析师目标价日历单页",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="BENZINGA_API_KEY",
        ),
    ),
)
