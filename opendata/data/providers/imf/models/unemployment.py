"""Unemployment fetcher (domain ``economy_unemployment``, contract ``MacroSeries``).

The generic pipeline lives on :class:`ImfIndicatorFetcher`; this module
only declares the capability (indicator ``LUR``, annual unemployment
rate).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models._indicator import ImfIndicatorFetcher


class ImfUnemploymentFetcher(ImfIndicatorFetcher):
    """Annual unemployment rate for one country (``LUR``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_unemployment",
        period="1A",
        market="global",
        source=SOURCE,
        verified=True,
    )
