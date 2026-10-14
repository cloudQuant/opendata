"""Public Multpl S&P 500 valuation tables as typed time-series rows."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from typing import ClassVar, Literal

from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.models.multpl import SP500Frequency, SP500Metric, SP500Multiples
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers._engine.http_json import HttpGet, HttpResponse, ProviderEngineError
from opendata.data.providers.multpl.specs import METRIC_PAGES, MULTPL_BASE_URL, MetricPage
from opendata.data.request_budget import RequestBudgetError

_DEFAULT_TIMEOUT_SECONDS = 30.0
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}
_DATE_PATTERN = re.compile(r"^([A-Za-z]{3,4})\s+(\d{1,2}),\s+(\d{4})$")
_NUMBER_PATTERN = re.compile(r"^-?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?$")


class MultplProviderError(ProviderEngineError):
    """A stable, sanitized Multpl response or transport failure.

    It is the engine's attributable refusal for this source, so a shape failure carries the
    typed ``code``/``status``/``url``/``rejected``/``missing``/``invalid`` face the governed
    callers expect in addition to Multpl's own stable code vocabulary.
    """

    def __init__(self, code: str, *, status: int | None = None) -> None:
        """Keep the public error to a code and optional HTTP status."""
        super().__init__(code, status=status)


class SP500MultiplesQuery(QueryParams):
    """Optional metric/date filter for the fixed S&P 500 data set."""

    source: Literal["multpl"] = "multpl"
    market: Literal["us"] = "us"
    metric: SP500Metric | None = None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_date_only(cls, value: object) -> object:
        if isinstance(value, datetime):
            raise ValueError("date bounds must not include a time")
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as error:
                raise ValueError("date bounds must be ISO dates") from error
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("date bounds must be ISO dates without a time")

    @model_validator(mode="after")
    def _validate_window(self) -> SP500MultiplesQuery:
        if self.symbol is not None:
            raise ValueError("SP500Multiples is a fixed S&P 500 series and accepts no symbol")
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        return self


@dataclass(frozen=True)
class SourceObservation:
    """One parsed date/value pair and the source-declared series metadata."""

    metric: SP500Metric
    frequency: SP500Frequency
    date: date
    value: float
    value_unit: Literal["ratio", "percent"]
    is_estimate: bool


class _TableCollector(HTMLParser):
    """Collect top-level HTML tables while ignoring nested markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table_depth = 0
        self._current_table: list[list[str]] | None = None
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            if self._table_depth == 0:
                self._current_table = []
            self._table_depth += 1
            return
        if self._table_depth != 1:
            return
        if tag == "tr":
            self._current_row = []
        elif tag in ("td", "th") and self._current_row is not None:
            self._current_cell = []

    def handle_endtag(self, tag: str) -> None:
        if self._table_depth == 1 and tag in ("td", "th") and self._current_cell is not None:
            self._current_row_append("".join(self._current_cell))
            self._current_cell = None
        elif self._table_depth == 1 and tag == "tr" and self._current_row is not None:
            if self._current_table is not None:
                self._current_table.append(self._current_row)
            self._current_row = None
        elif tag == "table" and self._table_depth > 0:
            self._table_depth -= 1
            if self._table_depth == 0 and self._current_table is not None:
                self.tables.append(self._current_table)
                self._current_table = None

    def handle_data(self, data: str) -> None:
        if self._table_depth == 1 and self._current_cell is not None:
            self._current_cell.append(data)

    def _current_row_append(self, value: str) -> None:
        if self._current_row is not None:
            self._current_row.append(" ".join(value.split()))


def _parse_date(value: str) -> date:
    """Parse the English month/day/year text printed in the public tables."""
    match = _DATE_PATTERN.fullmatch(" ".join(value.split()))
    if match is None:
        raise MultplProviderError("MULTPL_BAD_DATE")
    month_name, day_text, year_text = match.groups()
    month = _MONTHS.get(month_name.casefold())
    if month is None:
        raise MultplProviderError("MULTPL_BAD_DATE")
    try:
        return date(int(year_text), month, int(day_text))
    except ValueError as error:
        raise MultplProviderError("MULTPL_BAD_DATE") from error


def _parse_value(value: str, *, expected_unit: str) -> tuple[float, bool]:
    """Parse a displayed number and preserve the source estimate marker."""
    is_estimate = "†" in value or "(estimate)" in value.casefold()
    normalized = " ".join(value.replace("†", " ").split())
    if "(estimate)" in normalized.casefold():
        normalized = re.sub(r"\(estimate\)", "", normalized, flags=re.IGNORECASE).strip()
    has_percent_sign = normalized.endswith("%")
    if has_percent_sign != (expected_unit == "percent"):
        raise MultplProviderError("MULTPL_BAD_VALUE_UNIT")
    if has_percent_sign:
        normalized = normalized[:-1].strip()
    if not _NUMBER_PATTERN.fullmatch(normalized):
        raise MultplProviderError("MULTPL_BAD_VALUE")
    try:
        number = float(normalized.replace(",", ""))
    except ValueError as error:
        raise MultplProviderError("MULTPL_BAD_VALUE") from error
    if not math.isfinite(number):
        raise MultplProviderError("MULTPL_BAD_VALUE")
    return number, is_estimate


def _date_value_rows(html: str) -> list[list[str]]:
    """Find the one table whose published header is Date and Value."""
    parser = _TableCollector()
    try:
        parser.feed(html)
        parser.close()
    except Exception as error:
        raise MultplProviderError("MULTPL_BAD_HTML") from error

    matching_tables: list[list[list[str]]] = []
    for table in parser.tables:
        for index, row in enumerate(table):
            normalized = tuple(cell.casefold() for cell in row)
            if normalized == ("date", "value"):
                matching_tables.append(table[index + 1 :])
                break
    if not matching_tables:
        raise MultplProviderError("MULTPL_TABLE_MISSING")
    if len(matching_tables) > 1:
        raise MultplProviderError("MULTPL_AMBIGUOUS_TABLE")
    rows = matching_tables[0]
    if not rows:
        raise MultplProviderError("MULTPL_EMPTY_TABLE")
    return rows


