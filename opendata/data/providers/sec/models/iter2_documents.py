"""Typed SEC filing, filing-document, taxonomy, and disclosure fetchers."""

from __future__ import annotations

import email.utils
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from typing import TYPE_CHECKING, ClassVar, Literal, TypeVar
from urllib.parse import urljoin, urlsplit

# Used only for runtime isinstance; XML parsing uses defusedxml SafeET below.
from xml.etree.ElementTree import Element  # nosec B405

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pydantic import Field, ValidationError, field_validator, model_validator

if TYPE_CHECKING:
    from pydantic_core import InitErrorDetails

    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet

from opendata.data.capability import Capability
from opendata.data.models.sec_documents import (
    SecCompanyFiling,
    SecFilingCoverPage,
    SecFilingDocument,
    SecFilingHeader,
    SecForm13FHRHolding,
    SecHtmFileData,
    SecLatestFinancialReport,
    SecManagementDiscussionAnalysis,
    SecNportDisclosureHolding,
    SecNportFiling,
    SecRssLitigationItem,
    SecSchemaFile,
)
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.sec.models._common import (
    MAX_TEXT_RESPONSE_BYTES,
    MAX_XML_RESPONSE_BYTES,
    SecProviderError,
    canonical_cik,
    company_ticker_rows,
    ensure_request_capacity,
    fetch_bytes,
    fetch_json,
    resolve_ticker,
)

_MAX_PAGE = 1_000
_MAX_HISTORY_FILES = 4
_MAX_HOLDINGS = 20_000
_FORM_13F_NEAREST_DOLLAR_EFFECTIVE_DATE = date(2023, 1, 3)
_TICKER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,19}$")
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_ARCHIVE_ROOT = "https://www.sec.gov/Archives/edgar/data"
_SUBMISSION_ROOT = "https://data.sec.gov/submissions"
_MF_TICKER_URL = "https://www.sec.gov/files/company_tickers_mf.json"
_LITIGATION_RSS_URL = (
    "https://www.sec.gov/enforcement-litigation/litigation-releases/rss"
    "?month=All&populate=labs&year=All"
)
_FINANCIAL_FORMS = frozenset(
    {"1-K", "1-SA", "1-U", "10-D", "10-K", "10-KT", "10-Q", "10-QT", "20-F", "40-F", "6-K", "8-K"}
)

_RowT = TypeVar("_RowT")


def _reject_unexpected(kwargs: dict[str, object], allowed: set[str]) -> None:
    unexpected = set(kwargs) - allowed - {"source", "market"}
    if unexpected:
        errors: list[InitErrorDetails] = [
            {"type": "extra_forbidden", "loc": (key,), "input": None} for key in sorted(unexpected)
        ]
        raise ValidationError.from_exception_data("QueryParams", errors)


def _query_cik(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,10}", value.strip()):
        raise ValueError("cik must be one to ten decimal digits")
    digits = value.strip()
    if int(digits) <= 0:
        raise ValueError("cik must be positive")
    return digits.zfill(10)


def _query_ticker(value: str | None) -> str | None:
    if value is None:
        return None
    ticker = value.strip().upper()
    if _TICKER_RE.fullmatch(ticker) is None:
        raise ValueError("symbol must be a short ticker token")
    return ticker


def _query_date(value: object) -> object:
    if isinstance(value, datetime):
        raise ValueError("SEC date filters must not include a time")
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return value
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("SEC date filters must be ISO dates") from exc
    if parsed.isoformat() != value:
        raise ValueError("SEC date filters must be ISO dates")
    return parsed


def _validate_identity(symbol: str | None, cik: str | None) -> None:
    if (symbol is None) == (cik is None):
        raise ValueError("provide exactly one of symbol or cik")


def _date_value(value: object, *, required: bool = False) -> date | None:
    if value is None or value == "":
        if required:
            raise SecProviderError("SEC_SUBMISSION_ROW_MISSING_DATE")
        return None
    if not isinstance(value, str):
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_DATE")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_DATE") from None
    if parsed.isoformat() != value:
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_DATE")
    return parsed


def _required_date(value: object) -> date:
    parsed = _date_value(value, required=True)
    if parsed is None:
        raise SecProviderError("SEC_SUBMISSION_ROW_MISSING_DATE")
    return parsed


