"""EIA Short-Term Energy Outlook series and archived table adapter."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, TypeGuard
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pydantic import field_validator, model_validator

from opendata.data.capability import Capability
from opendata.data.models.eia import ShortTermEnergyOutlook
from opendata.data.protocol import FetchContext, Fetcher, FetchResult, MultiSymbolQueryParams
from opendata.data.providers.eia.models import _client

if TYPE_CHECKING:
    from collections.abc import Iterable

_TABLES = Literal[
    "01",
    "02",
    "03a",
    "03b",
    "03c",
    "03d",
    "03e",
    "04a",
    "04b",
    "04c",
    "04d",
    "05a",
    "05b",
    "06",
    "07a",
    "07b",
    "07c",
    "07d1",
    "07d2",
    "07e",
    "08",
    "09a",
    "09b",
    "09c",
    "10a",
    "10b",
]
_TABLE_IDS = frozenset(
    {
        "01",
        "02",
        "03a",
        "03b",
        "03c",
        "03d",
        "03e",
        "04a",
        "04b",
        "04c",
        "04d",
        "05a",
        "05b",
        "06",
        "07a",
        "07b",
        "07c",
        "07d1",
        "07d2",
        "07e",
        "08",
        "09a",
        "09b",
        "09c",
        "10a",
        "10b",
    }
)
_SERIES_ID = re.compile(r"[A-Za-z0-9_.$-]{1,80}\Z")
_TABLE_TITLE = re.compile(
    r"^Table\s+(\d{1,2})([a-z])?(?:\s*(?:\(\s*([12])\s*\)|part\s*([12])))?",
    re.IGNORECASE,
)
_ARCHIVE_FILE = re.compile(r"[a-z]{3}\d{2}_base\.(xlsx?)\Z", re.IGNORECASE)
_ARCHIVE_DATE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_MONTH_CODE = re.compile(r"(19\d{2}|20\d{2}|21\d{2})(0[1-9]|1[0-2])\Z")
_INTEGER = re.compile(r"\d+\Z")
_UNIT_CUES = (
    "barrel",
    "btu",
    "cent",
    "cubic",
    "degree",
    "dollar",
    "gigawatt",
    "index",
    "kilowatt",
    "million",
    "billion",
    "quadrillion",
    "percent",
    "per ",
    "short ton",
    "ton",
)
ARCHIVE_URL = "https://www.eia.gov/outlooks/steo/outlook.php/archives/1Q95.pdf"
WORKBOOK_BASE_URL = "https://www.eia.gov/outlooks/steo/archives/"
API_URL = "https://api.eia.gov/v2/steo/data/"
PAGE_SIZE = 5_000
MAX_ROWS = 50_000
MAX_SYMBOLS = 20
MAX_WORKBOOK_UNCOMPRESSED_BYTES = 100 * 1024 * 1024


class _ReadableCell(Protocol):
    value: object


class _ReadableWorksheet(Protocol):
    def iter_rows(self, *, values_only: bool = ...) -> Iterable[tuple[object, ...]]: ...

    def cell(self, row: int, column: int) -> _ReadableCell: ...


class _ReadableWorkbook(Protocol):
    sheetnames: list[str]

    def __getitem__(self, key: str) -> _ReadableWorksheet: ...

    def close(self) -> None: ...


class ShortTermEnergyOutlookQuery(MultiSymbolQueryParams):
    """Query a public STEO table or named series from a dated API or release."""

    source: Literal["eia"] = "eia"
    market: Literal["us"] = "us"
    symbol: str | list[str] | None = None
    table: _TABLES | None = "01"
    release_date: date | None = None
    frequency: Literal["month", "quarter", "annual"] = "month"

    @field_validator("symbol", mode="before")
    @classmethod
    def _series_ids(cls, value: object) -> str | list[str] | None:
        if value is None:
            return None
        if isinstance(value, list):
            if not value or len(value) > MAX_SYMBOLS:
                raise ValueError(
                    f"symbol must be a series ID or a list of at most {MAX_SYMBOLS} IDs"
                )
            values = []
            for item in value:
                if type(item) is not str:
                    raise ValueError(
                        f"symbol must be a series ID or a list of at most {MAX_SYMBOLS} IDs"
                    )
                values.append(item)
        elif type(value) is str:
            values = [value]
        else:
            raise ValueError(f"symbol must be a series ID or a list of at most {MAX_SYMBOLS} IDs")
        normalized: list[str] = []
        for item in values:
            candidate = item.strip()
            if not _SERIES_ID.fullmatch(candidate):
                raise ValueError("symbol contains an invalid EIA series ID")
            if candidate not in normalized:
                normalized.append(candidate)
        if isinstance(value, list):
            return normalized
        return normalized[0]

    @field_validator("table", mode="before")
    @classmethod
    def _default_table(cls, value: object) -> object:
        return "01" if value is None else value

    @field_validator("release_date", mode="before")
    @classmethod
    def _date_only_release(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("release_date must be an ISO calendar date") from exc
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("release_date must be an ISO calendar date without a time")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _date_only(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str):
            try:
                parsed = date.fromisoformat(value)
            except ValueError as exc:
                raise ValueError("date bounds must be ISO calendar dates") from exc
            if parsed.isoformat() == value:
                return parsed
        raise ValueError("date bounds must be ISO calendar dates without a time")

    @model_validator(mode="after")
    def _validate_request(self) -> ShortTermEnergyOutlookQuery:
        if (
            self.start_date is not None
            and self.end_date is not None
            and self.start_date > self.end_date
        ):
            raise ValueError("start_date must be on or before end_date")
        if (self.symbol is None or self.release_date is not None) and self.frequency != "month":
            raise ValueError("archived STEO tables support monthly frequency only")
        return self


class ShortTermEnergyOutlookFetcher(Fetcher[ShortTermEnergyOutlookQuery, list[dict[str, Any]]]):
    """Fetch EIA API series or a selected official Excel release table."""

    async_mode = "bounded_thread"
    canonical_model: ClassVar[str | None] = "ShortTermEnergyOutlook"
    row_model: ClassVar[type[ShortTermEnergyOutlook]] = ShortTermEnergyOutlook
    capability: ClassVar[Capability] = Capability(
        asset_class="energy",
        domain="energy_short_term_energy_outlook",
        period="variable",
        market="us",
        source="eia",
        verified=False,
        notes=(
            "The EIA API provides named series. When no symbol is given, the official STEO "
            "archive workbook supplies table membership, monthly values, release date, and "
            "historical-versus-forecast flags."
        ),
    )

    def transform_query(self, **kwargs: object) -> ShortTermEnergyOutlookQuery:
        """Validate series identity, table, release selector, frequency, and dates."""
        return ShortTermEnergyOutlookQuery.model_validate(kwargs)

    def extract_data(
        self, params: ShortTermEnergyOutlookQuery, ctx: FetchContext
    ) -> list[dict[str, Any]]:
        """Fetch an archived table or every selected EIA API series."""
        if params.symbol is None or params.release_date is not None:
            return _fetch_archive_table(params, ctx)

        api_key = _client.api_key()
        symbols = [params.symbol] if isinstance(params.symbol, str) else params.symbol
        if symbols is None:  # Defensive: transform_query normalizes this case.
            raise _client.EIAProviderError("EIA_STEO_SERIES_REQUIRED")
        api_frequency = {
            "month": "monthly",
            "quarter": "quarterly",
            "annual": "annual",
        }[params.frequency]
        output: list[dict[str, Any]] = []
        for symbol in symbols:
            offset = 0
            total: int | None = None
            while total is None or offset < total:
                page_length = min(PAGE_SIZE, MAX_ROWS + 1 - len(output))
                if page_length <= 0:
                    raise _client.EIAProviderError("EIA_RESULT_LIMIT_EXCEEDED")
                response_body = _client.get_json(
                    API_URL,
                    params={
                        "api_key": api_key,
                        "data[]": "value",
                        "facets[seriesId][]": symbol,
                        "frequency": api_frequency,
                        "sort[0][column]": "period",
                        "sort[0][direction]": "asc",
                        "offset": str(offset),
                        "length": str(page_length),
                    },
                    context=ctx,
                )
                if not _is_string_keyed_dict(response_body):
                    raise _client.EIAProviderError("EIA_BAD_RESPONSE_SHAPE")
                response = response_body
                page_total = _parse_total(response.get("total"))
                if total is None:
                    total = page_total
                    if total > MAX_ROWS or total > MAX_ROWS - len(output):
                        raise _client.EIAProviderError("EIA_RESULT_LIMIT_EXCEEDED")
                elif page_total != total:
                    raise _client.EIAProviderError("EIA_INCONSISTENT_PAGE_TOTAL")
                if response.get("frequency") != api_frequency:
                    raise _client.EIAProviderError("EIA_FREQUENCY_MISMATCH")
                page = response.get("data")
                if not _is_api_rows(page):
                    raise _client.EIAProviderError("EIA_BAD_PAGE")
                if len(page) > page_length:
                    raise _client.EIAProviderError("EIA_BAD_PAGE")
                if offset < total and not page:
                    raise _client.EIAProviderError("EIA_TRUNCATED_PAGE")
                for row in page:
                    returned_symbol = row.get("seriesId")
                    if (
                        not isinstance(returned_symbol, str)
                        or returned_symbol.casefold() != symbol.casefold()
                    ):
                        raise _client.EIAProviderError("EIA_BAD_SERIES_ID")
                    output.append(row)
                    if len(output) > MAX_ROWS:
                        raise _client.EIAProviderError("EIA_RESULT_LIMIT_EXCEEDED")
                offset += len(page)
                if len(page) < page_length and offset < total:
                    raise _client.EIAProviderError("EIA_TRUNCATED_PAGE")
                if total == 0:
                    break
        return output

    def transform_data(
        self, raw: list[dict[str, Any]], params: ShortTermEnergyOutlookQuery
    ) -> FetchResult:
        """Normalize API series or archived table values into the typed contract."""
        if params.symbol is None or params.release_date is not None:
            return _transform_archive_rows(raw, params)

        symbols = [params.symbol] if isinstance(params.symbol, str) else params.symbol
        if symbols is None:
            raise _client.EIAProviderError("EIA_STEO_SERIES_REQUIRED")
        api_frequency = {
            "month": "monthly",
            "quarter": "quarterly",
            "annual": "annual",
        }[params.frequency]
        rows: list[ShortTermEnergyOutlook] = []
        requested = {symbol.casefold() for symbol in symbols}
        for row in raw:
            period = row.get("period")
            symbol = row.get("seriesId")
            value = row.get("value")
            if not isinstance(symbol, str) or symbol.casefold() not in requested:
                raise _client.EIAProviderError("EIA_BAD_SERIES_ID")
            observation_date = _parse_period(period, api_frequency)
            parsed_value = _parse_value(value)
            if parsed_value is None:
                continue
            unit = _optional_text(row.get("value-units"))
            title = _optional_text(row.get("seriesName"))
            if params.start_date is not None and observation_date < params.start_date:
                continue
            if params.end_date is not None and observation_date > params.end_date:
                continue
            try:
                rows.append(
                    ShortTermEnergyOutlook.model_validate(
                        {
                            "date": observation_date,
                            "release_date": None,
                            "is_forecast": None,
                            "table": None,
                            "symbol": symbol,
                            "order": None,
                            "title": title,
                            "value": parsed_value,
                            "unit": unit,
                        }
                    )
                )
            except (TypeError, ValueError):
                raise _client.EIAProviderError("EIA_BAD_STEO_ROW") from None
        return tuple(rows)


def _fetch_archive_table(
    params: ShortTermEnergyOutlookQuery, ctx: FetchContext
) -> list[dict[str, Any]]:
    """Resolve a release from EIA's archive page and read its official workbook."""
    archive_body, content_type = _client.get_bytes(ARCHIVE_URL, params=None, context=ctx)
    if content_type not in {"text/html", "application/xhtml+xml"}:
        raise _client.EIAProviderError("EIA_STEO_BAD_ARCHIVE_CONTENT_TYPE")
    release_date, workbook_url, extension = _select_release(archive_body, params.release_date)
    if extension != "xlsx":
        raise _client.EIAProviderError("EIA_STEO_ARCHIVE_FORMAT_UNSUPPORTED")
    workbook_body, _workbook_content_type = _client.get_bytes(
        workbook_url, params=None, context=ctx
    )
    if not workbook_body.startswith(b"PK"):
        raise _client.EIAProviderError("EIA_STEO_BAD_WORKBOOK")
    return _parse_workbook(workbook_body, params, release_date)