def parse_table(html: str, metric: SP500Metric, page: MetricPage) -> tuple[SourceObservation, ...]:
    """Parse a real Multpl Date/Value table into typed source observations."""
    observations: list[SourceObservation] = []
    for row in _date_value_rows(html):
        if not row:
            continue
        if len(row) != 2 or not row[0] or not row[1]:
            raise MultplProviderError("MULTPL_BAD_ROW")
        observation_date = _parse_date(row[0])
        value, is_estimate = _parse_value(row[1], expected_unit=page.value_unit)
        observations.append(
            SourceObservation(
                metric=metric,
                frequency=page.frequency,
                date=observation_date,
                value=value,
                value_unit=page.value_unit,
                is_estimate=is_estimate,
            )
        )
    if not observations:
        raise MultplProviderError("MULTPL_EMPTY_TABLE")
    return tuple(observations)


def _http_get_html(url: str, timeout: float | None) -> tuple[int, str]:
    """Read one public HTML page through the governed shared HTTP client."""
    try:
        response = get_shared_http_client().get(
            url,
            headers={"Accept": "text/html"},
            timeout=_DEFAULT_TIMEOUT_SECONDS if timeout is None else timeout,
            source="multpl",
            cache_context={"representation": "date-value-table"},
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as error:
        if error.status == 429:
            code = "MULTPL_RATE_LIMITED"
        elif error.status == 403:
            code = "MULTPL_BLOCKED"
        else:
            code = "MULTPL_HTTP_ERROR"
        raise MultplProviderError(code, status=error.status) from error
    return response.status_code, response.text


def _selected_metrics(metric: SP500Metric | None) -> tuple[SP500Metric, ...]:
    """Return metrics in a stable order for an all-series request."""
    if metric is not None:
        return (metric,)
    return tuple(METRIC_PAGES)


def _response_body(response: HttpResponse) -> str:
    """Return the page body exactly as the transport delivered it.

    A governed HTTP client hands over ``payload``; the repository's offline witness seam has no
    raw-text carrier, so it supplies the same page losslessly as the list of its source lines.
    Anything else is a transport the parser was never given a page by.
    """
    if isinstance(response.payload, str):
        return response.payload
    if isinstance(response.payload, bytes):
        return response.payload.decode("utf-8")
    if isinstance(response.document, str):
        return response.document
    if isinstance(response.document, list) and all(
        isinstance(line, str) for line in response.document
    ):
        return "\n".join(response.document)
    raise MultplProviderError("MULTPL_BAD_HTML")


def _http_status_code(status: int) -> str:
    """Name the documented failure for one non-2xx page status."""
    if status == 429:
        return "MULTPL_RATE_LIMITED"
    if status == 403:
        return "MULTPL_BLOCKED"
    return "MULTPL_HTTP_ERROR"


class SP500MultiplesFetcher(Fetcher[SP500MultiplesQuery, tuple[SourceObservation, ...]]):
    """Fetch Multpl valuation tables and return typed contract rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "SP500Multiples"
    row_model: ClassVar[type[SP500Multiples]] = SP500Multiples
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="index",
        domain="sp500_multiples",
        period="variable",
        market="us",
        source="multpl",
        verified=False,
        notes="Public HTML tables are parsed; cross-source verification is pending.",
    )

    def _get_html(self, url: str, timeout: float | None) -> str:
        """Read one public page, through the offline seam when a witness installed one."""
        transport = self.http_transport
        if transport is None:
            status, html = _http_get_html(url, timeout)
        else:
            response = transport(
                url,
                {},
                timeout=_DEFAULT_TIMEOUT_SECONDS if timeout is None else float(timeout),
                source="multpl",
                headers={"Accept": "text/html"},
            )
            status, html = response.status, _response_body(response)
        if not 200 <= status < 300:
            raise MultplProviderError(_http_status_code(status), status=status)
        return html

    def transform_query(self, **kwargs: object) -> SP500MultiplesQuery:
        """Validate source, metric, and inclusive date bounds before any request."""
        return SP500MultiplesQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: SP500MultiplesQuery,
        ctx: FetchContext,
    ) -> tuple[SourceObservation, ...]:
        """Fetch every selected public table and parse the published rows."""
        observations: list[SourceObservation] = []
        for metric in _selected_metrics(params.metric):
            page = METRIC_PAGES[metric]
            url = f"{MULTPL_BASE_URL}{page.path}"
            html = self._get_html(url, ctx.timeout)
            observations.extend(parse_table(html, metric, page))
        return tuple(observations)

    def transform_data(
        self,
        raw: tuple[SourceObservation, ...],
        params: SP500MultiplesQuery,
    ) -> FetchResult:
        """Apply the caller's inclusive window and validate typed output rows."""
        rows = []
        for observation in raw:
            if params.start_date is not None and observation.date < params.start_date:
                continue
            if params.end_date is not None and observation.date > params.end_date:
                continue
            rows.append(
                SP500Multiples(
                    metric=observation.metric,
                    frequency=observation.frequency,
                    date=observation.date,
                    value=observation.value,
                    value_unit=observation.value_unit,
                    is_estimate=observation.is_estimate,
                )
            )
        return tuple(rows)


__all__ = [
    "MultplProviderError",
    "SP500MultiplesFetcher",
    "SP500MultiplesQuery",
    "SourceObservation",
    "parse_table",
]
