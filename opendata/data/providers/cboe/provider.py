"""Provider metadata and lazy bindings for cboe."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="cboe",
    name="Cboe",
    description=(
        "Public Cboe delayed-quote JSON adapter for the index directory, its search and its "
        "constituents."
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
            "opendata.data.providers.cboe.models.index_constituents",
            "CboeIndexConstituentsFetcher",
            canonical_model_ids=("IndexConstituents",),
            scenario="成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.equity_search",
            "CboeEquitySearchFetcher",
            canonical_model_ids=("EquitySearch",),
            scenario="股票目录检索：下载 cboe 期权公司目录 CSV 并按代码或公司名筛选上市公司",
        ),
    ),
    credentials=(),
)
