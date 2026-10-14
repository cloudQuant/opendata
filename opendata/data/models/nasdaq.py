"""Typed outputs for the Nasdaq models supported by this provider adapter."""

from __future__ import annotations

from datetime import date, datetime

from opendata.data.models.base import ContractModel


class NasdaqCalendarDividend(ContractModel):
    """One Nasdaq calendar dividend announcement."""

    ex_dividend_date: date
    symbol: str
    amount: float | None = None
    name: str | None = None
    record_date: date | None = None
    payment_date: date | None = None
    declaration_date: date | None = None
    annualized_amount: float | None = None


class NasdaqCalendarEarnings(ContractModel):
    """One Nasdaq earnings calendar entry."""

    report_date: date
    symbol: str
    name: str | None = None
    eps_previous: float | None = None
    eps_consensus: float | None = None
    eps_actual: float | None = None
    surprise_percent: float | None = None
    num_estimates: int | None = None
    period_ending: str | None = None
    previous_report_date: date | None = None
    reporting_time: str | None = None
    market_cap: int | None = None


class NasdaqCalendarIpo(ContractModel):
    """One Nasdaq IPO or secondary-offering calendar entry."""

    symbol: str | None = None
    ipo_date: date | None = None
    name: str | None = None
    offer_amount: float | None = None
    share_count: int | None = None
    expected_price_date: date | None = None
    filed_date: date | None = None
    withdraw_date: date | None = None
    deal_status: str | None = None


class NasdaqCompanyFiling(ContractModel):
    """One filing indexed by Nasdaq."""

    filing_date: date
    report_type: str | None = None
    report_url: str
    period_ending: date | None = None
    name: str | None = None
    reporting_owner: str | None = None
    pdf_url: str | None = None
    xls_url: str | None = None
    xbr_url: str | None = None
    doc_link: str | None = None


class NasdaqEconomicEvent(ContractModel):
    """One event published by Nasdaq's economic calendar."""

    date: datetime | str | None = None
    country: str | None = None
    category: str | None = None
    event: str | None = None
    importance: str | None = None
    source: str | None = None
    currency: str | None = None
    unit: str | None = None
    consensus: str | float | None = None
    previous: str | float | None = None
    revised: str | float | None = None
    actual: str | float | None = None
    description: str | None = None


class NasdaqEquitySearch(ContractModel):
    """One Nasdaq-trader symbol directory row."""

    symbol: str
    name: str | None = None
    nasdaq_traded: str | None = None
    exchange: str | None = None
    market_category: str | None = None
    etf: str | None = None
    round_lot_size: float | None = None
    test_issue: str | None = None
    financial_status: str | None = None
    cqs_symbol: str | None = None
    nasdaq_symbol: str | None = None
    next_shares: str | None = None


class NasdaqEquityScreener(ContractModel):
    """One company row from Nasdaq's stock screener."""

    symbol: str
    name: str | None = None
    last_price: float
    change: float | None = None
    change_percent: float | None = None
    market_cap: int | None = None


class NasdaqHistoricalDividend(ContractModel):
    """One historical dividend payment for a symbol."""

    symbol: str | None = None
    ex_dividend_date: date
    amount: float
    dividend_type: str | None = None
    currency: str | None = None
    record_date: date | None = None
    payment_date: date | None = None
    declaration_date: date | None = None


class NasdaqTopRetail(ContractModel):
    """One top-retail-activity row from Nasdaq Data Link RTAT10."""

    date: date | str
    symbol: str
    activity: float
    sentiment: float


__all__ = [
    "NasdaqCalendarDividend",
    "NasdaqCalendarEarnings",
    "NasdaqCalendarIpo",
    "NasdaqCompanyFiling",
    "NasdaqEconomicEvent",
    "NasdaqEquitySearch",
    "NasdaqEquityScreener",
    "NasdaqHistoricalDividend",
    "NasdaqTopRetail",
]
