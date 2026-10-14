"""Provider metadata and lazy binding for the Trading Economics calendar."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="tradingeconomics",
    name="Trading Economics",
    description=(
        "Clean-room adapter for the documented Economic Calendar REST endpoints, including "
        "country, group, date-window, importance, and calendar-ID routes."
    ),
    website="https://tradingeconomics.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.tradingeconomics.models.economic_calendar",
            "TradingEconomicsEconomicCalendarFetcher",
            canonical_model_ids=("EconomicCalendar",),
            scenario=(
                "经济事件日历：按国家、分组、重要度、日期窗或 Trading Economics calendar ID 查询"
            ),
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="TRADINGECONOMICS_API_KEY",
        ),
    ),
)
