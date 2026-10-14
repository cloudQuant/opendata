"""Strict shared query and value parsing for source-specific Fed models."""

from __future__ import annotations

import math
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.protocol import QueryParams
from opendata.data.providers.federal_reserve._iter2_client import FederalReserveSourceError


class FederalReserveDateQuery(QueryParams):
    """Common strict inclusive date window accepting service-injected identity fields."""

    source: Literal["federal_reserve"] = "federal_reserve"
    market: Literal["us"] = "us"
    symbol: None = None
    start_date: date | None = None
    end_date: date | None = None
    max_records: StrictInt = Field(default=200_000, ge=1, le=200_000)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _parse_iso_date(cls, value: object) -> object:
        """Require a date or canonical YYYY-MM-DD string, never a timestamp."""
        if value is None or type(value) is date:
            return value
        if isinstance(value, datetime):
            raise ValueError("date bounds must not include a time")
        if not isinstance(value, str):
            raise ValueError("date bounds must be dates")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date bounds must be canonical YYYY-MM-DD strings") from exc
        if parsed.isoformat() != value:
            raise ValueError("date bounds must be canonical YYYY-MM-DD strings")
        return parsed

    @model_validator(mode="after")
    def _validate_window(self) -> FederalReserveDateQuery:
        """Reject inverted windows before any source request."""
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.end_date < self.start_date
        ):
            raise ValueError("end_date must not precede start_date")
        return self


def parse_decimal(value: object, *, nullable: bool = False) -> Decimal | None:
    """Parse a source numeric text cell exactly, preserving only documented blanks."""
    if value is None or value == "":
        if nullable:
            return None
        raise FederalReserveSourceError("FEDERAL_RESERVE_MISSING_NUMERIC_VALUE")
    if isinstance(value, bool):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE") from None
    if not parsed.is_finite():
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE")
    return parsed


def parse_float(value: object, *, nullable: bool = True) -> float | None:
    """Parse a finite workbook cell without converting blanks into zero."""
    if value is None or value == "":
        if nullable:
            return None
        raise FederalReserveSourceError("FEDERAL_RESERVE_MISSING_NUMERIC_VALUE")
    if isinstance(value, bool):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE")
    if not isinstance(value, (int, float, str, Decimal)):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE") from None
    if not math.isfinite(parsed):
        raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_NUMERIC_VALUE")
    return parsed


def parse_workbook_date(value: object) -> date:
    """Read a strict workbook date cell, rejecting timestamps with non-midnight time."""
    if type(value) is date:
        return value
    if isinstance(value, datetime):
        if value.time().isoformat() != "00:00:00":
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
        return value.date()
    if isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE") from None
        if parsed.isoformat() != value:
            raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")
        return parsed
    raise FederalReserveSourceError("FEDERAL_RESERVE_BAD_DATE")


__all__ = [
    "FederalReserveDateQuery",
    "Literal",
    "StrictStr",
    "parse_decimal",
    "parse_float",
    "parse_workbook_date",
]
