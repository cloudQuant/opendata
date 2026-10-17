"""Fail-closed Finviz quote-page adapters for ODP v4 performance models."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import urlencode

from pydantic import field_validator

from opendata.data.capability import Capability
from opendata.data.models.finviz_performance import (
    FinvizEtfPricePerformance,
    FinvizPerformanceProvenance,
    FinvizPricePerformance,
    PartialPerformanceField,
)
from opendata.data.protocol import BaseQueryParams, FetchContext, Fetcher, FetchResult
from opendata.data.providers.finviz import _source
from opendata.data.request_budget import (
    GrantDecision,
    RequestAttemptLimitError,
    current_request_scopes,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel

_TICKER = re.compile(r"[A-Za-z][A-Za-z0-9.\-]{0,9}\Z")
_PERCENT = re.compile(r"[+-]?(?:\d+(?:,\d{3})*(?:\.\d*)?|\.\d+)%\Z")
_EMPTY_VALUES = frozenset({"", "-", "—", "–"})
_MAX_SYMBOLS_PER_CALL = 10
_ETF_CATEGORY = "Exchange Traded Fund"
_QUOTE_DELAY_NOTICE: Literal["Stock quotes delayed by 1 minute."] = (
    "Stock quotes delayed by 1 minute."
)

_LABEL_TO_FIELD: dict[str, PartialPerformanceField] = {
    "Change %": "one_day",
    "Perf Week": "one_week",
    "Perf Month": "one_month",
    "Perf Quarter": "three_month",
    "Perf Half Y": "six_month",
    "Perf YTD": "ytd",
    "Perf Year": "one_year",
    "Perf 3Y": "three_year",
    "Perf 5Y": "five_year",
    "Perf 10Y": "ten_year",
}
_LABEL_TO_TOOLTIP = {
    "Change %": "Performance (today)",
    "Perf Week": "Performance (Week, Excl. Dividends)",
    "Perf Month": "Performance (Month, Excl. Dividends)",
    "Perf Quarter": "Performance (Quarter, Excl. Dividends)",
    "Perf Half Y": "Performance (Half Year, Excl. Dividends)",
    "Perf YTD": "Performance (Year To Date, Excl. Dividends)",
    "Perf Year": "Performance (Year, Excl. Dividends)",
    "Perf 3Y": "Performance (3 Years, Excl. Dividends)",
    "Perf 5Y": "Performance (5 Years, Excl. Dividends)",
    "Perf 10Y": "Performance (10 Years, Excl. Dividends)",
}
_PARTIAL_FIELDS = tuple(_LABEL_TO_FIELD.values())


class FinvizPerformanceQuery(BaseQueryParams):
    """Only a ticker or a non-empty list of ticker strings is accepted."""

    source: Literal["finviz"] = "finviz"
    market: Literal["us"] = "us"
    symbol: str | list[str]

    @field_validator("symbol", mode="before")
    @classmethod
    def _validate_symbols(cls, value: object) -> str | list[str]:
        if type(value) is str:
            values = [value]
            is_list = False
        elif type(value) is list:
            values = value
            is_list = True
        else:
            raise ValueError("symbol must be a string or a list of strings")
        if not values:
            raise ValueError("symbol list must not be empty")
        normalized: list[str] = []
        for item in values:
            if type(item) is not str:
                raise ValueError("every symbol must be a string")
            ticker = item.strip().upper()
            if not _TICKER.fullmatch(ticker):
                raise ValueError("symbol must be a safe Finviz ticker")
            normalized.append(ticker)
        return normalized if is_list else normalized[0]


@dataclass(frozen=True)
class _RetrievedPage:
    requested_symbol: str
    source_url: str
    retrieved_at_utc: datetime
    body: bytes


@dataclass(frozen=True)
class _TableCell:
    text: str
    label: str | None
    boxover: str | None


class _PerformancePageParser(HTMLParser):
    """Capture only visible identity, category, quote-time, and snapshot rows."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.wrapper_symbols: list[str] = []
        self.heading_symbols: list[tuple[str, str]] = []
        self.categories: list[str] = []
        self.quote_timestamps: list[str] = []
        self.rows: list[tuple[_TableCell, ...]] = []
        self.has_quote_content = False
        self._table_depth = 0
        self._row: list[_TableCell] | None = None
        self._cell_text: list[str] | None = None
        self._cell_label: list[str] | None = None
        self._cell_boxover: str | None = None
        self._label_depth = 0
        self._active_heading: tuple[str, list[str]] | None = None
        self._active_category: list[str] | None = None
        self._active_quote_timestamp: list[str] | None = None
        self._quote_timestamp_depth = 0
        self._suppressed_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag in {"script", "style"}:
            self._suppressed_depth += 1
        if tag == "div" and "ticker-wrapper" in classes:
            symbol = attributes.get("data-ticker")
            if symbol:
                self.wrapper_symbols.append(symbol.strip())
        if tag == "h1" and "js-recent-quote-ticker" in classes:
            symbol = attributes.get("data-ticker")
            if symbol:
                self._active_heading = (symbol.strip(), [])
        if tag == "a" and "quote-header_category" in classes:
            self._active_category = []
        if tag == "span" and "quote-price_date" in classes:
            self._active_quote_timestamp = []
            self._quote_timestamp_depth = 1
        elif tag == "span" and self._active_quote_timestamp is not None:
            self._quote_timestamp_depth += 1
        if attributes.get("data-testid") == "stock-data-content":
            self.has_quote_content = True
        if tag == "table":
            self._table_depth += 1
        elif tag == "tr" and self._table_depth > 0:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None and self._cell_text is None:
            self._cell_text = []
            self._cell_label = None
            self._cell_boxover = attributes.get("data-boxover-html")
        elif tag == "div" and self._cell_text is not None and "snapshot-td-label" in classes:
            self._label_depth += 1
            self._cell_label = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "div" and self._label_depth:
            self._label_depth -= 1
        if tag in {"td", "th"} and self._cell_text is not None:
            if self._row is None:
                raise _source.FinvizSourceError("FINVIZ_INVALID_HTML")
            self._row.append(
                _TableCell(
                    text=_clean_text("".join(self._cell_text)),
                    label=(
                        _clean_text("".join(self._cell_label))
                        if self._cell_label is not None
                        else None
                    ),
                    boxover=self._cell_boxover,
                )
            )
            self._cell_text = None
            self._cell_label = None
            self._cell_boxover = None
            self._label_depth = 0
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(tuple(self._row))
            self._row = None
        elif tag == "table" and self._table_depth:
            self._table_depth -= 1
        elif tag == "h1" and self._active_heading is not None:
            symbol, text = self._active_heading
            self.heading_symbols.append((symbol, _clean_text("".join(text))))
            self._active_heading = None
        elif tag == "a" and self._active_category is not None:
            category = _clean_text("".join(self._active_category))
            if category:
                self.categories.append(category)
            self._active_category = None
        elif tag == "span" and self._active_quote_timestamp is not None:
            self._quote_timestamp_depth -= 1
            if self._quote_timestamp_depth == 0:
                timestamp = _clean_text("".join(self._active_quote_timestamp))
                if timestamp:
                    self.quote_timestamps.append(timestamp)
                self._active_quote_timestamp = None
        elif tag in {"script", "style"} and self._suppressed_depth:
            self._suppressed_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._suppressed_depth:
            return
        if self._cell_text is not None:
            self._cell_text.append(data)
        if self._cell_label is not None:
            self._cell_label.append(data)
        if self._active_heading is not None:
            self._active_heading[1].append(data)
        if self._active_category is not None:
            self._active_category.append(data)
        if self._active_quote_timestamp is not None:
            self._active_quote_timestamp.append(data)


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def _parse_page(
    body: bytes,
    *,
    requested_symbol: str,
    source_url: str,
    retrieved_at_utc: datetime,
    require_etf: bool,
) -> dict[str, object]:
    if not body:
        raise _source.FinvizSourceError("FINVIZ_EMPTY_RESPONSE")
    try:
        html = body.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise _source.FinvizSourceError("FINVIZ_INVALID_ENCODING") from error
    if "<" not in html or ">" not in html:
        raise _source.FinvizSourceError("FINVIZ_INVALID_HTML")
    parser = _PerformancePageParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ValueError) as error:
        raise _source.FinvizSourceError("FINVIZ_INVALID_HTML") from error

    returned_symbol = _validated_page_identity(parser, requested_symbol)
    if not parser.has_quote_content:
        raise _source.FinvizSourceError("FINVIZ_QUOTE_CONTENT_MISSING")
    if require_etf and _ETF_CATEGORY not in parser.categories:
        raise _source.FinvizSourceError("FINVIZ_NOT_ETF")
    if len(parser.quote_timestamps) > 1:
        raise _source.FinvizSourceError("FINVIZ_AMBIGUOUS_QUOTE_TIMESTAMP")

    displayed: dict[str, str] = {}
    source_labels: dict[PartialPerformanceField, str] = {}
    for row in parser.rows:
        for index, cell in enumerate(row):
            label = cell.label
            if label not in _LABEL_TO_FIELD:
                continue
            if label in displayed:
                raise _source.FinvizSourceError("FINVIZ_AMBIGUOUS_PERFORMANCE_ROW")
            if cell.boxover != _LABEL_TO_TOOLTIP[label]:
                raise _source.FinvizSourceError("FINVIZ_PERFORMANCE_TOOLTIP_MISMATCH")
            if index != 0 or len(row) != 2:
                raise _source.FinvizSourceError("FINVIZ_PERFORMANCE_ROW_SHAPE")
            value_cell = row[1]
            displayed[label] = value_cell.text
            source_labels[_LABEL_TO_FIELD[label]] = label

    missing_labels = set(_LABEL_TO_FIELD).difference(displayed)
    if missing_labels:
        raise _source.FinvizSourceError("FINVIZ_PERFORMANCE_FIELDS_MISSING")

    values: dict[str, object] = dict.fromkeys(_PARTIAL_FIELDS, None)
    for label, raw_value in displayed.items():
        field = _LABEL_TO_FIELD[label]
        values[field] = _parse_percent(raw_value)

    provenance = FinvizPerformanceProvenance(
        requested_symbol=requested_symbol,
        returned_symbol=returned_symbol,
        source_url=source_url,
        retrieved_at_utc=retrieved_at_utc,
        quote_timestamp_raw=(parser.quote_timestamps[0] if parser.quote_timestamps else None),
        quote_delay_notice=_QUOTE_DELAY_NOTICE,
        source_labels=source_labels,
        raw_source_values=displayed,
        return_basis="price-only; Finviz Perf excludes dividends",
        dividends_included=False,
    )
    return {
        "symbol": returned_symbol,
        **values,
        "provenance": provenance,
        "partial_supported_fields": _PARTIAL_FIELDS,
    }


