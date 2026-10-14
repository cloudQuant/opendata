"""Alpha Vantage fetchers for explicitly bound canonical models."""

from opendata.data.providers.alpha_vantage.models.equity_historical import (
    AlphaVantageEquityHistoricalFetcher,
)
from opendata.data.providers.alpha_vantage.models.etf_historical import (
    AlphaVantageEtfHistoricalFetcher,
)
from opendata.data.providers.alpha_vantage.models.historical_eps import (
    AlphaVantageHistoricalEpsFetcher,
)

__all__ = [
    "AlphaVantageEquityHistoricalFetcher",
    "AlphaVantageEtfHistoricalFetcher",
    "AlphaVantageHistoricalEpsFetcher",
]
