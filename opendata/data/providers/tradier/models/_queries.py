"""Shared strict query validation for bounded Tradier requests."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from pydantic import Field, field_validator, model_validator

from opendata.data.protocol import QueryParams

MAX_SYMBOLS = 8
MAX_FANOUT_REQUESTS = 16


class TradierSymbolQuery(QueryParams):
    """One symbol or a short, explicit fan-out list for symbol-based endpoints."""

    source: Literal["tradier"] = "tradier"
    market: Literal["us"] = "us"
    symbol: str | None = None
    symbols: tuple[str, ...] | None = None
    max_requests: int = Field(
        default=MAX_FANOUT_REQUESTS,
        ge=1,
        le=MAX_FANOUT_REQUESTS,
        strict=True,
    )

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str | None) -> str | None:
        return None if value is None else _clean_symbol(value)

    @field_validator("symbols", mode="before")
    @classmethod
    def _validate_symbols(cls, values: object) -> object:
        if values is None:
            return None
        if not isinstance(values, (list, tuple)):
            raise ValueError("symbols must be a sequence of symbols")
        return tuple(_clean_symbol(value) for value in values)

    @model_validator(mode="after")
    def _require_one_symbol_form(self) -> TradierSymbolQuery:
        if (self.symbol is None) == (self.symbols is None):
            raise ValueError("provide exactly one of symbol or symbols")
        if self.symbols is not None:
            if not self.symbols or len(self.symbols) > MAX_SYMBOLS:
                raise ValueError(f"symbols must contain between 1 and {MAX_SYMBOLS} values")
            if len(set(self.symbols)) != len(self.symbols):
                raise ValueError("symbols must not contain duplicates")
        return self

    @property
    def requested_symbols(self) -> tuple[str, ...]:
        """Return the query's canonical, ordered symbols."""
        if self.symbol is not None:
            return (self.symbol,)
        return self.symbols or ()


class TradierHistoricalQuery(TradierSymbolQuery):
    """Required explicit historical window and interval."""

    start_date: date_type
    end_date: date_type
    interval: Literal["daily", "weekly", "monthly"] = "daily"
    max_records: int = Field(default=50_000, ge=1, le=50_000, strict=True)

    @model_validator(mode="after")
    def _validate_history_limits(self) -> TradierHistoricalQuery:
        if isinstance(self.start_date, date_type) and self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        required_requests = 2 * len(self.requested_symbols)
        if self.max_requests < required_requests:
            raise ValueError("max_requests must cover type lookup and history for each symbol")
        if self.start_date == self.end_date:
            # A one-day interval is valid and remains a historical endpoint request.
            return self
        return self


class TradierOptionsChainQuery(TradierSymbolQuery):
    """Required option expiration and optional Greeks request flag."""

    expiration: date_type
    greeks: bool = False
    max_records: int = Field(default=10_000, ge=1, le=10_000, strict=True)

    @field_validator("greeks", mode="before")
    @classmethod
    def _require_bool_greeks(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("greeks must be a boolean")
        return value

    @model_validator(mode="after")
    def _validate_chain_limits(self) -> TradierOptionsChainQuery:
        if self.max_requests < len(self.requested_symbols):
            raise ValueError("max_requests must cover one chains request per symbol")
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("option chain queries do not accept a historical date window")
        return self


def _clean_symbol(value: object) -> str:
    """Normalize a safe symbol token before any transport or credential access."""
    if not isinstance(value, str):
        raise ValueError("symbol must be a string")
    symbol = value.strip().upper()
    if (
        not symbol
        or len(symbol) > 32
        or "," in symbol
        or any(ord(char) < 33 or ord(char) == 127 for char in symbol)
    ):
        raise ValueError("symbol is invalid")
    return symbol