def _select_release(body: bytes, requested_date: date | None) -> tuple[date, str, str]:
    """Select an exact official release link from the archive issue table."""
    try:
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        raise _client.EIAProviderError("EIA_STEO_BAD_ARCHIVE_HTML") from None
    releases: dict[date, tuple[str, str]] = {}
    for row in soup.find_all("tr"):
        row_text = row.get_text(" ", strip=True)
        date_match = _ARCHIVE_DATE.search(row_text)
        if date_match is None:
            continue
        try:
            month, day, year = (int(part) for part in date_match.groups())
            release = date(year, month, day)
        except ValueError:
            continue
        for anchor in row.find_all("a", href=True):
            filename = str(anchor.get("href", "")).rsplit("/", 1)[-1]
            file_match = _ARCHIVE_FILE.fullmatch(filename)
            if file_match is None:
                continue
            extension = file_match.group(1).lower()
            releases[release] = (
                f"{WORKBOOK_BASE_URL}{filename}",
                "xlsx" if extension == "xlsx" else "xls",
            )
            break
    if not releases:
        raise _client.EIAProviderError("EIA_STEO_BAD_ARCHIVE_HTML")
    selected = requested_date if requested_date is not None else max(releases)
    link = releases.get(selected)
    if link is None:
        raise _client.EIAProviderError("EIA_STEO_RELEASE_NOT_FOUND")
    return selected, link[0], link[1]


