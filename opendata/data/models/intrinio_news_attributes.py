"""Typed Intrinio contracts for news, filings, attributes, and FRED series."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated

from pydantic import BeforeValidator, StrictBool, StrictInt, StrictStr

from opendata.data.models.base import ContractModel


def _finite_float(value: object) -> float:
    """Accept finite JSON numbers without coercing booleans or numeric text."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a finite JSON number")
    result = float(value)
    if result != result or result in (float("inf"), float("-inf")):
        raise ValueError("expected a finite JSON number")
    return result


FiniteFloat = Annotated[float, BeforeValidator(_finite_float)]


class IntrinioNewsCompany(ContractModel):
    """The documented, typed company identity subset nested in news rows."""

    id: StrictStr | None = None
    ticker: StrictStr | None = None
    name: StrictStr | None = None
    lei: StrictStr | None = None
    cik: StrictStr | None = None


class IntrinioNewsSecurity(ContractModel):
    """The documented, typed security identity subset nested in news rows."""

    id: StrictStr | None = None
    company_id: StrictStr | None = None
    ticker: StrictStr | None = None
    primary_listing: StrictBool | None = None


class _IntrinioNews(ContractModel):
    """Intrinio-specific CompanyNews/WorldNews fields shared by both contracts."""

    date: datetime
    title: StrictStr
    author: StrictStr | None = None
    excerpt: StrictStr | None = None
    url: StrictStr
    symbols: StrictStr | None = None
    source: StrictStr | None = None
    summary: StrictStr | None = None
    topics: StrictStr | None = None
    word_count: StrictInt | None = None
    business_relevance: FiniteFloat | None = None
    sentiment: StrictStr | None = None
    sentiment_confidence: FiniteFloat | None = None
    language: StrictStr | None = None
    spam: StrictBool | None = None
    copyright: StrictStr | None = None
    id: StrictStr


class IntrinioCompanyNews(_IntrinioNews):
    """One Intrinio article returned for a requested company identifier."""

    security: IntrinioNewsSecurity | None = None


class IntrinioWorldNews(_IntrinioNews):
    """One article from Intrinio's all-company news feed."""

    company: IntrinioNewsCompany | None = None
    security: IntrinioNewsSecurity | None = None


class IntrinioCompanyFiling(ContractModel):
    """One company filing with only fields supported by the API response."""

    filing_date: date
    report_type: StrictStr | None = None
    report_url: StrictStr
    filing_url: StrictStr | None = None
    symbol: StrictStr | None = None
    cik: StrictStr | None = None
    accepted_date: datetime | None = None
    id: StrictStr
    period_end_date: date | None = None
    sec_unique_id: StrictStr
    instance_url: StrictStr | None = None


class IntrinioSearchAttribute(ContractModel):
    """One financial data tag returned by Intrinio's tag search."""

    id: StrictStr
    name: StrictStr
    tag: StrictStr
    statement_code: StrictStr
    statement_type: StrictStr | None = None
    parent_name: StrictStr | None = None
    sequence: StrictInt | None = None
    factor: StrictStr | None = None
    transaction: StrictStr | None = None
    type: StrictStr | None = None
    unit: StrictStr | None = None


class IntrinioLatestAttribute(ContractModel):
    """One latest security datapoint, retaining explicit source value type."""

    symbol: StrictStr
    tag: StrictStr | None = None
    value: StrictStr | FiniteFloat | None


class IntrinioHistoricalAttribute(ContractModel):
    """One numeric security datapoint observation; units/PIT are not inferred."""

    date: date
    symbol: StrictStr
    tag: StrictStr | None = None
    value: FiniteFloat | None


class IntrinioFredSeries(ContractModel):
    """One economic-index observation returned for a single FRED identifier."""

    date: date
    value: FiniteFloat | None


__all__ = [
    "IntrinioCompanyFiling",
    "IntrinioCompanyNews",
    "IntrinioFredSeries",
    "IntrinioHistoricalAttribute",
    "IntrinioLatestAttribute",
    "IntrinioNewsCompany",
    "IntrinioNewsSecurity",
    "IntrinioSearchAttribute",
    "IntrinioWorldNews",
]
