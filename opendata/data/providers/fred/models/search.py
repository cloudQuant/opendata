"""Generic, bounded FRED series catalog search."""

from __future__ import annotations

import re
from datetime import date as date_type  # noqa: TC003 - Pydantic resolves this at runtime.
from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models import SeriesCatalogItem
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fred.models._client import FredProviderError
from opendata.data.providers.fred.models._paged import fetch_series_search_pages


class FredSearchQuery(QueryParams):
    """Complete catalog query, paged from ``offset`` within explicit caps."""

    search_text: str
    search_type: Literal["full_text", "series_id"] = "full_text"
    realtime_start: date_type | None = None
    realtime_end: date_type | None = None
    filter_variable: Literal["frequency", "units", "seasonal_adjustment"] | None = None
    filter_value: str | None = None
    tag_names: list[str] | None = None
    exclude_tag_names: list[str] | None = None
    page_size: int = Field(default=1000, ge=1, le=1000, strict=True)
    offset: int = Field(default=0, ge=0, strict=True)
    order_by: Literal[
        "search_rank",
        "series_id",
        "title",
        "units",
        "frequency",
        "seasonal_adjustment",
        "last_updated",
        "observation_start",
        "observation_end",
        "popularity",
    ] = "search_rank"
    sort_order: Literal["asc", "desc"] = "desc"
    max_records: int = Field(default=10_000, ge=1, le=100_000, strict=True)
    max_pages: int = Field(default=10, ge=1, le=100, strict=True)

    @field_validator("search_text")
    @classmethod
    def _require_search_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("search_text must not be empty")
        return value

    @field_validator("filter_value")
    @classmethod
    def _require_nonempty_filter_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("filter_value must not be empty")
        return value

    @field_validator("tag_names", "exclude_tag_names")
    @classmethod
    def _validate_tag_names(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        if not values:
            raise ValueError("tag lists must not be empty")
        cleaned: list[str] = []
        for value in values:
            tag = value.strip()
            if not tag or ";" in tag:
                raise ValueError("tag names must be nonempty and exclude separators")
            cleaned.append(tag)
        return cleaned

    @model_validator(mode="after")
    def _validate_search_filters(self) -> FredSearchQuery:
        if (
            self.realtime_start is not None
            and self.realtime_end is not None
            and self.realtime_start > self.realtime_end
        ):
            raise ValueError("realtime_start must not be after realtime_end")
        if self.exclude_tag_names is not None and self.tag_names is None:
            raise ValueError("exclude_tag_names requires tag_names")
        return self


class FredSearchFetcher(Fetcher[FredSearchQuery, list[dict[str, Any]]]):
    """Search the generic FRED series catalog and return typed catalog rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "FredSearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fred_search",
        period="snapshot",
        market="us",
        source="fred",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> FredSearchQuery:
        """Validate a full catalog query before any API key or transport use."""
        return FredSearchQuery.model_validate(kwargs)

    def extract_data(self, params: FredSearchQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Fetch the complete result set from ``offset`` within declared caps."""
        return fetch_series_search_pages(
            params={
                "search_text": params.search_text,
                "search_type": params.search_type,
                "order_by": params.order_by,
                "sort_order": params.sort_order,
                **_optional_search_params(params),
            },
            offset=params.offset,
            page_size=params.page_size,
            max_records=params.max_records,
            max_pages=params.max_pages,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, Any]], params: FredSearchQuery) -> FetchResult:
        """Normalize FRED catalog fields into the shared typed contract."""
        del params
        return tuple(_normalize_catalog_item(item) for item in raw)


def _normalize_catalog_item(item: dict[str, Any]) -> SeriesCatalogItem:
    """Validate and map one raw FRED catalog entry."""
    try:
        record: dict[str, object] = {
            "series_id": item["id"],
            "title": item["title"],
            "frequency": item["frequency"],
            "units": item["units"],
            "seasonal_adjustment": item["seasonal_adjustment"],
            "observation_start": item["observation_start"],
            "observation_end": item["observation_end"],
            "last_updated": _timezone_aware_timestamp(item["last_updated"]),
            "notes": item.get("notes"),
            "popularity": item.get("popularity"),
        }
        return SeriesCatalogItem.model_validate(record)
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise FredProviderError("FRED_BAD_SERIES") from exc


def _optional_search_params(params: FredSearchQuery) -> dict[str, str]:
    """Serialize optional FRED search filters without inventing pair rules."""
    serialized: dict[str, str] = {}
    if params.realtime_start is not None:
        serialized["realtime_start"] = params.realtime_start.isoformat()
    if params.realtime_end is not None:
        serialized["realtime_end"] = params.realtime_end.isoformat()
    if params.filter_variable is not None:
        serialized["filter_variable"] = params.filter_variable
    if params.filter_value is not None:
        serialized["filter_value"] = params.filter_value
    if params.tag_names is not None:
        serialized["tag_names"] = ";".join(params.tag_names)
    if params.exclude_tag_names is not None:
        serialized["exclude_tag_names"] = ";".join(params.exclude_tag_names)
    return serialized


def _timezone_aware_timestamp(value: object) -> object:
    """Normalize FRED's compact numeric UTC offset for Pydantic parsing."""
    if not isinstance(value, str):
        return value
    timestamp = value.strip()
    return re.sub(r"([+-]\d{2})$", r"\1:00", timestamp)
