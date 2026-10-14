"""Provider metadata and lazy model bindings for Deribit's public market data."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="deribit",
    name="Deribit",
    description=(
        "Clean-room public derivatives adapter for futures candles, live futures quotes, "
        "instrument metadata, current futures curves, and bounded option-chain quotes."
    ),
    website="https://www.deribit.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.deribit.models.fetchers",
            "DeribitFuturesCurveFetcher",
            canonical_model_ids=("FuturesCurve",),
            scenario="当前期货曲线：按结算币种合并到期日目录与实时 book summary",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.deribit.models.fetchers",
            "DeribitFuturesHistoricalFetcher",
            canonical_model_ids=("FuturesHistorical",),
            scenario="期货历史蜡烛：按一个或多个 instrument、UTC 日期窗口与分辨率取 OHLCV",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.deribit.models.fetchers",
            "DeribitFuturesInfoFetcher",
            canonical_model_ids=("FuturesInfo",),
            scenario="期货实时行情：读取 Deribit ticker 的盘口与 24 小时统计",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.deribit.models.fetchers",
            "DeribitFuturesInstrumentsFetcher",
            canonical_model_ids=("FuturesInstruments",),
            scenario="期货合约目录：按结算币种读取当前或近期交割的 instruments",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.deribit.models.fetchers",
            "DeribitOptionsChainsFetcher",
            canonical_model_ids=("OptionsChains",),
            scenario="期权链快照：匹配官方合约目录、book summary 与有界逐合约 ticker",
        ),
    ),
    credentials=(),
)
