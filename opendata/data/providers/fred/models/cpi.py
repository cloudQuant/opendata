"""CPI fetcher (domain ``economy_cpi``, contract ``MacroSeries``).

The generic pipeline lives on :class:`FredSeriesFetcher`; this module
only declares the capability (the CPIAUCSL family is monthly).
"""

from typing import ClassVar

from opendata.data.capability import Capability
from opendata.data.providers.fred._source import SOURCE
from opendata.data.providers.fred.models._series import FredSeriesFetcher


class FredCpiFetcher(FredSeriesFetcher):
    """CPI observations for one FRED series (for example ``CPIAUCSL``)."""

    async_mode = "bounded_thread"

    #: Verified against 80 official CPIAUCSL CSV observations (date/value, rtol=1e-9).
    #: Evidence: docs/evidence/C65/fred-official-comparison.json and .txt.
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="economy_cpi",
        period="1M",
        market="us",
        source=SOURCE,
        verified=True,
    )
