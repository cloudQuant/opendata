"""FRED's fixed-ID Federal Funds composite model."""

from __future__ import annotations

import math
import re
from datetime import date as date_type
from typing import Any, ClassVar, Literal

from pydantic import Field, StrictBool, ValidationError

from opendata.data.capability import Capability
from opendata.data.models.fred_iter2 import (
    FredFederalFundsRate,
    FredOvernightBankFundingRate,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models.series import FredSeriesFetcher, FredSeriesQuery
from opendata.data.providers.fred.models.treasury_rates import _FixedRateQuery

_DATE_TOKEN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
FederalFundsFrequency = Literal[
    "a",
    "q",
    "m",
    "w",
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
FederalFundsTransform = Literal["chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"]
_FED_FUNDS_SERIES: dict[str, str] = {
    "rate": "EFFR",
    "target_range_upper": "DFEDTARU",
    "target_range_lower": "DFEDTARL",
    "percentile_1": "EFFR1",
    "percentile_25": "EFFR25",
    "percentile_75": "EFFR75",
    "percentile_99": "EFFR99",
    "volume": "EFFRVOL",
    "intraday_low": "EFFRL",
    "intraday_high": "EFFRH",
    "standard_deviation": "EFFRSD",
}
_OVERNIGHT_BANK_FUNDING_SERIES: dict[str, str] = {
    "rate": "OBFR",
    "percentile_1": "OBFR1",
    "percentile_25": "OBFR25",
    "percentile_75": "OBFR75",
    "percentile_99": "OBFR99",
    "volume": "OBFRVOL",
}


class FederalFundsRateQuery(_FixedRateQuery):
    """Documented FRED FederalFundsRate query, within a bounded fan-out."""

    frequency: FederalFundsFrequency | None = None
    aggregation_method: Literal["avg", "sum", "eop"] = "avg"
    transform: FederalFundsTransform | None = None
    effr_only: StrictBool = False
    page_size: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_records: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=2, strict=True)


class FederalFundsRateFetcher(Fetcher[FederalFundsRateQuery, list[dict[str, Any]]]):
    """Join explicitly mapped FRED series on observation date."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FederalFundsRate"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_federal_funds_rate",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = FederalFundsRateQuery
    output_model = FredFederalFundsRate

    def transform_query(self, **kwargs: object) -> FederalFundsRateQuery:
        """Validate dates, revisions and fan-out bounds before source I/O."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: FederalFundsRateQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch EFFR and, unless disabled, its documented release series."""
        selected = {"rate": _FED_FUNDS_SERIES["rate"]}
        if not params.effr_only:
            selected.update(_FED_FUNDS_SERIES)
        if len(selected) * params.max_pages > 22:
            raise FredProviderError("FRED_BAD_QUERY") from None

        output: list[dict[str, Any]] = []
        try:
            for field_name, series_id in selected.items():
                values = params.model_dump(mode="python")
                observation_params = {
                    key: value
                    for key, value in values.items()
                    if key in FredSeriesQuery.model_fields
                }
                query = FredSeriesQuery.model_validate(
                    {
                        **observation_params,
                        "series_id": series_id,
                        "transform_units": params.transform or "lin",
                        "output_type": 1,
                        "frequency": params.frequency,
                        "aggregation_method": params.aggregation_method,
                    }
                )
                observations = FredSeriesFetcher().extract_data(query, ctx)
                output.extend(
                    {
                        "field_name": field_name,
                        "series_id": series_id,
                        "observation": observation,
                    }
                    for observation in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return output

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: FederalFundsRateQuery,
    ) -> FetchResult:
        """Merge aligned dates and retain each source revision privately."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        by_field: dict[str, dict[date_type, tuple[float | None, date_type, date_type]]] = {
            field_name: {} for field_name in _FED_FUNDS_SERIES
        }
        try:
            for wrapper in raw:
                if type(wrapper) is not dict or type(wrapper.get("observation")) is not dict:
                    raise ValueError("an observation wrapper must be an object")
                field_name = wrapper["field_name"]
                series_id = wrapper["series_id"]
                if type(field_name) is not str or _FED_FUNDS_SERIES.get(field_name) != series_id:
                    raise ValueError("unmapped Federal Funds source series")
                observation = wrapper["observation"]
                date = _parse_date(observation["date"])
                realtime_start = _parse_date(observation["realtime_start"])
                realtime_end = _parse_date(observation["realtime_end"])
                if realtime_start > realtime_end:
                    raise ValueError("real-time interval is reversed")
                if params.start_date is not None and date < params.start_date:
                    raise ValueError("observation precedes requested start date")
                if params.end_date is not None and date > params.end_date:
                    raise ValueError("observation follows requested end date")
                _validate_vintage(realtime_start, realtime_end, params)
                field_rows = by_field[field_name]
                if date in field_rows:
                    raise ValueError("multiple revisions cannot fit the date key")
                field_rows[date] = (
                    _parse_value(observation["value"]),
                    realtime_start,
                    realtime_end,
                )

            rows: list[FredFederalFundsRate] = []
            for date in sorted(by_field["rate"]):
                rate, rate_start, rate_end = by_field["rate"][date]
                if rate is None:
                    raise ValueError("EFFR requires a numeric rate")
                fields: dict[str, object] = {"date": date, "rate": rate}
                provenance: dict[str, tuple[date_type, date_type]] = {
                    _FED_FUNDS_SERIES["rate"]: (rate_start, rate_end)
                }
                for field_name, field_rows in by_field.items():
                    if field_name == "rate":
                        continue
                    selected_row = field_rows.get(date)
                    fields[field_name] = None if selected_row is None else selected_row[0]
                    if selected_row is not None:
                        provenance[_FED_FUNDS_SERIES[field_name]] = (
                            selected_row[1],
                            selected_row[2],
                        )
                row = FredFederalFundsRate.model_validate(fields)
                row._realtime_by_series = provenance
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        if params.sort_order == "desc":
            rows.reverse()
        return tuple(rows)


class OvernightBankFundingRateQuery(_FixedRateQuery):
    """Documented FRED OBFR query with bounded daily-rate aggregation."""

    frequency: FederalFundsFrequency | None = None
    aggregation_method: Literal["avg", "sum", "eop"] = "avg"
    transform: FederalFundsTransform | None = None
    page_size: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_records: int = Field(default=10_000, ge=1, le=10_000, strict=True)
    max_pages: int = Field(default=2, ge=1, le=2, strict=True)


class OvernightBankFundingRateFetcher(Fetcher[OvernightBankFundingRateQuery, list[dict[str, Any]]]):
    """Join FRED OBFR and the five published distribution/volume series."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "OvernightBankFundingRate"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_overnight_bank_funding_rate",
        period="1D",
        market="us",
        source="fred",
        verified=False,
    )
    query_model = OvernightBankFundingRateQuery
    output_model = FredOvernightBankFundingRate

    def transform_query(self, **kwargs: object) -> OvernightBankFundingRateQuery:
        """Validate dates, revisions and page bounds before network access."""
        return self.query_model.model_validate(kwargs)

    def extract_data(
        self,
        params: OvernightBankFundingRateQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read the fixed OBFR components via the bounded observations pager."""
        if len(_OVERNIGHT_BANK_FUNDING_SERIES) * params.max_pages > 12:
            raise FredProviderError("FRED_BAD_QUERY") from None
        output: list[dict[str, Any]] = []
        try:
            for field_name, series_id in _OVERNIGHT_BANK_FUNDING_SERIES.items():
                values = params.model_dump(mode="python")
                observation_params = {
                    key: value
                    for key, value in values.items()
                    if key in FredSeriesQuery.model_fields
                }
                query = FredSeriesQuery.model_validate(
                    {
                        **observation_params,
                        "series_id": series_id,
                        "transform_units": params.transform or "lin",
                        "output_type": 1,
                        "frequency": params.frequency,
                        "aggregation_method": params.aggregation_method,
                    }
                )
                observations = FredSeriesFetcher().extract_data(query, ctx)
                output.extend(
                    {
                        "field_name": field_name,
                        "series_id": series_id,
                        "observation": observation,
                    }
                    for observation in observations
                )
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_QUERY") from exc
        return output

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: OvernightBankFundingRateQuery,
    ) -> FetchResult:
        """Merge the primary OBFR dates and preserve source vintages privately."""
        if type(raw) is not list:
            raise FredProviderError("FRED_BAD_OBSERVATION")
        by_field: dict[str, dict[date_type, tuple[float | None, date_type, date_type]]] = {
            field_name: {} for field_name in _OVERNIGHT_BANK_FUNDING_SERIES
        }
        try:
            for wrapper in raw:
                if type(wrapper) is not dict or type(wrapper.get("observation")) is not dict:
                    raise ValueError("an observation wrapper must be an object")
                field_name = wrapper["field_name"]
                series_id = wrapper["series_id"]
                if (
                    type(field_name) is not str
                    or _OVERNIGHT_BANK_FUNDING_SERIES.get(field_name) != series_id
                ):
                    raise ValueError("unmapped OBFR source series")
                observation = wrapper["observation"]
                date = _parse_date(observation["date"])
                realtime_start = _parse_date(observation["realtime_start"])
                realtime_end = _parse_date(observation["realtime_end"])
                if realtime_start > realtime_end:
                    raise ValueError("real-time interval is reversed")
                if params.start_date is not None and date < params.start_date:
                    raise ValueError("observation precedes requested start date")
                if params.end_date is not None and date > params.end_date:
                    raise ValueError("observation follows requested end date")
                _validate_vintage(realtime_start, realtime_end, params)
                field_rows = by_field[field_name]
                if date in field_rows:
                    raise ValueError("multiple revisions cannot fit the date key")
                field_rows[date] = (
                    _parse_value(observation["value"]),
                    realtime_start,
                    realtime_end,
                )

            rows: list[FredOvernightBankFundingRate] = []
            for date in sorted(by_field["rate"]):
                rate, rate_start, rate_end = by_field["rate"][date]
                if rate is None:
                    raise ValueError("OBFR requires a numeric rate")
                fields: dict[str, object] = {"date": date, "rate": rate}
                provenance: dict[str, tuple[date_type, date_type]] = {
                    _OVERNIGHT_BANK_FUNDING_SERIES["rate"]: (rate_start, rate_end)
                }
                for field_name, field_rows in by_field.items():
                    if field_name == "rate":
                        continue
                    selected_row = field_rows.get(date)
                    fields[field_name] = None if selected_row is None else selected_row[0]
                    if selected_row is not None:
                        provenance[_OVERNIGHT_BANK_FUNDING_SERIES[field_name]] = (
                            selected_row[1],
                            selected_row[2],
                        )
                row = FredOvernightBankFundingRate.model_validate(fields)
                row._realtime_by_series = provenance
                rows.append(row)
        except (KeyError, TypeError, ValueError, OverflowError, ValidationError) as exc:
            raise FredProviderError("FRED_BAD_OBSERVATION") from exc
        if params.sort_order == "desc":
            rows.reverse()
        return tuple(rows)


def _parse_date(value: object) -> date_type:
    """Require a canonical FRED date without implicit string coercion."""
    if type(value) is not str or _DATE_TOKEN.fullmatch(value) is None:
        raise ValueError("observation date must be YYYY-MM-DD text")
    parsed = date_type.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("observation date must be canonical")
    return parsed


def _parse_value(value: object) -> float | None:
    """Map only FRED's documented dot sentinel to null."""
    if type(value) is not str:
        raise ValueError("FRED observation value must be text")
    if value == ".":
        return None
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("FRED observation value must be finite")
    return parsed


def _validate_vintage(
    realtime_start: date_type,
    realtime_end: date_type,
    params: _FixedRateQuery,
) -> None:
    """Ensure each component row represents the requested FRED vintage."""
    if params.as_of is not None and not realtime_start <= params.as_of <= realtime_end:
        raise ValueError("observation does not cover the requested as_of date")
    if params.realtime_start is not None and realtime_end < params.realtime_start:
        raise ValueError("observation ends before the requested real-time window")
    if params.realtime_end is not None and realtime_start > params.realtime_end:
        raise ValueError("observation starts after the requested real-time window")
    if params.vintage_dates is not None and not any(
        realtime_start <= vintage <= realtime_end for vintage in params.vintage_dates
    ):
        raise ValueError("observation does not cover a requested vintage date")


__all__ = (
    "FederalFundsRateFetcher",
    "FederalFundsRateQuery",
    "OvernightBankFundingRateFetcher",
    "OvernightBankFundingRateQuery",
    "FederalFundsFrequency",
    "FederalFundsTransform",
)
