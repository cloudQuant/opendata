"""Nasdaq weekday economic-calendar fetcher."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from typing import Any, ClassVar

from opendata.data.capability import Capability
from opendata.data.models.nasdaq import NasdaqEconomicEvent
from opendata.data.protocol import FetchContext, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import ProviderEngineError
from opendata.data.providers.nasdaq.models._common import (
    NasdaqFetcher,
    bounded_dates,
    build_query,
    dates_inclusive,
    html_text,
    optional_text,
    parse_gmt_datetime,
)


class EconomicCalendarQuery(QueryParams):
    """Date-window and country selection for the economic calendar."""

    country: str | None = None


class EconomicCalendarFetcher(
    NasdaqFetcher[EconomicCalendarQuery, tuple[tuple[date, Mapping[str, Any]], ...]]
):
    """Fetch weekdays in a bounded range, synthesize UTC event timestamps, and filter country."""

    canonical_model: ClassVar[str] = "EconomicCalendar"
    row_model: ClassVar[type[NasdaqEconomicEvent]] = NasdaqEconomicEvent
    capability: ClassVar[Capability] = Capability(
        asset_class="economy",
        domain="nasdaq_economic_calendar",
        period="event",
        market="global",
        source="nasdaq",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EconomicCalendarQuery:
        """Validate the short date window and optional country filter before HTTP."""
        query = build_query(
            EconomicCalendarQuery,
            kwargs,
            {"start_date", "end_date", "country"},
            markets=frozenset({"us", "usa", "nasdaq", "united states", "global"}),
        )
        try:
            start, end = bounded_dates(query.start_date, query.end_date)
        except ValueError as error:
            raise ProviderEngineError(
                "NASDAQ_QUERY_INVALID", invalid=("start_date", "end_date")
            ) from error
        country = query.country.strip() if query.country is not None else None
        if country is not None and (not country or len(country) > 80):
            raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=("country",))
        return query.model_copy(update={"start_date": start, "end_date": end, "country": country})

    def extract_data(
        self, params: EconomicCalendarQuery, ctx: FetchContext
    ) -> tuple[tuple[date, Mapping[str, Any]], ...]:
        """Fetch weekdays only, bounded by the validated calendar range."""
        start, end = bounded_dates(params.start_date, params.end_date)
        weekdays = tuple(day for day in dates_inclusive(start, end) if day.weekday() < 5)
        return tuple(
            (
                day,
                self._json("/api/calendar/economicevents", {"date": day.isoformat()}, ctx),
            )
            for day in weekdays
        )

    def transform_data(
        self, raw: tuple[tuple[date, Mapping[str, Any]], ...], params: EconomicCalendarQuery
    ) -> FetchResult:
        """Synthesize UTC event times, strip descriptions, and apply country filtering."""
        output: list[NasdaqEconomicEvent] = []
        for requested_day, document in raw:
            data = document.get("data")
            if data is None and _is_no_record_response(document):
                continue
            for item in self._rows_or_empty(document, "data", "rows"):
                country = optional_text(item.get("country"))
                if params.country and (country or "").casefold() != params.country.casefold():
                    continue
                timestamp = parse_gmt_datetime(requested_day, item.get("gmt"))
                event_name = optional_text(item.get("eventName"))
                if timestamp is None and event_name is None:
                    raise ProviderEngineError("NASDAQ_SHAPE_INVALID", missing=("gmt", "eventName"))
                output.append(
                    self._typed(
                        NasdaqEconomicEvent,
                        {
                            "date": timestamp,
                            "country": country,
                            "category": None,
                            "event": event_name,
                            "importance": None,
                            "source": None,
                            "currency": None,
                            "unit": None,
                            "consensus": _economic_value(item.get("consensus")),
                            "previous": _economic_value(item.get("previous")),
                            "revised": None,
                            "actual": _economic_value(item.get("actual")),
                            "description": html_text(item.get("description")),
                        },
                    )
                )
        output.sort(
            key=lambda row: (
                _event_date_key(row.date),
                row.country or "",
                row.event or "",
            )
        )
        return tuple(output)


def _economic_value(value: object) -> str | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text or text.casefold() in {
            "n/a",
            "na",
            "null",
            "--",
            "-",
            "&nbsp;",
            "&#160;",
        }:
            return None
        return text
    raise ProviderEngineError("NASDAQ_SHAPE_INVALID")


def _event_date_key(value: datetime | str | None) -> str:
    """Keep the previous lexical sort key across timestamp, text, and null contract values."""
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _is_no_record_response(document: Mapping[str, Any]) -> bool:
    """Recognize Nasdaq's explicit 200/1002 no-record envelope as an empty date."""
    status = document.get("status")
    if not isinstance(status, Mapping) or status.get("rCode") != 200:
        return False
    messages = status.get("bCodeMessage")
    if isinstance(messages, Mapping):
        messages = [messages]
    if not isinstance(messages, list) or not messages:
        return False
    return all(
        isinstance(message, Mapping)
        and message.get("code") == 1002
        and "no record found" in str(message.get("errorMessage", "")).casefold()
        for message in messages
    )
