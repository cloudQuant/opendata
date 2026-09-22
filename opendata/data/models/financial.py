"""Financial contract models (design §4.1)."""

from __future__ import annotations

from datetime import date

from opendata.data.models.base import ContractModel


class FinancialStatement(ContractModel):
    """One statement line in long format.

    Three time dimensions distinguish first releases from restatements
    (design §4.1): ``report_period`` (the fiscal period end),
    ``announce_date`` (when the figure became public) and ``revision``
    (1 for the first release, incremented for restatements). Together
    they support point-in-time queries for backtests.

    ``statement_type`` is one of ``balance`` / ``income`` / ``cashflow``;
    ``item`` is the normalized statement item code (e.g.
    ``total_assets``), mapped from source-specific names in
    ``normalize()``.
    """

    symbol: str
    statement_type: str
    report_period: date
    announce_date: date
    item: str
    value: float
    revision: int = 1


class FinancialIndicator(ContractModel):
    """One normalized financial indicator value.

    Shares the three time dimensions of :class:`FinancialStatement`.
    ``indicator`` is the normalized code (e.g. ``eps``, ``roe``) and
    ``unit`` the display unit (e.g. ``元``, ``%``), carried through from
    the source when the code is a passthrough.
    """

    symbol: str
    report_period: date
    announce_date: date
    indicator: str
    value: float
    unit: str | None = None
    revision: int = 1
