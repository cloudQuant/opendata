"""Nasdaq IPO calendar fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, ClassVar, Literal

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqCalendarIpo
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    build_query,
    month_keys,
    optional_date,
    optional_float,
    optional_int,
    optional_text,
)

IpoStatus = Literal["upcoming", "priced", "filed", "withdrawn"]


class CalendarIpoQuery(QueryParams):
    """Month-bounded IPO query with an explicit response status branch."""

    status: IpoStatus = "upcoming"
    is_spo: bool = False


class CalendarIpoFetcher(
    NasdaqFetcher[CalendarIpoQuery, tuple[tuple[str, Mapping[str, Any]], ...]]
):
    """Fetch at most twelve month slices from the selected IPO status table."""

    canonical_model: ClassVar[str] = "CalendarIpo"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_calendar_ipo",
        period="event",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarIpoQuery:
        """Validate one status and normalize the bounded query to calendar months."""
        query = build_query(
            CalendarIpoQuery, kwargs, {"start_date", "end_date", "status", "is_spo"}
        )
        start = query.start_date or query.end_date or date.today()
        end = query.end_date or start
        try:
            month_keys(start, end)
        except ValueError as error:
            raise ProviderEngineError(
                "NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date")
            ) from error
        return query.model_copy(update={"start_date": start, "end_date": end})

    def extract_data(
        self, params: CalendarIpoQuery, ctx: FetchContext
    ) -> tuple[tuple[str, Mapping[str, Any]], ...]:
        """Fetch at most one response for each month in the validated window."""
        months = month_keys(params.start_date, params.end_date)
        return tuple(
            (
                month,
                self._json(
                    "/api/ipo/calendar",
                    {
                        "date": month,
                        **({"type": "spo"} if params.is_spo else {}),
                    },
                    ctx,
                ),
            )
            for month in months
        )

    def transform_data(
        self, raw: tuple[tuple[str, Mapping[str, Any]], ...], params: CalendarIpoQuery
    ) -> FetchResult:
        """Select the status-specific row table and validate each IPO output row."""
        rows: list[NasdaqCalendarIpo] = []
        for _, document in raw:
            if params.status == "upcoming":
                source_rows = self._rows_or_empty(
                    document, "data", "upcoming", "upcomingTable", "rows"
                )
            else:
                source_rows = self._rows_or_empty(document, "data", params.status, "rows")
            for item in source_rows:
                symbol = optional_text(item.get("proposedTickerSymbol") or item.get("symbol"))
                name = optional_text(item.get("companyName") or item.get("name"))
                priced_date = optional_date(item.get("pricedDate"))
                expected_price_date = optional_date(item.get("expectedPriceDate"))
                filed_date = optional_date(item.get("filedDate"))
                withdraw_date = optional_date(item.get("withdrawDate"))
                event_date = {
                    "upcoming": expected_price_date,
                    "priced": priced_date,
                    "filed": filed_date,
                    "withdrawn": withdraw_date,
                }[params.status]
                if symbol is None and name is None and event_date is None:
                    raise ProviderEngineError(
                        "NASDAQ_SHAPE_INVALID", missing=("symbol", "name", "status date")
                    )
                rows.append(
                    self._typed(
                        NasdaqCalendarIpo,
                        {
                            "symbol": symbol,
                            "ipo_date": priced_date,
                            "name": name,
                            "offer_amount": optional_float(item.get("dollarValueOfSharesOffered")),
                            "share_count": optional_int(item.get("sharesOffered")),
                            "expected_price_date": expected_price_date,
                            "filed_date": filed_date,
                            "withdraw_date": withdraw_date,
                            "deal_status": params.status,
                        },
                    )
                )
        filtered_rows: list[NasdaqCalendarIpo] = []
        for row in rows:
            event_date = (
                row.ipo_date or row.expected_price_date or row.filed_date or row.withdraw_date
            )
            if event_date is None or (
                (params.start_date is None or event_date >= params.start_date)
                and (params.end_date is None or event_date <= params.end_date)
            ):
                filtered_rows.append(row)
        rows = filtered_rows
        rows.sort(key=lambda row: (row.ipo_date or date.max, row.symbol or ""))
        return tuple(rows)