def _parse_workbook(
    body: bytes, params: ShortTermEnergyOutlookQuery, release_date: date
) -> list[dict[str, Any]]:
    """Read selected table cells plus workbook-provided historical markers."""
    try:
        with ZipFile(BytesIO(body)) as archive:
            unpacked_size = sum(info.file_size for info in archive.infolist())
            if unpacked_size > MAX_WORKBOOK_UNCOMPRESSED_BYTES:
                raise _client.EIAProviderError("EIA_STEO_WORKBOOK_TOO_LARGE")
        workbook = load_workbook(BytesIO(body), read_only=True, data_only=True)
    except _client.EIAProviderError:
        raise
    except (BadZipFile, InvalidFileException, OSError, ValueError):
        raise _client.EIAProviderError("EIA_STEO_BAD_WORKBOOK") from None

    try:
        for required in ("Dates", "Contents"):
            if required not in workbook.sheetnames:
                raise _client.EIAProviderError("EIA_STEO_BAD_WORKBOOK")
        periods = _read_period_markers(workbook["Dates"])
        table_sheets = _read_table_map(workbook)
        symbols = _requested_symbols(params.symbol)
        selected_tables: list[tuple[str, str | None]] = []
        if symbols is None:
            query_table_id = params.table or "01"
            selected_tables.append((query_table_id, table_sheets.get(query_table_id)))
        elif params.release_date is not None:
            selected_tables.extend(
                (mapped_table_id, sheet_name)
                for mapped_table_id, sheet_name in table_sheets.items()
            )

        output: list[dict[str, Any]] = []
        for selected_table_id, sheet_name in selected_tables:
            if sheet_name is None:
                raise _client.EIAProviderError("EIA_STEO_TABLE_NOT_FOUND")
            output.extend(
                _read_table_sheet(
                    workbook[sheet_name],
                    selected_table_id,
                    periods,
                    release_date,
                    symbols,
                )
            )
            if len(output) > MAX_ROWS:
                raise _client.EIAProviderError("EIA_RESULT_LIMIT_EXCEEDED")
        return output
    finally:
        workbook.close()


