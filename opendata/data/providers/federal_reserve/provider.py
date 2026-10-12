"""Provider metadata and lazy bindings for federal_reserve."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="federal_reserve",
    name="Federal Reserve",
    description=(
        "Public Federal Reserve datadownload CSV adapter for the H.6 money stock measures, the "
        "H.15 daily Treasury yields and the Svensson fitted-factor yield curve, plus the New "
        "York Fed reference-rate JSON for the effective federal funds rate and SOFR."
    ),
    website="https://www.federalreserve.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.money_measures",
            "FederalReserveMoneyMeasuresFetcher",
            canonical_model_ids=("MoneyMeasures",),
            scenario="货币供应量月表：取出 H.6 发布表在五行元数据之后的七个货币总量序列",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.treasury_rates",
            "FederalReserveTreasuryRatesFetcher",
            canonical_model_ids=("TreasuryRates",),
            scenario="国债收益率日表：取出 H.15 发布表在五行元数据之后的十一个期限档",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.svensson_yield_curve",
            "FederalReserveSvenssonYieldCurveFetcher",
            canonical_model_ids=("SvenssonYieldCurve",),
            scenario="Svensson 拟合因子收益率曲线：宽表按序列读出并转置成一行一个日期",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.federal_funds_rate",
            "FederalReserveFederalFundsRateFetcher",
            canonical_model_ids=("FederalFundsRate",),
            scenario="联邦基金有效利率日表：取出纽约联储参考利率 JSON 的百分序列并折成小数",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.sofr",
            "FederalReserveSOFRFetcher",
            canonical_model_ids=("SOFR",),
            scenario="有担保隔夜融资利率日表：取出纽约联储 SOFR JSON 的百分序列并折成小数",
        ),
    ),
    credentials=(),
)
