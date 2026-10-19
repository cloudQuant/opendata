"""Earnings calendar from Seeking Alpha's public JSON:API ticker route.

One request per calendar day answers with that day's schedule; the route paginates, and the page
cursor is a query parameter rather than a link the client may follow. Both facts are measured, not
inferred from the reference implementation
(``docs/evidence/C142/wave38-seeking-alpha-calendar-20261019/capture-receipt.json``
(``HTTP/2 200``):

* ``filter[selected_date]=2026-10-20`` returns
  ``meta {size:50, total_count:56, total_pages:2}`` with a non-null ``links.next``, and the same
  address plus ``page[number]=2`` returns the remaining 6 records with
  ``links.next`` null -- 50 + 6 equals ``total_count``, so stopping at the first page silently
  loses 6 of 56 rows on this date.
* ``links.next`` is ``/earnings_calendar/tickers?…``; resolved against the site root it returns
  ``HTTP/2 404`` with ``text/html`` (same receipt, capture 5), so the adapter builds
  ``page[number]`` on the API address and never follows the published link.
* The reference implementation's extra ``filter[currency]=USD`` narrows the same date from 56 rows
  to 51 (capture 4), so this adapter does not send it: a row the source lists on the
  calendar is a row this repo returns.

Date bounds and the row limit are applied locally on top of the source's own per-day selection.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import StrictBool, StrictInt, StrictStr, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.seeking_alpha import SeekingAlphaCalendarEarnings
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.seeking_alpha._source import SOURCE
from opendata.data.request_budget import RequestBudgetError

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

API_URL = "https://seekingalpha.com/api/v3/earnings_calendar/tickers"
_RECORD_TYPE = "earning_calendar_ticker"
_MAX_DAYS = 8
_MAX_PAGES_PER_DAY = 25
_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_QUERY_RESPONSE_BYTES = 16 * 1024 * 1024
_MAX_ROWS = 5_000
_TIMEOUT_SECONDS = 30.0


class SeekingAlphaCalendarEarningsError(RuntimeError):
    """Sanitized transport or envelope failure for the public earnings-calendar route."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Expose only a stable code and optional HTTP status."""
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


class SeekingAlphaCalendarEarningsQuery(QueryParams):
    """Bounded calendar window, local row limit, and the raw-response cache choice."""

    symbol: StrictStr | None = None
    limit: StrictInt | None = None
    use_cache: StrictBool | None = True
    start_date: date | None = None
    end_date: date | None = None
    source: Literal["auto", "seeking_alpha"] = "auto"
    market: Literal["us"] | None = None

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: object) -> object:
        """Uppercase text symbols without coercing anything else."""
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("limit")
    @classmethod
    def _bound_limit(cls, value: int | None) -> int | None:
        """Reject a non-positive local limit before any request is admitted."""
        if value is not None and value < 1:
            raise ValueError("limit must be positive")
        return value

    @model_validator(mode="after")
    def _validate_window(self) -> SeekingAlphaCalendarEarningsQuery:
        """Order the window, because each day costs at least one request."""
        start, end = self.start_date, self.end_date
        if start is not None and end is not None and start > end:
            raise ValueError("start_date must not be after end_date")
        if self.symbol == "":
            raise ValueError("symbol must not be empty")
        return self

    def window(self) -> tuple[date, ...]:
        """Resolve the requested calendar days, mirroring the house bound-and-omit rule.

        An omitted bound means that one day, not an unbounded range: ``limit`` caps rows, but no
        query may ask for an open-ended calendar walk, because the route selects exactly one day.
        """
        start = self.start_date or self.end_date or date.today()
        end = self.end_date or self.start_date or start
        days = (end - start).days + 1
        if days <= 0:
            raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_REVERSED_WINDOW")
        if days > _MAX_DAYS:
            raise SeekingAlphaCalendarEarningsError(
                f"SEEKING_ALPHA_CALENDAR_WINDOW_TOO_LARGE_{_MAX_DAYS}D"
            )
        return tuple(start + timedelta(days=offset) for offset in range(days))


