"""Yahoo Finance available-index directory contract."""

from __future__ import annotations

from opendata.data.models.base import ContractModel


class YFinanceAvailableIndices(ContractModel):
    """One index the Yahoo Finance reference table publishes."""

    code: str
    name: str
    symbol: str


__all__ = ["YFinanceAvailableIndices"]
