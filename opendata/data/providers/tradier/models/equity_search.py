"""Tradier partial stock/security search fetcher."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.tradier import TradierEquitySearch
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.tradier.models._client import (
    TradierProviderError,
    TradierQueryLimitError,
    fetch_json,
    object_rows,
)


class TradierEquitySearchQuery(QueryParams):
    """Search query and explicit maximum result count."""

    source: Literal["tradier"] = "tradier"
    market: Literal["us"] = "us"
    query: str
    indexes: bool = False
    max_records: int = Field(default=1_000, ge=1, le=1_000, strict=True)

    @field_validator("query")
    @classmethod
    def _validate_query(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in normalized)
        ):
            raise ValueError("query must be non-empty printable text of at most 128 characters")
        return normalized

    @field_validator("indexes", mode="before")
    @classmethod
    def _require_bool_indexes(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("indexes must be a boolean")
        return value

    @model_validator(mode="after")
    def _reject_window(self) -> TradierEquitySearchQuery:
        if self.start_date is not None or self.end_date is not None:
            raise ValueError("security search is a catalog lookup and has no date window")
        return self


class TradierEquitySearchFetcher(Fetcher[TradierEquitySearchQuery, list[dict[str, Any]]]):
    """Fetch one complete, unpaginated security-search result."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EquitySearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="tradier_equity_search",
        period="snapshot",
        market="us",
        source="tradier",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TradierEquitySearchQuery:
        """Validate a search term before any key or transport access."""
        return TradierEquitySearchQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: TradierEquitySearchQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Request the complete single-page result; never silently truncate it."""
        document = fetch_json(
            "/markets/search",
            {"q": params.query, "indexes": str(params.indexes).lower()},
            ctx.timeout,
        )
        records = object_rows(document, "securities", "security")
        if len(records) > params.max_records:
            raise TradierQueryLimitError(
                "TRADIER_RECORD_LIMIT",
                maximum=params.max_records,
                requests_made=1,
            )
        return list(records)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: TradierEquitySearchQuery,
    ) -> FetchResult:
        """Validate each search record without inventing matches or pages."""
        del params
        rows: list[TradierEquitySearch] = []
        seen: set[tuple[str, str]] = set()
        try:
            for item in raw:
                record = TradierEquitySearch.model_validate(
                    {
                        "symbol": item.get("symbol"),
                        "exchange": item.get("exchange"),
                        "security_type": item.get("type"),
                        "description": item.get("description"),
                    }
                )
                key = (record.symbol.upper(), record.exchange.upper())
                if key in seen:
                    raise TradierProviderError("TRADIER_DUPLICATE_SEARCH_RESULT")
                seen.add(key)
                rows.append(record)
        except (ValidationError, TypeError, ValueError) as exc:
            if isinstance(exc, TradierProviderError):
                raise
            raise TradierProviderError("TRADIER_BAD_SHAPE") from None
        return tuple(rows)
