"""EIA provider metadata and lazy fetcher bindings."""

from opendata.data.provider import CredentialSpec, LazyFetcherBinding, Provider

PROVIDER = Provider(
    source="eia",
    name="U.S. Energy Information Administration",
    description=(
        "Public weekly petroleum report tables and key-protected STEO series observations."
    ),
    website="https://www.eia.gov/",
    fetcher_bindings=(
        LazyFetcherBinding(
            "opendata.data.providers.eia.models.petroleum_status_report",
            "PetroleumStatusReportFetcher",
            canonical_model_ids=("PetroleumStatusReport",),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.eia.models.short_term_energy_outlook",
            "ShortTermEnergyOutlookFetcher",
            canonical_model_ids=("ShortTermEnergyOutlook",),
        ),
    ),
    credentials=(
        CredentialSpec(
            name="EIA API key (required for STEO API queries)",
            environment_variable="EIA_API_KEY",
            required_for_health=False,
        ),
    ),
)
