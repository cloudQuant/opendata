"""Typed FMP market contracts for the Iteration 2 market-data slice.

The models intentionally expose only stable, useful fields from the FMP Stable
routes.  They remain source-neutral contract types: FMP spelling conversion
belongs in the provider fetchers.
"""

from __future__ import annotations

import math
from datetime import date as date_type
from datetime import datetime, timezone

from pydantic import ConfigDict, field_validator

from opendata.data.models.base import ContractModel


def _finite_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("market values must be JSON numbers")
    if not math.isfinite(float(value)):
        raise ValueError("market values must be finite")
    return value


def _text(value: str) -> str:
    if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("required market text is invalid")
    return value


def _optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("market text contains control characters")
    return value


def _source_date(value: object) -> object:
    if value is None or isinstance(value, date_type) and not isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("source date must be an ISO date")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("source date must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError("source date must be an ISO date")
    return parsed


def _source_flexible_date(value: object) -> object:
    """Normalize ISO calendar dates, while retaining documented date labels."""
    if value is None:
        return None
    if isinstance(value, datetime):
        raise ValueError("source date must not include a time")
    if isinstance(value, date_type):
        return value
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("source date must be a date or clean text")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError:
        return value
    return parsed if parsed.isoformat() == value else value


def _source_datetime(value: object) -> object:
    if value is None or isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("source timestamp must be ISO-8601") from exc
    elif isinstance(value, int) and not isinstance(value, bool) and value > 0:
        # FMP quote endpoints document Unix timestamps in seconds.
        parsed = datetime.fromtimestamp(value, tz=timezone.utc)
    else:
        raise ValueError("source timestamp must be ISO-8601 or Unix seconds")
    if parsed is not None:
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("market timestamps must include a timezone")
        return parsed.astimezone(timezone.utc)
    return parsed


class _StrictMarketModel(ContractModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class _DailyBar(_StrictMarketModel):
    date: date_type
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: int | float | None
    vwap: float | None
    change: float | None
    change_percent: float | None

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return _source_date(value)

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "change",
        "change_percent",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _finite_number(value)


class CryptoHistorical(_DailyBar):
    """Daily crypto EOD bars; FMP defines crypto close at 00:00 UTC."""


class CurrencyHistorical(_DailyBar):
    """Daily foreign-exchange EOD bars."""


class IndexHistorical(_DailyBar):
    """Daily index EOD bars."""

    symbol: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)


class CryptoSearch(_StrictMarketModel):
    """One crypto from the FMP cryptocurrency directory."""

    symbol: str
    name: str | None
    exchange: str | None
    ico_date: date_type | None
    circulating_supply: int | float | None
    total_supply: int | float | None

    @field_validator("symbol")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _text(value)

    @field_validator("name", "exchange")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("ico_date", mode="before")
    @classmethod
    def _validate_ico_date(cls, value: object) -> object:
        return _source_date(value)

    @field_validator("circulating_supply", "total_supply", mode="before")
    @classmethod
    def _validate_supply(cls, value: object) -> object:
        return _finite_number(value)


class CurrencyPair(_StrictMarketModel):
    """Currency-pair identifiers and the source currency names."""

    symbol: str
    name: str | None
    from_currency: str
    to_currency: str
    from_name: str
    to_name: str

    @field_validator("symbol", "from_currency", "to_currency", "from_name", "to_name")
    @classmethod
    def _validate_pair_text(cls, value: str) -> str:
        return _text(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _optional_text(value)


class CurrencySnapshot(_StrictMarketModel):
    """Live FX quote with rates expressed in the requested base perspective."""

    base_currency: str
    counter_currency: str
    last_rate: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    last_rate_timestamp: datetime | None = None

    @field_validator("base_currency", "counter_currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        return _text(value)

    @field_validator(
        "last_rate",
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "change",
        "change_percent",
        "ma50",
        "ma200",
        "year_high",
        "year_low",
        mode="before",
    )
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("volume", mode="before")
    @classmethod
    def _validate_volume(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("currency snapshot volume must be a non-negative integer")
        return value

    @field_validator("last_rate_timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> object:
        return _source_datetime(value)


class EquityActive(_StrictMarketModel):
    """Most actively traded equity quote fields."""

    symbol: str
    name: str | None
    price: float
    change: float
    percent_change: float
    volume: int | float | None
    exchange: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator("name")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("exchange")
    @classmethod
    def _validate_exchange(cls, value: str) -> str:
        return _text(value)

    @field_validator("price", "change", "percent_change", "volume", mode="before")
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)


class EquityPeers(_StrictMarketModel):
    """Lightweight quote and market cap for a peer company."""

    symbol: str
    name: str
    price: float | None
    market_cap: int | None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _text(value)

    @field_validator("price", mode="before")
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("market_cap", mode="before")
    @classmethod
    def _validate_market_cap(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("market_cap must be a non-negative integer")
        return value


class EquityInfo(_StrictMarketModel):
    """FMP company-profile fields used by the public EquityInfo model."""

    symbol: str
    name: str | None = None
    cik: str | None = None
    cusip: str | None = None
    isin: str | None = None
    stock_exchange: str | None = None
    long_description: str | None = None
    company_url: str | None = None
    ceo: str | None = None
    sector: str | None = None
    industry_category: str | None = None
    business_address: str | None = None
    business_phone_no: str | None = None
    hq_address_city: str | None = None
    hq_state: str | None = None
    hq_address_postal_code: str | None = None
    hq_country: str | None = None
    employees: int | None = None
    image: str | None = None
    currency: str | None = None
    market_cap: int | None = None
    last_price: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    volume_avg: int | None = None
    annualized_dividend_amount: float | None = None
    beta: float | None = None
    is_etf: bool
    is_actively_trading: bool
    is_adr: bool
    is_fund: bool

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator(
        "name",
        "cik",
        "cusip",
        "isin",
        "stock_exchange",
        "long_description",
        "company_url",
        "ceo",
        "sector",
        "industry_category",
        "business_address",
        "business_phone_no",
        "hq_address_city",
        "hq_state",
        "hq_address_postal_code",
        "hq_country",
        "image",
        "currency",
    )
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("market_cap", "volume_avg", "employees", mode="before")
    @classmethod
    def _validate_integer_values(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("company counts and market cap must be non-negative integers")
        return value

    @field_validator(
        "last_price", "year_high", "year_low", "annualized_dividend_amount", "beta", mode="before"
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("is_etf", "is_actively_trading", "is_adr", "is_fund", mode="before")
    @classmethod
    def _validate_flags(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("company flags must be booleans")
        return value


class EquityGainers(_StrictMarketModel):
    """Current quote fields for a top gaining equity."""

    symbol: str
    name: str | None
    price: float
    change: float
    percent_change: float
    volume: int | float | None
    exchange: str

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator("name")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("exchange")
    @classmethod
    def _validate_exchange(cls, value: str) -> str:
        return _text(value)

    @field_validator("price", "change", "percent_change", "volume", mode="before")
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)


class EquityLosers(EquityGainers):
    """US equity losers, normalized to the same fields as gainers."""


class EquityScreener(_StrictMarketModel):
    """Company screener results with source identifiers and filters."""

    symbol: str
    name: str | None
    market_cap: int | None
    sector: str | None
    industry: str | None
    beta: float | None
    price: float | None
    last_annual_dividend: float | None
    volume: int | None
    exchange: str | None
    exchange_name: str | None
    country: str | None
    is_etf: bool | None
    is_fund: bool | None
    actively_trading: bool | None

    @field_validator("symbol")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        return _text(value)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("sector", "industry", "exchange", "exchange_name", "country")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator(
        "market_cap",
        "beta",
        "price",
        "last_annual_dividend",
        "volume",
        mode="before",
    )
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("market_cap", "volume", mode="before")
    @classmethod
    def _validate_integer_values(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("screener market cap and volume must be non-negative integers")
        return value

    @field_validator("is_etf", "is_fund", "actively_trading", mode="before")
    @classmethod
    def _validate_optional_flags(cls, value: object) -> object:
        if value is not None and type(value) is not bool:
            raise ValueError("screener flags must be booleans")
        return value


class MarketSnapshot(_StrictMarketModel):
    """Current quote and session statistics for one symbol."""

    exchange: str | None = None
    symbol: str
    name: str | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None
    prev_close: float | None = None
    change: float | None = None
    change_percent: float | None = None
    ma50: float | None = None
    ma200: float | None = None
    year_high: float | None = None
    year_low: float | None = None
    market_cap: int | float | None = None
    last_price_timestamp: datetime | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator("exchange", "name")
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "prev_close",
        "change",
        "change_percent",
        "ma50",
        "ma200",
        "year_high",
        "year_low",
        "market_cap",
        mode="before",
    )
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("volume", mode="before")
    @classmethod
    def _validate_volume(cls, value: object) -> object:
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("snapshot volume must be a non-negative integer")
        return value

    @field_validator("last_price_timestamp", mode="before")
    @classmethod
    def _validate_timestamp(cls, value: object) -> object:
        return _source_datetime(value)


class PricePerformance(_StrictMarketModel):
    """FMP's published percentage-change horizons for one symbol."""

    symbol: str
    one_day: float | None
    wtd: float | None = None
    one_week: float | None = None
    mtd: float | None = None
    one_month: float | None
    qtd: float | None = None
    three_month: float | None
    six_month: float | None
    ytd: float | None
    one_year: float | None
    two_year: float | None = None
    three_year: float | None
    four_year: float | None = None
    five_year: float | None
    ten_year: float | None
    max: float | None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator(
        "one_day",
        "wtd",
        "one_week",
        "mtd",
        "one_month",
        "qtd",
        "three_month",
        "six_month",
        "ytd",
        "one_year",
        "two_year",
        "three_year",
        "four_year",
        "five_year",
        "ten_year",
        "max",
        mode="before",
    )
    @classmethod
    def _validate_values(cls, value: object) -> object:
        return _finite_number(value)


class ShareStatistics(_StrictMarketModel):
    """Company float, outstanding-share, date, and source-link fields."""

    symbol: str
    date: datetime | date_type | None
    free_float: float | None
    float_shares: int | float | None
    outstanding_shares: int | float | None
    url: str | None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None or value.utcoffset() is None:
                return value
            return _source_datetime(value)
        if isinstance(value, date_type):
            return value
        if isinstance(value, str) and (" " in value or "T" in value):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("share statistics date must be ISO date or timestamp") from exc
            return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else parsed
        return _source_date(value)

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("free_float", "float_shares", "outstanding_shares", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return _finite_number(value)


class FmpIndexConstituent(_StrictMarketModel):
    """Current index membership or a historical membership-change row."""

    symbol: str
    name: str | None
    sector: str | None
    industry: str | None
    headquarter: str | None
    date_added: date_type | str | None
    cik: str | None
    founded: date_type | str | None
    removed_symbol: str | None
    removed_name: str | None
    reason: str | None
    date: date_type | str | None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _text(value)

    @field_validator(
        "name",
        "sector",
        "industry",
        "headquarter",
        "cik",
        "removed_symbol",
        "removed_name",
        "reason",
    )
    @classmethod
    def _validate_text(cls, value: str | None) -> str | None:
        return _optional_text(value)

    @field_validator("date_added", "founded", "date", mode="before")
    @classmethod
    def _validate_dates(cls, value: object) -> object:
        return _source_flexible_date(value)


__all__ = [
    "CryptoHistorical",
    "CryptoSearch",
    "CurrencyHistorical",
    "CurrencyPair",
    "CurrencySnapshot",
    "EquityActive",
    "EquityGainers",
    "EquityInfo",
    "EquityLosers",
    "EquityPeers",
    "EquityScreener",
    "FmpIndexConstituent",
    "IndexHistorical",
    "MarketSnapshot",
    "PricePerformance",
    "ShareStatistics",
]
