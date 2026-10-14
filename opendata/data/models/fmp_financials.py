"""Typed contracts for FMP's stable financial statement and metric APIs.

These contracts use the familiar period/metric vocabulary of financial
statement data while preserving the exact reported values and currencies from
FMP. They intentionally do not rescale amounts or convert growth ratios to
percentages.
"""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime
from typing import Annotated, Literal

from pydantic import AliasChoices, BeforeValidator, ConfigDict, Field, field_validator

from opendata.data.models.base import ContractModel

FinancialPeriod = Literal["FY", "Q1", "Q2", "Q3", "Q4"]


def _source_number(value: object) -> object:
    """Accept only finite JSON numeric values, preserving their source scale."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("FMP financial values must be JSON numbers or null")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError("FMP financial values must be finite")
    return value


FinancialValue = Annotated[int | float | None, BeforeValidator(_source_number)]


def _source_statement_amount(value: object) -> object:
    """Keep statement amounts integral, as documented by the public model."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("FMP statement amounts must be JSON integers or null")
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("FMP statement amounts must be integral JSON numbers")
        return int(value)
    return value


StatementAmount = Annotated[int | None, BeforeValidator(_source_statement_amount)]
RatioValue = Annotated[float | None, BeforeValidator(_source_number)]


def _source_segment_revenue(value: object) -> object:
    """Require a finite numeric segment amount, as the public model does."""
    if value is None:
        raise ValueError("FMP segment revenue must be a JSON number")
    return _source_number(value)


SegmentRevenue = Annotated[int | float, BeforeValidator(_source_segment_revenue)]


def _year(value: object) -> object:
    """Normalize a four-digit FMP fiscal year without permissive coercion."""
    if isinstance(value, bool):
        raise ValueError("fiscalYear must be a four-digit year")
    if isinstance(value, int):
        year = value
    elif isinstance(value, str) and re.fullmatch(r"\d{4}", value):
        year = int(value)
    else:
        raise ValueError("fiscalYear must be a four-digit year")
    if not 1800 <= year <= 2200:
        raise ValueError("fiscalYear is outside the supported calendar range")
    return year


def _currency_code(value: str | None) -> str | None:
    """Validate an optional ISO-like source currency code without coercion."""
    if value is not None and not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("currency must be a three-letter code or null")
    return value


