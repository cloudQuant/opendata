"""Typed Fama/French fetchers loaded through the provider descriptor."""

from opendata.data.providers.famafrench.models.fetchers import (
    FamaFrenchBreakpointsFetcher,
    FamaFrenchCountryPortfolioReturnsFetcher,
    FamaFrenchFactorsFetcher,
    FamaFrenchInternationalIndexReturnsFetcher,
    FamaFrenchRegionalPortfolioReturnsFetcher,
    FamaFrenchUSPortfolioReturnsFetcher,
)

__all__ = [
    "FamaFrenchBreakpointsFetcher",
    "FamaFrenchCountryPortfolioReturnsFetcher",
    "FamaFrenchFactorsFetcher",
    "FamaFrenchInternationalIndexReturnsFetcher",
    "FamaFrenchRegionalPortfolioReturnsFetcher",
    "FamaFrenchUSPortfolioReturnsFetcher",
]
