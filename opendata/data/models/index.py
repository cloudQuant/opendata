"""Index contract models (design §4.1)."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class IndexConstituent(ContractModel):
    """Index membership as of a snapshot date.

    Design §4.1: historical snapshots are supported by storing one row
    per (index, constituent, as_of); a date-range query reconstructs
    membership at any past rebalance. ``weight`` is in percent and may
    be missing for free-float-unweighted sources.

    ``as_of`` is the date the source's own list belongs to - the CSI
    weight file's data date, an index company's effective date - and not
    the moment the call was made. Only sources that publish such a date
    may fill the field with it; a source whose endpoint carries no list
    date says so in its capability ``notes`` instead of borrowing one, and
    its rows are then an observation, which is why two legs of this domain
    can disagree about membership without either being wrong (C34,
    ``docs/evidence/C34/``).
    """

    index_symbol: str
    symbol: str
    as_of: date
    weight: float | None = None
