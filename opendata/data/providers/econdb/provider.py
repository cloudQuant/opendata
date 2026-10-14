"""Provider metadata and lazy bindings for econdb."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="econdb",
    name="EconDB",
    description=(
        "EconDB adapter for the indicator roster plus documented v1 macro series and port traffic. "
        "Five additional fixed models use the current series, port roster, container-traffic, and "
        "dwell-time APIs; country profile and merchandise export destinations remain unsupported "
        "because current documentation does not provide those metric mappings."
    ),
    website="https://econdb.com/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.available_indicators",
            "EcondbAvailableIndicatorsFetcher",
            canonical_model_ids=("AvailableIndicators",),
            scenario=(
                "指标目录名册：取出 econdb 静态 main_tickers.csv，剔除 "
                "OECD 旧块并按最新更新日期倒序"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.iter2",
            "EconDBEconomicIndicatorsFetcher",
            canonical_model_ids=("EconomicIndicators",),
            scenario="宏观指标：读取一个 EconDB v1 series 的官方日期和值数组并保留原始单位",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.iter2",
            "EconDBGdpNominalFetcher",
            canonical_model_ids=("GdpNominal",),
            scenario="名义 GDP：验证当前价格 GDP series 元数据并计算可对齐的期间增长率",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.iter2",
            "EconDBGdpRealFetcher",
            canonical_model_ids=("GdpReal",),
            scenario="实际 GDP：验证实际/链量 GDP series 元数据并计算可对齐的期间增长率",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.iter2",
            "EconDBPortVolumeFetcher",
            canonical_model_ids=("PortVolume",),
            scenario="港口量：按 locode 合并官方 TEU 流量与堆场 dwell time 序列",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.econdb.models.iter2",
            "EconDBYieldCurveFetcher",
            canonical_model_ids=("YieldCurve",),
            scenario="收益率曲线：按显式限定的 series ID 与到期标签组合共同时点曲线",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="API token",
            environment_variable="ECONDB_API_KEY",
            required_for_health=True,
        ),
    ),
)
