"""Bounded HTTP and parsing helpers for Nasdaq's typed fetchers."""

from __future__ import annotations

import csv
import html
import io
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, time, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar
from urllib.parse import urlencode

from pydantic import ValidationError

from opendata.data.models.base import ContractModel
from opendata.data.protocol import FetchContext, Fetcher, QueryParams
from opendata.data.providers._engine.http_json import (
    HttpResponse,
    ProviderEngineError,
    _http_get_json,
)
from opendata.data.providers.nasdaq._source import (
    NASDAQ_COM_BASE_URL,
    NASDAQ_COM_HEADERS,
    SOURCE,
)

if TYPE_CHECKING:
    from opendata.data.capability import Capability

DEFAULT_TIMEOUT = 20.0
MAX_CALENDAR_DAYS = 31
MAX_IPO_MONTHS = 12
MAX_COMPANY_FILING_PAGES = 50
MAX_COMPANY_FILING_ROWS = 5_000
MAX_SYMBOLS_PER_REQUEST = 20
MAX_DIRECTORY_ROWS = 50_000

NasdaqTransport = Callable[..., HttpResponse]
QueryT = TypeVar("QueryT", bound=QueryParams)
RawT = TypeVar("RawT")
RowT = TypeVar("RowT", bound=ContractModel)


class NasdaqFetcher(Fetcher[QueryT, RawT], Generic[QueryT, RawT]):
    """Common guarded request path and stable error classification."""

    async_mode = "bounded_thread"
    http_transport: ClassVar[NasdaqTransport | None] = None
    canonical_model: ClassVar[str]
    capability: ClassVar[Capability]

    def _get(
        self,
        path_or_url: str,
        params: Mapping[str, str],
        ctx: FetchContext,
        *,
        headers: Mapping[str, str] | None = NASDAQ_COM_HEADERS,
        decoder: str = "json",
    ) -> HttpResponse:
        """Send one governed GET and classify status before parsing its body."""
        url = (
            path_or_url if path_or_url.startswith("https://") else NASDAQ_COM_BASE_URL + path_or_url
        )
        timeout = ctx.timeout or DEFAULT_TIMEOUT
        transport = self.http_transport or _http_get_json
        try:
            response = transport(
                url,
                dict(params),
                timeout=timeout,
                source=SOURCE,
                headers=headers,
                decoder=decoder,
            )
        except ProviderEngineError as error:
            if error.status == 429:
                raise ProviderEngineError("NASDAQ_RATE_LIMITED", status=429, url=url) from None
            if error.status in (401, 403):
                raise ProviderEngineError(
                    "NASDAQ_CREDENTIAL_REJECTED", status=error.status, url=url
                ) from None
            if error.status is not None and error.status >= 500:
                raise ProviderEngineError(
                    "NASDAQ_UPSTREAM_UNAVAILABLE", status=error.status, url=url
                ) from None
            # Engine transport errors can include a prepared URL. Keep the public error stable and
            # discard its context so an API-key query parameter can never escape in a traceback.
            raise ProviderEngineError("NASDAQ_TRANSPORT_ERROR", url=url) from None
        if not isinstance(response, HttpResponse):
            raise ProviderEngineError("NASDAQ_BAD_RESPONSE", url=url)
        if response.status == 429:
            raise ProviderEngineError("NASDAQ_RATE_LIMITED", status=429, url=url)
        if response.status in (401, 403):
            raise ProviderEngineError("NASDAQ_CREDENTIAL_REJECTED", status=response.status, url=url)
        if response.status == 404:
            raise ProviderEngineError("NASDAQ_RESOURCE_MISSING", status=404, url=url)
        if response.status in (400, 422):
            raise ProviderEngineError("NASDAQ_QUERY_REJECTED", status=response.status, url=url)
        if response.status >= 500:
            raise ProviderEngineError(
                "NASDAQ_UPSTREAM_UNAVAILABLE", status=response.status, url=url
            )
        if not 200 <= response.status < 300:
            raise ProviderEngineError("NASDAQ_HTTP_ERROR", status=response.status, url=url)
        return response

    def _json(
        self,
        path: str,
        params: Mapping[str, str],
        ctx: FetchContext,
        *,
        headers: Mapping[str, str] | None = NASDAQ_COM_HEADERS,
    ) -> Mapping[str, Any]:
        """Return an object document; a valid empty body is not treated as an empty result."""
        response = self._get(path, params, ctx, headers=headers)
        if not isinstance(response.document, Mapping):
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        return response.document

    def _rows(
        self,
        document: Mapping[str, Any],
        *path: str,
    ) -> list[Mapping[str, Any]]:
        """Resolve a required row list while keeping absent and explicitly empty distinct."""
        value: object = document
        for key in path:
            if not isinstance(value, Mapping) or key not in value:
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=(key,))
            value = value[key]
        if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        return value

    def _rows_or_empty(
        self,
        document: Mapping[str, Any],
        *path: str,
    ) -> list[Mapping[str, Any]]:
        """Accept Nasdaq's explicit null-row empty tables without masking absent fields."""
        if not path:
            raise ProviderEngineError("NASDAQ_SHAPE_MISSING")
        parent: object = document
        for key in path[:-1]:
            if not isinstance(parent, Mapping) or key not in parent:
                raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=(key,))
            parent = parent[key]
        key = path[-1]
        if not isinstance(parent, Mapping) or key not in parent:
            raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=(key,))
        value = parent[key]
        if isinstance(value, list) and all(isinstance(row, Mapping) for row in value):
            return value
        if value is None and (
            ("headers" in parent and parent["headers"] is None)
            or parent.get("totalRecords") == 0
            or parent.get("totalrecords") == 0
        ):
            return []
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")

    def _typed(self, model: type[RowT], values: Mapping[str, object]) -> RowT:
        """Validate one exact row contract, preserving no source-only keys."""
        try:
            return model.model_validate(values)
        except ValidationError as error:
            fields = tuple(
                sorted({str(item["loc"][0]) for item in error.errors() if item.get("loc")})
            )
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID", invalid=fields) from error


