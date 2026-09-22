"""Adjustment contract models (design §4.1)."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class AdjustFactor(ContractModel):
    """Cumulative per-day adjustment factor.

    D10: adjusted price = raw price x factor. ``qfq_factor`` is 1.0 on
    the most recent date (forward-adjusted anchor); ``hfq_factor`` is
    1.0 on the earliest date (back-adjusted anchor). One row per
    (symbol, trade_date); sources with event-only factor series must
    cumulate them in ``normalize()`` before emitting this model.
    """

    symbol: str
    trade_date: date
    qfq_factor: float
    hfq_factor: float


class CorporateAction(ContractModel):
    """Corporate action event effective on the ex-date.

    All per-share quantities; cash amounts in CNY. A row with all zeros
    is invalid and must be filtered in ``normalize()``.
    """

    symbol: str
    ex_date: date
    cash_dividend: float = 0.0
    stock_dividend: float = 0.0
    rights_shares: float = 0.0
    rights_price: float = 0.0
