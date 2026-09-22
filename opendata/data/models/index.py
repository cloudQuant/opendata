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
    """

    index_symbol: str
    symbol: str
    as_of: date
    weight: float | None = None
