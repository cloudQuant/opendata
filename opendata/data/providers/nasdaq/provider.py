"""Provider descriptor for the clean-room Nasdaq response adapters."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="nasdaq",
    name="Nasdaq",
    description=(
        "Typed Nasdaq calendars, company data, security search, stock screening, dividends, "
        "and the Nasdaq Data Link RTAT10 table."
    ),
    website="https://www.nasdaq.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.calendar_dividend",
            "CalendarDividendFetcher",
            ("CalendarDividend",),
            "US-listed dividend events within a bounded calendar window",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.calendar_earnings",
            "CalendarEarningsFetcher",
            ("CalendarEarnings",),
            "US-listed earnings dates and reported consensus fields",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.calendar_ipo",
            "CalendarIpoFetcher",
            ("CalendarIpo",),
            "Monthly IPO and secondary offering calendar by deal status",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.company_filings",
            "CompanyFilingsFetcher",
            ("CompanyFilings",),
            "Bounded company filing lookup with explicit offset pagination",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.economic_calendar",
            "EconomicCalendarFetcher",
            ("EconomicCalendar",),
            "Weekday economic calendar with country filtering",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.equity_search",
            "EquitySearchFetcher",
            ("EquitySearch",),
            "Nasdaq Trader symbol directory lookup",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.equity_screener",
            "EquityScreenerFetcher",
            ("EquityScreener",),
            "US stock screener query with bounded result count",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.historical_dividends",
            "HistoricalDividendsFetcher",
            ("HistoricalDividends",),
            "Historical dividend rows for a bounded set of symbols",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.nasdaq.models.top_retail",
            "TopRetailFetcher",
            ("TopRetail",),
            "Daily top-ten retail activity table from Nasdaq Data Link",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="data_link_api_key",
            environment_variable="NASDAQ_DATA_LINK_API_KEY",
            required_for_health=False,
        ),
    ),
)
