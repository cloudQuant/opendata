"""Provider metadata and lazy bindings for public Multpl pages."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="multpl",
    name="Multpl",
    description=(
        "Public S&P 500 valuation tables. SP500Multiples reads the visible Date/Value HTML "
        "tables for five listed metrics, preserving the displayed date, unit, and estimate marker."
    ),
    website="https://www.multpl.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.multpl.models.sp500_multiples",
            "SP500MultiplesFetcher",
            canonical_model_ids=("SP500Multiples",),
            scenario="S&P 500 多估值历史：按公开表格中的估值指标和日期返回估算标记",
        ),
    ),
    credentials=(),
)
