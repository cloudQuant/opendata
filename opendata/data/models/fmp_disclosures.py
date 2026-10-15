"""Typed FMP company-news, filings, ownership, and disclosure contracts.

The fields mirror the stable FMP response examples cited in
``docs/evidence/C100/fmp-disclosures/README.md``. They intentionally retain
source units and source text where the API does not define a stronger meaning.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, ClassVar, Literal

from pydantic import ConfigDict, Field, model_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from typing_extensions import Self


class _FmpDisclosureModel(ContractModel):
    """Strict shared source parsing for the FMP disclosure family."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)
    _date_fields: ClassVar[frozenset[str]] = frozenset()
    _datetime_fields: ClassVar[frozenset[str]] = frozenset()
    _numeric_fields: ClassVar[frozenset[str]] = frozenset()
    _integer_fields: ClassVar[frozenset[str]] = frozenset()
    _boolean_fields: ClassVar[frozenset[str]] = frozenset()
    _empty_text_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _validate_source_values(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        parsed = dict(value)
        for name in cls._date_fields:
            key = _input_key(cls, parsed, name)
            if key is not None:
                parsed[key] = _parse_source_date(parsed[key])
        for name in cls._datetime_fields:
            key = _input_key(cls, parsed, name)
            if key is not None:
                parsed[key] = _parse_source_datetime(parsed[key])
        for name in cls._numeric_fields:
            key = _input_key(cls, parsed, name)
            if key is not None:
                _require_finite_number(parsed[key], name)
        for name in cls._integer_fields:
            key = _input_key(cls, parsed, name)
            if key is not None and parsed[key] is not None and type(parsed[key]) is not int:
                raise ValueError(f"{name} must be an integer")
        for name in cls._boolean_fields:
            key = _input_key(cls, parsed, name)
            if key is not None and parsed[key] is not None and type(parsed[key]) is not bool:
                raise ValueError(f"{name} must be a boolean")
        return parsed

    @model_validator(mode="after")
    def _validate_text_cells(self) -> Self:
        for name, value in self.__dict__.items():
            if not isinstance(value, str):
                continue
            if not value and (
                name in self._empty_text_fields or not type(self).model_fields[name].is_required()
            ):
                continue
            if not value.strip() or any(ord(char) < 32 and char not in "\t\n\r" for char in value):
                raise ValueError(f"{name} must be non-empty source text")
        return self


def _input_key(
    model: type[_FmpDisclosureModel], values: Mapping[str, object], name: str
) -> str | None:
    field = model.model_fields[name]
    alias = field.alias
    if alias is not None and alias in values:
        return alias
    return name if name in values else None


def _parse_source_date(value: object) -> object:
    if value is None or value == "":
        return value
    if isinstance(value, datetime):
        raise ValueError("FMP date fields must not include a time")
    if isinstance(value, date_type):
        return value
    if not isinstance(value, str):
        raise ValueError("FMP date fields must be ISO dates")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("FMP date fields must be ISO dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("FMP date fields must be ISO dates")
    return parsed


def _parse_source_datetime(value: object) -> object:
    if value is None or value == "":
        return value
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or ("T" not in value and " " not in value):
        raise ValueError("FMP timestamp fields must include a time")
    iso_value = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(iso_value)
    except ValueError as exc:
        raise ValueError("FMP timestamp fields must be ISO timestamps") from exc


def _require_finite_number(value: object, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a JSON number")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise ValueError(f"{field_name} must be finite")


class FmpCompanyNews(_FmpDisclosureModel):
    """Company news story with its publication timestamp and source URL."""

    symbol: str | None = None
    published_date: datetime = Field(alias="publishedDate")
    publisher: str | None = None
    title: str
    image: str | None = None
    site: str | None = None
    text: str | None = None
    url: str
    _datetime_fields = frozenset({"published_date"})
    _empty_text_fields = frozenset()


class FmpWorldNews(_FmpDisclosureModel):
    """World news story with its publication timestamp and source URL."""

    symbol: str | None = None
    published_date: datetime = Field(alias="publishedDate")
    publisher: str | None = None
    title: str
    image: str | None = None
    site: str | None = None
    text: str | None = None
    url: str
    _datetime_fields = frozenset({"published_date"})


class FmpCompanyFiling(_FmpDisclosureModel):
    """SEC filing listed for a company and its filing timestamps."""

    symbol: str
    cik: str
    filing_date: datetime = Field(alias="filingDate")
    accepted_date: datetime | None = Field(default=None, alias="acceptedDate")
    form_type: str = Field(alias="formType")
    has_financials: bool | None = Field(default=None, alias="hasFinancials")
    link: str
    final_link: str | None = Field(default=None, alias="finalLink")
    _datetime_fields = frozenset({"filing_date", "accepted_date"})
    _boolean_fields = frozenset({"has_financials"})


class FmpDiscoveryFiling(FmpCompanyFiling):
    """One filing from the market-wide SEC filings discovery endpoint."""


class FmpHistoricalDividend(_FmpDisclosureModel):
    """Historical cash dividend event and optional lifecycle dates."""

    symbol: str
    date: date_type
    record_date: date_type | Literal[""] | None = Field(default=None, alias="recordDate")
    payment_date: date_type | Literal[""] | None = Field(default=None, alias="paymentDate")
    declaration_date: date_type | Literal[""] | None = Field(default=None, alias="declarationDate")
    adjusted_dividend: int | float | None = Field(default=None, alias="adjDividend")
    dividend: int | float | None = None
    yield_percent: int | float | None = Field(default=None, alias="yield")
    frequency: str | None = None
    _date_fields = frozenset({"date", "record_date", "payment_date", "declaration_date"})
    _numeric_fields = frozenset({"adjusted_dividend", "dividend", "yield_percent"})
    _empty_text_fields = frozenset({"record_date", "payment_date", "declaration_date"})


class FmpHistoricalSplit(_FmpDisclosureModel):
    """Historical stock split ratio and event date."""

    symbol: str
    date: date_type
    numerator: int
    denominator: int
    split_type: str = Field(alias="splitType")
    _date_fields = frozenset({"date"})
    _integer_fields = frozenset({"numerator", "denominator"})


class FmpHistoricalEps(_FmpDisclosureModel):
    """Historical earnings and revenue estimates with actuals."""

    symbol: str
    date: date_type
    eps_actual: int | float | None = Field(default=None, alias="epsActual")
    eps_estimated: int | float | None = Field(default=None, alias="epsEstimated")
    revenue_actual: int | float | None = Field(default=None, alias="revenueActual")
    revenue_estimated: int | float | None = Field(default=None, alias="revenueEstimated")
    last_updated: date_type | None = Field(default=None, alias="lastUpdated")
    _date_fields = frozenset({"date", "last_updated"})
    _numeric_fields = frozenset(
        {"eps_actual", "eps_estimated", "revenue_actual", "revenue_estimated"}
    )


class FmpHistoricalEmployee(_FmpDisclosureModel):
    """Historical company employee count reported in a filing."""

    symbol: str
    cik: str
    acceptance_time: datetime = Field(alias="acceptanceTime")
    period_of_report: date_type = Field(alias="periodOfReport")
    company_name: str = Field(alias="companyName")
    form_type: str = Field(alias="formType")
    filing_date: date_type = Field(alias="filingDate")
    employee_count: int = Field(alias="employeeCount")
    source: str
    _date_fields = frozenset({"period_of_report", "filing_date"})
    _datetime_fields = frozenset({"acceptance_time"})
    _integer_fields = frozenset({"employee_count"})


class FmpHistoricalMarketCap(_FmpDisclosureModel):
    """Historical market capitalization observation for one symbol."""

    symbol: str
    date: date_type
    market_cap: int | float = Field(alias="marketCap")
    _date_fields = frozenset({"date"})
    _numeric_fields = frozenset({"market_cap"})


class FmpEarningsCallTranscript(_FmpDisclosureModel):
    """Transcript text and identity for one earnings call."""

    symbol: str
    period: str
    year: int
    date: date_type
    content: str
    _date_fields = frozenset({"date"})
    _integer_fields = frozenset({"year"})


class FmpPriceTarget(_FmpDisclosureModel):
    """Analyst price target and its publication metadata."""

    symbol: str
    published_date: datetime = Field(alias="publishedDate")
    news_url: str | None = Field(default=None, alias="newsURL")
    news_title: str | None = Field(default=None, alias="newsTitle")
    analyst_name: str | None = Field(default=None, alias="analystName")
    price_target: int | float = Field(alias="priceTarget")
    adjusted_price_target: int | float | None = Field(default=None, alias="adjPriceTarget")
    price_when_posted: int | float | None = Field(default=None, alias="priceWhenPosted")
    news_publisher: str | None = Field(default=None, alias="newsPublisher")
    news_base_url: str | None = Field(default=None, alias="newsBaseURL")
    analyst_company: str | None = Field(default=None, alias="analystCompany")
    _datetime_fields = frozenset({"published_date"})
    _numeric_fields = frozenset({"price_target", "adjusted_price_target", "price_when_posted"})


class FmpPriceTargetConsensus(_FmpDisclosureModel):
    """Aggregate high, low, consensus, and median analyst targets."""

    symbol: str
    target_high: int | float = Field(alias="targetHigh")
    target_low: int | float = Field(alias="targetLow")
    target_consensus: int | float = Field(alias="targetConsensus")
    target_median: int | float = Field(alias="targetMedian")
    _numeric_fields = frozenset({"target_high", "target_low", "target_consensus", "target_median"})


class FmpKeyExecutive(_FmpDisclosureModel):
    """Key executive identity, role, compensation, and tenure fields."""

    symbol: str
    title: str
    name: str
    pay: int | float | None = None
    currency_pay: str | None = Field(default=None, alias="currencyPay")
    gender: str | None = None
    year_born: int | None = Field(default=None, alias="yearBorn")
    title_since: date_type | int | None = Field(default=None, alias="titleSince")
    active: bool | None = None
    _numeric_fields = frozenset({"pay"})
    _integer_fields = frozenset({"year_born"})
    _boolean_fields = frozenset({"active"})

    @model_validator(mode="before")
    @classmethod
    def _parse_title_since(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        parsed = dict(value)
        key = _input_key(cls, parsed, "title_since")
        if key is None or parsed[key] is None:
            return parsed
        raw = parsed[key]
        if type(raw) is int and raw >= 0:
            return parsed
        # The contract re-validates its own parsed rows, so an already-decoded date must round-trip.
        if isinstance(raw, datetime):
            raise ValueError("titleSince must be an ISO date or Unix timestamp")
        if isinstance(raw, date_type):
            return parsed
        if isinstance(raw, str):
            try:
                as_date = date_type.fromisoformat(raw)
            except ValueError:
                raise ValueError("titleSince must be an ISO date or Unix timestamp") from None
            if as_date.isoformat() != raw:
                raise ValueError("titleSince must be an ISO date or Unix timestamp")
            parsed[key] = as_date
            return parsed
        raise ValueError("titleSince must be an ISO date or Unix timestamp")


class FmpExecutiveCompensation(_FmpDisclosureModel):
    """Executive compensation details published in a company filing."""

    cik: str
    symbol: str
    company_name: str = Field(alias="companyName")
    filing_date: date_type = Field(alias="filingDate")
    accepted_date: datetime = Field(alias="acceptedDate")
    name_and_position: str = Field(alias="nameAndPosition")
    year: int
    salary: int | float | None = None
    bonus: int | float | None = None
    stock_award: int | float | None = Field(default=None, alias="stockAward")
    option_award: int | float | None = Field(default=None, alias="optionAward")
    incentive_plan_compensation: int | float | None = Field(
        default=None, alias="incentivePlanCompensation"
    )
    all_other_compensation: int | float | None = Field(default=None, alias="allOtherCompensation")
    total: int | float | None = None
    link: str
    _date_fields = frozenset({"filing_date"})
    _datetime_fields = frozenset({"accepted_date"})
    _integer_fields = frozenset({"year"})
    _numeric_fields = frozenset(
        {
            "salary",
            "bonus",
            "stock_award",
            "option_award",
            "incentive_plan_compensation",
            "all_other_compensation",
            "total",
        }
    )


class FmpGovernmentTrade(_FmpDisclosureModel):
    """Disclosed congressional trade and its transaction details."""

    symbol: str
    chamber: Literal["house", "senate"]
    member_id: str = Field(alias="senateID")
    disclosure_date: date_type = Field(alias="disclosureDate")
    transaction_date: date_type = Field(alias="transactionDate")
    first_name: str = Field(alias="firstName")
    last_name: str = Field(alias="lastName")
    office: str
    district: str
    owner: str
    asset_description: str = Field(alias="assetDescription")
    asset_type: str = Field(alias="assetType")
    transaction_type: str = Field(alias="type")
    amount: str
    capital_gains_over_200_usd: str | None = Field(default=None, alias="capitalGainsOver200USD")
    comment: str
    link: str
    _date_fields = frozenset({"disclosure_date", "transaction_date"})
    _empty_text_fields = frozenset({"district", "owner", "comment"})


class FmpInsiderTrade(_FmpDisclosureModel):
    """Insider transaction details and securities quantities."""

    symbol: str
    filing_date: date_type = Field(alias="filingDate")
    transaction_date: date_type = Field(alias="transactionDate")
    reporting_cik: str = Field(alias="reportingCik")
    company_cik: str = Field(alias="companyCik")
    transaction_type: str = Field(alias="transactionType")
    securities_owned: int | float = Field(alias="securitiesOwned")
    reporting_name: str = Field(alias="reportingName")
    type_of_owner: str = Field(alias="typeOfOwner")
    acquisition_or_disposition: str = Field(alias="acquisitionOrDisposition")
    direct_or_indirect: str = Field(alias="directOrIndirect")
    form_type: str = Field(alias="formType")
    securities_transacted: int | float = Field(alias="securitiesTransacted")
    price: int | float | None = None
    security_name: str = Field(alias="securityName")
    url: str
    _date_fields = frozenset({"filing_date", "transaction_date"})
    _numeric_fields = frozenset({"securities_owned", "securities_transacted", "price"})


class FmpInstitutionalOwnership(_FmpDisclosureModel):
    """Institutional ownership totals and period-over-period changes."""

    symbol: str
    cik: str
    date: date_type
    investors_holding: int = Field(alias="investorsHolding")
    last_investors_holding: int = Field(alias="lastInvestorsHolding")
    investors_holding_change: int = Field(alias="investorsHoldingChange")
    number_of_13f_shares: int | float = Field(alias="numberOf13Fshares")
    last_number_of_13f_shares: int | float = Field(alias="lastNumberOf13Fshares")
    number_of_13f_shares_change: int | float = Field(alias="numberOf13FsharesChange")
    total_invested: int | float = Field(alias="totalInvested")
    last_total_invested: int | float = Field(alias="lastTotalInvested")
    total_invested_change: int | float = Field(alias="totalInvestedChange")
    ownership_percent: int | float = Field(alias="ownershipPercent")
    last_ownership_percent: int | float = Field(alias="lastOwnershipPercent")
    ownership_percent_change: int | float = Field(alias="ownershipPercentChange")
    new_positions: int = Field(alias="newPositions")
    last_new_positions: int = Field(alias="lastNewPositions")
    new_positions_change: int = Field(alias="newPositionsChange")
    increased_positions: int = Field(alias="increasedPositions")
    last_increased_positions: int = Field(alias="lastIncreasedPositions")
    increased_positions_change: int = Field(alias="increasedPositionsChange")
    closed_positions: int = Field(alias="closedPositions")
    last_closed_positions: int = Field(alias="lastClosedPositions")
    closed_positions_change: int = Field(alias="closedPositionsChange")
    reduced_positions: int = Field(alias="reducedPositions")
    last_reduced_positions: int = Field(alias="lastReducedPositions")
    reduced_positions_change: int = Field(alias="reducedPositionsChange")
    total_calls: int | float = Field(alias="totalCalls")
    last_total_calls: int | float = Field(alias="lastTotalCalls")
    total_calls_change: int | float = Field(alias="totalCallsChange")
    total_puts: int | float = Field(alias="totalPuts")
    last_total_puts: int | float = Field(alias="lastTotalPuts")
    total_puts_change: int | float = Field(alias="totalPutsChange")
    put_call_ratio: int | float = Field(alias="putCallRatio")
    last_put_call_ratio: int | float = Field(alias="lastPutCallRatio")
    put_call_ratio_change: int | float = Field(alias="putCallRatioChange")
    _date_fields = frozenset({"date"})
    _integer_fields = frozenset(
        {
            "investors_holding",
            "last_investors_holding",
            "investors_holding_change",
            "new_positions",
            "last_new_positions",
            "new_positions_change",
            "increased_positions",
            "last_increased_positions",
            "increased_positions_change",
            "closed_positions",
            "last_closed_positions",
            "closed_positions_change",
            "reduced_positions",
            "last_reduced_positions",
            "reduced_positions_change",
        }
    )
    _numeric_fields = frozenset(
        {
            "number_of_13f_shares",
            "last_number_of_13f_shares",
            "number_of_13f_shares_change",
            "total_invested",
            "last_total_invested",
            "total_invested_change",
            "ownership_percent",
            "last_ownership_percent",
            "ownership_percent_change",
            "total_calls",
            "last_total_calls",
            "total_calls_change",
            "total_puts",
            "last_total_puts",
            "total_puts_change",
            "put_call_ratio",
            "last_put_call_ratio",
            "put_call_ratio_change",
        }
    )


class FmpEquityOwnership(_FmpDisclosureModel):
    """Beneficial ownership detail for a reported equity position."""

    cik: str
    symbol: str
    filing_date: date_type = Field(alias="filingDate")
    accepted_date: date_type = Field(alias="acceptedDate")
    cusip: str
    name_of_reporting_person: str = Field(alias="nameOfReportingPerson")
    citizenship_or_place_of_organization: str = Field(alias="citizenshipOrPlaceOfOrganization")
    sole_voting_power: str = Field(alias="soleVotingPower")
    shared_voting_power: str = Field(alias="sharedVotingPower")
    sole_dispositive_power: str = Field(alias="soleDispositivePower")
    shared_dispositive_power: str = Field(alias="sharedDispositivePower")
    amount_beneficially_owned: str = Field(alias="amountBeneficiallyOwned")
    percent_of_class: str = Field(alias="percentOfClass")
    type_of_reporting_person: str = Field(alias="typeOfReportingPerson")
    url: str
    _date_fields = frozenset({"filing_date", "accepted_date"})


__all__ = [
    "FmpCompanyNews",
    "FmpCompanyFiling",
    "FmpDiscoveryFiling",
    "FmpEarningsCallTranscript",
    "FmpEquityOwnership",
    "FmpExecutiveCompensation",
    "FmpGovernmentTrade",
    "FmpHistoricalDividend",
    "FmpHistoricalEmployee",
    "FmpHistoricalEps",
    "FmpHistoricalMarketCap",
    "FmpHistoricalSplit",
    "FmpInsiderTrade",
    "FmpInstitutionalOwnership",
    "FmpKeyExecutive",
    "FmpPriceTarget",
    "FmpPriceTargetConsensus",
    "FmpWorldNews",
]
