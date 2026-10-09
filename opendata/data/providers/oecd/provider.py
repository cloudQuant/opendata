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
        # No engine binding: all nine ledger rows are recorded as not declarable in
        # ``opendata.data.providers.oecd.specs.NOT_DECLARABLE``, including the two this package
        # declared last round and retracted when ``declaration_provenance`` showed those
        # declarations described this repository's own clean-room reader rather than the pinned
        # upstream models. A row becomes a binding the moment a ``ModelSpec`` for it is added.
    ),
)
