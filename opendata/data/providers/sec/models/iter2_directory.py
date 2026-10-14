"""SEC company/ticker maps, equity search, and the official SIC catalog."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar

from pydantic import Field, ValidationError, field_validator

if TYPE_CHECKING:
    from pydantic_core import InitErrorDetails

from opendata.data.capability import Capability
from opendata.data.models.sec_iter2 import (
    SecCikMapRecord,
    SecEquitySearchRecord,
    SecSicCode,
    SecSymbolMapRecord,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.sec.models._common import (
    SecProviderError,
    canonical_cik,
    company_ticker_rows,
    ensure_request_capacity,
    exchange_ticker_rows,
    fetch_bytes,
    fetch_json,
)

SEC_SIC_URL = "https://www.sec.gov/search-filings/standard-industrial-classification-sic-code-list"
SEC_FUND_TICKERS_URL = "https://www.sec.gov/files/company_tickers_mf.json"
MAX_DIRECTORY_ROWS = 50_000
MAX_SIC_ROWS = 20_000
MAX_PAGE_SIZE = 1_000
MAX_PAGE_OFFSET = 50_000
_TICKER_RE = re.compile(r"^[A-Za-z0-9.-]{1,20}$")
_RowT = TypeVar("_RowT")


def _reject_unexpected(kwargs: dict[str, object], allowed: set[str]) -> None:
    unexpected = set(kwargs) - allowed - {"source", "market"}
    if unexpected:
        errors: list[InitErrorDetails] = [
            {"type": "extra_forbidden", "loc": (key,), "input": None} for key in sorted(unexpected)
        ]
        raise ValidationError.from_exception_data("QueryParams", errors)


def _filter_page(rows: tuple[_RowT, ...], offset: int, limit: int) -> tuple[_RowT, ...]:
    if len(rows) > MAX_DIRECTORY_ROWS:
        raise SecProviderError("SEC_DIRECTORY_TOO_LARGE")
    return rows[offset : offset + limit]


def _fund_ticker_rows(ctx: FetchContext) -> tuple[dict[str, str | None], ...]:
    """Parse the SEC fund ticker file, whose rows identify ticker, CIK, series, and class."""
    document = fetch_json(ctx, SEC_FUND_TICKERS_URL)
    fields = document.get("fields")
    data = document.get("data")
    if (
        not isinstance(fields, list)
        or any(not isinstance(field, str) for field in fields)
        or len(fields) != len(set(fields))
        or not {"cik", "seriesId", "classId", "symbol"}.issubset(fields)
        or not isinstance(data, list)
        or not data
        or len(data) > MAX_DIRECTORY_ROWS
    ):
        raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
    positions = {field: fields.index(field) for field in ("cik", "seriesId", "classId", "symbol")}
    rows: list[dict[str, str | None]] = []
    identities: set[tuple[str, str, str, str]] = set()
    for raw in data:
        if not isinstance(raw, list) or len(raw) != len(fields):
            raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
        cik = canonical_cik(raw[positions["cik"]])
        series_id = raw[positions["seriesId"]]
        class_id = raw[positions["classId"]]
        ticker = raw[positions["symbol"]]
        if (
            not isinstance(series_id, str)
            or re.fullmatch(r"S\d{9}", series_id) is None
            or not isinstance(class_id, str)
            or re.fullmatch(r"C\d{9}", class_id) is None
            or not isinstance(ticker, str)
            or _TICKER_RE.fullmatch(ticker) is None
        ):
            raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
        normalized_ticker = ticker.upper()
        identity = (cik, series_id, class_id, normalized_ticker)
        if identity in identities:
            raise SecProviderError("SEC_TICKER_DIRECTORY_DUPLICATE")
        identities.add(identity)
        rows.append(
            {
                "cik": cik,
                "ticker": normalized_ticker,
                "name": None,
                "exchange": None,
                "series_id": series_id,
                "class_id": class_id,
            }
        )
    return tuple(sorted(rows, key=lambda row: (row["ticker"] or "", row["cik"] or "")))


def _validate_cache_option(query: QueryParams) -> None:
    if getattr(query, "use_cache", True) is False:
        raise ValueError("use_cache=False is unsupported by the governed SEC transport")


class CikMapQuery(QueryParams):
    """Lookup the SEC-published CIK for one public ticker symbol."""

    symbol: str
    market: Literal["us"] = "us"
    use_cache: bool | None = True
    offset: int = Field(default=0, ge=0, le=MAX_PAGE_OFFSET, strict=True)
    limit: int = Field(default=500, ge=1, le=MAX_PAGE_SIZE, strict=True)

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str) -> str:
        value = value.strip()
        if _TICKER_RE.fullmatch(value) is None:
            raise ValueError("symbol must be a short ticker token")
        return value.upper()


class CikMapFetcher(Fetcher[CikMapQuery, tuple[dict[str, str], ...]]):
    """Return SEC company-ticker associations as typed CIK map rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "CikMap"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_cik_map",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CikMapQuery:
        """Validate the public ticker lookup and local page bounds."""
        _reject_unexpected(kwargs, {"symbol", "offset", "limit", "use_cache"})
        params = CikMapQuery.model_validate(kwargs)
        _validate_cache_option(params)
        return params

    def extract_data(self, params: CikMapQuery, ctx: FetchContext) -> tuple[dict[str, str], ...]:
        """Fetch the SEC ticker directory after checking its one-request budget."""
        del params
        ensure_request_capacity(ctx, 1)
        return company_ticker_rows(ctx)

    def transform_data(self, raw: tuple[dict[str, str], ...], params: CikMapQuery) -> FetchResult:
        """Filter, page, and validate CIK-map output rows."""
        selected = tuple(row for row in raw if row["ticker"] == params.symbol)
        return tuple(
            SecCikMapRecord(ticker=row["ticker"], cik=row["cik"], name=row["name"])
            for row in _filter_page(selected, params.offset, params.limit)
        )


