"""Provider metadata and lazy bindings for cboe."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="cboe",
    name="Cboe",
    description="Public Cboe delayed-quote JSON adapter for index directory and constituents.",
    website="https://www.cboe.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.available_indices",
            "CboeAvailableIndicesFetcher",
            canonical_model_ids=("AvailableIndices",),
            scenario="指数目录发现：列出 cboe 发布的全部指数及其计算时段与延迟",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cboe.models.index_constituents",
            "CboeIndexConstituentsFetcher",
            canonical_model_ids=("IndexConstituents",),
            scenario="成分股快照：取出一只欧洲指数在源端发布的全部成分及其盘中报价",
        ),
    ),
    credentials=(),
)
