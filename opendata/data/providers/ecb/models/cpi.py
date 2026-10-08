"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

The generic pipeline lives on :class:`EcbSeriesFetcher`; this module
only declares the capability (HICP monthly, euro area).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.ecb._source import SOURCE
from opendata.data.providers.ecb.models._series import EcbSeriesFetcher


class EcbCpiFetcher(EcbSeriesFetcher):
    """HICP observations for one ECB series (for example the euro-area ANR)."""

    async_mode = "bounded_thread"

    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1M",
        market="eu",
        source=SOURCE,
        verified=True,
    )
