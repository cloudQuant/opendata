"""Explicit provider-to-canonical-model fetcher bindings."""

from opendata.data.provider import LazyFetcherBinding

FETCHER_BINDINGS = (
    LazyFetcherBinding(
        "opendata.data.providers.alpha_vantage.models.equity_historical",
        "AlphaVantageEquityHistoricalFetcher",
        ("EquityHistorical",),
        scenario="用于按股票代码分析区间内的历史行情，支持日、周、月及原生分钟级 OHLCV 序列。",
    ),
    LazyFetcherBinding(
        "opendata.data.providers.alpha_vantage.models.etf_historical",
        "AlphaVantageEtfHistoricalFetcher",
        ("EtfHistorical",),
        scenario="用于按 ETF 代码分析基金历史价格与成交量，支持日、周、月及原生分钟级 OHLCV 序列。",
    ),
    LazyFetcherBinding(
        "opendata.data.providers.alpha_vantage.models.historical_eps",
        "AlphaVantageHistoricalEpsFetcher",
        ("HistoricalEps",),
        scenario="用于追踪公司年度或季度每股收益及其相对市场预期的历史变化。",
    ),
)