class FMPFinancialPeriod(ContractModel):
    """Shared period identity present in the stable financial APIs."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str | None
    period_ending: date_type | None = Field(alias="date")
    fiscal_year: int | None = Field(alias="fiscalYear")
    fiscal_period: FinancialPeriod | None = Field(alias="period")

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not normalized or any(ord(char) < 32 or ord(char) == 127 for char in normalized):
            raise ValueError("symbol must be a nonblank token")
        return normalized

    @field_validator("period_ending", mode="before")
    @classmethod
    def _valid_period_ending(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            raise ValueError("date must not include a time")
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            try:
                parsed = date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date must be an exact ISO date") from exc
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("date must be an exact ISO date")

    @field_validator("fiscal_year", mode="before")
    @classmethod
    def _valid_fiscal_year(cls, value: object) -> object:
        if value is None:
            return None
        return _year(value)


class FMPReportedPeriod(FMPFinancialPeriod):
    """Period metadata for source models whose public contract uses reported currency."""

    reported_currency: str | None = Field(alias="reportedCurrency")

    @field_validator("reported_currency")
    @classmethod
    def _valid_reported_currency(cls, value: str | None) -> str | None:
        return _currency_code(value)


class FMPStatementPeriod(FMPReportedPeriod):
    """Additional filing lineage supplied by FMP statement endpoints."""

    cik: str | None = None
    filing_date: date_type | None = Field(default=None, alias="filingDate")
    accepted_date: datetime | None = Field(default=None, alias="acceptedDate")

    @field_validator("filing_date", mode="before")
    @classmethod
    def _valid_filing_date(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            raise ValueError("filingDate must not include a time")
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            try:
                parsed = date_type.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("filingDate must be an exact ISO date") from exc
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("filingDate must be an exact ISO date")

    @field_validator("accepted_date", mode="before")
    @classmethod
    def _valid_accepted_date(cls, value: object) -> object:
        if value is None or isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            except ValueError as exc:
                raise ValueError("acceptedDate must use YYYY-MM-DD HH:MM:SS") from exc
            if parsed.strftime("%Y-%m-%d %H:%M:%S") == value:
                return parsed
        raise ValueError("acceptedDate must use YYYY-MM-DD HH:MM:SS")


class BalanceSheet(FMPStatementPeriod):
    """One balance sheet, with each documented source line item typed."""

    cash_and_cash_equivalents: StatementAmount = Field(default=None, alias="cashAndCashEquivalents")
    short_term_investments: StatementAmount = Field(default=None, alias="shortTermInvestments")
    cash_and_short_term_investments: StatementAmount = Field(
        default=None, alias="cashAndShortTermInvestments"
    )
    net_receivables: StatementAmount = Field(default=None, alias="netReceivables")
    accounts_receivables: StatementAmount = Field(default=None, alias="accountsReceivables")
    other_receivables: StatementAmount = Field(default=None, alias="otherReceivables")
    inventory: StatementAmount = None
    prepaid_expenses: StatementAmount = Field(default=None, alias="prepaids")
    other_current_assets: StatementAmount = Field(default=None, alias="otherCurrentAssets")
    total_current_assets: StatementAmount = Field(default=None, alias="totalCurrentAssets")
    plant_property_equipment_net: StatementAmount = Field(
        default=None, alias="propertyPlantEquipmentNet"
    )
    goodwill: StatementAmount = None
    intangible_assets: StatementAmount = Field(default=None, alias="intangibleAssets")
    goodwill_and_intangible_assets: StatementAmount = Field(
        default=None, alias="goodwillAndIntangibleAssets"
    )
    long_term_investments: StatementAmount = Field(default=None, alias="longTermInvestments")
    tax_assets: StatementAmount = Field(default=None, alias="taxAssets")
    other_non_current_assets: StatementAmount = Field(default=None, alias="otherNonCurrentAssets")
    non_current_assets: StatementAmount = Field(default=None, alias="totalNonCurrentAssets")
    other_assets: StatementAmount = Field(default=None, alias="otherAssets")
    total_assets: StatementAmount = Field(default=None, alias="totalAssets")
    total_payables: StatementAmount = Field(default=None, alias="totalPayables")
    accounts_payable: StatementAmount = Field(default=None, alias="accountPayables")
    other_payables: StatementAmount = Field(default=None, alias="otherPayables")
    accrued_expenses: StatementAmount = Field(default=None, alias="accruedExpenses")
    short_term_debt: StatementAmount = Field(default=None, alias="shortTermDebt")
    capital_lease_obligations_current: StatementAmount = Field(
        default=None, alias="capitalLeaseObligationsCurrent"
    )
    tax_payables: StatementAmount = Field(default=None, alias="taxPayables")
    current_deferred_revenue: StatementAmount = Field(default=None, alias="deferredRevenue")
    other_current_liabilities: StatementAmount = Field(
        default=None, alias="otherCurrentLiabilities"
    )
    total_current_liabilities: StatementAmount = Field(
        default=None, alias="totalCurrentLiabilities"
    )
    long_term_debt: StatementAmount = Field(default=None, alias="longTermDebt")
    capital_lease_obligations_non_current: StatementAmount = Field(
        default=None, alias="capitalLeaseObligationsNonCurrent"
    )
    deferred_revenue_non_current: StatementAmount = Field(
        default=None, alias="deferredRevenueNonCurrent"
    )
    deferred_tax_liabilities_non_current: StatementAmount = Field(
        default=None, alias="deferredTaxLiabilitiesNonCurrent"
    )
    other_non_current_liabilities: StatementAmount = Field(
        default=None, alias="otherNonCurrentLiabilities"
    )
    total_non_current_liabilities: StatementAmount = Field(
        default=None, alias="totalNonCurrentLiabilities"
    )
    other_liabilities: StatementAmount = Field(default=None, alias="otherLiabilities")
    capital_lease_obligations: StatementAmount = Field(
        default=None, alias="capitalLeaseObligations"
    )
    total_liabilities: StatementAmount = Field(default=None, alias="totalLiabilities")
    treasury_stock: StatementAmount = Field(default=None, alias="treasuryStock")
    preferred_stock: StatementAmount = Field(default=None, alias="preferredStock")
    common_stock: StatementAmount = Field(default=None, alias="commonStock")
    retained_earnings: StatementAmount = Field(default=None, alias="retainedEarnings")
    additional_paid_in_capital: StatementAmount = Field(
        default=None, alias="additionalPaidInCapital"
    )
    accumulated_other_comprehensive_income: StatementAmount = Field(
        default=None, alias="accumulatedOtherComprehensiveIncomeLoss"
    )
    # FMP's documented statement sample has no separate other-shareholders line.
    other_shareholders_equity: StatementAmount = None
    other_total_shareholders_equity: StatementAmount = Field(
        default=None,
        validation_alias=AliasChoices(
            "otherTotalStockholdersEquity", "othertotalStockholdersEquity"
        ),
    )
    total_common_equity: StatementAmount = Field(default=None, alias="totalStockholdersEquity")
    total_equity_non_controlling_interests: StatementAmount = Field(
        default=None, alias="totalEquity"
    )
    minority_interest: StatementAmount = Field(default=None, alias="minorityInterest")
    total_liabilities_and_shareholders_equity: StatementAmount = Field(
        default=None, alias="totalLiabilitiesAndStockholdersEquity"
    )
    total_liabilities_and_total_equity: StatementAmount = Field(
        default=None, alias="totalLiabilitiesAndTotalEquity"
    )
    total_investments: StatementAmount = Field(default=None, alias="totalInvestments")
    total_debt: StatementAmount = Field(default=None, alias="totalDebt")
    net_debt: StatementAmount = Field(default=None, alias="netDebt")


class BalanceSheetGrowth(FMPReportedPeriod):
    """Year-over-year balance-sheet changes; FMP growth ratios stay fractional."""

    growth_cash_and_cash_equivalents: RatioValue = Field(
        default=None, alias="growthCashAndCashEquivalents"
    )
    growth_short_term_investments: RatioValue = Field(
        default=None, alias="growthShortTermInvestments"
    )
    growth_cash_and_short_term_investments: RatioValue = Field(
        default=None, alias="growthCashAndShortTermInvestments"
    )
    growth_net_receivables: RatioValue = Field(default=None, alias="growthNetReceivables")
    growth_inventory: RatioValue = Field(default=None, alias="growthInventory")
    growth_other_current_assets: RatioValue = Field(default=None, alias="growthOtherCurrentAssets")
    growth_total_current_assets: RatioValue = Field(default=None, alias="growthTotalCurrentAssets")
    growth_property_plant_equipment_net: RatioValue = Field(
        default=None, alias="growthPropertyPlantEquipmentNet"
    )
    growth_goodwill: RatioValue = Field(default=None, alias="growthGoodwill")
    growth_intangible_assets: RatioValue = Field(default=None, alias="growthIntangibleAssets")
    growth_goodwill_and_intangible_assets: RatioValue = Field(
        default=None, alias="growthGoodwillAndIntangibleAssets"
    )
    growth_long_term_investments: RatioValue = Field(
        default=None, alias="growthLongTermInvestments"
    )
    growth_tax_assets: RatioValue = Field(default=None, alias="growthTaxAssets")
    growth_other_non_current_assets: RatioValue = Field(
        default=None, alias="growthOtherNonCurrentAssets"
    )
    growth_total_non_current_assets: RatioValue = Field(
        default=None, alias="growthTotalNonCurrentAssets"
    )
    growth_other_assets: RatioValue = Field(default=None, alias="growthOtherAssets")
    growth_total_assets: RatioValue = Field(default=None, alias="growthTotalAssets")
    growth_account_payables: RatioValue = Field(default=None, alias="growthAccountPayables")
    growth_short_term_debt: RatioValue = Field(default=None, alias="growthShortTermDebt")
    growth_tax_payables: RatioValue = Field(default=None, alias="growthTaxPayables")
    growth_deferred_revenue: RatioValue = Field(default=None, alias="growthDeferredRevenue")
    growth_other_current_liabilities: RatioValue = Field(
        default=None, alias="growthOtherCurrentLiabilities"
    )
    growth_total_current_liabilities: RatioValue = Field(
        default=None, alias="growthTotalCurrentLiabilities"
    )
    growth_long_term_debt: RatioValue = Field(default=None, alias="growthLongTermDebt")
    growth_deferred_revenue_non_current: RatioValue = Field(
        default=None, alias="growthDeferredRevenueNonCurrent"
    )
    growth_deferred_tax_liabilities_non_current: RatioValue = Field(
        default=None, alias="growthDeferredTaxLiabilitiesNonCurrent"
    )
    growth_other_non_current_liabilities: RatioValue = Field(
        default=None, alias="growthOtherNonCurrentLiabilities"
    )
    growth_total_non_current_liabilities: RatioValue = Field(
        default=None, alias="growthTotalNonCurrentLiabilities"
    )
    growth_other_liabilities: RatioValue = Field(default=None, alias="growthOtherLiabilities")
    growth_total_liabilities: RatioValue = Field(default=None, alias="growthTotalLiabilities")
    growth_preferred_stock: RatioValue = Field(default=None, alias="growthPreferredStock")
    growth_common_stock: RatioValue = Field(default=None, alias="growthCommonStock")
    growth_retained_earnings: RatioValue = Field(default=None, alias="growthRetainedEarnings")
    growth_accumulated_other_comprehensive_income: RatioValue = Field(
        default=None, alias="growthAccumulatedOtherComprehensiveIncomeLoss"
    )
    growth_other_total_shareholders_equity: RatioValue = Field(
        default=None, alias="growthOthertotalStockholdersEquity"
    )
    growth_total_shareholders_equity: RatioValue = Field(
        default=None, alias="growthTotalStockholdersEquity"
    )
    growth_minority_interest: RatioValue = Field(default=None, alias="growthMinorityInterest")
    growth_total_equity: RatioValue = Field(default=None, alias="growthTotalEquity")
    growth_total_liabilities_and_shareholders_equity: RatioValue = Field(
        default=None, alias="growthTotalLiabilitiesAndStockholdersEquity"
    )
    growth_total_investments: RatioValue = Field(default=None, alias="growthTotalInvestments")
    growth_total_debt: RatioValue = Field(default=None, alias="growthTotalDebt")
    growth_net_debt: RatioValue = Field(default=None, alias="growthNetDebt")
    growth_accounts_receivables: RatioValue = Field(default=None, alias="growthAccountsReceivables")
    growth_other_receivables: RatioValue = Field(default=None, alias="growthOtherReceivables")
    growth_prepaid_expenses: RatioValue = Field(default=None, alias="growthPrepaids")
    growth_total_payables: RatioValue = Field(default=None, alias="growthTotalPayables")
    growth_other_payables: RatioValue = Field(default=None, alias="growthOtherPayables")
    growth_accrued_expenses: RatioValue = Field(default=None, alias="growthAccruedExpenses")
    growth_capital_lease_obligations_current: RatioValue = Field(
        default=None, alias="growthCapitalLeaseObligationsCurrent"
    )
    growth_additional_paid_in_capital: RatioValue = Field(
        default=None, alias="growthAdditionalPaidInCapital"
    )
    growth_treasury_stock: RatioValue = Field(default=None, alias="growthTreasuryStock")


class CashFlowStatement(FMPStatementPeriod):
    """One cash flow statement, with operating/investing/financing flows typed."""

    net_income: StatementAmount = Field(default=None, alias="netIncome")
    depreciation_and_amortization: StatementAmount = Field(
        default=None, alias="depreciationAndAmortization"
    )
    deferred_income_tax: StatementAmount = Field(default=None, alias="deferredIncomeTax")
    stock_based_compensation: StatementAmount = Field(default=None, alias="stockBasedCompensation")
    change_in_working_capital: StatementAmount = Field(default=None, alias="changeInWorkingCapital")
    change_in_account_receivables: StatementAmount = Field(
        default=None, alias="accountsReceivables"
    )
    change_in_inventory: StatementAmount = Field(default=None, alias="inventory")
    change_in_account_payable: StatementAmount = Field(default=None, alias="accountsPayables")
    change_in_other_working_capital: StatementAmount = Field(
        default=None, alias="otherWorkingCapital"
    )
    change_in_other_non_cash_items: StatementAmount = Field(default=None, alias="otherNonCashItems")
    net_cash_from_operating_activities: StatementAmount = Field(
        default=None, alias="netCashProvidedByOperatingActivities"
    )
    purchase_of_property_plant_and_equipment: StatementAmount = Field(
        default=None, alias="investmentsInPropertyPlantAndEquipment"
    )
    acquisitions: StatementAmount = Field(default=None, alias="acquisitionsNet")
    purchase_of_investment_securities: StatementAmount = Field(
        default=None, alias="purchasesOfInvestments"
    )
    sale_and_maturity_of_investments: StatementAmount = Field(
        default=None, alias="salesMaturitiesOfInvestments"
    )
    other_investing_activities: StatementAmount = Field(
        default=None, alias="otherInvestingActivities"
    )
    net_cash_from_investing_activities: StatementAmount = Field(
        default=None, alias="netCashProvidedByInvestingActivities"
    )
    # FMP's stable response uses net-debt issuance but documents no debt-repayment key.
    # Do not infer repayment from netDebtIssuance; it is a distinct flow.
    repayment_of_debt: StatementAmount = None
    net_debt_issuance: StatementAmount = Field(default=None, alias="netDebtIssuance")
    long_term_net_debt_issuance: StatementAmount = Field(
        default=None, alias="longTermNetDebtIssuance"
    )
    short_term_net_debt_issuance: StatementAmount = Field(
        default=None, alias="shortTermNetDebtIssuance"
    )
    net_equity_issuance: StatementAmount = Field(default=None, alias="netStockIssuance")
    net_common_equity_issuance: StatementAmount = Field(
        default=None, alias="netCommonStockIssuance"
    )
    issuance_of_common_equity: StatementAmount = Field(default=None, alias="commonStockIssuance")
    repurchase_of_common_equity: StatementAmount = Field(
        default=None, alias="commonStockRepurchased"
    )
    net_preferred_equity_issuance: StatementAmount = Field(
        default=None, alias="netPreferredStockIssuance"
    )
    net_dividends_paid: StatementAmount = Field(default=None, alias="netDividendsPaid")
    common_dividends_paid: StatementAmount = Field(default=None, alias="commonDividendsPaid")
    preferred_dividends_paid: StatementAmount = Field(default=None, alias="preferredDividendsPaid")
    other_financing_activities: StatementAmount = Field(
        default=None, alias="otherFinancingActivities"
    )
    net_cash_from_financing_activities: StatementAmount = Field(
        default=None, alias="netCashProvidedByFinancingActivities"
    )
    effect_of_exchange_rate_changes_on_cash: StatementAmount = Field(
        default=None, alias="effectOfForexChangesOnCash"
    )
    net_change_in_cash_and_equivalents: StatementAmount = Field(
        default=None, alias="netChangeInCash"
    )
    cash_at_end_of_period: StatementAmount = Field(default=None, alias="cashAtEndOfPeriod")
    cash_at_beginning_of_period: StatementAmount = Field(
        default=None, alias="cashAtBeginningOfPeriod"
    )
    operating_cash_flow: StatementAmount = Field(default=None, alias="operatingCashFlow")
    capital_expenditure: StatementAmount = Field(default=None, alias="capitalExpenditure")
    free_cash_flow: StatementAmount = Field(default=None, alias="freeCashFlow")
    income_taxes_paid: StatementAmount = Field(default=None, alias="incomeTaxesPaid")
    interest_paid: StatementAmount = Field(default=None, alias="interestPaid")


class CashFlowStatementGrowth(FMPReportedPeriod):
    """Period growth in reported cash flow fields; ratios are not percentage-scaled."""

    growth_net_income: RatioValue = Field(default=None, alias="growthNetIncome")
    growth_depreciation_and_amortization: RatioValue = Field(
        default=None, alias="growthDepreciationAndAmortization"
    )
    growth_deferred_income_tax: RatioValue = Field(default=None, alias="growthDeferredIncomeTax")
    growth_stock_based_compensation: RatioValue = Field(
        default=None, alias="growthStockBasedCompensation"
    )
    growth_change_in_working_capital: RatioValue = Field(
        default=None, alias="growthChangeInWorkingCapital"
    )
    growth_account_receivables: RatioValue = Field(default=None, alias="growthAccountsReceivables")
    growth_inventory: RatioValue = Field(default=None, alias="growthInventory")
    growth_account_payable: RatioValue = Field(default=None, alias="growthAccountsPayables")
    growth_other_working_capital: RatioValue = Field(
        default=None, alias="growthOtherWorkingCapital"
    )
    growth_other_non_cash_items: RatioValue = Field(default=None, alias="growthOtherNonCashItems")
    growth_net_cash_from_operating_activities: RatioValue = Field(
        default=None, alias="growthNetCashProvidedByOperatingActivites"
    )
    growth_purchase_of_property_plant_and_equipment: RatioValue = Field(
        default=None, alias="growthInvestmentsInPropertyPlantAndEquipment"
    )
    growth_acquisitions: RatioValue = Field(default=None, alias="growthAcquisitionsNet")
    growth_purchase_of_investment_securities: RatioValue = Field(
        default=None, alias="growthPurchasesOfInvestments"
    )
    growth_sale_and_maturity_of_investments: RatioValue = Field(
        default=None, alias="growthSalesMaturitiesOfInvestments"
    )
    growth_other_investing_activities: RatioValue = Field(
        default=None, alias="growthOtherInvestingActivites"
    )
    growth_net_cash_from_investing_activities: RatioValue = Field(
        default=None, alias="growthNetCashUsedForInvestingActivites"
    )
    growth_repayment_of_debt: RatioValue = Field(default=None, alias="growthDebtRepayment")
    growth_common_equity_issuance: RatioValue = Field(default=None, alias="growthCommonStockIssued")
    growth_common_equity_repurchased: RatioValue = Field(
        default=None, alias="growthCommonStockRepurchased"
    )
    growth_dividends_paid: RatioValue = Field(default=None, alias="growthDividendsPaid")
    growth_other_financing_activities: RatioValue = Field(
        default=None, alias="growthOtherFinancingActivites"
    )
    growth_net_cash_from_financing_activities: RatioValue = Field(
        default=None, alias="growthNetCashUsedProvidedByFinancingActivities"
    )
    growth_effect_of_exchange_rate_changes_on_cash: RatioValue = Field(
        default=None, alias="growthEffectOfForexChangesOnCash"
    )
    growth_net_change_in_cash_and_equivalents: RatioValue = Field(
        default=None, alias="growthNetChangeInCash"
    )
    growth_cash_at_end_of_period: RatioValue = Field(default=None, alias="growthCashAtEndOfPeriod")
    growth_cash_at_beginning_of_period: RatioValue = Field(
        default=None, alias="growthCashAtBeginningOfPeriod"
    )
    growth_operating_cash_flow: RatioValue = Field(default=None, alias="growthOperatingCashFlow")
    growth_capital_expenditure: RatioValue = Field(default=None, alias="growthCapitalExpenditure")
    growth_free_cash_flow: RatioValue = Field(default=None, alias="growthFreeCashFlow")
    growth_net_debt_issuance: RatioValue = Field(default=None, alias="growthNetDebtIssuance")
    growth_long_term_net_debt_issuance: RatioValue = Field(
        default=None, alias="growthLongTermNetDebtIssuance"
    )
    growth_short_term_net_debt_issuance: RatioValue = Field(
        default=None, alias="growthShortTermNetDebtIssuance"
    )
    growth_net_equity_issuance: RatioValue = Field(default=None, alias="growthNetStockIssuance")
    growth_preferred_dividends_paid: RatioValue = Field(
        default=None, alias="growthPreferredDividendsPaid"
    )
    growth_income_taxes_paid: RatioValue = Field(default=None, alias="growthIncomeTaxesPaid")
    growth_interest_paid: RatioValue = Field(default=None, alias="growthInterestPaid")


class IncomeStatement(FMPStatementPeriod):
    """One income statement with each documented reported line item typed."""

    revenue: StatementAmount = None
    cost_of_revenue: StatementAmount = Field(default=None, alias="costOfRevenue")
    gross_profit: StatementAmount = Field(default=None, alias="grossProfit")
    research_and_development_expense: StatementAmount = Field(
        default=None, alias="researchAndDevelopmentExpenses"
    )
    general_and_admin_expense: StatementAmount = Field(
        default=None, alias="generalAndAdministrativeExpenses"
    )
    selling_and_marketing_expense: StatementAmount = Field(
        default=None, alias="sellingAndMarketingExpenses"
    )
    selling_general_and_admin_expense: StatementAmount = Field(
        default=None, alias="sellingGeneralAndAdministrativeExpenses"
    )
    other_expenses: StatementAmount = Field(default=None, alias="otherExpenses")
    total_operating_expenses: StatementAmount = Field(default=None, alias="operatingExpenses")
    cost_and_expenses: StatementAmount = Field(default=None, alias="costAndExpenses")
    net_interest_income: StatementAmount = Field(default=None, alias="netInterestIncome")
    interest_income: StatementAmount = Field(default=None, alias="interestIncome")
    total_interest_expense: StatementAmount = Field(default=None, alias="interestExpense")
    depreciation_and_amortization: StatementAmount = Field(
        default=None, alias="depreciationAndAmortization"
    )
    ebitda: StatementAmount = None
    ebit: StatementAmount = None
    non_operating_income_excluding_interest: StatementAmount = Field(
        default=None, alias="nonOperatingIncomeExcludingInterest"
    )
    total_operating_income: StatementAmount = Field(default=None, alias="operatingIncome")
    total_other_income_expenses: StatementAmount = Field(
        default=None, alias="totalOtherIncomeExpensesNet"
    )
    total_pre_tax_income: StatementAmount = Field(default=None, alias="incomeBeforeTax")
    income_tax_expense: StatementAmount = Field(default=None, alias="incomeTaxExpense")
    net_income_from_continuing_operations: StatementAmount = Field(
        default=None, alias="netIncomeFromContinuingOperations"
    )
    net_income_from_discontinued_operations: StatementAmount = Field(
        default=None, alias="netIncomeFromDiscontinuedOperations"
    )
    other_adjustments_to_net_income: StatementAmount = Field(
        default=None, alias="otherAdjustmentsToNetIncome"
    )
    consolidated_net_income: StatementAmount = Field(default=None, alias="netIncome")
    net_income_deductions: StatementAmount = Field(default=None, alias="netIncomeDeductions")
    bottom_line_net_income: StatementAmount = Field(default=None, alias="bottomLineNetIncome")
    basic_earnings_per_share: RatioValue = Field(default=None, alias="eps")
    diluted_earnings_per_share: RatioValue = Field(default=None, alias="epsDiluted")
    weighted_average_basic_shares_outstanding: StatementAmount = Field(
        default=None, alias="weightedAverageShsOut"
    )
    weighted_average_diluted_shares_outstanding: StatementAmount = Field(
        default=None, alias="weightedAverageShsOutDil"
    )


class IncomeStatementGrowth(FMPReportedPeriod):
    """Income statement growth records with source decimal values preserved."""

    growth_revenue: RatioValue = Field(default=None, alias="growthRevenue")
    growth_cost_of_revenue: RatioValue = Field(default=None, alias="growthCostOfRevenue")
    growth_gross_profit: RatioValue = Field(default=None, alias="growthGrossProfit")
    growth_gross_profit_margin: RatioValue = Field(default=None, alias="growthGrossProfitRatio")
    growth_research_and_development_expense: RatioValue = Field(
        default=None, alias="growthResearchAndDevelopmentExpenses"
    )
    growth_general_and_admin_expense: RatioValue = Field(
        default=None, alias="growthGeneralAndAdministrativeExpenses"
    )
    growth_selling_and_marketing_expense: RatioValue = Field(
        default=None, alias="growthSellingAndMarketingExpenses"
    )
    growth_other_expenses: RatioValue = Field(default=None, alias="growthOtherExpenses")
    growth_operating_expenses: RatioValue = Field(default=None, alias="growthOperatingExpenses")
    growth_cost_and_expenses: RatioValue = Field(default=None, alias="growthCostAndExpenses")
    growth_interest_income: RatioValue = Field(default=None, alias="growthInterestIncome")
    growth_interest_expense: RatioValue = Field(default=None, alias="growthInterestExpense")
    growth_depreciation_and_amortization: RatioValue = Field(
        default=None, alias="growthDepreciationAndAmortization"
    )
    growth_ebitda: RatioValue = Field(default=None, alias="growthEBITDA")
    growth_ebit: RatioValue = Field(default=None, alias="growthEBIT")
    growth_operating_income: RatioValue = Field(default=None, alias="growthOperatingIncome")
    growth_income_before_tax: RatioValue = Field(default=None, alias="growthIncomeBeforeTax")
    growth_income_tax_expense: RatioValue = Field(default=None, alias="growthIncomeTaxExpense")
    growth_consolidated_net_income: RatioValue = Field(default=None, alias="growthNetIncome")
    growth_basic_earings_per_share: RatioValue = Field(default=None, alias="growthEPS")
    growth_diluted_earnings_per_share: RatioValue = Field(default=None, alias="growthEPSDiluted")
    growth_weighted_average_basic_shares_outstanding: RatioValue = Field(
        default=None, alias="growthWeightedAverageShsOut"
    )
    growth_weighted_average_diluted_shares_outstanding: RatioValue = Field(
        default=None, alias="growthWeightedAverageShsOutDil"
    )
    growth_non_operating_income_excluding_interest: RatioValue = Field(
        default=None, alias="growthNonOperatingIncomeExcludingInterest"
    )
    growth_net_interest_income: RatioValue = Field(default=None, alias="growthNetInterestIncome")
    growth_total_other_income_expenses_net: RatioValue = Field(
        default=None, alias="growthTotalOtherIncomeExpensesNet"
    )
    growth_net_income_from_continuing_operations: RatioValue = Field(
        default=None, alias="growthNetIncomeFromContinuingOperations"
    )
    growth_other_adjustments_to_net_income: RatioValue = Field(
        default=None, alias="growthOtherAdjustmentsToNetIncome"
    )
    growth_net_income_deductions: RatioValue = Field(
        default=None, alias="growthNetIncomeDeductions"
    )


class FinancialRatios(FMPFinancialPeriod):
    """FMP's period-aligned financial ratio data, with numeric ratios typed."""

    currency: str | None = Field(default=None, alias="reportedCurrency")

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str | None) -> str | None:
        return _currency_code(value)

    gross_profit_margin: RatioValue = Field(default=None, alias="grossProfitMargin")
    ebit_margin: RatioValue = Field(default=None, alias="ebitMargin")
    ebitda_margin: RatioValue = Field(default=None, alias="ebitdaMargin")
    operating_profit_margin: RatioValue = Field(default=None, alias="operatingProfitMargin")
    pretax_profit_margin: RatioValue = Field(default=None, alias="pretaxProfitMargin")
    continuous_operations_profit_margin: RatioValue = Field(
        default=None, alias="continuousOperationsProfitMargin"
    )
    net_profit_margin: RatioValue = Field(default=None, alias="netProfitMargin")
    bottom_line_profit_margin: RatioValue = Field(default=None, alias="bottomLineProfitMargin")
    receivables_turnover: RatioValue = Field(default=None, alias="receivablesTurnover")
    payables_turnover: RatioValue = Field(default=None, alias="payablesTurnover")
    inventory_turnover: RatioValue = Field(default=None, alias="inventoryTurnover")
    fixed_asset_turnover: RatioValue = Field(default=None, alias="fixedAssetTurnover")
    asset_turnover: RatioValue = Field(default=None, alias="assetTurnover")
    current_ratio: RatioValue = Field(default=None, alias="currentRatio")
    quick_ratio: RatioValue = Field(default=None, alias="quickRatio")
    solvency_ratio: RatioValue = Field(default=None, alias="solvencyRatio")
    cash_ratio: RatioValue = Field(default=None, alias="cashRatio")
    price_to_earnings: RatioValue = Field(default=None, alias="priceToEarningsRatio")
    price_to_earnings_growth: RatioValue = Field(default=None, alias="priceToEarningsGrowthRatio")
    forward_price_to_earnings_growth: RatioValue = Field(
        default=None, alias="forwardPriceToEarningsGrowthRatio"
    )
    price_to_earnings_diluted: RatioValue = Field(default=None, alias="priceToEarningsDilutedRatio")
    price_to_earnings_diluted_growth: RatioValue = Field(
        default=None, alias="priceToEarningsDilutedGrowthRatio"
    )
    price_to_book: RatioValue = Field(default=None, alias="priceToBookRatio")
    price_to_sales: RatioValue = Field(default=None, alias="priceToSalesRatio")
    price_to_free_cash_flow: RatioValue = Field(default=None, alias="priceToFreeCashFlowRatio")
    price_to_operating_cash_flow: RatioValue = Field(
        default=None, alias="priceToOperatingCashFlowRatio"
    )
    debt_to_assets: RatioValue = Field(default=None, alias="debtToAssetsRatio")
    debt_to_equity: RatioValue = Field(default=None, alias="debtToEquityRatio")
    debt_to_capital: RatioValue = Field(default=None, alias="debtToCapitalRatio")
    long_term_debt_to_capital: RatioValue = Field(default=None, alias="longTermDebtToCapitalRatio")
    financial_leverage_ratio: RatioValue = Field(default=None, alias="financialLeverageRatio")
    working_capital_turnover_ratio: RatioValue = Field(
        default=None, alias="workingCapitalTurnoverRatio"
    )
    operating_cash_flow_ratio: RatioValue = Field(default=None, alias="operatingCashFlowRatio")
    operating_cash_flow_sales_ratio: RatioValue = Field(
        default=None, alias="operatingCashFlowSalesRatio"
    )
    free_cash_flow_operating_cash_flow_ratio: RatioValue = Field(
        default=None, alias="freeCashFlowOperatingCashFlowRatio"
    )
    debt_service_coverage_ratio: RatioValue = Field(default=None, alias="debtServiceCoverageRatio")
    interest_coverage_ratio: RatioValue = Field(default=None, alias="interestCoverageRatio")
    short_term_operating_cash_flow_coverage_ratio: RatioValue = Field(
        default=None, alias="shortTermOperatingCashFlowCoverageRatio"
    )
    operating_cash_flow_coverage_ratio: RatioValue = Field(
        default=None, alias="operatingCashFlowCoverageRatio"
    )
    capital_expenditure_coverage_ratio: RatioValue = Field(
        default=None, alias="capitalExpenditureCoverageRatio"
    )
    dividend_paid_and_capex_coverage_ratio: RatioValue = Field(
        default=None, alias="dividendPaidAndCapexCoverageRatio"
    )
    dividend_payout_ratio: RatioValue = Field(default=None, alias="dividendPayoutRatio")
    dividend_yield: RatioValue = Field(default=None, alias="dividendYield")
    # FMP documents a percentage-form extension distinct from dividendYield.
    dividend_yield_percentage: RatioValue = Field(default=None, alias="dividendYieldPercentage")
    revenue_per_share: RatioValue = Field(default=None, alias="revenuePerShare")
    net_income_per_share: RatioValue = Field(default=None, alias="netIncomePerShare")
    interest_debt_per_share: RatioValue = Field(default=None, alias="interestDebtPerShare")
    cash_per_share: RatioValue = Field(default=None, alias="cashPerShare")
    book_value_per_share: RatioValue = Field(default=None, alias="bookValuePerShare")
    tangible_book_value_per_share: RatioValue = Field(
        default=None, alias="tangibleBookValuePerShare"
    )
    shareholders_equity_per_share: RatioValue = Field(
        default=None, alias="shareholdersEquityPerShare"
    )
    operating_cash_flow_per_share: RatioValue = Field(
        default=None, alias="operatingCashFlowPerShare"
    )
    capex_per_share: RatioValue = Field(default=None, alias="capexPerShare")
    free_cash_flow_per_share: RatioValue = Field(default=None, alias="freeCashFlowPerShare")
    net_income_per_ebt: RatioValue = Field(default=None, alias="netIncomePerEBT")
    ebt_per_ebit: RatioValue = Field(default=None, alias="ebtPerEbit")
    price_to_fair_value: RatioValue = Field(default=None, alias="priceToFairValue")
    debt_to_market_cap: RatioValue = Field(default=None, alias="debtToMarketCap")
    effective_tax_rate: RatioValue = Field(default=None, alias="effectiveTaxRate")
    enterprise_value_multiple: RatioValue = Field(default=None, alias="enterpriseValueMultiple")
    dividend_per_share: RatioValue = Field(default=None, alias="dividendPerShare")


