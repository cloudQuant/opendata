"""Nasdaq Data Link RTAT10 top-retail fetcher."""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

from pydantic import Field

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqTopRetail
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq._source import NASDAQ_DATA_LINK_BASE_URL
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    build_query,
    data_link_api_key,
    data_link_rows,
    required_date,
    required_float,
    required_text,
)

MAX_TOP_RETAIL_DAYS = 31


class TopRetailQuery(QueryParams):
    """Retrieve the documented top-10 table for an inclusive date interval."""

    start_date: date | None = None
    end_date: date | None = None
    limit: int = Field(default=10, ge=1, le=10)


class TopRetailFetcher(NasdaqFetcher[TopRetailQuery, dict[str, object]]):
    """Query NDAQ/RTAT10 with the key only for this specific model."""

    canonical_model: ClassVar[str] = "TopRetail"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_top_retail",
        period="event",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TopRetailQuery:
        """Validate the API-specific key scope, top-ten limit, and short date interval."""
        query = build_query(TopRetailQuery, kwargs, {"start_date", "end_date", "limit"})
        start, end = _resolve_date_range(query.start_date, query.end_date)
        if start > end or (end - start).days + 1 > MAX_TOP_RETAIL_DAYS:
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date"))
        return query.model_copy(update={"start_date": start, "end_date": end})

    def extract_data(self, params: TopRetailQuery, ctx: FetchContext) -> dict[str, object]:
        """Query only the official RTAT10 table and add the key to this request."""
        if params.start_date is None or params.end_date is None:  # pragma: no cover
            raise ProviderEngineError("NASDAQ_QUERY_INVALID")
        query = {
            "date.gte": params.start_date.isoformat(),
            "date.lte": params.end_date.isoformat(),
            "per_page": "1000",
            "api_key": data_link_api_key(),
        }
        return dict(
            self._json(
                f"{NASDAQ_DATA_LINK_BASE_URL}/api/v3/datatables/NDAQ/RTAT10.json",
                query,
                ctx,
                headers=None,
            )
        )

    def transform_data(self, raw: dict[str, object], params: TopRetailQuery) -> FetchResult:
        """Validate positional table columns and select the latest matching top rows."""
        if params.start_date is None or params.end_date is None:  # pragma: no cover
            raise ProviderEngineError("NASDAQ_QUERY_INVALID")
        table = raw.get("datatable")
        if isinstance(table, dict):
            metadata = table.get("meta")
            if isinstance(metadata, dict) and metadata.get("next_cursor_id"):
                raise ProviderEngineError("NASDAQ_INCOMPLETE")
        source_rows = data_link_rows(
            raw,
            expected_columns=("date", "ticker", "activity", "sentiment"),
        )
        output: list[NasdaqTopRetail] = []
        for item in source_rows:
            row_date = required_date(item.get("date"))
            row = self._typed(
                NasdaqTopRetail,
                {
                    "date": row_date,
                    "symbol": required_text(item.get("ticker")),
                    "activity": required_float(item.get("activity")),
                    "sentiment": required_float(item.get("sentiment")),
                },
            )
            if row_date < params.start_date or row_date > params.end_date:
                continue
            output.append(row)
        if output:
            newest_day = max(row.date for row in output)
            output = [row for row in output if row.date == newest_day]
        output.sort(key=lambda row: (-row.activity, row.symbol))
        return tuple(output[: params.limit])


def _resolve_date_range(start: date | None, end: date | None) -> tuple[date, date]:
    today = date.today()
    if start is None and end is None:
        return today - timedelta(days=MAX_TOP_RETAIL_DAYS - 1), today
    return (start or end, end or start)  # type: ignore[return-value]
