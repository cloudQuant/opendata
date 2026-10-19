"""Declared public documentation and unresolved machine-response contracts.

``CalendarEarnings`` left :data:`NOT_DECLARED` in wave 38. The reason it carried -- "no stable
machine-response schema" -- was answered by the source itself in an unauthenticated round
(``docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/capture-receipt.json``): the route
requires ``filter[selected_date]``, publishes ``meta.total_pages`` with a page cursor, and 50 + 6
records equal ``meta.total_count`` for one date, while its own ``links.next`` resolves to
``HTTP/2 404`` against the site root. ``models.calendar_earnings_public`` maps that envelope.

The two estimates rows stay here. Every ``symbol_data/estimates`` request in the same round
answered ``HTTP/2 400`` naming the next required parameter, so no machine protocol for them is on
file and no cell of their contract has been read from a live response.
"""

from __future__ import annotations

from typing import Final

DECLARED_MODELS: Final[tuple[str, ...]] = ()
NOT_DECLARED: Final[dict[str, str]] = {
    "ForwardEpsEstimates": (
        "Official help confirms past and future EPS estimates in the UI, but the public estimates "
        "page returned HTTP 403 without credentials and no official machine protocol is documented."
    ),
    "ForwardSalesEstimates": (
        "Official help confirms past and future revenue estimates in the UI, but the public "
        "estimates page returned HTTP 403 without credentials and no official machine protocol is "
        "documented."
    ),
}