def _read_period_markers(sheet: _ReadableWorksheet) -> dict[int, tuple[date, bool]]:
    """Read period columns and the workbook's 1=historical, 0=forecast flags."""
    rows = list(sheet.iter_rows(values_only=True))
    historical_rows = [
        index
        for index, row in enumerate(rows)
        if any(
            isinstance(value, str) and value.strip().casefold() == "historical" for value in row[:5]
        )
    ]
    for historical_index in historical_rows:
        marker_row = rows[historical_index]
        period_candidates = []
        for index in range(historical_index):
            candidate = rows[index]
            valid = sum(_month_date(value) is not None for value in candidate[2:])
            if valid >= 12:
                period_candidates.append((index, valid))
        if not period_candidates:
            continue
        period_index = max(period_candidates, key=lambda item: item[0])[0]
        periods: dict[int, tuple[date, bool]] = {}
        for column, month_value in enumerate(rows[period_index], start=1):
            if column < 3:
                continue
            observation_date = _month_date(month_value)
            if observation_date is None:
                continue
            marker = marker_row[column - 1] if column - 1 < len(marker_row) else None
            if type(marker) not in (int, float) or marker not in (0, 1):
                raise _client.EIAProviderError("EIA_STEO_HISTORICAL_MARKER_INVALID")
            periods[column] = (observation_date, marker == 0)
        if len(periods) >= 12:
            return periods
    raise _client.EIAProviderError("EIA_STEO_HISTORICAL_MARKER_MISSING")


def _month_date(value: object) -> date | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str) or _MONTH_CODE.fullmatch(value) is None:
        return None
    try:
        return date(int(value[:4]), int(value[4:]), 1)
    except ValueError:
        return None


