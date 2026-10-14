"""Alpha Vantage stock history fetcher."""

from opendata.data.capability import Capability
from opendata.data.models.alpha_vantage import AlphaVantageEquityHistorical
from opendata.data.providers.alpha_vantage.models._historical import (
    AlphaVantageHistoricalFetcher,
)


class AlphaVantageEquityHistoricalFetcher(
    AlphaVantageHistoricalFetcher[AlphaVantageEquityHistorical]
):
    """Fetch one explicitly requested equity symbol's historical bars."""

    capability = Capability(
        asset_class="equity",
        domain="alpha_vantage_equity_historical",
        period="historical",
        market="global",
        source="alpha_vantage",
        verified=False,
        notes="Offline fixtures only; provider identity and live semantics are unverified.",
    )
    canonical_model = "EquityHistorical"
    row_type = AlphaVantageEquityHistorical
    domain = "alpha_vantage_equity_historical"
    asset_class = "equity"
