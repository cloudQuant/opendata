"""Provider metadata and lazy bindings for federal_reserve."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="federal_reserve",
    name="Federal Reserve",
    description="Public Federal Reserve Board and regional Federal Reserve data adapters.",
    website="https://www.federalreserve.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_rates",
            "MoneyMeasuresFetcher",
            canonical_model_ids=("MoneyMeasures",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_rates",
            "TreasuryRatesFetcher",
            canonical_model_ids=("TreasuryRates",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_rates",
            "SvenssonYieldCurveFetcher",
            canonical_model_ids=("SvenssonYieldCurve",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_rates",
            "FederalFundsRateFetcher",
            canonical_model_ids=("FederalFundsRate",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_rates",
            "OvernightBankFundingRateFetcher",
            canonical_model_ids=("OvernightBankFundingRate",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_nyfed",
            "SOFRFetcher",
            canonical_model_ids=("SOFR",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_nyfed",
            "PrimaryDealerFailsFetcher",
            canonical_model_ids=("PrimaryDealerFails",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_nyfed",
            "PrimaryDealerPositioningFetcher",
            canonical_model_ids=("PrimaryDealerPositioning",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_economy",
            "CentralBankHoldingsFetcher",
            canonical_model_ids=("CentralBankHoldings",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_economy",
            "InflationExpectationsFetcher",
            canonical_model_ids=("InflationExpectations",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_economy",
            "TotalFactorProductivityFetcher",
            canonical_model_ids=("TotalFactorProductivity",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_documents",
            "YieldCurveFetcher",
            canonical_model_ids=("YieldCurve",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.federal_reserve.models.clean_documents",
            "FomcDocumentsFetcher",
            canonical_model_ids=("FomcDocuments",),
        ),
    ),
    credentials=(),
)
