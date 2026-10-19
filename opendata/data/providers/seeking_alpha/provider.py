"""Provider descriptor for the clean-room Seeking Alpha public-route adapters."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="seeking_alpha",
    name="Seeking Alpha",
    description=(
        "Typed adapters over Seeking Alpha's public unauthenticated JSON:API routes. The calendar "
        "route is read per requested day and followed through its own page cursor; no credential, "
        "session cookie, or account login is used or required. The local binding does not "
        "establish data-use or redistribution rights."
    ),
    website="https://seekingalpha.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.seeking_alpha.models.calendar_earnings_public",
            "SeekingAlphaCalendarEarningsFetcher",
            canonical_model_ids=("CalendarEarnings",),
            scenario="财报日历：按日读取 Seeking Alpha 公开的 earnings_calendar/tickers 分页 JSON",
        ),
    ),
    credentials=(),
)