def bounded_dates(start: date | None, end: date | None) -> tuple[date, date]:
    """Resolve an omitted calendar bound to one day and reject reversed/long windows."""
    if start is None and end is None:
        start = end = date.today()
    elif start is None:
        start = end
    elif end is None:
        end = start
    if start is None or end is None:  # pragma: no cover - resolved above
        raise ValueError("date bounds could not be resolved")
    days = (end - start).days + 1
    if days <= 0:
        raise ValueError("start_date must not be after end_date")
    if days > MAX_CALENDAR_DAYS:
        raise ValueError(f"date range exceeds the {MAX_CALENDAR_DAYS}-day request bound")
    return start, end


def dates_inclusive(start: date, end: date) -> tuple[date, ...]:
    """Build a bounded inclusive date sequence."""
    from datetime import timedelta

    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def month_keys(start: date | None, end: date | None) -> tuple[str, ...]:
    """Resolve IPO dates to at most twelve distinct calendar months."""
    if start is None and end is None:
        start = end = date.today()
    elif start is None:
        start = end
    elif end is None:
        end = start
    if start is None or end is None:  # pragma: no cover - resolved above
        raise ValueError("date bounds could not be resolved")
    if start > end:
        raise ValueError("start_date must not be after end_date")
    months = (end.year - start.year) * 12 + end.month - start.month + 1
    if months > MAX_IPO_MONTHS:
        raise ValueError(f"date range exceeds the {MAX_IPO_MONTHS}-month request bound")
    return tuple(
        f"{year:04d}-{month:02d}" for year, month in _iter_months(start.year, start.month, months)
    )


def _iter_months(year: int, month: int, count: int) -> tuple[tuple[int, int], ...]:
    values: list[tuple[int, int]] = []
    for _ in range(count):
        values.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(values)


def optional_text(value: object) -> str | None:
    """Normalize a present textual cell without turning a missing key into a value."""
    if value is None:
        return None
    text = str(value).strip()
    return None if text.casefold() in {"", "n/a", "na", "null", "none", "--", "-"} else text


