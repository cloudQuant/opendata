"""Provider metadata and lazy bindings for the THS/Fuyao adapter."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="ths",
    name="THS",
    description="Self-developed Fuyao adapter for verified domestic market data.",
    website="https://www.10jqka.com.cn/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.stock_daily",
            "ThsStockDailyFetcher",
            canonical_model_ids=("stock_daily",),
            scenario="行情日线（回测数据准备 / dwd 日线域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.stock_action",
            "ThsStockActionFetcher",
            canonical_model_ids=("stock_action",),
            scenario="除权除息事件（dwd 事件域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.index_daily",
            "ThsIndexDailyFetcher",
            canonical_model_ids=("index_daily",),
            scenario="指数日线（业绩基准与指数增强回测 / dwd 指数域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.index_constituent",
            "ThsIndexConstituentFetcher",
            canonical_model_ids=("index_constituent",),
            scenario="指数成分股（dwd 指数域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.financial_statement",
            "ThsFinancialStatementFetcher",
            canonical_model_ids=("financial_statement",),
            scenario="财务报表（dwd 财务域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.fund_action",
            "ThsFundActionFetcher",
            canonical_model_ids=("fund_action",),
            scenario="基金分红事件（ETF 前复权序列换回不复权的因子来源，D10 前置件）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.fund_etf_daily",
            "ThsFundEtfDailyFetcher",
            canonical_model_ids=("fund_etf_daily",),
            scenario="ETF 日线（场内基金回测数据准备 / dwd ETF 域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.instrument",
            "ThsInstrumentFetcher",
            canonical_model_ids=("instrument",),
            scenario="标的目录（上市区间谓词：空窗是「未上市/已退市」还是丢数的判据；入库前代码消歧）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.futures_daily",
            "ThsFuturesDailyFetcher",
            canonical_model_ids=("futures_daily",),
            scenario="期货日线（期货回测数据准备 / dwd 期货域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.option_daily",
            "ThsOptionDailyFetcher",
            canonical_model_ids=("option_daily",),
            scenario="期权合约日线（期权策略回测数据准备 / dwd 期权域）",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ths.models.trading_calendar",
            "ThsTradingCalendarFetcher",
            canonical_model_ids=("trading_calendar",),
            scenario="交易日历（跑批窗口端点与新鲜度 lag 的期望判据，A4.7）",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="fuyao_api_key",
            environment_variable="FUYAO_API_KEY",
            settings_attribute="fuyao_api_key",
            required_for_health=True,
        ),
    ),
)
