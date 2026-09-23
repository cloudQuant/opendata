"""Unemployment fetcher (domain ``economy_unemployment``, contract ``MacroSeries``).

The generic pipeline lives on :class:`FredSeriesFetcher`; this module
only declares the capability (the UNRATE family is monthly).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.fred._source import SOURCE
from opendata.data.providers.fred.models._series import FredSeriesFetcher


class FredUnemploymentFetcher(FredSeriesFetcher):
    """Unemployment rate observations for one FRED series (``UNRATE``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_unemployment",
        period="1M",
        market="us",
        source=SOURCE,
        verified=False,
    )