def _read_table_map(workbook: _ReadableWorkbook) -> dict[str, str]:
    """Pair the official Contents table list with its workbook tab order."""
    content_tables: list[str] = []
    for row in workbook["Contents"].iter_rows(values_only=True):
        for value in row:
            if isinstance(value, str):
                table_id = _table_id_from_title(value)
                if table_id is not None:
                    content_tables.append(table_id)
    sheets = workbook.sheetnames[2:]
    if not content_tables or len(content_tables) != len(sheets):
        raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_MAP")
    if len(set(content_tables)) != len(content_tables):
        raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_MAP")
    mapping = dict(zip(content_tables, sheets, strict=True))
    for table_id, sheet_name in mapping.items():
        if table_id not in _TABLE_IDS:
            raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_MAP")
        title = workbook[sheet_name].cell(1, 2).value
        title_id = _table_id_from_title(title) if isinstance(title, str) else None
        if title_id is not None and title_id != table_id:
            raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_MAP")
    return mapping


def _table_id_from_title(value: str) -> str | None:
    match = _TABLE_TITLE.match(value.strip())
    if match is None:
        return None
    number, suffix, parenthesized_part, word_part = match.groups()
    part = parenthesized_part or word_part or ""
    return f"{int(number):02d}{(suffix or '').lower()}{part}"


def _read_table_sheet(
    sheet: _ReadableWorksheet,
    table_id: str,
    periods: dict[int, tuple[date, bool]],
    release_date: date,
    symbols: set[str] | None,
) -> list[dict[str, Any]]:
    """Extract one wide monthly table without guessing missing values or units."""
    output: list[dict[str, Any]] = []
    header_label = sheet.cell(1, 2).value
    active_unit = _unit_from_text(header_label) if isinstance(header_label, str) else None
    section_title: str | None = None
    order = 0
    for source_row, row in enumerate(sheet.iter_rows(values_only=True), start=1):
        if source_row <= 4:
            continue
        code = row[0] if row else None
        label = row[1] if len(row) > 1 else None
        if code is None:
            if isinstance(label, str) and label.strip():
                heading = label.strip()
                unit = _unit_from_text(heading)
                if unit is not None:
                    active_unit = unit
                if table_id == "01":
                    section_title = heading
            elif label is not None and (not isinstance(label, str) or label.strip()):
                raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_ROW")
            continue
        if not isinstance(code, str) or not _SERIES_ID.fullmatch(code):
            raise _client.EIAProviderError("EIA_STEO_BAD_SERIES_ID")
        order += 1
        if symbols is not None and code.casefold() not in symbols:
            continue

        if table_id == "01":
            if isinstance(label, str) and _is_unit_text(label):
                title = section_title
                unit = _normalize_unit_cell(label)
            elif isinstance(label, str):
                title = label.strip()
                unit = active_unit
            else:
                title = section_title
                unit = active_unit
        else:
            if label is not None and not isinstance(label, str):
                raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_ROW")
            title = label.strip() if isinstance(label, str) and label.strip() else None
            unit = _unit_from_text(label) if isinstance(label, str) else None
            if unit is not None:
                active_unit = unit
            else:
                unit = active_unit

        for column, value in enumerate(row, start=1):
            if column < 3 or value is None:
                continue
            marker = periods.get(column)
            if marker is None:
                raise _client.EIAProviderError("EIA_STEO_PERIOD_COLUMN_MISSING")
            observation_date, is_forecast = marker
            if _parse_value(value) is None:
                continue
            output.append(
                {
                    "date": observation_date,
                    "release_date": release_date,
                    "is_forecast": is_forecast,
                    "table": table_id,
                    "symbol": code,
                    "order": order,
                    "title": title,
                    "value": value,
                    "unit": unit,
                }
            )
    return output


def _is_string_keyed_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(type(key) is str for key in value)


def _is_api_rows(value: object) -> TypeGuard[list[dict[str, object]]]:
    return isinstance(value, list) and all(_is_string_keyed_dict(row) for row in value)


def _is_unit_text(value: str) -> bool:
    normalized = value.strip().casefold()
    return (
        (_unit_from_text(value) is not None and normalized.startswith("("))
        or normalized.startswith("percent ")
        or normalized.startswith("percentage ")
    )


