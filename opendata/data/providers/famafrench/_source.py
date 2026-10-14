"""Bounded, clean-room transport and parsers for Kenneth French public archives."""

from __future__ import annotations

import calendar
import csv
import io
import math
import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import PurePosixPath
from stat import S_ISLNK
from typing import Literal

from opendata.data.http_client import HttpFetchError, get_shared_http_client
from opendata.data.request_budget import RequestBudgetError

FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/Faculty/ken.french/ftp/"
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_MEMBER_COUNT = 32
MAX_OUTPUT_ROWS = 250_000
BREAKPOINT_PERCENTILE_COUNT = 20

_DATE_TOKEN = re.compile(r"(?:\d{4}|\d{6}|\d{8})\Z")
_NUMBER_TOKEN = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z")
_PORTFOLIO_HEADER_TOKEN = re.compile(
    r"(?:\b(?:lo|hi)\s+(?:be/me|b/m|bm|op|inv(?:estment)?|me|e/p|cf/p|d/p|mom(?:entum)?|prior)\b"
    r"|\b(?:low|high)\s+(?:be/me|b/m|bm|op|inv(?:estment)?|me|e/p|cf/p|d/p|mom(?:entum)?|prior)\b"
    r"|\b(?:small|big)\b"
    r"|\b(?:qnt|quintile)\s+\d+\b"
    r"|\b(?:lo|hi)\s+\d+\b"
    r"|\b(?:lo|hi)(?:be/me|b/m|bm|op|inv|me|e/p|cf/p|d/p|mom|prior)\b"
    r"|\b(?:lo|hi)\b"
    r"|\b\d+\b"
    r"|\b[a-z][a-z0-9+./_-]*\b)",
    re.IGNORECASE,
)
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9_.() -]+\.zip\Z")
_MISSING_TOKENS = frozenset({"-99.99", "-999"})
_DAT_COLUMNS = (
    ("market", "market"),
    ("be_me", "high"),
    ("be_me", "low"),
    ("e_p", "high"),
    ("e_p", "low"),
    ("ce_p", "high"),
    ("ce_p", "low"),
    ("dividend_yield", "high"),
    ("dividend_yield", "low"),
    ("dividend_yield", "zero"),
)
_DAT_VALUE_HEADER = ("mkt", "high", "low", "high", "low", "high", "low", "high", "low", "zero")


class FrenchProviderError(RuntimeError):
    """Sanitized stable error from official-data transport or parsing."""

    def __init__(self, code: str, *, status: int | None = None) -> None:
        self.code = code
        self.status = status
        suffix = "" if status is None else f" status={status}"
        super().__init__(code + suffix)


@dataclass(frozen=True)
class SourceObservation:
    """Typed intermediate row parsed from one official table cell."""

    date: date
    key: str
    value: float | None
    source_value: str


@dataclass(frozen=True)
class SourceBreakpoint:
    """Typed intermediate breakpoint and sample-count row."""

    date: date
    percentile: int
    value: float | None
    source_value: str
    count_1: int | None
    count_2: int | None


@dataclass(frozen=True)
class SourceDatReturn:
    """Typed intermediate return row from one official whitespace table."""

    date: date
    sort: str
    portfolio: str
    value: float | None
    source_value: str


@dataclass(frozen=True)
class SourceDatRatio:
    """Typed annual ratio row from a country ``.Dat`` archive."""

    date: date
    firms: int
    bm: float | None
    ep: float | None
    ce_p: float | None
    yld: float | None


