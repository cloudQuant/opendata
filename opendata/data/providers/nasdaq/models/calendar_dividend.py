"""Nasdaq dividend-calendar fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqCalendarDividend
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    bounded_dates,
    build_query,
    dates_inclusive,
    optional_date,
    optional_float,
    required_date,
    required_text,
)


class CalendarDividendQuery(QueryParams):
    """Date-window query for the dividend calendar."""


class CalendarDividendFetcher(
    NasdaqFetcher[CalendarDividendQuery, tuple[tuple[date, Mapping[str, Any]], ...]]
):
    """Read each bounded calendar day, then validate and sort all dividend rows."""

    canonical_model: ClassVar[str] = "CalendarDividend"
    row_model: ClassVar[type[NasdaqCalendarDividend]] = NasdaqCalendarDividend
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_calendar_dividend",
        period="event",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarDividendQuery:
        """Reject unsupported fields and cap the requested calendar interval."""
        query = build_query(CalendarDividendQuery, kwargs, {"start_date", "end_date"})
        try:
            start, end = bounded_dates(query.start_date, query.end_date)
        except ValueError as error:
            raise ProviderEngineError(
                "NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date")
            ) from error
        return query.model_copy(update={"start_date": start, "end_date": end})

    def extract_data(
        self, params: CalendarDividendQuery, ctx: FetchContext
    ) -> tuple[tuple[date, Mapping[str, Any]], ...]:
        """Fetch one source response for each validated day, in stable order."""
        start, end = bounded_dates(params.start_date, params.end_date)
        return tuple(
            (day, self._json("/api/calendar/dividends", {"date": day.isoformat()}, ctx))
            for day in dates_inclusive(start, end)
        )

    def transform_data(
        self, raw: tuple[tuple[date, Mapping[str, Any]], ...], params: CalendarDividendQuery
    ) -> FetchResult:
        """Map dividend rows into the strict output model and sort by date and ticker."""
        del params
        rows: list[NasdaqCalendarDividend] = []
        for requested_day, document in raw:
            for item in self._rows_or_empty(document, "data", "calendar", "rows"):
                ex_date = required_date(item.get("dividend_Ex_Date"))
                # One response is requested per day; discard only valid out-of-window rows.
                if ex_date != requested_day:
                    continue
                rows.append(
                    self._typed(
                        NasdaqCalendarDividend,
                        {
                            "ex_dividend_date": ex_date,
                            "symbol": required_text(item.get("symbol")),
                            "amount": optional_float(item.get("dividend_Rate")),
                            "name": item.get("companyName"),
                            "record_date": optional_date(item.get("record_Date")),
                            "payment_date": optional_date(item.get("payment_Date")),
                            # Nasdaq publishes announcement_Date; it is not labeled a
                            # declaration date, so leave the separate contract field unset.
                            "declaration_date": None,
                            "annualized_amount": optional_float(
                                item.get("indicated_Annual_Dividend")
                            ),
                        },
                    )
                )
        rows.sort(key=lambda row: (row.ex_dividend_date, row.symbol))
        return tuple(rows)
