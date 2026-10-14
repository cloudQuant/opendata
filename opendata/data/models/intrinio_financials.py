"""Strict typed output contracts for Intrinio financial statements and estimates.

The standardized statement models expose the common OpenBB wide columns while also
retaining Intrinio's documented tag metadata as typed line items.  This lets a
consumer use fields whose tag mapping is verified without losing unmapped source
facts or pretending their units are interchangeable.
"""

from __future__ import annotations

import datetime as dt
import math
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BeforeValidator, ConfigDict, StrictBool, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


def _finite_json_number(value: object) -> object:
    """Accept finite JSON numbers only; never coerce bools or numeric strings."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("financial values must be JSON numbers or null")
    if not math.isfinite(float(value)):
        raise ValueError("financial values must be finite")
    return value


FinancialValue = Annotated[int | float | None, BeforeValidator(_finite_json_number)]


def _strict_amount(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("financial statement amounts must be JSON integers or null")
    return value


def _finite_float(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("financial ratios must be JSON numbers or null")
    if not math.isfinite(float(value)):
        raise ValueError("financial values must be finite")
    return float(value)


FinancialAmount = Annotated[int | None, BeforeValidator(_strict_amount)]
FinancialFloat = Annotated[float | None, BeforeValidator(_finite_float)]


class IntrinioFinancialLineItem(ContractModel):
    """One standardized Intrinio data-tag observation, in the source's unit."""

    data_tag_id: StrictStr | None = None
    tag: StrictStr
    name: StrictStr | None = None
    value: FinancialValue
    unit: StrictStr | None = None
    parent: StrictStr | None = None
    sequence: FinancialValue = None
    factor: StrictStr | None = None
    balance: StrictStr | None = None


class IntrinioReportedDimension(ContractModel):
    """One XBRL axis/member pair attached to an as-reported fact."""

    axis: StrictStr
    member: StrictStr


class IntrinioReportedLineItem(ContractModel):
    """One as-reported XBRL fact with its hierarchy and dimensional context."""

    tag: StrictStr
    name: StrictStr | None = None
    value: FinancialValue
    unit: StrictStr | None = None
    balance: StrictStr | None = None
    abstract: StrictBool | None = None
    sequence: StrictInt | None = None
    depth: StrictInt | None = None
    dimensions: list[IntrinioReportedDimension]


