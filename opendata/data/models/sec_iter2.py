"""Strict output contracts for the SEC iteration-2 provider models."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.models.base import ContractModel


class SecCikMapRecord(ContractModel):
    """Public CIK result with SEC ticker/name traceability extensions."""

    cik: str
    ticker: str
    name: str


class SecSymbolMapRecord(ContractModel):
    """Public symbol-map result with SEC identity fields retained for traceability."""

    symbol: str
    cik: str
    ticker: str
    name: str


class SecEquitySearchRecord(ContractModel):
    """Public equity-search row with SEC identity and exchange extensions."""

    symbol: str
    name: str | None = None
    cik: str
    ticker: str
    exchange: str | None = None
    series_id: str | None = None
    class_id: str | None = None


class SecBalanceSheetFact(ContractModel):
    """One supported instant fact from a company's SEC companyfacts document."""

    cik: str
    symbol: str | None
    taxonomy: Literal["us-gaap"]
    tag: str
    label: str
    unit: str
    value: Decimal
    period_start: None = None
    period_end: date
    filed_date: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession_number: str

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("value must be finite")
        return value


class SecCashFlowFact(ContractModel):
    """One supported duration fact from a company's SEC companyfacts document."""

    cik: str
    symbol: str | None
    taxonomy: Literal["us-gaap"]
    tag: str
    label: str
    unit: str
    value: Decimal
    period_start: date
    period_end: date
    filed_date: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession_number: str

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("value must be finite")
        return value


class SecIncomeStatementFact(SecCashFlowFact):
    """One supported duration fact from an income statement."""


