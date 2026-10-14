"""Typed SEC filing, disclosure, and document response contracts."""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from opendata.data.models.base import ContractModel

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _required_text(value: str) -> str:
    if not value.strip() or _CONTROL_RE.search(value):
        raise ValueError("SEC text is empty or contains control characters")
    return value


def _sec_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"www.sec.gov", "data.sec.gov", "xbrl.sec.gov"}
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.query
        or parts.fragment
    ):
        raise ValueError("URL must be an HTTPS SEC public document URL")
    return value


class _SecContract(ContractModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class SecCompanyFiling(_SecContract):
    """One filing-history row from the SEC Submissions API."""

    filing_date: date
    report_type: str | None = None
    report_url: str
    report_date: date | None = None
    act: str | int | None = None
    items: str | float | None = None
    primary_doc_description: str | None = None
    primary_doc: str | None = None
    accession_number: str | int | None = None
    file_number: str | int | None = None
    film_number: str | int | None = None
    is_inline_xbrl: str | int | None = None
    is_xbrl: str | int | None = None
    size: str | int | None = None
    complete_submission_url: str | None = None
    filing_detail_url: str | None = None

    @field_validator(
        "report_type",
        "items",
        "primary_doc_description",
        "primary_doc",
        mode="before",
    )
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("report_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)

    @field_validator("complete_submission_url", "filing_detail_url")
    @classmethod
    def _validate_optional_url(cls, value: str | None) -> str | None:
        return _sec_url(value) if value is not None else value


class SecFilingCoverPage(_SecContract):
    """Small, typed subset of SEC cover-page entity fields."""

    registrant_name: str | None = None
    cik: str | None = None
    fiscal_year_end: str | None = None
    state_of_incorporation: str | None = None
    commission_file_number: str | None = None
    ein: str | None = None
    trading_symbols: tuple[str, ...] = ()

    @field_validator(
        "registrant_name",
        "cik",
        "fiscal_year_end",
        "state_of_incorporation",
        "commission_file_number",
        "ein",
        mode="before",
    )
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("trading_symbols")
    @classmethod
    def _validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_required_text(item) for item in value)


class SecFilingDocument(_SecContract):
    """One document advertised by a filing's SEC index.json listing."""

    name: str
    document_type: str | None = None
    description: str | None = None
    size: int | None = Field(default=None, ge=0)
    url: str

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("document_type", "description", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)


class SecFilingHeader(_SecContract):
    """Normalized header and document index for one SEC accession."""

    base_url: str
    name: str
    cik: str
    trading_symbols: tuple[str, ...] = ()
    sic: str
    sic_organization_name: str
    accession_number: str
    filing_date: date
    period_ending: date | None = None
    fiscal_year_end: str | None = None
    document_type: str
    has_cover_page: bool
    description: str | None = None
    cover_page: SecFilingCoverPage | None = None
    document_urls: tuple[str, ...]
    documents: tuple[SecFilingDocument, ...]

    @field_validator("name", "cik", "sic", "sic_organization_name", "document_type", mode="before")
    @classmethod
    def _validate_required_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) else value

    @field_validator("description", "fiscal_year_end", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        return _sec_url(value)

    @field_validator("accession_number")
    @classmethod
    def _validate_accession(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
            raise ValueError("invalid SEC accession number")
        return value

    @field_validator("document_urls")
    @classmethod
    def _validate_document_urls(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_sec_url(item) for item in value)


class SecHtmFileData(_SecContract):
    """A bounded, unmodified SEC HTML filing document."""

    url: str
    content: str

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        if not value.strip() or _CONTROL_RE.search(value):
            raise ValueError("SEC HTML content is empty or contains control characters")
        return value


class SecRssLitigationItem(_SecContract):
    """A litigation-release item from the SEC public RSS feed."""

    published: datetime
    title: str
    summary: str
    id: str
    link: str

    @field_validator("title", "summary", "id")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("link")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)


class SecSchemaFile(_SecContract):
    """One XBRL taxonomy element or taxonomy-directory record."""

    name: str
    label: str | None = None
    description: str | None = None
    category: str | None = None
    style: str | None = None
    has_label_linkbase: bool | None = None
    url: str | None = None
    level: int | None = Field(default=None, ge=0)
    order: float | None = None
    parent_id: str | None = None
    preferred_label: str | None = None
    xbrl_type: str | None = None
    period_type: Literal["instant", "duration"] | None = None
    balance_type: Literal["credit", "debit"] | None = None
    abstract: bool | None = None
    substitution_group: str | None = None
    nillable: bool | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _required_text(value)

    @field_validator(
        "label",
        "description",
        "category",
        "style",
        "parent_id",
        "preferred_label",
        "xbrl_type",
        "substitution_group",
        mode="before",
    )
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        return _sec_url(value) if value is not None else value

    @field_validator("order")
    @classmethod
    def _validate_order(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("order must be finite")
        return value


class SecLatestFinancialReport(_SecContract):
    """One dated SEC daily-index result, with filing and report periods separate."""

    filing_date: date
    period_ending: date | None = None
    symbol: str | None = None
    name: str | None = None
    cik: str | None = None
    sic: str | None = None
    report_type: str | None = None
    description: str | None = None
    url: str
    items: str | None = None
    index_headers: str
    complete_submission: str
    metadata: str | None = None
    financial_report: str | None = None

    @field_validator(
        "symbol", "name", "cik", "sic", "report_type", "description", "items", mode="before"
    )
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("url", "index_headers", "complete_submission", "metadata", "financial_report")
    @classmethod
    def _validate_urls(cls, value: str | None) -> str | None:
        return _sec_url(value) if value is not None else value


class SecManagementDiscussionAnalysis(_SecContract):
    """A filing's management discussion and analysis content."""

    symbol: str
    calendar_year: int = Field(ge=1900, le=2200)
    calendar_period: int = Field(ge=1, le=4)
    period_ending: date | None = None
    content: str
    url: str

    @field_validator("symbol", "content")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)


class SecForm13FHRHolding(_SecContract):
    """One long holding from a SEC Form 13F-HR information table."""

    period_ending: date
    filing_date: date
    manager_cik: str
    manager_name: str | None = None
    accession_number: str
    issuer: str
    cusip: str
    asset_class: str
    security_type: Literal["SH", "PRN"] | None = None
    option_type: Literal["call", "put"] | None = None
    investment_discretion: str | None = None
    other_manager: str | None = None
    voting_authority_sole: int | None = Field(default=None, ge=0)
    voting_authority_shared: int | None = Field(default=None, ge=0)
    voting_authority_none: int | None = Field(default=None, ge=0)
    principal_amount: int = Field(ge=0)
    value: int = Field(ge=0)
    weight: float = Field(ge=0)

    @field_validator("issuer", "cusip", "asset_class")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("investment_discretion", "other_manager", mode="before")
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("weight")
    @classmethod
    def _validate_weight(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("weight must be finite")
        return value

    @field_validator("manager_cik")
    @classmethod
    def _validate_manager_cik(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}", value):
            raise ValueError("manager CIK must contain ten digits")
        return value

    @field_validator("manager_name", mode="before")
    @classmethod
    def _validate_manager_name(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("accession_number")
    @classmethod
    def _validate_accession(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
            raise ValueError("invalid SEC accession number")
        return value

    @model_validator(mode="after")
    def _check_period_order(self) -> SecForm13FHRHolding:
        if self.filing_date < self.period_ending:
            raise ValueError("filing date cannot precede the reporting period")
        return self


class SecNportDisclosureHolding(_SecContract):
    """One holding row from a SEC N-PORT filing."""

    symbol: str | None = None
    asset_symbol: str | None = None
    fund_cik: str
    fund_name: str | None = None
    accession_number: str
    filing_date: date
    acceptance_datetime: datetime | None = None
    period_ending: date
    name: str | None = None
    title: str | None = None
    cusip: str | None = None
    lei: str | None = None
    isin: str | None = None
    other_id: str | None = None
    is_restricted: str | None = None
    fair_value_level: int | None = Field(default=None, ge=1, le=3)
    is_cash_collateral: str | None = None
    is_non_cash_collateral: str | None = None
    is_loan_by_fund: str | None = None
    loan_value: float | None = None
    issuer_conditional: str | None = None
    asset_conditional: str | None = None
    payoff_profile: str | None = None
    asset_category: str | None = None
    issuer_category: str | None = None
    country: str | None = None
    balance: float | int | None = None
    units: float | int | str | None = None
    currency: str | None = None
    value: float | int | None = None
    weight: float | None = None
    as_of: date | None = None
    maturity_date: date | None = None
    coupon_kind: str | None = None
    rate_type: str | None = None
    annualized_return: float | None = None
    is_default: str | None = None
    in_arrears: str | None = None
    is_paid_kind: str | None = None
    derivative_category: str | None = None
    counterparty: str | None = None
    underlying_name: str | None = None
    option_type: str | None = None
    derivative_payoff: str | None = None
    expiry_date: date | None = None
    exercise_price: float | None = None
    exercise_currency: str | None = None
    shares_per_contract: float | None = None
    delta: str | float | None = None
    rate_type_rec: str | None = None
    receive_currency: str | None = None
    upfront_receive: float | None = None
    floating_rate_index_rec: str | None = None
    floating_rate_spread_rec: float | None = None
    rate_tenor_rec: str | None = None
    rate_tenor_unit_rec: str | int | None = None
    reset_date_rec: str | None = None
    reset_date_unit_rec: str | int | None = None
    rate_type_pmnt: str | None = None
    payment_currency: str | None = None
    upfront_payment: float | None = None
    floating_rate_index_pmnt: str | None = None
    floating_rate_spread_pmnt: float | None = None
    rate_tenor_pmnt: str | None = None
    rate_tenor_unit_pmnt: str | int | None = None
    reset_date_pmnt: str | None = None
    reset_date_unit_pmnt: str | int | None = None
    repo_type: str | None = None
    is_cleared: str | None = None
    is_tri_party: str | None = None
    principal_amount: float | None = None
    principal_currency: str | None = None
    collateral_type: str | None = None
    collateral_amount: float | None = None
    collateral_currency: str | None = None
    exchange_currency: str | None = None
    exchange_rate: float | None = None
    currency_sold: str | None = None
    currency_amount_sold: float | None = None
    currency_bought: str | None = None
    currency_amount_bought: float | None = None
    notional_amount: float | None = None
    notional_currency: str | None = None
    unrealized_gain: float | None = None

    @field_validator(
        "symbol",
        "asset_symbol",
        "name",
        "title",
        "cusip",
        "lei",
        "isin",
        "other_id",
        "is_restricted",
        "is_cash_collateral",
        "is_non_cash_collateral",
        "is_loan_by_fund",
        "issuer_conditional",
        "asset_conditional",
        "payoff_profile",
        "asset_category",
        "issuer_category",
        "country",
        "currency",
        "coupon_kind",
        "rate_type",
        "is_default",
        "in_arrears",
        "is_paid_kind",
        "derivative_category",
        "counterparty",
        "underlying_name",
        "option_type",
        "derivative_payoff",
        "exercise_currency",
        "rate_type_rec",
        "receive_currency",
        "floating_rate_index_rec",
        "rate_tenor_rec",
        "rate_tenor_unit_rec",
        "reset_date_rec",
        "reset_date_unit_rec",
        "rate_type_pmnt",
        "payment_currency",
        "floating_rate_index_pmnt",
        "rate_tenor_pmnt",
        "rate_tenor_unit_pmnt",
        "reset_date_pmnt",
        "reset_date_unit_pmnt",
        "repo_type",
        "is_cleared",
        "is_tri_party",
        "principal_currency",
        "collateral_type",
        "collateral_currency",
        "exchange_currency",
        "currency_sold",
        "currency_bought",
        "notional_currency",
        mode="before",
    )
    @classmethod
    def _validate_optional_text(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator(
        "loan_value",
        "balance",
        "value",
        "weight",
        "annualized_return",
        "exercise_price",
        "shares_per_contract",
        "upfront_receive",
        "floating_rate_spread_rec",
        "upfront_payment",
        "floating_rate_spread_pmnt",
        "principal_amount",
        "collateral_amount",
        "exchange_rate",
        "currency_amount_sold",
        "currency_amount_bought",
        "notional_amount",
        "unrealized_gain",
    )
    @classmethod
    def _validate_finite_amount(cls, value: float | int | str | None) -> float | int | str | None:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("amount must be finite")
        return value

    @field_validator("maturity_date", "expiry_date", mode="before")
    @classmethod
    def _validate_optional_date(cls, value: object) -> object:
        if isinstance(value, str) and value:
            try:
                return date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("invalid SEC disclosure date") from exc
        return value

    @field_validator("fund_cik")
    @classmethod
    def _validate_fund_cik(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}", value):
            raise ValueError("fund CIK must contain ten digits")
        return value

    @field_validator("fund_name", mode="before")
    @classmethod
    def _validate_fund_name(cls, value: object) -> object:
        return _required_text(value) if isinstance(value, str) and value else value

    @field_validator("accession_number")
    @classmethod
    def _validate_accession(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
            raise ValueError("invalid SEC accession number")
        return value

    @model_validator(mode="after")
    def _check_period_order(self) -> SecNportDisclosureHolding:
        if self.filing_date < self.period_ending:
            raise ValueError("filing date cannot precede the reporting period")
        return self


class SecNportFiling(_SecContract):
    """Typed filing metadata paired with the holdings extracted from N-PORT XML."""

    cik: str
    accession_number: str
    filing_date: date
    period_ending: date
    primary_document_url: str
    holdings: tuple[SecNportDisclosureHolding, ...]

    @field_validator("primary_document_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _sec_url(value)

    @field_validator("accession_number")
    @classmethod
    def _validate_accession(cls, value: str) -> str:
        if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", value):
            raise ValueError("invalid SEC accession number")
        return value

    @model_validator(mode="after")
    def _check_period_order(self) -> SecNportFiling:
        if self.filing_date < self.period_ending:
            raise ValueError("filing date cannot precede the reporting period")
        return self


__all__ = [
    "SecCompanyFiling",
    "SecFilingCoverPage",
    "SecFilingDocument",
    "SecFilingHeader",
    "SecForm13FHRHolding",
    "SecHtmFileData",
    "SecLatestFinancialReport",
    "SecManagementDiscussionAnalysis",
    "SecNportDisclosureHolding",
    "SecNportFiling",
    "SecRssLitigationItem",
    "SecSchemaFile",
]
