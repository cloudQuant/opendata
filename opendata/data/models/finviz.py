"""Strict contract rows parsed from Finviz's public, delayed HTML pages."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import field_validator

from opendata.data.models.base import ContractModel

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")


def _valid_symbol(value: str) -> str:
    """Validate the symbol spelling retained by a Finviz contract row."""
    normalized = value.strip().upper()
    if not _SYMBOL.fullmatch(normalized):
        raise ValueError("symbol must be a valid Finviz ticker")
    return normalized


class FinvizGroupComparison(ContractModel):
    """One sector row from Finviz Groups Overview or Performance."""

    group: Literal["sector"]
    name: str
    stocks: int | None = None
    market_cap_usd: float | None = None
    dividend_yield_pct: float | None = None
    pe_ratio: float | None = None
    forward_pe_ratio: float | None = None
    peg_ratio: float | None = None
    long_term_debt_to_equity: float | None = None
    debt_to_equity: float | None = None
    short_float_pct: float | None = None
    recommendation: float | None = None
    performance_week_pct: float | None = None
    performance_month_pct: float | None = None
    performance_quarter_pct: float | None = None
    performance_half_year_pct: float | None = None
    performance_year_pct: float | None = None
    performance_ytd_pct: float | None = None
    average_volume_shares: float | None = None
    relative_volume: float | None = None
    change_pct: float | None = None
    volume_shares: float | None = None


class FinvizEtfPricePerformance(ContractModel):
    """One ETF's performance row from the public Finviz ETF Perf view."""

    symbol: str
    holdings: int | None = None
    assets_under_management_usd: float | None = None
    flows_1m_pct: float | None = None
    flows_3m_pct: float | None = None
    ytd_return_pct: float | None = None
    return_1y_pct: float | None = None
    return_3y_pct: float | None = None
    expense_5y_pct: float | None = None
    price_usd: float | None = None
    change_pct: float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)


class FinvizPricePerformance(ContractModel):
    """One equity's published lookback performance row."""

    symbol: str
    performance_week_pct: float | None = None
    performance_month_pct: float | None = None
    performance_quarter_pct: float | None = None
    performance_half_year_pct: float | None = None
    performance_ytd_pct: float | None = None
    performance_year_pct: float | None = None
    performance_3y_pct: float | None = None
    performance_5y_pct: float | None = None
    performance_10y_pct: float | None = None
    volatility_week_pct: float | None = None
    volatility_month_pct: float | None = None
    average_volume_shares: float | None = None
    relative_volume: float | None = None
    price_usd: float | None = None
    change_pct: float | None = None
    volume_shares: float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)


class FinvizEquityInfo(ContractModel):
    """Company identity and profile values visible on a Finviz stock page."""

    symbol: str
    company_name: str
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    market_cap_usd: float | None = None
    employees: int | None = None
    ipo_date: date | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)

    @field_validator("company_name")
    @classmethod
    def _validate_company_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("company_name must not be blank")
        return value.strip()


class FinvizKeyMetrics(ContractModel):
    """Typed key-metric snapshot from the public Finviz stock page."""

    symbol: str
    market_cap_usd: float | None = None
    enterprise_value_usd: float | None = None
    net_income_usd: float | None = None
    sales_usd: float | None = None
    book_value_per_share_usd: float | None = None
    cash_per_share_usd: float | None = None
    dividend_estimate_per_share_usd: float | None = None
    dividend_ttm_per_share_usd: float | None = None
    dividend_estimate_yield_pct: float | None = None
    dividend_ttm_yield_pct: float | None = None
    dividend_growth_3y_pct: float | None = None
    dividend_growth_5y_pct: float | None = None
    payout_pct: float | None = None
    pe_ratio: float | None = None
    forward_pe_ratio: float | None = None
    peg_ratio: float | None = None
    price_to_sales: float | None = None
    price_to_book: float | None = None
    price_to_cash: float | None = None
    price_to_free_cash_flow: float | None = None
    enterprise_value_to_ebitda: float | None = None
    enterprise_value_to_sales: float | None = None
    quick_ratio: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    long_term_debt_to_equity: float | None = None
    eps_ttm_usd: float | None = None
    eps_next_year_usd: float | None = None
    eps_next_quarter_usd: float | None = None
    eps_growth_this_year_pct: float | None = None
    eps_growth_next_year_pct: float | None = None
    eps_growth_next_5y_pct: float | None = None
    eps_growth_past_3y_pct: float | None = None
    eps_growth_past_5y_pct: float | None = None
    sales_growth_past_3y_pct: float | None = None
    sales_growth_past_5y_pct: float | None = None
    eps_growth_ttm_yoy_pct: float | None = None
    sales_growth_ttm_yoy_pct: float | None = None
    eps_growth_qoq_pct: float | None = None
    sales_growth_qoq_pct: float | None = None
    insider_ownership_pct: float | None = None
    insider_transactions_pct: float | None = None
    institutional_ownership_pct: float | None = None
    institutional_transactions_pct: float | None = None
    roa_pct: float | None = None
    roe_pct: float | None = None
    roic_pct: float | None = None
    gross_margin_pct: float | None = None
    operating_margin_pct: float | None = None
    profit_margin_pct: float | None = None
    sma_20_pct: float | None = None
    sma_50_pct: float | None = None
    sma_200_pct: float | None = None
    shares_outstanding: float | None = None
    float_shares: float | None = None
    short_float_pct: float | None = None
    short_ratio: float | None = None
    short_interest_shares: float | None = None
    high_52w_usd: float | None = None
    low_52w_usd: float | None = None
    high_52w_change_pct: float | None = None
    low_52w_change_pct: float | None = None
    volatility_week_pct: float | None = None
    volatility_month_pct: float | None = None
    average_true_range_14_usd: float | None = None
    rsi_14: float | None = None
    beta: float | None = None
    relative_volume: float | None = None
    average_volume_shares: float | None = None
    volume_shares: float | None = None
    performance_week_pct: float | None = None
    performance_month_pct: float | None = None
    performance_quarter_pct: float | None = None
    performance_half_year_pct: float | None = None
    performance_ytd_pct: float | None = None
    performance_year_pct: float | None = None
    performance_3y_pct: float | None = None
    performance_5y_pct: float | None = None
    performance_10y_pct: float | None = None
    recommendation: float | None = None
    target_price_usd: float | None = None
    previous_close_usd: float | None = None
    price_usd: float | None = None
    change_pct: float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)


class FinvizPriceTarget(ContractModel):
    """One analyst action from the ratings rows currently shown on a stock page."""

    symbol: str
    date: date
    action: str
    analyst: str
    rating_change: str | None = None
    price_target_previous_usd: float | None = None
    price_target_usd: float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)

    @field_validator("action", "analyst")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("required Finviz text is blank")
        return value.strip()


class FinvizEquityScreener(ContractModel):
    """One stock-screen row from Finviz's public Overview view."""

    rank: int
    symbol: str
    company_name: str
    sector: str | None = None
    industry: str | None = None
    country: str | None = None
    market_cap_usd: float | None = None
    pe_ratio: float | None = None
    price_usd: float | None = None
    change_pct: float | None = None
    volume_shares: float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _valid_symbol(value)

    @field_validator("company_name")
    @classmethod
    def _validate_company_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("company_name must not be blank")
        return value.strip()
