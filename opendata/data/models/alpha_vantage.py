"""Strict typed output rows for the Alpha Vantage provider slice."""

from __future__ import annotations

import math
from datetime import date as _date
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import ConfigDict, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.models.base import ContractModel

if TYPE_CHECKING:
    from typing_extensions import Self


def _source_symbol(value: object) -> object:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError("symbol must be a non-empty, trimmed string")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("symbol contains a control character")
    return value


def _source_date(value: object) -> object:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            raise ValueError("source timestamps must be timezone-naive")
        return value
    if isinstance(value, _date):
        return value
    if not isinstance(value, str):
        raise ValueError("date must be an ISO date or source timestamp")
    try:
        parsed_date = _date.fromisoformat(value)
    except ValueError:
        try:
            parsed_datetime = datetime.fromisoformat(value.replace(" ", "T"))
        except ValueError as exc:
            raise ValueError("date must be an ISO date or source timestamp") from exc
        if parsed_datetime.tzinfo is not None:
            raise ValueError("source timestamps must be timezone-naive") from None
        return parsed_datetime
    if parsed_date.isoformat() != value:
        raise ValueError("date must use YYYY-MM-DD format")
    return parsed_date


def _finite_number(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("source numeric values must already be parsed numbers")
    if not math.isfinite(float(value)):
        raise ValueError("source numeric values must be finite")
    return float(value)


class _HistoricalPriceRow(ContractModel):
    """OpenBB-compatible OHLCV fields plus the requested symbol."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    date: _date | datetime
    open: float
    high: float
    low: float
    close: float
    volume: StrictInt | None = None
    vwap: float | None = None
    adj_close: float | None = None
    dividend: float | None = None
    split_ratio: float | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> object:
        return _source_symbol(value)

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> object:
        return _source_date(value)

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "vwap",
        "adj_close",
        "dividend",
        "split_ratio",
        mode="before",
    )
    @classmethod
    def _validate_numeric_fields(cls, value: object) -> object:
        return _finite_number(value)

    @field_validator("dividend")
    @classmethod
    def _validate_dividend(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("dividend must be non-negative")
        return value

    @field_validator("split_ratio")
    @classmethod
    def _validate_split_ratio(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("split_ratio must be positive")
        return value

    @model_validator(mode="after")
    def _validate_ohlc(self) -> Self:
        if (
            self.low > self.high
            or self.high < max(self.open, self.close)
            or self.low > min(self.open, self.close)
        ):
            raise ValueError("OHLC values are inconsistent")
        if self.volume is not None and self.volume < 0:
            raise ValueError("volume must be non-negative")
        return self


class AlphaVantageEquityHistorical(_HistoricalPriceRow):
    """One Alpha Vantage equity history row."""


class AlphaVantageEtfHistorical(_HistoricalPriceRow):
    """One Alpha Vantage ETF history row."""


class AlphaVantageHistoricalEps(ContractModel):
    """One Alpha Vantage annual or quarterly earnings row."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    symbol: str
    date: _date
    eps_actual: float | None
    eps_estimated: float | None = None
    surprise: float | None = None
    surprise_percent: float | None = None
    reported_date: _date | None = None
    report_time: StrictStr | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbol(cls, value: object) -> object:
        return _source_symbol(value)

    @field_validator("date", "reported_date", mode="before")
    @classmethod
    def _validate_eps_dates(cls, value: object) -> object:
        if value is None:
            return None
        result = _source_date(value)
        if isinstance(result, datetime):
            raise ValueError("earnings dates must not include a time")
        return result

    @field_validator("eps_actual", "eps_estimated", "surprise", "surprise_percent", mode="before")
    @classmethod
    def _validate_numeric_fields(cls, value: object) -> object:
        return _finite_number(value)
