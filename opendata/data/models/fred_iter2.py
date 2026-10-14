"""OpenBB-shaped contracts for fixed FRED models added in iteration two."""

from __future__ import annotations

from datetime import date as date_type
from typing import Literal

from pydantic import Field, PrivateAttr

from opendata.data.models.base import ContractModel


class FredTreasuryConstantMaturity(ContractModel):
    """One Treasury constant-maturity rate row (OpenBB date/rate shape)."""

    date: date_type = Field(strict=True)
    rate: float | None = Field(default=None, strict=True)

    _series_id: str = PrivateAttr(default="")
    _maturity: str = PrivateAttr(default="")
    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredSelectedTreasuryBill(ContractModel):
    """One selected Treasury bill rate row (OpenBB date/rate shape)."""

    date: date_type = Field(strict=True)
    rate: float | None = Field(default=None, strict=True)

    _series_id: str = PrivateAttr(default="")
    _maturity: str = PrivateAttr(default="")
    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredSelectedTreasuryConstantMaturity(ContractModel):
    """One selected Treasury constant-maturity rate row (date/rate shape)."""

    date: date_type = Field(strict=True)
    rate: float | None = Field(default=None, strict=True)

    _series_id: str = PrivateAttr(default="")
    _maturity: str = PrivateAttr(default="")
    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredAmeribor(ContractModel):
    """One AMERIBOR series observation in the OpenBB date/symbol shape."""

    date: date_type = Field(strict=True)
    symbol: str = Field(strict=True)
    maturity: str = Field(strict=True)
    rate: float = Field(strict=True)
    title: str | None = Field(default=None, strict=True)

    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredDiscountWindowPrimaryCreditRate(ContractModel):
    """One primary-credit rate observation."""

    date: date_type = Field(strict=True)
    rate: float | None = Field(default=None, strict=True)

    _series_id: str = PrivateAttr(default="")
    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredEuropeanCentralBankInterestRate(ContractModel):
    """One ECB interest-rate observation."""

    date: date_type = Field(strict=True)
    rate: float | None = Field(default=None, strict=True)

    _series_id: str = PrivateAttr(default="")
    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredTipsYield(ContractModel):
    """One constant-maturity TIPS yield observation."""

    date: date_type = Field(strict=True)
    symbol: str | None = Field(default=None, strict=True)
    due: date_type | None = Field(default=None, strict=True)
    name: str | None = Field(default=None, strict=True)
    value: float | None = Field(default=None, strict=True)

    _realtime_start: date_type | None = PrivateAttr(default=None)
    _realtime_end: date_type | None = PrivateAttr(default=None)


class FredFederalFundsRate(ContractModel):
    """One effective federal funds observation and aligned NY Fed fields."""

    date: date_type = Field(strict=True)
    rate: float = Field(strict=True)
    target_range_upper: float | None = Field(default=None, strict=True)
    target_range_lower: float | None = Field(default=None, strict=True)
    percentile_1: float | None = Field(default=None, strict=True)
    percentile_25: float | None = Field(default=None, strict=True)
    percentile_75: float | None = Field(default=None, strict=True)
    percentile_99: float | None = Field(default=None, strict=True)
    volume: float | None = Field(default=None, strict=True)
    intraday_low: float | None = Field(default=None, strict=True)
    intraday_high: float | None = Field(default=None, strict=True)
    standard_deviation: float | None = Field(default=None, strict=True)
    revision_indicator: str | None = Field(default=None, strict=True)

    _realtime_by_series: dict[str, tuple[date_type, date_type]] = PrivateAttr(default_factory=dict)


class FredOvernightBankFundingRate(ContractModel):
    """One OBFR observation and aligned New York Fed distribution fields."""

    date: date_type = Field(strict=True)
    rate: float = Field(strict=True)
    percentile_1: float | None = Field(default=None, strict=True)
    percentile_25: float | None = Field(default=None, strict=True)
    percentile_75: float | None = Field(default=None, strict=True)
    percentile_99: float | None = Field(default=None, strict=True)
    volume: float | None = Field(default=None, strict=True)
    revision_indicator: str | None = Field(default=None, strict=True)

    _realtime_by_series: dict[str, tuple[date_type, date_type]] = PrivateAttr(default_factory=dict)


class FredReleaseTable(ContractModel):
    """One flattened FRED release-table element."""

    date: date_type | None = Field(default=None, strict=True)
    level: int | None = Field(default=None, strict=True)
    element_type: str | None = Field(default=None, strict=True)
    line: int | None = Field(default=None, strict=True)
    element_id: str | None = Field(default=None, strict=True)
    parent_id: str | None = Field(default=None, strict=True)
    children: str | None = Field(default=None, strict=True)
    symbol: str | None = Field(default=None, strict=True)
    name: str | None = Field(default=None, strict=True)
    value: float | None = Field(default=None, strict=True)


class FredEconomicCalendar(ContractModel):
    """An official FRED release calendar date, independent of data availability."""

    date: date_type = Field(strict=True)
    release_id: int = Field(strict=True)
    event: str = Field(strict=True)


class FredRegional(ContractModel):
    """A regional cross-section row returned by GeoFRED."""

    date: date_type = Field(strict=True)
    region: str = Field(strict=True)
    code: str = Field(strict=True)
    value: float | None = Field(default=None, strict=True)
    series_id: str = Field(strict=True)


class FredBalanceOfPayments(ContractModel):
    """A U.S. BOP quarterly row with explicit FRED units and source IDs."""

    period: date_type | None = Field(default=None, strict=True)
    current_account: float | None = Field(default=None, strict=True)
    goods_balance: float | None = Field(default=None, strict=True)
    services_balance: float | None = Field(default=None, strict=True)
    primary_income_balance: float | None = Field(default=None, strict=True)
    secondary_income_balance: float | None = Field(default=None, strict=True)
    currency_code: Literal["USD"] = "USD"
    unit: Literal["millions_of_us_dollars"] = "millions_of_us_dollars"
    source_series_ids: dict[str, str] = Field(strict=True)

    _realtime_by_series: dict[str, tuple[date_type, date_type]] = PrivateAttr(default_factory=dict)


class FredPROJECTIONS(ContractModel):
    """FOMC Fed-funds projection values aligned by FRED observation date."""

    date: date_type = Field(strict=True)
    range_high: float | None = Field(default=None, strict=True)
    central_tendency_high: float | None = Field(default=None, strict=True)
    median: float | None = Field(default=None, strict=True)
    range_midpoint: float | None = Field(default=None, strict=True)
    central_tendency_midpoint: float | None = Field(default=None, strict=True)
    range_low: float | None = Field(default=None, strict=True)
    central_tendency_low: float | None = Field(default=None, strict=True)

    _realtime_by_series: dict[str, tuple[date_type, date_type]] = PrivateAttr(default_factory=dict)


__all__ = (
    "FredAmeribor",
    "FredDiscountWindowPrimaryCreditRate",
    "FredEuropeanCentralBankInterestRate",
    "FredBalanceOfPayments",
    "FredEconomicCalendar",
    "FredFederalFundsRate",
    "FredOvernightBankFundingRate",
    "FredPROJECTIONS",
    "FredRegional",
    "FredReleaseTable",
    "FredSelectedTreasuryBill",
    "FredSelectedTreasuryConstantMaturity",
    "FredTipsYield",
    "FredTreasuryConstantMaturity",
)
