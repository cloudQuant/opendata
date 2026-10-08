"""Provider metadata and lazy bindings for the ECB adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="ecb",
    name="ECB",
    description="ECB Data Portal adapter for euro-area macroeconomic series.",
    website="https://data.ecb.europa.eu/",
    fetcher_bindings=(
        LazyFetcherBinding("opendata.data.providers.ecb.models.cpi", "EcbCpiFetcher"),
        LazyFetcherBinding("opendata.data.providers.ecb.models.gdp", "EcbGdpFetcher"),
        LazyFetcherBinding("opendata.data.providers.ecb.models.rate", "EcbRateFetcher"),
        LazyFetcherBinding(
            "opendata.data.providers.ecb.models.reference_rates",
            "EcbCurrencyReferenceRatesFetcher",
            canonical_model_ids=("CurrencyReferenceRates",),
            scenario="保留ECB源维度与属性的欧元日参考汇率研究",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ecb.models.yield_curve",
            "EcbYieldCurveFetcher",
            canonical_model_ids=("YieldCurve",),
            scenario="保留ECB七维与源属性的日度收益率曲线研究",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.ecb.models.balance_of_payments",
            "EcbBalanceOfPaymentsFetcher",
            canonical_model_ids=("BalanceOfPayments",),
            scenario="保留ECB十七维与原月季度期间的国际收支研究",
        ),
    ),
)
