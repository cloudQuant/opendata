"""GDP fetcher (domain ``economy_gdp``, contract ``MacroSeries``).

The generic pipeline lives on :class:`ImfIndicatorFetcher`; this module
only declares the capability (indicator ``NGDP_RPCH``, annual real GDP
growth).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models._indicator import ImfIndicatorFetcher


class ImfGdpFetcher(ImfIndicatorFetcher):
    """Annual real GDP growth for one country (``NGDP_RPCH``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_gdp",
        period="1A",
        market="global",
        source=SOURCE,
        verified=True,
    )