def _validated_page_identity(parser: _PerformancePageParser, requested: str) -> str:
    if len(parser.wrapper_symbols) != 1 or len(parser.heading_symbols) != 1:
        raise _source.FinvizSourceError("FINVIZ_STOCK_IDENTITY_MISSING")
    wrapper_symbol = parser.wrapper_symbols[0]
    heading_symbol, heading_text = parser.heading_symbols[0]
    symbols = (wrapper_symbol, heading_symbol, heading_text)
    if any(not _TICKER.fullmatch(symbol) for symbol in symbols):
        raise _source.FinvizSourceError("FINVIZ_STOCK_IDENTITY_MISSING")
    if len({symbol.casefold() for symbol in symbols}) != 1:
        raise _source.FinvizSourceError("FINVIZ_STOCK_IDENTITY_MISMATCH")
    if heading_symbol.casefold() != requested.casefold():
        raise _source.FinvizSourceError("FINVIZ_STOCK_IDENTITY_MISMATCH")
    return heading_symbol


def _parse_percent(value: str) -> float | None:
    cleaned = value.strip()
    if cleaned.casefold() in _EMPTY_VALUES:
        return None
    if not _PERCENT.fullmatch(cleaned):
        raise _source.FinvizSourceError("FINVIZ_INVALID_NUMBER")
    try:
        parsed = float(Decimal(cleaned[:-1].replace(",", "")) / Decimal(100))
    except (InvalidOperation, OverflowError, ValueError) as error:
        raise _source.FinvizSourceError("FINVIZ_INVALID_NUMBER") from error
    if not math.isfinite(parsed):
        raise _source.FinvizSourceError("FINVIZ_INVALID_NUMBER")
    return parsed


