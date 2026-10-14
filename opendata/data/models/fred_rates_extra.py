"""Typed public contracts for FRED fixed-income and commodity models.

Field names and output shapes mirror the published FRED model references.
Numerical values stay in FRED's published units (for example, percentage
points are not divided by 100).
"""

from __future__ import annotations

import re
from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import StrictFloat, StrictInt, StrictStr, field_validator

from opendata.data.models.base import ContractModel

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _strict_date(value: object) -> date_type:
    if type(value) is date_type:
        return value
    if isinstance(value, datetime):
        raise ValueError("date fields must not include a time")
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("date fields must be date objects or canonical YYYY-MM-DD strings")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date fields must be valid YYYY-MM-DD dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("date fields must be canonical YYYY-MM-DD strings")
    return parsed


class _DateRow(ContractModel):
    date: date_type

    @field_validator("date", mode="before")
    @classmethod
    def _validate_date(cls, value: object) -> date_type:
        return _strict_date(value)


class FredEuroShortTermRate(_DateRow):
    """Wide ECB €STR output with the seven documented daily statistics."""

    rate: StrictFloat
    percentile_25: StrictFloat | None = None
    percentile_75: StrictFloat | None = None
    volume: StrictFloat | None = None
    transactions: StrictInt | None = None
    number_of_banks: StrictInt | None = None
    large_bank_share_of_volume: StrictFloat | None = None

    @field_validator(
        "rate",
        "percentile_25",
        "percentile_75",
        "volume",
        "large_bank_share_of_volume",
        mode="before",
    )
    @classmethod
    def _require_exact_float_values(cls, value: object) -> object:
        if value is not None and type(value) is not float:
            raise ValueError("rate statistics must be floats or None")
        return value

    @field_validator("transactions", "number_of_banks", mode="before")
    @classmethod
    def _require_exact_count_values(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("count statistics must be integers or None")
        return value


class FredCommercialPaper(_DateRow):
    """One daily commercial-paper observation, matching the public long row."""

    symbol: StrictStr | None
    maturity: StrictStr
    rate: StrictFloat
    title: StrictStr | None
    asset_type: Literal["asset_backed", "financial", "nonfinancial", "a2p2"]

    @field_validator("symbol", "title")
    @classmethod
    def _require_nonempty_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("optional text fields must not be empty")
        return value

    @field_validator("maturity")
    @classmethod
    def _require_maturity_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("maturity must not be empty")
        return value

    @field_validator("rate", mode="before")
    @classmethod
    def _require_exact_rate(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("commercial-paper rate must be a float")
        return value


class FredSpotRate(_DateRow):
    """Corporate spot or par rate at one mapped maturity."""

    category: Literal["spot_rate", "par_yield"]
    maturity: StrictStr
    rate: StrictFloat | None

    @field_validator("maturity")
    @classmethod
    def _require_maturity_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("maturity must not be empty")
        return value

    @field_validator("rate", mode="before")
    @classmethod
    def _require_exact_rate(cls, value: object) -> object:
        if value is not None and type(value) is not float:
            raise ValueError("rate must be a float or None")
        return value


class FredHighQualityMarketCorporateBond(_DateRow):
    """High-quality-market corporate-bond curve point."""

    yield_curve: Literal["spot", "par"]
    rate: StrictFloat
    maturity: StrictStr

    @field_validator("rate", mode="before")
    @classmethod
    def _require_exact_rate(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("rate must be a float")
        return value

    @field_validator("maturity")
    @classmethod
    def _require_maturity_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("maturity must not be empty")
        return value


class FredBondIndices(_DateRow):
    """One mapped index observation, matching the public long row."""

    symbol: StrictStr | None
    value: StrictFloat
    maturity: StrictStr | None
    title: StrictStr

    @field_validator("symbol", "maturity", "title")
    @classmethod
    def _require_nonempty_text_metadata(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text metadata must not be empty")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def _require_exact_value(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("bond-index value must be a float")
        return value


class FredCommoditySpotPrices(_DateRow):
    """One FRED-distributed EIA commodity spot-price observation."""

    symbol: StrictStr | None
    commodity: StrictStr | None
    price: StrictFloat
    unit: StrictStr | None

    @field_validator("symbol", "commodity", "unit")
    @classmethod
    def _require_nonempty_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("text metadata must not be empty")
        return value

    @field_validator("price", mode="before")
    @classmethod
    def _require_exact_price(cls, value: object) -> object:
        if type(value) is not float:
            raise ValueError("price must be a float")
        return value


class FredYieldCurve(_DateRow):
    """Published FRED yield-curve point at a source-mapped maturity."""

    yield_curve_type: Literal[
        "nominal",
        "real",
        "breakeven",
        "treasury_minus_fed_funds",
        "corporate_spot",
        "corporate_par",
    ]
    maturity: StrictStr
    rate: StrictFloat | None

    @field_validator("maturity")
    @classmethod
    def _require_maturity_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("maturity must not be empty")
        return value

    @field_validator("rate", mode="before")
    @classmethod
    def _require_exact_rate(cls, value: object) -> object:
        if value is not None and type(value) is not float:
            raise ValueError("rate must be a float or None")
        return value


__all__ = (
    "FredBondIndices",
    "FredCommercialPaper",
    "FredCommoditySpotPrices",
    "FredEuroShortTermRate",
    "FredHighQualityMarketCorporateBond",
    "FredSpotRate",
    "FredYieldCurve",
)
