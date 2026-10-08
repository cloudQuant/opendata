"""Provider metadata and lazy bindings for the akshare adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="akshare",
    name="AKShare",
    description="Self-developed adapter over the bundled AkShare-compatible source tree.",
    website="https://akshare.akfamily.xyz/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.stock_daily", "AkshareStockDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.futures_daily", "AkshareFuturesDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.index_daily", "AkshareIndexDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.fund_etf_daily", "AkshareFundEtfDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.option_daily", "AkshareOptionDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.bond_daily", "AkshareBondDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.stock_action", "AkshareStockActionFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.financial_statement",
            "AkshareFinancialStatementFetcher",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.financial_indicator",
            "AkshareFinancialIndicatorFetcher",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.index_constituent",
            "AkshareIndexConstituentFetcher",
        ),
    ),
)
