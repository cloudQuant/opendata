"""Typed row contract for Multpl S&P 500 valuation history."""

from __future__ import annotations

from datetime import date
from typing import Literal

from opendata.data.models.base import ContractModel

SP500Metric = Literal[
    "pe_ratio",
    "shiller_pe",
    "price_to_book",
    "earnings_yield",
    "price_to_sales",
]
SP500Frequency = Literal["monthly", "annual"]
SP500ValueUnit = Literal["ratio", "percent"]


class SP500Multiples(ContractModel):
    """One dated S&P 500 valuation observation as displayed by Multpl."""

    metric: SP500Metric
    frequency: SP500Frequency
    date: date
    value: float
    value_unit: SP500ValueUnit
    is_estimate: bool