def fetch_archive(filename: str, timeout: float | None) -> bytes:
    """GET one internally mapped ZIP through the shared governed client."""
    if _ARCHIVE_NAME.fullmatch(filename) is None or "/" in filename or ".." in filename:
        raise FrenchProviderError("FRENCH_UNMAPPED_ARCHIVE")
    response = None
    try:
        response = get_shared_http_client().get(
            FRENCH_BASE + filename,
            timeout=timeout if timeout is not None else 30.0,
            source="famafrench",
        )
    except RequestBudgetError:
        raise
    except HttpFetchError as exc:
        raise FrenchProviderError("FRENCH_HTTP_ERROR", status=exc.status) from None
    except Exception:
        raise FrenchProviderError("FRENCH_TRANSPORT_ERROR") from None
    status = getattr(response, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        raise FrenchProviderError("FRENCH_TRANSPORT_ERROR")
    if not 200 <= status < 300:
        raise FrenchProviderError("FRENCH_HTTP_ERROR", status=status)
    payload = getattr(response, "content", None)
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ARCHIVE_BYTES:
        raise FrenchProviderError("FRENCH_ARCHIVE_SIZE")
    return payload


def archive_text(
    payload: bytes,
    *,
    suffix: Literal[".csv", ".txt", ".dat"],
    member_name: str | None = None,
) -> str:
    """Select a single flat ZIP member without extracting archive paths."""
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_ARCHIVE_BYTES:
        raise FrenchProviderError("FRENCH_BAD_ARCHIVE")
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except (OSError, zipfile.BadZipFile, ValueError):
        raise FrenchProviderError("FRENCH_BAD_ARCHIVE") from None
    with archive:
        members = archive.infolist()
        if not members or len(members) > MAX_MEMBER_COUNT:
            raise FrenchProviderError("FRENCH_BAD_ARCHIVE_MEMBERS")
        seen: set[str] = set()
        selected: list[zipfile.ZipInfo] = []
        for member in members:
            name = member.filename
            path = PurePosixPath(name)
            if (
                name in seen
                or path.is_absolute()
                or len(path.parts) != 1
                or path.name in {"", ".", ".."}
                or "\\" in name
                or member.flag_bits & 0x1
                or S_ISLNK(member.external_attr >> 16)
            ):
                raise FrenchProviderError("FRENCH_UNSAFE_ARCHIVE_MEMBER")
            seen.add(name)
            if member.is_dir():
                continue
            if member.file_size > MAX_MEMBER_BYTES or member.file_size < 1:
                raise FrenchProviderError("FRENCH_ARCHIVE_MEMBER_SIZE")
            if name.casefold().endswith(suffix) and (member_name is None or name == member_name):
                selected.append(member)
        if len(selected) != 1:
            raise FrenchProviderError("FRENCH_ARCHIVE_MEMBER_MISMATCH")
        member = selected[0]
        try:
            raw = archive.read(member)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise FrenchProviderError("FRENCH_BAD_ARCHIVE_MEMBER") from None
        if len(raw) != member.file_size or len(raw) > MAX_MEMBER_BYTES:
            raise FrenchProviderError("FRENCH_ARCHIVE_MEMBER_SIZE")
    try:
        return raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1252", errors="strict")
        except UnicodeDecodeError:
            raise FrenchProviderError("FRENCH_BAD_TEXT_ENCODING") from None


def parse_period(token: str, frequency: str) -> date:
    """Parse exactly the requested source precision; do not infer frequency."""
    if _DATE_TOKEN.fullmatch(token) is None:
        raise ValueError("invalid period token")
    formats = {
        "annual": (4, "%Y"),
        "monthly": (6, "%Y%m"),
        "daily": (8, "%Y%m%d"),
        "weekly": (8, "%Y%m%d"),
    }
    spec = formats.get(frequency)
    if spec is None or len(token) != spec[0]:
        raise ValueError("source period precision does not match requested frequency")
    return datetime.strptime(token, spec[1]).date()


def parse_number(token: str) -> tuple[float | None, str]:
    """Parse a finite decimal or the two French-library missing sentinels."""
    text = token.strip()
    if text in _MISSING_TOKENS:
        return None, text
    if _NUMBER_TOKEN.fullmatch(text) is None:
        raise ValueError("invalid numeric source cell")
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite numeric source cell")
    return value, text


def _csv_fields(line: str) -> list[str]:
    try:
        return [field.strip() for field in next(csv.reader([line]))]
    except (csv.Error, StopIteration):
        raise FrenchProviderError("FRENCH_BAD_CSV") from None


def _frequency_of_token(token: str) -> str | None:
    return {4: "annual", 6: "monthly", 8: "daily"}.get(len(token)) if token.isdigit() else None


def _date_row(token: str, frequency: str) -> bool:
    if not token.isdigit():
        return False
    if frequency in {"daily", "weekly"}:
        return len(token) == 8
    return (frequency == "monthly" and len(token) == 6) or (
        frequency == "annual" and len(token) == 4
    )


def parse_factor_table(
    text: str,
    *,
    dataset: str,
    frequency: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceObservation]:
    """Read the requested period block from a preamble-plus-CSV factor archive."""
    lines = text.splitlines()
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in lines:
        if not line.strip():
            if block:
                blocks.append(block)
                block = []
        else:
            block.append(line)
    if block:
        blocks.append(block)
    result: list[SourceObservation] = []
    found_table = False
    for lines_in_block in blocks:
        indexed_rows: list[tuple[int, list[str]]] = []
        for index, line in enumerate(lines_in_block):
            fields = _csv_fields(line)
            if fields and _date_row(fields[0], frequency):
                indexed_rows.append((index, fields))
        if not indexed_rows:
            continue
        header_index = -1
        header: list[str] = []
        first_data_index = indexed_rows[0][0]
        for index, line in enumerate(lines_in_block[:first_data_index]):
            fields = _csv_fields(line)
            if fields and fields[0] == "" and len(fields) > 1 and all(fields[1:]):
                header_index = index
                header = fields[1:]
        if header_index < 0 or len(header) != len(set(header)):
            raise FrenchProviderError("FRENCH_FACTOR_HEADER_MISSING")
        found_table = True
        for _, fields in indexed_rows:
            if len(fields) != len(header) + 1:
                raise FrenchProviderError("FRENCH_FACTOR_COLUMN_COUNT")
            try:
                record_date = parse_period(fields[0], frequency)
            except ValueError:
                raise FrenchProviderError("FRENCH_FACTOR_PERIOD_MISMATCH") from None
            if start_date is not None and record_date < start_date:
                continue
            if end_date is not None and record_date > end_date:
                continue
            for factor, token in zip(header, fields[1:], strict=True):
                try:
                    value, source_value = parse_number(token)
                except ValueError:
                    raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
                result.append(SourceObservation(record_date, factor, value, source_value))
                if len(result) > MAX_OUTPUT_ROWS:
                    raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not found_table:
        raise FrenchProviderError("FRENCH_FACTOR_TABLE_NOT_FOUND")
    if not result:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return result


def parse_factor_text_table(
    text: str,
    *,
    dataset: str,
    frequency: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceObservation]:
    """Read a period block from the official whitespace-delimited TXT format."""
    del dataset  # The selected dataset is retained by the typed fetcher, not a cell.
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            if block:
                blocks.append(block)
                block = []
        else:
            block.append(line)
    if block:
        blocks.append(block)

    result: list[SourceObservation] = []
    found_table = False
    for lines_in_block in blocks:
        indexed_rows: list[tuple[int, list[str]]] = []
        for index, line in enumerate(lines_in_block):
            fields = line.split()
            if fields and _date_row(fields[0], frequency):
                indexed_rows.append((index, fields))
        if not indexed_rows:
            continue
        first_data_index = indexed_rows[0][0]
        header: list[str] = []
        for line in lines_in_block[:first_data_index]:
            candidate = line.split()
            if candidate and all(_DATE_TOKEN.fullmatch(token) is None for token in candidate):
                header = candidate
        if not header or len(header) != len(set(header)):
            raise FrenchProviderError("FRENCH_FACTOR_HEADER_MISSING")
        found_table = True
        for _, fields in indexed_rows:
            if len(fields) != len(header) + 1:
                raise FrenchProviderError("FRENCH_FACTOR_COLUMN_COUNT")
            try:
                record_date = parse_period(fields[0], frequency)
            except ValueError:
                raise FrenchProviderError("FRENCH_FACTOR_PERIOD_MISMATCH") from None
            if start_date is not None and record_date < start_date:
                continue
            if end_date is not None and record_date > end_date:
                continue
            for factor, token in zip(header, fields[1:], strict=True):
                try:
                    value, source_value = parse_number(token)
                except ValueError:
                    raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
                result.append(SourceObservation(record_date, factor, value, source_value))
                if len(result) > MAX_OUTPUT_ROWS:
                    raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not found_table:
        raise FrenchProviderError("FRENCH_FACTOR_TABLE_NOT_FOUND")
    if not result:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return result


def parse_portfolio_table(
    text: str,
    *,
    frequency: str,
    weighting: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceObservation]:
    """Parse one weighting/frequency return section from an official CSV archive."""
    result: list[SourceObservation] = []
    found_matching_section = False
    lines = text.splitlines()
    blocks: list[list[str]] = []
    block: list[str] = []
    for line in lines:
        if not line.strip():
            if block:
                blocks.append(block)
                block = []
        else:
            block.append(line)
    if block:
        blocks.append(block)
    title_needle = "value weighted returns" if weighting == "value" else "equal weighted returns"
    for lines_in_block in blocks:
        indexed_rows: list[tuple[int, list[str]]] = []
        for index, line in enumerate(lines_in_block):
            fields = _csv_fields(line)
            if fields and _date_row(fields[0], frequency):
                indexed_rows.append((index, fields))
        if not indexed_rows:
            continue
        title = " ".join(lines_in_block[: indexed_rows[0][0]]).casefold()
        if title_needle not in title:
            continue
        found_matching_section = True
        header: list[str] = []
        first_data_index = indexed_rows[0][0]
        for line in lines_in_block[:first_data_index]:
            fields = _csv_fields(line)
            if fields and fields[0] == "" and len(fields) > 1 and all(fields[1:]):
                header = fields[1:]
        if not header or len(header) != len(set(header)):
            raise FrenchProviderError("FRENCH_PORTFOLIO_HEADER_MISSING")
        for _, fields in indexed_rows:
            if len(fields) != len(header) + 1:
                raise FrenchProviderError("FRENCH_PORTFOLIO_COLUMN_COUNT")
            try:
                record_date = parse_period(fields[0], frequency)
            except ValueError:
                raise FrenchProviderError("FRENCH_PORTFOLIO_PERIOD_MISMATCH") from None
            if start_date is not None and record_date < start_date:
                continue
            if end_date is not None and record_date > end_date:
                continue
            for portfolio, token in zip(header, fields[1:], strict=True):
                try:
                    value, source_value = parse_number(token)
                except ValueError:
                    raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
                result.append(SourceObservation(record_date, portfolio, value, source_value))
                if len(result) > MAX_OUTPUT_ROWS:
                    raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not found_matching_section:
        raise FrenchProviderError("FRENCH_PORTFOLIO_TABLE_NOT_FOUND")
    if not result:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return result


def _portfolio_header_label(value: str) -> str:
    """Normalize compact official labels while retaining their sort meanings."""
    compact = re.sub(r"\s+", "", value).casefold()
    labels = {
        "small": "Small",
        "big": "Big",
        "lobm": "Lo BM",
        "hibm": "Hi BM",
        "lob/m": "Lo B/M",
        "hib/m": "Hi B/M",
        "lobe/me": "Lo BE/ME",
        "hibe/me": "Hi BE/ME",
        "loop": "Lo OP",
        "hiop": "Hi OP",
        "loinv": "Lo INV",
        "hiinv": "Hi INV",
        "lome": "Lo ME",
        "hime": "Hi ME",
        "loe/p": "Lo E/P",
        "hie/p": "Hi E/P",
        "locf/p": "Lo CF/P",
        "hicf/p": "Hi CF/P",
        "lod/p": "Lo D/P",
        "hid/p": "Hi D/P",
        "lomom": "Lo Mom",
        "himom": "Hi Mom",
    }
    if compact in labels:
        return labels[compact]
    return " ".join(value.split())


def _portfolio_header_components(line: str) -> list[tuple[float, str]]:
    return [
        ((match.start() + match.end()) / 2.0, _portfolio_header_label(match.group()))
        for match in _PORTFOLIO_HEADER_TOKEN.finditer(line)
    ]


def parse_portfolio_text_table(
    text: str,
    *,
    frequency: str,
    weighting: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceObservation]:
    """Parse an official fixed-column TXT portfolio table and its tiered header."""
    title_needle = (
        "average value weighted returns"
        if weighting == "value"
        else "average equal weighted returns"
    )
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    for line in text.splitlines():
        lower = line.casefold()
        if (
            "average" in lower
            and "returns" in lower
            and ("value weighted" in lower or "equal weighted" in lower)
        ):
            if current_title is not None:
                sections.append((current_title, current_lines))
            current_title = line
            current_lines = []
        elif current_title is not None:
            current_lines.append(line)
    if current_title is not None:
        sections.append((current_title, current_lines))

    result: list[SourceObservation] = []
    found_matching_section = False
    for title, lines in sections:
        if title_needle not in title.casefold():
            continue
        indexed_rows: list[tuple[int, list[str]]] = []
        for index, line in enumerate(lines):
            fields = line.split()
            if fields and _date_row(fields[0], frequency):
                indexed_rows.append((index, fields))
        if not indexed_rows:
            continue
        found_matching_section = True
        first_data_index = indexed_rows[0][0]
        header_rows = [
            components
            for line in lines[:first_data_index]
            if (components := _portfolio_header_components(line))
        ]
        if not header_rows:
            raise FrenchProviderError("FRENCH_PORTFOLIO_HEADER_MISSING")

        expected_columns = len(indexed_rows[0][1]) - 1
        if expected_columns < 1:
            raise FrenchProviderError("FRENCH_PORTFOLIO_COLUMN_COUNT")
        for _, fields in indexed_rows:
            if len(fields) != expected_columns + 1:
                raise FrenchProviderError("FRENCH_PORTFOLIO_COLUMN_COUNT")
            if any(len(row) == 0 for row in header_rows):
                raise FrenchProviderError("FRENCH_PORTFOLIO_HEADER_MISSING")
            labels_by_column: list[list[str]] = [[] for _ in range(expected_columns)]
            for header in header_rows:
                if expected_columns % len(header) == 0:
                    group_size = expected_columns // len(header)
                    for column in range(expected_columns):
                        labels_by_column[column].append(header[column // group_size][1])
                else:
                    for column in range(expected_columns):
                        label_index = min(
                            len(header) - 1,
                            int((column + 0.5) * len(header) / expected_columns),
                        )
                        labels_by_column[column].append(header[label_index][1])
            portfolios = [
                " ".join(label for label in labels if label).strip() for labels in labels_by_column
            ]
            if any(not portfolio for portfolio in portfolios) or len(portfolios) != len(
                set(portfolios)
            ):
                raise FrenchProviderError("FRENCH_PORTFOLIO_HEADER_MISSING")
            try:
                record_date = parse_period(fields[0], frequency)
            except ValueError:
                raise FrenchProviderError("FRENCH_PORTFOLIO_PERIOD_MISMATCH") from None
            if start_date is not None and record_date < start_date:
                continue
            if end_date is not None and record_date > end_date:
                continue
            for portfolio, token in zip(portfolios, fields[1:], strict=True):
                try:
                    value, source_value = parse_number(token)
                except ValueError:
                    raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
                result.append(SourceObservation(record_date, portfolio, value, source_value))
                if len(result) > MAX_OUTPUT_ROWS:
                    raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not found_matching_section:
        raise FrenchProviderError("FRENCH_PORTFOLIO_TABLE_NOT_FOUND")
    if not result:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return result


def parse_breakpoint_table(
    text: str,
    *,
    dataset: str,
    frequency: str,
    period_frequency: str | None = None,
    annual_period_end_month: int | None = None,
    archive_format: Literal["csv", "txt"] = "csv",
    count_columns: int,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceBreakpoint]:
    """Parse breakpoint files using their source precision and reviewed period anchors."""
    result: list[SourceBreakpoint] = []
    found_period = False
    source_period_frequency = period_frequency or frequency
    if annual_period_end_month is not None and (
        source_period_frequency != "annual" or not 1 <= annual_period_end_month <= 12
    ):
        raise FrenchProviderError("FRENCH_BREAKPOINT_PERIOD_MISMATCH")
    for line in text.splitlines():
        fields = _csv_fields(line) if archive_format == "csv" else line.split()
        if not fields or not fields[0].isdigit():
            continue
        token = fields[0]
        if not _date_row(token, source_period_frequency):
            if len(token) in {4, 6, 8} and _frequency_of_token(token) is not None:
                found_period = True
                continue
            continue
        found_period = True
        try:
            record_date = parse_period(token, source_period_frequency)
        except ValueError:
            raise FrenchProviderError("FRENCH_BREAKPOINT_PERIOD_MISMATCH") from None
        if annual_period_end_month is not None:
            last_day = calendar.monthrange(record_date.year, annual_period_end_month)[1]
            record_date = date(record_date.year, annual_period_end_month, last_day)
        expected = 1 + count_columns + BREAKPOINT_PERCENTILE_COUNT
        if len(fields) != expected:
            raise FrenchProviderError("FRENCH_BREAKPOINT_COLUMN_COUNT")
        if start_date is not None and record_date < start_date:
            continue
        if end_date is not None and record_date > end_date:
            continue
        count_values: list[int] = []
        for token_count in fields[1 : 1 + count_columns]:
            if not token_count.isdigit():
                raise FrenchProviderError("FRENCH_BREAKPOINT_BAD_COUNT")
            count_values.append(int(token_count))
        for index, token_value in enumerate(fields[1 + count_columns :]):
            try:
                value, source_value = parse_number(token_value)
            except ValueError:
                raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
            result.append(
                SourceBreakpoint(
                    date=record_date,
                    percentile=(index + 1) * 5,
                    value=value,
                    source_value=source_value,
                    count_1=count_values[0] if count_values else None,
                    count_2=count_values[1] if len(count_values) > 1 else None,
                )
            )
            if len(result) > MAX_OUTPUT_ROWS:
                raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not found_period:
        raise FrenchProviderError("FRENCH_BREAKPOINT_TABLE_NOT_FOUND")
    if not result:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return result


def parse_dat_returns(
    text: str,
    *,
    frequency: str,
    currency_basis: str,
    data_requirement: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceDatReturn]:
    """Read a selected return section from the official whitespace ``.Dat`` format."""
    selected: list[SourceDatReturn] = []
    current_title = ""
    saw_value_header = False
    saw_candidate_section = False
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.casefold()
        if "returns" in lower and ("dollar" in lower or "local" in lower):
            current_title = stripped
            saw_value_header = False
            if currency_basis in lower and (
                (data_requirement == "required" and "required" in lower and "not req" not in lower)
                or (data_requirement == "not_required" and "not req" in lower)
            ):
                saw_candidate_section = True
        elif "average of annual" in lower or "ratios" in lower:
            current_title = ""
        if not current_title:
            continue
        tokens = stripped.split()
        if tokens and tuple(token.casefold() for token in tokens) == _DAT_VALUE_HEADER:
            saw_value_header = True
            continue
        if tokens and tokens[0].casefold() in {"mkt", "market"}:
            raise FrenchProviderError("FRENCH_DAT_HEADER_MISMATCH")
        if not tokens or not tokens[0].isdigit():
            continue
        token_date = tokens[0]
        if len(token_date) not in {4, 6, 8}:
            continue
        if not _date_row(token_date, frequency):
            continue
        try:
            record_date = parse_period(token_date, frequency)
        except ValueError:
            raise FrenchProviderError("FRENCH_DAT_PERIOD_MISMATCH") from None
        if currency_basis not in current_title.casefold():
            continue
        title = current_title.casefold()
        req_matches = (
            data_requirement == "required" and "required" in title and "not req" not in title
        ) or (data_requirement == "not_required" and "not req" in title)
        if not req_matches:
            continue
        if not saw_value_header:
            raise FrenchProviderError("FRENCH_DAT_HEADER_MISMATCH")
        if len(tokens) != len(_DAT_COLUMNS) + 1:
            raise FrenchProviderError("FRENCH_DAT_COLUMN_COUNT")
        if start_date is not None and record_date < start_date:
            continue
        if end_date is not None and record_date > end_date:
            continue
        for (sort, portfolio), token in zip(_DAT_COLUMNS, tokens[1:], strict=True):
            try:
                value, source_value = parse_number(token)
            except ValueError:
                raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
            selected.append(SourceDatReturn(record_date, sort, portfolio, value, source_value))
            if len(selected) > MAX_OUTPUT_ROWS:
                raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not saw_candidate_section:
        raise FrenchProviderError("FRENCH_DAT_TABLE_NOT_FOUND")
    if not selected:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return selected


def parse_dat_ratios(
    text: str,
    *,
    data_requirement: str,
    start_date: date | None,
    end_date: date | None,
) -> list[SourceDatRatio]:
    """Parse the documented annual country ratio table with ratios scaled by 100."""
    selected: list[SourceDatRatio] = []
    in_annual_averages = False
    selected_requirement = False
    saw_candidate_section = False
    saw_header = False
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.casefold()
        if "average of annual value-weight averages" in lower:
            in_annual_averages = True
            selected_requirement = False
            saw_header = False
            continue
        if not in_annual_averages:
            continue
        if lower.startswith("all 4 data items"):
            selected_requirement = (
                data_requirement == "required" and "required" in lower and "not req" not in lower
            ) or (data_requirement == "not_required" and "not req" in lower)
            if selected_requirement:
                saw_candidate_section = True
            saw_header = False
            continue
        if "ratios x 100" in lower:
            continue
        tokens = stripped.split()
        if tuple(token.casefold() for token in tokens) == ("firms", "b/m", "e/p", "ce/p", "yld"):
            if selected_requirement:
                saw_header = True
            continue
        if not tokens or not tokens[0].isdigit() or len(tokens[0]) != 4:
            continue
        if not selected_requirement:
            continue
        if not saw_header:
            raise FrenchProviderError("FRENCH_DAT_RATIO_HEADER_MISMATCH")
        if len(tokens) != 6:
            raise FrenchProviderError("FRENCH_DAT_RATIO_COLUMN_COUNT")
        try:
            record_date = parse_period(tokens[0], "annual")
        except ValueError:
            raise FrenchProviderError("FRENCH_DAT_RATIO_PERIOD_MISMATCH") from None
        if start_date is not None and record_date < start_date:
            continue
        if end_date is not None and record_date > end_date:
            continue
        if not tokens[1].isdigit():
            raise FrenchProviderError("FRENCH_DAT_RATIO_BAD_FIRM_COUNT")
        values: list[float | None] = []
        for token in tokens[2:]:
            try:
                value, _source_value = parse_number(token)
            except ValueError:
                raise FrenchProviderError("FRENCH_BAD_NUMERIC_VALUE") from None
            values.append(value)
        selected.append(
            SourceDatRatio(
                date=record_date,
                firms=int(tokens[1]),
                bm=values[0],
                ep=values[1],
                ce_p=values[2],
                yld=values[3],
            )
        )
        if len(selected) > MAX_OUTPUT_ROWS:
            raise FrenchProviderError("FRENCH_RESULT_LIMIT")
    if not saw_candidate_section:
        raise FrenchProviderError("FRENCH_DAT_RATIO_TABLE_NOT_FOUND")
    if not selected:
        raise FrenchProviderError("FRENCH_EMPTY_RESULT")
    return selected
