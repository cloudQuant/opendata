"""Provider metadata and lazy bindings for yfinance."""

from opendata.data.provider import (
    LazyFetcherBinding,
    OptionalDependencySpec,
    Provider,
)

PROVIDER = Provider(
    source="yfinance",
    name="Yahoo Finance",
    description="Optional SDK adapter for overseas daily equity prices.",
    website="https://finance.yahoo.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.yfinance.models.stock_daily", "YfinanceStockDailyFetcher"
        ),
    ),
    optional_dependencies=(
        OptionalDependencySpec(
            distribution="yfinance",
            import_name="yfinance",
            required_for_health=True,
        ),
    ),
)