class SymbolMapQuery(QueryParams):
    """Search SEC company-to-symbol associations."""

    query: str
    market: Literal["us"] = "us"
    use_cache: bool | None = True
    offset: int = Field(default=0, ge=0, le=MAX_PAGE_OFFSET, strict=True)
    limit: int = Field(default=500, ge=1, le=MAX_PAGE_SIZE, strict=True)

    @field_validator("query")
    @classmethod
    def _valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("query must be nonempty and printable")
        if len(value) > 100:
            raise ValueError("query may not exceed 100 characters")
        return value


class SymbolMapFetcher(Fetcher[SymbolMapQuery, tuple[dict[str, str], ...]]):
    """Return SEC company-ticker associations as typed symbol map rows."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "SymbolMap"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_symbol_map",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SymbolMapQuery:
        """Validate the public search term and local page bounds."""
        _reject_unexpected(kwargs, {"query", "offset", "limit", "use_cache"})
        params = SymbolMapQuery.model_validate(kwargs)
        _validate_cache_option(params)
        return params

    def extract_data(self, params: SymbolMapQuery, ctx: FetchContext) -> tuple[dict[str, str], ...]:
        """Fetch the SEC ticker directory after checking its one-request budget."""
        del params
        ensure_request_capacity(ctx, 1)
        return company_ticker_rows(ctx)

    def transform_data(
        self, raw: tuple[dict[str, str], ...], params: SymbolMapQuery
    ) -> FetchResult:
        """Search ticker, issuer name, or CIK and return typed symbol rows."""
        needle = params.query.casefold()
        selected = tuple(
            row
            for row in raw
            if needle in row["ticker"].casefold()
            or needle in row["name"].casefold()
            or needle in row["cik"].casefold()
        )
        return tuple(
            SecSymbolMapRecord(
                symbol=row["ticker"], cik=row["cik"], ticker=row["ticker"], name=row["name"]
            )
            for row in _filter_page(selected, params.offset, params.limit)
        )


class EquitySearchQuery(QueryParams):
    """Substring search over the SEC's documented company/ticker/exchange file."""

    query: str | None = None
    is_symbol: bool | None = False
    is_fund: bool | None = False
    exchange: str | None = None
    market: Literal["us"] = "us"
    use_cache: bool | None = True
    offset: int = Field(default=0, ge=0, le=MAX_PAGE_OFFSET, strict=True)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_SIZE, strict=True)

    @field_validator("query", "exchange")
    @classmethod
    def _clean_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("search filters must be nonempty and printable")
        if len(value) > 100:
            raise ValueError("search filters may not exceed 100 characters")
        return value