def _symbols(params: FinvizPerformanceQuery) -> tuple[str, ...]:
    return (params.symbol,) if isinstance(params.symbol, str) else tuple(params.symbol)


def _preflight_budget(symbol_count: int, canonical_model: str) -> None:
    if symbol_count > _MAX_SYMBOLS_PER_CALL:
        raise RequestAttemptLimitError("Finviz local per-call request ceiling exceeded")
    scopes = current_request_scopes()
    if not scopes:
        return

    remaining_limits: list[int] = []
    now = datetime.now(timezone.utc)
    for scope in scopes:
        if scope.source != "finviz" or scope.canonical_model != canonical_model:
            return
        budget = scope.budget
        grant = next(
            (
                candidate
                for candidate in budget.grants
                if candidate.source == scope.source
                and candidate.canonical_model == scope.canonical_model
                and candidate.operation is scope.operation
            ),
            None,
        )
        if (
            grant is None
            or grant.decision is not GrantDecision.ALLOWED
            or grant.conditions
            or grant.expires_at <= now
            or _source.BASE_URL.removeprefix("https://") not in grant.allowed_hosts
        ):
            return
        task_used = budget.attempts_used
        source_used = budget.attempts_used_for("finviz")
        remaining_limits.extend(
            (
                budget.task_attempt_limit - task_used,
                budget.source_attempt_limit - source_used,
                grant.task_attempts - task_used,
                grant.source_attempts - source_used,
            )
        )
    if remaining_limits and symbol_count > min(remaining_limits):
        raise RequestAttemptLimitError("Finviz batch exceeds remaining governed request budget")


