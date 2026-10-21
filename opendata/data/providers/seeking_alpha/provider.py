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
        LazyFetcherBinding(
            "opendata.data.providers.seeking_alpha.models.estimates_public",
            "SeekingAlphaForwardEpsEstimatesFetcher",
            canonical_model_ids=("ForwardEpsEstimates",),
            scenario=(
                "前瞻 EPS 预测：先用公开 searches 路由解析站点内 id，再读 symbol_data/estimates "
                "的 per-item 序列并转置成每期一行（seeking_alpha_forward_eps_estimates 域）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.seeking_alpha.models.estimates_public",
            "SeekingAlphaForwardSalesEstimatesFetcher",
            canonical_model_ids=("ForwardSalesEstimates",),
            scenario=(
                "前瞻营收预测：同一两次请求通路，读取五个 revenue_* 数据项"
                "（seeking_alpha_forward_sales_estimates 域）"
            ),
        ),
    ),
    credentials=(),
)