def _unit_from_text(value: str) -> str | None:
    for match in re.finditer(r"\(([^()]*)\)", value):
        candidate = match.group(1).strip()
        if any(cue in candidate.casefold() for cue in _UNIT_CUES):
            return candidate
    normalized = value.strip()
    if "percent" in normalized.casefold() or "percentage" in normalized.casefold():
        return normalized
    return None


def _normalize_unit_cell(value: str) -> str:
    normalized = value.strip()
    if normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized


def _requested_symbols(value: str | list[str] | None) -> set[str] | None:
    if value is None:
        return None
    values = [value] if isinstance(value, str) else value
    return {symbol.casefold() for symbol in values}


def _transform_archive_rows(
    raw: list[dict[str, Any]], params: ShortTermEnergyOutlookQuery
) -> FetchResult:
    symbols = _requested_symbols(params.symbol)
    selected_table = params.table or "01"
    rows: list[ShortTermEnergyOutlook] = []
    for row in raw:
        observation_date = row.get("date")
        symbol = row.get("symbol")
        table = row.get("table")
        value = _parse_value(row.get("value"))
        release_date = row.get("release_date")
        is_forecast = row.get("is_forecast")
        if type(observation_date) is not date:
            raise _client.EIAProviderError("EIA_STEO_BAD_PERIOD")
        if not isinstance(symbol, str) or not _SERIES_ID.fullmatch(symbol):
            raise _client.EIAProviderError("EIA_BAD_SERIES_ID")
        if not isinstance(table, str) or table not in _TABLE_IDS:
            raise _client.EIAProviderError("EIA_STEO_BAD_TABLE_ROW")
        if type(release_date) is not date or type(is_forecast) is not bool:
            raise _client.EIAProviderError("EIA_STEO_BAD_VINTAGE_METADATA")
        if value is None:
            continue
        if symbols is not None and symbol.casefold() not in symbols:
            continue
        if symbols is None and table != selected_table:
            continue
        if params.start_date is not None and observation_date < params.start_date:
            continue
        if params.end_date is not None and observation_date > params.end_date:
            continue
        if params.release_date is not None and release_date != params.release_date:
            raise _client.EIAProviderError("EIA_STEO_RELEASE_MISMATCH")
        title = _optional_text(row.get("title"))
        unit = _optional_text(row.get("unit"))
        try:
            rows.append(
                ShortTermEnergyOutlook.model_validate(
                    {
                        "date": observation_date,
                        "release_date": release_date,
                        "is_forecast": is_forecast,
                        "table": table,
                        "symbol": symbol,
                        "order": row.get("order"),
                        "title": title,
                        "value": value,
                        "unit": unit,
                    }
                )
            )
        except (TypeError, ValueError):
            raise _client.EIAProviderError("EIA_BAD_STEO_ROW") from None
    return tuple(rows)


def _parse_total(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and _INTEGER.fullmatch(value):
        return int(value)
    raise _client.EIAProviderError("EIA_BAD_TOTAL")


def _parse_period(value: object, frequency: str) -> date:
    if not isinstance(value, str):
        raise _client.EIAProviderError("EIA_BAD_PERIOD")
    try:
        if frequency == "monthly":
            match = re.fullmatch(r"(\d{4})-(\d{2})", value)
            if match is None:
                raise ValueError
            year, month = (int(part) for part in match.groups())
            return date(year, month, 1)
        if frequency == "quarterly":
            match = re.fullmatch(r"(\d{4})-?Q([1-4])", value, re.IGNORECASE)
            if match is None:
                raise ValueError
            year, quarter = int(match.group(1)), int(match.group(2))
            return date(year, ((quarter - 1) * 3) + 1, 1)
        if frequency == "annual" and re.fullmatch(r"\d{4}", value):
            return date(int(value), 1, 1)
    except ValueError:
        raise _client.EIAProviderError("EIA_BAD_PERIOD") from None
    raise _client.EIAProviderError("EIA_BAD_PERIOD")


def _parse_value(value: object) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip().casefold() in {
        "",
        "null",
        "w",
        "*",
        "-",
        "--",
        "—",
        "na",
        "n/a",
    }:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise _client.EIAProviderError("EIA_BAD_NUMBER")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _client.EIAProviderError("EIA_BAD_NUMBER") from None
    if not parsed.is_finite():
        raise _client.EIAProviderError("EIA_BAD_NUMBER")
    return int(parsed) if parsed == parsed.to_integral_value() else float(parsed)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _client.EIAProviderError("EIA_BAD_TEXT")
    normalized = value.strip()
    return normalized or None
