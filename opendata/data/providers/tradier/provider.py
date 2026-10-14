"""Provider metadata and lazy bindings for Tradier's five request/response models."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="tradier",
    name="Tradier",
    description=(
        "Clean-room request/response adapters for stock and ETF history, equity quotes, "
        "security search, and option chains. Historical prices retain Tradier's caveat that "
        "dividend adjustment may be absent and split adjustment is unspecified."
    ),
    website="https://tradier.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.tradier.models.equity_historical",
            "TradierEquityHistoricalFetcher",
            canonical_model_ids=("EquityHistorical",),
            scenario="带显式日期窗口的美股历史日线、周线或月线",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tradier.models.etf_historical",
            "TradierEtfHistoricalFetcher",
            canonical_model_ids=("EtfHistorical",),
            scenario="先核验ETF类型再读取显式日期窗口历史价格",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tradier.models.equity_quote",
            "TradierEquityQuoteFetcher",
            canonical_model_ids=("EquityQuote",),
            scenario="当前美股报价快照，不接受历史日期窗口",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tradier.models.equity_search",
            "TradierEquitySearchFetcher",
            canonical_model_ids=("EquitySearch",),
            scenario="按部分代码或公司名称检索证券目录",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.tradier.models.options_chains",
            "TradierOptionsChainsFetcher",
            canonical_model_ids=("OptionsChains",),
            scenario="按标的和到期日读取有界期权链，可选Greeks",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="token",
            environment_variable="TRADIER_TOKEN",
            required_for_health=True,
        ),
    ),
)
