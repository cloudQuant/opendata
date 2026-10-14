"""Provider metadata and lazy bindings for selected public FINRA datasets."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="finra",
    name="FINRA",
    description=(
        "Clean-room adapters for FINRA OTC weekly aggregates and consolidated short interest."
    ),
    website="https://developer.finra.org/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.finra.models.otc_aggregate",
            "OTCAggregateFetcher",
            canonical_model_ids=("OTCAggregate",),
            scenario="按证券代码和日期查询 FINRA OTC 市场交易量汇总",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.finra.models.equity_short_interest",
            "EquityShortInterestFetcher",
            canonical_model_ids=("EquityShortInterest",),
            scenario="按证券代码和结算日期查询 FINRA 公开卖空持仓统计",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="API client id",
            environment_variable="FINRA_API_CLIENT_ID",
            required_for_health=True,
        ),
        CredentialSpec(
            name="API client secret",
            environment_variable="FINRA_API_CLIENT_SECRET",
            required_for_health=True,
        ),
    ),
)
