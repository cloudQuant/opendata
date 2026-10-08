"""Standardized data contract models (design §4.1, FR-2).

Every model extends :class:`opendata.data.models.base.ContractModel`
and serializes to both pydantic objects and ``pandas.DataFrame``.
"""

from __future__ import annotations

from opendata.data.models.adjustment import AdjustFactor, CorporateAction
from opendata.data.models.base import ContractModel
from opendata.data.models.currency import CurrencyReferenceRate, SdmxGroupContext
from opendata.data.models.ecb_series import (
    EcbBalanceOfPaymentsObservation,
    EcbYieldCurveObservation,
)
from opendata.data.models.economic_series import (
    FredOutputType,
    FredTransformUnits,
    SeriesCatalogItem,
    SeriesObservation,
)
from opendata.data.models.equity_price import EquityHistorical, EquityQuote
from opendata.data.models.financial import FinancialIndicator, FinancialStatement
from opendata.data.models.fred_sofr import FredSofrObservation
from opendata.data.models.fred_sonia import FredSoniaObservation
from opendata.data.models.futures import FuturesFundamentals
from opendata.data.models.index import IndexConstituent
from opendata.data.models.macro import MacroSeries
from opendata.data.models.market import Bar, OverseasBar
from opendata.data.models.metadata import Instrument, TradingCalendar
from opendata.data.models.period_series import (
    BlsCatalogItem,
    BlsCatalogPage,
    BlsFootnote,
    BlsObservation,
)

__all__ = [
    "AdjustFactor",
    "Bar",
    "BlsCatalogItem",
    "BlsCatalogPage",
    "BlsFootnote",
    "BlsObservation",
    "MacroSeries",
    "OverseasBar",
    "ContractModel",
    "CorporateAction",
    "CurrencyReferenceRate",
    "EcbBalanceOfPaymentsObservation",
    "EcbYieldCurveObservation",
    "EquityHistorical",
    "EquityQuote",
    "FinancialIndicator",
    "FinancialStatement",
    "FredOutputType",
    "FredSoniaObservation",
    "FredSofrObservation",
    "FredTransformUnits",
    "FuturesFundamentals",
    "IndexConstituent",
    "Instrument",
    "TradingCalendar",
    "SeriesCatalogItem",
    "SeriesObservation",
    "SdmxGroupContext",
]
