"""Declared public documentation and unresolved machine-response contracts.

``CalendarEarnings`` left :data:`NOT_DECLARED` in wave 38. The reason it carried -- "no stable
machine-response schema" -- was answered by the source itself in an unauthenticated round
(``docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/capture-receipt.json``): the route
requires ``filter[selected_date]``, publishes ``meta.total_pages`` with a page cursor, and 50 + 6
records equal ``meta.total_count`` for one date, while its own ``links.next`` resolves to
``HTTP/2 404`` against the site root. ``models.calendar_earnings_public`` maps that envelope.

Both estimates rows left this ledger in wave 52: the request protocol is the one C144
walked out of the ``HTTP/2 400`` chain (search resolves the numeric site id, then
``ticker_ids``/``period_type``/``relative_periods``/``estimates_data_items``), and the
response layout is recorded field by field in
``docs/evidence/C147/wave41-objective-scoreboard-20261020/runs/w152_sa_estimates_shape_inventory.txt``.
Live access without credentials is still a bot challenge, so the rows are accepted offline
only -- see ``face.md` §53 for that distinction.
"""

from __future__ import annotations

from typing import Final

DECLARED_MODELS: Final[tuple[str, ...]] = ()
NOT_DECLARED: Final[dict[str, str]] = {}