class SecBalanceSheetGrowth(ContractModel):
    """Year-over-year change between comparable SEC instant facts."""

    cik: str
    symbol: str | None
    taxonomy: Literal["us-gaap"]
    tag: str
    label: str
    unit: str
    value: Decimal
    prior_value: Decimal
    growth_percent: Decimal | None
    period_end: date
    prior_period_end: date
    filed_date: date
    prior_filed_date: date
    form: str
    prior_form: str
    accession_number: str
    prior_accession_number: str

    @field_validator("value", "prior_value", "growth_percent")
    @classmethod
    def _finite_values(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("growth values must be finite")
        return value


class SecCashFlowStatementGrowth(ContractModel):
    """Year-over-year change between comparable SEC duration facts."""

    cik: str
    symbol: str | None
    taxonomy: Literal["us-gaap"]
    tag: str
    label: str
    unit: str
    value: Decimal
    prior_value: Decimal
    growth_percent: Decimal | None
    period_start: date
    period_end: date
    prior_period_start: date
    prior_period_end: date
    filed_date: date
    prior_filed_date: date
    form: str
    prior_form: str
    accession_number: str
    prior_accession_number: str

    @field_validator("value", "prior_value", "growth_percent")
    @classmethod
    def _finite_values(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("growth values must be finite")
        return value


class SecIncomeStatementGrowth(SecCashFlowStatementGrowth):
    """Year-over-year change between comparable SEC income facts."""


class SecStatementFactExtension(ContractModel):
    """An exact SEC companyfacts observation retained alongside a wide statement row."""

    tag: str
    label: str
    unit: str
    value: Decimal
    period_beginning: date | None
    period_ending: date
    filed_date: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession: str


class SecStatementRow(ContractModel):
    """Common public statement columns and SEC filing-vintage traceability."""

    period_ending: date
    fiscal_period: str | None
    fiscal_year: int | None
    filing_date: date | None
    accepted_date: datetime | None = None
    cik: str
    symbol: str | None
    reported_currency: str | None
    period_beginning: date | None = None
    form: str | None = None
    accession: str
    sec_facts: tuple[SecStatementFactExtension, ...] = ()


class _SecBalanceSheetPublicFields(ContractModel):
    """SEC-tab balance-sheet columns; nullable means the source did not report it."""

    cash_and_equivalents: float | None = None
    restricted_cash: float | None = None
    fed_funds_sold: float | None = None
    interest_bearing_deposits_at_other_banks: float | None = None
    time_deposits_placed: float | None = None
    trading_account_securities: float | None = None
    loans_and_leases: float | None = None
    allowance_for_loan_and_lease_losses: float | None = None
    net_loans_and_leases: float | None = None
    loans_held_for_sale: float | None = None
    accrued_investment_income: float | None = None
    customer_and_other_receivables: float | None = None
    note_receivable: float | None = None
    nontrade_receivables: float | None = None
    net_inventory: float | None = None
    prepaid_expenses: float | None = None
    current_deferred_tax_assets: float | None = None
    other_current_nonoperating_assets: float | None = None
    gross_ppe: float | None = None
    accumulated_depreciation: float | None = None
    net_ppe: float | None = None
    net_premises_and_equipment: float | None = None
    operating_lease_right_of_use_asset: float | None = None
    finance_lease_right_of_use_asset: float | None = None
    mortgage_servicing_rights: float | None = None
    deferred_acquisition_cost: float | None = None
    separate_account_business_assets: float | None = None
    noncurrent_note_receivables: float | None = None
    noncurrent_deferred_tax_assets: float | None = None
    employee_benefit_assets: float | None = None
    other_noncurrent_nonoperating_assets: float | None = None
    other_noncurrent_assets_excl_ppe: float | None = None
    total_noncurrent_assets: float | None = None
    noninterest_bearing_deposits: float | None = None
    interest_bearing_deposits: float | None = None
    fed_funds_purchased: float | None = None
    current_portion_of_long_term_debt: float | None = None
    bankers_acceptances: float | None = None
    accrued_interest_payable: float | None = None
    other_short_term_payables: float | None = None
    customer_deposits: float | None = None
    dividends_payable: float | None = None
    current_deferred_tax_liabilities: float | None = None
    current_employee_benefit_liabilities: float | None = None
    other_taxes_payable: float | None = None
    other_current_nonoperating_liabilities: float | None = None
    operating_lease_liability_current: float | None = None
    finance_lease_liability_current: float | None = None
    capital_lease_obligations: float | None = None
    operating_lease_liability_noncurrent: float | None = None
    claims_and_claim_expenses: float | None = None
    future_policy_benefits: float | None = None
    unearned_premiums_credit: float | None = None
    policyholder_funds: float | None = None
    participating_policyholder_equity: float | None = None
    separate_account_business_liabilities: float | None = None
    other_long_term_liabilities: float | None = None
    asset_retirement_and_litigation_obligation: float | None = None
    noncurrent_deferred_revenue: float | None = None
    noncurrent_deferred_tax_liabilities: float | None = None
    noncurrent_employee_benefit_liabilities: float | None = None
    other_noncurrent_nonoperating_liabilities: float | None = None
    commitments_and_contingencies: float | None = None
    temporary_equity: float | None = None
    temporary_equity_parent: float | None = None
    redeemable_noncontrolling_interest: float | None = None
    redeemable_nci_common: float | None = None
    redeemable_nci_preferred: float | None = None
    redeemable_nci_other: float | None = None
    total_preferred_equity: float | None = None
    common_equity: float | None = None
    other_equity: float | None = None
    noncontrolling_interests: float | None = None
    total_equity_and_noncontrolling_interests: float | None = None
    accumulated_other_comprehensive_income: float | None = None
    additional_paid_in_capital: float | None = None
    other_noncurrent_assets: float | None = None
    other_noncurrent_liabilities: float | None = None
    total_common_equity: float | None = None
    total_noncurrent_liabilities: float | None = None
    treasury_stock: float | None = None


class SecBalanceSheetStatement(SecStatementRow, _SecBalanceSheetPublicFields):
    """Public wide balance-sheet row for one SEC filing vintage."""

    cash_and_cash_equivalents: int | float | None = None
    short_term_investments: int | float | None = None
    cash_and_short_term_investments: int | float | None = None
    accounts_receivable: int | float | None = None
    inventory: int | float | None = None
    other_current_assets: int | float | None = None
    total_current_assets: int | float | None = None
    property_plant_and_equipment_net: int | float | None = None
    goodwill: int | float | None = None
    intangible_assets: int | float | None = None
    goodwill_and_intangible_assets: int | float | None = None
    long_term_investments: int | float | None = None
    tax_assets: int | float | None = None
    other_non_current_assets: int | float | None = None
    non_current_assets: int | float | None = None
    other_assets: int | float | None = None
    total_assets: int | float | None = None
    accounts_payable: int | float | None = None
    accrued_expenses: int | float | None = None
    short_term_debt: int | float | None = None
    tax_payables: int | float | None = None
    current_deferred_revenue: int | float | None = None
    other_current_liabilities: int | float | None = None
    total_current_liabilities: int | float | None = None
    long_term_debt: int | float | None = None
    other_non_current_liabilities: int | float | None = None
    total_liabilities: int | float | None = None
    retained_earnings: int | float | None = None
    total_stockholders_equity: int | float | None = None
    total_equity: int | float | None = None
    total_liabilities_and_equity: int | float | None = None


class _SecCashFlowPublicFields(ContractModel):
    """SEC-tab cash-flow columns; unsupported source concepts remain nullable."""

    net_income_discontinued: float | None = None
    net_income_continuing: float | None = None
    provision_for_loan_losses: float | None = None
    depreciation_expense: float | None = None
    amortization_expense: float | None = None
    gain_loss_on_investments: float | None = None
    gain_loss_on_sale_of_assets: float | None = None
    income_loss_from_equity_method_investments: float | None = None
    asset_impairment_charges: float | None = None
    noncash_adjustments_to_net_income: float | None = None
    other_operating_activities: float | None = None
    change_in_insurance_reserves: float | None = None
    change_in_accounts_receivable: float | None = None
    change_in_nontrade_receivables: float | None = None
    change_in_inventories: float | None = None
    change_in_accounts_payable: float | None = None
    change_in_other_operating_assets_and_liabilities: float | None = None
    increase_decrease_in_operating_capital: float | None = None
    net_cash_from_continuing_operating_activities: float | None = None
    net_cash_from_discontinued_operating_activities: float | None = None
    net_cash_from_operating_activities: float | None = None
    purchase_of_plant_property_and_equipment: float | None = None
    acquisitions: float | None = None
    purchase_of_investments: float | None = None
    purchase_of_held_to_maturity_investments: float | None = None
    sale_of_plant_property_and_equipment: float | None = None
    sale_of_productive_assets: float | None = None
    divestitures: float | None = None
    sale_of_investments: float | None = None
    maturity_of_investments: float | None = None
    net_increase_in_fed_funds_sold: float | None = None
    loans_held_for_sale_net: float | None = None
    other_investing_activities_net: float | None = None
    net_cash_from_continuing_investing_activities: float | None = None
    net_cash_from_discontinued_investing_activities: float | None = None
    net_cash_from_investing_activities: float | None = None
    repayment_of_debt: float | None = None
    repurchase_of_preferred_equity: float | None = None
    repurchase_of_common_equity: float | None = None
    payment_of_dividends: float | None = None
    issuance_of_debt: float | None = None
    issuance_of_preferred_equity: float | None = None
    issuance_of_common_equity: float | None = None
    net_change_in_deposits: float | None = None
    short_term_debt_net: float | None = None
    tax_withholding_share_based_compensation: float | None = None
    other_financing_activities_net: float | None = None
    net_cash_from_continuing_financing_activities: float | None = None
    net_cash_from_discontinued_financing_activities: float | None = None
    net_cash_from_financing_activities: float | None = None
    effect_of_exchange_rate_changes: float | None = None
    net_cash_from_discontinued_operations: float | None = None
    other_net_changes_in_cash: float | None = None
    cash_at_beginning_of_period: float | None = None
    cash_at_end_of_period: float | None = None
    cash_interest_paid: float | None = None
    cash_interest_received: float | None = None
    cash_income_taxes_paid: float | None = None


class SecCashFlowStatementRow(SecStatementRow, _SecCashFlowPublicFields):
    """Public wide cash-flow row for one SEC filing vintage."""

    period_beginning: date

    net_income: int | float | None = None
    depreciation_and_amortization: int | float | None = None
    deferred_income_tax: int | float | None = None
    stock_based_compensation: int | float | None = None
    change_in_working_capital: int | float | None = None
    accounts_receivable: int | float | None = None
    inventory: int | float | None = None
    accounts_payable: int | float | None = None
    operating_cash_flow: int | float | None = None
    capital_expenditure: int | float | None = None
    investing_cash_flow: int | float | None = None
    debt_issuance: int | float | None = None
    common_stock_issuance: int | float | None = None
    common_stock_repurchased: int | float | None = None
    dividends_paid: int | float | None = None
    financing_cash_flow: int | float | None = None
    net_change_in_cash: int | float | None = None
    free_cash_flow: int | float | None = None


class _SecIncomeStatementPublicFields(ContractModel):
    """SEC-tab income-statement columns; unsupported source concepts remain nullable."""

    operating_cost_of_revenue: float | None = None
    other_cost_of_revenue: float | None = None
    excise_and_sales_taxes: float | None = None
    sga_expense: float | None = None
    rd_expense: float | None = None
    exploration_expense: float | None = None
    depreciation_expense: float | None = None
    amortization_expense: float | None = None
    depletion_expense: float | None = None
    other_operating_expenses: float | None = None
    impairment_expense: float | None = None
    restructuring_charge: float | None = None
    other_special_charges: float | None = None
    costs_and_expenses: float | None = None
    loans_and_lease_interest_income: float | None = None
    investment_securities_interest_income: float | None = None
    deposits_interest_income: float | None = None
    fed_funds_and_repo_interest_income: float | None = None
    trading_account_interest_income: float | None = None
    other_interest_income: float | None = None
    total_interest_income: float | None = None
    deposits_interest_expense: float | None = None
    short_term_borrowing_interest_expense: float | None = None
    long_term_debt_interest_expense: float | None = None
    fed_funds_and_repo_interest_expense: float | None = None
    capitalized_lease_obligation_interest_expense: float | None = None
    other_interest_expense: float | None = None
    total_interest_expense: float | None = None
    net_interest_income: float | None = None
    revenues_excl_interest_dividends: float | None = None
    trust_fee_income: float | None = None
    service_charges_on_deposits_income: float | None = None
    other_service_charge_income: float | None = None
    net_realized_capital_gains: float | None = None
    premiums_earned: float | None = None
    investment_banking_income: float | None = None
    other_noninterest_income: float | None = None
    total_noninterest_income: float | None = None
    provision_for_credit_losses: float | None = None
    net_interest_income_after_provision: float | None = None
    benefits_costs_expenses: float | None = None
    current_and_future_benefits: float | None = None
    salaries_and_employee_benefits_expense: float | None = None
    net_occupancy_equipment_expense: float | None = None
    marketing_expense: float | None = None
    property_liability_insurance_claims: float | None = None
    policy_acquisition_costs: float | None = None
    amortization_of_deferred_policy_acquisition_costs: float | None = None
    total_noninterest_expense: float | None = None
    non_operating_income: float | None = None
    other_income: float | None = None
    other_gains: float | None = None
    total_other_income: float | None = None
    total_pretax_income: float | None = None
    income_tax_current: float | None = None
    income_tax_deferred: float | None = None
    income_before_equity_method: float | None = None
    equity_method_investments: float | None = None
    net_income_discontinued: float | None = None
    extraordinary_income: float | None = None
    other_adjustments_to_consolidated_net_income: float | None = None
    gain_on_sale_properties: float | None = None
    gain_loss_disposition_subsidiary: float | None = None
    preferred_dividends: float | None = None
    net_income_nci_redeemable: float | None = None
    net_income_nci_nonredeemable: float | None = None
    net_income_to_noncontrolling_interest: float | None = None
    other_adjustments_to_net_income_to_common: float | None = None
    net_income_to_common: float | None = None
    weighted_ave_basic_shares_os: float | None = None
    basic_eps: float | None = None
    weighted_ave_diluted_shares_os: float | None = None
    diluted_eps: float | None = None
    weighted_ave_basic_diluted_shares_os: float | None = None
    basic_diluted_eps: float | None = None
    cash_dividends_per_share: float | None = None
    other_comprehensive_income_parent: float | None = None
    comprehensive_income_parent: float | None = None
    other_comprehensive_income_nci: float | None = None
    comprehensive_income_nci: float | None = None
    comprehensive_income: float | None = None
    depreciation_and_amortization: float | None = None
    net_income_continuing: float | None = None
    other_revenue: float | None = None
    total_operating_income: float | None = None


class SecIncomeStatementRow(SecStatementRow, _SecIncomeStatementPublicFields):
    """Public wide income-statement row for one SEC filing vintage."""

    period_beginning: date

    revenue: int | float | None = None
    operating_revenue: int | float | None = None
    total_revenue: int | float | None = None
    cost_of_revenue: int | float | None = None
    total_cost_of_revenue: int | float | None = None
    gross_profit: int | float | None = None
    total_gross_profit: int | float | None = None
    general_and_admin_expense: int | float | None = None
    selling_general_and_admin_expense: int | float | None = None
    research_and_development_expense: int | float | None = None
    selling_and_marketing_expense: int | float | None = None
    total_operating_expenses: int | float | None = None
    operating_expenses: int | float | None = None
    operating_income: int | float | None = None
    interest_income: int | float | None = None
    interest_expense: int | float | None = None
    income_before_tax: int | float | None = None
    income_tax_expense: int | float | None = None
    net_income: int | float | None = None
    earnings_per_share_basic: int | float | None = None
    earnings_per_share_diluted: int | float | None = None
    weighted_average_shares_outstanding: int | float | None = None
    weighted_average_diluted_shares_outstanding: int | float | None = None


class SecStatementGrowthRow(ContractModel):
    """Common public growth metadata and exact-period SEC comparison extensions."""

    period_ending: date
    fiscal_period: str | None
    fiscal_year: int | None
    filing_date: date | None
    accepted_date: datetime | None = None
    cik: str
    symbol: str | None
    reported_currency: str | None
    period_beginning: date | None = None
    form: str | None = None
    accession: str
    sec_growth_facts: tuple["SecGrowthFactExtension", ...] = ()


class SecGrowthFactExtension(ContractModel):
    """One concept's current and comparison values, keeping units and vintage explicit."""

    tag: str
    unit: str
    current_value: Decimal
    prior_value: Decimal | None
    prior_period_ending: date | None
    prior_filed_date: date | None
    prior_accession: str | None
    growth: float | None


class _SecBalanceSheetGrowthPublicFields(ContractModel):
    """SEC-tab balance-sheet growth columns; uncomputable values remain null."""

    growth_cash_and_equivalents: float | None = None
    growth_restricted_cash: float | None = None
    growth_fed_funds_sold: float | None = None
    growth_interest_bearing_deposits_at_other_banks: float | None = None
    growth_time_deposits_placed: float | None = None
    growth_trading_account_securities: float | None = None
    growth_loans_and_leases: float | None = None
    growth_allowance_for_loan_and_lease_losses: float | None = None
    growth_net_loans_and_leases: float | None = None
    growth_loans_held_for_sale: float | None = None
    growth_accrued_investment_income: float | None = None
    growth_customer_and_other_receivables: float | None = None
    growth_note_receivable: float | None = None
    growth_nontrade_receivables: float | None = None
    growth_net_inventory: float | None = None
    growth_prepaid_expenses: float | None = None
    growth_current_deferred_tax_assets: float | None = None
    growth_other_current_nonoperating_assets: float | None = None
    growth_gross_ppe: float | None = None
    growth_accumulated_depreciation: float | None = None
    growth_net_ppe: float | None = None
    growth_net_premises_and_equipment: float | None = None
    growth_operating_lease_right_of_use_asset: float | None = None
    growth_finance_lease_right_of_use_asset: float | None = None
    growth_mortgage_servicing_rights: float | None = None
    growth_deferred_acquisition_cost: float | None = None
    growth_separate_account_business_assets: float | None = None
    growth_noncurrent_note_receivables: float | None = None
    growth_noncurrent_deferred_tax_assets: float | None = None
    growth_employee_benefit_assets: float | None = None
    growth_other_noncurrent_nonoperating_assets: float | None = None
    growth_other_noncurrent_assets_excl_ppe: float | None = None
    growth_total_noncurrent_assets: float | None = None
    growth_noninterest_bearing_deposits: float | None = None
    growth_interest_bearing_deposits: float | None = None
    growth_fed_funds_purchased: float | None = None
    growth_current_portion_of_long_term_debt: float | None = None
    growth_bankers_acceptances: float | None = None
    growth_accrued_interest_payable: float | None = None
    growth_other_short_term_payables: float | None = None
    growth_customer_deposits: float | None = None
    growth_dividends_payable: float | None = None
    growth_current_deferred_tax_liabilities: float | None = None
    growth_current_employee_benefit_liabilities: float | None = None
    growth_other_taxes_payable: float | None = None
    growth_other_current_nonoperating_liabilities: float | None = None
    growth_operating_lease_liability_current: float | None = None
    growth_finance_lease_liability_current: float | None = None
    growth_capital_lease_obligations: float | None = None
    growth_operating_lease_liability_noncurrent: float | None = None
    growth_claims_and_claim_expenses: float | None = None
    growth_future_policy_benefits: float | None = None
    growth_unearned_premiums_credit: float | None = None
    growth_policyholder_funds: float | None = None
    growth_participating_policyholder_equity: float | None = None
    growth_separate_account_business_liabilities: float | None = None
    growth_other_long_term_liabilities: float | None = None
    growth_asset_retirement_and_litigation_obligation: float | None = None
    growth_noncurrent_deferred_revenue: float | None = None
    growth_noncurrent_deferred_tax_liabilities: float | None = None
    growth_noncurrent_employee_benefit_liabilities: float | None = None
    growth_other_noncurrent_nonoperating_liabilities: float | None = None
    growth_commitments_and_contingencies: float | None = None
    growth_temporary_equity: float | None = None
    growth_temporary_equity_parent: float | None = None
    growth_redeemable_noncontrolling_interest: float | None = None
    growth_redeemable_nci_common: float | None = None
    growth_redeemable_nci_preferred: float | None = None
    growth_redeemable_nci_other: float | None = None
    growth_total_preferred_equity: float | None = None
    growth_common_equity: float | None = None
    growth_other_equity: float | None = None
    growth_noncontrolling_interests: float | None = None
    growth_total_equity_and_noncontrolling_interests: float | None = None
    growth_accrued_expenses: float | None = None
    growth_accumulated_other_comprehensive_income: float | None = None
    growth_additional_paid_in_capital: float | None = None
    growth_current_deferred_revenue: float | None = None
    growth_long_term_investments: float | None = None
    growth_other_assets: float | None = None
    growth_other_current_assets: float | None = None
    growth_other_current_liabilities: float | None = None
    growth_other_noncurrent_assets: float | None = None
    growth_other_noncurrent_liabilities: float | None = None
    growth_total_common_equity: float | None = None
    growth_total_liabilities_and_equity: float | None = None
    growth_total_noncurrent_liabilities: float | None = None
    growth_treasury_stock: float | None = None


class SecBalanceSheetGrowthRow(SecStatementGrowthRow, _SecBalanceSheetGrowthPublicFields):
    """Public balance-sheet growth row."""

    growth_cash_and_cash_equivalents: float | None = None
    growth_short_term_investments: float | None = None
    growth_accounts_receivable: float | None = None
    growth_inventory: float | None = None
    growth_total_current_assets: float | None = None
    growth_property_plant_and_equipment_net: float | None = None
    growth_goodwill: float | None = None
    growth_intangible_assets: float | None = None
    growth_total_assets: float | None = None
    growth_accounts_payable: float | None = None
    growth_short_term_debt: float | None = None
    growth_total_current_liabilities: float | None = None
    growth_long_term_debt: float | None = None
    growth_total_liabilities: float | None = None
    growth_retained_earnings: float | None = None
    growth_total_equity: float | None = None


class _SecCashFlowGrowthPublicFields(ContractModel):
    """SEC-tab cash-flow growth columns; uncomputable values remain null."""

    growth_net_income_discontinued: float | None = None
    growth_net_income_continuing: float | None = None
    growth_provision_for_loan_losses: float | None = None
    growth_depreciation_expense: float | None = None
    growth_amortization_expense: float | None = None
    growth_gain_loss_on_investments: float | None = None
    growth_gain_loss_on_sale_of_assets: float | None = None
    growth_income_loss_from_equity_method_investments: float | None = None
    growth_asset_impairment_charges: float | None = None
    growth_noncash_adjustments_to_net_income: float | None = None
    growth_other_operating_activities: float | None = None
    growth_change_in_insurance_reserves: float | None = None
    growth_change_in_accounts_receivable: float | None = None
    growth_change_in_nontrade_receivables: float | None = None
    growth_change_in_inventories: float | None = None
    growth_change_in_accounts_payable: float | None = None
    growth_change_in_other_operating_assets_and_liabilities: float | None = None
    growth_increase_decrease_in_operating_capital: float | None = None
    growth_net_cash_from_continuing_operating_activities: float | None = None
    growth_net_cash_from_discontinued_operating_activities: float | None = None
    growth_net_cash_from_operating_activities: float | None = None
    growth_purchase_of_plant_property_and_equipment: float | None = None
    growth_acquisitions: float | None = None
    growth_purchase_of_investments: float | None = None
    growth_purchase_of_held_to_maturity_investments: float | None = None
    growth_sale_of_plant_property_and_equipment: float | None = None
    growth_sale_of_productive_assets: float | None = None
    growth_divestitures: float | None = None
    growth_sale_of_investments: float | None = None
    growth_maturity_of_investments: float | None = None
    growth_net_increase_in_fed_funds_sold: float | None = None
    growth_loans_held_for_sale_net: float | None = None
    growth_other_investing_activities_net: float | None = None
    growth_net_cash_from_continuing_investing_activities: float | None = None
    growth_net_cash_from_discontinued_investing_activities: float | None = None
    growth_net_cash_from_investing_activities: float | None = None
    growth_repayment_of_debt: float | None = None
    growth_repurchase_of_preferred_equity: float | None = None
    growth_repurchase_of_common_equity: float | None = None
    growth_payment_of_dividends: float | None = None
    growth_issuance_of_debt: float | None = None
    growth_issuance_of_preferred_equity: float | None = None
    growth_issuance_of_common_equity: float | None = None
    growth_net_change_in_deposits: float | None = None
    growth_short_term_debt_net: float | None = None
    growth_net_short_term_borrowings: float | None = None
    growth_tax_withholding_share_based_compensation: float | None = None
    growth_other_financing_activities_net: float | None = None
    growth_net_cash_from_continuing_financing_activities: float | None = None
    growth_net_cash_from_discontinued_financing_activities: float | None = None
    growth_net_cash_from_financing_activities: float | None = None
    growth_effect_of_exchange_rate_changes: float | None = None
    growth_net_cash_from_discontinued_operations: float | None = None
    growth_other_net_changes_in_cash: float | None = None
    growth_cash_at_beginning_of_period: float | None = None
    growth_cash_at_end_of_period: float | None = None
    growth_cash_interest_paid: float | None = None
    growth_cash_interest_received: float | None = None
    growth_cash_income_taxes_paid: float | None = None


class SecCashFlowStatementGrowthRow(SecStatementGrowthRow, _SecCashFlowGrowthPublicFields):
    """Public cash-flow growth row."""

    period_beginning: date

    growth_net_income: float | None = None
    growth_depreciation_and_amortization: float | None = None
    growth_deferred_income_tax: float | None = None
    growth_stock_based_compensation: float | None = None
    growth_change_in_working_capital: float | None = None
    growth_operating_cash_flow: float | None = None
    growth_capital_expenditure: float | None = None
    growth_investing_cash_flow: float | None = None
    growth_financing_cash_flow: float | None = None
    growth_net_change_in_cash: float | None = None
    growth_free_cash_flow: float | None = None


class _SecIncomeStatementGrowthPublicFields(ContractModel):
    """SEC-tab income-statement growth columns; uncomputable values remain null."""

    growth_operating_cost_of_revenue: float | None = None
    growth_other_cost_of_revenue: float | None = None
    growth_excise_and_sales_taxes: float | None = None
    growth_sga_expense: float | None = None
    growth_rd_expense: float | None = None
    growth_exploration_expense: float | None = None
    growth_depreciation_expense: float | None = None
    growth_amortization_expense: float | None = None
    growth_depletion_expense: float | None = None
    growth_other_operating_expenses: float | None = None
    growth_impairment_expense: float | None = None
    growth_restructuring_charge: float | None = None
    growth_other_special_charges: float | None = None
    growth_costs_and_expenses: float | None = None
    growth_loans_and_lease_interest_income: float | None = None
    growth_investment_securities_interest_income: float | None = None
    growth_deposits_interest_income: float | None = None
    growth_fed_funds_and_repo_interest_income: float | None = None
    growth_trading_account_interest_income: float | None = None
    growth_other_interest_income: float | None = None
    growth_total_interest_income: float | None = None
    growth_deposits_interest_expense: float | None = None
    growth_short_term_borrowing_interest_expense: float | None = None
    growth_long_term_debt_interest_expense: float | None = None
    growth_fed_funds_and_repo_interest_expense: float | None = None
    growth_capitalized_lease_obligation_interest_expense: float | None = None
    growth_other_interest_expense: float | None = None
    growth_total_interest_expense: float | None = None
    growth_net_interest_income: float | None = None
    growth_revenues_excl_interest_dividends: float | None = None
    growth_trust_fee_income: float | None = None
    growth_service_charges_on_deposits_income: float | None = None
    growth_other_service_charge_income: float | None = None
    growth_net_realized_capital_gains: float | None = None
    growth_premiums_earned: float | None = None
    growth_investment_banking_income: float | None = None
    growth_other_noninterest_income: float | None = None
    growth_total_noninterest_income: float | None = None
    growth_provision_for_credit_losses: float | None = None
    growth_net_interest_income_after_provision: float | None = None
    growth_benefits_costs_expenses: float | None = None
    growth_current_and_future_benefits: float | None = None
    growth_salaries_and_employee_benefits_expense: float | None = None
    growth_net_occupancy_equipment_expense: float | None = None
    growth_marketing_expense: float | None = None
    growth_property_liability_insurance_claims: float | None = None
    growth_policy_acquisition_costs: float | None = None
    growth_amortization_of_deferred_policy_acquisition_costs: float | None = None
    growth_total_noninterest_expense: float | None = None
    growth_non_operating_income: float | None = None
    growth_other_income: float | None = None
    growth_other_gains: float | None = None
    growth_total_other_income: float | None = None
    growth_total_pretax_income: float | None = None
    growth_income_tax_current: float | None = None
    growth_income_tax_deferred: float | None = None
    growth_income_before_equity_method: float | None = None
    growth_equity_method_investments: float | None = None
    growth_net_income_discontinued: float | None = None
    growth_extraordinary_income: float | None = None
    growth_other_adjustments_to_consolidated_net_income: float | None = None
    growth_gain_on_sale_properties: float | None = None
    growth_gain_loss_disposition_subsidiary: float | None = None
    growth_preferred_dividends: float | None = None
    growth_net_income_nci_redeemable: float | None = None
    growth_net_income_nci_nonredeemable: float | None = None
    growth_net_income_to_noncontrolling_interest: float | None = None
    growth_other_adjustments_to_net_income_to_common: float | None = None
    growth_net_income_to_common: float | None = None
    growth_weighted_ave_basic_shares_os: float | None = None
    growth_basic_eps: float | None = None
    growth_weighted_ave_diluted_shares_os: float | None = None
    growth_diluted_eps: float | None = None
    growth_weighted_ave_basic_diluted_shares_os: float | None = None
    growth_basic_diluted_eps: float | None = None
    growth_cash_dividends_per_share: float | None = None
    growth_other_comprehensive_income_parent: float | None = None
    growth_comprehensive_income_parent: float | None = None
    growth_other_comprehensive_income_nci: float | None = None
    growth_comprehensive_income_nci: float | None = None
    growth_comprehensive_income: float | None = None
    growth_depreciation_and_amortization: float | None = None
    growth_net_income_continuing: float | None = None
    growth_operating_revenue: float | None = None
    growth_other_revenue: float | None = None
    growth_total_cost_of_revenue: float | None = None
    growth_total_gross_profit: float | None = None
    growth_total_operating_income: float | None = None
    growth_total_revenue: float | None = None


class SecIncomeStatementGrowthRow(SecStatementGrowthRow, _SecIncomeStatementGrowthPublicFields):
    """Public income-statement growth row."""

    period_beginning: date

    growth_revenue: float | None = None
    growth_cost_of_revenue: float | None = None
    growth_gross_profit: float | None = None
    growth_general_and_admin_expense: float | None = None
    growth_selling_general_and_admin_expense: float | None = None
    growth_research_and_development_expense: float | None = None
    growth_selling_and_marketing_expense: float | None = None
    growth_total_operating_expenses: float | None = None
    growth_operating_income: float | None = None
    growth_interest_expense: float | None = None
    growth_income_before_tax: float | None = None
    growth_income_tax_expense: float | None = None
    growth_net_income: float | None = None
    growth_earnings_per_share_basic: float | None = None
    growth_earnings_per_share_diluted: float | None = None


class SecCompanyFactComparison(ContractModel):
    """One SEC companyfacts observation projected onto the public comparison contract."""

    symbol: str | None
    name: str | None
    value: float
    reported_date: date | None
    period_beginning: date | None
    period_ending: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    cik: str
    location: str | None
    frame: str | None
    accession: str
    fact: str
    unit: str
    taxonomy: Literal["us-gaap"]
    tag: str

    @field_validator("value")
    @classmethod
    def _finite_value(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("value must be finite")
        return value


class SecFtdRecord(ContractModel):
    """Public FTD row with exact SEC source labels retained as extensions."""

    settlement_date: date
    symbol: str
    cusip: str
    quantity: int = Field(ge=0, strict=True)
    price: float | None
    description: str
    ticker: str
    quantity_fails: int = Field(ge=0, strict=True)
    issuer_name: str
    prior_close_price: Decimal | None

    @field_validator("price")
    @classmethod
    def _finite_public_price(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError("price must be finite and nonnegative")
        return value

    @field_validator("prior_close_price")
    @classmethod
    def _finite_price(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("prior close price must be finite and nonnegative")
        return value


class SecInsiderOwner(ContractModel):
    """One reporting owner identified in an SEC ownership filing."""

    cik: str
    name: str
    is_director: bool | None
    is_officer: bool | None
    is_ten_percent_owner: bool | None
    officer_title: str | None


class SecInsiderTransaction(ContractModel):
    """Public insider-trading row plus detailed SEC Form 4 traceability fields."""

    issuer_cik: str
    issuer_name: str
    company_cik: str
    symbol: str | None
    owner_cik: str | None
    owner_name: str | None
    owner_title: str | None
    ownership_type: Literal["D", "I"] | None
    transaction_type: str | None
    acquisition_or_disposition: Literal["A", "D"]
    securities_owned: float | None
    securities_transacted: float | None
    transaction_price: float | None
    filing_url: str
    owners: tuple[SecInsiderOwner, ...]
    filing_date: date
    report_date: date | None
    form: Literal["4", "4/A"]
    accession_number: str
    security_type: Literal["non_derivative", "derivative"]
    transaction_index: int = Field(ge=1, strict=True)
    report_line_number: int | None = None
    security_title: str
    transaction_date: date
    transaction_code: str | None
    shares: Decimal | None
    transaction_total_value: Decimal | None
    price_per_share: Decimal | None
    conversion_or_exercise_price: Decimal | None
    underlying_security_title: str | None
    acquired_disposed: Literal["A", "D"]
    shares_owned_after: Decimal | None
    direct_or_indirect: Literal["D", "I"] | None
    company_name: str
    other: bool | None = None
    other_text: str | None = None
    transaction_timeliness: str | None = None
    nature_of_ownership: str | None = None
    exercise_date: date | None = None
    expiration_date: date | None = None
    deemed_execution_date: date | None = None
    underlying_security_shares: Decimal | None = None
    underlying_security_value: Decimal | None = None
    conversion_exercise_price: Decimal | None = None
    transaction_value: Decimal | None = None
    value_owned: Decimal | None = None
    footnote: str | None = None
    director: bool | None = None
    officer: bool | None = None
    ten_percent_owner: bool | None = None

    @field_validator(
        "securities_owned",
        "securities_transacted",
        "transaction_price",
        "shares",
        "transaction_total_value",
        "price_per_share",
        "conversion_or_exercise_price",
        "shares_owned_after",
        "underlying_security_shares",
        "underlying_security_value",
        "conversion_exercise_price",
        "transaction_value",
        "value_owned",
    )
    @classmethod
    def _finite_numeric_values(cls, value: float | Decimal | None) -> float | Decimal | None:
        if value is None:
            return value
        finite = value.is_finite() if isinstance(value, Decimal) else math.isfinite(value)
        if not finite or value < 0:
            raise ValueError("ownership transaction numbers must be finite and nonnegative")
        return value

    @model_validator(mode="after")
    def _has_transaction_measure(self) -> SecInsiderTransaction:
        if self.shares is None and self.transaction_total_value is None:
            raise ValueError("transaction needs a reported share count or total value")
        return self


class SecSicCode(ContractModel):
    """Public SIC-search result with the SEC source labels retained for traceability."""

    sic: int
    industry: str
    office: str
    sic_code: int
    industry_title: str