class _FinvizPerformanceFetcher(Fetcher[FinvizPerformanceQuery, tuple[_RetrievedPage, ...]]):
    """Shared bounded quote-page acquisition for the two Finviz identities."""

    async_mode: ClassVar[str] = "bounded_thread"
    query_type: ClassVar[type[FinvizPerformanceQuery]] = FinvizPerformanceQuery
    endpoint: ClassVar[str] = "/stock"
    canonical_model: ClassVar[str]
    capability: ClassVar[Capability]

    def transform_query(self, **kwargs: object) -> FinvizPerformanceQuery:
        return FinvizPerformanceQuery.model_validate(kwargs)

    def extract_data(
        self,
        params: FinvizPerformanceQuery,
        ctx: FetchContext,
    ) -> tuple[_RetrievedPage, ...]:
        symbols = _symbols(params)
        _preflight_budget(len(symbols), self.canonical_model)
        pages: list[_RetrievedPage] = []
        for symbol in symbols:
            query = urlencode({"t": symbol})
            body = _source.request_html(self.endpoint, {"t": symbol}, timeout=ctx.timeout)
            pages.append(
                _RetrievedPage(
                    requested_symbol=symbol,
                    source_url=f"{_source.BASE_URL}{self.endpoint}?{query}",
                    retrieved_at_utc=datetime.now(timezone.utc),
                    body=body,
                )
            )
        return tuple(pages)

    def _transform_pages(
        self, raw: tuple[_RetrievedPage, ...], *, require_etf: bool
    ) -> tuple[dict[str, object], ...]:
        return tuple(
            _parse_page(
                page.body,
                requested_symbol=page.requested_symbol,
                source_url=page.source_url,
                retrieved_at_utc=page.retrieved_at_utc,
                require_etf=require_etf,
            )
            for page in raw
        )


class FinvizPricePerformanceFetcher(_FinvizPerformanceFetcher):
    """Return bounded public Finviz quote-page returns for equity symbols."""

    canonical_model = "PricePerformance"
    row_model: ClassVar[type[ContractModel]] = FinvizPricePerformance
    capability = Capability(
        asset_class="stock",
        domain="finviz_price_performance",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public quote-page price-only returns; incomplete ODP fields remain null.",
    )

    def transform_data(
        self,
        raw: tuple[_RetrievedPage, ...],
        params: FinvizPerformanceQuery,
    ) -> FetchResult:
        """Normalize all equity pages into the provider-specific contract."""
        return tuple(
            FinvizPricePerformance.model_validate(record)
            for record in self._transform_pages(raw, require_etf=False)
        )


class FinvizEtfPricePerformanceFetcher(_FinvizPerformanceFetcher):
    """Return bounded public quote-page returns after exact ETF classification."""

    canonical_model = "EtfPricePerformance"
    row_model: ClassVar[type[ContractModel]] = FinvizEtfPricePerformance
    capability = Capability(
        asset_class="etf",
        domain="finviz_etf_price_performance",
        period="snapshot",
        market="us",
        source="finviz",
        verified=False,
        notes="Public quote-page price-only returns; exact ETF category is required.",
    )

    def transform_data(
        self,
        raw: tuple[_RetrievedPage, ...],
        params: FinvizPerformanceQuery,
    ) -> FetchResult:
        """Normalize pages only after each identity is verified as an ETF."""
        return tuple(
            FinvizEtfPricePerformance.model_validate(record)
            for record in self._transform_pages(raw, require_etf=True)
        )
