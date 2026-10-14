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
            scenario=(
                "EIA Weekly Petroleum Status Report（energy_petroleum_status_report 域，"
                "解析映射的官方 WPSR CSV 周度表）"
            ),
        ),
        LazyFetcherBinding(
            "opendata.data.providers.eia.models.short_term_energy_outlook",
            "ShortTermEnergyOutlookFetcher",
            canonical_model_ids=("ShortTermEnergyOutlook",),
            scenario=(
                "EIA Short-Term Energy Outlook（energy_short_term_energy_outlook 域，"
                "取 EIA API 命名序列或官方发布表）"
            ),
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
