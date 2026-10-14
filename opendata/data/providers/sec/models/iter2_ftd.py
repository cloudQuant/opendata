"""SEC fails-to-deliver archive discovery and bounded pipe-delimited parsing."""

from __future__ import annotations

import csv
import io
import math
import re
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar, Literal
from urllib.parse import urljoin, urlparse

from pydantic import Field, ValidationError, field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.sec_iter2 import SecFtdRecord
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.sec.models._common import (
    MAX_TEXT_RESPONSE_BYTES,
    SecProviderError,
    ensure_request_capacity,
    fetch_bytes,
)

if TYPE_CHECKING:
    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet

SEC_FTD_PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
MAX_FTD_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_FTD_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_FTD_ROWS = 500_000
MAX_FTD_REPORTS = 31
MAX_FTD_SKIP_REPORTS = 2_000
_ARCHIVE_RE = re.compile(
    r"^cnsfails(?P<year>\d{4})(?P<month>0[1-9]|1[0-2])(?P<half>[ab])\.zip$",
    re.I,
)
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.$-]{1,20}$")


class EquityFtdQuery(QueryParams):
    """Select a bounded number of recent SEC half-month FTD reports."""

    symbol: str
    limit: int | None = Field(default=24, ge=1, le=MAX_FTD_REPORTS, strict=True)
    skip_reports: int | None = Field(default=0, ge=0, le=MAX_FTD_SKIP_REPORTS, strict=True)
    use_cache: bool | None = True
    market: Literal["us"] = "us"

    @field_validator("symbol")
    @classmethod
    def _valid_symbol(cls, value: str) -> str:
        value = value.strip().upper()
        if _SYMBOL_RE.fullmatch(value) is None:
            raise ValueError("symbol must be a short ticker token")
        return value

    @model_validator(mode="after")
    def _validate_public_options(self) -> EquityFtdQuery:
        if self.use_cache is False:
            raise ValueError("use_cache=False is unsupported by the governed SEC transport")
        return self


class _ArchiveLinkParser(HTMLParser):
    """Collect SEC-linked archive URLs without evaluating page markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self._href: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        values = dict(attrs)
        href = values.get("href")
        if isinstance(href, str):
            self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._href = None


def _archive_key(url: str) -> tuple[int, int, str] | None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {"sec.gov", "www.sec.gov"}:
        return None
    if not parsed.path.startswith("/files/data/fails-deliver-data/"):
        return None
    match = _ARCHIVE_RE.fullmatch(parsed.path.rsplit("/", 1)[-1])
    if match is None:
        return None
    half = "first" if match.group("half").casefold() == "a" else "second"
    return int(match.group("year")), int(match.group("month")), half


def parse_ftd_archive_links(body: bytes) -> dict[tuple[int, int, str], str]:
    """Read only SEC first/second-half archive links from its public index page."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise SecProviderError("SEC_FTD_PAGE_BAD_ENCODING") from None
    parser = _ArchiveLinkParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        raise SecProviderError("SEC_FTD_PAGE_BAD_HTML") from None
    result: dict[tuple[int, int, str], str] = {}
    for href in parser.links:
        absolute = urljoin(SEC_FTD_PAGE_URL, href)
        key = _archive_key(absolute)
        if key is None:
            continue
        previous = result.get(key)
        if previous is not None and previous != absolute:
            raise SecProviderError("SEC_FTD_ARCHIVE_LINK_CONFLICT")
        result[key] = absolute
    if not result:
        raise SecProviderError("SEC_FTD_ARCHIVE_INDEX_EMPTY")
    return result


def _required_archives(start: date, end: date) -> tuple[tuple[int, int, str], ...]:
    keys: list[tuple[int, int, str]] = []
    current = date(start.year, start.month, 1)
    while current <= end:
        next_month = date(
            current.year + (current.month == 12),
            1 if current.month == 12 else current.month + 1,
            1,
        )
        month_end = date.fromordinal(next_month.toordinal() - 1)
        first_half = (current.year, current.month, "first")
        second_half = (current.year, current.month, "second")
        if start <= min(date(current.year, current.month, 15), end):
            keys.append(first_half)
        if end >= date(current.year, current.month, 16) and start <= min(month_end, end):
            keys.append(second_half)
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return tuple(keys)


def _select_recent_archives(
    links: dict[tuple[int, int, str], str], *, skip_reports: int, limit: int
) -> tuple[str, ...]:
    ordered = sorted(links.items(), key=lambda item: item[0], reverse=True)
    return tuple(url for _key, url in ordered[skip_reports : skip_reports + limit])


def _parse_decimal(cell: str) -> Decimal | None:
    value = cell.strip()
    if not value:
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise SecProviderError("SEC_FTD_BAD_PRICE") from None
    if not result.is_finite() or result < 0:
        raise SecProviderError("SEC_FTD_BAD_PRICE")
    return result


def _public_float(value: Decimal | None) -> float | None:
    """Project a source decimal into the public v4 float field without hiding loss."""
    if value is None:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise SecProviderError("SEC_FTD_BAD_PRICE") from None
    if not math.isfinite(result) or (value != 0 and result == 0.0):
        raise SecProviderError("SEC_FTD_BAD_PRICE")
    return result


