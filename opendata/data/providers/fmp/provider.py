"""Provider metadata and lazy bindings for stable FMP equity price models."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="fmp",
    name="Financial Modeling Prep",
    description="Stable historical equity prices and single-symbol quotes.",
    website="https://financialmodelingprep.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.fmp.models.equity_historical",
            "EquityHistoricalFetcher",
            canonical_model_ids=("EquityHistorical",),
            scenario="保留FMP原生价格调整说明的日线研究",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.fmp.models.equity_quote",
            "EquityQuoteFetcher",
            canonical_model_ids=("EquityQuote",),
            scenario="当前报价查询，不提供历史快照承诺",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="FMP_API_KEY",
            required_for_health=True,
        ),
    ),
)
