"""Generic FRED observations with explicit transformation and vintage semantics."""

from __future__ import annotations

import math
from datetime import (
    date as date_type,  # noqa: TC003 - Pydantic resolves this annotation at runtime.
)
from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models import FredOutputType, FredTransformUnits, SeriesObservation
from opendata.data.models.economic_series import FredFrequency  # noqa: TC001
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._paged import fetch_observation_pages


class FredSeriesQuery(QueryParams):
    """Complete observations query within explicit result and page caps."""

    series_id: str
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

    @field_validator("series_id")
    @classmethod
    def _validate_series_id_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("series_id must not be empty")
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("series_id must not contain control characters")
        return value

    @field_validator("vintage_dates")
    @classmethod
    def _require_nonempty_vintage_dates(
        cls, values: list[date_type] | None
    ) -> list[date_type] | None:
        if values is not None and not values:
            raise ValueError("vintage_dates must not be empty when provided")
        return values

    @model_validator(mode="after")
    def _validate_date_windows(self) -> FredSeriesQuery:
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


class FredSeriesFetcher(Fetcher[FredSeriesQuery, list[dict[str, Any]]]):
    """Fetch observations while retaining FRED's page and revision order.

    Normalization requires the known JSON row fields (date, value, and
    real-time interval). The JSON wire shape for output types 2 and 3 remains
    unverified; alternate shapes fail validation instead of being inferred.
    """

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FredSeries"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_series",
        period="variable",
        market="us",
        source="fred",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FredSeriesQuery:
        """Validate observation, transformation, vintage, and page options."""
        return FredSeriesQuery.model_validate(kwargs)

    def extract_data(self, params: FredSeriesQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the complete observation result from ``offset`` within caps."""
        api_params = {
            "series_id": params.series_id,
            "units": params.transform_units,
            "output_type": str(params.output_type),
            "sort_order": params.sort_order,
            "aggregation_method": params.aggregation_method,
        }
        if params.frequency is not None:
            api_params["frequency"] = params.frequency
        if params.vintage_dates is not None:
            api_params["vintage_dates"] = ",".join(
                vintage_date.isoformat() for vintage_date in params.vintage_dates
            )
        if params.start_date is not None:
            api_params["observation_start"] = params.start_date.isoformat()
        if params.end_date is not None:
            api_params["observation_end"] = params.end_date.isoformat()
        if params.as_of is not None:
            api_params["realtime_start"] = params.as_of.isoformat()
            api_params["realtime_end"] = params.as_of.isoformat()
        else:
            if params.realtime_start is not None:
                api_params["realtime_start"] = params.realtime_start.isoformat()
            if params.realtime_end is not None:
                api_params["realtime_end"] = params.realtime_end.isoformat()
        return fetch_observation_pages(
            params=api_params,
            offset=params.offset,
            page_size=params.page_size,
            max_records=params.max_records,
            max_pages=params.max_pages,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, Any]], params: FredSeriesQuery) -> FetchResult:
        """Normalize observations without collapsing distinct real-time rows."""
        rows = [_normalize_observation(item, params) for item in raw]
        return tuple(rows)


def _normalize_observation(item: dict[str, Any], params: FredSeriesQuery) -> SeriesObservation:
    """Validate and map one raw observation, including its real-time interval."""
    try:
        value = _parse_observation_value(item["value"])
        record: dict[str, object] = {
            "series_id": params.series_id,
            "date": item["date"],
            "value": value,
            "realtime_start": item["realtime_start"],
            "realtime_end": item["realtime_end"],
            "transform_units": params.transform_units,
            "output_type": params.output_type,
            "requested_frequency": params.frequency,
            "requested_aggregation_method": params.aggregation_method,
        }
        return SeriesObservation.model_validate(record)
    except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
        raise FredProviderError("FRED_BAD_OBSERVATION") from exc


def _parse_observation_value(raw: object) -> float | None:
    """Map only FRED's documented dot sentinel to a nullable value."""
    if not isinstance(raw, str):
        raise ValueError("FRED observation value must be a string")
    value_text = raw.strip()
    if value_text == ".":
        return None
    if not value_text:
        raise ValueError("empty observation value is malformed")
    value = float(value_text)
    if not math.isfinite(value):
        raise ValueError("observation value must be finite")
    return value
