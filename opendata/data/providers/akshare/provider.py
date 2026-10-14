"""Provider metadata and lazy bindings for the akshare adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="akshare",
    name="AKShare",
    description="Self-developed adapter over the bundled AkShare-compatible source tree.",
    website="https://akshare.akfamily.xyz/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.stock_daily",
            "AkshareStockDailyFetcher",
            scenario="行情日线（回测数据准备 / dwd 日线域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.futures_daily",
            "AkshareFuturesDailyFetcher",
            scenario="期货日线（期货回测数据准备 / dwd 期货域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.index_daily",
            "AkshareIndexDailyFetcher",
            scenario="指数日线（业绩基准与指数增强回测 / dwd 指数域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.fund_etf_daily",
            "AkshareFundEtfDailyFetcher",
            scenario="ETF 日线（场内基金回测数据准备 / dwd ETF 域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.option_daily",
            "AkshareOptionDailyFetcher",
            scenario="期权合约日线（期权策略回测数据准备 / dwd 期权域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.bond_daily",
            "AkshareBondDailyFetcher",
            scenario="可转债日线（转债轮动策略数据准备 / dwd 债券域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.stock_action",
            "AkshareStockActionFetcher",
            scenario="除权除息事件（dwd 事件域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.financial_statement",
            "AkshareFinancialStatementFetcher",
            scenario="财务报表（dwd 财务域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.financial_indicator",
            "AkshareFinancialIndicatorFetcher",
            scenario="财务指标（dwd 财务域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.akshare.models.index_constituent",
            "AkshareIndexConstituentFetcher",
            scenario="指数成分股（dwd 指数域）",
        ),
    ),
)
