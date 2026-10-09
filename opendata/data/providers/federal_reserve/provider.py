"""Provider metadata and lazy bindings for federal_reserve."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="federal_reserve",
    name="Federal Reserve",
    description=(
        "Public Federal Reserve datadownload CSV adapter for the H.6 money stock measures and the "
        "H.15 daily Treasury yields."
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
    ),
    credentials=(),
)
