"""Standardized data contract models (design §4.1, FR-2).

Every model extends :class:`opendata.data.models.base.ContractModel`
and serializes to both pydantic objects and ``pandas.DataFrame``.
"""

from __future__ import annotations

from opendata.data.models.adjustment import AdjustFactor, CorporateAction
from opendata.data.models.base import ContractModel
from opendata.data.models.financial import FinancialIndicator, FinancialStatement
from opendata.data.models.futures import FuturesFundamentals
from opendata.data.models.index import IndexConstituent
from opendata.data.models.market import Bar
from opendata.data.models.metadata import Instrument, TradingCalendar

__all__ = [
    "AdjustFactor",
    "Bar",
    "ContractModel",
    "CorporateAction",
    "FinancialIndicator",
    "FinancialStatement",
    "FuturesFundamentals",
    "IndexConstituent",
    "Instrument",
    "TradingCalendar",
]
