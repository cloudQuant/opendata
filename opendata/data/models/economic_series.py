"""Typed contracts for FRED series catalog entries and observations."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime
from typing import Literal

from pydantic import Field, StrictInt, field_validator, model_validator

from opendata.data.models.base import ContractModel

FredTransformUnits = Literal["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"]
FredOutputType = Literal[1, 2, 3, 4]
FredFrequency = Literal[
    "d",
    "w",
    "bw",
    "m",
    "q",
    "sa",
    "a",
    "wef",
    "weth",
    "wew",
    "wetu",
    "wem",
    "wesu",
    "wesa",
    "bwew",
    "bwem",
]


class SeriesCatalogItem(ContractModel):
    """One FRED series returned by the series search endpoint."""

    series_id: str
    title: str
    frequency: str
    units: str
    seasonal_adjustment: str
    observation_start: date_type
    observation_end: date_type
    last_updated: datetime
    notes: str | None = None
    popularity: StrictInt | None = None

    @field_validator("series_id", "title", "frequency", "units", "seasonal_adjustment")
    @classmethod
    def _require_nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("last_updated")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("last_updated must include a timezone")
        return value

    @model_validator(mode="after")
    def _validate_observation_range(self) -> SeriesCatalogItem:
        if self.observation_start > self.observation_end:
            raise ValueError("observation_start must not be after observation_end")
        return self


class SeriesObservation(ContractModel):
    """One observation, revision interval, and request transformation context.

    ``requested_frequency`` records the requested aggregation frequency;
    ``None`` means it was unspecified, not that the source frequency is known.
    ``requested_aggregation_method`` records the query value and does not
    claim that FRED applied aggregation to a native-frequency response.
    """

    series_id: str
    date: date_type
    value: float | None
    realtime_start: date_type
    realtime_end: date_type
    transform_units: FredTransformUnits
    output_type: FredOutputType
    requested_frequency: FredFrequency | None = Field(
        description="Requested FRED frequency; null means the query did not specify one."
    )
    requested_aggregation_method: Literal["avg", "sum", "eop"] = Field(
        description="Aggregation method requested from FRED; records the query value."
    )

    @field_validator("series_id")
    @classmethod
    def _require_series_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("series_id must not be empty")
        return value

    @model_validator(mode="after")
    def _validate_realtime_range(self) -> SeriesObservation:
        if self.realtime_start > self.realtime_end:
            raise ValueError("realtime_start must not be after realtime_end")
        return self
