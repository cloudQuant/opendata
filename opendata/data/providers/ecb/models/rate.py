"""Interest rate fetcher (domain ``economy_rate``, contract ``MacroSeries``).

The generic pipeline lives on :class:`EcbSeriesFetcher`; this module
only declares the capability. The FM key family is daily, event-shaped
(rate-change dates carry values), so the period axis is ``1D``.
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.ecb._source import SOURCE
from opendata.data.providers.ecb.models._series import EcbSeriesFetcher


class EcbRateFetcher(EcbSeriesFetcher):
    """ECB key interest rates (for example the MRO ``MRR_FR.LEV``)."""

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_rate",
        period="1D",
        market="eu",
        source=SOURCE,
        verified=True,
    )
