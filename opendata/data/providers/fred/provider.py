"""Provider metadata and lazy bindings for FRED."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="fred",
    name="FRED",
    description="FRED REST adapter for macroeconomic series.",
    website="https://fred.stlouisfed.org/",
    fetcher_bindings=(
        LazyFetcherBinding("opendata.data.providers.fred.models.cpi", "FredCpiFetcher"),
        LazyFetcherBinding("opendata.data.providers.fred.models.gdp", "FredGdpFetcher"),
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.unemployment", "FredUnemploymentFetcher"
        ),
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.search",
            "FredSearchFetcher",
            canonical_model_ids=("FredSearch",),
            scenario="FRED序列发现与研究参数选择",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.series",
            "FredSeriesFetcher",
            canonical_model_ids=("FredSeries",),
            scenario="保留修订区间及请求变换上下文的宏观研究",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.sofr",
            "FredSofrFetcher",
            canonical_model_ids=("SOFR",),
            scenario="保留SOFR原始数值、修订区间与请求变换上下文的融资成本研究",
        ),
        LazyFetcherBinding(
            "opendata.data.providers.fred.models.sonia",
            "FredSoniaFetcher",
            canonical_model_ids=("SONIA",),
            scenario="保留SONIA七测度、原始数值与修订区间的英镑融资成本研究",
        ),
    ),
    credentials=(
        CredentialSpec(
            name="api_key",
            environment_variable="FRED_API_KEY",
            settings_attribute="fred_api_key",
            required_for_health=True,
        ),
    ),
)
