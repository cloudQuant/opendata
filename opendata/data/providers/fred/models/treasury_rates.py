"""Fixed-series Treasury rates exposed by the OpenBB FRED models."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.fred_iter2 import (
    FredSelectedTreasuryBill,
    FredSelectedTreasuryConstantMaturity,
    FredTreasuryConstantMaturity,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.series import FredSeriesFetcher, FredSeriesQuery

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _parse_strict_date(value: object) -> date_type:
    """Accept only date objects or canonical ISO date strings."""
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


class _FixedRateQuery(QueryParams):
    """Fixed Treasury-series options with explicit PIT and paging controls."""

    source: Literal["auto", "fred"] = "auto"
    market: str | None = None
    symbol: None = None
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    as_of: date_type | None = None
    vintage_dates: list[date_type] | None = None
    sort_order: Literal["asc", "desc"] = "asc"
    page_size: int = Field(default=100_000, ge=1, le=100_000, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    max_records: int = Field(default=200_000, ge=1, le=1_000_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=100, strict=True)

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
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("vintage_dates must be a non-empty ordered list of dates")
        if len(value) != 1:
            raise ValueError("the date/rate contract supports one vintage date per request")
        return [_parse_strict_date(item) for item in value]

    @model_validator(mode="after")
    def _validate_windows(self) -> _FixedRateQuery:
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
        has_realtime_window = self.realtime_start is not None or self.realtime_end is not None
        if self.as_of is not None and has_realtime_window:
            raise ValueError("as_of cannot be combined with a real-time window")
        if self.vintage_dates is not None and (self.as_of is not None or has_realtime_window):
            raise ValueError("vintage_dates cannot be combined with another real-time selector")
        return self


class _TreasuryFixedRateQuery(_FixedRateQuery):
    """Shared query shape for fixed Treasury maturities."""

    maturity: str


class TreasuryConstantMaturityQuery(_TreasuryFixedRateQuery):
    """Supported maturities for the Treasury constant-maturity endpoint."""

    market: Literal["us"] | None = None
    maturity: Literal["3m", "2y"] = "3m"


class SelectedTreasuryBillQuery(_TreasuryFixedRateQuery):
    """Supported maturities for the selected Treasury bill endpoint."""

    market: Literal["us"] | None = None
    maturity: Literal["3m", "6m"] = "3m"


class SelectedTreasuryConstantMaturityQuery(_TreasuryFixedRateQuery):
    """Supported maturities for the selected Treasury yield endpoint."""

    market: Literal["us"] | None = None
    maturity: Literal["10y", "5y", "1y", "6m", "3m"] = "10y"


class _FixedFredRateFetcher(Fetcher[_FixedRateQuery, list[dict[str, Any]]]):
    """Map a documented maturity choice to one fixed FRED series."""

    async_mode = "bounded_thread"
    query_model: ClassVar[type[_FixedRateQuery]]
    output_model: ClassVar[
        type[
            FredTreasuryConstantMaturity
            | FredSelectedTreasuryBill
            | FredSelectedTreasuryConstantMaturity
        ]
    ]
    series_by_maturity: ClassVar[dict[str, str]]

    def transform_query(self, **kwargs: object) -> _FixedRateQuery:
        """Validate all query parameters before any source request."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: _FixedRateQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Use the existing bounded FRED observations pager for the fixed ID."""
        if not isinstance(params, _TreasuryFixedRateQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        try:
            query_fields = params.model_dump(exclude={"maturity"})
            observations_query = FredSeriesQuery.model_validate(
                {
                    **query_fields,
                    "series_id": self.series_by_maturity[params.maturity],
                    "transform_units": "lin",
                    "output_type": 1,
                    "frequency": None,
                    "aggregation_method": "avg",
                }
            )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return FredSeriesFetcher().extract_data(observations_query, ctx)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: _FixedRateQuery,
    ) -> FetchResult:
        """Normalize strict source tokens without losing the selected vintage."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        if not isinstance(params, _TreasuryFixedRateQuery):
            raise FredProviderError("FRED_BAD_QUERY") from None
        series_id = self.series_by_maturity[params.maturity]
        rows: list[
            FredTreasuryConstantMaturity
            | FredSelectedTreasuryBill
            | FredSelectedTreasuryConstantMaturity
        ] = []
        seen_dates: set[date_type] = set()
        try:
            for item in raw:
                if type(item) is not dict:
                    raise ValueError("an observation must be a JSON object")
                observation_date = _parse_source_date(item["date"])
                realtime_start = _parse_source_date(item["realtime_start"])
                realtime_end = _parse_source_date(item["realtime_end"])
                if realtime_start > realtime_end:
                    raise ValueError("real-time interval is reversed")
                if params.start_date is not None and observation_date < params.start_date:
                    raise ValueError("observation precedes requested start date")
                if params.end_date is not None and observation_date > params.end_date:
                    raise ValueError("observation follows requested end date")
                self._validate_selected_vintage(realtime_start, realtime_end, params)
                if observation_date in seen_dates:
                    raise ValueError("multiple revisions cannot fit the date/rate output key")
                seen_dates.add(observation_date)
                row = self.output_model.model_validate(
                    {"date": observation_date, "rate": _parse_rate(item["value"])}
                )
                row._series_id = series_id
                row._maturity = params.maturity
                row._realtime_start = realtime_start
                row._realtime_end = realtime_end
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        return tuple(rows)

    @staticmethod
    def _validate_selected_vintage(
        realtime_start: date_type,
        realtime_end: date_type,
        params: _FixedRateQuery,
    ) -> None:
        if params.as_of is not None and not realtime_start <= params.as_of <= realtime_end:
            raise ValueError("observation does not cover the requested as_of date")
        if params.realtime_start is not None and realtime_end < params.realtime_start:
            raise ValueError("observation ends before the requested real-time window")
        if params.realtime_end is not None and realtime_start > params.realtime_end:
            raise ValueError("observation starts after the requested real-time window")
        if params.vintage_dates is not None and not any(
            realtime_start <= vintage_date <= realtime_end for vintage_date in params.vintage_dates
        ):
            raise ValueError("observation does not cover a requested vintage date")


class TreasuryConstantMaturityFetcher(_FixedFredRateFetcher):
    """FRED DGS3MO/DGS2 mapped to the OpenBB Treasury model."""

    canonical_model: ClassVar[str | None] = "TreasuryConstantMaturity"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_treasury_constant_maturity",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = TreasuryConstantMaturityQuery
    output_model = FredTreasuryConstantMaturity
    series_by_maturity = {"3m": "DGS3MO", "2y": "DGS2"}


class SelectedTreasuryBillFetcher(_FixedFredRateFetcher):
    """FRED DTB3/DTB6 mapped to the OpenBB selected-bill model."""

    canonical_model: ClassVar[str | None] = "SelectedTreasuryBill"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_selected_treasury_bill",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = SelectedTreasuryBillQuery
    output_model = FredSelectedTreasuryBill
    series_by_maturity = {"3m": "DTB3", "6m": "DTB6"}


class SelectedTreasuryConstantMaturityFetcher(_FixedFredRateFetcher):
    """FRED DGS10/DGS5/DGS1/DGS6MO/DGS3MO fixed maturity mapping."""

    canonical_model: ClassVar[str | None] = "SelectedTreasuryConstantMaturity"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_selected_treasury_constant_maturity",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = SelectedTreasuryConstantMaturityQuery
    output_model = FredSelectedTreasuryConstantMaturity
    series_by_maturity = {
        "10y": "DGS10",
        "5y": "DGS5",
        "1y": "DGS1",
        "6m": "DGS6MO",
        "3m": "DGS3MO",
    }


def _parse_source_date(value: object) -> date_type:
    if type(value) is not str:
        raise ValueError("FRED source date must be text")
    return _parse_strict_date(value)


def _parse_rate(raw: object) -> float | None:
    """Map only FRED's documented dot sentinel to missing."""
    if type(raw) is not str:
        raise ValueError("FRED rate token must be text")
    rate_text = raw.strip()
    if rate_text == ".":
        return None
    if not rate_text:
        raise ValueError("empty rate token is malformed")
    rate = float(rate_text)
    if not math.isfinite(rate):
        raise ValueError("rate must be finite")
    return rate


__all__ = (
    "SelectedTreasuryBillFetcher",
    "SelectedTreasuryBillQuery",
    "SelectedTreasuryConstantMaturityFetcher",
    "SelectedTreasuryConstantMaturityQuery",
    "TreasuryConstantMaturityFetcher",
    "TreasuryConstantMaturityQuery",
)
