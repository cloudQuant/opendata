"""Strict FRED Euro Short-Term Rate query contracts and deterministic fan-out."""

from __future__ import annotations

import re
from datetime import date as date_type
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from opendata.data.models.economic_series import (
    FredFrequency,  # noqa: TC001 - Pydantic resolves query annotations at runtime.
    FredOutputType,  # noqa: TC001 - Pydantic resolves query annotations at runtime.
    FredTransformUnits,  # noqa: TC001 - Pydantic resolves query annotations at runtime.
)
from opendata.data.models.fred_estr import _ESTR_MEASURE_MAP
from opendata.data.protocol import QueryParams
from opendata.data.providers.fred.models.series import FredSeriesQuery

_EstrMeasure = Literal[
    "rate",
    "percentile_25",
    "percentile_75",
    "volume",
    "transactions",
    "number_of_banks",
    "large_bank_share_of_volume",
]
_ESTR_MEASURES: tuple[_EstrMeasure, ...] = (
    "rate",
    "percentile_25",
    "percentile_75",
    "volume",
    "transactions",
    "number_of_banks",
    "large_bank_share_of_volume",
)


def _parse_strict_date(value: object) -> date_type:
    """Accept only exact date objects or canonical YYYY-MM-DD strings."""
    if type(value) is date_type:
        return value
    if type(value) is not str or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ValueError("date values must be date objects or canonical YYYY-MM-DD strings")
    try:
        return date_type.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date values must be valid YYYY-MM-DD dates") from exc


def _parse_vintage_dates(value: object) -> list[date_type] | None:
    """Preserve vintage selector order and duplicates while validating each date."""
    if value is None:
        return None
    if not isinstance(value, (list, tuple)):
        raise ValueError("vintage_dates must be an ordered list of dates")
    if not value:
        raise ValueError("vintage_dates must not be empty when provided")
    if len(value) > 2000:
        raise ValueError("vintage_dates must contain at most 2000 dates")
    return [_parse_strict_date(item) for item in value]


def _validate_output_type(value: object) -> object:
    if type(value) is not int:
        raise ValueError("output_type must be an integer from 1 through 4")
    return value


class FredEuroShortTermRateQuery(QueryParams):
    """Complete multi-measure request with bounded result and page caps.

    Output types 1 through 4 are accepted request parameters; this does not
    claim that unverified FRED JSON shapes for output types 2 and 3 are known.
    The total caps limit the requested result and do not grant HTTP budget.
    """

    model_config = ConfigDict(revalidate_instances="always")

    source: Literal["auto", "fred"] = "auto"
    market: Literal["eu"] | None = None
    symbol: None = None
    measures: tuple[_EstrMeasure, ...] = _ESTR_MEASURES
    transform_units: FredTransformUnits = "lin"
    output_type: FredOutputType = 1
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    as_of: date_type | None = None
    vintage_dates: list[date_type] | None = None
    frequency: FredFrequency | None = None
    aggregation_method: Literal["avg", "sum", "eop"] = "avg"
    sort_order: Literal["asc", "desc"] = "asc"
    page_size: int = Field(default=100_000, ge=1, le=100_000, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    max_records: int = Field(default=200_000, ge=1, le=1_000_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=100, strict=True)
    max_total_records: int = Field(default=200_000, ge=1, le=1_000_000, strict=True)
    max_total_pages: int = Field(default=14, ge=1, le=64, strict=True)

    @field_validator("measures", mode="before")
    @classmethod
    def _validate_measures(cls, value: object) -> tuple[_EstrMeasure, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("measures must be an ordered list or tuple")
        if not value:
            raise ValueError("measures must not be empty")
        if any(type(item) is not str for item in value):
            raise ValueError("measures must contain supported measure names")
        if len(set(value)) != len(value):
            raise ValueError("measures must not contain duplicates")
        return tuple(value)

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
    def _validate_vintage_dates(cls, value: object) -> list[date_type] | None:
        return _parse_vintage_dates(value)

    @field_validator("output_type", mode="before")
    @classmethod
    def _require_exact_output_type_integer(cls, value: object) -> object:
        return _validate_output_type(value)

    @model_validator(mode="after")
    def _validate_date_windows(self) -> FredEuroShortTermRateQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must not be after end_date")
        if (
            self.realtime_start is not None
            and self.realtime_end is not None
            and self.realtime_start > self.realtime_end
        ):
            raise ValueError("realtime_start must not be after realtime_end")
        realtime_selector = self.realtime_start is not None or self.realtime_end is not None
        if self.as_of is not None and realtime_selector:
            raise ValueError("as_of cannot be combined with a real-time window")
        if self.vintage_dates is not None and (self.as_of is not None or realtime_selector):
            raise ValueError("vintage_dates cannot be combined with another real-time selector")
        return self


class FredEuroShortTermRateSeriesQuery(FredSeriesQuery):
    """One measure's bounded FRED request with exact measure-to-series binding."""

    model_config = ConfigDict(revalidate_instances="always")

    series_id: Literal[
        "ECBESTRVOLWGTTRMDMNRT",
        "ECBESTRRT25THPCTVOL",
        "ECBESTRRT75THPCTVOL",
        "ECBESTRTOTVOL",
        "ECBESTRNUMTRANS",
        "ECBESTRNUMACTBANKS",
        "ECBESTRSHRVOL5LRGACTBNK",
    ]
    measure: _EstrMeasure
    source: Literal["auto", "fred"] = "auto"
    market: Literal["eu"] | None = None
    symbol: None = None

    @model_validator(mode="before")
    @classmethod
    def _bind_omitted_series_id(cls, values: object) -> object:
        if not isinstance(values, dict):
            return values
        bound_values = dict(values)
        if "series_id" not in bound_values:
            measure = bound_values.get("measure")
            if type(measure) is str:
                binding = _ESTR_MEASURE_MAP.get(measure)
                if binding is not None:
                    bound_values["series_id"] = binding[0]
        return bound_values

    @field_validator("series_id")
    @classmethod
    def _validate_series_id_text(cls, value: str) -> str:
        """Do not inherit generic FRED trimming for canonical identifiers."""
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
    def _validate_vintage_dates(cls, value: object) -> list[date_type] | None:
        return _parse_vintage_dates(value)

    @field_validator("output_type", mode="before")
    @classmethod
    def _require_exact_output_type_integer(cls, value: object) -> object:
        return _validate_output_type(value)

    @model_validator(mode="after")
    def _validate_measure_binding(self) -> FredEuroShortTermRateSeriesQuery:
        expected_series_id, _ = _ESTR_MEASURE_MAP[self.measure]
        if self.series_id != expected_series_id:
            raise ValueError("series_id must match measure")
        return self


def expand_estr_queries(
    query: FredEuroShortTermRateQuery,
) -> tuple[FredEuroShortTermRateSeriesQuery, ...]:
    """Revalidate an exact top-level query and expand measures in request order."""
    if type(query) is not FredEuroShortTermRateQuery:
        raise TypeError("query must be an exact FredEuroShortTermRateQuery")

    validated = FredEuroShortTermRateQuery.model_validate(query.model_dump(mode="python"))
    shared_values = validated.model_dump(
        mode="python",
        exclude={"measures", "max_total_records", "max_total_pages"},
    )
    expanded: list[FredEuroShortTermRateSeriesQuery] = []
    for measure in validated.measures:
        series_values = dict(shared_values)
        series_values["measure"] = measure
        expanded.append(FredEuroShortTermRateSeriesQuery.model_validate(series_values))
    return tuple(expanded)
