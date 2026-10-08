"""Source-native equity price contract models shared across providers."""

from __future__ import annotations

import math
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING, Literal

import pandas as pd
from pydantic import ConfigDict, Field, StrictInt, field_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from collections.abc import Sequence

    from typing_extensions import Self


class EquityHistorical(ContractModel):
    """One FMP stable EOD row with source adjustment and unit caveats retained."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    date: date_type
    open: float
    high: float
    low: float
    close: float
    volume: int | float | None = None
    change: float | None = None
    change_percent: float | None = Field(default=None, alias="changePercent")
    vwap: float | None = None
    currency: None = None
    currency_semantics: Literal["source_unverified"] = "source_unverified"
    volume_unit: None = None
    volume_unit_semantics: Literal["source_unverified"] = "source_unverified"
    query_window_scope: Literal["explicit", "provider_default_unknown"]
    window_boundary_semantics: Literal["source_unverified"] = "source_unverified"
    provider_default_window_semantics: Literal["source_unverified"] = "source_unverified"
    close_adjustment_semantics: Literal["split_adjusted_per_source_faq"] = (
        "split_adjusted_per_source_faq"
    )
    adj_close_provided: Literal[False] = False

    @classmethod
    def to_frame(cls, rows: Sequence[Self]) -> pd.DataFrame:
        """Serialize nullable volume without pandas float coercion."""
        frame = super().to_frame(rows)
        if rows:
            frame["volume"] = pd.Series(
                [row.volume for row in rows],
                index=frame.index,
                dtype=object,
            )
        return frame

    @field_validator("symbol")
    @classmethod
    def _validate_source_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source symbol is invalid")
        return value

    @field_validator("date", mode="before")
    @classmethod
    def _validate_source_date(cls, value: object) -> object:
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

    @field_validator(
        "open", "high", "low", "close", "volume", "change", "change_percent", "vwap", mode="before"
    )
    @classmethod
    def _validate_finite_source_number(cls, value: object) -> object:
        return _validate_finite_source_number(value)


class EquityQuote(ContractModel):
    """One FMP stable quote row; the provider timestamp stays uninterpreted."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)

    symbol: str
    name: str | None = None
    price: float
    change_percentage: float | None = Field(default=None, alias="changePercentage")
    change: float | None = None
    volume: int | float | None = None
    day_low: float | None = Field(default=None, alias="dayLow")
    day_high: float | None = Field(default=None, alias="dayHigh")
    year_low: float | None = Field(default=None, alias="yearLow")
    year_high: float | None = Field(default=None, alias="yearHigh")
    market_cap: float | None = Field(default=None, alias="marketCap")
    price_avg_50: float | None = Field(default=None, alias="priceAvg50")
    price_avg_200: float | None = Field(default=None, alias="priceAvg200")
    exchange: str | None = None
    open: float | None = None
    previous_close: float | None = Field(default=None, alias="previousClose")
    provider_timestamp: StrictInt | None = Field(default=None, alias="timestamp")
    timestamp_unit: None = None
    timestamp_timezone: None = None
    timestamp_semantics: Literal["source_unverified"] = "source_unverified"
    currency: None = None
    currency_semantics: Literal["source_unverified"] = "source_unverified"
    volume_unit: None = None
    volume_unit_semantics: Literal["source_unverified"] = "source_unverified"

    @classmethod
    def to_frame(cls, rows: Sequence[Self]) -> pd.DataFrame:
        """Serialize nullable integer and numeric values without precision loss."""
        frame = super().to_frame(rows)
        if rows:
            frame["provider_timestamp"] = pd.Series(
                [row.provider_timestamp for row in rows],
                index=frame.index,
                dtype=object,
            )
            frame["volume"] = pd.Series(
                [row.volume for row in rows],
                index=frame.index,
                dtype=object,
            )
        return frame

    @field_validator("symbol")
    @classmethod
    def _validate_source_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("source symbol is invalid")
        return value

    @field_validator(
        "price",
        "change_percentage",
        "change",
        "volume",
        "day_low",
        "day_high",
        "year_low",
        "year_high",
        "market_cap",
        "price_avg_50",
        "price_avg_200",
        "open",
        "previous_close",
        mode="before",
    )
    @classmethod
    def _validate_finite_source_number(cls, value: object) -> object:
        return _validate_finite_source_number(value)


def _validate_finite_source_number(value: object) -> object:
    """Reject booleans, non-numbers, NaN, and infinity from source fields."""
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