def parse_ftd_zip(
    body: bytes,
    *,
    symbol: str,
) -> tuple[SecFtdRecord, ...]:
    """Unzip one SEC archive and validate every source row before filtering."""
    if len(body) > MAX_FTD_ARCHIVE_BYTES:
        raise SecProviderError("SEC_FTD_ARCHIVE_TOO_LARGE")
    try:
        archive = zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, OSError):
        raise SecProviderError("SEC_FTD_BAD_ARCHIVE") from None
    with archive:
        files = [entry for entry in archive.infolist() if not entry.is_dir()]
        if len(files) != 1 or files[0].flag_bits & 0x1:
            raise SecProviderError("SEC_FTD_BAD_ARCHIVE")
        if files[0].file_size > MAX_FTD_UNCOMPRESSED_BYTES:
            raise SecProviderError("SEC_FTD_ARCHIVE_TOO_LARGE")
        try:
            raw = archive.read(files[0])
        except (RuntimeError, OSError, zipfile.BadZipFile):
            raise SecProviderError("SEC_FTD_BAD_ARCHIVE") from None
    if len(raw) > MAX_FTD_UNCOMPRESSED_BYTES:
        raise SecProviderError("SEC_FTD_ARCHIVE_TOO_LARGE")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise SecProviderError("SEC_FTD_BAD_ENCODING") from None
    records: list[SecFtdRecord] = []
    reader = csv.reader(io.StringIO(text, newline=""), delimiter="|", strict=True)
    try:
        for line_number, cells in enumerate(reader, start=1):
            if line_number == 1 and cells and cells[0].strip().casefold() == "settlement date":
                continue
            if not cells or all(not value.strip() for value in cells):
                continue
            if len(cells) != 6:
                raise SecProviderError("SEC_FTD_BAD_ROW")
            settlement, cusip, row_symbol, quantity, issuer, price = (
                value.strip() for value in cells
            )
            if not re.fullmatch(r"\d{8}", settlement):
                raise SecProviderError("SEC_FTD_BAD_DATE")
            try:
                settled = datetime.strptime(settlement, "%Y%m%d").date()
            except ValueError:
                raise SecProviderError("SEC_FTD_BAD_DATE") from None
            if not cusip or len(cusip) > 9 or not row_symbol or len(row_symbol) > 10 or not issuer:
                raise SecProviderError("SEC_FTD_BAD_ROW")
            if not quantity.isdigit():
                raise SecProviderError("SEC_FTD_BAD_QUANTITY")
            if line_number > MAX_FTD_ROWS:
                raise SecProviderError("SEC_FTD_TOO_MANY_ROWS")
            if row_symbol.casefold() != symbol.casefold():
                continue
            price_decimal = _parse_decimal(price)
            records.append(
                SecFtdRecord(
                    settlement_date=settled,
                    symbol=row_symbol.upper(),
                    cusip=cusip,
                    quantity=int(quantity),
                    price=_public_float(price_decimal),
                    description=issuer,
                    ticker=row_symbol.upper(),
                    quantity_fails=int(quantity),
                    issuer_name=issuer,
                    prior_close_price=price_decimal,
                )
            )
    except csv.Error:
        raise SecProviderError("SEC_FTD_BAD_DELIMITED_DATA") from None
    records.sort(key=lambda row: (row.settlement_date, row.cusip, row.ticker))
    return tuple(records)


class EquityFtdFetcher(Fetcher[EquityFtdQuery, tuple[bytes, ...]]):
    """Discover and download only the SEC FTD archive halves intersecting a date window."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "EquityFTD"
    row_model: ClassVar[type[ContractModel]] = SecFtdRecord
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="sec_equity_ftd",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> EquityFtdQuery:
        """Validate public ticker, report pagination, and injected identity fields."""
        unexpected = set(kwargs) - {
            "symbol",
            "limit",
            "skip_reports",
            "use_cache",
            "market",
            "source",
        }
        if unexpected:
            raise ValidationError.from_exception_data(
                "QueryParams",
                [
                    {"type": "extra_forbidden", "loc": (key,), "input": None}
                    for key in sorted(unexpected)
                ],
            )
        return EquityFtdQuery.model_validate(kwargs)

    def extract_data(self, params: EquityFtdQuery, ctx: FetchContext) -> tuple[bytes, ...]:
        """Fetch only the newest selected half-month report archives."""
        limit = params.limit or 24
        skip_reports = params.skip_reports or 0
        ensure_request_capacity(ctx, limit + 1)
        page = fetch_bytes(
            ctx, SEC_FTD_PAGE_URL, max_bytes=MAX_TEXT_RESPONSE_BYTES, transport=self.http_transport
        )
        links = parse_ftd_archive_links(page)
        selected = _select_recent_archives(links, skip_reports=skip_reports, limit=limit)
        return tuple(
            fetch_bytes(ctx, url, max_bytes=MAX_FTD_ARCHIVE_BYTES, transport=self.http_transport)
            for url in selected
        )

    def transform_data(self, raw: tuple[bytes, ...], params: EquityFtdQuery) -> FetchResult:
        """Parse selected report archives and retain matching public ticker rows."""
        rows: list[SecFtdRecord] = []
        for archive in raw:
            rows.extend(
                parse_ftd_zip(
                    archive,
                    symbol=params.symbol,
                )
            )
            if len(rows) > MAX_FTD_ROWS:
                raise SecProviderError("SEC_FTD_TOO_MANY_ROWS")
        rows.sort(key=lambda row: (row.settlement_date, row.symbol, row.cusip), reverse=True)
        return tuple(rows)


__all__ = ["EquityFtdFetcher", "EquityFtdQuery", "parse_ftd_archive_links", "parse_ftd_zip"]
