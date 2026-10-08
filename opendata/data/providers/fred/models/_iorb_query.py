"""Strict query contract for the single FRED IORB series."""

from __future__ import annotations

import re
from datetime import date as date_type
from typing import Literal

from pydantic import ConfigDict, field_validator

from opendata.data.providers.fred.models.series import FredSeriesQuery

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _parse_strict_date(value: object) -> date_type:
    if type(value) is date_type:
        return value
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("date values must be date objects or canonical YYYY-MM-DD strings")
    try:
        parsed = date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date values must be valid YYYY-MM-DD dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("date values must be canonical YYYY-MM-DD strings")
    return parsed


class FredIorbQuery(FredSeriesQuery):
    """Complete FRED observation request fixed to the IORB series."""

    model_config = ConfigDict(revalidate_instances="always")

    series_id: Literal["IORB"] = "IORB"
    source: Literal["auto", "fred"] = "auto"
    market: Literal["us"] | None = None
    symbol: None = None

    @field_validator("output_type", mode="before")
    @classmethod
    def _require_exact_output_type_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("output_type must be an integer from 1 through 4")
        return value

    @field_validator(
        "start_date",
        "end_date",
        "as_of",
        "realtime_start",
        "realtime_end",
        mode="before",
    )
    @classmethod
    def _validate_strict_date(cls, value: object) -> date_type | None:
        if value is None:
            return None
        return _parse_strict_date(value)

    @field_validator("vintage_dates", mode="before")
    @classmethod
    def _validate_vintage_dates(cls, value: object) -> object:
        if value is None:
            return None
        if not isinstance(value, (list, tuple)):
            raise ValueError("vintage_dates must be an ordered list of dates")
        if not value:
            raise ValueError("vintage_dates must not be empty when provided")
        if len(value) > 2000:
            raise ValueError("vintage_dates must contain at most 2000 dates")
        return [_parse_strict_date(item) for item in value]


__all__ = ("FredIorbQuery",)
