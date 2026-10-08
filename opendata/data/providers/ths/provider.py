"""Provider metadata and lazy bindings for the THS/Fuyao adapter."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="ths",
    name="THS",
    description="Self-developed Fuyao adapter for verified domestic market data.",
    website="https://www.10jqka.com.cn/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.stock_daily", "ThsStockDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.stock_action", "ThsStockActionFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.index_daily", "ThsIndexDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.index_constituent", "ThsIndexConstituentFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.financial_statement",
            "ThsFinancialStatementFetcher",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.fund_action", "ThsFundActionFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.fund_etf_daily", "ThsFundEtfDailyFetcher"
        ),
        LazyFetcherBinding("opendata.data.providers.ths.models.instrument", "ThsInstrumentFetcher"),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.futures_daily", "ThsFuturesDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.option_daily", "ThsOptionDailyFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.trading_calendar", "ThsTradingCalendarFetcher"
        ),
    ),
    credentials=(
        CredentialSpec(
            name="fuyao_api_key",
            environment_variable="FUYAO_API_KEY",
            settings_attribute="fuyao_api_key",
            required_for_health=True,
        ),
    ),
)
