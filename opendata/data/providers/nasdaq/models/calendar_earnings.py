"""Nasdaq earnings-calendar fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqCalendarEarnings
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    bounded_dates,
    build_query,
    dates_inclusive,
    optional_date,
    optional_float,
    optional_int,
    optional_text,
    required_date,
    required_text,
)


class CalendarEarningsQuery(QueryParams):
    """Date-window query for the earnings calendar."""


class CalendarEarningsFetcher(
    NasdaqFetcher[CalendarEarningsQuery, tuple[tuple[date, Mapping[str, Any]], ...]]
):
    """Read each bounded day and attach the response's as-of day to its rows."""

    canonical_model: ClassVar[str] = "CalendarEarnings"
    row_model: ClassVar[type[NasdaqCalendarEarnings]] = NasdaqCalendarEarnings
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="nasdaq_calendar_earnings",
        period="event",
        market="us",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarEarningsQuery:
        """Reject unsupported fields and cap the requested calendar interval."""
        query = build_query(CalendarEarningsQuery, kwargs, {"start_date", "end_date"})
        try:
            start, end = bounded_dates(query.start_date, query.end_date)
        except ValueError as error:
            raise ProviderEngineError(
                "NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date")
            ) from error
        return query.model_copy(update={"start_date": start, "end_date": end})

    def extract_data(
        self, params: CalendarEarningsQuery, ctx: FetchContext
    ) -> tuple[tuple[date, Mapping[str, Any]], ...]:
        """Fetch one source response for each validated day, in stable order."""
        start, end = bounded_dates(params.start_date, params.end_date)
        return tuple(
            (day, self._json("/api/calendar/earnings", {"date": day.isoformat()}, ctx))
            for day in dates_inclusive(start, end)
        )

    def transform_data(
        self, raw: tuple[tuple[date, Mapping[str, Any]], ...], params: CalendarEarningsQuery
    ) -> FetchResult:
        """Attach each response's as-of date and map the typed earnings fields."""
        del params
        output: list[NasdaqCalendarEarnings] = []
        for requested_day, document in raw:
            data = document.get("data")
            if not isinstance(data, Mapping):
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("data",))
            as_of = required_date(data.get("asOf"))
            if as_of != requested_day:
                raise ProviderEngineError("NASDAQ_SHAPE_INVALID", invalid=("asOf",))
            output.extend(
                self._typed(
                    NasdaqCalendarEarnings,
                    {
                        "report_date": as_of,
                        "symbol": required_text(item.get("symbol")),
                        "name": item.get("name"),
                        "eps_previous": optional_float(item.get("lastYearEPS")),
                        "eps_consensus": optional_float(item.get("epsForecast")),
                        # The observed Nasdaq earnings-calendar row does not include actual EPS
                        # or a surprise percentage.
                        "eps_actual": None,
                        "surprise_percent": None,
                        "num_estimates": optional_int(item.get("noOfEsts")),
                        "period_ending": item.get("fiscalQuarterEnding"),
                        "previous_report_date": optional_date(item.get("lastYearRptDt")),
                        "reporting_time": _reporting_time(item.get("time")),
                        "market_cap": optional_int(item.get("marketCap")),
                    },
                )
                for item in self._rows_or_empty(document, "data", "rows")
            )
        output.sort(key=lambda row: (row.report_date, row.symbol))
        return tuple(output)


def _reporting_time(value: object) -> str | None:
    """Normalize Nasdaq's explicit unsupplied-time sentinel to a missing value."""
    text = optional_text(value)
    return None if text and text.casefold() == "time-not-supplied" else text