def _date_time_value(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_ACCEPTED_TIME")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(candidate)
    except ValueError:
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_ACCEPTED_TIME") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result


def _text_value(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or any(ord(char) < 32 and char not in "\t\r\n" for char in value):
        raise SecProviderError("SEC_SUBMISSION_ROW_BAD_TEXT")
    return value.strip() or None


def _fiscal_year_end_text(value: object) -> str | None:
    """Render the Submissions API's ``fiscalYearEnd`` as the month/day text pair it publishes.

    EDGAR's Submissions document carries ``fiscalYearEnd`` as an object with ``month`` and
    ``day`` keys, while some mirrored payloads publish the same value as one text token. Both are
    normalized onto the contract's text column; a payload naming neither is refused.
    """
    if isinstance(value, Mapping):
        month = _text_value(value.get("month"))
        day = _text_value(value.get("day"))
        if month is None or day is None:
            raise SecProviderError("SEC_SUBMISSIONS_BAD_FISCAL_YEAR_END")
        return f"{month.zfill(2)}{day.zfill(2)}"
    return _text_value(value)


def _validate_filename(value: str, code: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", value) is None:
        raise SecProviderError(code)
    return value


def _parse_int(value: object, *, code: str, allow_none: bool = True) -> int | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise SecProviderError(code)
    if isinstance(value, bool):
        raise SecProviderError(code)
    try:
        result = int(str(value).replace(",", "").strip())
    except (TypeError, ValueError, OverflowError):
        raise SecProviderError(code) from None
    if result < 0:
        raise SecProviderError(code)
    return result


def _resolve_cik(
    ctx: FetchContext,
    symbol: str | None,
    cik: str | None,
    *,
    transport: HttpGet | None = None,
) -> tuple[str, str | None]:
    if cik is not None:
        return canonical_cik(cik), None
    if symbol is None:
        raise SecProviderError("SEC_SYMBOL_REQUIRED")
    row = resolve_ticker(company_ticker_rows(ctx, transport=transport), symbol)
    return row["cik"], row["name"]


def _submission_url(cik: str) -> str:
    return f"{_SUBMISSION_ROOT}/CIK{cik}.json"


def _archive_base(cik: str, accession: str) -> str:
    if _ACCESSION_RE.fullmatch(accession) is None:
        raise SecProviderError("SEC_BAD_ACCESSION")
    return f"{_ARCHIVE_ROOT}/{int(cik)}/{accession.replace('-', '')}"


def _complete_submission_url(cik: str, accession: str) -> str:
    return f"{_archive_base(cik, accession)}.txt"


def _filing_detail_url(cik: str, accession: str) -> str:
    return f"{_archive_base(cik, accession)}-index.htm"


def _accession_parts(value: str) -> tuple[str, str]:
    if _ACCESSION_RE.fullmatch(value) is None:
        raise ValueError("accession_number must use the SEC accession format")
    return value[:10], value


def _archive_identity(url: str, *, require_document: bool) -> tuple[str, str, str | None]:
    parts = urlsplit(url)
    path_parts = parts.path.split("/")
    if (
        parts.scheme != "https"
        or parts.hostname != "www.sec.gov"
        or parts.username is not None
        or parts.password is not None
        or parts.port is not None
        or parts.query
        or parts.fragment
        or "%" in parts.path
        or any(part in {".", ".."} for part in path_parts)
        or len(path_parts) < 6
        or path_parts[1:4] != ["Archives", "edgar", "data"]
        or not path_parts[4].isdigit()
    ):
        raise ValueError("url must be a canonical SEC filing archive URL")
    cik = _query_cik(path_parts[4])
    if _ACCESSION_RE.fullmatch(path_parts[5]) is not None:
        accession = path_parts[5]
    elif re.fullmatch(r"\d{18}", path_parts[5]):
        compact = path_parts[5]
        accession = f"{compact[:10]}-{compact[10:12]}-{compact[12:]}"
    else:
        raise ValueError("url must contain a SEC accession folder")
    filename = path_parts[6] if len(path_parts) == 7 and path_parts[6] else None
    if len(path_parts) > 7 or (require_document and filename is None):
        raise ValueError("url must identify one filing document")
    if filename is not None and (
        not filename
        or filename in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", filename) is None
    ):
        raise ValueError("url document name is invalid")
    if (
        require_document
        and filename is not None
        and not filename.lower().endswith((".htm", ".html"))
    ):
        raise ValueError("url must identify an HTML filing document")
    return cik, accession, filename


@dataclass(frozen=True)
class _FilingRow:
    filing_date: date
    report_date: date | None
    form: str
    accession: str
    primary_document: str | None
    primary_description: str | None
    items: str | None
    file_number: str | None
    film_number: str | None
    inline_xbrl: bool | None
    xbrl: bool | None
    size: int | None
    accepted: datetime | None


@dataclass(frozen=True)
class _Parsed13FHolding:
    issuer: str
    cusip: str
    asset_class: str
    security_type: Literal["SH", "PRN"] | None
    option_type: Literal["call", "put"] | None
    investment_discretion: str | None
    other_manager: str | None
    voting_authority_sole: int | None
    voting_authority_shared: int | None
    voting_authority_none: int | None
    principal_amount: int
    value: int


def _parse_bool(value: object, code: str) -> bool | None:
    if value is None or value == "":
        return None
    if value in (True, 1, "1", "true", "True"):
        return True
    if value in (False, 0, "0", "false", "False"):
        return False
    raise SecProviderError(code)


def _parse_filing_arrays(
    document: object,
) -> tuple[str, str, tuple[_FilingRow, ...], list[dict[str, object]]]:
    if not isinstance(document, dict):
        raise SecProviderError("SEC_SUBMISSIONS_BAD_SHAPE")
    cik = canonical_cik(document.get("cik"))
    name = _text_value(document.get("name")) or _text_value(document.get("entityName"))
    if name is None:
        raise SecProviderError("SEC_SUBMISSIONS_MISSING_NAME")
    filings = document.get("filings")
    if not isinstance(filings, dict) or not isinstance(filings.get("recent"), dict):
        raise SecProviderError("SEC_SUBMISSIONS_BAD_SHAPE")
    recent = filings["recent"]
    required = ("filingDate", "form", "accessionNumber")
    arrays: dict[str, list[object]] = {}
    for key in required:
        value = recent.get(key)
        if not isinstance(value, list):
            raise SecProviderError("SEC_SUBMISSIONS_BAD_SHAPE")
        arrays[key] = value
    row_count = len(arrays["filingDate"])
    if any(len(arrays[key]) != row_count for key in required):
        raise SecProviderError("SEC_SUBMISSIONS_BAD_SHAPE")
    optional_keys = (
        "reportDate",
        "acceptanceDateTime",
        "primaryDocument",
        "primaryDocDescription",
        "items",
        "fileNumber",
        "filmNumber",
        "isInlineXBRL",
        "isXBRL",
        "size",
    )
    for key in optional_keys:
        value = recent.get(key)
        if value is None:
            arrays[key] = [None] * row_count
        elif not isinstance(value, list) or len(value) != row_count:
            raise SecProviderError("SEC_SUBMISSIONS_BAD_SHAPE")
        else:
            arrays[key] = value

    rows: list[_FilingRow] = []
    for index in range(row_count):
        form = _text_value(arrays["form"][index])
        accession = _text_value(arrays["accessionNumber"][index])
        if form is None or accession is None or _ACCESSION_RE.fullmatch(accession) is None:
            raise SecProviderError("SEC_SUBMISSIONS_BAD_ROW")
        size = _parse_int(arrays["size"][index], code="SEC_SUBMISSIONS_BAD_SIZE")
        rows.append(
            _FilingRow(
                filing_date=_required_date(arrays["filingDate"][index]),
                report_date=_date_value(arrays["reportDate"][index]),
                form=form,
                accession=accession,
                primary_document=_text_value(arrays["primaryDocument"][index]),
                primary_description=_text_value(arrays["primaryDocDescription"][index]),
                items=_text_value(arrays["items"][index]),
                file_number=_text_value(arrays["fileNumber"][index]),
                film_number=_text_value(arrays["filmNumber"][index]),
                inline_xbrl=_parse_bool(arrays["isInlineXBRL"][index], "SEC_SUBMISSIONS_BAD_FLAG"),
                xbrl=_parse_bool(arrays["isXBRL"][index], "SEC_SUBMISSIONS_BAD_FLAG"),
                size=size,
                accepted=_date_time_value(arrays["acceptanceDateTime"][index]),
            )
        )

    raw_files = filings.get("files", [])
    if raw_files is None:
        raw_files = []
    if not isinstance(raw_files, list):
        raise SecProviderError("SEC_SUBMISSIONS_BAD_HISTORY")
    history: list[dict[str, object]] = []
    for row in raw_files:
        if not isinstance(row, dict):
            raise SecProviderError("SEC_SUBMISSIONS_BAD_HISTORY")
        filename = row.get("name")
        if (
            not isinstance(filename, str)
            or re.fullmatch(r"CIK\d{10}-submissions-\d{3}\.json", filename) is None
        ):
            raise SecProviderError("SEC_SUBMISSIONS_BAD_HISTORY")
        history.append(row)
    return cik, name, tuple(rows), history


def _load_filings(
    ctx: FetchContext,
    *,
    symbol: str | None,
    cik: str | None,
    start_date: date | None,
    end_date: date | None,
    history_pages: int = 0,
    max_requests: int | None = None,
    transport: HttpGet | None = None,
) -> tuple[str, str, tuple[_FilingRow, ...]]:
    request_bound = (1 if symbol is not None else 0) + 1 + history_pages
    if max_requests is not None:
        request_bound = max(request_bound, max_requests)
    ensure_request_capacity(ctx, request_bound)
    resolved_cik, resolved_name = _resolve_cik(ctx, symbol, cik, transport=transport)
    document = fetch_json(ctx, _submission_url(resolved_cik), transport=transport)
    parsed_cik, name, recent, history = _parse_filing_arrays(document)
    if parsed_cik != resolved_cik:
        raise SecProviderError("SEC_SUBMISSIONS_CIK_MISMATCH")
    selected_rows = list(recent)
    filtered = _filter_filing_dates(recent, start_date, end_date)
    if start_date is not None and recent:
        earliest = min(row.filing_date for row in recent)
        if start_date < earliest:
            overlapping = []
            for item in history:
                filing_from = _date_value(item.get("filingFrom"))
                filing_to = _date_value(item.get("filingTo"))
                if filing_to is not None and filing_to < start_date:
                    continue
                if end_date is not None and filing_from is not None and filing_from > end_date:
                    continue
                overlapping.append(item)
            # SEC publishes historical chunks newest first. Keep this ordering and refuse a
            # partial result if the caller's explicit history bound cannot reach the requested
            # window.
            overlapping.sort(key=lambda item: str(item.get("name")), reverse=True)
            if not overlapping and not history:
                raise SecProviderError("SEC_SUBMISSIONS_HISTORY_UNAVAILABLE")
            if len(overlapping) > history_pages:
                raise SecProviderError("SEC_HISTORY_PAGE_LIMIT_EXCEEDED")
            for item in overlapping:
                filename = item["name"]
                historical = fetch_json(ctx, f"{_SUBMISSION_ROOT}/{filename}", transport=transport)
                hist_cik, _, hist_rows, _ = _parse_filing_arrays(historical)
                if hist_cik != resolved_cik:
                    raise SecProviderError("SEC_SUBMISSIONS_CIK_MISMATCH")
                selected_rows.extend(hist_rows)
            filtered = _filter_filing_dates(tuple(selected_rows), start_date, end_date)
    return resolved_cik, resolved_name or name, tuple(filtered)


def _filter_filing_dates(
    rows: tuple[_FilingRow, ...], start_date: date | None, end_date: date | None
) -> tuple[_FilingRow, ...]:
    return tuple(
        sorted(
            (
                row
                for row in rows
                if (start_date is None or row.filing_date >= start_date)
                and (end_date is None or row.filing_date <= end_date)
            ),
            key=lambda row: (row.filing_date, row.accession),
            reverse=True,
        )
    )


def _company_filing_record(cik: str, row: _FilingRow) -> SecCompanyFiling:
    base = _archive_base(cik, row.accession)
    primary = row.primary_document
    if primary is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", primary) is None:
        raise SecProviderError("SEC_SUBMISSIONS_BAD_PRIMARY_DOCUMENT")
    report_url = f"{base}/{primary}" if primary else _filing_detail_url(cik, row.accession)
    return SecCompanyFiling(
        filing_date=row.filing_date,
        report_type=row.form,
        report_url=report_url,
        report_date=row.report_date,
        act=None,
        items=row.items,
        primary_doc_description=row.primary_description,
        primary_doc=primary,
        accession_number=row.accession,
        file_number=row.file_number,
        film_number=row.film_number,
        is_inline_xbrl=None if row.inline_xbrl is None else int(row.inline_xbrl),
        is_xbrl=None if row.xbrl is None else int(row.xbrl),
        size=row.size,
        complete_submission_url=_complete_submission_url(cik, row.accession),
        filing_detail_url=_filing_detail_url(cik, row.accession),
    )


def _filtered_rows(
    rows: tuple[_FilingRow, ...], form: str | None, offset: int, limit: int
) -> tuple[_FilingRow, ...]:
    filtered = tuple(row for row in rows if form is None or row.form.casefold() == form.casefold())
    if offset >= len(filtered):
        return ()
    return filtered[offset : offset + limit]


class CompanyFilingsQuery(QueryParams):
    """Query parameters for SEC issuer filing history."""

    symbol: str | None = None
    cik: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    form: str | None = None
    offset: int = Field(default=0, ge=0, le=50_000, strict=True)
    limit: int = Field(default=100, ge=1, le=_MAX_PAGE, strict=True)
    history_pages: int = Field(default=0, ge=0, le=_MAX_HISTORY_FILES, strict=True)

    @field_validator("symbol")
    @classmethod
    def _ticker(cls, value: str | None) -> str | None:
        return _query_ticker(value)

    @field_validator("cik", mode="before")
    @classmethod
    def _cik(cls, value: object) -> object:
        return None if value is None else _query_cik(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _dates(cls, value: object) -> object:
        return _query_date(value)

    @field_validator("form")
    @classmethod
    def _form(cls, value: str | None) -> str | None:
        if value is None:
            return None
        form = value.strip().upper()
        if re.fullmatch(r"[0-9A-Z][0-9A-Z/-]{0,19}", form) is None:
            raise ValueError("form must be a short SEC form token")
        return form

    @model_validator(mode="after")
    def _valid_range(self) -> CompanyFilingsQuery:
        _validate_identity(self.symbol, self.cik)
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("start_date must not follow end_date")
        return self


class CompanyFilingsFetcher(Fetcher[CompanyFilingsQuery, tuple[SecCompanyFiling, ...]]):
    """Fetch company filing history from SEC submissions."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "CompanyFilings"
    row_model: ClassVar[type[ContractModel]] = SecCompanyFiling
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_company_filings",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> CompanyFilingsQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(
            kwargs,
            {"symbol", "cik", "start_date", "end_date", "form", "offset", "limit", "history_pages"},
        )
        return CompanyFilingsQuery.model_validate(kwargs)

    def extract_data(
        self, params: CompanyFilingsQuery, ctx: FetchContext
    ) -> tuple[SecCompanyFiling, ...]:
        """Fetch SEC source data and parse it into typed models."""
        cik, _, rows = _load_filings(
            ctx,
            symbol=params.symbol,
            cik=params.cik,
            start_date=params.start_date,
            end_date=params.end_date,
            history_pages=params.history_pages,
            transport=self.http_transport,
        )
        return tuple(
            _company_filing_record(cik, row)
            for row in _filtered_rows(rows, params.form, params.offset, params.limit)
        )

    def transform_data(
        self, raw: tuple[SecCompanyFiling, ...], params: CompanyFilingsQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw


class FilingsFetcher(CompanyFilingsFetcher):
    """The ``Filings`` SEC identity, matching the independent company filing route."""

    canonical_model: ClassVar[str] = "Filings"
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_filings",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )


class SecFilingQuery(QueryParams):
    """Query parameters for one SEC filing package."""

    url: str | None = None
    cik: str | None = None
    accession_number: str | None = None

    @field_validator("cik", mode="before")
    @classmethod
    def _cik(cls, value: object) -> object:
        return None if value is None else _query_cik(value)

    @field_validator("accession_number")
    @classmethod
    def _accession(cls, value: str | None) -> str | None:
        if value is not None and _ACCESSION_RE.fullmatch(value) is None:
            raise ValueError("accession_number must use the SEC accession format")
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        if value is not None:
            _archive_identity(value, require_document=False)
        return value

    @model_validator(mode="after")
    def _one_source(self) -> SecFilingQuery:
        if self.url is not None:
            if self.cik is not None or self.accession_number is not None:
                raise ValueError("url cannot be combined with cik or accession_number")
        elif self.cik is None or self.accession_number is None:
            raise ValueError("provide url or both cik and accession_number")
        return self


class SecFilingFetcher(Fetcher[SecFilingQuery, SecFilingHeader]):
    """Fetch one SEC filing header and document index."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "SecFiling"
    row_model: ClassVar[type[ContractModel]] = SecFilingHeader
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_filing",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SecFilingQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"url", "cik", "accession_number"})
        return SecFilingQuery.model_validate(kwargs)

    def extract_data(self, params: SecFilingQuery, ctx: FetchContext) -> SecFilingHeader:
        """Fetch SEC source data and parse it into typed models."""
        if params.url is not None:
            cik, accession, _ = _archive_identity(params.url, require_document=False)
        else:
            if params.cik is None or params.accession_number is None:
                raise SecProviderError("SEC_FILING_QUERY_IDENTITY_MISSING")
            cik, accession = params.cik, params.accession_number
        ensure_request_capacity(ctx, 2)
        submission = fetch_json(ctx, _submission_url(cik), transport=self.http_transport)
        parsed_cik, name, rows, _ = _parse_filing_arrays(submission)
        if parsed_cik != cik:
            raise SecProviderError("SEC_SUBMISSIONS_CIK_MISMATCH")
        filing = next((row for row in rows if row.accession == accession), None)
        if filing is None:
            raise SecProviderError("SEC_FILING_NOT_IN_SUBMISSIONS")
        index_url = f"{_archive_base(cik, accession)}/index.json"
        index = fetch_json(ctx, index_url, transport=self.http_transport)
        documents = _parse_index_documents(index, cik, accession)
        submission_meta = submission
        tickers_value = (
            submission_meta.get("tickers", []) if isinstance(submission_meta, dict) else []
        )
        tickers = (
            tuple(item for item in tickers_value if isinstance(item, str))
            if isinstance(tickers_value, list)
            else ()
        )
        sic = _text_value(submission_meta.get("sic"))
        sic_name = _text_value(submission_meta.get("sicDescription"))
        if sic is None or sic_name is None:
            raise SecProviderError("SEC_SUBMISSIONS_MISSING_SIC")
        fiscal_year_end = _fiscal_year_end_text(submission_meta.get("fiscalYearEnd"))
        cover_page = (
            SecFilingCoverPage(
                registrant_name=name,
                cik=cik,
                fiscal_year_end=fiscal_year_end,
                commission_file_number=filing.file_number,
                trading_symbols=tickers,
            )
            if filing.primary_document is not None
            else None
        )
        return SecFilingHeader(
            base_url=_archive_base(cik, accession) + "/",
            name=name,
            cik=cik,
            trading_symbols=tickers,
            sic=sic,
            sic_organization_name=sic_name,
            accession_number=accession,
            filing_date=filing.filing_date,
            period_ending=filing.report_date,
            fiscal_year_end=fiscal_year_end,
            document_type=filing.form,
            has_cover_page=cover_page is not None,
            description=filing.primary_description,
            cover_page=cover_page,
            document_urls=tuple(item.url for item in documents),
            documents=documents,
        )

    def transform_data(self, raw: SecFilingHeader, params: SecFilingQuery) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return (raw,)


def _parse_index_documents(
    document: object, cik: str, accession: str
) -> tuple[SecFilingDocument, ...]:
    if not isinstance(document, dict):
        raise SecProviderError("SEC_FILING_INDEX_BAD_SHAPE")
    directory = document.get("directory")
    items = directory.get("item") if isinstance(directory, dict) else None
    if not isinstance(items, list) or len(items) > 20_000:
        raise SecProviderError("SEC_FILING_INDEX_BAD_SHAPE")
    base = _archive_base(cik, accession)
    documents: list[SecFilingDocument] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise SecProviderError("SEC_FILING_INDEX_BAD_ROW")
        name = raw.get("name")
        if (
            not isinstance(name, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", name) is None
        ):
            raise SecProviderError("SEC_FILING_INDEX_BAD_NAME")
        size = _parse_int(raw.get("size"), code="SEC_FILING_INDEX_BAD_SIZE")
        documents.append(
            SecFilingDocument(
                name=name,
                document_type=_text_value(raw.get("type")),
                description=_text_value(raw.get("description")),
                size=size,
                url=f"{base}/{name}",
            )
        )
    return tuple(documents)


class SecHtmFileQuery(QueryParams):
    """Query parameters for one archived SEC HTML document."""

    url: str

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        _archive_identity(value, require_document=True)
        return value


class SecHtmFileFetcher(Fetcher[SecHtmFileQuery, SecHtmFileData]):
    """Fetch a bounded SEC HTML filing document."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "SecHtmFile"
    row_model: ClassVar[type[ContractModel]] = SecHtmFileData
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_htm_file",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SecHtmFileQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"url"})
        return SecHtmFileQuery.model_validate(kwargs)

    def extract_data(self, params: SecHtmFileQuery, ctx: FetchContext) -> SecHtmFileData:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 1)
        body = fetch_bytes(
            ctx, params.url, max_bytes=MAX_TEXT_RESPONSE_BYTES, transport=self.http_transport
        )
        try:
            content = body.decode("utf-8")
        except UnicodeDecodeError:
            raise SecProviderError("SEC_HTML_BAD_ENCODING") from None
        try:
            return SecHtmFileData(url=params.url, content=content)
        except ValidationError:
            raise SecProviderError("SEC_HTML_CONTRACT_INVALID") from None

    def transform_data(self, raw: SecHtmFileData, params: SecHtmFileQuery) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return (raw,)


class RssLitigationQuery(QueryParams):
    """Query parameters for the SEC litigation RSS feed."""

    limit: int = Field(default=100, ge=1, le=_MAX_PAGE, strict=True)


class RssLitigationFetcher(Fetcher[RssLitigationQuery, tuple[SecRssLitigationItem, ...]]):
    """Fetch SEC litigation release items from the public RSS feed."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "RssLitigation"
    row_model: ClassVar[type[ContractModel]] = SecRssLitigationItem
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="public_data",
        domain="sec_rss_litigation",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> RssLitigationQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"limit"})
        return RssLitigationQuery.model_validate(kwargs)

    def extract_data(
        self, params: RssLitigationQuery, ctx: FetchContext
    ) -> tuple[SecRssLitigationItem, ...]:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 1)
        body = fetch_bytes(
            ctx,
            _LITIGATION_RSS_URL,
            max_bytes=MAX_XML_RESPONSE_BYTES,
            transport=self.http_transport,
        )
        root = _safe_xml(body, "SEC_RSS_BAD_XML")
        candidates = [
            element
            for element in root.iter()
            if _local_name(element.tag).casefold() in {"item", "entry"}
        ]
        if len(candidates) > 10_000:
            raise SecProviderError("SEC_RSS_TOO_MANY_ITEMS")
        records: list[SecRssLitigationItem] = []
        for item in candidates[: params.limit]:
            title = _first_xml_text(item, ("title",))
            summary = _first_xml_text(item, ("description", "summary", "content"))
            published_text = _first_xml_text(item, ("pubDate", "published", "updated", "date"))
            link = _first_xml_text(item, ("link",))
            for child in item.iter():
                if _local_name(child.tag).casefold() == "link" and not link:
                    link = child.attrib.get("href")
            item_id = _first_xml_text(item, ("guid", "id")) or link
            if not title or not published_text or not link or not item_id:
                raise SecProviderError("SEC_RSS_BAD_ITEM")
            records.append(
                SecRssLitigationItem(
                    published=_parse_rss_date(published_text),
                    title=_plain_text(title),
                    summary=_plain_text(summary or title),
                    id=item_id,
                    link=link,
                )
            )
        return tuple(records)

    def transform_data(
        self, raw: tuple[SecRssLitigationItem, ...], params: RssLitigationQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw


def _parse_rss_date(value: str) -> datetime:
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise SecProviderError("SEC_RSS_BAD_DATE") from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SchemaFilesQuery(QueryParams):
    """Query parameters for a published SEC XBRL taxonomy."""

    taxonomy: Literal["dei", "ecd", "cyd", "cef", "fnd", "oef", "sbs"] = "dei"
    year: int = Field(default=2026, ge=2012, le=2200, strict=True)
    offset: int = Field(default=0, ge=0, le=100_000, strict=True)
    limit: int = Field(default=500, ge=1, le=_MAX_PAGE, strict=True)


class SchemaFilesFetcher(Fetcher[SchemaFilesQuery, tuple[SecSchemaFile, ...]]):
    """Fetch SEC XBRL taxonomy elements and labels."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "SchemaFiles"
    row_model: ClassVar[type[ContractModel]] = SecSchemaFile
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="public_data",
        domain="sec_schema_files",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> SchemaFilesQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"taxonomy", "year", "offset", "limit"})
        return SchemaFilesQuery.model_validate(kwargs)

    def extract_data(
        self, params: SchemaFilesQuery, ctx: FetchContext
    ) -> tuple[SecSchemaFile, ...]:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 2)
        base = f"https://xbrl.sec.gov/{params.taxonomy}/{params.year}"
        schema_url = f"{base}/{params.taxonomy}-{params.year}.xsd"
        body = fetch_bytes(
            ctx, schema_url, max_bytes=MAX_XML_RESPONSE_BYTES, transport=self.http_transport
        )
        root = _safe_xml(body, "SEC_SCHEMA_BAD_XML")
        label_urls = _label_linkbase_urls(root, schema_url, params.taxonomy, params.year)
        labels: dict[str, str] = {}
        if label_urls:
            label_body = fetch_bytes(
                ctx,
                label_urls[0],
                max_bytes=MAX_XML_RESPONSE_BYTES,
                transport=self.http_transport,
            )
            labels = _parse_labels(
                _safe_xml(label_body, "SEC_SCHEMA_BAD_LABEL_XML"), params.taxonomy
            )
        elements = [element for element in root.iter() if _local_name(element.tag) == "element"]
        if len(elements) > 100_000:
            raise SecProviderError("SEC_SCHEMA_TOO_MANY_ELEMENTS")
        records: list[SecSchemaFile] = []
        for element in elements:
            name = element.attrib.get("name")
            if not isinstance(name, str) or not name:
                continue
            documentation = _first_xml_text(element, ("documentation",))
            attrs = {_local_name(key): value for key, value in element.attrib.items()}
            records.append(
                SecSchemaFile(
                    name=name,
                    label=labels.get(name, name),
                    description=documentation,
                    category=None,
                    style=None,
                    has_label_linkbase=bool(label_urls),
                    url=schema_url,
                    xbrl_type=attrs.get("type"),
                    period_type=_schema_period_type(attrs.get("periodType")),
                    balance_type=_schema_balance_type(attrs.get("balance")),
                    abstract=_xml_bool(attrs.get("abstract")),
                    substitution_group=attrs.get("substitutionGroup"),
                    nillable=_xml_bool(attrs.get("nillable")),
                )
            )
        if not records:
            raise SecProviderError("SEC_SCHEMA_EMPTY")
        records.sort(key=lambda record: record.name.casefold())
        return tuple(records[params.offset : params.offset + params.limit])

    def transform_data(
        self, raw: tuple[SecSchemaFile, ...], params: SchemaFilesQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw


def _label_linkbase_urls(
    root: Element[str], schema_url: str, taxonomy: str, year: int
) -> tuple[str, ...]:
    urls: list[str] = []
    for element in root.iter():
        if _local_name(element.tag) != "linkbaseRef":
            continue
        attrs = {_local_name(key): value for key, value in element.attrib.items()}
        href = attrs.get("href")
        role = attrs.get("role", "")
        if isinstance(href, str) and ("label" in role.casefold() or "_lab" in href.casefold()):
            candidate = urljoin(schema_url, href)
            parts = urlsplit(candidate)
            if (
                parts.hostname == "xbrl.sec.gov"
                and parts.path.startswith(f"/{taxonomy}/{year}/")
                and parts.path.lower().endswith(("_lab.xml", "_lab.xsd", "-lab.xml", "-lab.xsd"))
                and not parts.query
                and not parts.fragment
            ):
                urls.append(candidate)
    return tuple(dict.fromkeys(urls))


def _parse_labels(root: Element[str], taxonomy: str) -> dict[str, str]:
    resources: dict[str, str] = {}
    hrefs: dict[str, str] = {}
    for element in root.iter():
        tag_name = _local_name(element.tag)
        attrs = {_local_name(key): value for key, value in element.attrib.items()}
        label_id = attrs.get("label")
        if tag_name == "label" and label_id and element.text:
            resources[label_id] = element.text.strip()
        elif tag_name == "loc" and label_id:
            href = attrs.get("href", "")
            concept = href.rsplit("#", 1)[-1]
            prefix = f"{taxonomy}_"
            hrefs[label_id] = concept[len(prefix) :] if concept.startswith(prefix) else concept
    labels: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) != "labelArc":
            continue
        attrs = {_local_name(key): value for key, value in element.attrib.items()}
        source = attrs.get("from")
        target = attrs.get("to")
        if source in hrefs and target in resources:
            labels.setdefault(hrefs[source], resources[target])
    return labels


def _xml_bool(value: str | None) -> bool | None:
    if value is None:
        return None
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise SecProviderError("SEC_SCHEMA_BAD_BOOLEAN")


def _schema_period_type(value: str | None) -> Literal["instant", "duration"] | None:
    if value is None:
        return None
    if value == "instant":
        return "instant"
    if value == "duration":
        return "duration"
    raise SecProviderError("SEC_SCHEMA_BAD_PERIOD_TYPE")


def _schema_balance_type(value: str | None) -> Literal["credit", "debit"] | None:
    if value is None:
        return None
    if value == "credit":
        return "credit"
    if value == "debit":
        return "debit"
    raise SecProviderError("SEC_SCHEMA_BAD_BALANCE_TYPE")


class LatestFinancialReportsQuery(QueryParams):
    """Query parameters for SEC daily filing index entries."""

    filing_date: date
    form: str | None = None
    offset: int = Field(default=0, ge=0, le=100_000, strict=True)
    limit: int = Field(default=100, ge=1, le=_MAX_PAGE, strict=True)

    @field_validator("filing_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> object:
        return _query_date(value)

    @field_validator("form")
    @classmethod
    def _form(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper()
        if re.fullmatch(r"[0-9A-Z][0-9A-Z/-]{0,19}", normalized) is None:
            raise ValueError("form must be a short SEC form token")
        return normalized


class LatestFinancialReportsFetcher(
    Fetcher[LatestFinancialReportsQuery, tuple[SecLatestFinancialReport, ...]]
):
    """Fetch SEC financial filing entries for a filing date."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "LatestFinancialReports"
    row_model: ClassVar[type[ContractModel]] = SecLatestFinancialReport
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_latest_financial_reports",
        period="event",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> LatestFinancialReportsQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"filing_date", "form", "offset", "limit"})
        return LatestFinancialReportsQuery.model_validate(kwargs)

    def extract_data(
        self, params: LatestFinancialReportsQuery, ctx: FetchContext
    ) -> tuple[SecLatestFinancialReport, ...]:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 1)
        filed = params.filing_date
        quarter = (filed.month - 1) // 3 + 1
        url = (
            f"https://www.sec.gov/Archives/edgar/daily-index/{filed.year}/QTR{quarter}/"
            f"master.{filed:%Y%m%d}.idx"
        )
        body = fetch_bytes(
            ctx, url, max_bytes=MAX_TEXT_RESPONSE_BYTES, transport=self.http_transport
        )
        try:
            text_body = body.decode("ascii")
        except UnicodeDecodeError:
            raise SecProviderError("SEC_DAILY_INDEX_BAD_ENCODING") from None
        rows = _parse_master_index(text_body, params.filing_date)
        filtered = (
            [row for row in rows if row["form"].casefold() == params.form.casefold()]
            if params.form
            else [row for row in rows if row["form"] in _FINANCIAL_FORMS]
        )
        selected = filtered[params.offset : params.offset + params.limit]
        results: list[SecLatestFinancialReport] = []
        for row in selected:
            cik = canonical_cik(row["cik"])
            accession = row["accession"]
            complete = _complete_submission_url(cik, accession)
            detail = _filing_detail_url(cik, accession)
            index_headers = f"{_archive_base(cik, accession)}-index-headers.html"
            results.append(
                SecLatestFinancialReport(
                    filing_date=filed,
                    period_ending=None,
                    symbol=None,
                    name=row["name"],
                    cik=cik,
                    sic=None,
                    report_type=row["form"],
                    description=None,
                    url=detail,
                    items=None,
                    index_headers=index_headers,
                    complete_submission=complete,
                    metadata=None,
                    financial_report=None,
                )
            )
        return tuple(results)

    def transform_data(
        self, raw: tuple[SecLatestFinancialReport, ...], params: LatestFinancialReportsQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw


def _parse_master_index(text_body: str, expected_date: date) -> tuple[dict[str, str], ...]:
    lines = text_body.splitlines()
    try:
        header = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "CIK|Company Name|Form Type|Date Filed|Filename"
        )
    except StopIteration:
        raise SecProviderError("SEC_DAILY_INDEX_MISSING_HEADER") from None
    results: list[dict[str, str]] = []
    for line in lines[header + 1 :]:
        if not line.strip():
            continue
        fields = line.split("|")
        if len(fields) != 5:
            raise SecProviderError("SEC_DAILY_INDEX_BAD_ROW")
        cik, name, form, date_text, filename = fields
        filed = _date_value(date_text, required=True)
        match = re.fullmatch(r"edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt", filename)
        if match is None or filed != expected_date:
            if filed != expected_date:
                continue
            raise SecProviderError("SEC_DAILY_INDEX_BAD_FILENAME")
        if not name.strip() or not form.strip():
            raise SecProviderError("SEC_DAILY_INDEX_BAD_ROW")
        results.append(
            {
                "cik": match.group(1),
                "name": name.strip(),
                "form": form.strip(),
                "accession": match.group(2),
            }
        )
    return tuple(results)


