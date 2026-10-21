"""Provider metadata and lazy bindings for the WSJ ETF mover tables."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="wsj",
    name="The Wall Street Journal",
    description=(
        "Public WSJ Market Data ETF mover tables (leaders, laggards, most active), read as "
        "unauthenticated JSON through the page's browser User-Agent."
    ),
    website="https://www.wsj.com",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.wsj.models.etf_movers",
            "WSJETFGainersFetcher",
            canonical_model_ids=("ETFGainers",),
            scenario="WSJ ETF leaders：公开 JSON 表中的涨幅居前 ETF 行",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.wsj.models.etf_movers",
            "WSJETFLosersFetcher",
            canonical_model_ids=("ETFLosers",),
            scenario="WSJ ETF laggards：公开 JSON 表中的跌幅居前 ETF 行",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.wsj.models.etf_movers",
            "WSJETFActiveFetcher",
            canonical_model_ids=("ETFActive",),
            scenario="WSJ ETF most active：公开 JSON 表中的成交活跃 ETF 行",
        ),
    ),
    credentials=(),
)