class EquitySearchFetcher(Fetcher[EquitySearchQuery, tuple[dict[str, str | None], ...]]):
    """Search SEC ticker/exchange associations locally and page stable results."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "EquitySearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_equity_search",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquitySearchQuery:
        """Validate listing-search filters and local page bounds."""
        _reject_unexpected(
            kwargs, {"query", "is_symbol", "is_fund", "exchange", "offset", "limit", "use_cache"}
        )
        params = EquitySearchQuery.model_validate(kwargs)
        _validate_cache_option(params)
        if params.is_fund and params.exchange is not None:
            raise ValueError("exchange filtering is unsupported for SEC fund ticker rows")
        return params

    def extract_data(
        self, params: EquitySearchQuery, ctx: FetchContext
    ) -> tuple[dict[str, str | None], ...]:
        """Fetch the SEC exchange-ticker file after checking its one-request budget."""
        ensure_request_capacity(ctx, 1)
        if params.is_fund is True:
            return _fund_ticker_rows(ctx)
        return tuple(
            {**row, "series_id": None, "class_id": None} for row in exchange_ticker_rows(ctx)
        )

    def transform_data(
        self, raw: tuple[dict[str, str | None], ...], params: EquitySearchQuery
    ) -> FetchResult:
        """Search the published listing rows and return one typed page."""
        query = (params.query or "").casefold()
        exchange = (params.exchange or "").casefold()
        filtered = tuple(
            row
            for row in raw
            if (not query or _matches_equity_query(row, query, params.is_symbol is True))
            and (
                not exchange
                or (row["exchange"] is not None and exchange == row["exchange"].casefold())
            )
        )
        records: list[SecEquitySearchRecord] = []
        for row in _filter_page(filtered, params.offset, params.limit):
            ticker = row.get("ticker")
            cik = row.get("cik")
            if ticker is None or cik is None:
                raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
            records.append(
                SecEquitySearchRecord(
                    symbol=ticker,
                    name=row.get("name"),
                    cik=cik,
                    ticker=ticker,
                    exchange=row.get("exchange"),
                    series_id=row.get("series_id"),
                    class_id=row.get("class_id"),
                )
            )
        return tuple(records)


def _matches_equity_query(row: dict[str, str | None], query: str, is_symbol: bool) -> bool:
    ticker = (row.get("ticker") or "").casefold()
    name = (row.get("name") or "").casefold()
    if is_symbol:
        return ticker == query
    return query in ticker or query in name


class SicSearchQuery(QueryParams):
    """Search the SEC's official SIC code list."""

    query: str
    market: Literal["us"] = "us"
    use_cache: bool | None = True
    offset: int = Field(default=0, ge=0, le=MAX_PAGE_OFFSET, strict=True)
    limit: int = Field(default=100, ge=1, le=MAX_PAGE_SIZE, strict=True)

    @field_validator("query")
    @classmethod
    def _valid_query(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("query must be nonempty and printable")
        if len(value) > 100:
            raise ValueError("query may not exceed 100 characters")
        return value


class _SicTableParser(HTMLParser):
    """Extract complete three-cell rows from the SEC SIC table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[str, ...]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            value = " ".join("".join(self._cell).split())
            self._row.append(value)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(tuple(self._row))
            self._row = None


class SicSearchFetcher(Fetcher[SicSearchQuery, bytes]):
    """Parse and search the official SEC SIC table with strict row checks."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "SicSearch"
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="sec_sic_search",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SicSearchQuery:
        """Validate the public search term and local page bounds."""
        _reject_unexpected(kwargs, {"query", "offset", "limit", "use_cache"})
        params = SicSearchQuery.model_validate(kwargs)
        _validate_cache_option(params)
        return params

    def extract_data(self, params: SicSearchQuery, ctx: FetchContext) -> bytes:
        """Fetch the official SIC table after checking its one-request budget."""
        del params
        ensure_request_capacity(ctx, 1)
        return fetch_bytes(ctx, SEC_SIC_URL)

    def transform_data(self, raw: bytes, params: SicSearchQuery) -> FetchResult:
        """Parse the official SIC table and return typed matching rows."""
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise SecProviderError("SEC_SIC_BAD_ENCODING") from None
        parser = _SicTableParser()
        try:
            parser.feed(text)
            parser.close()
        except Exception:
            raise SecProviderError("SEC_SIC_BAD_HTML") from None
        if len(parser.rows) > MAX_SIC_ROWS:
            raise SecProviderError("SEC_SIC_TOO_MANY_ROWS")
        records: dict[int, SecSicCode] = {}
        for cells in parser.rows:
            if len(cells) != 3 or not cells[0].isdigit():
                continue
            code = int(cells[0])
            if not 1 <= code <= 9999 or not cells[1] or not cells[2]:
                raise SecProviderError("SEC_SIC_BAD_ROW")
            row = SecSicCode(
                sic=code,
                industry=cells[2],
                office=cells[1],
                sic_code=code,
                industry_title=cells[2],
            )
            previous = records.get(code)
            if previous is not None and previous != row:
                raise SecProviderError("SEC_SIC_DUPLICATE_CONFLICT")
            records[code] = row
        if not records:
            raise SecProviderError("SEC_SIC_EMPTY")
        needle = params.query.casefold()
        results = tuple(
            row
            for row in sorted(records.values(), key=lambda item: item.sic_code)
            if needle in str(row.sic).casefold()
            or needle in row.office.casefold()
            or needle in row.industry.casefold()
        )
        return _filter_page(results, params.offset, params.limit)


__all__ = [
    "CikMapFetcher",
    "EquitySearchFetcher",
    "SicSearchFetcher",
    "SymbolMapFetcher",
]
