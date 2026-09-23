"""GDP fetcher (domain ``economy_gdp``, contract ``MacroSeries``).

The generic pipeline lives on :class:`FredSeriesFetcher`; this module
only declares the capability (the GDPC1 family is quarterly).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.fred._source import SOURCE
from opendata.data.providers.fred.models._series import FredSeriesFetcher


class FredGdpFetcher(FredSeriesFetcher):
    """Real GDP observations for one FRED series (for example ``GDPC1``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_gdp",
        period="1Q",
        market="us",
        source=SOURCE,
        verified=False,
    )
