"""Provider descriptor and lazy model bindings for Fama/French data."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="famafrench",
    name="Kenneth French Data Library",
    description=(
        "Public factor, breakpoint, country, index, US, and regional portfolio archives; "
        "downloaded as bounded ZIP files and parsed with source-specific contracts."
    ),
    website="https://mba.tuck.dartmouth.edu/pages/Faculty/ken.french/data_library.html",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchBreakpointsFetcher",
            canonical_model_ids=("FamaFrenchBreakpoints",),
            scenario="按数据集、频率和日期查询官方因子分位点断点",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchCountryPortfolioReturnsFetcher",
            canonical_model_ids=("FamaFrenchCountryPortfolioReturns",),
            scenario="按国家、组合类型和日期读取官方国家组合收益",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchFactorsFetcher",
            canonical_model_ids=("FamaFrenchFactors",),
            scenario="按数据集和日期查询 Ken French 因子收益序列",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchInternationalIndexReturnsFetcher",
            canonical_model_ids=("FamaFrenchInternationalIndexReturns",),
            scenario="按国际指数标识和日期读取官方指数收益",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchRegionalPortfolioReturnsFetcher",
            canonical_model_ids=("FamaFrenchRegionalPortfolioReturns",),
            scenario="按地区组合、投资组合规模和日期读取区域组合收益",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.famafrench.models.fetchers",
            "FamaFrenchUSPortfolioReturnsFetcher",
            canonical_model_ids=("FamaFrenchUSPortfolioReturns",),
            scenario="按美国投资组合类型和日期查询官方组合收益",
        ),
    ),
)
