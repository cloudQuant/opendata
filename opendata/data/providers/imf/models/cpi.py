"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

The generic pipeline lives on :class:`ImfIndicatorFetcher`; this module
only declares the capability (indicator ``PCPIPCH``).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.imf._source import SOURCE
from opendata.data.providers.imf.models._indicator import ImfIndicatorFetcher


class ImfCpiFetcher(ImfIndicatorFetcher):
    """Annual CPI observations for one country (``PCPIPCH``)."""

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1A",
        market="global",
        source=SOURCE,
        verified=True,
    )
