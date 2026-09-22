"""Capability declaration for data source registration (design §4.3).

Field names are globally unified (FR-2): no ``asset``/``asset_class``
mixing, one spelling everywhere.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

#: Registered but not implemented yet; excluded from auto routing (§4.3).
ON_DEMAND = "on-demand"
#: Upstream work pending; excluded from auto routing (§4.3).
UPSTREAM_PENDING = "upstream-pending"

_AUTO_EXCLUDED_NOTES = frozenset({ON_DEMAND, UPSTREAM_PENDING})


class Capability(BaseModel):
    """One capability of one data source for one data domain.

    A fetcher declares exactly one capability; the registry matches
    ``resolve()`` calls against these fields. ``verified`` marks data
    that has passed cross-source validation; unverified capabilities
    never participate in ``source="auto"`` routing (FR-3). ``notes``
    carries free-form annotations, with ``on-demand`` and
    ``upstream-pending`` reserved for registered-but-unimplemented
    capabilities (auto routing skips those too).
    """

    model_config = ConfigDict(extra="forbid")

    asset_class: str
    domain: str
    period: str
    market: str
    source: str
    verified: bool
    notes: str = ""

    def participates_in_auto(self) -> bool:
        """Whether this capability may serve ``source="auto"`` requests.

        Returns:
            False when unverified or annotated as on-demand /
            upstream-pending, True otherwise.
        """
        return self.verified and self.notes not in _AUTO_EXCLUDED_NOTES
