"""Strict source-shaped FMP contracts for the Iteration 2 directory and calendar models."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import ConfigDict, Field, field_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from typing_extensions import Self


class AvailableIndex(ContractModel):
    """One index in the FMP stock market indexes list."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    name: str
    exchange: str
    currency: str

    @field_validator("symbol", "name", "exchange")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("required source text is invalid")
        return value


class DividendCalendarEvent(ContractModel):
    """One FMP market-wide dividend calendar row."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    record_date: date_type | Literal[""] | None = Field(default=None, alias="recordDate")
    payment_date: date_type | Literal[""] | None = Field(default=None, alias="paymentDate")
    declaration_date: date_type | Literal[""] | None = Field(default=None, alias="declarationDate")
    adjusted_dividend: int | float | None = Field(default=None, alias="adjDividend")
    dividend: int | float | None = None
    yield_percent: int | float | None = Field(default=None, alias="yield")
    frequency: str | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source symbol is invalid")
        return value

    @field_validator("date", "record_date", "payment_date", "declaration_date", mode="before")
    @classmethod
    def _validate_source_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("adjusted_dividend", "dividend", "yield_percent", mode="before")
    @classmethod
    def _validate_finite_source_number(cls, value: object) -> object:
        return _validate_finite_number(value)

    @classmethod
    def to_frame(cls, rows: Sequence[Self]) -> pd.DataFrame:
        """Keep blank provider date strings and nullable amounts intact in frames."""
        frame = super().to_frame(rows)
        if rows:
            for column in ("record_date", "payment_date", "declaration_date"):
                frame[column] = pd.Series(
                    [getattr(row, column) for row in rows],
                    index=frame.index,
                    dtype=object,
                )
            for column in ("adjusted_dividend", "dividend", "yield_percent"):
                frame[column] = pd.Series(
                    [getattr(row, column) for row in rows],
                    index=frame.index,
                    dtype=object,
                )
        return frame


class EarningsCalendarEvent(ContractModel):
    """One FMP earnings calendar row with source date and amount fields."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    eps_actual: int | float | None = Field(default=None, alias="epsActual")
    eps_estimated: int | float | None = Field(default=None, alias="epsEstimated")
    revenue_actual: int | float | None = Field(default=None, alias="revenueActual")
    revenue_estimated: int | float | None = Field(default=None, alias="revenueEstimated")
    last_updated: date_type | None = Field(default=None, alias="lastUpdated")

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source symbol is invalid")
        return value

    @field_validator("date", "last_updated", mode="before")
    @classmethod
    def _validate_source_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator(
        "eps_actual",
        "eps_estimated",
        "revenue_actual",
        "revenue_estimated",
        mode="before",
    )
    @classmethod
    def _validate_finite_source_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class FmpCalendarEvent(ContractModel):
    """One normalized FMP company-calendar event in the OpenBB public shape.

    The public CalendarEvents output contract exposes only the event date and
    symbol, so source-specific event labels are intentionally not added here.
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    date: date_type
    symbol: str

    @field_validator("date", mode="before")
    @classmethod
    def _validate_event_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("symbol")
    @classmethod
    def _validate_event_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source symbol is invalid")
        return value


class IpoCalendarEvent(ContractModel):
    """One FMP IPO calendar row, including the 2026 deal-size field."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    daa: datetime | None
    company: str
    exchange: str
    actions: str | None
    shares: int | None
    price_range: str | None = Field(alias="priceRange")
    market_cap: int | float | None = Field(alias="marketCap")
    deal_size: int | float | None = Field(alias="dealSize")

    @field_validator("symbol", "company", "exchange")
    @classmethod
    def _validate_ipo_required_text(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("actions", "price_range")
    @classmethod
    def _validate_ipo_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_ipo_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("daa", mode="before")
    @classmethod
    def _validate_ipo_timestamp(cls, value: object) -> object:
        return _validate_source_datetime(value)

    @field_validator("shares", mode="before")
    @classmethod
    def _validate_ipo_shares(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("IPO shares must be a non-negative integer")
        return value

    @field_validator("market_cap", "deal_size", mode="before")
    @classmethod
    def _validate_ipo_amount(cls, value: object) -> object:
        return _validate_finite_number(value)


class SplitsCalendarEvent(ContractModel):
    """One FMP stock splits calendar row."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    numerator: int
    denominator: int
    split_type: str = Field(alias="splitType")

    @field_validator("symbol", "split_type")
    @classmethod
    def _validate_split_text(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_split_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("numerator", "denominator", mode="before")
    @classmethod
    def _validate_split_ratio(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("split ratio values must be positive integers")
        return value


class EconomicCalendarEvent(ContractModel):
    """One FMP economic release, normalized to an aware UTC timestamp."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    date: datetime
    country: str
    event: str
    currency: str | None = None
    previous: int | float | None = None
    estimate: int | float | None = None
    actual: int | float | None = None
    change: int | float | None = None
    impact: str | None = None
    change_percentage: int | float | None = Field(default=None, alias="changePercentage")
    unit: str | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_economic_timestamp(cls, value: object) -> object:
        return _validate_source_datetime(value)

    @field_validator("country", "event")
    @classmethod
    def _validate_economic_required_text(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("currency", "impact", "unit")
    @classmethod
    def _validate_economic_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _validate_source_text(value)

    @field_validator(
        "previous",
        "estimate",
        "actual",
        "change",
        "change_percentage",
        mode="before",
    )
    @classmethod
    def _validate_economic_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class TreasuryRateObservation(ContractModel):
    """One date of U.S. Treasury rates across the documented maturities."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    date: date_type
    month1: int | float | None
    month2: int | float | None
    month3: int | float | None
    month6: int | float | None
    year1: int | float | None
    year2: int | float | None
    year3: int | float | None
    year5: int | float | None
    year7: int | float | None
    year10: int | float | None
    year20: int | float | None
    year30: int | float | None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_treasury_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator(
        "month1",
        "month2",
        "month3",
        "month6",
        "year1",
        "year2",
        "year3",
        "year5",
        "year7",
        "year10",
        "year20",
        "year30",
        mode="before",
    )
    @classmethod
    def _validate_treasury_rate(cls, value: object) -> object:
        return _validate_finite_number(value)


class MarketRiskPremium(ContractModel):
    """One FMP country and total equity risk premium observation."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    country: str
    continent: str | None
    country_risk_premium: int | float | None = Field(alias="countryRiskPremium")
    total_equity_risk_premium: int | float | None = Field(alias="totalEquityRiskPremium")

    @field_validator("country")
    @classmethod
    def _validate_risk_country(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("continent")
    @classmethod
    def _validate_risk_continent(cls, value: str | None) -> str | None:
        return None if value is None else _validate_source_text(value)

    @field_validator("country_risk_premium", "total_equity_risk_premium", mode="before")
    @classmethod
    def _validate_risk_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class AnalystEstimate(ContractModel):
    """One FMP annual or quarterly analyst-estimate row."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    revenue_low: int | float | None = Field(alias="revenueLow")
    revenue_high: int | float | None = Field(alias="revenueHigh")
    revenue_average: int | float | None = Field(alias="revenueAvg")
    ebitda_low: int | float | None = Field(alias="ebitdaLow")
    ebitda_high: int | float | None = Field(alias="ebitdaHigh")
    ebitda_average: int | float | None = Field(alias="ebitdaAvg")
    ebit_low: int | float | None = Field(alias="ebitLow")
    ebit_high: int | float | None = Field(alias="ebitHigh")
    ebit_average: int | float | None = Field(alias="ebitAvg")
    net_income_low: int | float | None = Field(alias="netIncomeLow")
    net_income_high: int | float | None = Field(alias="netIncomeHigh")
    net_income_average: int | float | None = Field(alias="netIncomeAvg")
    sga_expense_low: int | float | None = Field(alias="sgaExpenseLow")
    sga_expense_high: int | float | None = Field(alias="sgaExpenseHigh")
    sga_expense_average: int | float | None = Field(alias="sgaExpenseAvg")
    eps_average: int | float | None = Field(alias="epsAvg")
    eps_high: int | float | None = Field(alias="epsHigh")
    eps_low: int | float | None = Field(alias="epsLow")
    num_analysts_revenue: int | None = Field(alias="numAnalystsRevenue")
    num_analysts_eps: int | None = Field(alias="numAnalystsEps")

    @field_validator("symbol")
    @classmethod
    def _validate_analyst_symbol(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_analyst_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator(
        "revenue_low",
        "revenue_high",
        "revenue_average",
        "ebitda_low",
        "ebitda_high",
        "ebitda_average",
        "ebit_low",
        "ebit_high",
        "ebit_average",
        "net_income_low",
        "net_income_high",
        "net_income_average",
        "sga_expense_low",
        "sga_expense_high",
        "sga_expense_average",
        "eps_average",
        "eps_high",
        "eps_low",
        mode="before",
    )
    @classmethod
    def _validate_analyst_number(cls, value: object) -> object:
        return _validate_finite_number(value)

    @field_validator("num_analysts_revenue", "num_analysts_eps", mode="before")
    @classmethod
    def _validate_analyst_count(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("analyst counts must be non-negative integers")
        return value


class ForwardEbitdaEstimate(ContractModel):
    """Forward EBITDA range and consensus projected from the estimates route."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    ebitda_low: int | float | None = Field(alias="ebitdaLow")
    ebitda_high: int | float | None = Field(alias="ebitdaHigh")
    ebitda_average: int | float | None = Field(alias="ebitdaAvg")

    @field_validator("symbol")
    @classmethod
    def _validate_forward_ebitda_symbol(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_forward_ebitda_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("ebitda_low", "ebitda_high", "ebitda_average", mode="before")
    @classmethod
    def _validate_forward_ebitda_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class ForwardEpsEstimate(ContractModel):
    """Forward EPS range and consensus projected from the estimates route."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    eps_average: int | float | None = Field(alias="epsAvg")
    eps_high: int | float | None = Field(alias="epsHigh")
    eps_low: int | float | None = Field(alias="epsLow")
    num_analysts_eps: int | None = Field(alias="numAnalystsEps")

    @field_validator("symbol")
    @classmethod
    def _validate_forward_eps_symbol(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_forward_eps_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("eps_average", "eps_high", "eps_low", mode="before")
    @classmethod
    def _validate_forward_eps_number(cls, value: object) -> object:
        return _validate_finite_number(value)

    @field_validator("num_analysts_eps", mode="before")
    @classmethod
    def _validate_forward_eps_count(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("analyst count must be a non-negative integer")
        return value


class FmpEsgScore(ContractModel):
    """One FMP ESG disclosure with its published score components."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    date: date_type
    accepted_date: date_type = Field(alias="acceptedDate")
    symbol: str
    cik: str
    company_name: str = Field(alias="companyName")
    form_type: str = Field(alias="formType")
    environmental_score: int | float | None = Field(alias="environmentalScore")
    social_score: int | float | None = Field(alias="socialScore")
    governance_score: int | float | None = Field(alias="governanceScore")
    esg_score: int | float | None = Field(alias="ESGScore")
    url: str

    @field_validator("symbol", "cik", "company_name", "form_type", "url")
    @classmethod
    def _validate_esg_text(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator("date", "accepted_date", mode="before")
    @classmethod
    def _validate_esg_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator(
        "environmental_score",
        "social_score",
        "governance_score",
        "esg_score",
        mode="before",
    )
    @classmethod
    def _validate_esg_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class NportDisclosureHolding(ContractModel):
    """One holding row from the FMP mutual-fund N-PORT disclosure route."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    cik: str
    fund_symbol: str
    date: date_type
    accepted_date: str = Field(alias="acceptedDate")
    symbol: str
    name: str
    lei: str | None = None
    title: str
    cusip: str | None = None
    isin: str | None = None
    balance: int | float | None
    units: str | None
    currency_code: str | None = Field(alias="cur_cd")
    value_usd: int | float | None = Field(alias="valUsd")
    portfolio_percent: int | float | None = Field(alias="pctVal")
    payoff_profile: str | None = Field(alias="payoffProfile")
    asset_category: str | None = Field(alias="assetCat")
    issuer_category: str | None = Field(alias="issuerCat")
    investment_country: str | None = Field(alias="invCountry")
    is_restricted_security: str | None = Field(alias="isRestrictedSec")
    fair_value_level: str | None = Field(alias="fairValLevel")
    is_cash_collateral: str | None = Field(alias="isCashCollateral")
    is_non_cash_collateral: str | None = Field(alias="isNonCashCollateral")
    is_loan_by_fund: str | None = Field(alias="isLoanByFund")

    @field_validator("cik", "fund_symbol", "symbol", "name", "title", "accepted_date")
    @classmethod
    def _validate_nport_required_text(cls, value: str) -> str:
        return _validate_source_text(value)

    @field_validator(
        "lei",
        "cusip",
        "isin",
        "units",
        "currency_code",
        "payoff_profile",
        "asset_category",
        "issuer_category",
        "investment_country",
        "is_restricted_security",
        "fair_value_level",
        "is_cash_collateral",
        "is_non_cash_collateral",
        "is_loan_by_fund",
    )
    @classmethod
    def _validate_nport_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _validate_source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_nport_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("balance", "value_usd", "portfolio_percent", mode="before")
    @classmethod
    def _validate_nport_number(cls, value: object) -> object:
        return _validate_finite_number(value)


class FmpYieldCurveObservation(ContractModel):
    """One FMP Treasury maturity in long form; rates remain source percentage points."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    date: date_type
    maturity: Literal["1M", "2M", "3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "20Y", "30Y"]
    rate: int | float | None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_yield_curve_date(cls, value: object) -> object:
        return _validate_calendar_date(value)

    @field_validator("rate", mode="before")
    @classmethod
    def _validate_yield_curve_rate(cls, value: object) -> object:
        return _validate_finite_number(value)


def _validate_calendar_date(value: object) -> object:
    """Accept an exact ISO date, a date object, nullable dates, or documented empty cells."""
    if value is None or value == "":
        return value
    if isinstance(value, datetime):
        raise ValueError("calendar dates must not include a time")
    if isinstance(value, date_type):
        return value
    if not isinstance(value, str):
        raise ValueError("calendar date must be an ISO date")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("calendar date must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("calendar date must be an ISO date")
    return parsed


def _validate_finite_number(value: object) -> object:
    """Reject booleans, strings, NaN, and infinity in numeric source fields."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("source numeric field must be a finite number")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, ValueError):
        finite = False
    if not finite:
        raise ValueError("source numeric field must be finite")
    return value


def _validate_source_text(value: str) -> str:
    """Reject blank/control-character text without rewriting source values."""
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("source text is invalid")
    return value


def _validate_source_datetime(value: object) -> object:
    """Parse documented UTC timestamps and normalize explicit offsets to UTC."""
    if value is None or value == "":
        return value
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        if (
            re.fullmatch(
                r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
                value,
            )
            is None
        ):
            raise ValueError("source timestamp must be ISO date-time")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("source timestamp must be ISO date-time") from exc
    else:
        raise ValueError("source timestamp must be ISO date-time")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


__all__ = [
    "AvailableIndex",
    "DividendCalendarEvent",
    "FmpCalendarEvent",
    "EarningsCalendarEvent",
    "IpoCalendarEvent",
    "SplitsCalendarEvent",
    "EconomicCalendarEvent",
    "TreasuryRateObservation",
    "MarketRiskPremium",
    "AnalystEstimate",
    "ForwardEbitdaEstimate",
    "ForwardEpsEstimate",
    "FmpEsgScore",
    "NportDisclosureHolding",
    "FmpYieldCurveObservation",
]
