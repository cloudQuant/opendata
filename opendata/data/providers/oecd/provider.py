"""Provider metadata and lazy bindings for the OECD adapter."""

from opendata.data.provider import LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="oecd",
    name="OECD",
    description="OECD SDMX adapter for macroeconomic series.",
    website="https://data-explorer.oecd.org/",
    fetcher_bindings=(
        LazyFetcherBinding("opendata.data.providers.oecd.models.cpi", "OecdCpiFetcher"),
        LazyFetcherBinding(
            "opendata.data.providers.oecd.models.unemployment", "OecdUnemploymentFetcher"
        ),
    ),
)
