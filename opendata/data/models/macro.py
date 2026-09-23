"""Contract models for macro time series (design §4.1).

Macro observations are not trades: the field set is deliberately minimal
and distinct from the equity bars (``date`` instead of ``trade_date``,
``value`` instead of OHLCV). ``value`` is nullable because upstream
macro sources genuinely publish missing observations (FRED uses its
``.`` sentinel) - the contract states what the source delivers instead
of fabricating zeros.
"""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class MacroSeries(ContractModel):
    """One observation of a macro series (for example CPI)."""

    series_id: str
    date: date
    value: float | None = None
