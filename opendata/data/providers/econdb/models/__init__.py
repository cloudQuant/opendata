"""EconDB fetcher modules, one per canonical provider model."""

from opendata.data.providers.econdb.models.iter2 import (
    EconDBEconomicIndicatorsFetcher,
    EconDBGdpNominalFetcher,
    EconDBGdpRealFetcher,
    EconDBPortVolumeFetcher,
    EconDBYieldCurveFetcher,
)

__all__ = [
    "EconDBEconomicIndicatorsFetcher",
    "EconDBGdpNominalFetcher",
    "EconDBGdpRealFetcher",
    "EconDBPortVolumeFetcher",
    "EconDBYieldCurveFetcher",
]
