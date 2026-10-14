"""Strict TMX Money output contracts backed by observed public UI fields.

These classes intentionally contain only fields whose source mapping is
reviewed in ``docs/evidence/C100/tmx/``.  They are source-specific contracts,
not claims that the TMX Money UI exposes every field in the general OpenBB
model.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import ConfigDict, StrictInt, StrictStr, field_validator

from opendata.data.models.base import ContractModel


def _number(value: object) -> object:
    """Reject JSON strings and booleans where TMX publishes numeric values."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("TMX numeric fields must be JSON numbers")
    return value


def _iso_date(value: object) -> object:
    """Parse only exact ISO calendar dates, without inventing a timezone."""
    if isinstance(value, datetime):
        raise ValueError("TMX calendar-date fields must not contain a time")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError("TMX calendar-date fields must be ISO date strings")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("TMX calendar-date fields must be exact ISO dates")
    return parsed


class _TmxContract(ContractModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)


class TmxEquitySearch(_TmxContract):
    """One row from the public TMX company keyword search."""

    symbol: StrictStr | None = None
    name: StrictStr | None = None


class TmxEquityHistorical(_TmxContract):
    """One daily price-history row returned by TMX Money's public chart query."""

    symbol: StrictStr
    date: date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | int | None = None
    vwap: float | None = None
    change: float | None = None
    change_percent: float | None = None
    transactions: StrictInt | None = None
    transactions_value: float | None = None

    @field_validator("date", mode="before")
    @classmethod
    def _parse_source_datetime(cls, value: object) -> object:
        if isinstance(value, datetime | date):
            return value
        if not isinstance(value, str):
            raise ValueError("TMX history datetime must be an ISO string")
        try:
            parsed: date | datetime
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            else:
                parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("TMX history datetime must be ISO formatted") from exc
        return parsed

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "change",
        "change_percent",
        "transactions_value",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEquityQuote(_TmxContract):
    """Current quote fields with direct, documented field correspondences."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    last_price: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    market_cap: float | None = None
    pe: float | None = None
    eps: float | None = None
    vwap: float | None = None
    currency: StrictStr | None = None

    @field_validator(
        "last_price",
        "open",
        "high",
        "low",
        "volume",
        "prev_close",
        "change",
        "change_percent",
        "year_high",
        "year_low",
        "market_cap",
        "pe",
        "eps",
        "vwap",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxEquityInfo(_TmxContract):
    """Profile fields present in the public TMX quote/profile selection."""

    symbol: StrictStr
    name: StrictStr | None = None
    stock_exchange: StrictStr | None = None
    long_description: StrictStr | None = None
    company_url: StrictStr | None = None
    business_address: StrictStr | None = None
    business_phone_no: StrictStr | None = None
    email: StrictStr | None = None
    employees: StrictInt | None = None
    sector: StrictStr | None = None
    issue_type: StrictStr | None = None
    shares_outstanding: StrictInt | None = None
    shares_escrow: StrictInt | None = None
    shares_total: StrictInt | None = None
    dividend_frequency: StrictStr | None = None


class TmxCompanyFiling(_TmxContract):
    """A public filing record; ``report_type`` stays null without a proven mapping."""

    symbol: StrictStr
    filing_date: date
    report_type: StrictStr | None = None
    report_url: StrictStr
    description: StrictStr | None = None
    size: StrictStr | StrictInt | None = None

    @field_validator("filing_date", mode="before")
    @classmethod
    def _parse_filing_date(cls, value: object) -> object:
        return _iso_date(value)


class TmxHistoricalDividend(_TmxContract):
    """One dividend row from TMX Money's explicitly paginated history query."""

    symbol: StrictStr
    ex_dividend_date: date
    amount: float
    currency: StrictStr | None = None
    declaration_date: date | None = None
    record_date: date | None = None
    payment_date: date | None = None

    @field_validator(
        "ex_dividend_date", "declaration_date", "record_date", "payment_date", mode="before"
    )
    @classmethod
    def _parse_dates(cls, value: object) -> object:
        return _iso_date(value) if value is not None else None

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> object:
        return _number(value)


class TmxIndexConstituent(_TmxContract):
    """One constituent in the TMX Money index directory response."""

    symbol: StrictStr
    name: StrictStr | None = None
    exchange: StrictStr | None = None
    market_value: float | None = None
    weight: float | None = None

    @field_validator("market_value", "weight", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


class TmxPriceTargetConsensus(_TmxContract):
    """Analyst consensus fields exposed by the public TMX quote page."""

    symbol: StrictStr
    name: StrictStr | None = None
    target_high: float | None = None
    target_low: float | None = None
    target_consensus: float | None = None
    target_median: float | None = None
    target_upside: float | None = None
    total_analysts: StrictInt | None = None
    buy_ratings: StrictInt | None = None
    sell_ratings: StrictInt | None = None
    hold_ratings: StrictInt | None = None
    consensus_action: StrictStr | None = None

    @field_validator(
        "target_high",
        "target_low",
        "target_consensus",
        "target_median",
        "target_upside",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _number(value)


TmxModelStatus = Literal["READY", "INCOMPLETE"]


__all__ = [
    "TmxCompanyFiling",
    "TmxEquityHistorical",
    "TmxEquityInfo",
    "TmxEquityQuote",
    "TmxEquitySearch",
    "TmxHistoricalDividend",
    "TmxIndexConstituent",
    "TmxPriceTargetConsensus",
    "TmxModelStatus",
]