def _optional_text(cell: object) -> str | None:
    """Read one published text cell, refusing a non-text value rather than stringifying it."""
    if cell is None:
        return None
    if not isinstance(cell, str):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_TEXT_FIELD")
    return cell if cell.strip() else None


def _optional_number(cell: object) -> float | None:
    """Read one published numeric cell; a bool or text in this position is a corrupt body."""
    if cell is None:
        return None
    if isinstance(cell, bool) or not isinstance(cell, (int, float)):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_NUMERIC_FIELD")
    return float(cell)


def _optional_int(cell: object) -> int | None:
    """Read the source's sector id, which the contract declares as an integer."""
    if cell is None:
        return None
    if isinstance(cell, bool) or not isinstance(cell, int):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_INTEGER_FIELD")
    return cell


def _release_day(cell: object, *, requested: date) -> date:
    """Read the ISO release timestamp's date part and require it to be the requested day."""
    if not isinstance(cell, str) or len(cell) < 10:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_DATE_FIELD")
    try:
        parsed = datetime.strptime(cell[:10], "%Y-%m-%d").date()
    except ValueError:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_DATE_FIELD") from None
    if parsed != requested:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_DATE_MISMATCH")
    return parsed


def _map_record(record: object, *, requested: date) -> dict[str, Any]:
    """Map one JSON:API calendar resource onto the contract's cell names."""
    if not isinstance(record, dict):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ROW")
    if record.get("type") != _RECORD_TYPE:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ROW_TYPE")
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ATTRIBUTES")
    return {
        "report_date": _release_day(attributes.get("release_date"), requested=requested),
        "symbol": _optional_text(attributes.get("slug")),
        "name": _optional_text(attributes.get("name")),
        "market_cap": _optional_number(attributes.get("marketcap")),
        "reporting_time": _optional_text(attributes.get("release_time")),
        "exchange": _optional_text(attributes.get("exchange")),
        "sector_id": _optional_int(attributes.get("sector_id")),
    }


def _parse_page(body: bytes, *, requested: date, page: int) -> tuple[list[dict[str, Any]], bool]:
    """Validate one page envelope and return its rows plus whether a further page is due."""
    if not body or len(body) > _MAX_RESPONSE_BYTES:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_RESPONSE_TOO_LARGE")
    try:
        document = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_JSON") from None
    if not isinstance(document, dict):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ENVELOPE")
    records = document.get("data")
    meta = document.get("meta")
    links = document.get("links")
    if not isinstance(records, list) or not isinstance(meta, dict):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ENVELOPE")
    total_pages = meta.get("total_pages")
    if not isinstance(total_pages, int) or isinstance(total_pages, bool) or total_pages < 1:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_PAGINATION")
    if meta.get("size") != len(records):
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_SIZE_MISMATCH")
    next_link = links.get("next") if isinstance(links, dict) else None
    more = page < total_pages
    if more and not isinstance(next_link, str):
        # Truncation is the failure this guard exists for: a further page is declared but the
        # envelope carries no cursor, so the rows after this page can never be reached.
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_PAGINATION")
    return [_map_record(record, requested=requested) for record in records], more


def _http_error_code(status: int | None) -> str:
    """Map a governed HTTP failure to a safe provider-specific code."""
    if status == 401:
        return "SEEKING_ALPHA_CALENDAR_UNAUTHORIZED"
    if status == 403:
        return "SEEKING_ALPHA_CALENDAR_FORBIDDEN"
    if status == 404:
        return "SEEKING_ALPHA_CALENDAR_NOT_FOUND"
    if status == 429:
        return "SEEKING_ALPHA_CALENDAR_RATE_LIMITED"
    if status is not None and status >= 500:
        return "SEEKING_ALPHA_CALENDAR_UPSTREAM_ERROR"
    return "SEEKING_ALPHA_CALENDAR_HTTP_ERROR"


