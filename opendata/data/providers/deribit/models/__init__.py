"""Lazy model bindings for the Deribit provider."""

from opendata.data.providers.deribit.models.fetchers import (
    DeribitFuturesCurveFetcher,
    DeribitFuturesHistoricalFetcher,
    DeribitFuturesInfoFetcher,
    DeribitFuturesInstrumentsFetcher,
    DeribitOptionsChainsFetcher,
)

__all__ = [
    "DeribitFuturesCurveFetcher",
    "DeribitFuturesHistoricalFetcher",
    "DeribitFuturesInfoFetcher",
    "DeribitFuturesInstrumentsFetcher",
    "DeribitOptionsChainsFetcher",
]
