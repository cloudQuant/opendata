"""Provider descriptor for Finviz's public HTML models."""

from opendata.data.provider import LazyFetcherBinding, Provider

_FETCHER_MODULE = "opendata.data.providers.finviz.models.fetchers"
_PERFORMANCE_FETCHER_MODULE = "opendata.data.providers.finviz.models.price_performance"

PROVIDER = Provider(
    source="finviz",
    name="Finviz",
    description=(
        "Public Finviz HTML adapter for sector comparisons, screener pages, company profiles, "
        "key metrics, performance, and visible analyst price-target actions."
    ),
    website="https://finviz.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            _FETCHER_MODULE,
            "FinvizEquityScreenerFetcher",
            canonical_model_ids=("EquityScreener",),
            scenario=(
                "匿名、未筛选的 Finviz 美股 Screener Overview 第一页；最多 20 行、11 字段"
                "子集，非完整 61 字段结果"
            ),
        ),
        LazyFetcherBinding(
            _FETCHER_MODULE,
            "FinvizCompareGroupsFetcher",
            canonical_model_ids=("CompareGroups",),
            scenario="板块比较：查看 Finviz 公开行业板块概览或表现表",
        ),
        LazyFetcherBinding(
            _FETCHER_MODULE,
            "FinvizEquityInfoFetcher",
            canonical_model_ids=("EquityInfo",),
            scenario="公司资料：查询 Finviz 公开股票页中的公司身份与基础资料",
        ),
        LazyFetcherBinding(
            _FETCHER_MODULE,
            "FinvizKeyMetricsFetcher",
            canonical_model_ids=("KeyMetrics",),
            scenario="股票估值与基本指标：查询 Finviz 公开股票页显示的关键指标",
        ),
        LazyFetcherBinding(
            _FETCHER_MODULE,
            "FinvizPriceTargetFetcher",
            canonical_model_ids=("PriceTarget",),
            scenario="分析师目标价事件：读取 Finviz 股票页当前显示的评级与目标价变动",
        ),
        LazyFetcherBinding(
            _PERFORMANCE_FETCHER_MODULE,
            "FinvizPricePerformanceFetcher",
            canonical_model_ids=("PricePerformance",),
        ),
        LazyFetcherBinding(
            _PERFORMANCE_FETCHER_MODULE,
            "FinvizEtfPricePerformanceFetcher",
            canonical_model_ids=("EtfPricePerformance",),
        ),
    ),
    credentials=(),
    optional_dependencies=(),
)
