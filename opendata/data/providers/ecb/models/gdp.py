"""GDP fetcher (domain ``economy_gdp``, contract ``MacroSeries``).

The generic pipeline lives on :class:`EcbSeriesFetcher`; this module
only declares the capability. MNA series are quarterly (the key family
also drives the ``YYYY-Qn`` period granularity on the base).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.ecb._source import SOURCE
from opendata.data.providers.ecb.models._series import EcbSeriesFetcher


class EcbGdpFetcher(EcbSeriesFetcher):
    """National-account GDP observations for one MNA series key."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_gdp",
        period="1Q",
        market="eu",
        source=SOURCE,
        verified=True,
    )
