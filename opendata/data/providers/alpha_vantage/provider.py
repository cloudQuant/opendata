"""Alpha Vantage provider descriptor for the three Iteration 2 models."""

from opendata.data.provider import CredentialSpec, Provider
from opendata.data.providers.alpha_vantage.bindings import FETCHER_BINDINGS

PROVIDER = Provider(
    source="alpha_vantage",
    name="Alpha Vantage",
    description="Equity and ETF historical prices plus historical EPS.",
    fetcher_bindings=FETCHER_BINDINGS,
    credentials=(
        CredentialSpec(
            name="API key",
            environment_variable="ALPHA_VANTAGE_API_KEY",
            required_for_health=True,
        ),
    ),
    optional_dependencies=(),
    website="https://www.alphavantage.co/",
)
