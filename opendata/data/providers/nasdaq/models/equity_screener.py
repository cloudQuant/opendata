"""Nasdaq equity screener fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from pydantic import Field, field_validator

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqEquityScreener
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    build_query,
    optional_float,
    optional_int,
    optional_text,
    required_float,
    required_text,
)

MAX_SCREENER_ROWS = 10_000
PAGE_SIZE = 100


class EquityScreenerQuery(QueryParams):
    """Bounded source-side filters supported by Nasdaq's stock screener route."""

    limit: int = Field(default=MAX_SCREENER_ROWS, ge=1, le=MAX_SCREENER_ROWS)
    exchange: str | None = None
    sector: str | None = None
    country: str | None = None
    market_cap: str | None = None

    @field_validator("exchange", "sector", "country", "market_cap")
    @classmethod
    def _safe_filter(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > 80):
            raise ValueError("filter must be non-blank and at most 80 characters")
        return value.strip() if value is not None else None


class EquityScreenerFetcher(NasdaqFetcher[EquityScreenerQuery, tuple[Mapping[str, Any], ...]]):
    """Read a bounded series of Nasdaq screener pages and validate typed rows."""

    canonical_model: ClassVar[str] = "EquityScreener"
    row_model: ClassVar[type[NasdaqEquityScreener]] = NasdaqEquityScreener
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_equity_screener",
        period="snapshot",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquityScreenerQuery:
        """Validate row count and bounded textual source filters."""
        return build_query(
            EquityScreenerQuery,
            kwargs,
            {"limit", "exchange", "sector", "country", "market_cap"},
        )

    def extract_data(
        self, params: EquityScreenerQuery, ctx: FetchContext
    ) -> tuple[Mapping[str, Any], ...]:
        """Encode official filter names and follow offsets up to the requested row bound."""
        filters: dict[str, str] = {}
        if params.exchange:
            filters["exchange"] = params.exchange.upper()
        if params.sector is not None:
            filters["sector"] = _source_filter(params.sector)
        if params.country is not None:
            filters["country"] = _source_filter(params.country)
        if params.market_cap is not None:
            filters["marketcap"] = _source_filter(params.market_cap)
        rows: list[Mapping[str, Any]] = []
        total_records: int | None = None
        target_rows: int | None = None
        offset = 0
        while target_rows is None or len(rows) < target_rows:
            page_limit = min(PAGE_SIZE, params.limit - len(rows))
            query = {"tableonly": "true", "limit": str(page_limit), "offset": str(offset)}
            query.update(filters)
            document = self._json("/api/screener/stocks", query, ctx)
            data = document.get("data")
            table = data.get("table") if isinstance(data, Mapping) else None
            if not isinstance(data, Mapping) or not isinstance(table, Mapping):
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("data.table",))
            current_total = data.get("totalrecords")
            if (
                isinstance(current_total, bool)
                or not isinstance(current_total, int)
                or current_total < 0
            ):
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("totalrecords",))
            if total_records is None:
                total_records = current_total
                target_rows = min(total_records, params.limit, MAX_SCREENER_ROWS)
            elif current_total != total_records:
                raise ProviderEngineError("NASDAQ_PAGINATION_INCONSISTENT")
            if target_rows is None:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
            if current_total == 0 and table.get("rows") is None:
                if "rows" not in table:
                    raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("rows",))
                page: list[Mapping[str, Any]] = []
            else:
                page = self._rows(document, "data", "table", "rows")
            if len(page) > page_limit or len(rows) + len(page) > target_rows:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
            rows.extend(page)
            if len(rows) == target_rows:
                return tuple(rows)
            if not page:
                raise ProviderEngineError("NASDAQ_INCOMPLETE")
            offset += len(page)
        return tuple(rows)

    def transform_data(
        self, raw: tuple[Mapping[str, Any], ...], params: EquityScreenerQuery
    ) -> FetchResult:
        """Validate screen rows, normalize percentage points, and sort deterministically."""
        if len(raw) > params.limit:
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        output = [
            self._typed(
                NasdaqEquityScreener,
                {
                    "symbol": required_text(item.get("symbol")),
                    "name": optional_text(item.get("name")),
                    "last_price": required_float(item.get("lastsale")),
                    "change": optional_float(item.get("netchange")),
                    "change_percent": _screener_percentage(item.get("pctchange")),
                    "market_cap": optional_int(item.get("marketCap")),
                },
            )
            for item in raw
        ]
        output.sort(
            key=lambda row: (
                -(row.change_percent if row.change_percent is not None else float("-inf")),
                row.symbol,
            )
        )
        return tuple(output)


def _source_filter(value: str) -> str:
    """Match the Nasdaq screen UI's value encoding (spaces become underscores)."""
    return value.replace(" ", "_")


def _screener_percentage(value: object) -> float | None:
    parsed = optional_float(value)
    return None if parsed is None else parsed / 100.0
