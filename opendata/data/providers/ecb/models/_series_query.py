"""Strict query-only contracts for the ECB YC and BPS series products.

These models validate one caller-supplied source key and optional observation
window. They do not establish that a series exists, is accessible, or is
covered by a rights grant.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import Field, StrictStr, ValidationInfo, field_validator, model_validator

from opendata.data.protocol import QueryParams

_MAX_RECORDS = 10_000
_DIMENSION_TOKEN = re.compile(r"[A-Z0-9_][A-Z0-9_-]{0,31}\Z")
_MONTH_PERIOD = re.compile(r"(?P<year>[0-9]{4})-(?:0[1-9]|1[0-2])\Z")
_QUARTER_PERIOD = re.compile(r"(?P<year>[0-9]{4})-Q[1-4]\Z")


def _validate_complete_series_key(
    value: str,
    *,
    prefix: Literal["YC", "BPS"],
    dimension_count: int,
    frequencies: frozenset[str],
) -> str:
    """Require a product prefix followed by its full ordered token tuple."""
    parts = value.split(".")
    if len(parts) != dimension_count + 1 or parts[0] != prefix:
        raise ValueError(f"series_key must contain the complete {prefix} dimension sequence")
    dimensions = parts[1:]
    if any(_DIMENSION_TOKEN.fullmatch(token) is None for token in dimensions):
        raise ValueError("series_key dimensions must be safe single uppercase source tokens")
    if dimensions[0] not in frequencies:
        raise ValueError(f"{prefix} frequency is outside the supported product slice")
    return value


def _validate_date_input(value: object) -> date | None:
    """Accept a date or canonical ISO date text without dropping time precision."""
    if value is None:
        return None
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date must be a canonical YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("date must be a canonical YYYY-MM-DD date")
        return parsed
    if isinstance(value, datetime):
        raise ValueError("date must not include a time")
    raise ValueError("date must be a date or canonical YYYY-MM-DD string")


def _validate_period(value: str, frequency: str, field_name: str) -> str:
    """Preserve a canonical monthly or quarterly BPS period string."""
    pattern = _MONTH_PERIOD if frequency == "M" else _QUARTER_PERIOD
    match = pattern.fullmatch(value)
    if match is None:
        expected = "YYYY-MM" if frequency == "M" else "YYYY-Qn"
        raise ValueError(f"{field_name} must match {expected} for the selected frequency")
    if not 1 <= int(match.group("year")) <= 9999:
        raise ValueError(f"{field_name} year must be between 0001 and 9999")
    return value


class EcbYieldCurveQuery(QueryParams):
    """One complete YC business-day series and an optional daily date window."""

    series_key: StrictStr
    source: Literal["auto", "ecb"] = "auto"
    market: Literal["eu"] | None = None
    symbol: None = None
    start_date: date | None = None
    end_date: date | None = None
    max_records: int = Field(default=_MAX_RECORDS, strict=True, ge=1, le=_MAX_RECORDS)

    @field_validator("series_key")
    @classmethod
    def _validate_series_key(cls, value: str) -> str:
        return _validate_complete_series_key(
            value,
            prefix="YC",
            dimension_count=7,
            frequencies=frozenset({"B"}),
        )

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_date_input(cls, value: object) -> date | None:
        return _validate_date_input(value)

    @model_validator(mode="after")
    def _validate_date_window(self) -> EcbYieldCurveQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not precede start_date")
        return self


class EcbBalanceOfPaymentsQuery(QueryParams):
    """One complete BPS monthly/quarterly series and optional source periods."""

    series_key: StrictStr
    source: Literal["auto", "ecb"] = "auto"
    market: Literal["eu"] | None = None
    symbol: None = None
    start_date: None = None
    end_date: None = None
    start_period: StrictStr | None = None
    end_period: StrictStr | None = None
    max_records: int = Field(default=_MAX_RECORDS, strict=True, ge=1, le=_MAX_RECORDS)

    @field_validator("series_key")
    @classmethod
    def _validate_series_key(cls, value: str) -> str:
        return _validate_complete_series_key(
            value,
            prefix="BPS",
            dimension_count=17,
            frequencies=frozenset({"M", "Q"}),
        )

    @field_validator("start_period", "end_period")
    @classmethod
    def _validate_period_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        series_key = info.data.get("series_key")
        if not isinstance(series_key, str):
            raise ValueError("a complete BPS series_key is required to validate periods")
        frequency = series_key.split(".", maxsplit=2)[1]
        return _validate_period(value, frequency, info.field_name or "period")

    @model_validator(mode="after")
    def _validate_period_window(self) -> EcbBalanceOfPaymentsQuery:
        if (
            self.start_period is not None
            and self.end_period is not None
            and self.end_period < self.start_period
        ):
            raise ValueError("end_period must not precede start_period")
        return self


__all__ = ("EcbBalanceOfPaymentsQuery", "EcbYieldCurveQuery")