def required_text(value: object) -> str:
    """Require one non-blank textual value."""
    text = optional_text(value)
    if text is None:
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    return text


def optional_date(value: object) -> date | None:
    """Parse a source date from ISO or Nasdaq's month/day/year spelling."""
    if value is None or optional_text(value) is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for pattern in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%Y%m%d",
        "%a, %b %d, %Y",
    ):
        parsed = _date_for_pattern(text, pattern)
        if parsed is not None:
            return parsed
    raise ProviderEngineError("NASDAQ_SHAPE_INVALID")


def _date_for_pattern(text: str, pattern: str) -> date | None:
    """Try one explicitly supported date spelling."""
    try:
        return datetime.strptime(text, pattern).date()
    except ValueError:
        return None


def required_date(value: object) -> date:
    """Parse a required source date."""
    result = optional_date(value)
    if result is None:
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    return result


def optional_float(value: object) -> float | None:
    """Parse an optional finite number from a source text cell."""
    if value is None or optional_text(value) is None:
        return None
    if isinstance(value, bool):
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
        if text.startswith("(") and text.endswith(")"):
            text = "-" + text[1:-1]
        try:
            number = float(text)
        except ValueError as error:
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID") from error
    if not math.isfinite(number):
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    return number


def required_float(value: object) -> float:
    """Parse a required finite number."""
    result = optional_float(value)
    if result is None:
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    return result


def optional_int(value: object) -> int | None:
    """Parse an optional whole number without truncating fractional values."""
    number = optional_float(value)
    if number is None:
        return None
    if not number.is_integer():
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    return int(number)


def percentage_fraction(value: object) -> float | None:
    """Convert a published percentage-point value into the contract's fractional value."""
    number = optional_float(value)
    return None if number is None else number / 100.0


def compact_query(params: Mapping[str, object]) -> dict[str, str]:
    """Encode simple validated values without accepting arbitrary JSON query structures."""
    query: dict[str, str] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[key] = "true" if value else "false"
        elif isinstance(value, date):
            query[key] = value.isoformat()
        elif isinstance(value, str):
            query[key] = value
        elif isinstance(value, int) and not isinstance(value, bool):
            query[key] = str(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            query[key] = "|".join(str(item) for item in value)
        else:
            raise ProviderEngineError("NASDAQ_PARAM_UNENCODABLE", rejected=(key,))
    return query


def validate_symbol(value: str) -> str:
    """Return one plain ticker safe for a Nasdaq path segment."""
    text = value.strip().upper()
    if not text or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-^]{0,14}", text):
        raise ValueError("symbol must be a plain ticker with no URL delimiters")
    return text


def symbols_from(value: str | list[str]) -> tuple[str, ...]:
    """Validate a bounded set of comma-separated or list-valued tickers."""
    raw = value.split(",") if isinstance(value, str) else value
    if not raw or len(raw) > MAX_SYMBOLS_PER_REQUEST:
        raise ValueError(f"symbol accepts 1 to {MAX_SYMBOLS_PER_REQUEST} tickers")
    symbols = tuple(validate_symbol(item) for item in raw)
    if len(symbols) != len(set(symbols)):
        raise ValueError("symbol list must not contain duplicates")
    return symbols


def build_query(
    query_type: type[QueryT],
    kwargs: dict[str, object],
    allowed: set[str],
    *,
    markets: frozenset[str] = frozenset({"us", "usa", "nasdaq", "united states"}),
) -> QueryT:
    """Validate a model-specific query before extraction, including inherited common keys."""
    unsupported = sorted(set(kwargs) - allowed - {"source", "market"})
    if unsupported:
        raise ProviderEngineError("NASDAQ_QUERY_INVALID", rejected=unsupported)
    try:
        query = query_type.model_validate(kwargs)
    except ValidationError as error:
        fields = tuple(sorted({str(item["loc"][0]) for item in error.errors() if item.get("loc")}))
        raise ProviderEngineError("NASDAQ_QUERY_INVALID", invalid=fields) from error
    if query.source.casefold() not in {"auto", "nasdaq"}:
        raise ProviderEngineError("NASDAQ_QUERY_INVALID", rejected=("source",))
    if query.market is not None and query.market.casefold() not in markets:
        raise ProviderEngineError("NASDAQ_QUERY_INVALID", rejected=("market",))
    return query


