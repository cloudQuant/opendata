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
        ),
        LazyFetcherBinding(
            "opendata.data.providers.cftc.models.cot_search",
            "COTSearchFetcher",
            canonical_model_ids=("COTSearch",),
        ),
    ),
)
