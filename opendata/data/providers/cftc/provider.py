"""CFTC provider metadata and lazy fetcher bindings."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="cftc",
    name="Commodity Futures Trading Commission",
    description=(
        "Public Reporting Environment adapters for Commitments of Traders reports and the "
        "current contract-market directory."
    ),
    website="https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.cftc.models.cot",
            "COTFetcher",
            canonical_model_ids=("COT",),
            scenario=(
                "CFTC Commitments of Traders（cftc_commitment_of_traders 域，"
                "经 Socrata 公共 API 分页归一 COT 数据集）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cftc.models.cot_search",
            "COTSearchFetcher",
            canonical_model_ids=("COTSearch",),
            scenario=(
                "CFTC COT Market Directory（cftc_market_directory 域，"
                "查询最近报告周活跃的市场代码与名称）"
            ),
        ),
    ),
)
