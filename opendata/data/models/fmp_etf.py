"""Strict typed contracts for FMP ETF directory, exposure, and market data."""

from __future__ import annotations

import math
from datetime import date as date_type
from datetime import datetime

from pydantic import ConfigDict, Field, StrictBool, StrictInt, field_validator

from opendata.data.models.base import ContractModel


def _finite_source_number(value: object) -> object:
    """Keep numeric JSON values in their source units and reject coercions."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("source value must be a JSON number")
    if not math.isfinite(float(value)):
        raise ValueError("source value must be finite")
    return value


def _source_text(value: str) -> str:
    if not value or value != value.strip() or any(ord(char) < 32 for char in value):
        raise ValueError("source text must be non-empty trimmed text")
    return value


def _source_date(value: object) -> object:
    if isinstance(value, datetime):
        raise ValueError("source date must not include a time")
    if isinstance(value, date_type):
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


class _FmpEtfContract(ContractModel):
    """ETF source contract base with strict JSON shape and finite numbers."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)


class FmpEtfSearch(_FmpEtfContract):
    """One symbol and name from FMP's stable ETF list."""

    symbol: str
    name: str

    @field_validator("symbol", "name")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _source_text(value)


class FmpEtfInfoSectorExposure(_FmpEtfContract):
    """Nested sector exposure object included by the FMP ETF-info endpoint."""

    industry: str
    exposure: int | float

    @field_validator("industry")
    @classmethod
    def _validate_industry(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("exposure", mode="before")
    @classmethod
    def _validate_exposure(cls, value: object) -> object:
        return _finite_source_number(value)


class FmpEtfInfo(_FmpEtfContract):
    """One FMP ETF/fund information row; values retain the source field units."""

    symbol: str
    name: str | None = None
    description: str | None = None
    isin: str | None = None
    asset_class: str | None = Field(default=None, alias="assetClass")
    security_cusip: str | None = Field(default=None, alias="securityCusip")
    domicile: str | None = None
    website: str | None = None
    etf_company: str | None = Field(default=None, alias="etfCompany")
    expense_ratio: int | float | None = Field(default=None, alias="expenseRatio")
    assets_under_management: int | float | None = Field(default=None, alias="assetsUnderManagement")
    average_volume: int | float | None = Field(default=None, alias="avgVolume")
    inception_date: date_type | None = Field(default=None, alias="inceptionDate")
    nav: int | float | None = None
    nav_currency: str | None = Field(default=None, alias="navCurrency")
    holdings_count: StrictInt | None = Field(default=None, alias="holdingsCount")
    is_actively_trading: StrictBool | None = Field(default=None, alias="isActivelyTrading")
    updated_at_source: str | None = Field(default=None, alias="updatedAt")
    sectors_list: tuple[FmpEtfInfoSectorExposure, ...] | None = Field(
        default=None, alias="sectorsList"
    )

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _source_text(value)

    @field_validator(
        "name",
        "description",
        "isin",
        "asset_class",
        "security_cusip",
        "domicile",
        "website",
        "etf_company",
        "nav_currency",
        "updated_at_source",
    )
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _source_text(value)

    @field_validator(
        "expense_ratio",
        "assets_under_management",
        "average_volume",
        "nav",
        mode="before",
    )
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)

    @field_validator("inception_date", mode="before")
    @classmethod
    def _validate_inception_date(cls, value: object) -> object:
        return None if value is None else _source_date(value)