class IntrinioFinancialPeriod(ContractModel):
    """Filing and fiscal-period identity common to standardized statements."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: StrictStr
    name: StrictStr | None = None
    period_ending: dt.date
    fiscal_period: StrictStr
    fiscal_year: StrictInt
    filing_date: dt.date | None = None
    filed_at: datetime | None = None
    accepted_date: datetime | None = None
    cik: StrictStr | None = None
    fundamental_id: StrictStr
    statement_code: StrictStr
    fundamental_type: StrictStr | None = None
    reported_currency: StrictStr | None = None
    is_latest: StrictBool | None = None
    line_items: list[IntrinioFinancialLineItem]


class IntrinioBalanceSheet(IntrinioFinancialPeriod):
    """Common balance-sheet wide columns plus source-preserving tagged facts."""

    cash_and_cash_equivalents: FinancialAmount = None
    short_term_investments: FinancialAmount = None
    cash_and_short_term_investments: FinancialAmount = None
    accounts_receivables: FinancialAmount = None
    other_receivables: FinancialAmount = None
    net_receivables: FinancialAmount = None
    inventory: FinancialAmount = None
    other_current_assets: FinancialAmount = None
    total_current_assets: FinancialAmount = None
    plant_property_equipment_net: FinancialAmount = None
    goodwill: FinancialAmount = None
    intangible_assets: FinancialAmount = None
    goodwill_and_intangible_assets: FinancialAmount = None
    long_term_investments: FinancialAmount = None
    tax_assets: FinancialAmount = None
    other_non_current_assets: FinancialAmount = None
    non_current_assets: FinancialAmount = None
    other_assets: FinancialAmount = None
    total_assets: FinancialAmount = None
    accounts_payable: FinancialAmount = None
    prepaid_expenses: FinancialAmount = None
    accrued_expenses: FinancialAmount = None
    short_term_debt: FinancialAmount = None
    tax_payables: FinancialAmount = None
    current_deferred_revenue: FinancialAmount = None
    other_current_liabilities: FinancialAmount = None
    other_payables: FinancialAmount = None
    total_current_liabilities: FinancialAmount = None
    total_payables: FinancialAmount = None
    long_term_debt: FinancialAmount = None
    deferred_revenue_non_current: FinancialAmount = None
    deferred_tax_liabilities_non_current: FinancialAmount = None
    other_non_current_liabilities: FinancialAmount = None
    total_non_current_liabilities: FinancialAmount = None
    capital_lease_obligations_current: FinancialAmount = None
    capital_lease_obligations_non_current: FinancialAmount = None
    capital_lease_obligations: FinancialAmount = None
    other_liabilities: FinancialAmount = None
    total_liabilities: FinancialAmount = None
    preferred_stock: FinancialAmount = None
    common_stock: FinancialAmount = None
    treasury_stock: FinancialAmount = None
    retained_earnings: FinancialAmount = None
    additional_paid_in_capital: FinancialAmount = None
    accumulated_other_comprehensive_income: FinancialAmount = None
    other_shareholders_equity: FinancialAmount = None
    other_total_shareholders_equity: FinancialAmount = None
    total_common_equity: FinancialAmount = None
    total_equity_non_controlling_interests: FinancialAmount = None
    total_liabilities_and_shareholders_equity: FinancialAmount = None
    minority_interest: FinancialAmount = None
    total_liabilities_and_total_equity: FinancialAmount = None
    total_investments: FinancialAmount = None
    total_debt: FinancialAmount = None
    net_debt: FinancialAmount = None


class IntrinioCashFlowStatement(IntrinioFinancialPeriod):
    """Common cash-flow wide columns plus source-preserving tagged facts."""

    net_income: FinancialAmount = None
    depreciation_and_amortization: FinancialAmount = None
    deferred_income_tax: FinancialAmount = None
    stock_based_compensation: FinancialAmount = None
    change_in_working_capital: FinancialAmount = None
    change_in_account_receivables: FinancialAmount = None
    change_in_inventory: FinancialAmount = None
    change_in_account_payable: FinancialAmount = None
    change_in_other_working_capital: FinancialAmount = None
    change_in_other_non_cash_items: FinancialAmount = None
    net_cash_from_operating_activities: FinancialAmount = None
    purchase_of_property_plant_and_equipment: FinancialAmount = None
    acquisitions: FinancialAmount = None
    purchase_of_investment_securities: FinancialAmount = None
    sale_and_maturity_of_investments: FinancialAmount = None
    other_investing_activities: FinancialAmount = None
    net_cash_from_investing_activities: FinancialAmount = None
    repayment_of_debt: FinancialAmount = None
    issuance_of_common_equity: FinancialAmount = None
    repurchase_of_common_equity: FinancialAmount = None
    net_common_equity_issuance: FinancialAmount = None
    net_preferred_equity_issuance: FinancialAmount = None
    net_equity_issuance: FinancialAmount = None
    short_term_net_debt_issuance: FinancialAmount = None
    long_term_net_debt_issuance: FinancialAmount = None
    net_debt_issuance: FinancialAmount = None
    common_dividends_paid: FinancialAmount = None
    preferred_dividends_paid: FinancialAmount = None
    net_dividends_paid: FinancialAmount = None
    other_financing_activities: FinancialAmount = None
    net_cash_from_financing_activities: FinancialAmount = None
    effect_of_exchange_rate_changes_on_cash: FinancialAmount = None
    net_change_in_cash_and_equivalents: FinancialAmount = None
    cash_at_beginning_of_period: FinancialAmount = None
    cash_at_end_of_period: FinancialAmount = None
    operating_cash_flow: FinancialAmount = None
    capital_expenditure: FinancialAmount = None
    free_cash_flow: FinancialAmount = None


class IntrinioIncomeStatement(IntrinioFinancialPeriod):
    """Common income-statement wide columns plus source-preserving tagged facts."""

    revenue: FinancialAmount = None
    operating_revenue: FinancialAmount = None
    cost_of_revenue: FinancialAmount = None
    operating_cost_of_revenue: FinancialAmount = None
    gross_profit: FinancialAmount = None
    general_and_admin_expense: FinancialAmount = None
    research_and_development_expense: FinancialAmount = None
    selling_and_marketing_expense: FinancialAmount = None
    selling_general_and_admin_expense: FinancialAmount = None
    other_expenses: FinancialAmount = None
    total_operating_expenses: FinancialAmount = None
    cost_and_expenses: FinancialAmount = None
    interest_income: FinancialAmount = None
    total_interest_expense: FinancialAmount = None
    net_interest_income: FinancialAmount = None
    depreciation_and_amortization: FinancialAmount = None
    ebit: FinancialAmount = None
    ebitda: FinancialAmount = None
    total_operating_income: FinancialAmount = None
    non_operating_income_excluding_interest: FinancialAmount = None
    net_income_from_continuing_operations: FinancialAmount = None
    net_income_from_discontinued_operations: FinancialAmount = None
    total_other_income_expenses: FinancialAmount = None
    total_pre_tax_income: FinancialAmount = None
    income_tax_expense: FinancialAmount = None
    other_adjustments_to_net_income: FinancialAmount = None
    net_income_deductions: FinancialAmount = None
    consolidated_net_income: FinancialAmount = None
    bottom_line_net_income: FinancialAmount = None
    basic_earnings_per_share: FinancialFloat = None
    diluted_earnings_per_share: FinancialFloat = None
    weighted_average_basic_shares_outstanding: FinancialAmount = None
    weighted_average_diluted_shares_outstanding: FinancialAmount = None
    weighted_average_basic_and_diluted_shares_outstanding: FinancialAmount = None
    cash_dividends_to_common_per_share: FinancialFloat = None


class IntrinioFinancialRatios(IntrinioFinancialPeriod):
    """Wide ratio vocabulary; unsupported/undocumented mappings stay null."""

    currency: StrictStr | None = None
    gross_profit_margin: FinancialFloat = None
    ebit_margin: FinancialFloat = None
    ebitda_margin: FinancialFloat = None
    operating_profit_margin: FinancialFloat = None
    pretax_profit_margin: FinancialFloat = None
    continuous_operations_profit_margin: FinancialFloat = None
    net_profit_margin: FinancialFloat = None
    bottom_line_profit_margin: FinancialFloat = None
    receivables_turnover: FinancialFloat = None
    payables_turnover: FinancialFloat = None
    inventory_turnover: FinancialFloat = None
    fixed_asset_turnover: FinancialFloat = None
    asset_turnover: FinancialFloat = None
    current_ratio: FinancialFloat = None
    quick_ratio: FinancialFloat = None
    solvency_ratio: FinancialFloat = None
    cash_ratio: FinancialFloat = None
    price_to_earnings: FinancialFloat = None
    price_to_earnings_growth: FinancialFloat = None
    forward_price_to_earnings_growth: FinancialFloat = None
    price_to_book: FinancialFloat = None
    price_to_sales: FinancialFloat = None
    price_to_free_cash_flow: FinancialFloat = None
    price_to_operating_cash_flow: FinancialFloat = None
    debt_to_assets: FinancialFloat = None
    debt_to_equity: FinancialFloat = None
    debt_to_capital: FinancialFloat = None
    long_term_debt_to_capital: FinancialFloat = None
    financial_leverage_ratio: FinancialFloat = None
    working_capital_turnover_ratio: FinancialFloat = None
    operating_cash_flow_ratio: FinancialFloat = None
    operating_cash_flow_sales_ratio: FinancialFloat = None
    free_cash_flow_operating_cash_flow_ratio: FinancialFloat = None
    debt_service_coverage_ratio: FinancialFloat = None
    interest_coverage_ratio: FinancialFloat = None
    short_term_operating_cash_flow_coverage_ratio: FinancialFloat = None
    operating_cash_flow_coverage_ratio: FinancialFloat = None
    capital_expenditure_coverage_ratio: FinancialFloat = None
    dividend_paid_and_capex_coverage_ratio: FinancialFloat = None
    dividend_payout_ratio: FinancialFloat = None
    dividend_yield: FinancialFloat = None
    dividend_per_share: FinancialFloat = None
    revenue_per_share: FinancialFloat = None
    net_income_per_share: FinancialFloat = None
    interest_debt_per_share: FinancialFloat = None
    cash_per_share: FinancialFloat = None
    book_value_per_share: FinancialFloat = None
    tangible_book_value_per_share: FinancialFloat = None
    shareholders_equity_per_share: FinancialFloat = None
    operating_cash_flow_per_share: FinancialFloat = None
    capex_per_share: FinancialFloat = None
    free_cash_flow_per_share: FinancialFloat = None
    net_income_per_ebt: FinancialFloat = None


class IntrinioKeyMetrics(IntrinioFinancialPeriod):
    """Wide key-metric vocabulary; unsupported/undocumented mappings stay null."""

    currency: StrictStr | None = None
    market_cap: FinancialValue = None
    enterprise_value: FinancialValue = None
    ev_to_sales: FinancialFloat = None
    ev_to_operating_cash_flow: FinancialFloat = None
    ev_to_free_cash_flow: FinancialFloat = None
    ev_to_ebitda: FinancialFloat = None
    net_debt_to_ebitda: FinancialFloat = None
    pe_ratio: FinancialFloat = None
    forward_pe: FinancialFloat = None
    eps: FinancialFloat = None
    price_to_sales: FinancialFloat = None
    price_to_book: FinancialFloat = None
    book_value_per_share: FinancialFloat = None
    price_to_cash: FinancialFloat = None
    cash_per_share: FinancialFloat = None
    price_to_free_cash_flow: FinancialFloat = None
    debt_to_equity: FinancialFloat = None
    long_term_debt_to_equity: FinancialFloat = None
    quick_ratio: FinancialFloat = None
    current_ratio: FinancialFloat = None
    gross_margin: FinancialFloat = None
    profit_margin: FinancialFloat = None
    operating_margin: FinancialFloat = None
    return_on_assets: FinancialFloat = None
    operating_return_on_assets: FinancialFloat = None
    return_on_tangible_assets: FinancialFloat = None
    return_on_equity: FinancialFloat = None
    return_on_invested_capital: FinancialFloat = None
    return_on_capital_employed: FinancialFloat = None
    return_on_investment: FinancialFloat = None
    payout_ratio: FinancialFloat = None
    dividend_yield: FinancialFloat = None
    income_quality: FinancialFloat = None
    graham_number: FinancialFloat = None
    graham_net_net: FinancialFloat = None
    tax_burden: FinancialFloat = None
    interest_burden: FinancialFloat = None
    working_capital: FinancialFloat = None
    invested_capital: FinancialFloat = None
    earnings_yield: FinancialFloat = None
    free_cash_flow_yield: FinancialFloat = None
    capex_to_operating_cash_flow: FinancialFloat = None
    capex_to_depreciation: FinancialFloat = None
    capex_to_revenue: FinancialFloat = None
    sales_general_and_administrative_to_revenue: FinancialFloat = None
    research_and_development_to_revenue: FinancialFloat = None


class IntrinioReportedFinancials(ContractModel):
    """As-reported statement header and variable XBRL facts.

    The public standard model only promises period metadata; variable source
    tags are represented here as strict typed line items rather than columns.
    """

    symbol: StrictStr
    period_ending: dt.date
    fiscal_period: StrictStr
    fiscal_year: StrictInt
    filing_date: dt.date | None = None
    filed_at: datetime | None = None
    accepted_date: datetime | None = None
    cik: StrictStr | None = None
    fundamental_id: StrictStr
    fundamental_type: StrictStr | None = None
    statement_code: StrictStr
    reported_currency: StrictStr | None = None
    is_latest: StrictBool | None = None
    line_items: list[IntrinioReportedLineItem]


class IntrinioForwardEstimate(ContractModel):
    """Common Zacks estimate statistics for future fiscal periods."""

    symbol: StrictStr
    name: StrictStr | None = None
    date: dt.date | None = None
    fiscal_year: StrictInt | None = None
    fiscal_period: StrictStr | None = None
    calendar_year: StrictInt | None = None
    calendar_period: StrictStr | None = None
    low_estimate: FinancialValue = None
    high_estimate: FinancialValue = None
    mean: FinancialValue = None
    median: FinancialValue = None
    standard_deviation: FinancialValue = None
    number_of_analysts: StrictInt | None = None
    percent_change: FinancialValue = None
    mean_7_days_ago: FinancialValue = None
    mean_30_days_ago: FinancialValue = None
    mean_60_days_ago: FinancialValue = None
    mean_90_days_ago: FinancialValue = None
    revisions_change_percent: FinancialFloat = None
    mean_1w: FinancialFloat = None
    mean_1m: FinancialFloat = None
    mean_2m: FinancialFloat = None
    mean_3m: FinancialFloat = None
    revisions_1w_up: StrictInt | None = None
    revisions_1w_down: StrictInt | None = None
    revisions_1w_change_percent: FinancialValue = None
    revisions_1m_up: StrictInt | None = None
    revisions_1m_down: StrictInt | None = None
    revisions_1m_change_percent: FinancialValue = None
    revisions_3m_up: StrictInt | None = None
    revisions_3m_down: StrictInt | None = None
    revisions_3m_change_percent: FinancialValue = None


class IntrinioForwardEbitdaEstimate(ContractModel):
    """One Zacks EBITDA consensus observation, preserving its date granularity."""

    symbol: StrictStr
    name: StrictStr | None = None
    last_updated: dt.date | None = None
    period_ending: dt.date | None = None
    fiscal_year: StrictInt | None = None
    fiscal_period: StrictStr | None = None
    calendar_year: StrictInt | None = None
    calendar_period: StrictInt | StrictStr | None = None
    estimate_year: StrictInt | None = None
    estimate_month: StrictInt | None = None
    source_period: Literal["fy", "fq"] | None = None
    low_estimate: FinancialAmount = None
    high_estimate: FinancialAmount = None
    mean: FinancialAmount = None
    median: FinancialAmount = None
    standard_deviation: FinancialAmount = None
    number_of_analysts: StrictInt | None = None
    conensus_type: StrictStr | None = None
    consensus_type: StrictStr | None = None


class IntrinioForwardEpsEstimate(IntrinioForwardEstimate):
    """One future EPS consensus period."""

    low_estimate: FinancialFloat = None
    high_estimate: FinancialFloat = None
    mean: FinancialFloat = None
    median: FinancialFloat = None
    standard_deviation: FinancialFloat = None


class IntrinioForwardSalesEstimate(IntrinioForwardEstimate):
    """One future sales consensus period."""

    low_estimate: FinancialAmount = None
    high_estimate: FinancialAmount = None
    mean: FinancialAmount = None
    median: FinancialAmount = None
    standard_deviation: FinancialAmount = None


class IntrinioForwardPeEstimate(ContractModel):
    """Forward P/E estimates for the next five fiscal years."""

    symbol: StrictStr
    name: StrictStr | None = None
    year1: FinancialFloat = None
    year2: FinancialFloat = None
    year3: FinancialFloat = None
    year4: FinancialFloat = None
    year5: FinancialFloat = None
    peg_ratio_year1: FinancialFloat = None
    eps_ttm: FinancialFloat = None
    last_updated: dt.date | None = None


__all__ = [
    "IntrinioBalanceSheet",
    "IntrinioCashFlowStatement",
    "IntrinioFinancialLineItem",
    "IntrinioFinancialPeriod",
    "IntrinioFinancialRatios",
    "IntrinioForwardEbitdaEstimate",
    "IntrinioForwardEpsEstimate",
    "IntrinioForwardEstimate",
    "IntrinioForwardPeEstimate",
    "IntrinioForwardSalesEstimate",
    "IntrinioIncomeStatement",
    "IntrinioKeyMetrics",
    "IntrinioReportedDimension",
    "IntrinioReportedFinancials",
    "IntrinioReportedLineItem",
]
