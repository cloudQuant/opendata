"""Strict FMP index directory and paged calendar fetchers for Iteration 2."""

from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any, ClassVar, Literal, TypeVar

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.base import ContractModel
from opendata.data.models.fmp_iter2 import (
    AvailableIndex,
    DividendCalendarEvent,
    EarningsCalendarEvent,
    EconomicCalendarEvent,
    FmpCalendarEvent,
    IpoCalendarEvent,
    MarketRiskPremium,
    SplitsCalendarEvent,
    TreasuryRateObservation,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.fmp.models._client import (
    FMPProviderError,
    FMPQueryLimitError,
    fetch_array,
)

FMP_CALENDAR_PAGE_LIMIT = 4_000
FMP_CALENDAR_MAX_DAYS = 90
_MAX_REQUESTS = 64
_MAX_RECORDS = 200_000
ModelT = TypeVar("ModelT", bound=ContractModel)


class AvailableIndicesQuery(QueryParams):
    """A global index directory query with an explicit local result bound."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    max_records: int = Field(default=20_000, ge=1, le=_MAX_RECORDS, strict=True)

    @model_validator(mode="after")
    def _reject_unsupported_filters(self) -> AvailableIndicesQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("the index directory does not accept symbol or date filters")
        return self


class CalendarQuery(QueryParams):
    """A bounded, explicit date range for the FMP calendar routes."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    start_date: date_type
    end_date: date_type
    max_requests: int = Field(default=8, ge=1, le=_MAX_REQUESTS, strict=True)
    max_records: int = Field(default=20_000, ge=1, le=_MAX_RECORDS, strict=True)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_iso_date(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("date bounds must not include a time")
        if isinstance(value, date_type):
            return value
        if not isinstance(value, str):
            raise ValueError("date bounds must be ISO dates")
        try:
            parsed = date_type.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("date bounds must be ISO dates") from exc
        if parsed.isoformat() != value:
            raise ValueError("date bounds must be ISO dates")
        return parsed

    @model_validator(mode="after")
    def _validate_window(self) -> CalendarQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days > FMP_CALENDAR_MAX_DAYS:
            raise ValueError("calendar date range must not exceed 90 days")
        if self.symbol is not None:
            raise ValueError("market-wide calendar queries do not accept a symbol")
        return self


class CalendarDividendQuery(CalendarQuery):
    """FMP dividend calendar query."""


class CalendarEarningsQuery(CalendarQuery):
    """FMP earnings calendar query; optional report-time expansion is not enabled."""


class CalendarEventsQuery(QueryParams):
    """A bounded aggregate of FMP company events for OpenBB's optional window.

    FMP requires explicit ``from``/``to`` dates. When callers omit either
    bound, the adapter completes a maximum 90-day window: the next 90 days
    when both are omitted, or 90 days on the missing side of one supplied
    bound.
    """

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    start_date: date_type | None = None
    end_date: date_type | None = None
    max_requests: int = Field(default=16, ge=4, le=_MAX_REQUESTS, strict=True)
    max_records: int = Field(default=20_000, ge=1, le=_MAX_RECORDS, strict=True)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_optional_iso_date(cls, value: object) -> object:
        if value is None:
            return None
        return CalendarQuery._validate_iso_date(value)

    @model_validator(mode="after")
    def _complete_bounded_window(self) -> CalendarEventsQuery:
        start_date = self.start_date
        end_date = self.end_date
        if start_date is None and end_date is None:
            start_date = datetime.now(timezone.utc).date()
            end_date = start_date + timedelta(days=FMP_CALENDAR_MAX_DAYS)
        elif start_date is None:
            if end_date is None:
                raise ValueError("start_date and end_date must be provided together")
            start_date = end_date - timedelta(days=FMP_CALENDAR_MAX_DAYS)
        elif end_date is None:
            end_date = start_date + timedelta(days=FMP_CALENDAR_MAX_DAYS)

        if start_date > end_date:
            raise ValueError("start_date must not be after end_date")
        if (end_date - start_date).days > FMP_CALENDAR_MAX_DAYS:
            raise ValueError("calendar date range must not exceed 90 days")
        if self.symbol is not None:
            raise ValueError("market-wide calendar queries do not accept a symbol")

        self.start_date = start_date
        self.end_date = end_date
        return self


class EconomicCalendarQuery(CalendarQuery):
    """FMP economic release calendar query; timestamps in results are UTC."""

    country: str | None = None

    @field_validator("country")
    @classmethod
    def _validate_country(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if not 2 <= len(normalized) <= 3 or not normalized.isascii() or not normalized.isalpha():
            raise ValueError("country must be a two- or three-letter code")
        return normalized


class TreasuryRatesQuery(QueryParams):
    """Bounded U.S. Treasury rates query matching FMP's documented date filters."""

    source: Literal["fmp"] = "fmp"
    market: Literal["us"] = "us"
    start_date: date_type
    end_date: date_type
    max_records: int = Field(default=20_000, ge=1, le=_MAX_RECORDS, strict=True)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_iso_date(cls, value: object) -> object:
        return CalendarQuery._validate_iso_date(value)

    @model_validator(mode="after")
    def _validate_window(self) -> TreasuryRatesQuery:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        if (self.end_date - self.start_date).days > FMP_CALENDAR_MAX_DAYS:
            raise ValueError("date range must not exceed 90 days")
        if self.symbol is not None:
            raise ValueError("treasury rates do not accept a symbol")
        return self


class MarketRiskPremiumQuery(QueryParams):
    """Market-wide country risk premium snapshot with a local result bound."""

    source: Literal["fmp"] = "fmp"
    market: Literal["global"] = "global"
    max_records: int = Field(default=20_000, ge=1, le=_MAX_RECORDS, strict=True)

    @model_validator(mode="after")
    def _reject_unsupported_filters(self) -> MarketRiskPremiumQuery:
        if self.symbol is not None or self.start_date is not None or self.end_date is not None:
            raise ValueError("market risk premium snapshot does not accept symbol or date filters")
        return self


class AvailableIndicesFetcher(Fetcher[AvailableIndicesQuery, list[dict[str, Any]]]):
    """Fetch and strictly validate the global stock market indexes list."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "AvailableIndices"
    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="fmp_index_directory",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> AvailableIndicesQuery:
        """Reject unsupported filters before the shared client or key is read."""
        return AvailableIndicesQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: AvailableIndicesQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read the documented FMP stable index list route."""
        return fetch_array("index-list", {}, ctx.timeout)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: AvailableIndicesQuery,
    ) -> FetchResult:
        """Return only complete rows matching the official index-list shape."""
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=1,
            )
        records: list[AvailableIndex] = []
        seen: set[tuple[str, str]] = set()
        for item in raw:
            record = _validate_record(AvailableIndex, item)
            key = (record.symbol, record.exchange)
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_INDEX")
            seen.add(key)
            records.append(record)
        return tuple(records)


class CalendarDividendFetcher(Fetcher[CalendarDividendQuery, list[dict[str, Any]]]):
    """Fetch every page of a bounded FMP dividends calendar window."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarDividend"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_dividend_calendar",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarDividendQuery:
        """Reject invalid windows and budgets before network access."""
        return CalendarDividendQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CalendarDividendQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Page until an empty response marks the selected window's tail."""
        return _fetch_calendar_pages(
            endpoint="dividends-calendar",
            params=params,
            timeout=ctx.timeout,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CalendarDividendQuery,
    ) -> FetchResult:
        """Reject malformed or out-of-window source rows without returning partial output."""
        records: list[DividendCalendarEvent] = []
        for item in raw:
            record = _validate_record(DividendCalendarEvent, item)
            if not params.start_date <= record.date <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            records.append(record)
        return tuple(records)


class CalendarEarningsFetcher(Fetcher[CalendarEarningsQuery, list[dict[str, Any]]]):
    """Fetch every page of a bounded FMP earnings calendar window."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarEarnings"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_earnings_calendar",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarEarningsQuery:
        """Reject invalid windows and budgets before network access."""
        return CalendarEarningsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CalendarEarningsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Use the documented basic response shape; report-time expansion stays disabled."""
        return _fetch_calendar_pages(
            endpoint="earnings-calendar",
            params=params,
            timeout=ctx.timeout,
            extra_params={"includeReportTimes": "false"},
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: CalendarEarningsQuery,
    ) -> FetchResult:
        """Reject malformed or out-of-window source rows without returning partial output."""
        records: list[EarningsCalendarEvent] = []
        for item in raw:
            record = _validate_record(EarningsCalendarEvent, item)
            if not params.start_date <= record.date <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            records.append(record)
        return tuple(records)


class CalendarEventsFetcher(Fetcher[CalendarEventsQuery, list[tuple[str, ContractModel]]]):
    """Aggregate documented FMP company calendars into date/symbol rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarEvents"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_calendar_events",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarEventsQuery:
        """Validate and complete the optional public query window before I/O."""
        return CalendarEventsQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: CalendarEventsQuery,
        ctx: FetchContext,
    ) -> list[tuple[str, ContractModel]]:
        """Read each official company-event calendar within one shared request budget."""
        if params.start_date is None or params.end_date is None:
            raise FMPProviderError("FMP_INVALID_QUERY")
        return _fetch_calendar_event_rows(
            params=params,
            timeout=ctx.timeout,
        )

    def transform_data(
        self,
        raw: list[tuple[str, ContractModel]],
        params: CalendarEventsQuery,
    ) -> FetchResult:
        """Validate full source rows and project the public date/symbol contract."""
        source_models: dict[str, type[ContractModel]] = {
            "earnings": EarningsCalendarEvent,
            "dividends": DividendCalendarEvent,
            "ipos": IpoCalendarEvent,
            "splits": SplitsCalendarEvent,
        }
        if len(raw) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=0,
            )
        if params.start_date is None or params.end_date is None:
            raise FMPProviderError("FMP_INVALID_QUERY")
        events: dict[tuple[date_type, str], FmpCalendarEvent] = {}
        for source_name, source_event in raw:
            source_model = source_models.get(source_name)
            if source_model is None or not isinstance(source_event, source_model):
                raise FMPProviderError("FMP_BAD_SHAPE")
            if not isinstance(
                source_event,
                (
                    EarningsCalendarEvent,
                    DividendCalendarEvent,
                    IpoCalendarEvent,
                    SplitsCalendarEvent,
                ),
            ):
                raise FMPProviderError("FMP_BAD_SHAPE")
            _ensure_date_in_window(source_event.date, params.start_date, params.end_date)
            event = _validate_record(
                FmpCalendarEvent,
                {"date": source_event.date, "symbol": source_event.symbol},
            )
            events.setdefault((event.date, event.symbol), event)
        return tuple(events[key] for key in sorted(events))


class CalendarIpoFetcher(Fetcher[CalendarQuery, list[dict[str, Any]]]):
    """Fetch the documented market-wide IPO calendar for a bounded date window."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarIpo"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_ipo_calendar",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarQuery:
        """Validate required date window and local result bounds."""
        return CalendarQuery.model_validate(kwargs)

    def extract_data(self, params: CalendarQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read the official stable IPO route with documented date filters."""
        return fetch_array(
            "ipos-calendar",
            {"from": params.start_date.isoformat(), "to": params.end_date.isoformat()},
            ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, Any]], params: CalendarQuery) -> FetchResult:
        """Validate each IPO row and reject incomplete or duplicate results."""
        _check_unpaged_record_limit(raw, params.max_records)
        records: list[IpoCalendarEvent] = []
        seen: set[tuple[str, date_type]] = set()
        for item in raw:
            record = _validate_record(IpoCalendarEvent, item)
            _ensure_date_in_window(record.date, params.start_date, params.end_date)
            key = (record.symbol, record.date)
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_IPO")
            seen.add(key)
            records.append(record)
        return tuple(records)


class CalendarSplitsFetcher(Fetcher[CalendarQuery, list[dict[str, Any]]]):
    """Fetch every documented zero-based page of stock split events."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarSplits"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="fmp_splits_calendar",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CalendarQuery:
        """Validate the split calendar date window and paging budgets."""
        return CalendarQuery.model_validate(kwargs)

    def extract_data(self, params: CalendarQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read bounded zero-based pages until FMP returns an empty array."""
        return _fetch_calendar_pages(
            endpoint="splits-calendar",
            params=params,
            timeout=ctx.timeout,
        )

    def transform_data(self, raw: list[dict[str, Any]], params: CalendarQuery) -> FetchResult:
        """Validate each source row and reject out-of-window or duplicate events."""
        records: list[SplitsCalendarEvent] = []
        seen: set[tuple[str, date_type, int, int, str]] = set()
        for item in raw:
            record = _validate_record(SplitsCalendarEvent, item)
            _ensure_date_in_window(record.date, params.start_date, params.end_date)
            key = (
                record.symbol,
                record.date,
                record.numerator,
                record.denominator,
                record.split_type,
            )
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_SPLIT")
            seen.add(key)
            records.append(record)
        return tuple(records)


class EconomicCalendarFetcher(Fetcher[EconomicCalendarQuery, list[dict[str, Any]]]):
    """Fetch the FMP economic calendar; source timestamps are documented as UTC."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "EconomicCalendar"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fmp_economic_calendar",
        period="event",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EconomicCalendarQuery:
        """Validate date bounds and normalize the optional country code."""
        return EconomicCalendarQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: EconomicCalendarQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Read one bounded calendar response using only documented filters."""
        query = {"from": params.start_date.isoformat(), "to": params.end_date.isoformat()}
        if params.country is not None:
            query["country"] = params.country
        return fetch_array("economic-calendar", query, ctx.timeout)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: EconomicCalendarQuery,
    ) -> FetchResult:
        """Validate UTC event times, requested dates, and optional country scope."""
        _check_unpaged_record_limit(raw, params.max_records)
        records: list[EconomicCalendarEvent] = []
        seen: set[tuple[datetime, str, str]] = set()
        for item in raw:
            record = _validate_record(EconomicCalendarEvent, item)
            if not params.start_date <= record.date.date() <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            if params.country is not None and record.country.upper() != params.country:
                raise FMPProviderError("FMP_COUNTRY_FILTER_MISMATCH")
            key = (record.date, record.country, record.event)
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_ECONOMIC_EVENT")
            seen.add(key)
            records.append(record)
        return tuple(records)


class TreasuryRatesFetcher(Fetcher[TreasuryRatesQuery, list[dict[str, Any]]]):
    """Fetch a bounded U.S. Treasury rates range from the stable FMP route."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "TreasuryRates"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fmp_treasury_rates",
        period="daily",
        market="us",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> TreasuryRatesQuery:
        """Validate the U.S. market identity, date window, and result bound."""
        return TreasuryRatesQuery.model_validate(kwargs)

    def extract_data(self, params: TreasuryRatesQuery, ctx: FetchContext) -> list[dict[str, Any]]:
        """Read the stable Treasury rates route with its required from/to dates."""
        return fetch_array(
            "treasury-rates",
            {"from": params.start_date.isoformat(), "to": params.end_date.isoformat()},
            ctx.timeout,
        )

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: TreasuryRatesQuery,
    ) -> FetchResult:
        """Validate maturity fields and ensure every date is inside the query."""
        _check_unpaged_record_limit(raw, params.max_records)
        records: list[TreasuryRateObservation] = []
        seen: set[date_type] = set()
        for item in raw:
            record = _validate_record(TreasuryRateObservation, item)
            if not params.start_date <= record.date <= params.end_date:
                raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")
            if record.date in seen:
                raise FMPProviderError("FMP_DUPLICATE_TREASURY_DATE")
            seen.add(record.date)
            records.append(record)
        return tuple(records)


class MarketRiskPremiumFetcher(Fetcher[MarketRiskPremiumQuery, list[dict[str, Any]]]):
    """Fetch FMP's unfiltered country-level market risk premium snapshot."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "RiskPremium"
    capability: ClassVar[Capability] = Capability(
        asset_class="macro",
        domain="fmp_market_risk_premium",
        period="snapshot",
        market="global",
        source="fmp",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> MarketRiskPremiumQuery:
        """Reject unsupported symbol/date filters before source I/O."""
        return MarketRiskPremiumQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: MarketRiskPremiumQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Fetch the all-country snapshot without inventing provider filters."""
        del params
        return fetch_array("market-risk-premium", {}, ctx.timeout)

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: MarketRiskPremiumQuery,
    ) -> FetchResult:
        """Validate the risk fields and reject duplicate country rows."""
        _check_unpaged_record_limit(raw, params.max_records)
        records: list[MarketRiskPremium] = []
        seen: set[str] = set()
        for item in raw:
            record = _validate_record(MarketRiskPremium, item)
            key = record.country.casefold()
            if key in seen:
                raise FMPProviderError("FMP_DUPLICATE_RISK_PREMIUM_COUNTRY")
            seen.add(key)
            records.append(record)
        return tuple(records)


def _fetch_calendar_pages(
    *,
    endpoint: str,
    params: CalendarQuery,
    timeout: float | None,
    extra_params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Read bounded zero-based pages and fail when the caller limit hides a tail."""
    rows: list[dict[str, Any]] = []
    requests_made = 0
    for page in range(params.max_requests):
        request_params = {
            "from": params.start_date.isoformat(),
            "to": params.end_date.isoformat(),
            "page": str(page),
        }
        if extra_params is not None:
            request_params.update(extra_params)
        page_rows = fetch_array(endpoint, request_params, timeout)
        requests_made += 1
        if len(page_rows) > FMP_CALENDAR_PAGE_LIMIT:
            raise FMPProviderError(
                "FMP_PROVIDER_LIMIT_VIOLATION",
                partial=True,
                requests_made=requests_made,
            )
        if len(rows) + len(page_rows) > params.max_records:
            raise FMPQueryLimitError(
                "FMP_INCOMPLETE_RECORD_BUDGET",
                limit_kind="max_records",
                maximum=params.max_records,
                requests_made=requests_made,
            )
        rows.extend(page_rows)
        if not page_rows:
            return rows
    raise FMPQueryLimitError(
        "FMP_INCOMPLETE_PAGE_BUDGET",
        limit_kind="max_requests",
        maximum=params.max_requests,
        requests_made=requests_made,
    )


def _fetch_calendar_event_rows(
    *,
    params: CalendarEventsQuery,
    timeout: float | None,
) -> list[tuple[str, ContractModel]]:
    """Fetch all official company calendars under one combined request cap."""
    if params.start_date is None or params.end_date is None:
        raise FMPProviderError("FMP_INVALID_QUERY")
    routes: tuple[tuple[str, str, bool, dict[str, str]], ...] = (
        ("earnings", "earnings-calendar", True, {"includeReportTimes": "false"}),
        ("dividends", "dividends-calendar", True, {}),
        ("ipos", "ipos-calendar", False, {}),
        ("splits", "splits-calendar", True, {}),
    )
    source_models: dict[str, type[ContractModel]] = {
        "earnings": EarningsCalendarEvent,
        "dividends": DividendCalendarEvent,
        "ipos": IpoCalendarEvent,
        "splits": SplitsCalendarEvent,
    }
    rows: list[tuple[str, ContractModel]] = []
    requests_made = 0
    for route_index, (source_name, endpoint, paginated, extra_params) in enumerate(routes):
        remaining_routes = len(routes) - route_index - 1
        page = 0
        while True:
            if requests_made + 1 + remaining_routes > params.max_requests:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_PAGE_BUDGET",
                    limit_kind="max_requests",
                    maximum=params.max_requests,
                    requests_made=requests_made,
                )
            request_params = {
                "from": params.start_date.isoformat(),
                "to": params.end_date.isoformat(),
            }
            if paginated:
                request_params["page"] = str(page)
            request_params.update(extra_params)
            page_rows = fetch_array(endpoint, request_params, timeout)
            requests_made += 1

            if paginated and len(page_rows) > FMP_CALENDAR_PAGE_LIMIT:
                raise FMPProviderError(
                    "FMP_PROVIDER_LIMIT_VIOLATION",
                    partial=True,
                    requests_made=requests_made,
                )
            if len(rows) + len(page_rows) > params.max_records:
                raise FMPQueryLimitError(
                    "FMP_INCOMPLETE_RECORD_BUDGET",
                    limit_kind="max_records",
                    maximum=params.max_records,
                    requests_made=requests_made,
                )
            source_model = source_models[source_name]
            for row in page_rows:
                source_event = _validate_record(source_model, row)
                if not isinstance(
                    source_event,
                    (
                        EarningsCalendarEvent,
                        DividendCalendarEvent,
                        IpoCalendarEvent,
                        SplitsCalendarEvent,
                    ),
                ):
                    raise FMPProviderError("FMP_BAD_SHAPE")
                _ensure_date_in_window(source_event.date, params.start_date, params.end_date)
                rows.append((source_name, source_event))

            if not paginated or not page_rows:
                break
            page += 1
    return rows


def _validate_record(model: type[ModelT], item: dict[str, Any]) -> ModelT:
    """Build one concrete contract or return a sanitized provider shape error."""
    try:
        return model.model_validate(item)
    except (ValidationError, TypeError, ValueError):
        raise FMPProviderError("FMP_BAD_SHAPE") from None


def _ensure_date_in_window(
    value: date_type,
    start_date: date_type,
    end_date: date_type,
) -> None:
    if not start_date <= value <= end_date:
        raise FMPProviderError("FMP_OUT_OF_WINDOW_ROW")


def _check_unpaged_record_limit(rows: list[dict[str, Any]], maximum: int) -> None:
    if len(rows) > maximum:
        raise FMPQueryLimitError(
            "FMP_INCOMPLETE_RECORD_BUDGET",
            limit_kind="max_records",
            maximum=maximum,
            requests_made=1,
        )


__all__ = [
    "AvailableIndicesFetcher",
    "AvailableIndicesQuery",
    "CalendarDividendFetcher",
    "CalendarDividendQuery",
    "CalendarEarningsFetcher",
    "CalendarEarningsQuery",
    "CalendarEventsFetcher",
    "CalendarEventsQuery",
    "CalendarIpoFetcher",
    "CalendarSplitsFetcher",
    "EconomicCalendarFetcher",
    "EconomicCalendarQuery",
    "TreasuryRatesFetcher",
    "TreasuryRatesQuery",
    "MarketRiskPremiumFetcher",
    "MarketRiskPremiumQuery",
]
