"""Provider metadata and lazy bindings for cboe."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="cboe",
    name="Cboe",
    description=(
        "Public delayed index-directory adapters plus typed Cboe LiveVol All Access adapters. "
        "All Access needs a separately provisioned bearer token; the local binding does not "
        "establish data-use or redistribution rights."
    ),
    website="https://www.cboe.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.available_indices",
            "CboeAvailableIndicesFetcher",
            canonical_model_ids=("AvailableIndices",),
            scenario="指数目录发现：列出 cboe 发布的全部指数及其计算时段与延迟",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.index_search",
            "CboeIndexSearchFetcher",
            canonical_model_ids=("IndexSearch",),
            scenario="指数检索：在 cboe 美国指数目录中按代码或名称与描述筛选指数",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.index_historical_public",
            "CboeIndexHistoricalFetcher",
            canonical_model_ids=("IndexHistorical",),
            scenario="指数日线：读取 Cboe 官方历史页明确链接的波动率指数 OHLC CSV",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.index_constituents",
            "CboeIndexConstituentsFetcher",
            canonical_model_ids=("IndexConstituents",),
            scenario="成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.all_access",
            "CboeEquitySearchFetcher",
            canonical_model_ids=("EquitySearch",),
            scenario="股票目录检索：按代码或公司名筛选 All Access 官方证券目录",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.all_access",
            "CboeEquityHistoricalFetcher",
            canonical_model_ids=("EquityHistorical",),
            scenario="股票日线：查询有界历史窗口内 Cboe 官方日终 OHLCV 快照",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.all_access",
            "CboeEtfHistoricalFetcher",
            canonical_model_ids=("EtfHistorical",),
            scenario="ETF 日线：使用 All Access underlying 日终快照",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.all_access",
            "CboeEquityQuoteFetcher",
            canonical_model_ids=("EquityQuote",),
            scenario="股票行情快照：查询单个代码的 Cboe 延迟报价字段",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.all_access",
            "CboeOptionsChainsFetcher",
            canonical_model_ids=("OptionsChains",),
            scenario="期权链快照：查询 Cboe 同步标的和期权报价及希腊值",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="all_access_access_token",
            environment_variable="CBOE_LIVEVOL_ACCESS_TOKEN",
            required_for_health=False,
        ),
    ),
)
