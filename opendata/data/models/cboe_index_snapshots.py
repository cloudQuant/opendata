"""Typed Cboe index-snapshot rows for the public delayed-quotes JSON envelope.

The column list is the declared output contract of the reference checkout's
``openbb_cboe/models/index_snapshots.py`` (``CboeIndexSnapshotsData``, 12 own annotations over the
``IndexSnapshotsData`` base), kept under the source cell names the envelope actually publishes.
The envelope is ``https://cdn.cboe.com/api/global/delayed_quotes/quotes/all_us_indices.json``
(307 to ``cdn-api.cboe.com``, then ``HTTP/2 200``; captured 2026-10-19 at
``docs/evidence/C140/wave37-unauthenticated-access-20261019/sources/cboe-all-us-indices.json``,
body sha256 ``9554ad964bce…`` per that bundle's ``capture-receipt.json``).

Three declared columns have no cell in that envelope -- ``name``, ``currency`` and ``status`` --
so they are declared nullable here and the fetcher never fills them. That is the point of listing
them: an implementer may not land a value for a column the transport does not return.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, StrictFloat, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


class CboeIndexSnapshots(ContractModel):
    """One Cboe delayed-quotes index snapshot; columns absent from the envelope stay null."""

    symbol: StrictStr
    price: StrictFloat | None = None
    open: StrictFloat | None = None
    high: StrictFloat | None = None
    low: StrictFloat | None = None
    close: StrictFloat | None = None
    prev_close: StrictFloat | None = None
    change: StrictFloat | None = None
    change_percent: StrictFloat | None = None
    bid: StrictFloat | None = None
    ask: StrictFloat | None = None
    volume: StrictInt | None = None
    last_trade_time: AwareDatetime | datetime | None = None
    name: StrictStr | None = None
    currency: StrictStr | None = None
    status: StrictStr | None = None


__all__ = ["CboeIndexSnapshots"]
