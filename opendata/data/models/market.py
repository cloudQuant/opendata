"""Market data contract models (design §4.1)."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class Bar(ContractModel):
    """Daily unadjusted OHLCV bar.

    D10: ``Bar`` stores unadjusted prices only; adjusted series are
    synthesized from ``Bar`` + ``AdjustFactor`` at query/merge time
    (see ``opendata.data.adjust``), never persisted as separate columns.

    volume is in shares and amount in CNY; ``normalize()`` is the single
    owner of unit conversion (手 -> 股, design §8.2).
    """

    symbol: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