def _fetch_page(params: dict[str, str], context: FetchContext, *, use_cache: bool) -> bytes:
    """Fetch one calendar page through the governed, grant-checked HTTP client."""
    timeout = (
        _TIMEOUT_SECONDS if context.timeout is None else min(_TIMEOUT_SECONDS, context.timeout)
    )
    try:
        response = get_shared_http_client().get(
            API_URL,
            params=params,
            headers={"Accept": "application/json"},
            timeout=(min(5.0, timeout), timeout),
            source=SOURCE,
            use_cache=use_cache,
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise SeekingAlphaCalendarEarningsError(
            _http_error_code(exc.status), status=exc.status
        ) from None
    except Exception:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_TRANSPORT_ERROR") from None
    body = getattr(response, "content", None)
    if not isinstance(body, bytes) or not body:
        raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_RESPONSE")
    return body


class SeekingAlphaCalendarEarningsFetcher(
    Fetcher[SeekingAlphaCalendarEarningsQuery, list[dict[str, Any]]]
):
    """Read Seeking Alpha's public per-day earnings-calendar pages."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "CalendarEarnings"
    # Verified against contract_model('seeking_alpha_calendar_earnings') ->
    # opendata.data.models.seeking_alpha.SeekingAlphaCalendarEarnings (the frozen row this
    # fetcher's transform_data already builds), as the offline witness requires.
    row_model: ClassVar[type[ContractModel]] = SeekingAlphaCalendarEarnings
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="seeking_alpha_calendar_earnings",
        period="event",
        market="us",
        source=SOURCE,
        verified=False,
        notes=(
            "Public unauthenticated Seeking Alpha calendar route, one request per calendar day, "
            "followed through page[number] until meta.total_pages. eps_previous and eps_consensus "
            "have no cell in this envelope and stay null. filter[currency] is deliberately not "
            "sent: the captured response shows it drops rows the calendar lists."
        ),
    )

    def transform_query(self, **kwargs: object) -> SeekingAlphaCalendarEarningsQuery:
        """Validate the window and limit before any request is admitted."""
        query = SeekingAlphaCalendarEarningsQuery.model_validate(kwargs)
        query.window()
        return query

    def extract_data(
        self,
        params: SeekingAlphaCalendarEarningsQuery,
        ctx: FetchContext,
    ) -> list[dict[str, Any]]:
        """Walk every requested day and every declared page of it, in stable order."""
        use_cache = params.use_cache is not False
        total_bytes = 0
        records: list[dict[str, Any]] = []
        for day in params.window():
            page = 1
            while True:
                query = {"filter[selected_date]": day.isoformat()}
                if page > 1:
                    query["page[number]"] = str(page)
                body = _fetch_page(query, ctx, use_cache=use_cache)
                total_bytes += len(body)
                if total_bytes > _MAX_QUERY_RESPONSE_BYTES:
                    raise SeekingAlphaCalendarEarningsError(
                        "SEEKING_ALPHA_CALENDAR_RESPONSE_TOO_LARGE"
                    )
                rows, more = _parse_page(body, requested=day, page=page)
                records.extend(rows)
                if len(records) > _MAX_ROWS:
                    raise SeekingAlphaCalendarEarningsError(
                        "SEEKING_ALPHA_CALENDAR_RESPONSE_TOO_LARGE"
                    )
                if not more:
                    break
                page += 1
                if page > _MAX_PAGES_PER_DAY:
                    raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_TOO_MANY_PAGES")
        if params.symbol is not None:
            records = [row for row in records if row.get("symbol") == params.symbol]
        records.sort(key=lambda row: (str(row.get("report_date")), str(row.get("symbol"))))
        return records[: params.limit] if params.limit is not None else records

    def transform_data(
        self,
        raw: list[dict[str, Any]],
        params: SeekingAlphaCalendarEarningsQuery,
    ) -> FetchResult:
        """Validate each calendar row against the Seeking Alpha contract."""
        del params
        try:
            return [SeekingAlphaCalendarEarnings.model_validate(row, strict=True) for row in raw]
        except Exception:
            raise SeekingAlphaCalendarEarningsError("SEEKING_ALPHA_CALENDAR_BAD_ROW") from None


__all__ = [
    "SeekingAlphaCalendarEarningsError",
    "SeekingAlphaCalendarEarningsFetcher",
    "SeekingAlphaCalendarEarningsQuery",
]
