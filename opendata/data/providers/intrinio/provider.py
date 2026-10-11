"""Provider metadata and lazy bindings for intrinio."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="intrinio",
    name="Intrinio",
    description=(
        "Declared Intrinio Web API v2 JSON adapter for "
        "the forex pair directory, the company search, "
        "one security's dividend/split adjustment feed "
        "and the IPO calendar."
    ),
    website="https://intrinio.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.intrinio.models.currency_pairs",
            "IntrinioCurrencyPairsFetcher",
            canonical_model_ids=("CurrencyPairs",),
            scenario="外汇对目录：列出 intrinio 发布的全部可交易货币对并按币种代码子串筛选",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.intrinio.models.equity_search",
            "IntrinioEquitySearchFetcher",
            canonical_model_ids=("EquitySearch",),
            scenario="公司检索：在 intrinio 美股公司库中检索并返回改名后的 symbol 与 intrinio_id",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.intrinio.models.historical_dividends",
            "IntrinioHistoricalDividendsFetcher",
            canonical_model_ids=("HistoricalDividends",),
            scenario="分红拆股：按证券代码取回该证券的除息日分红与拆股调整序列",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.intrinio.models.calendar_ipo",
            "IntrinioCalendarIpoFetcher",
            canonical_model_ids=("CalendarIpo",),
            scenario="IPO日历：按状态与发行规模取回新股发行日历并展平嵌套公司证券字段",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="INTRINIO_API_KEY",
        ),
    ),
)
