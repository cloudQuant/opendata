"""Provider metadata and lazy bindings for the IMF adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="imf",
    name="IMF",
    description="IMF DataMapper adapter for international macroeconomic series.",
    website="https://www.imf.org/en/Data",
    fetcher_bindings=(
        LazyFetcherBinding("opendata.data.providers.imf.models.cpi", "ImfCpiFetcher"),
        LazyFetcherBinding("opendata.data.providers.imf.models.gdp", "ImfGdpFetcher"),
        LazyFetcherBinding(
            "opendata.data.providers.imf.models.unemployment", "ImfUnemploymentFetcher"
        ),
    ),
)