class ManagementDiscussionAnalysisQuery(QueryParams):
    """Query parameters for an issuer filing management discussion."""

    symbol: str
    year: int = Field(ge=1900, le=2200, strict=True)
    period: Literal["Q1", "Q2", "Q3", "Q4"]
    include_tables: bool = True
    raw_html: bool = False

    @field_validator("symbol")
    @classmethod
    def _ticker(cls, value: str) -> str:
        normalized = _query_ticker(value)
        if normalized is None:
            raise ValueError("symbol is required")
        return normalized


class ManagementDiscussionAnalysisFetcher(
    Fetcher[ManagementDiscussionAnalysisQuery, SecManagementDiscussionAnalysis]
):
    """Fetch management discussion content from an issuer filing."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "ManagementDiscussionAnalysis"
    row_model: ClassVar[type[ContractModel]] = SecManagementDiscussionAnalysis
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_management_discussion_analysis",
        period="period",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> ManagementDiscussionAnalysisQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"symbol", "year", "period", "include_tables", "raw_html"})
        return ManagementDiscussionAnalysisQuery.model_validate(kwargs)

    def extract_data(
        self, params: ManagementDiscussionAnalysisQuery, ctx: FetchContext
    ) -> SecManagementDiscussionAnalysis:
        """Fetch SEC source data and parse it into typed models."""
        period_num = int(params.period[1])
        ensure_request_capacity(ctx, 3)
        cik, _ = _resolve_cik(ctx, params.symbol, None, transport=self.http_transport)
        submissions = fetch_json(ctx, _submission_url(cik), transport=self.http_transport)
        _, name, rows, _ = _parse_filing_arrays(submissions)
        filing = _select_period_filing(rows, params.year, period_num, forms=("10-K", "10-Q"))
        if filing.primary_document is None:
            raise SecProviderError("SEC_MDA_PRIMARY_DOCUMENT_MISSING")
        _validate_filename(filing.primary_document, "SEC_MDA_PRIMARY_DOCUMENT_BAD")
        url = f"{_archive_base(cik, filing.accession)}/{filing.primary_document}"
        body = fetch_bytes(
            ctx, url, max_bytes=MAX_TEXT_RESPONSE_BYTES, transport=self.http_transport
        )
        try:
            document = body.decode("utf-8")
        except UnicodeDecodeError:
            raise SecProviderError("SEC_HTML_BAD_ENCODING") from None
        content = (
            document
            if params.raw_html
            else _extract_mda(document, params.period, include_tables=params.include_tables)
        )
        return SecManagementDiscussionAnalysis(
            symbol=params.symbol,
            calendar_year=params.year,
            calendar_period=period_num,
            period_ending=filing.report_date,
            content=content,
            url=url,
        )

    def transform_data(
        self, raw: SecManagementDiscussionAnalysis, params: ManagementDiscussionAnalysisQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return (raw,)


def _select_period_filing(
    rows: tuple[_FilingRow, ...], year: int, quarter: int, *, forms: tuple[str, ...]
) -> _FilingRow:
    candidates = []
    for row in rows:
        if row.form not in forms or row.report_date is None:
            continue
        if row.report_date.year != year or (row.report_date.month - 1) // 3 + 1 != quarter:
            continue
        candidates.append(row)
    if not candidates:
        raise SecProviderError("SEC_PERIOD_FILING_NOT_FOUND")
    candidates.sort(key=lambda row: row.filing_date, reverse=True)
    return candidates[0]


class _VisibleTextParser(HTMLParser):
    def __init__(self, *, include_tables: bool = True) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0
        self._table_depth = 0
        self._include_tables = include_tables

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "table":
            self._table_depth += 1
        if tag.casefold() in {"script", "style", "ix:header"}:
            self._hidden += 1
        if tag.casefold() in {"br", "p", "div", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "table" and self._table_depth:
            self._table_depth -= 1
        if tag.casefold() in {"script", "style", "ix:header"} and self._hidden:
            self._hidden -= 1
        if tag.casefold() in {"br", "p", "div", "tr", "td", "th", "h1", "h2", "h3", "h4"}:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._hidden and (self._include_tables or not self._table_depth):
            self.parts.append(data)


def _plain_text(value: str, *, include_tables: bool = True) -> str:
    parser = _VisibleTextParser(include_tables=include_tables)
    parser.feed(value)
    return re.sub(r"\s+", " ", " ".join(parser.parts)).strip()


def _extract_mda(document: str, period: str, *, include_tables: bool) -> str:
    visible = _plain_text(document, include_tables=include_tables)
    item_numbers = ("7", "7A") if period == "Q4" else ("2", "3")
    start_patterns = [
        re.compile(
            rf"\bitem\s+{number}\.?\s+(?:management['’]s\s+discussion|management\s+discussion)",
            re.I,
        )
        for number in item_numbers[:1]
    ]
    starts = [match.start() for pattern in start_patterns if (match := pattern.search(visible))]
    if not starts:
        raise SecProviderError("SEC_MDA_SECTION_NOT_FOUND")
    start = min(starts)
    stop_pattern = re.compile(r"\bitem\s+(?:7A|8)\.?\s+|\bitem\s+(?:3|4)\.?\s+", re.I)
    stop = stop_pattern.search(visible, start + 1)
    content = visible[start : stop.start() if stop else len(visible)].strip()
    if len(content) < 40:
        raise SecProviderError("SEC_MDA_SECTION_TOO_SHORT")
    return content


class Form13FHRQuery(QueryParams):
    """Query parameters for one SEC Form 13F-HR filing."""

    symbol: str | None = None
    cik: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    quarter: int | None = Field(default=None, ge=1, le=4, strict=True)
    offset: int = Field(default=0, ge=0, le=_MAX_HOLDINGS, strict=True)
    limit: int = Field(default=1_000, ge=1, le=_MAX_HOLDINGS, strict=True)

    @field_validator("symbol")
    @classmethod
    def _ticker(cls, value: str | None) -> str | None:
        return _query_ticker(value)

    @field_validator("cik", mode="before")
    @classmethod
    def _cik(cls, value: object) -> object:
        return None if value is None else _query_cik(value)

    @model_validator(mode="after")
    def _filters(self) -> Form13FHRQuery:
        _validate_identity(self.symbol, self.cik)
        if (self.year is None) != (self.quarter is None):
            raise ValueError("year and quarter must be provided together")
        return self


class Form13FHRFetcher(Fetcher[Form13FHRQuery, tuple[SecForm13FHRHolding, ...]]):
    """Fetch typed holdings from a Form 13F-HR information table."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "Form13FHR"
    row_model: ClassVar[type[ContractModel]] = SecForm13FHRHolding
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="stock",
        domain="sec_form_13f_hr",
        period="period",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> Form13FHRQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(kwargs, {"symbol", "cik", "year", "quarter", "offset", "limit"})
        return Form13FHRQuery.model_validate(kwargs)

    def extract_data(
        self, params: Form13FHRQuery, ctx: FetchContext
    ) -> tuple[SecForm13FHRHolding, ...]:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 4 if params.symbol is not None else 3)
        cik, _ = _resolve_cik(ctx, params.symbol, params.cik, transport=self.http_transport)
        submissions = fetch_json(ctx, _submission_url(cik), transport=self.http_transport)
        _, manager_name, rows, _ = _parse_filing_arrays(submissions)
        filing = _select_disclosure_filing(
            rows, forms={"13F-HR", "13F-HR/A"}, year=params.year, quarter=params.quarter
        )
        documents = _parse_index_documents(
            fetch_json(
                ctx,
                f"{_archive_base(cik, filing.accession)}/index.json",
                transport=self.http_transport,
            ),
            cik,
            filing.accession,
        )
        candidates = [
            item
            for item in documents
            if (item.document_type or "").upper().startswith("INFORMATION TABLE")
            or "infotable" in item.name.casefold()
        ]
        if not candidates:
            raise SecProviderError("SEC_13F_INFORMATION_TABLE_NOT_FOUND")
        information_table = candidates[0]
        xml_root = _safe_xml(
            fetch_bytes(
                ctx,
                information_table.url,
                max_bytes=MAX_XML_RESPONSE_BYTES,
                transport=self.http_transport,
            ),
            "SEC_13F_BAD_XML",
        )
        rows_xml = [
            element
            for element in xml_root.iter()
            if _local_name(element.tag).casefold() == "infotable"
        ]
        if len(rows_xml) > _MAX_HOLDINGS:
            raise SecProviderError("SEC_13F_TOO_MANY_HOLDINGS")
        report_date = filing.report_date
        if report_date is None:
            raise SecProviderError("SEC_13F_REPORT_PERIOD_MISSING")
        parsed = [_parse_13f_holding(item, filing.filing_date) for item in rows_xml]
        total_value = sum(item.value for item in parsed)
        parsed = parsed[params.offset : params.offset + params.limit]
        return tuple(
            SecForm13FHRHolding(
                period_ending=report_date,
                filing_date=filing.filing_date,
                manager_cik=cik,
                manager_name=manager_name,
                accession_number=filing.accession,
                issuer=item.issuer,
                cusip=item.cusip,
                asset_class=item.asset_class,
                security_type=item.security_type,
                option_type=item.option_type,
                investment_discretion=item.investment_discretion,
                other_manager=item.other_manager,
                voting_authority_sole=item.voting_authority_sole,
                voting_authority_shared=item.voting_authority_shared,
                voting_authority_none=item.voting_authority_none,
                principal_amount=item.principal_amount,
                value=item.value,
                weight=(item.value / total_value) if total_value else 0.0,
            )
            for item in parsed
        )

    def transform_data(
        self, raw: tuple[SecForm13FHRHolding, ...], params: Form13FHRQuery
    ) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw


def _select_disclosure_filing(
    rows: tuple[_FilingRow, ...],
    *,
    forms: set[str],
    year: int | None,
    quarter: int | None,
    filing_date: date | None = None,
) -> _FilingRow:
    candidates = [
        row
        for row in rows
        if row.form in forms
        and row.report_date is not None
        and (year is None or row.report_date.year == year)
        and (quarter is None or (row.report_date.month - 1) // 3 + 1 == quarter)
        and (filing_date is None or row.filing_date == filing_date)
    ]
    if not candidates:
        raise SecProviderError("SEC_DISCLOSURE_FILING_NOT_FOUND")
    candidates.sort(key=lambda row: (row.report_date or date.min, row.filing_date), reverse=True)
    return candidates[0]


def _local_name(tag: object) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _safe_xml(body: bytes, error_code: str) -> Element[str]:
    if not body or len(body) > MAX_XML_RESPONSE_BYTES:
        raise SecProviderError("SEC_XML_EMPTY_OR_TOO_LARGE")
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise SecProviderError("SEC_XML_DTD_FORBIDDEN")
    try:
        root = SafeET.fromstring(body)
    except (DefusedXmlException, SafeET.ParseError, ValueError, UnicodeError):
        raise SecProviderError(error_code) from None
    if not isinstance(root, Element):
        raise SecProviderError(error_code)
    return root


def _first_xml_text(element: Element[str], names: tuple[str, ...]) -> str | None:
    wanted = {name.casefold() for name in names}
    for child in element.iter():
        text = child.text
        if _local_name(child.tag).casefold() in wanted and text and text.strip():
            return text.strip()
    return None


def _parse_13f_holding(element: Element[str], filing_date: date) -> _Parsed13FHolding:
    issuer = _first_xml_text(element, ("nameOfIssuer",))
    cusip = _first_xml_text(element, ("cusip",))
    asset_class = _first_xml_text(element, ("titleOfClass",))
    value_text = _first_xml_text(element, ("value",))
    amount_text = _first_xml_text(element, ("sshPrnamt", "shrsOrPrnAmt"))
    if amount_text is None:
        amount_node = next(
            (child for child in element.iter() if _local_name(child.tag) == "shrsOrPrnAmt"), None
        )
        amount_text = (
            _first_xml_text(amount_node, ("sshPrnamt",)) if amount_node is not None else None
        )
    if not issuer or not cusip or not asset_class or value_text is None or amount_text is None:
        raise SecProviderError("SEC_13F_ROW_MISSING_FIELD")
    value = _required_int(value_text, code="SEC_13F_BAD_VALUE")
    if filing_date < _FORM_13F_NEAREST_DOLLAR_EFFECTIVE_DATE:
        value *= 1_000
    principal_amount = _required_int(amount_text, code="SEC_13F_BAD_AMOUNT")
    security_text = _first_xml_text(element, ("sshPrnamtType",))
    security_type: Literal["SH", "PRN"] | None = None
    if security_text is not None:
        normalized_security = security_text.upper()
        if normalized_security == "SH":
            security_type = "SH"
        elif normalized_security == "PRN":
            security_type = "PRN"
        else:
            raise SecProviderError("SEC_13F_BAD_SECURITY_TYPE")
    option_text = _first_xml_text(element, ("putCall",))
    option: Literal["call", "put"] | None = None
    if option_text is not None:
        normalized_option = option_text.casefold()
        if normalized_option == "put":
            option = "put"
        elif normalized_option == "call":
            option = "call"
        else:
            raise SecProviderError("SEC_13F_BAD_OPTION_TYPE")
    return _Parsed13FHolding(
        issuer=issuer,
        cusip=cusip,
        asset_class=asset_class,
        security_type=security_type,
        option_type=option,
        investment_discretion=_first_xml_text(element, ("investmentDiscretion",)),
        other_manager=_first_xml_text(element, ("otherManager",)),
        voting_authority_sole=_parse_int(
            _first_xml_text(element, ("sole",)), code="SEC_13F_BAD_VOTE"
        ),
        voting_authority_shared=_parse_int(
            _first_xml_text(element, ("shared",)), code="SEC_13F_BAD_VOTE"
        ),
        voting_authority_none=_parse_int(
            _first_xml_text(element, ("none",)), code="SEC_13F_BAD_VOTE"
        ),
        principal_amount=principal_amount,
        value=value,
    )


def _required_int(value: object, *, code: str) -> int:
    parsed = _parse_int(value, code=code, allow_none=False)
    if parsed is None:
        raise SecProviderError(code)
    return parsed


class NportDisclosureQuery(QueryParams):
    """Query parameters for SEC Form N-PORT fund disclosures."""

    symbol: str | None = None
    cik: str | None = None
    year: int | None = Field(default=None, ge=1900, le=2200, strict=True)
    quarter: int | None = Field(default=None, ge=1, le=4, strict=True)
    filing_date: date | None = None
    offset: int = Field(default=0, ge=0, le=_MAX_HOLDINGS, strict=True)
    limit: int = Field(default=1_000, ge=1, le=_MAX_HOLDINGS, strict=True)

    @field_validator("symbol")
    @classmethod
    def _ticker(cls, value: str | None) -> str | None:
        return _query_ticker(value)

    @field_validator("cik", mode="before")
    @classmethod
    def _cik(cls, value: object) -> object:
        return None if value is None else _query_cik(value)

    @field_validator("filing_date", mode="before")
    @classmethod
    def _date(cls, value: object) -> object:
        return _query_date(value)

    @model_validator(mode="after")
    def _filters(self) -> NportDisclosureQuery:
        _validate_identity(self.symbol, self.cik)
        if (self.year is None) != (self.quarter is None):
            raise ValueError("year and quarter must be provided together")
        return self


class NportDisclosureFetcher(Fetcher[NportDisclosureQuery, SecNportFiling]):
    """Fetch typed holdings from an SEC Form N-PORT filing."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "NportDisclosure"
    row_model: ClassVar[type[ContractModel]] = SecNportDisclosureHolding
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="fund",
        domain="sec_nport_disclosure",
        period="period",
        market="us",
        source="sec",
        verified=False,
    )

    def transform_query(self, **kwargs: object) -> NportDisclosureQuery:
        """Validate external arguments before source I/O."""
        _reject_unexpected(
            kwargs, {"symbol", "cik", "year", "quarter", "filing_date", "offset", "limit"}
        )
        return NportDisclosureQuery.model_validate(kwargs)

    def extract_data(self, params: NportDisclosureQuery, ctx: FetchContext) -> SecNportFiling:
        """Fetch SEC source data and parse it into typed models."""
        ensure_request_capacity(ctx, 4 if params.symbol is not None else 3)
        if params.symbol is not None:
            directory = fetch_json(ctx, _MF_TICKER_URL, transport=self.http_transport)
            rows = _parse_mutual_fund_directory(directory)
            cik = resolve_ticker(rows, params.symbol)["cik"]
            symbol = params.symbol
        else:
            if params.cik is None:
                raise SecProviderError("SEC_QUERY_IDENTITY_MISSING")
            cik = params.cik
            symbol = None
        submissions = fetch_json(ctx, _submission_url(cik), transport=self.http_transport)
        _, name, filings, _ = _parse_filing_arrays(submissions)
        filing = _select_disclosure_filing(
            filings,
            forms={"NPORT-P", "NPORT-P/A"},
            year=params.year,
            quarter=params.quarter,
            filing_date=params.filing_date,
        )
        if filing.report_date is None:
            raise SecProviderError("SEC_NPORT_REPORT_PERIOD_MISSING")
        index = fetch_json(
            ctx,
            f"{_archive_base(cik, filing.accession)}/index.json",
            transport=self.http_transport,
        )
        documents = _parse_index_documents(index, cik, filing.accession)
        xml_files = [item for item in documents if item.name.lower().endswith(".xml")]
        if not xml_files:
            raise SecProviderError("SEC_NPORT_XML_NOT_FOUND")
        primary = next(
            (item for item in xml_files if item.document_type in {"NPORT-P", "NPORT-P/A"}),
            xml_files[0],
        )
        root = _safe_xml(
            fetch_bytes(
                ctx, primary.url, max_bytes=MAX_XML_RESPONSE_BYTES, transport=self.http_transport
            ),
            "SEC_NPORT_BAD_XML",
        )
        investment_nodes = [
            element
            for element in root.iter()
            if _local_name(element.tag).casefold() == "invstorsec"
        ]
        if len(investment_nodes) > _MAX_HOLDINGS:
            raise SecProviderError("SEC_NPORT_TOO_MANY_HOLDINGS")
        parsed = [
            _parse_nport_holding(
                element,
                cik=cik,
                name=name,
                symbol=symbol,
                accession=filing.accession,
                filing_date=filing.filing_date,
                acceptance_datetime=filing.accepted,
                period_ending=filing.report_date,
            )
            for element in investment_nodes
        ]
        if not parsed:
            raise SecProviderError("SEC_NPORT_HOLDINGS_MISSING")
        holdings = tuple(parsed[params.offset : params.offset + params.limit])
        return SecNportFiling(
            cik=cik,
            accession_number=filing.accession,
            filing_date=filing.filing_date,
            period_ending=filing.report_date,
            primary_document_url=primary.url,
            holdings=holdings,
        )

    def transform_data(self, raw: SecNportFiling, params: NportDisclosureQuery) -> FetchResult:
        """Return typed records through the fetcher protocol."""
        del params
        return raw.holdings


def _parse_mutual_fund_directory(document: object) -> tuple[dict[str, str], ...]:
    from opendata.data.providers.sec.models._common import parse_ticker_directory

    if not isinstance(document, dict):
        raise SecProviderError("SEC_TICKER_DIRECTORY_BAD_SHAPE")
    return parse_ticker_directory(document)


def _parse_nport_holding(
    element: Element[str],
    *,
    cik: str,
    name: str,
    symbol: str | None,
    accession: str,
    filing_date: date,
    acceptance_datetime: datetime | None,
    period_ending: date,
) -> SecNportDisclosureHolding:
    values: dict[str, str | None] = {}
    for child in element.iter():
        local = _local_name(child.tag)
        if child.text and child.text.strip():
            values.setdefault(local, child.text.strip())
    normalized_values = {key.casefold(): value for key, value in values.items()}

    def text(*keys: str) -> str | None:
        return next(
            (
                normalized_values[key.casefold()]
                for key in keys
                if normalized_values.get(key.casefold()) is not None
            ),
            None,
        )

    identity_nodes = [
        child
        for child in element.iter()
        if _local_name(child.tag).casefold() in {"isin", "otherid", "ticker"}
    ]
    identities: dict[str, str] = {}
    for node in identity_nodes:
        label = node.attrib.get("otherIdType", node.attrib.get("idType", ""))
        local = _local_name(node.tag).casefold()
        if local == "isin":
            identities["isin"] = node.text or ""
        elif local == "ticker":
            identities["symbol"] = node.text or ""
        elif local == "otherid":
            identities["other_id"] = node.text or ""
        if label and node.text:
            identities.setdefault("other_id", node.text.strip())

    def number(*keys: str) -> float | None:
        value = text(*keys)
        if value is None:
            return None
        try:
            result = float(value.replace(",", ""))
        except (ValueError, OverflowError):
            raise SecProviderError("SEC_NPORT_BAD_NUMBER") from None
        if not (float("-inf") < result < float("inf")):
            raise SecProviderError("SEC_NPORT_BAD_NUMBER")
        return result

    as_of = acceptance_datetime.date() if acceptance_datetime is not None else None
    return SecNportDisclosureHolding(
        symbol=symbol,
        asset_symbol=identities.get("symbol"),
        fund_cik=cik,
        fund_name=name,
        accession_number=accession,
        filing_date=filing_date,
        acceptance_datetime=acceptance_datetime,
        period_ending=period_ending,
        name=text("name"),
        title=text("title"),
        cusip=text("cusip"),
        lei=text("lei"),
        isin=identities.get("isin"),
        other_id=identities.get("other_id"),
        is_restricted=text("isRestrictedSec"),
        fair_value_level=_parse_int(text("fairValLevel"), code="SEC_NPORT_BAD_FAIR_VALUE_LEVEL"),
        is_cash_collateral=text("isCashCollateral"),
        is_non_cash_collateral=text("isNonCashCollateral"),
        is_loan_by_fund=text("isLoanByFund"),
        loan_value=number("loanVal", "loanValue"),
        issuer_conditional=text("issuerConditional"),
        asset_conditional=text("assetConditional"),
        payoff_profile=text("payoffProfile"),
        asset_category=text("assetCat"),
        issuer_category=text("issuerCat"),
        country=text("invCountry"),
        balance=number("balance"),
        units=text("units"),
        currency=text("currencyCd"),
        value=number("valUSD"),
        weight=number("pctVal"),
        as_of=as_of,
        maturity_date=_date_value(text("maturityDt", "maturityDate"))
        if text("maturityDt", "maturityDate")
        else None,
        coupon_kind=text("couponKind"),
        rate_type=text("rateType"),
        annualized_return=number("annualizedReturn", "annualizedRt"),
        is_default=text("isDefault"),
        in_arrears=text("inArrears"),
        is_paid_kind=text("isPaidKind"),
        derivative_category=text("derivativeCategory", "derivCat"),
        counterparty=text("counterparty", "counterpartyName"),
        underlying_name=text("underlyingName"),
        option_type=text("optionType", "putCall"),
        derivative_payoff=text("derivativePayoff", "derivativePayoffProfile", "derivPayoffProfile"),
        expiry_date=_date_value(text("expDt", "expDate", "expiryDate", "expirationDate"))
        if text("expDt", "expDate", "expiryDate", "expirationDate")
        else None,
        exercise_price=number("exercisePrice"),
        exercise_currency=text("exerciseCurrency", "exercisePriceCurrency"),
        shares_per_contract=number("sharesPerContract"),
        delta=text("delta"),
        rate_type_rec=text("rateTypeRec"),
        receive_currency=text("receiveCurrency"),
        upfront_receive=number("upfrontReceive"),
        floating_rate_index_rec=text("floatingRateIndexRec"),
        floating_rate_spread_rec=number("floatingRateSpreadRec"),
        rate_tenor_rec=text("rateTenorRec"),
        rate_tenor_unit_rec=text("rateTenorUnitRec"),
        reset_date_rec=text("resetDateRec"),
        reset_date_unit_rec=text("resetDateUnitRec"),
        rate_type_pmnt=text("rateTypePmnt", "rateTypePay"),
        payment_currency=text("paymentCurrency"),
        upfront_payment=number("upfrontPayment"),
        floating_rate_index_pmnt=text("floatingRateIndexPmnt", "floatingRateIndexPay"),
        floating_rate_spread_pmnt=number("floatingRateSpreadPmnt", "floatingRateSpreadPay"),
        rate_tenor_pmnt=text("rateTenorPmnt", "rateTenorPay"),
        rate_tenor_unit_pmnt=text("rateTenorUnitPmnt", "rateTenorUnitPay"),
        reset_date_pmnt=text("resetDatePmnt", "resetDatePay"),
        reset_date_unit_pmnt=text("resetDateUnitPmnt", "resetDateUnitPay"),
        repo_type=text("repoType"),
        is_cleared=text("isCleared"),
        is_tri_party=text("isTriParty"),
        principal_amount=number("principalAmount"),
        principal_currency=text("principalCurrency"),
        collateral_type=text("collateralType"),
        collateral_amount=number("collateralAmount"),
        collateral_currency=text("collateralCurrency"),
        exchange_currency=text("exchangeCurrency"),
        exchange_rate=number("exchangeRate"),
        currency_sold=text("currencySold"),
        currency_amount_sold=number("currencyAmountSold", "currencyAmtSold"),
        currency_bought=text("currencyBought"),
        currency_amount_bought=number("currencyAmountBought", "currencyAmtBought"),
        notional_amount=number("notionalAmount"),
        notional_currency=text("notionalCurrency"),
        unrealized_gain=number("unrealizedGain"),
    )


__all__ = [
    "CompanyFilingsFetcher",
    "CompanyFilingsQuery",
    "FilingsFetcher",
    "Form13FHRFetcher",
    "Form13FHRQuery",
    "LatestFinancialReportsFetcher",
    "LatestFinancialReportsQuery",
    "ManagementDiscussionAnalysisFetcher",
    "ManagementDiscussionAnalysisQuery",
    "NportDisclosureFetcher",
    "NportDisclosureQuery",
    "RssLitigationFetcher",
    "RssLitigationQuery",
    "SchemaFilesFetcher",
    "SchemaFilesQuery",
    "SecFilingFetcher",
    "SecFilingQuery",
    "SecHtmFileFetcher",
    "SecHtmFileQuery",
]