class _TextOnlyHtml(HTMLParser):
    """Collect text nodes without preserving HTML tags or script/style content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in {"script", "style"}:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in {"script", "style"} and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self.parts.append(data)


def html_text(value: object) -> str | None:
    """Unescape and strip a published HTML description."""
    text = optional_text(value)
    if text is None:
        return None
    parser = _TextOnlyHtml()
    parser.feed(html.unescape(text))
    parser.close()
    return " ".join(" ".join(parser.parts).split()) or None


def parse_gmt_datetime(day: date, value: object) -> datetime | None:
    """Combine a calendar day with a published GMT time."""
    text = optional_text(value)
    if text is None:
        return None
    for pattern in ("%H:%M", "%H:%M:%S", "%I:%M %p"):
        parsed = _time_for_pattern(text, pattern)
        if parsed is not None:
            return datetime.combine(day, parsed).replace(tzinfo=timezone.utc)
    raise ProviderEngineError("NASDAQ_SHAPE_INVALID")


def _time_for_pattern(text: str, pattern: str) -> time | None:
    """Try one documented-style GMT time spelling."""
    try:
        return datetime.strptime(text, pattern).time()
    except ValueError:
        return None


def data_link_api_key() -> str:
    """Read the optional Nasdaq Data Link key only for the RTAT10 model."""
    key = os.environ.get("NASDAQ_DATA_LINK_API_KEY", "").strip()
    if not key:
        raise ProviderEngineError("NASDAQ_CREDENTIAL_MISSING")
    return key


def pipe_file_rows(payload: bytes | str) -> list[dict[str, str]]:
    """Decode Nasdaq Trader's pipe-delimited symbol directory."""
    try:
        text = payload.decode("utf-8-sig") if isinstance(payload, bytes) else payload
        reader = csv.DictReader(io.StringIO(text), delimiter="|")
        if not reader.fieldnames or "Symbol" not in reader.fieldnames:
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        rows: list[dict[str, str]] = []
        for raw in reader:
            if len(rows) >= MAX_DIRECTORY_ROWS:
                raise ProviderEngineError("NASDAQ_INCOMPLETE")
            if not raw.get("Symbol") or raw.get("Symbol", "").startswith("File Creation Time"):
                continue
            rows.append({key: value or "" for key, value in raw.items() if key is not None})
        return rows
    except (UnicodeDecodeError, csv.Error) as error:
        raise ProviderEngineError("NASDAQ_BAD_RESPONSE") from error


def data_link_rows(
    document: Mapping[str, Any],
    *,
    expected_columns: tuple[str, ...],
) -> list[dict[str, object]]:
    """Validate Nasdaq Data Link's positional table rows against declared column names."""
    table = document.get("datatable")
    if not isinstance(table, Mapping):
        raise ProviderEngineError("NASDAQ_SHAPE_MISSING", missing=("datatable",))
    raw_columns = table.get("columns")
    raw_data = table.get("data")
    if not isinstance(raw_columns, list) or not isinstance(raw_data, list):
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    column_names: list[str] = []
    for item in raw_columns:
        if not isinstance(item, Mapping) or not isinstance(item.get("name"), str):
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        column_names.append(item["name"].casefold())
    if tuple(column_names) != expected_columns:
        raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
    rows: list[dict[str, object]] = []
    for raw in raw_data:
        if not isinstance(raw, list) or len(raw) != len(expected_columns):
            raise ProviderEngineError("NASDAQ_SHAPE_INVALID")
        rows.append(dict(zip(expected_columns, raw, strict=True)))
    return rows


def append_query(url: str, query: Mapping[str, str]) -> str:
    """Append bounded query values for a GET endpoint that requires offset in its URL."""
    encoded = urlencode(query)
    return url if not encoded else f"{url}?{encoded}"