class FmpEtfHolding(_FmpEtfContract):
    """One holding row from the stable ETF & Fund Holdings response."""

    symbol: str
    asset: str
    name: str | None = None
    isin: str | None = None
    security_cusip: str | None = Field(default=None, alias="securityCusip")
    shares_number: int | float | None = Field(default=None, alias="sharesNumber")
    weight_percentage: int | float | None = Field(default=None, alias="weightPercentage")
    market_value: int | float | None = Field(default=None, alias="marketValue")
    updated_at_source: str | None = Field(default=None, alias="updatedAt")

    @field_validator("symbol", "asset")
    @classmethod
    def _validate_symbols(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("name", "isin", "security_cusip", "updated_at_source")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _source_text(value)

    @field_validator("shares_number", "weight_percentage", "market_value", mode="before")
    @classmethod
    def _validate_numbers(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)


class FmpEtfCountry(_FmpEtfContract):
    """One country-allocation row, retaining percentage-point magnitude."""

    symbol: str
    country: str
    weight: int | float

    @field_validator("symbol", "country")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("weight", mode="before")
    @classmethod
    def _parse_weight(cls, value: object) -> object:
        if isinstance(value, str) and value.endswith("%"):
            text = value[:-1].strip()
            if not text:
                raise ValueError("weight percentage is empty")
            try:
                number = float(text)
            except ValueError as exc:
                raise ValueError("weight percentage must be numeric") from exc
            return _finite_source_number(number)
        return _finite_source_number(value)


class FmpEtfEquityExposure(_FmpEtfContract):
    """One ETF containing the requested equity, with source units unchanged."""

    equity_symbol: str
    etf_symbol: str
    weight: int | float | None
    market_value: int | float | None
    shares: int | float | None

    @field_validator("equity_symbol", "etf_symbol")
    @classmethod
    def _validate_symbols(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("weight", "market_value", "shares", mode="before")
    @classmethod
    def _validate_optional_number(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)


class FmpEtfPricePerformance(_FmpEtfContract):
    """ETF quote performance fields currently evidenced by the Stable quote route."""

    symbol: str
    one_day: int | float | None = Field(default=None, alias="changePercentage")

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("one_day", mode="before")
    @classmethod
    def _validate_one_day(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)


class FmpEtfQuoteSourceRow(_FmpEtfContract):
    """Stable full-quote row shape; ETF-specific field support is still unconfirmed."""

    symbol: str
    name: str | None = None
    price: int | float | None = None
    change_percentage: int | float = Field(alias="changePercentage")
    change: int | float | None = None
    volume: StrictInt | None = None
    day_low: int | float | None = Field(default=None, alias="dayLow")
    day_high: int | float | None = Field(default=None, alias="dayHigh")
    year_high: int | float | None = Field(default=None, alias="yearHigh")
    year_low: int | float | None = Field(default=None, alias="yearLow")
    market_cap: int | float | None = Field(default=None, alias="marketCap")
    price_avg_50: int | float | None = Field(default=None, alias="priceAvg50")
    price_avg_200: int | float | None = Field(default=None, alias="priceAvg200")
    exchange: str | None = None
    open: int | float | None = None
    previous_close: int | float | None = Field(default=None, alias="previousClose")
    timestamp: StrictInt | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("name", "exchange")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        return None if value is None else _source_text(value)

    @field_validator(
        "price",
        "change_percentage",
        "change",
        "day_low",
        "day_high",
        "year_high",
        "year_low",
        "market_cap",
        "price_avg_50",
        "price_avg_200",
        "open",
        "previous_close",
        mode="before",
    )
    @classmethod
    def _validate_optional_numbers(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)


class FmpEtfSector(_FmpEtfContract):
    """One sector allocation row from FMP's stable ETF sector-weightings route."""

    symbol: str
    sector: str
    weight: int | float

    @field_validator("symbol", "sector")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("weight", mode="before")
    @classmethod
    def _validate_weight(cls, value: object) -> object:
        return _finite_source_number(value)


class FmpEtfHistorical(_FmpEtfContract):
    """One daily history row for an ETF symbol present in the ETF directory."""

    symbol: str
    date: date_type
    open: int | float
    high: int | float
    low: int | float
    close: int | float
    volume: StrictInt | None = None
    change: int | float | None = None
    change_percent: int | float | None = Field(default=None, alias="changePercent")
    vwap: int | float | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        return _source_text(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return _source_date(value)

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def _validate_required_prices(cls, value: object) -> object:
        return _finite_source_number(value)

    @field_validator("change", "change_percent", "vwap", mode="before")
    @classmethod
    def _validate_optional_prices(cls, value: object) -> object:
        return None if value is None else _finite_source_number(value)


class FmpEtfHistoricalSourceRow(_FmpEtfContract):
    """Exact row shape published by FMP's legacy ETF-history page."""

    date: date_type
    open: int | float
    high: int | float
    low: int | float
    close: int | float
    volume: StrictInt
    unadjusted_volume: StrictInt | None = Field(default=None, alias="unadjustedVolume")
    change: int | float
    change_percent: int | float = Field(alias="changePercent")
    vwap: int | float
    label: str
    change_over_time: int | float = Field(alias="changeOverTime")

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return _source_date(value)

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "change",
        "change_percent",
        "vwap",
        "change_over_time",
        mode="before",
    )
    @classmethod
    def _validate_numeric_values(cls, value: object) -> object:
        return _finite_source_number(value)

    @field_validator("label")
    @classmethod
    def _validate_label(cls, value: str) -> str:
        return _source_text(value)
