"""Strict source contracts for Tradier market data.

Tradier's historical-data guide says historical prices may not be dividend adjusted; it does not
state split-adjustment behavior. These contracts keep that distinction explicit instead of
borrowing adjustment semantics from another provider.
"""

from __future__ import annotations

import math
from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, StrictInt, field_validator

from opendata.data.models.base import ContractModel


class _TradierHistoricalBar(ContractModel):
    """One Tradier historical OHLCV record with source interval and adjustment caveats."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    date: date_type
    interval: Literal["daily", "weekly", "monthly"]
    open: float
    high: float
    low: float
    close: float
    volume: StrictInt
    dividend_adjustment_semantics: Literal["may_not_be_dividend_adjusted"] = (
        "may_not_be_dividend_adjusted"
    )
    split_adjustment_semantics: Literal["not_specified_by_tradier_docs"] = (
        "not_specified_by_tradier_docs"
    )

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("symbol must be non-empty and trimmed")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def _validate_iso_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("historical date must not include a time")
        if isinstance(value, date_type):
            return value
        if not isinstance(value, str):
            raise ValueError("historical date must be an ISO date")
        parsed = date_type.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("historical date must be an ISO date")
        return parsed

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def _validate_price(cls, value: object) -> object:
        return _finite_number(value)


class TradierEquityHistorical(_TradierHistoricalBar):
    """One stock historical OHLCV record returned by ``/markets/history``."""


class TradierEtfHistorical(_TradierHistoricalBar):
    """One ETF historical OHLCV record returned by ``/markets/history``."""


class TradierEquityQuote(ContractModel):
    """One current stock quote from Tradier's quote endpoint."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    description: str | None = None
    exchange: str | None = None
    security_type: Literal["stock", "etf"]
    last: float | None = None
    change: float | None = None
    volume: StrictInt | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bid: float | None = None
    ask: float | None = None
    change_percentage: float | None = None
    average_volume: StrictInt | None = None
    last_volume: StrictInt | None = None
    trade_date: StrictInt | None = None
    previous_close: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    bid_size: StrictInt | None = None
    bid_exchange: str | None = None
    bid_date: StrictInt | None = None
    ask_size: StrictInt | None = None
    ask_exchange: str | None = None
    ask_date: StrictInt | None = None
    lot_size: StrictInt | None = None

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("symbol must be non-empty and trimmed")
        return value

    @field_validator(
        "last",
        "change",
        "open",
        "high",
        "low",
        "close",
        "bid",
        "ask",
        "change_percentage",
        "previous_close",
        "week_52_high",
        "week_52_low",
        mode="before",
    )
    @classmethod
    def _validate_optional_price(cls, value: object) -> object:
        return None if value is None else _finite_number(value)


class TradierEquitySearch(ContractModel):
    """One security returned by Tradier's partial symbol/name search."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    exchange: str
    security_type: Literal["stock", "etf", "index", "option", "mutual_fund"]
    description: str

    @field_validator("symbol", "exchange", "description")
    @classmethod
    def _require_trimmed_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("text fields must be non-empty and trimmed")
        return value


class TradierOptionChain(ContractModel):
    """One option contract quote returned for a requested underlying and expiry."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    description: str | None = None
    exchange: str | None = None
    security_type: Literal["option"] = "option"
    last: float | None = None
    change: float | None = None
    volume: StrictInt | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bid: float | None = None
    ask: float | None = None
    change_percentage: float | None = None
    average_volume: StrictInt | None = None
    last_volume: StrictInt | None = None
    trade_date: StrictInt | None = None
    previous_close: float | None = None
    week_52_high: float | None = None
    week_52_low: float | None = None
    bid_size: StrictInt | None = None
    bid_exchange: str | None = None
    bid_date: StrictInt | None = None
    ask_size: StrictInt | None = None
    ask_exchange: str | None = None
    ask_date: StrictInt | None = None
    open_interest: StrictInt | None = None
    contract_size: StrictInt | None = None
    expiration_date: date_type
    expiration_type: str | None = None
    option_type: Literal["call", "put"]
    root_symbol: str
    underlying: str
    strike: float
    lot_size: StrictInt | None = None
    greeks: dict[str, float] | None = None

    @field_validator("symbol", "root_symbol", "underlying")
    @classmethod
    def _require_symbol_text(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("symbol fields must be non-empty and trimmed")
        return value

    @field_validator("expiration_date", mode="before")
    @classmethod
    def _validate_expiration_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("expiration date must not include a time")
        if isinstance(value, date_type):
            return value
        if not isinstance(value, str):
            raise ValueError("expiration date must be an ISO date")
        parsed = date_type.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError("expiration date must be an ISO date")
        return parsed

    @field_validator(
        "last",
        "change",
        "open",
        "high",
        "low",
        "close",
        "bid",
        "ask",
        "change_percentage",
        "previous_close",
        "week_52_high",
        "week_52_low",
        "strike",
        mode="before",
    )
    @classmethod
    def _validate_option_price(cls, value: object) -> object:
        return None if value is None else _finite_number(value)

    @field_validator("greeks", mode="before")
    @classmethod
    def _validate_greeks(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
            raise ValueError("greeks must be a string-keyed object")
        return {key: _finite_number(number) for key, number in value.items()}


def _finite_number(value: object) -> float:
    """Accept source JSON numbers only, rejecting booleans, strings, and non-finite values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("source numeric field must be a finite JSON number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError("source numeric field must be a finite JSON number") from exc
    if not math.isfinite(number):
        raise ValueError("source numeric field must be a finite JSON number")
    return number


__all__ = [
    "TradierEquityHistorical",
    "TradierEquityQuote",
    "TradierEquitySearch",
    "TradierEtfHistorical",
    "TradierOptionChain",
]
