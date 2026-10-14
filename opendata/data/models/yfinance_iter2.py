"""Typed contracts for the source-native yfinance Iteration 2 responses.

These contracts preserve fields exposed by the optional SDK. They deliberately do not
invent announcement dates, financial currencies, or valuation units that the SDK response
does not contain.
"""

from __future__ import annotations

import math
from datetime import date as date_type
from datetime import datetime
from numbers import Real
from typing import Literal

from pydantic import StrictInt, StrictStr, field_validator, model_validator

from opendata.data.models.base import ContractModel


class YfinanceFinancialStatementObservation(ContractModel):
    """One reported line item for one fiscal period from yfinance's statement frame."""

    symbol: StrictStr
    statement_type: Literal["balance_sheet", "cash_flow_statement", "income_statement"]
    report_period: date_type
    line_item: StrictStr
    value: float | None
    unit_status: Literal["not_provided_by_sdk"] = "not_provided_by_sdk"
    frequency: Literal["yearly", "quarterly"]

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be a nonblank token")
        return value

    @field_validator("line_item")
    @classmethod
    def _validate_line_item(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("line_item must be a nonblank token")
        return value

    @field_validator("report_period", mode="before")
    @classmethod
    def _validate_report_period(cls, value: object) -> object:
        if isinstance(value, datetime):
            if value.time().isoformat() != "00:00:00":
                raise ValueError("report_period must not include a time")
            return value.date()
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            parsed = date_type.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("report_period must be a date")

    @field_validator("value", mode="before")
    @classmethod
    def _validate_value(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("statement value must be a finite number or None")
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("statement value must be a finite number or None") from exc
        if not math.isfinite(converted):
            raise ValueError("statement value must be a finite number or None")
        return converted


class YfinanceDividendObservation(ContractModel):
    """Public dividend fields from the SDK's exchange-local Series index/value."""

    symbol: StrictStr
    ex_dividend_date: date_type
    amount: float
    currency: None = None
    currency_status: Literal["not_provided_by_sdk"] = "not_provided_by_sdk"
    date_semantics_status: Literal["sdk_event_date_not_verified_as_ex_dividend_date"] = (
        "sdk_event_date_not_verified_as_ex_dividend_date"
    )
    amount_semantics_status: Literal[
        "sdk_amount_scale_preserved_currency_and_per_share_basis_unverified"
    ] = "sdk_amount_scale_preserved_currency_and_per_share_basis_unverified"

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be a nonblank token")
        return value

    @field_validator("ex_dividend_date", mode="before")
    @classmethod
    def _validate_ex_dividend_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            # The SDK index is already exchange-local. Keep its calendar day;
            # converting to UTC here could shift the public date by one day.
            return value.date()
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            parsed = date_type.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("ex_dividend_date must be a date")

    @field_validator("amount", mode="before")
    @classmethod
    def _validate_amount(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("amount must be a finite number")
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("amount must be a finite number") from exc
        if not math.isfinite(converted):
            raise ValueError("amount must be a finite number")
        return converted

    @property
    def event_date(self) -> date_type:
        """Read-only compatibility name for pre-public-field callers."""
        return self.ex_dividend_date

    @property
    def amount_per_share(self) -> float:
        """Read-only compatibility name; the source basis remains unverified."""
        return self.amount


class YfinanceHistoricalBar(ContractModel):
    """One Yahoo bar, preserving intraday timestamps and the requested cadence."""

    model_id: Literal[
        "CryptoHistorical",
        "CurrencyHistorical",
        "EquityHistorical",
        "EtfHistorical",
        "FuturesHistorical",
        "IndexHistorical",
    ]
    symbol: StrictStr
    session_date: datetime | date_type
    interval: Literal[
        "1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h", "1d", "5d", "1W", "1M", "1Q"
    ] = "1d"
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float | None
    volume: float | None
    dividends: float | None
    stock_splits: float | None
    currency: None = None
    currency_status: Literal["not_provided_by_sdk"] = "not_provided_by_sdk"
    price_adjustment: Literal["not_applicable", "splits_only", "splits_and_dividends"] = (
        "not_applicable"
    )

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be a nonblank token")
        return value

    @field_validator("session_date", mode="before")
    @classmethod
    def _validate_session_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            parsed = date_type.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("session_date must be a date or datetime")

    @field_validator("interval")
    @classmethod
    def _validate_interval(cls, value: str) -> str:
        if value not in {
            "1m",
            "2m",
            "5m",
            "15m",
            "30m",
            "60m",
            "90m",
            "1h",
            "1d",
            "5d",
            "1W",
            "1M",
            "1Q",
        }:
            raise ValueError("interval is not in the yfinance historical contract")
        return value

    @field_validator(
        "open",
        "high",
        "low",
        "close",
        "adjusted_close",
        "volume",
        "dividends",
        "stock_splits",
        mode="before",
    )
    @classmethod
    def _validate_bar_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("bar value must be a finite number or None")
        try:
            converted = float(value)
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError("bar value must be a finite number or None") from exc
        if not math.isfinite(converted):
            raise ValueError("bar value must be a finite number or None")
        return converted


class YfinancePayloadObservation(ContractModel):
    """A source-native SDK payload encoded losslessly as canonical JSON."""

    model_id: StrictStr
    symbol: StrictStr
    ordinal: int = 0
    retrieved_at_utc: datetime
    raw_payload_json: StrictStr

    @field_validator("symbol", "model_id")
    @classmethod
    def _validate_tokens(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("payload identity must be a nonblank token")
        return value

    @field_validator("ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ordinal must be nonnegative")
        return value

    @field_validator("retrieved_at_utc")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("retrieved_at_utc must be timezone-aware UTC")
        return value


class YfinanceCompanyNewsObservation(ContractModel):
    """One company news item, keeping the provider payload and parsed publish time."""

    symbol: StrictStr
    ordinal: int
    published_at_utc: datetime
    raw_payload_json: StrictStr

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("symbol must be nonblank")
        return value

    @field_validator("ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ordinal must be nonnegative")
        return value


class YfinanceScreenerObservation(ContractModel):
    """One Yahoo screener quote, retaining all SDK quote fields in JSON."""

    screener_id: StrictStr
    ordinal: int
    symbol: StrictStr
    raw_payload_json: StrictStr

    @field_validator("screener_id")
    @classmethod
    def _validate_screener_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("screener_id must be nonblank")
        return value

    @field_validator("ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ordinal must be nonnegative")
        return value


MetricAvailability = Literal["reported", "source_missing", "not_returned_by_sdk_method"]


class YfinanceKeyMetricObservation(ContractModel):
    """One public named-field row per symbol and valuation-measures period."""

    symbol: StrictStr
    period_ending: date_type | None
    # Source-specific identity extensions: ``Current`` has no period date, while
    # dated columns retain both the public date and their current/history kind.
    period_date: date_type | None
    period_kind: Literal["current", "historical"]
    fiscal_year: StrictInt | None = None
    fiscal_period: StrictStr | None = None
    currency: StrictStr | None = None
    market_cap: int | float | None = None
    pe_ratio: float | None = None
    forward_pe: float | None = None
    peg_ratio: float | None = None
    peg_ratio_ttm: float | None = None
    eps_ttm: float | None = None
    eps_forward: float | None = None
    enterprise_to_ebitda: float | None = None
    earnings_growth: float | None = None
    earnings_growth_quarterly: float | None = None
    revenue_per_share: float | None = None
    revenue_growth: float | None = None
    enterprise_to_revenue: float | None = None
    cash_per_share: float | None = None
    quick_ratio: float | None = None
    current_ratio: float | None = None
    debt_to_equity: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    ebitda_margin: float | None = None
    profit_margin: float | None = None
    return_on_assets: float | None = None
    return_on_equity: float | None = None
    dividend_yield: float | None = None
    dividend_yield_5y_avg: float | None = None
    payout_ratio: float | None = None
    book_value: float | None = None
    price_to_book: float | None = None
    enterprise_value: StrictInt | None = None
    overall_risk: float | None = None
    audit_risk: float | None = None
    board_risk: float | None = None
    compensation_risk: float | None = None
    shareholder_rights_risk: float | None = None
    beta: float | None = None
    price_return_1y: float | None = None
    # yfinance returns Price/Sales, but ODP's yfinance block has no named field for it.
    price_to_sales: float | None = None
    metric_availability: dict[str, MetricAvailability]
    unit_status: Literal["not_provided_by_sdk"] = "not_provided_by_sdk"
    fiscal_metadata_status: Literal["not_returned_by_sdk"] = "not_returned_by_sdk"

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be a nonblank token")
        return value

    @field_validator("period_ending", mode="before")
    @classmethod
    def _validate_period_ending(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.time().isoformat() != "00:00:00":
                raise ValueError("period_ending must not include a time")
            return value.date()
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            parsed = date_type.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("period_ending must be a date or None")

    @field_validator("period_date", mode="before")
    @classmethod
    def _validate_period_date(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.time().isoformat() != "00:00:00":
                raise ValueError("period_date must not include a time")
            return value.date()
        if isinstance(value, date_type):
            return value
        if isinstance(value, str):
            parsed = date_type.fromisoformat(value)
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("period_date must be a date or None")

    @model_validator(mode="after")
    def _validate_period_identity(self) -> YfinanceKeyMetricObservation:
        if self.period_date != self.period_ending:
            raise ValueError("period_date must equal period_ending")
        if (self.period_kind == "current") != (self.period_date is None):
            raise ValueError("current metrics have no period date; historical metrics require one")
        return self

    @field_validator("market_cap", mode="before")
    @classmethod
    def _validate_market_cap(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("market_cap must be a finite number or None")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("market_cap must be a finite number or None")
        return int(value) if isinstance(value, int) else converted

    @field_validator(
        "pe_ratio",
        "forward_pe",
        "peg_ratio",
        "peg_ratio_ttm",
        "eps_ttm",
        "eps_forward",
        "enterprise_to_ebitda",
        "earnings_growth",
        "earnings_growth_quarterly",
        "revenue_per_share",
        "revenue_growth",
        "enterprise_to_revenue",
        "cash_per_share",
        "quick_ratio",
        "current_ratio",
        "debt_to_equity",
        "gross_margin",
        "operating_margin",
        "ebitda_margin",
        "profit_margin",
        "return_on_assets",
        "return_on_equity",
        "dividend_yield",
        "dividend_yield_5y_avg",
        "payout_ratio",
        "book_value",
        "price_to_book",
        "overall_risk",
        "audit_risk",
        "board_risk",
        "compensation_risk",
        "shareholder_rights_risk",
        "beta",
        "price_return_1y",
        "price_to_sales",
        mode="before",
    )
    @classmethod
    def _validate_metric_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("metric value must be a finite number or None")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("metric value must be a finite number or None")
        return converted

    @field_validator("enterprise_value", mode="before")
    @classmethod
    def _validate_enterprise_value(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("enterprise_value must be an integer or None")
        if isinstance(value, int):
            return value
        converted = float(value)
        if not math.isfinite(converted) or not converted.is_integer():
            raise ValueError("enterprise_value must be an integer or None")
        return int(converted)


class YfinanceKeyExecutiveObservation(ContractModel):
    """Public key-executive fields mapped from an unstable info payload sample."""

    symbol: StrictStr
    title: StrictStr
    name: StrictStr
    pay: StrictInt | None = None
    currency_pay: StrictStr | None = None
    gender: StrictStr | None = None
    year_born: StrictInt | None = None
    exercised_value: StrictInt | None = None
    unexercised_value: StrictInt | None = None
    fiscal_year: StrictInt | None = None
    ordinal: int
    retrieved_at_utc: datetime
    raw_payload_json: StrictStr

    @field_validator("symbol")
    @classmethod
    def _validate_symbol(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("symbol must be a nonblank token")
        return value

    @field_validator("name", "title")
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("executive name and title must be nonblank")
        return value

    @field_validator("ordinal")
    @classmethod
    def _validate_ordinal(cls, value: int) -> int:
        if value < 0:
            raise ValueError("ordinal must be nonnegative")
        return value

    @field_validator("retrieved_at_utc")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("retrieved_at_utc must be timezone-aware UTC")
        return value


class YfinanceOptionChainObservation(ContractModel):
    """One dated call/put option row from a specific expiration chain."""

    symbol: StrictStr
    expiration_date: date_type
    option_type: Literal["call", "put"]
    contract_symbol: StrictStr
    strike: float
    last_price: float | None
    bid: float | None
    ask: float | None
    volume: float | None
    open_interest: float | None
    implied_volatility: float | None
    in_the_money: bool | None
    raw_payload_json: StrictStr

    @field_validator(
        "strike",
        "last_price",
        "bid",
        "ask",
        "volume",
        "open_interest",
        "implied_volatility",
        mode="before",
    )
    @classmethod
    def _validate_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("option value must be a finite number or None")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("option value must be a finite number or None")
        return converted


class YfinancePriceTargetConsensus(ContractModel):
    """Provider's current analyst target statistics, with no inferred as-of date."""

    symbol: StrictStr
    name: StrictStr | None = None
    target_high: float | None
    target_low: float | None
    target_consensus: float | None
    target_median: float | None
    recommendation: StrictStr | None = None
    recommendation_mean: float | None = None
    number_of_analysts: StrictInt | None = None
    current_price: float | None
    retrieved_at_utc: datetime
    currency: StrictStr | None = None
    currency_status: Literal["not_provided_by_sdk"] = "not_provided_by_sdk"
    timestamp_semantics_status: Literal["retrieval_time_is_not_estimate_date"] = (
        "retrieval_time_is_not_estimate_date"
    )

    @field_validator(
        "current_price",
        "target_high",
        "target_low",
        "target_consensus",
        "target_median",
        "recommendation_mean",
        mode="before",
    )
    @classmethod
    def _validate_target_number(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("price target must be a finite number or None")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("price target must be a finite number or None")
        return converted

    @field_validator("retrieved_at_utc")
    @classmethod
    def _require_utc_timestamp(cls, value: datetime) -> datetime:
        offset = value.utcoffset()
        if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
            raise ValueError("retrieved_at_utc must be timezone-aware UTC")
        return value

    @property
    def current(self) -> float | None:
        """Read-only compatibility name for the SDK's current-price key."""
        return self.current_price

    @property
    def low(self) -> float | None:
        """Read-only compatibility name for the SDK's low target key."""
        return self.target_low

    @property
    def high(self) -> float | None:
        """Read-only compatibility name for the SDK's high target key."""
        return self.target_high

    @property
    def mean(self) -> float | None:
        """Read-only compatibility name for the SDK's mean consensus key."""
        return self.target_consensus

    @property
    def median(self) -> float | None:
        """Read-only compatibility name for the SDK's median target key."""
        return self.target_median
