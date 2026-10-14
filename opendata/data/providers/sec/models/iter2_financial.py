"""SEC companyfacts statement, growth, and XBRL frame comparison models."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.sec_iter2 import (
    SecBalanceSheetGrowthRow,
    SecBalanceSheetStatement,
    SecCashFlowStatementGrowthRow,
    SecCashFlowStatementRow,
    SecCompanyFactComparison,
    SecGrowthFactExtension,
    SecIncomeStatementGrowthRow,
    SecIncomeStatementRow,
    SecStatementFactExtension,
)
from opendata.data.protocol import (
    FetchContext,
    Fetcher,
    FetchResult,
    MultiSymbolQueryParams,
    QueryParams,
)
from opendata.data.providers.sec.models._common import (
    SEC_COMPANYFACTS_URL,
    SecProviderError,
    canonical_cik,
    company_ticker_rows,
    ensure_request_capacity,
    fetch_json,
    resolve_ticker,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

MAX_FACT_ROWS = 100_000
MAX_RESULT_ROWS = 5_000
_TICKER_RE = re.compile(r"^[A-Za-z0-9.-]{1,20}$")
_XBRL_TAG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,99}$")
_FieldSource = tuple[date, str, str]
_FORM_GROUPS: dict[str, frozenset[str]] = {
    "annual": frozenset({"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}),
    "quarterly": frozenset({"10-Q", "10-Q/A", "6-K", "6-K/A"}),
}
_ORIGINAL_FORMS: dict[str, frozenset[str]] = {
    "annual": frozenset({"10-K", "20-F", "40-F"}),
    "quarterly": frozenset({"10-Q", "6-K"}),
}
_PRELIMINARY_FORMS = frozenset({"8-K", "8-K/A"})

# The source contracts are the SEC's standard us-gaap concepts. Each item is a concept name
# documented in the XBRL companyfacts API; local labels are taken from that response.
BALANCE_SHEET_COLUMNS: dict[str, tuple[str, str]] = {
    "CashAndCashEquivalentsAtCarryingValue": ("cash_and_equivalents", "currency"),
    "Cash": ("cash_and_equivalents", "currency"),
    "RestrictedCash": ("restricted_cash", "currency"),
    "RestrictedCashAndCashEquivalents": ("restricted_cash", "currency"),
    "FederalFundsSold": ("fed_funds_sold", "currency"),
    "InterestBearingDepositsInOtherBanks": ("interest_bearing_deposits_at_other_banks", "currency"),
    "TradingSecurities": ("trading_account_securities", "currency"),
    "AllowanceForLoanLeaseLossesCurrent": ("allowance_for_loan_and_lease_losses", "currency"),
    "LoansAndLeasesReceivableNetReportedAmount": ("net_loans_and_leases", "currency"),
    "LoansReceivableHeldForSaleNet": ("loans_held_for_sale", "currency"),
    "AccruedInvestmentIncomeReceivable": ("accrued_investment_income", "currency"),
    "ShortTermInvestments": ("short_term_investments", "currency"),
    "AccountsReceivableNetCurrent": ("accounts_receivable", "currency"),
    "ReceivablesNetCurrent": ("accounts_receivable", "currency"),
    "AccountsReceivableNontradeCurrent": ("nontrade_receivables", "currency"),
    "InventoryNet": ("net_inventory", "currency"),
    "PrepaidExpenseCurrent": ("prepaid_expenses", "currency"),
    "OtherAssetsCurrent": ("other_current_assets", "currency"),
    "PrepaidExpenseAndOtherAssetsCurrent": ("other_current_assets", "currency"),
    "AssetsCurrent": ("total_current_assets", "currency"),
    "PropertyPlantAndEquipmentGross": ("gross_ppe", "currency"),
    "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment": (
        "accumulated_depreciation",
        "currency",
    ),
    "PropertyPlantAndEquipmentNet": ("net_ppe", "currency"),
    "OperatingLeaseRightOfUseAsset": ("operating_lease_right_of_use_asset", "currency"),
    "FinanceLeaseRightOfUseAsset": ("finance_lease_right_of_use_asset", "currency"),
    "Goodwill": ("goodwill", "currency"),
    "IntangibleAssetsNetExcludingGoodwill": ("intangible_assets", "currency"),
    "LongTermInvestments": ("long_term_investments", "currency"),
    "DeferredTaxAssetsNet": ("tax_assets", "currency"),
    "OtherAssetsNoncurrent": ("other_noncurrent_assets", "currency"),
    "AssetsNoncurrent": ("total_noncurrent_assets", "currency"),
    "Assets": ("total_assets", "currency"),
    "LiabilitiesNoncurrent": ("total_noncurrent_liabilities", "currency"),
    "AccumulatedOtherComprehensiveIncomeLossNetOfTax": (
        "accumulated_other_comprehensive_income",
        "currency",
    ),
    "AdditionalPaidInCapital": ("additional_paid_in_capital", "currency"),
    "TreasuryStockValue": ("treasury_stock", "currency"),
    "OtherAssets": ("other_assets", "currency"),
    "NoninterestBearingDeposits": ("noninterest_bearing_deposits", "currency"),
    "InterestBearingDeposits": ("interest_bearing_deposits", "currency"),
    "FederalFundsPurchased": ("fed_funds_purchased", "currency"),
    "AccountsPayableCurrent": ("accounts_payable", "currency"),
    "AccruedLiabilitiesCurrent": ("accrued_expenses", "currency"),
    "ShortTermBorrowings": ("short_term_debt", "currency"),
    "LongTermDebtCurrent": ("short_term_debt", "currency"),
    "LongTermDebtCurrentMaturities": ("current_portion_of_long_term_debt", "currency"),
    "BankersAcceptancesOutstanding": ("bankers_acceptances", "currency"),
    "AccruedInterestPayableCurrent": ("accrued_interest_payable", "currency"),
    "TaxesPayableCurrent": ("tax_payables", "currency"),
    "ContractWithCustomerLiabilityCurrent": ("current_deferred_revenue", "currency"),
    "OtherLiabilitiesCurrent": ("other_current_liabilities", "currency"),
    "OperatingLeaseLiabilityCurrent": ("operating_lease_liability_current", "currency"),
    "FinanceLeaseLiabilityCurrent": ("finance_lease_liability_current", "currency"),
    "LiabilitiesCurrent": ("total_current_liabilities", "currency"),
    "LongTermDebt": ("long_term_debt", "currency"),
    "LongTermDebtNoncurrent": ("long_term_debt", "currency"),
    "CapitalLeaseObligations": ("capital_lease_obligations", "currency"),
    "OperatingLeaseLiabilityNoncurrent": ("operating_lease_liability_noncurrent", "currency"),
    "OtherLiabilitiesNoncurrent": ("other_noncurrent_liabilities", "currency"),
    "Liabilities": ("total_liabilities", "currency"),
    "CommitmentsAndContingencies": ("commitments_and_contingencies", "currency"),
    "RetainedEarningsAccumulatedDeficit": ("retained_earnings", "currency"),
    "StockholdersEquity": ("total_stockholders_equity", "currency"),
    "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest": (
        "total_equity_and_noncontrolling_interests",
        "currency",
    ),
    "LiabilitiesAndStockholdersEquity": ("total_liabilities_and_equity", "currency"),
    "RedeemableNoncontrollingInterestEquityCarryingAmount": (
        "redeemable_noncontrolling_interest",
        "currency",
    ),
    "StockholdersEquityOther": ("other_equity", "currency"),
    "NoncontrollingInterestInConsolidatedEntity": ("noncontrolling_interests", "currency"),
}
CASH_FLOW_COLUMNS: dict[str, tuple[str, str]] = {
    "NetIncomeLoss": ("net_income", "currency"),
    "ProfitLoss": ("net_income", "currency"),
    "NetIncomeLossFromContinuingOperations": ("net_income_continuing", "currency"),
    "NetIncomeLossFromDiscontinuedOperations": ("net_income_discontinued", "currency"),
    "ProvisionForLoanLeaseAndOtherLosses": ("provision_for_loan_losses", "currency"),
    "Depreciation": ("depreciation_expense", "currency"),
    "AmortizationOfIntangibleAssets": ("amortization_expense", "currency"),
    "DepreciationAndAmortization": ("depreciation_and_amortization", "currency"),
    "DepreciationDepletionAndAmortization": ("depreciation_and_amortization", "currency"),
    "DeferredIncomeTaxExpenseBenefit": ("deferred_income_tax", "currency"),
    "ShareBasedCompensation": ("stock_based_compensation", "currency"),
    "IncreaseDecreaseInOperatingCapital": ("increase_decrease_in_operating_capital", "currency"),
    "IncreaseDecreaseInAccountsReceivable": ("change_in_accounts_receivable", "currency"),
    "IncreaseDecreaseInAccountsPayable": ("change_in_accounts_payable", "currency"),
    "IncreaseDecreaseInInventories": ("change_in_inventories", "currency"),
    "IncreaseDecreaseInOtherOperatingAssetsAndLiabilities": (
        "change_in_other_operating_assets_and_liabilities",
        "currency",
    ),
    "NetCashProvidedByUsedInOperatingActivities": (
        "net_cash_from_operating_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInContinuingOperatingActivities": (
        "net_cash_from_continuing_operating_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInDiscontinuedOperatingActivities": (
        "net_cash_from_discontinued_operating_activities",
        "currency",
    ),
    "PaymentsToAcquirePropertyPlantAndEquipment": (
        "purchase_of_plant_property_and_equipment",
        "currency",
    ),
    "PaymentsToAcquireBusinessesNetOfCashAcquired": ("acquisitions", "currency"),
    "PaymentsToAcquireInvestments": ("purchase_of_investments", "currency"),
    "PaymentsToAcquireHeldToMaturitySecurities": (
        "purchase_of_held_to_maturity_investments",
        "currency",
    ),
    "ProceedsFromSaleOfPropertyPlantAndEquipment": (
        "sale_of_plant_property_and_equipment",
        "currency",
    ),
    "ProceedsFromSaleOfProductiveAssets": ("sale_of_productive_assets", "currency"),
    "ProceedsFromDivestitureOfBusinesses": ("divestitures", "currency"),
    "ProceedsFromSaleOfInvestments": ("sale_of_investments", "currency"),
    "ProceedsFromMaturitiesPrepaymentsAndCallsOfAvailableForSaleSecurities": (
        "maturity_of_investments",
        "currency",
    ),
    "NetCashProvidedByUsedInInvestingActivities": (
        "net_cash_from_investing_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInContinuingInvestingActivities": (
        "net_cash_from_continuing_investing_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInDiscontinuedInvestingActivities": (
        "net_cash_from_discontinued_investing_activities",
        "currency",
    ),
    "RepaymentsOfDebt": ("repayment_of_debt", "currency"),
    "PaymentsForRepurchaseOfPreferredStock": ("repurchase_of_preferred_equity", "currency"),
    "PaymentsForRepurchaseOfCommonStock": ("repurchase_of_common_equity", "currency"),
    "PaymentsOfDividends": ("payment_of_dividends", "currency"),
    "ProceedsFromIssuanceOfDebt": ("issuance_of_debt", "currency"),
    "ProceedsFromIssuanceOfPreferredStock": ("issuance_of_preferred_equity", "currency"),
    "ProceedsFromIssuanceOfCommonStock": ("issuance_of_common_equity", "currency"),
    "NetChangeInDeposits": ("net_change_in_deposits", "currency"),
    "NetCashProvidedByUsedInFinancingActivities": (
        "net_cash_from_financing_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInContinuingFinancingActivities": (
        "net_cash_from_continuing_financing_activities",
        "currency",
    ),
    "NetCashProvidedByUsedInDiscontinuedFinancingActivities": (
        "net_cash_from_discontinued_financing_activities",
        "currency",
    ),
    (
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecrease"
        "IncludingExchangeRateEffect"
    ): (
        "net_change_in_cash",
        "currency",
    ),
}
INCOME_STATEMENT_COLUMNS: dict[str, tuple[str, str]] = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("operating_revenue", "currency"),
    "RevenueFromContractWithCustomerIncludingAssessedTax": ("operating_revenue", "currency"),
    "Revenues": ("total_revenue", "currency"),
    "SalesRevenueNet": ("total_revenue", "currency"),
    "CostOfRevenue": ("total_cost_of_revenue", "currency"),
    "CostOfGoodsAndServicesSold": ("total_cost_of_revenue", "currency"),
    "GrossProfit": ("total_gross_profit", "currency"),
    "GeneralAndAdministrativeExpense": ("general_and_admin_expense", "currency"),
    "SellingGeneralAndAdministrativeExpense": (
        "sga_expense",
        "currency",
    ),
    "ResearchAndDevelopmentExpense": ("rd_expense", "currency"),
    "SellingAndMarketingExpense": ("selling_and_marketing_expense", "currency"),
    "OperatingExpenses": ("total_operating_expenses", "currency"),
    "OperatingIncomeLoss": ("total_operating_income", "currency"),
    "InvestmentIncomeInterest": ("investment_securities_interest_income", "currency"),
    "InterestAndDebtExpense": ("interest_expense", "currency"),
    "InterestExpenseDebt": ("interest_expense", "currency"),
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": (
        "income_before_tax",
        "currency",
    ),
    "NetIncomeLossFromContinuingOperations": ("net_income_continuing", "currency"),
    "NetIncomeLossFromDiscontinuedOperations": ("net_income_discontinued", "currency"),
    "IncomeTaxExpenseBenefit": ("income_tax_expense", "currency"),
    "NetIncomeLoss": ("net_income", "currency"),
    "EarningsPerShareBasic": ("earnings_per_share_basic", "per_share"),
    "EarningsPerShareDiluted": ("earnings_per_share_diluted", "per_share"),
    "WeightedAverageNumberOfSharesOutstandingBasic": (
        "weighted_average_shares_outstanding",
        "shares",
    ),
    "WeightedAverageNumberOfDilutedSharesOutstanding": (
        "weighted_average_diluted_shares_outstanding",
        "shares",
    ),
}
BALANCE_SHEET_TAGS = frozenset(BALANCE_SHEET_COLUMNS)
CASH_FLOW_TAGS = frozenset(CASH_FLOW_COLUMNS)
INCOME_STATEMENT_TAGS = frozenset(INCOME_STATEMENT_COLUMNS)

_STATEMENT_FIELD_ALIASES: dict[str, dict[str, str]] = {
    "balance": {
        "cash_and_cash_equivalents": "cash_and_equivalents",
        "inventory": "net_inventory",
        "property_plant_and_equipment_net": "net_ppe",
        "other_non_current_assets": "other_noncurrent_assets",
        "non_current_assets": "total_noncurrent_assets",
        "total_equity": "total_equity_and_noncontrolling_interests",
    },
    "cash_flow": {
        "change_in_working_capital": "increase_decrease_in_operating_capital",
        "operating_cash_flow": "net_cash_from_operating_activities",
        "capital_expenditure": "purchase_of_plant_property_and_equipment",
        "investing_cash_flow": "net_cash_from_investing_activities",
        "financing_cash_flow": "net_cash_from_financing_activities",
        "debt_issuance": "issuance_of_debt",
        "common_stock_issuance": "issuance_of_common_equity",
        "common_stock_repurchased": "repurchase_of_common_equity",
        "dividends_paid": "payment_of_dividends",
        "net_change_in_cash": "net_change_in_cash",
    },
    "income": {
        "cost_of_revenue": "total_cost_of_revenue",
        "gross_profit": "total_gross_profit",
        "selling_general_and_admin_expense": "sga_expense",
        "research_and_development_expense": "rd_expense",
        "operating_expenses": "total_operating_expenses",
        "operating_income": "total_operating_income",
        "earnings_per_share_basic": "basic_eps",
        "earnings_per_share_diluted": "diluted_eps",
        "weighted_average_shares_outstanding": "weighted_ave_basic_shares_os",
        "weighted_average_diluted_shares_outstanding": "weighted_ave_diluted_shares_os",
    },
}
_STATEMENT_DERIVED_FIELDS = {
    "balance": ("cash_and_short_term_investments",),
    "cash_flow": ("free_cash_flow",),
    "income": ("revenue",),
}


def _add_statement_aliases(row: dict[str, object], kind: str) -> None:
    for alias, source in _STATEMENT_FIELD_ALIASES[kind].items():
        if row.get(source) is not None:
            row[alias] = row[source]
    if kind == "income":
        # `revenue` is a legacy summary alias. Prefer the explicit total when available.
        value = row.get("total_revenue")
        if value is None:
            value = row.get("operating_revenue")
        if value is not None:
            row["revenue"] = value
    if kind == "balance":
        cash = row.get("cash_and_equivalents")
        short_term = row.get("short_term_investments")
        if cash is not None and short_term is not None:
            row["cash_and_short_term_investments"] = _as_float(cash) + _as_float(short_term)
    if kind == "cash_flow":
        cash_flow = row.get("operating_cash_flow")
        capital_expenditure = row.get("capital_expenditure")
        if cash_flow is not None and capital_expenditure is not None:
            row["free_cash_flow"] = _as_float(cash_flow) - abs(_as_float(capital_expenditure))


def _as_float(value: object) -> float:
    """Narrow an internal value before applying the existing numeric conversion."""
    if not isinstance(value, (int, float, Decimal, str)):
        raise TypeError(f"expected an SEC numeric value, got {type(value).__name__}")
    return float(value)


def _row_date(row: dict[str, object], key: str) -> date:
    """Read a required date from a dynamically assembled statement row."""
    value = row[key]
    if not isinstance(value, date):
        raise TypeError(f"expected date in SEC statement row field {key}")
    return value


def _row_text(row: dict[str, object], key: str) -> str:
    """Read required text from a dynamically assembled statement row."""
    value = row[key]
    if not isinstance(value, str):
        raise TypeError(f"expected text in SEC statement row field {key}")
    return value


def _row_statement_facts(row: dict[str, object]) -> tuple[SecStatementFactExtension, ...]:
    """Narrow the statement-fact extension tuple carried by a dynamic row."""
    value = row["sec_facts"]
    if not isinstance(value, tuple):
        raise TypeError("expected SEC statement fact extensions")
    facts: list[SecStatementFactExtension] = []
    for item in value:
        fact: object = item
        if not isinstance(fact, SecStatementFactExtension):
            raise TypeError("expected SEC statement fact extensions")
        facts.append(fact)
    return tuple(facts)


def _field_sources_for(row: dict[str, object], field_name: str) -> tuple[_FieldSource, ...]:
    """Narrow source lineage tuples stored in an internal statement row."""
    source_map = row.get("_field_sources")
    if not isinstance(source_map, dict):
        return ()
    values: object = source_map.get(field_name, ())
    if not isinstance(values, tuple):
        raise TypeError("expected SEC statement field-source tuples")
    sources: list[_FieldSource] = []
    for item in values:
        source: object = item
        if not isinstance(source, tuple) or len(source) != 3:
            raise TypeError("expected SEC statement field-source tuples")
        period: object = source[0]
        tag: object = source[1]
        unit: object = source[2]
        if not isinstance(period, date) or not isinstance(tag, str) or not isinstance(unit, str):
            raise TypeError("expected SEC statement field-source tuples")
        sources.append((period, tag, unit))
    return tuple(sources)


def _add_growth_aliases(row: dict[str, object], kind: str) -> None:
    for alias, source in _STATEMENT_FIELD_ALIASES[kind].items():
        alias_field = f"growth_{alias}"
        source_field = f"growth_{source}"
        if row.get(source_field) is not None:
            row[alias_field] = row[source_field]
    if kind == "income":
        value = row.get("growth_total_revenue")
        if value is None:
            value = row.get("growth_operating_revenue")
        if value is not None:
            row["growth_revenue"] = value


def _reject_unexpected(kwargs: dict[str, object], allowed: set[str]) -> None:
    unexpected = set(kwargs) - allowed - {"source", "market"}
    if unexpected:
        raise ValueError(f"unsupported query parameters: {sorted(unexpected)}")


def _date_value(value: object) -> date:
    if not isinstance(value, str):
        raise SecProviderError("SEC_XBRL_BAD_FACT")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SecProviderError("SEC_XBRL_BAD_FACT") from None


def _decimal_value(value: object) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise SecProviderError("SEC_XBRL_BAD_FACT")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SecProviderError("SEC_XBRL_BAD_FACT") from None
    if not result.is_finite():
        raise SecProviderError("SEC_XBRL_BAD_FACT")
    return result


@dataclass(frozen=True)
class SecRawFact:
    """Validated source fact; kept separate from the public statement contract."""

    cik: str
    symbol: str | None
    company_name: str | None
    tag: str
    label: str
    unit: str
    value: Decimal
    period_start: date | None
    period_end: date
    filed_date: date
    form: str
    fiscal_year: int | None
    fiscal_period: str | None
    accession_number: str
    frame: str | None
    source_period_start: date | None = None
    source_value: Decimal | None = None
    source_fiscal_period: str | None = None


@dataclass(frozen=True)
class SecFactsRaw:
    """Validated companyfacts envelope shared by the statement models."""

    cik: str
    symbol: str | None
    facts: tuple[SecRawFact, ...]


class SecFinancialQuery(QueryParams):
    """Public statement query with bounded SEC-specific date extensions."""

    cik: str | None = None
    symbol: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    filed_start_date: date | None = None
    filed_end_date: date | None = None
    market: Literal["us"] = "us"
    fiscal_year: int | None = Field(default=None, strict=True)
    period: Literal["annual", "quarterly", "quarterly_yoy", "ttm"] = "annual"
    include_preliminary: bool = False
    pit_mode: bool = False
    use_cache: bool | None = True
    offset: int = Field(default=0, ge=0, le=MAX_RESULT_ROWS, strict=True)
    limit: int = Field(default=5, ge=1, le=MAX_RESULT_ROWS, strict=True)

    @field_validator("cik")
    @classmethod
    def _valid_cik(cls, value: str | None) -> str | None:
        return None if value is None else canonical_cik(value)

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if _TICKER_RE.fullmatch(normalized) is None:
            raise ValueError("symbol must be a short ticker token")
        return normalized

    @model_validator(mode="after")
    def _validate_bounds(self) -> SecFinancialQuery:
        if (self.cik is None) == (self.symbol is None):
            raise ValueError("provide exactly one of cik or symbol")
        for start, end, names in (
            (self.start_date, self.end_date, ("start_date", "end_date")),
            (self.filed_start_date, self.filed_end_date, ("filed_start_date", "filed_end_date")),
        ):
            if start is not None and end is not None and start > end:
                raise ValueError(f"{names[0]} must not be after {names[1]}")
        if self.fiscal_year is not None and not 1900 <= self.fiscal_year <= 2200:
            raise ValueError("fiscal_year must be between 1900 and 2200")
        if self.use_cache is False:
            raise ValueError("use_cache=False is unsupported by the governed SEC transport")
        return self


def _parse_companyfacts(
    document: Mapping[str, object],
    *,
    cik: str,
    symbol: str | None,
    company_name: str | None = None,
    tags: frozenset[str],
    instant: bool | None,
    params: SecFinancialQuery,
) -> SecFactsRaw:
    raw_cik = document.get("cik")
    if raw_cik is not None and canonical_cik(raw_cik) != cik:
        raise SecProviderError("SEC_XBRL_CIK_MISMATCH")
    facts_root = document.get("facts")
    if not isinstance(facts_root, Mapping):
        raise SecProviderError("SEC_XBRL_BAD_SHAPE")
    taxonomy = facts_root.get("us-gaap")
    if taxonomy is None:
        return SecFactsRaw(cik=cik, symbol=symbol, facts=())
    if not isinstance(taxonomy, Mapping):
        raise SecProviderError("SEC_XBRL_BAD_SHAPE")
    if any(not isinstance(tag, str) for tag in taxonomy):
        raise SecProviderError("SEC_XBRL_BAD_SHAPE")

    parsed: list[SecRawFact] = []
    for tag in sorted(tags.intersection(taxonomy)):
        definition = taxonomy[tag]
        if not isinstance(definition, Mapping):
            raise SecProviderError("SEC_XBRL_BAD_SHAPE")
        label = definition.get("label")
        units = definition.get("units")
        if not isinstance(label, str) or not label.strip() or not isinstance(units, Mapping):
            raise SecProviderError("SEC_XBRL_BAD_SHAPE")
        for unit, observations in units.items():
            if not isinstance(unit, str) or not unit or not isinstance(observations, list):
                raise SecProviderError("SEC_XBRL_BAD_SHAPE")
            for observation in observations:
                if not isinstance(observation, Mapping):
                    raise SecProviderError("SEC_XBRL_BAD_SHAPE")
                form = observation.get("form")
                if not isinstance(form, str) or not form.strip():
                    raise SecProviderError("SEC_XBRL_BAD_FACT")
                allowed_forms = _FORM_GROUPS["annual"] | _FORM_GROUPS["quarterly"]
                if params.include_preliminary:
                    allowed_forms |= _PRELIMINARY_FORMS
                if form not in allowed_forms:
                    continue
                period_end = _date_value(observation.get("end"))
                filed_date = _date_value(observation.get("filed"))
                raw_start = observation.get("start")
                if instant is True:
                    if raw_start not in (None, ""):
                        continue
                    period_start = None
                elif instant is False:
                    if raw_start in (None, ""):
                        continue
                    period_start = _date_value(raw_start)
                    if period_start >= period_end or (period_end - period_start).days > 400:
                        raise SecProviderError("SEC_XBRL_BAD_FACT")
                elif raw_start in (None, ""):
                    period_start = None
                else:
                    period_start = _date_value(raw_start)
                    if period_start >= period_end or (period_end - period_start).days > 400:
                        raise SecProviderError("SEC_XBRL_BAD_FACT")
                if params.start_date is not None and period_end < params.start_date:
                    continue
                if params.end_date is not None and period_end > params.end_date:
                    continue
                if params.filed_start_date is not None and filed_date < params.filed_start_date:
                    continue
                if params.filed_end_date is not None and filed_date > params.filed_end_date:
                    continue
                accession = observation.get("accn")
                if not isinstance(accession, str) or not accession.strip():
                    raise SecProviderError("SEC_XBRL_BAD_FACT")
                fiscal_year = observation.get("fy")
                if fiscal_year is not None and (
                    isinstance(fiscal_year, bool) or not isinstance(fiscal_year, int)
                ):
                    raise SecProviderError("SEC_XBRL_BAD_FACT")
                fiscal_period = observation.get("fp")
                if fiscal_period is not None and not isinstance(fiscal_period, str):
                    raise SecProviderError("SEC_XBRL_BAD_FACT")
                frame = observation.get("frame")
                if frame is not None and not isinstance(frame, str):
                    raise SecProviderError("SEC_XBRL_BAD_FACT")
                fact_value = _decimal_value(observation.get("val"))
                parsed.append(
                    SecRawFact(
                        cik=cik,
                        symbol=symbol,
                        company_name=company_name,
                        tag=tag,
                        label=label.strip(),
                        unit=unit,
                        value=fact_value,
                        period_start=period_start,
                        period_end=period_end,
                        filed_date=filed_date,
                        form=form,
                        fiscal_year=fiscal_year,
                        fiscal_period=fiscal_period,
                        accession_number=accession.strip(),
                        frame=frame,
                        source_period_start=period_start,
                        source_value=fact_value,
                        source_fiscal_period=fiscal_period,
                    )
                )
                if len(parsed) > MAX_FACT_ROWS:
                    raise SecProviderError("SEC_XBRL_TOO_MANY_FACTS")
    parsed.sort(
        key=lambda item: (
            item.period_end,
            item.period_start or date.min,
            item.filed_date,
            item.tag,
            item.unit,
            item.accession_number,
        )
    )
    return SecFactsRaw(cik=cik, symbol=symbol, facts=tuple(parsed))


def _fetch_companyfacts(
    params: SecFinancialQuery,
    ctx: FetchContext,
    *,
    tags: frozenset[str],
    instant: bool,
) -> SecFactsRaw:
    company_name: str | None
    if params.cik is not None:
        cik, symbol, request_count = params.cik, None, 1
        ensure_request_capacity(ctx, request_count)
    else:
        # The SEC ticker directory is one stage; the per-company XBRL response is the second.
        request_count = 2
        ensure_request_capacity(ctx, request_count)
        rows = company_ticker_rows(ctx)
        identity = resolve_ticker(rows, params.symbol or "")
        cik, symbol = identity["cik"], identity["ticker"]
        company_name = identity["name"]
    if params.cik is not None:
        company_name = None
    document = fetch_json(ctx, f"{SEC_COMPANYFACTS_URL}/CIK{cik}.json")
    return _parse_companyfacts(
        document,
        cik=cik,
        symbol=symbol,
        company_name=company_name,
        tags=tags,
        instant=instant,
        params=params,
    )


def _form_family(form: str) -> str | None:
    if form in _FORM_GROUPS["annual"]:
        return "annual"
    if form in _FORM_GROUPS["quarterly"]:
        return "quarterly"
    if form in _PRELIMINARY_FORMS:
        return "preliminary"
    return None


def _period_metadata(fact: SecRawFact, *, instant: bool) -> tuple[int | None, str | None]:
    """Use a frame or a filing-context label only when dates agree with it."""
    frame_match = re.fullmatch(r"CY(\d{4})(?:Q([1-4])(I?))?", fact.frame or "")
    if frame_match and int(frame_match.group(1)) == fact.period_end.year:
        frame_year = int(frame_match.group(1))
        frame_quarter = frame_match.group(2)
        if fact.fiscal_year == frame_year:
            if frame_quarter is None:
                if (
                    _form_family(fact.form) == "annual"
                    and (
                        instant
                        or (
                            fact.period_start is not None
                            and 330 <= (fact.period_end - fact.period_start).days <= 400
                        )
                    )
                    and (fact.fiscal_period or "").upper() == "FY"
                ):
                    return frame_year, "FY"
            elif int(frame_quarter) == (fact.period_end.month - 1) // 3 + 1:
                frame_period = f"Q{frame_quarter}"
                if (instant and (fact.fiscal_period or "").upper() == frame_period) or (
                    not instant
                    and fact.period_start is not None
                    and 70 <= (fact.period_end - fact.period_start).days <= 110
                    and _form_family(fact.form) == "quarterly"
                    and (fact.fiscal_period or "").upper() == frame_period
                ):
                    return frame_year, frame_period
    if fact.fiscal_year != fact.period_end.year:
        return None, None
    family = _form_family(fact.form)
    fp = (fact.fiscal_period or "").upper()
    if instant and family == "annual" and fp == "FY":
        return fact.fiscal_year, "FY"
    if instant and family == "quarterly" and fp in {"Q1", "Q2", "Q3"}:
        return fact.fiscal_year, fp
    if fact.period_start is not None:
        duration = (fact.period_end - fact.period_start).days
        if family == "annual" and duration >= 330 and fp == "FY":
            return fact.fiscal_year, "FY"
        if family in {"quarterly", "annual"} and 70 <= duration <= 110 and fp in {"Q1", "Q2", "Q3"}:
            return fact.fiscal_year, fp
        if family == "annual" and 70 <= duration <= 110 and fp == "Q4":
            return fact.fiscal_year, fp
    return None, None


def _select_vintages(
    facts: tuple[SecRawFact, ...], *, pit_mode: bool, include_preliminary: bool
) -> tuple[SecRawFact, ...]:
    """Choose one coherent accession per observed period; PIT keeps the original filing."""
    grouped: dict[tuple[date | None, date], list[SecRawFact]] = defaultdict(list)
    for fact in facts:
        grouped[(fact.period_start, fact.period_end)].append(fact)
    selected: list[SecRawFact] = []
    for candidates in grouped.values():
        if not include_preliminary or any(
            _form_family(fact.form) != "preliminary" for fact in candidates
        ):
            candidates = [fact for fact in candidates if _form_family(fact.form) != "preliminary"]
        if pit_mode:
            originals = [
                fact
                for fact in candidates
                if fact.form in _ORIGINAL_FORMS.get(_form_family(fact.form) or "", frozenset())
            ]
            candidates = originals or [fact for fact in candidates if fact.form == "8-K"]
        if not candidates:
            continue

        def ordering(fact: SecRawFact) -> tuple[date, str]:
            return fact.filed_date, fact.accession_number

        selected_vintage = (min if pit_mode else max)(ordering(fact) for fact in candidates)
        accession = selected_vintage[1]
        selected.extend(fact for fact in candidates if fact.accession_number == accession)
    return tuple(selected)


def _annual_facts(facts: tuple[SecRawFact, ...], *, instant: bool) -> tuple[SecRawFact, ...]:
    if instant:
        return tuple(
            fact for fact in facts if _form_family(fact.form) == "annual" or fact.form == "8-K"
        )
    return tuple(
        fact
        for fact in facts
        if fact.period_start is not None
        and 330 <= (fact.period_end - fact.period_start).days <= 400
        and (_form_family(fact.form) == "annual" or fact.form == "8-K")
    )


def _quarter_facts(facts: tuple[SecRawFact, ...]) -> tuple[SecRawFact, ...]:
    """Normalize single-quarter facts; YTD differencing requires same accession/start/FY."""
    duration_facts = [fact for fact in facts if fact.period_start is not None]
    direct = [
        fact
        for fact in duration_facts
        if fact.period_start is not None and 70 <= (fact.period_end - fact.period_start).days <= 110
    ]
    direct_keys = {(fact.tag, fact.unit, fact.period_end, fact.accession_number) for fact in direct}
    ytd = [
        fact
        for fact in duration_facts
        if fact.period_start is not None
        and 110 < (fact.period_end - fact.period_start).days <= 400
        and (fact.tag, fact.unit, fact.period_end, fact.accession_number) not in direct_keys
    ]
    output = list(direct)
    for current in ytd:
        prior = [
            candidate
            for candidate in duration_facts
            if candidate.tag == current.tag
            and candidate.unit == current.unit
            and candidate.accession_number == current.accession_number
            and candidate.period_start == current.period_start
            and candidate.form == current.form
            and candidate.fiscal_year is not None
            and candidate.fiscal_year == current.fiscal_year
            and candidate.period_end < current.period_end
            and 70 <= (current.period_end - candidate.period_end).days <= 110
        ]
        if not prior:
            continue
        previous = max(prior, key=lambda fact: fact.period_end)
        output.append(
            replace(
                current,
                value=current.value - previous.value,
                period_start=previous.period_end.fromordinal(previous.period_end.toordinal() + 1),
                fiscal_period=(
                    "Q4"
                    if (current.fiscal_period or "").upper() == "FY"
                    and (previous.fiscal_period or "").upper() == "Q3"
                    else current.fiscal_period
                ),
            )
        )
    output.sort(
        key=lambda fact: (fact.period_end, fact.period_start or date.min, fact.tag, fact.unit)
    )
    return tuple(output)


def _instant_quarter_facts(facts: tuple[SecRawFact, ...]) -> tuple[SecRawFact, ...]:
    """Select supported quarter snapshots and identify a validated FY-end as Q4."""
    selected: list[SecRawFact] = []
    for fact in facts:
        _fiscal_year, fiscal_period = _period_metadata(fact, instant=True)
        if fiscal_period in {"Q1", "Q2", "Q3", "Q4"}:
            selected.append(fact)
        elif (
            fiscal_period == "FY"
            and _form_family(fact.form) == "annual"
            and fact.fiscal_year == fact.period_end.year
        ):
            # The annual filing reports the balance at the fiscal year-end instant.
            selected.append(replace(fact, fiscal_period="Q4"))
    return tuple(selected)


def _unit_matches(unit: str, category: str) -> bool:
    compact = unit.replace(" ", "").casefold()
    if category == "currency":
        return re.fullmatch(r"[a-z]{3}", compact) is not None
    if category == "shares":
        return compact in {"shares", "share"}
    if category == "per_share":
        return compact in {"usd/shares", "usd/share"}
    return False


def _fact_extension(fact: SecRawFact) -> SecStatementFactExtension:
    return SecStatementFactExtension(
        tag=fact.tag,
        label=fact.label,
        unit=fact.unit,
        value=fact.source_value if fact.source_value is not None else fact.value,
        period_beginning=(
            fact.source_period_start if fact.source_period_start is not None else fact.period_start
        ),
        period_ending=fact.period_end,
        filed_date=fact.filed_date,
        form=fact.form,
        fiscal_year=fact.fiscal_year,
        fiscal_period=(
            fact.source_fiscal_period
            if fact.source_fiscal_period is not None
            else fact.fiscal_period
        ),
        accession=fact.accession_number,
    )


def _project_statement_rows(
    facts: tuple[SecRawFact, ...],
    *,
    columns: dict[str, tuple[str, str]],
    kind: str,
    instant: bool,
    params: SecFinancialQuery,
) -> tuple[dict[str, object], ...]:
    grouped: dict[tuple[str, str, date | None, date, str], list[SecRawFact]] = defaultdict(list)
    for fact in facts:
        grouped[
            (fact.cik, fact.accession_number, fact.period_start, fact.period_end, fact.form)
        ].append(fact)
    output: list[dict[str, object]] = []
    tag_order = {tag: index for index, tag in enumerate(columns)}
    for (cik, accession, period_start, period_end, form), group in grouped.items():
        currency_units: set[str] = set()
        mapped: dict[str, list[SecRawFact]] = defaultdict(list)
        for fact in group:
            mapping = columns.get(fact.tag)
            if mapping is None:
                continue
            field_name, category = mapping
            if not _unit_matches(fact.unit, category):
                continue
            mapped[field_name].append(fact)
            if category == "currency":
                currency_units.add(fact.unit.upper())
        representative = min(group, key=lambda fact: (tag_order.get(fact.tag, 10_000), fact.unit))
        fiscal_year, fiscal_period = _period_metadata(representative, instant=instant)
        if params.fiscal_year is not None and fiscal_year != params.fiscal_year:
            continue
        row: dict[str, object] = {
            "period_ending": period_end,
            "fiscal_period": fiscal_period,
            "fiscal_year": fiscal_year,
            "filing_date": max(fact.filed_date for fact in group),
            "accepted_date": None,
            "cik": cik,
            "symbol": representative.symbol,
            "reported_currency": next(iter(currency_units)) if len(currency_units) == 1 else None,
            "period_beginning": period_start,
            "form": form,
            "accession": accession,
            "sec_facts": tuple(
                _fact_extension(fact)
                for fact in sorted(group, key=lambda fact: (fact.tag, fact.unit, fact.value))
            ),
        }
        field_sources: dict[str, tuple[tuple[date, str, str], ...]] = {}
        for field_name, candidates in mapped.items():
            candidate_units = {fact.unit.upper() for fact in candidates}
            category = next(
                category
                for candidate_tag, (candidate_field, category) in columns.items()
                if candidate_field == field_name
                and any(fact.tag == candidate_tag for fact in candidates)
            )
            if category == "currency" and len(candidate_units) > 1:
                if row["reported_currency"] not in candidate_units:
                    continue
                candidates = [
                    fact for fact in candidates if fact.unit.upper() == row["reported_currency"]
                ]
            elif len(candidate_units) > 1:
                continue
            chosen = min(candidates, key=lambda fact: tag_order[fact.tag])
            row[field_name] = float(chosen.value)
            field_sources[field_name] = ((period_end, chosen.tag, chosen.unit),)
        row["_field_sources"] = field_sources
        _add_statement_aliases(row, kind)
        output.append(row)
    output.sort(
        key=lambda row: (
            _row_date(row, "period_ending"),
            _row_date(row, "filing_date"),
            _row_text(row, "accession"),
        ),
        reverse=True,
    )
    return tuple(output)


def _quarter_rows_contiguous(rows: list[dict[str, object]]) -> bool:
    if not all(
        70
        <= (
            _row_date(rows[index], "period_ending") - _row_date(rows[index - 1], "period_ending")
        ).days
        <= 110
        for index in range(1, len(rows))
    ):
        return False
    raw_metadata = [(row.get("fiscal_period"), row.get("fiscal_year")) for row in rows]
    if not any(period is not None or year is not None for period, year in raw_metadata):
        return True
    metadata: list[tuple[str, int]] = []
    for period, year in raw_metadata:
        if (
            not isinstance(period, str)
            or period not in {"Q1", "Q2", "Q3", "Q4"}
            or not isinstance(year, int)
        ):
            return False
        metadata.append((period, year))
    for (prior_period, prior_year), (current_period, current_year) in zip(
        metadata, metadata[1:], strict=False
    ):
        expected_year = (
            prior_year + 1 if prior_period == "Q4" and current_period == "Q1" else prior_year
        )
        if _next_fiscal_quarter(prior_period) != current_period or current_year != expected_year:
            return False
    return True


def _ttm_rows(
    quarter_rows: tuple[dict[str, object], ...],
    *,
    columns: dict[str, tuple[str, str]],
    instant: bool,
    kind: str,
) -> tuple[dict[str, object], ...]:
    output: list[dict[str, object]] = []
    ordered = sorted(quarter_rows, key=lambda row: _row_date(row, "period_ending"))
    field_names = {field for field, _ in columns.values()}
    for index in range(3, len(ordered)):
        window = ordered[index - 3 : index + 1]
        if not _quarter_rows_contiguous(window):
            continue
        current = window[-1]
        row = dict(current)
        row["fiscal_period"] = "TTM"
        period_beginning = window[0].get("period_beginning")
        row["period_beginning"] = (
            _row_date(window[0], "period_beginning")
            if period_beginning is not None
            else _row_date(window[0], "period_ending")
        )
        row["sec_facts"] = tuple(fact for item in window for fact in _row_statement_facts(item))
        row["_field_sources"] = {
            field_name: tuple(
                source for item in window for source in _field_sources_for(item, field_name)
            )
            for field_name in field_names
        }
        for field_name in field_names:
            values = [item.get(field_name) for item in window]
            if any(value is None for value in values):
                row[field_name] = None
            else:
                total = sum(_as_float(value) for value in values)
                row[field_name] = total / 4 if instant else total
        for alias in _STATEMENT_FIELD_ALIASES[kind]:
            row.pop(alias, None)
        for field_name in _STATEMENT_DERIVED_FIELDS[kind]:
            row.pop(field_name, None)
        _add_statement_aliases(row, kind)
        output.append(row)
    return tuple(reversed(output))


def _compatible_prior(
    current: dict[str, object],
    candidates: tuple[dict[str, object], ...],
    *,
    mode: str,
    instant: bool,
) -> dict[str, object] | None:
    prior_rows: list[dict[str, object]] = []
    for prior in candidates:
        current_ending = _row_date(current, "period_ending")
        prior_ending = _row_date(prior, "period_ending")
        if prior_ending >= current_ending or prior["cik"] != current["cik"]:
            continue
        delta = (current_ending - prior_ending).days
        if mode == "quarterly":
            current_period = current.get("fiscal_period")
            prior_period = prior.get("fiscal_period")
            current_year = current.get("fiscal_year")
            prior_year = prior.get("fiscal_year")
            if not 70 <= delta <= 110:
                continue
            if (
                not isinstance(current_period, str)
                or not isinstance(prior_period, str)
                or current_period not in {"Q1", "Q2", "Q3", "Q4"}
                or prior_period not in {"Q1", "Q2", "Q3", "Q4"}
                or _next_fiscal_quarter(prior_period) != current_period
                or not isinstance(current_year, int)
                or not isinstance(prior_year, int)
            ):
                continue
            expected_year = (
                prior_year + 1 if prior_period == "Q4" and current_period == "Q1" else prior_year
            )
            if current_year != expected_year:
                continue
        if mode == "annual":
            if not 330 <= delta <= 400:
                continue
            current_year = current.get("fiscal_year")
            prior_year = prior.get("fiscal_year")
            if (
                not isinstance(current_year, int)
                or not isinstance(prior_year, int)
                or current_year != prior_year + 1
            ):
                continue
        if mode in {"quarterly_yoy", "ttm"} and not 350 <= delta <= 380:
            continue
        if mode == "quarterly_yoy":
            current_period = current.get("fiscal_period")
            prior_period = prior.get("fiscal_period")
            current_year = current.get("fiscal_year")
            prior_year = prior.get("fiscal_year")
            if (
                current_period not in {"Q1", "Q2", "Q3", "Q4"}
                or prior_period != current_period
                or not isinstance(current_year, int)
                or not isinstance(prior_year, int)
                or current_year != prior_year + 1
            ):
                continue
        if mode == "ttm" and current.get("fiscal_period") != prior.get("fiscal_period"):
            continue
        if not instant:
            current_start = current.get("period_beginning")
            prior_start = prior.get("period_beginning")
            if current_start is None or prior_start is None:
                continue
            if mode in {"annual", "quarterly_yoy", "ttm"} and (
                abs(
                    (current_ending - _row_date(current, "period_beginning")).days
                    - (prior_ending - _row_date(prior, "period_beginning")).days
                )
                > 5
            ):
                continue
        prior_rows.append(prior)
    return max(prior_rows, key=lambda row: _row_date(row, "period_ending")) if prior_rows else None


def _next_fiscal_quarter(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith("Q") or len(value) != 2:
        return None
    try:
        quarter = int(value[1])
    except ValueError:
        return None
    if quarter not in {1, 2, 3, 4}:
        return None
    return f"Q{quarter % 4 + 1}"


def _ordered_limited_fiscal_years(values: list[object]) -> bool:
    """Check the existing TTM year ordering after narrowing dynamic row values."""
    if not all(isinstance(value, int) for value in values):
        return False
    years = [value for value in values if isinstance(value, int)]
    return len(years) == len(values) and years == sorted(years) and len(set(years)) <= 2


def _growth_rows(
    rows: tuple[dict[str, object], ...],
    *,
    columns: dict[str, tuple[str, str]],
    instant: bool,
    mode: str,
    output_fields: frozenset[str],
    kind: str,
) -> tuple[dict[str, object], ...]:
    output: list[dict[str, object]] = []
    field_categories: dict[str, str] = {}
    for field_name, category in columns.values():
        field_categories.setdefault(field_name, category)
    growth_fields = {
        f"growth_{field_name}"
        for field_name in field_categories
        if f"growth_{field_name}" in output_fields
    }
    for current in rows:
        prior = _compatible_prior(current, rows, mode=mode, instant=instant)
        ttm_prior: tuple[dict[str, object], ...] = ()
        if mode == "ttm" and instant:
            same_quarter_prior = prior
            ordered = sorted(
                (
                    candidate
                    for candidate in rows
                    if candidate["cik"] == current["cik"]
                    and _row_date(candidate, "period_ending") < _row_date(current, "period_ending")
                ),
                key=lambda row: _row_date(row, "period_ending"),
            )
            if same_quarter_prior is not None:
                prior_index = next(
                    (
                        index
                        for index, candidate in enumerate(ordered)
                        if _row_date(candidate, "period_ending")
                        == _row_date(same_quarter_prior, "period_ending")
                    ),
                    None,
                )
                if prior_index is not None and prior_index >= 3:
                    window = tuple(ordered[prior_index - 3 : prior_index + 1])
                    if _quarter_rows_contiguous(list(window)):
                        expected = [
                            _next_fiscal_quarter(value.get("fiscal_period"))
                            for value in window[:-1]
                        ]
                        actual = [value.get("fiscal_period") for value in window[1:]]
                        years = [value.get("fiscal_year") for value in window]
                        if expected == actual and _ordered_limited_fiscal_years(years):
                            ttm_prior = window
        comparison_prior = ttm_prior[-1] if mode == "ttm" and instant and ttm_prior else prior
        result = {
            key: current.get(key)
            for key in (
                "period_ending",
                "fiscal_period",
                "fiscal_year",
                "filing_date",
                "accepted_date",
                "cik",
                "symbol",
                "reported_currency",
                "period_beginning",
                "form",
                "accession",
            )
        }
        for field in growth_fields:
            result[field] = None
        growth_facts: list[SecGrowthFactExtension] = []
        for field_name, category in field_categories.items():
            growth_field = f"growth_{field_name}"
            if growth_field not in result:
                continue
            current_value = current.get(field_name)
            source_pairs = _field_sources_for(current, field_name)
            prior_sources: tuple[tuple[date, str, str], ...] = ()
            prior_value: object | None
            if mode == "ttm" and instant:
                prior_values = [candidate.get(field_name) for candidate in ttm_prior]
                if len(prior_values) == 4 and all(value is not None for value in prior_values):
                    prior_value = sum(
                        _as_float(value) for value in prior_values if value is not None
                    ) / len(prior_values)
                else:
                    prior_value = None
                prior_sources = tuple(
                    source
                    for candidate in ttm_prior
                    for source in _field_sources_for(candidate, field_name)
                )
            else:
                prior_raw_value = (
                    comparison_prior.get(field_name) if comparison_prior is not None else None
                )
                prior_value = prior_raw_value
                if comparison_prior is not None:
                    prior_sources = _field_sources_for(comparison_prior, field_name)

            if mode == "ttm" and instant:
                source_comparable = (
                    len(source_pairs) == 1
                    and len(prior_sources) == 4
                    and all(
                        (tag, unit.upper()) == (source_pairs[0][1], source_pairs[0][2].upper())
                        for _period, tag, unit in prior_sources
                    )
                )
            else:
                source_comparable = (
                    len(source_pairs) == len(prior_sources)
                    and bool(source_pairs)
                    and tuple((tag, unit.upper()) for _period, tag, unit in source_pairs)
                    == tuple((tag, unit.upper()) for _period, tag, unit in prior_sources)
                )
            if category == "currency" and comparison_prior is not None:
                source_comparable = source_comparable and (
                    current.get("reported_currency") == comparison_prior.get("reported_currency")
                )
            if not source_pairs or current_value is None:
                continue
            current_source = source_pairs[-1]
            if not source_comparable:
                prior_value = None
            growth = None
            if prior_value is not None and prior_value != 0:
                growth = (_as_float(current_value) / _as_float(prior_value)) - 1.0
            result[growth_field] = growth
            growth_facts.append(
                SecGrowthFactExtension(
                    tag=current_source[1],
                    unit=current_source[2],
                    current_value=Decimal(str(current_value)),
                    prior_value=Decimal(str(prior_value)) if prior_value is not None else None,
                    prior_period_ending=(
                        _row_date(comparison_prior, "period_ending")
                        if comparison_prior is not None
                        else None
                    ),
                    prior_filed_date=(
                        _row_date(comparison_prior, "filing_date")
                        if comparison_prior is not None
                        else None
                    ),
                    prior_accession=(
                        _row_text(comparison_prior, "accession")
                        if comparison_prior is not None
                        else None
                    ),
                    growth=growth,
                )
            )
        result["sec_growth_facts"] = tuple(growth_facts)
        _add_growth_aliases(result, kind)
        output.append(result)
    return tuple(output)


class _SecStatementFetcher(Fetcher[SecFinancialQuery, SecFactsRaw]):
    """Common bounded SEC companyfacts stage and typed statement projection."""

    async_mode = "bounded_thread"
    tags: ClassVar[frozenset[str]]
    instant: ClassVar[bool]
    growth: ClassVar[bool] = False
    columns: ClassVar[dict[str, tuple[str, str]]]
    statement_kind: ClassVar[str]
    output_model: ClassVar[type[ContractModel]]
    capability: ClassVar[Capability]
    canonical_model: ClassVar[str]

    def transform_query(self, **kwargs: object) -> SecFinancialQuery:
        _reject_unexpected(
            kwargs,
            {
                "cik",
                "symbol",
                "start_date",
                "end_date",
                "filed_start_date",
                "filed_end_date",
                "period",
                "offset",
                "limit",
                "fiscal_year",
                "include_preliminary",
                "pit_mode",
                "use_cache",
            },
        )
        params = SecFinancialQuery.model_validate(kwargs)
        if params.period == "quarterly_yoy" and not self.growth:
            raise ValueError("quarterly_yoy is only supported by growth models")
        return params

    def extract_data(self, params: SecFinancialQuery, ctx: FetchContext) -> SecFactsRaw:
        return _fetch_companyfacts(params, ctx, tags=self.tags, instant=self.instant)

    def transform_data(self, raw: SecFactsRaw, params: SecFinancialQuery) -> FetchResult:
        facts = _select_vintages(
            raw.facts,
            pit_mode=params.pit_mode,
            include_preliminary=params.include_preliminary,
        )
        if params.period == "annual":
            selected = _annual_facts(facts, instant=self.instant)
        elif self.instant:
            selected = _instant_quarter_facts(facts)
        else:
            selected = _quarter_facts(facts)
        if params.period != "annual":
            # YTD observations are normalized into single quarters above. Resolve filing
            # vintages again on those normalized periods so a direct quarter and a YTD
            # difference cannot both survive as duplicate rows for the same quarter.
            selected = _select_vintages(
                selected,
                pit_mode=params.pit_mode,
                include_preliminary=params.include_preliminary,
            )
        rows = _project_statement_rows(
            selected,
            columns=self.columns,
            kind=self.statement_kind,
            instant=self.instant,
            params=params,
        )
        if params.period == "ttm":
            if self.growth and self.instant:
                rows = _growth_rows(
                    rows,
                    columns=self.columns,
                    instant=True,
                    mode="ttm",
                    output_fields=frozenset(self.output_model.model_fields),
                    kind=self.statement_kind,
                )
            else:
                rows = _ttm_rows(
                    rows,
                    columns=self.columns,
                    instant=self.instant,
                    kind=self.statement_kind,
                )
                if self.growth:
                    rows = _growth_rows(
                        rows,
                        columns=self.columns,
                        instant=False,
                        mode="ttm",
                        output_fields=frozenset(self.output_model.model_fields),
                        kind=self.statement_kind,
                    )
        elif self.growth:
            rows = _growth_rows(
                rows,
                columns=self.columns,
                instant=self.instant,
                mode=params.period,
                output_fields=frozenset(self.output_model.model_fields),
                kind=self.statement_kind,
            )
        if len(rows) > MAX_RESULT_ROWS:
            raise SecProviderError("SEC_XBRL_RESULT_TOO_LARGE")
        page = rows[params.offset : params.offset + params.limit]
        try:
            fields = self.output_model.model_fields
            return tuple(
                self.output_model.model_validate(
                    {key: value for key, value in row.items() if key in fields}
                )
                for row in page
            )
        except ValidationError:
            raise SecProviderError("SEC_XBRL_CONTRACT_INVALID") from None


class BalanceSheetFetcher(_SecStatementFetcher):
    """Expose SEC-reported US-GAAP instant facts for selected balance sheet concepts."""

    canonical_model: ClassVar[str] = "BalanceSheet"
    tags = BALANCE_SHEET_TAGS
    instant = True
    columns = BALANCE_SHEET_COLUMNS
    statement_kind = "balance"
    output_model = SecBalanceSheetStatement
    capability = Capability(
        asset_class="stock",
        domain="sec_balance_sheet",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class BalanceSheetGrowthFetcher(_SecStatementFetcher):
    """Compare SEC balance sheet instant facts to the closest prior-year period."""

    canonical_model: ClassVar[str] = "BalanceSheetGrowth"
    tags = BALANCE_SHEET_TAGS
    instant = True
    growth = True
    columns = BALANCE_SHEET_COLUMNS
    statement_kind = "balance"
    output_model = SecBalanceSheetGrowthRow
    capability = Capability(
        asset_class="stock",
        domain="sec_balance_sheet_growth",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class CashFlowStatementFetcher(_SecStatementFetcher):
    """Expose supported SEC-reported US-GAAP cash flow duration facts."""

    canonical_model: ClassVar[str] = "CashFlowStatement"
    tags = CASH_FLOW_TAGS
    instant = False
    columns = CASH_FLOW_COLUMNS
    statement_kind = "cash_flow"
    output_model = SecCashFlowStatementRow
    capability = Capability(
        asset_class="stock",
        domain="sec_cash_flow_statement",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class CashFlowStatementGrowthFetcher(_SecStatementFetcher):
    """Compare SEC cash flow facts to the closest comparable prior-year duration."""

    canonical_model: ClassVar[str] = "CashFlowStatementGrowth"
    tags = CASH_FLOW_TAGS
    instant = False
    growth = True
    columns = CASH_FLOW_COLUMNS
    statement_kind = "cash_flow"
    output_model = SecCashFlowStatementGrowthRow
    capability = Capability(
        asset_class="stock",
        domain="sec_cash_flow_statement_growth",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class IncomeStatementFetcher(_SecStatementFetcher):
    """Expose supported SEC-reported US-GAAP income statement duration facts."""

    canonical_model: ClassVar[str] = "IncomeStatement"
    tags = INCOME_STATEMENT_TAGS
    instant = False
    columns = INCOME_STATEMENT_COLUMNS
    statement_kind = "income"
    output_model = SecIncomeStatementRow
    capability = Capability(
        asset_class="stock",
        domain="sec_income_statement",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class IncomeStatementGrowthFetcher(_SecStatementFetcher):
    """Compare SEC income statement facts to comparable prior-year durations."""

    canonical_model: ClassVar[str] = "IncomeStatementGrowth"
    tags = INCOME_STATEMENT_TAGS
    instant = False
    growth = True
    columns = INCOME_STATEMENT_COLUMNS
    statement_kind = "income"
    output_model = SecIncomeStatementGrowthRow
    capability = Capability(
        asset_class="stock",
        domain="sec_income_statement_growth",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class CompareCompanyFactsQuery(MultiSymbolQueryParams):
    """Select companyfacts by public issuer, GAAP concept, and optional period."""

    symbol: str | list[str | None] | None = None
    fact: str | None = "Revenues"
    year: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    fiscal_period: Literal["fy", "q1", "q2", "q3", "q4"] | None = None
    instantaneous: bool | None = False
    use_cache: bool | None = True
    market: Literal["us"] = "us"

    @field_validator("symbol", mode="before")
    @classmethod
    def _valid_symbols(cls, value: object) -> object:
        if value is None:
            return value
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > 20:
            raise ValueError("symbol list must contain between 1 and 20 issuers")
        normalized: list[str] = []
        for symbol in values:
            if not isinstance(symbol, str):
                raise ValueError("SEC companyfacts requires non-null ticker symbols")
            ticker = symbol.strip().upper()
            if _TICKER_RE.fullmatch(ticker) is None:
                raise ValueError("symbol must be a short ticker token")
            if ticker not in normalized:
                normalized.append(ticker)
        return normalized if isinstance(value, list) else normalized[0]

    @field_validator("fact")
    @classmethod
    def _valid_fact(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if _XBRL_TAG_RE.fullmatch(value) is None:
            raise ValueError("fact must be a SEC taxonomy concept token")
        return value

    @model_validator(mode="after")
    def _require_bounded_symbols(self) -> CompareCompanyFactsQuery:
        if self.symbol is None:
            raise ValueError("SEC companyfacts requires one or more symbol values")
        if self.use_cache is False:
            raise ValueError("use_cache=False is unsupported by the governed SEC transport")
        return self


class CompareCompanyFactsFetcher(Fetcher[CompareCompanyFactsQuery, tuple[SecRawFact, ...]]):
    """Fetch SEC companyfacts per issuer and expose public comparison records."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "CompareCompanyFacts"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_compare_company_facts",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CompareCompanyFactsQuery:
        """Validate public SEC comparison arguments before source I/O."""
        _reject_unexpected(
            kwargs,
            {"symbol", "fact", "year", "fiscal_period", "instantaneous", "use_cache"},
        )
        return CompareCompanyFactsQuery.model_validate(kwargs)

    def extract_data(
        self, params: CompareCompanyFactsQuery, ctx: FetchContext
    ) -> tuple[SecRawFact, ...]:
        """Fetch each selected issuer's official CIK-prefixed companyfacts document."""
        symbols = params.symbol if isinstance(params.symbol, list) else [params.symbol]
        ensure_request_capacity(ctx, 1 + len(symbols))
        identities = company_ticker_rows(ctx)
        identities_by_symbol = {row["ticker"]: row for row in identities}
        selected: list[SecRawFact] = []
        for ticker in symbols:
            identity = identities_by_symbol.get(ticker or "")
            if identity is None:
                raise SecProviderError("SEC_TICKER_NOT_FOUND")
            cik = identity["cik"]
            document = fetch_json(ctx, f"{SEC_COMPANYFACTS_URL}/CIK{cik}.json")
            facts_root = document.get("facts")
            taxonomy = facts_root.get("us-gaap") if isinstance(facts_root, Mapping) else None
            if not isinstance(taxonomy, Mapping):
                if isinstance(facts_root, Mapping) and "us-gaap" not in facts_root:
                    continue
                raise SecProviderError("SEC_XBRL_BAD_SHAPE")
            aliases = _compare_fact_tags(params.fact)
            tags = frozenset(tag for tag in taxonomy if isinstance(tag, str))
            if aliases is not None:
                tags = tags.intersection(aliases)
            parse_params = SecFinancialQuery(
                cik=cik,
                period="annual",
                limit=MAX_RESULT_ROWS,
            )
            parsed = _parse_companyfacts(
                document,
                cik=cik,
                symbol=ticker,
                company_name=identity["name"],
                tags=tags,
                instant=None,
                params=parse_params,
            )
            for fact in parsed.facts:
                if params.year is not None and fact.period_end.year != params.year:
                    continue
                selected.append(fact)
                if len(selected) > MAX_FACT_ROWS:
                    raise SecProviderError("SEC_XBRL_TOO_MANY_FACTS")
        return tuple(selected)

    def transform_data(
        self,
        raw: tuple[SecRawFact, ...],
        params: CompareCompanyFactsQuery,
    ) -> FetchResult:
        """Project observations without merging or combining their units."""
        del params
        try:
            return tuple(
                SecCompanyFactComparison.model_validate(
                    {
                        "symbol": fact.symbol,
                        "name": fact.company_name,
                        "value": float(fact.value),
                        "reported_date": fact.filed_date,
                        "period_beginning": fact.period_start,
                        "period_ending": fact.period_end,
                        "form": fact.form,
                        "fiscal_year": fact.fiscal_year,
                        "fiscal_period": fact.fiscal_period,
                        "cik": fact.cik,
                        "location": None,
                        "frame": fact.frame,
                        "accession": fact.accession_number,
                        "fact": fact.label,
                        "unit": fact.unit,
                        "taxonomy": "us-gaap",
                        "tag": fact.tag,
                    }
                )
                for fact in raw
            )
        except ValidationError:
            raise SecProviderError("SEC_XBRL_COMPARISON_CONTRACT_INVALID") from None


def _compare_fact_tags(fact: str | None) -> frozenset[str] | None:
    if fact is None:
        return None
    if fact.casefold() == "revenues":
        return frozenset(
            {
                "Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet",
            }
        )
    return frozenset({fact})


__all__ = [
    "BalanceSheetFetcher",
    "BalanceSheetGrowthFetcher",
    "CashFlowStatementFetcher",
    "CashFlowStatementGrowthFetcher",
    "CompareCompanyFactsFetcher",
    "IncomeStatementFetcher",
    "IncomeStatementGrowthFetcher",
]
