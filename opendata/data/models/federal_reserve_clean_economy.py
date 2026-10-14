"""Clean-room public-output contracts for Federal Reserve economy data.

These models follow the OpenBB V4 public data-model fields.  The
``wam``/``security_types`` Central Bank Holdings fields are explicit
Federal Reserve source extensions because the V4 query exposes WAM but
the V4 data-model page omits the corresponding response field.
"""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from opendata.data.models.base import ContractModel


class CentralBankHoldings(ContractModel):
    """A SOMA holding, weekly/monthly summary, or official WAM observation.

    Monetary fields are in thousands of U.S. dollars, as required by the
    OpenBB V4 public contract.  The source API publishes monetary amounts
    in dollars, so the Fetcher normalizes them by 1,000. ``wam`` preserves the
    source-reported weighted average maturity value without inferring a unit.
    """

    date: date_type
    security_type: str | None = None
    description: str | None = None
    is_aggregated: Literal["Y"] | None = None
    cusip: str | None = None
    issuer: str | None = None
    maturity_date: date_type | None = None
    term: str | None = None
    face_value: float | None = None
    par_value: float | None = None
    coupon: float | None = None
    spread: float | None = None
    percent_outstanding: float | None = None
    bills: float | None = None
    frn: float | None = None
    notes_and_bonds: float | None = None
    tips: float | None = None
    mbs: float | None = None
    cmbs: float | None = None
    agencies: float | None = None
    total: float | None = None
    tips_inflation_compensation: float | None = None
    change_prior_week: float | None = None
    change_prior_year: float | None = None
    record_type: Literal["holding", "summary", "wam"]
    wam: float | None = None
    security_types: list[str] | None = None


class InflationExpectations(ContractModel):
    """Quarterly Philly Fed SPF inflation forecast series (percentage points)."""

    date: date_type
    infpgdp1yr: float | None = None
    infcpi1yr: float | None = None
    infcpi10yr: float | None = None


class TotalFactorProductivity(ContractModel):
    """SF Fed utilization-adjusted TFP data in annualized percent units."""

    date: str | None = None
    d_y_prod: float | None = None
    d_y_inc: float | None = None
    d_y: float | None = None
    d_hours: float | None = None
    d_lp: float | None = None
    d_k: float | None = None
    d_lq_bls_interpolated: float | None = None
    d_lq_aaronson_sullivan: float | None = None
    d_lq: float | None = None
    alpha: float | None = None
    d_tfp: float | None = None
    d_util: float | None = None
    d_tfp_util: float | None = None
    relative_price: float | None = None
    inv_share: float | None = None
    d_tfp_i: float | None = None
    d_tfp_c: float | None = None
    d_u_invest: float | None = None
    d_u_consumption: float | None = None
    d_tfp_i_util: float | None = None
    d_tfp_c_util: float | None = None
    variable: str | None = None
    variable_title: str | None = None
    full_sample_mean: float | None = None
    past_4_quarters: float | None = None
    past_8_quarters: float | None = None
    since_2019: float | None = None
    period_2004_2019: float | None = None
    period_1995_2004: float | None = None
    period_1973_1995: float | None = None
    period_1947_1973: float | None = None
