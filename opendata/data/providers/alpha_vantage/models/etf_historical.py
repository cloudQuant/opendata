"""Alpha Vantage ETF history fetcher."""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.models.alpha_vantage import AlphaVantageEtfHistorical
from opendata.data.providers.alpha_vantage.models._historical import (
    AlphaVantageHistoricalFetcher,
)


class AlphaVantageEtfHistoricalFetcher(AlphaVantageHistoricalFetcher[AlphaVantageEtfHistorical]):
    """Fetch one explicitly requested ETF symbol's historical bars."""

    capability = Capability(
        asset_class="etf",
        domain="alpha_vantage_etf_historical",
        period="historical",
        market="global",
        source="alpha_vantage",
        verified=False,
        notes="Offline fixtures only; provider identity and live semantics are unverified.",
    )
    canonical_model = "EtfHistorical"
    row_model: ClassVar[type[AlphaVantageEtfHistorical]] = AlphaVantageEtfHistorical
    row_type = AlphaVantageEtfHistorical
    domain = "alpha_vantage_etf_historical"
    asset_class = "etf"
