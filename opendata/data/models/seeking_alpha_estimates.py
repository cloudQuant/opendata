"""Seeking Alpha forward estimate rows, one row per reporting period per ticker.

The upstream envelope publishes a value series per *data item* rather than per period, keyed
``estimates.<sa_ticker_id>.<item>.<relative_period>[{dataitemvalue, effectivedate, period}]``.
These contracts are the transposed view: one row per ``(relative_period, period_end_date)`` with the
items read across. Field names keep the upstream item spelling so a row can be traced back to the
cell it came from; the shape is recorded in
``docs/evidence/C147/wave41-objective-scoreboard-20261020/runs/w152_sa_estimates_shape_inventory.txt``.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, StrictStr

from opendata.data.models.base import ContractModel


def _finite(value: object) -> object:
    """Reject a non-number or a non-finite number in a numeric cell."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a JSON number")
    if not math.isfinite(float(value)):
        raise ValueError("expected a finite JSON number")
    return value


FiniteNumber = Annotated[float, BeforeValidator(_finite)]
FiniteCount = Annotated[int, BeforeValidator(_finite), Field(ge=0)]


class _SeekingAlphaEstimateRow(ContractModel):
    """The columns every estimate row carries, straight from the envelope's ``period`` object."""

    symbol: StrictStr
    sa_ticker_id: StrictStr
    relative_period: StrictStr
    period_type: Literal["quarterly", "annual"]
    fiscal_year: FiniteCount
    fiscal_quarter: FiniteCount | None = None
    calendar_year: FiniteCount | None = None
    calendar_quarter: FiniteCount | None = None
    period_end_date: date
    as_of: datetime | None = None


class SeekingAlphaForwardEpsEstimates(_SeekingAlphaEstimateRow):
    """Per-period EPS estimates: GAAP and normalized actual, consensus low/mean/high, count."""

    eps_gaap_actual: FiniteNumber | None = None
    eps_gaap_consensus_low: FiniteNumber | None = None
    eps_gaap_consensus_mean: FiniteNumber | None = None
    eps_gaap_consensus_high: FiniteNumber | None = None
    eps_normalized_actual: FiniteNumber | None = None
    eps_normalized_consensus_low: FiniteNumber | None = None
    eps_normalized_consensus_mean: FiniteNumber | None = None
    eps_normalized_consensus_high: FiniteNumber | None = None
    eps_normalized_num_of_estimates: FiniteCount | None = None


class SeekingAlphaForwardSalesEstimates(_SeekingAlphaEstimateRow):
    """Per-period revenue estimates: actual, consensus low/mean/high, count."""

    revenue_actual: FiniteNumber | None = None
    revenue_consensus_low: FiniteNumber | None = None
    revenue_consensus_mean: FiniteNumber | None = None
    revenue_consensus_high: FiniteNumber | None = None
    revenue_num_of_estimates: FiniteCount | None = None


__all__ = [
    "SeekingAlphaForwardEpsEstimates",
    "SeekingAlphaForwardSalesEstimates",
]
