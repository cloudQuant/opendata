"""Futures contract models (design §4.1)."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class FuturesFundamentals(ContractModel):
    """Daily fundamentals of one futures contract.

    ``open_interest`` and ``open_interest_change`` are in lots (手);
    ``settlement`` is the exchange settlement price in CNY and may be
    missing for sources that do not publish it.
    """

    symbol: str
    exchange: str
    trade_date: date
    open_interest: float
    open_interest_change: float = 0.0
    settlement: float | None = None