class KeyMetrics(FMPFinancialPeriod):
    """Period-aligned FMP key metrics; valuation ratios and currency values stay distinct."""

    currency: str | None = Field(default=None, alias="reportedCurrency")

    @field_validator("currency")
    @classmethod
    def _valid_currency(cls, value: str | None) -> str | None:
        return _currency_code(value)

    market_cap: FinancialValue = Field(default=None, alias="marketCap")
    enterprise_value: FinancialValue = Field(default=None, alias="enterpriseValue")
    ev_to_sales: FinancialValue = Field(default=None, alias="evToSales")
    ev_to_operating_cash_flow: FinancialValue = Field(default=None, alias="evToOperatingCashFlow")
    ev_to_free_cash_flow: FinancialValue = Field(default=None, alias="evToFreeCashFlow")
    ev_to_ebitda: FinancialValue = Field(default=None, alias="evToEBITDA")
    net_debt_to_ebitda: FinancialValue = Field(default=None, alias="netDebtToEBITDA")
    current_ratio: FinancialValue = Field(default=None, alias="currentRatio")
    income_quality: FinancialValue = Field(default=None, alias="incomeQuality")
    graham_number: FinancialValue = Field(default=None, alias="grahamNumber")
    graham_net_net: FinancialValue = Field(default=None, alias="grahamNetNet")
    tax_burden: FinancialValue = Field(default=None, alias="taxBurden")
    interest_burden: FinancialValue = Field(default=None, alias="interestBurden")
    working_capital: FinancialValue = Field(default=None, alias="workingCapital")
    invested_capital: FinancialValue = Field(default=None, alias="investedCapital")
    return_on_assets: FinancialValue = Field(default=None, alias="returnOnAssets")
    operating_return_on_assets: FinancialValue = Field(
        default=None, alias="operatingReturnOnAssets"
    )
    return_on_tangible_assets: FinancialValue = Field(default=None, alias="returnOnTangibleAssets")
    return_on_equity: FinancialValue = Field(default=None, alias="returnOnEquity")
    return_on_invested_capital: FinancialValue = Field(
        default=None, alias="returnOnInvestedCapital"
    )
    return_on_capital_employed: FinancialValue = Field(
        default=None, alias="returnOnCapitalEmployed"
    )
    earnings_yield: FinancialValue = Field(default=None, alias="earningsYield")
    free_cash_flow_yield: FinancialValue = Field(default=None, alias="freeCashFlowYield")
    capex_to_operating_cash_flow: FinancialValue = Field(
        default=None, alias="capexToOperatingCashFlow"
    )
    capex_to_depreciation: FinancialValue = Field(default=None, alias="capexToDepreciation")
    capex_to_revenue: FinancialValue = Field(default=None, alias="capexToRevenue")
    sales_general_and_administrative_to_revenue: FinancialValue = Field(
        default=None, alias="salesGeneralAndAdministrativeToRevenue"
    )
    research_and_development_to_revenue: FinancialValue = Field(
        default=None, alias="researchAndDevelopementToRevenue"
    )
    stock_based_compensation_to_revenue: FinancialValue = Field(
        default=None, alias="stockBasedCompensationToRevenue"
    )
    intangibles_to_total_assets: FinancialValue = Field(
        default=None, alias="intangiblesToTotalAssets"
    )
    average_receivables: FinancialValue = Field(default=None, alias="averageReceivables")
    average_payables: FinancialValue = Field(default=None, alias="averagePayables")
    average_inventory: FinancialValue = Field(default=None, alias="averageInventory")
    days_of_sales_outstanding: FinancialValue = Field(default=None, alias="daysOfSalesOutstanding")
    days_of_payables_outstanding: FinancialValue = Field(
        default=None, alias="daysOfPayablesOutstanding"
    )
    days_of_inventory_outstanding: FinancialValue = Field(
        default=None, alias="daysOfInventoryOutstanding"
    )
    operating_cycle: FinancialValue = Field(default=None, alias="operatingCycle")
    cash_conversion_cycle: FinancialValue = Field(default=None, alias="cashConversionCycle")
    free_cash_flow_to_equity: FinancialValue = Field(default=None, alias="freeCashFlowToEquity")
    free_cash_flow_to_firm: FinancialValue = Field(default=None, alias="freeCashFlowToFirm")
    tangible_asset_value: FinancialValue = Field(default=None, alias="tangibleAssetValue")
    net_current_asset_value: FinancialValue = Field(default=None, alias="netCurrentAssetValue")


class RevenueBusinessLine(FMPFinancialPeriod):
    """One normalized business-line revenue observation from the nested FMP map."""

    business_line: str | None
    revenue: SegmentRevenue
    filing_date: date_type | None = None

    @field_validator("business_line")
    @classmethod
    def _valid_business_line(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("business line must be nonblank source text")
        return value


class RevenueGeographic(FMPFinancialPeriod):
    """One normalized geographic revenue observation from the nested FMP map."""

    region: str | None
    revenue: SegmentRevenue
    filing_date: date_type | None = None

    @field_validator("region")
    @classmethod
    def _valid_region(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("region must be nonblank source text")
        return value


__all__ = [
    "BalanceSheet",
    "BalanceSheetGrowth",
    "CashFlowStatement",
    "CashFlowStatementGrowth",
    "FinancialRatios",
    "IncomeStatement",
    "IncomeStatementGrowth",
    "KeyMetrics",
    "RevenueBusinessLine",
    "RevenueGeographic",
]
