"""Strict query contract for FRED observations of Bank of England SONIA."""

from __future__ import annotations

import re
from datetime import date as date_type
from typing import Literal

from pydantic import ConfigDict, field_validator, model_validator

from opendata.data.models.fred_sonia import _SONIA_PARAMETER_MAP
from opendata.data.providers.fred.models.series import FredSeriesQuery


def _parse_strict_date(value: object) -> date_type:
    if type(value) is date_type:
        return value
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("date values must be date objects or canonical YYYY-MM-DD strings")
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date values must be valid YYYY-MM-DD dates") from exc


class FredSoniaQuery(FredSeriesQuery):
    """SONIA query with exact parameter-to-series binding and FRED bounds."""

    model_config = ConfigDict(revalidate_instances="always")

    parameter: Literal[
        "rate",
        "index",
        "10th_percentile",
        "25th_percentile",
        "75th_percentile",
        "90th_percentile",
        "total_nominal_value",
    ] = "rate"
    series_id: Literal[
        "IUDSOIA",
        "IUDZOS2",
        "IUDZLS6",
        "IUDZLS7",
        "IUDZLS8",
        "IUDZLS9",
        "IUDZLT2",
    ] = "IUDSOIA"
    source: Literal["auto", "fred"] = "auto"
    market: Literal["gb"] | None = None
    symbol: None = None

    @model_validator(mode="before")
    @classmethod
    def _bind_default_series_id(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        bound_values = dict(values)
        if "series_id" not in bound_values:
            parameter = bound_values.get("parameter", "rate")
            if type(parameter) is str:
                binding = _SONIA_PARAMETER_MAP.get(parameter)
                if binding is not None:
                    bound_values["series_id"] = binding[0]
        return bound_values

    @field_validator("series_id")
    @classmethod
    def _validate_series_id_text(cls, value: str) -> str:
        """Keep the exact identifier without inheriting generic trimming."""
        return value

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
    def _validate_strict_query_date(cls, value: object) -> date_type | None:
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

    @model_validator(mode="after")
    def _validate_selector_binding(self) -> FredSoniaQuery:
        expected_series_id, _ = _SONIA_PARAMETER_MAP[self.parameter]
        if self.series_id != expected_series_id:
            raise ValueError("series_id must match parameter")
        return self
