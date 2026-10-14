"""Bounded search over the latest SEC-published Form 13F filing-manager batch."""

from __future__ import annotations

import csv
import io
import re
import stat
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, ClassVar, Literal, NoReturn
from urllib.parse import urljoin, urlparse

from pydantic import Field, ValidationError, field_validator

from opendata.data.capability import Capability
from opendata.data.models.sec_institutions import SecInstitutionSearchResult
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, QueryParams
from opendata.data.providers.sec.models._common import (
    SecProviderError,
    canonical_cik,
    ensure_request_capacity,
    fetch_bytes,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic_core import InitErrorDetails

    from opendata.data.models.base import ContractModel
    from opendata.data.providers._engine.http_json import HttpGet

SEC_13F_DATASETS_PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_13F_ARCHIVE_PATH = "/files/datastandardsinnovation/data/form-13f-data-sets/"
MAX_DATASETS_PAGE_BYTES = 2 * 1024 * 1024
MAX_DATASET_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 16
MAX_COMPRESSION_RATIO = 250
MAX_TABLE_BYTES = 64 * 1024 * 1024
MAX_TABLE_LINE_BYTES = 1024 * 1024
MAX_TABLE_ROWS = 250_000
MAX_UPSTREAM_TIMEOUT_SECONDS = 60.0
_BATCH_FILENAME_RE = re.compile(
    r"^(?P<start>\d{2}[A-Z]{3}\d{4})-(?P<end>\d{2}[A-Z]{3}\d{4})_form13f\.zip$",
    re.IGNORECASE,
)
_SUBMISSION_COLUMNS = frozenset(
    {"ACCESSION_NUMBER", "FILING_DATE", "SUBMISSIONTYPE", "CIK", "PERIODOFREPORT"}
)
_COVERPAGE_COLUMNS = frozenset(
    {"ACCESSION_NUMBER", "REPORTCALENDARORQUARTER", "FILINGMANAGER_NAME"}
)
_SubmissionType = Literal["13F-HR", "13F-HR/A", "13F-NT", "13F-NT/A"]
_SUBMISSION_TYPES: dict[str, _SubmissionType] = {
    "13F-HR": "13F-HR",
    "13F-HR/A": "13F-HR/A",
    "13F-NT": "13F-NT",
    "13F-NT/A": "13F-NT/A",
}


def _fail(code: str) -> NoReturn:
    raise SecProviderError(code)


def _reject_unexpected(kwargs: dict[str, object], allowed: set[str]) -> None:
    unexpected = set(kwargs) - allowed - {"source", "market"}
    if unexpected:
        errors: list[InitErrorDetails] = [
            {"type": "extra_forbidden", "loc": (key,), "input": None} for key in sorted(unexpected)
        ]
        raise ValidationError.from_exception_data("QueryParams", errors)


class InstitutionsSearchQuery(QueryParams):
    """Public search term and cache option for SEC filing managers."""

    query: str | None = None
    market: Literal["us"] = "us"
    use_cache: bool | None = Field(default=True, strict=True)

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("query must contain printable text")
        if len(value) > 200:
            raise ValueError("query may not exceed 200 characters")
        return value or None


@dataclass(frozen=True)
class _DatasetBatch:
    url: str
    filename: str
    filing_window_start: date
    filing_window_end: date


@dataclass(frozen=True)
class _Submission:
    accession_number: str
    cik: str
    filing_date: date
    submission_type: _SubmissionType
    as_of: date
    line_number: int


@dataclass(frozen=True)
class _Coverpage:
    accession_number: str
    report_calendar_or_quarter: date
    name: str | None
    line_number: int


@dataclass(frozen=True)
class _InstitutionRow:
    name: str | None
    cik: str
    source_batch: str
    as_of: date
    filing_window_start: date
    filing_window_end: date
    filing_date: date
    submission_type: _SubmissionType
    accession_number: str
    report_calendar_or_quarter: date
    submission_line_number: int
    coverpage_line_number: int


@dataclass(frozen=True)
class _ParsedBatch:
    batch: _DatasetBatch
    rows: tuple[_InstitutionRow, ...]


class _ArchiveLinkParser(HTMLParser):
    """Collect anchor targets without relying on page presentation text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        for key, value in attrs:
            if key.casefold() == "href" and value:
                self.hrefs.append(value.strip())


def _parse_batch_filename(filename: str) -> tuple[date, date] | None:
    match = _BATCH_FILENAME_RE.fullmatch(filename)
    if match is None:
        return None
    try:
        start = datetime.strptime(match.group("start").upper(), "%d%b%Y").date()
        end = datetime.strptime(match.group("end").upper(), "%d%b%Y").date()
    except ValueError:
        _fail("SEC_13F_DATASET_BAD_BATCH_FILENAME")
    if start > end:
        _fail("SEC_13F_DATASET_BAD_BATCH_FILENAME")
    return start, end


def _discover_latest_batch(page: bytes) -> _DatasetBatch:
    if not page or len(page) > MAX_DATASETS_PAGE_BYTES:
        _fail("SEC_13F_DATASET_PAGE_BAD_SIZE")
    try:
        text = page.decode("utf-8")
    except UnicodeDecodeError:
        _fail("SEC_13F_DATASET_PAGE_BAD_ENCODING")
    parser = _ArchiveLinkParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        _fail("SEC_13F_DATASET_PAGE_BAD_HTML")

    candidates: list[_DatasetBatch] = []
    for href in parser.hrefs:
        resolved = urljoin(SEC_13F_DATASETS_PAGE_URL, href)
        parsed = urlparse(resolved)
        if (
            parsed.scheme != "https"
            or parsed.netloc.casefold() != "www.sec.gov"
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith(SEC_13F_ARCHIVE_PATH)
        ):
            continue
        filename = PurePosixPath(parsed.path).name
        window = _parse_batch_filename(filename)
        if window is None:
            continue
        start, end = window
        candidates.append(_DatasetBatch(resolved, filename, start, end))

    if not candidates:
        _fail("SEC_13F_DATASET_LATEST_BATCH_NOT_FOUND")
    latest_end = max(candidate.filing_window_end for candidate in candidates)
    latest = [candidate for candidate in candidates if candidate.filing_window_end == latest_end]
    if len(latest) != 1:
        _fail("SEC_13F_DATASET_LATEST_BATCH_AMBIGUOUS")
    return latest[0]


def _parse_sec_date(value: str, code: str) -> date:
    try:
        return datetime.strptime(value.strip().upper(), "%d-%b-%Y").date()
    except (AttributeError, ValueError):
        _fail(code)


def _iter_table_rows(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    required_columns: frozenset[str],
    error_prefix: str,
) -> Iterator[tuple[int, dict[str, str]]]:
    """Read one required TSV member under byte, line, and row caps."""
    if info.file_size > MAX_TABLE_BYTES:
        _fail(f"{error_prefix}_TOO_LARGE")
    consumed_bytes = 0
    row_count = 0
    try:
        with (
            archive.open(info, "r") as member,
            io.TextIOWrapper(member, encoding="utf-8", newline="") as text,
        ):

            def bounded_lines() -> Iterator[str]:
                nonlocal consumed_bytes
                for line in text:
                    line_bytes = len(line.encode("utf-8"))
                    consumed_bytes += line_bytes
                    if line_bytes > MAX_TABLE_LINE_BYTES:
                        _fail(f"{error_prefix}_LINE_TOO_LARGE")
                    if consumed_bytes > MAX_TABLE_BYTES:
                        _fail(f"{error_prefix}_TOO_LARGE")
                    yield line

            reader = csv.reader(bounded_lines(), delimiter="\t", strict=True)
            try:
                raw_header = next(reader)
            except StopIteration:
                _fail(f"{error_prefix}_BAD_SHAPE")
            headers = [header.strip().lstrip("\ufeff").upper() for header in raw_header]
            if (
                not headers
                or any(not header for header in headers)
                or len(headers) != len(set(headers))
                or not required_columns.issubset(headers)
            ):
                _fail(f"{error_prefix}_BAD_SHAPE")
            for values in reader:
                if not values:
                    continue
                if len(values) != len(headers):
                    _fail(f"{error_prefix}_BAD_ROW")
                row_count += 1
                if row_count > MAX_TABLE_ROWS:
                    _fail(f"{error_prefix}_TOO_MANY_ROWS")
                yield reader.line_num, dict(zip(headers, values, strict=True))
    except SecProviderError:
        raise
    except (UnicodeDecodeError, csv.Error, OSError, RuntimeError, zipfile.BadZipFile):
        _fail(f"{error_prefix}_BAD_ENCODING_OR_TSV")
    if row_count == 0:
        _fail(f"{error_prefix}_EMPTY")


def _accession(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"\d{10}-\d{2}-\d{6}", value) is None:
        _fail("SEC_13F_BAD_ACCESSION")
    return value


def _submission_type(value: str) -> _SubmissionType:
    normalized = value.strip().upper()
    parsed = _SUBMISSION_TYPES.get(normalized)
    if parsed is None:
        _fail("SEC_13F_BAD_SUBMISSION_TYPE")
    return parsed


def _submission_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> dict[str, _Submission]:
    result: dict[str, _Submission] = {}
    for line_number, row in _iter_table_rows(
        archive,
        info,
        required_columns=_SUBMISSION_COLUMNS,
        error_prefix="SEC_13F_SUBMISSION",
    ):
        accession = _accession(row["ACCESSION_NUMBER"])
        submission_type = _submission_type(row["SUBMISSIONTYPE"])
        try:
            cik = canonical_cik(row["CIK"])
        except SecProviderError:
            _fail("SEC_13F_BAD_CIK")
        submission = _Submission(
            accession_number=accession,
            cik=cik,
            filing_date=_parse_sec_date(row["FILING_DATE"], "SEC_13F_BAD_FILING_DATE"),
            submission_type=submission_type,
            as_of=_parse_sec_date(row["PERIODOFREPORT"], "SEC_13F_BAD_REPORT_PERIOD"),
            line_number=line_number,
        )
        previous = result.get(accession)
        if previous is not None:
            _fail("SEC_13F_SUBMISSION_DUPLICATE_ACCESSION_CONFLICT")
        result[accession] = submission
    return result


def _coverpage_name(value: str) -> str | None:
    name = " ".join(value.split())
    if not name:
        return None
    if len(name) > 150 or any(ord(char) < 32 or ord(char) == 127 for char in name):
        _fail("SEC_13F_BAD_FILING_MANAGER_NAME")
    return name


def _coverpage_rows(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> Iterator[_Coverpage]:
    for line_number, row in _iter_table_rows(
        archive,
        info,
        required_columns=_COVERPAGE_COLUMNS,
        error_prefix="SEC_13F_COVERPAGE",
    ):
        accession = _accession(row["ACCESSION_NUMBER"])
        yield _Coverpage(
            accession_number=accession,
            report_calendar_or_quarter=_parse_sec_date(
                row["REPORTCALENDARORQUARTER"], "SEC_13F_BAD_COVERPAGE_PERIOD"
            ),
            name=_coverpage_name(row["FILINGMANAGER_NAME"]),
            line_number=line_number,
        )


def _target_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    try:
        members = archive.infolist()
    except (OSError, zipfile.BadZipFile, RuntimeError):
        _fail("SEC_13F_BAD_ARCHIVE")
    if not members or len(members) > MAX_ARCHIVE_MEMBERS:
        _fail("SEC_13F_ARCHIVE_MEMBER_COUNT_INVALID")

    seen_paths: set[str] = set()
    targets: dict[str, zipfile.ZipInfo] = {}
    for info in members:
        filename = info.filename
        path = PurePosixPath(filename)
        mode = (info.external_attr >> 16) & 0xFFFF
        normalized_path = path.as_posix().casefold()
        if (
            not filename
            or len(filename) > 255
            or "/" in filename
            or "\\" in filename
            or any(ord(char) < 32 or ord(char) == 127 for char in filename)
            or stat.S_ISLNK(mode)
            or info.flag_bits & 0x1
            or info.file_size < 0
            or info.compress_size < 0
        ):
            _fail("SEC_13F_ARCHIVE_MEMBER_INVALID")
        basename = path.name.casefold()
        if basename in {"submission.tsv", "coverpage.tsv"} and basename in targets:
            _fail("SEC_13F_TARGET_MEMBER_AMBIGUOUS")
        if normalized_path in seen_paths:
            _fail("SEC_13F_ARCHIVE_MEMBER_INVALID")
        seen_paths.add(normalized_path)
        if info.compress_size == 0:
            if info.file_size:
                _fail("SEC_13F_COMPRESSION_RATIO_EXCEEDED")
        elif info.file_size > info.compress_size * MAX_COMPRESSION_RATIO:
            _fail("SEC_13F_COMPRESSION_RATIO_EXCEEDED")
        if basename in {"submission.tsv", "coverpage.tsv"}:
            if info.is_dir():
                _fail("SEC_13F_TARGET_MEMBER_INVALID")
            targets[basename] = info
    if set(targets) != {"submission.tsv", "coverpage.tsv"}:
        _fail("SEC_13F_TARGET_MEMBER_MISSING")
    for info in targets.values():
        if info.file_size > MAX_TABLE_BYTES:
            _fail("SEC_13F_TARGET_TABLE_TOO_LARGE")
    return targets


def _parse_dataset_archive(payload: bytes, batch: _DatasetBatch) -> _ParsedBatch:
    if not payload or len(payload) > MAX_DATASET_ARCHIVE_BYTES:
        _fail("SEC_13F_ARCHIVE_BAD_SIZE")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), mode="r") as archive:
            targets = _target_members(archive)
            submissions = _submission_rows(archive, targets["submission.tsv"])
            latest_by_cik: dict[str, _InstitutionRow] = {}
            covered_accessions: set[str] = set()
            for coverpage in _coverpage_rows(archive, targets["coverpage.tsv"]):
                accession = coverpage.accession_number
                if accession in covered_accessions:
                    _fail("SEC_13F_COVERPAGE_DUPLICATE_ACCESSION_CONFLICT")
                covered_accessions.add(accession)
                submission = submissions.pop(accession, None)
                if submission is None:
                    _fail("SEC_13F_ACCESSION_JOIN_MISMATCH")
                row = _InstitutionRow(
                    name=coverpage.name,
                    cik=submission.cik,
                    source_batch=batch.filename,
                    as_of=submission.as_of,
                    filing_window_start=batch.filing_window_start,
                    filing_window_end=batch.filing_window_end,
                    filing_date=submission.filing_date,
                    submission_type=submission.submission_type,
                    accession_number=submission.accession_number,
                    report_calendar_or_quarter=coverpage.report_calendar_or_quarter,
                    submission_line_number=submission.line_number,
                    coverpage_line_number=coverpage.line_number,
                )
                previous = latest_by_cik.get(row.cik)
                if previous is None or (row.filing_date, row.accession_number) > (
                    previous.filing_date,
                    previous.accession_number,
                ):
                    latest_by_cik[row.cik] = row
    except SecProviderError:
        raise
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile):
        _fail("SEC_13F_BAD_ARCHIVE")

    if submissions or not latest_by_cik:
        _fail("SEC_13F_ACCESSION_JOIN_MISMATCH")
    joined = tuple(
        sorted(latest_by_cik.values(), key=lambda row: ((row.name or "").casefold(), row.cik))
    )
    return _ParsedBatch(batch=batch, rows=joined)


class InstitutionsSearchFetcher(Fetcher[InstitutionsSearchQuery, _ParsedBatch]):
    """Search filing managers present in the latest published SEC 13F batch."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str] = "InstitutionsSearch"
    row_model: ClassVar[type[ContractModel]] = SecInstitutionSearchResult
    http_transport: ClassVar[HttpGet | None] = None
    capability: ClassVar[Capability] = Capability(
        asset_class="equity",
        domain="sec_institutions_search",
        period="snapshot",
        market="us",
        source="sec",
        verified=False,
        notes="PARTIAL: latest published SEC Form 13F quarterly batch only",
    )

    def transform_query(self, **kwargs: object) -> InstitutionsSearchQuery:
        """Validate the public query and governed cache option."""
        _reject_unexpected(kwargs, {"query", "use_cache"})
        return InstitutionsSearchQuery.model_validate(kwargs)

    def extract_data(self, params: InstitutionsSearchQuery, ctx: FetchContext) -> _ParsedBatch:
        """Fetch at most the official listing page and its newest quarterly ZIP."""
        if ctx.timeout is not None and ctx.timeout > MAX_UPSTREAM_TIMEOUT_SECONDS:
            _fail("SEC_13F_TIMEOUT_OUT_OF_RANGE")
        ensure_request_capacity(ctx, 2)
        cache_options = {"use_cache": False} if params.use_cache is False else {}
        page = fetch_bytes(
            ctx,
            SEC_13F_DATASETS_PAGE_URL,
            max_bytes=MAX_DATASETS_PAGE_BYTES,
            transport=self.http_transport,
            **cache_options,
        )
        batch = _discover_latest_batch(page)
        archive = fetch_bytes(
            ctx,
            batch.url,
            max_bytes=MAX_DATASET_ARCHIVE_BYTES,
            transport=self.http_transport,
            **cache_options,
        )
        return _parse_dataset_archive(archive, batch)

    def transform_data(self, raw: _ParsedBatch, params: InstitutionsSearchQuery) -> FetchResult:
        """Filter by institution name or CIK and retain selected-filing lineage."""
        query = (params.query or "").casefold()
        selected = tuple(
            row
            for row in raw.rows
            if not query or query in (row.name or "").casefold() or query in row.cik.casefold()
        )
        return tuple(
            SecInstitutionSearchResult(
                name=row.name,
                cik=row.cik,
                source_batch=row.source_batch,
                as_of=row.as_of,
                filing_window_start=row.filing_window_start,
                filing_window_end=row.filing_window_end,
                filing_date=row.filing_date,
                submission_type=row.submission_type,
                accession_number=row.accession_number,
                report_calendar_or_quarter=row.report_calendar_or_quarter,
                submission_line_number=row.submission_line_number,
                coverpage_line_number=row.coverpage_line_number,
            )
            for row in selected
        )
