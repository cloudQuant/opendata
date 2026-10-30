"""Group bounded SZSE lifecycle workbooks as exchange-local source evidence.

This adapter consumes already captured XLSX bytes through the raw-only SZSE
parser. It keeps workbook categories and original cells separate, and fixes the
declared exchange scope to SZSE. It does not authenticate a source, interpret
lifecycle events, prove pagination, or establish current or historical market
coverage. Every completeness and lifecycle claim therefore remains UNKNOWN.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from itertools import islice
from typing import Literal

from opendata.pipeline.szse_lifecycle_xlsx_adapter import (
    _SZSE_OBSERVED_HEADERS,
    SZSE_LIFECYCLE_SYNTHETIC_HEADERS,
    SzseLifecycleDatasetClass,
    SzseLifecycleRawCell,
    SzseLifecycleRawRow,
    SzseLifecycleXlsxResult,
    parse_szse_lifecycle_xlsx,
)

__all__ = [
    "SzseLifecycleCensusCapture",
    "SzseLifecycleCensusError",
    "SzseLifecycleDatasetInput",
    "build_szse_lifecycle_census",
]

_DATASET_CLASSES = frozenset(("current_a", "suspended", "terminated"))
_MAX_DATASETS = 3
_MAX_CAPTURE_ROWS = 150_000
_MAX_CELL_CHARS = 4096
_MAX_SOURCE_ROW = 1_048_576
_MAX_SOURCE_COLUMN = 32
_CELL_REFERENCE = re.compile(r"^([A-Z]{1,2})([1-9][0-9]{0,6})$", re.ASCII)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SAFE_ERROR_CODES = frozenset(
    {
        "observed_at_invalid",
        "datasets_invalid",
        "dataset_count_invalid",
        "dataset_class_invalid",
        "dataset_class_duplicate",
        "dataset_input_invalid",
        "dataset_result_invalid",
        "capture_builder_required",
        "body_type_invalid",
        "capture_row_count_exceeded",
    }
)


class SzseLifecycleCensusError(ValueError):
    """A fixed-code refusal for malformed SZSE workbook-group inputs."""

    def __init__(self, code: str) -> None:
        """Create an error that contains no source values or paths."""
        self.code = code if type(code) is str and code in _SAFE_ERROR_CODES else "datasets_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, repr=False)
class SzseLifecycleDatasetInput:
    """One caller-declared workbook category and its already captured bytes."""

    dataset_class: SzseLifecycleDatasetClass
    raw_xlsx_body: bytes = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject unsupported labels and non-byte bodies before parsing."""
        if type(self.dataset_class) is not str or self.dataset_class not in _DATASET_CLASSES:
            raise SzseLifecycleCensusError("dataset_class_invalid")
        if type(self.raw_xlsx_body) is not bytes:
            raise SzseLifecycleCensusError("body_type_invalid")

    def __repr__(self) -> str:
        """Describe body size without exposing workbook content."""
        return (
            f"{type(self).__name__}(dataset_class={self.dataset_class!r}, "
            f"body_bytes={len(self.raw_xlsx_body)}, body_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False, init=False)
class SzseLifecycleCensusCapture:
    """Parser-shaped evidence candidate with unresolved market claims.

    Direct construction is disabled and nested result structure is validated.
    The private Python factory is not a security boundary, however: these
    checks cannot prove that a result actually came from the XLSX parser or
    authenticate its source. Use :func:`build_szse_lifecycle_census` for the
    parser-backed path; source identity and all market claims remain unknown.
    """

    observed_at: datetime
    datasets: tuple[SzseLifecycleXlsxResult, ...] = field(repr=False)
    source_id: Literal["szse.lifecycle.xlsx"] = field(default="szse.lifecycle.xlsx", init=False)
    exchange_scope: tuple[Literal["SZ"], ...] = field(default=("SZ",), init=False)
    source_identity_status: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)
    pagination_status: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)
    lifecycle_semantics_status: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)
    market_completeness_status: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)
    historical_coverage_status: Literal["UNKNOWN"] = field(default="UNKNOWN", init=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        """Reject direct construction outside the parser-backed builder."""
        raise SzseLifecycleCensusError("capture_builder_required")

    def __post_init__(self) -> None:
        """Validate capture bounds without promoting any source claims."""
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise SzseLifecycleCensusError("observed_at_invalid")
        try:
            if self.observed_at.utcoffset() is None:
                raise SzseLifecycleCensusError("observed_at_invalid")
        except Exception:
            raise SzseLifecycleCensusError("observed_at_invalid") from None
        if type(self.datasets) is not tuple or not 1 <= len(self.datasets) <= _MAX_DATASETS:
            raise SzseLifecycleCensusError("dataset_count_invalid")
        if any(type(dataset) is not SzseLifecycleXlsxResult for dataset in self.datasets):
            raise SzseLifecycleCensusError("dataset_result_invalid")
        if any(type(dataset.rows) is not tuple for dataset in self.datasets):
            raise SzseLifecycleCensusError("dataset_result_invalid")
        if sum(len(dataset.rows) for dataset in self.datasets) > _MAX_CAPTURE_ROWS:
            raise SzseLifecycleCensusError("capture_row_count_exceeded")
        if any(not _has_parser_result_shape(dataset) for dataset in self.datasets):
            raise SzseLifecycleCensusError("dataset_result_invalid")
        dataset_classes = tuple(dataset.dataset_class for dataset in self.datasets)
        if len(dataset_classes) != len(set(dataset_classes)):
            raise SzseLifecycleCensusError("dataset_class_duplicate")

    @property
    def dataset_classes(self) -> tuple[SzseLifecycleDatasetClass, ...]:
        """Return workbook categories actually supplied by the caller."""
        return tuple(dataset.dataset_class for dataset in self.datasets)

    @property
    def row_count(self) -> int:
        """Return parsed row count without implying a source-reported total."""
        return sum(len(dataset.rows) for dataset in self.datasets)

    def __repr__(self) -> str:
        """Render bounded counts and unknown states without source row values."""
        return (
            f"{type(self).__name__}(dataset_count={len(self.datasets)}, "
            f"row_count={self.row_count}, exchange_scope={self.exchange_scope!r}, "
            "market_completeness_status='UNKNOWN', values_redacted=True)"
        )


def build_szse_lifecycle_census(
    datasets: Iterable[SzseLifecycleDatasetInput], *, observed_at: datetime
) -> SzseLifecycleCensusCapture:
    """Parse up to three caller-captured SZSE workbook classes as raw evidence.

    The input labels select parser header contracts only. They do not prove
    which source produced the bytes or what any row means. Missing categories,
    workbook row counts, and matching codes are not treated as market gaps or
    lifecycle events.

    Args:
        datasets: One to three distinct captured workbook classes. A category
            may be omitted when its capture is unavailable; omitted data stays
            unknown and is not represented as an empty source result.
        observed_at: Caller-declared timezone-aware capture time.

    Returns:
        An immutable SZSE-scoped collection of parser results and raw rows.

    Raises:
        SzseLifecycleCensusError: If the input group is malformed or exceeds
            its row bound.
        The XLSX parser's fixed-code error: If a workbook fails its bounded
            package, XML, header, or row contract.
    """
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        raise SzseLifecycleCensusError("observed_at_invalid")
    try:
        if observed_at.utcoffset() is None:
            raise SzseLifecycleCensusError("observed_at_invalid")
    except Exception:
        raise SzseLifecycleCensusError("observed_at_invalid") from None
    if isinstance(datasets, (str, bytes, bytearray, Mapping)) or not isinstance(datasets, Iterable):
        raise SzseLifecycleCensusError("datasets_invalid")
    try:
        inputs = tuple(islice(iter(datasets), _MAX_DATASETS + 1))
    except Exception:
        raise SzseLifecycleCensusError("datasets_invalid") from None
    if not 1 <= len(inputs) <= _MAX_DATASETS:
        raise SzseLifecycleCensusError("dataset_count_invalid")
    if any(type(item) is not SzseLifecycleDatasetInput for item in inputs):
        raise SzseLifecycleCensusError("dataset_input_invalid")
    classes = tuple(item.dataset_class for item in inputs)
    if len(classes) != len(set(classes)):
        raise SzseLifecycleCensusError("dataset_class_duplicate")

    parsed = tuple(
        parse_szse_lifecycle_xlsx(item.raw_xlsx_body, dataset_class=item.dataset_class)
        for item in inputs
    )
    if sum(len(dataset.rows) for dataset in parsed) > _MAX_CAPTURE_ROWS:
        raise SzseLifecycleCensusError("capture_row_count_exceeded")
    return _create_parser_capture(observed_at=observed_at, datasets=parsed)


def _create_parser_capture(
    *, observed_at: datetime, datasets: tuple[SzseLifecycleXlsxResult, ...]
) -> SzseLifecycleCensusCapture:
    """Create a capture candidate after the public builder invokes the parser.

    This helper is private by convention only. Python privacy does not prove
    parser provenance; capture validation checks structure and safe metadata,
    while source identity remains explicitly unknown.
    """
    capture = object.__new__(SzseLifecycleCensusCapture)
    object.__setattr__(capture, "observed_at", observed_at)
    object.__setattr__(capture, "datasets", datasets)
    object.__setattr__(capture, "source_id", "szse.lifecycle.xlsx")
    object.__setattr__(capture, "exchange_scope", ("SZ",))
    object.__setattr__(capture, "source_identity_status", "UNKNOWN")
    object.__setattr__(capture, "pagination_status", "UNKNOWN")
    object.__setattr__(capture, "lifecycle_semantics_status", "UNKNOWN")
    object.__setattr__(capture, "market_completeness_status", "UNKNOWN")
    object.__setattr__(capture, "historical_coverage_status", "UNKNOWN")
    capture.__post_init__()
    return capture


def _has_parser_result_shape(dataset: SzseLifecycleXlsxResult) -> bool:
    """Check parser DTO structure and conservative metadata without provenance claims."""
    if (
        type(dataset.dataset_class) is not str
        or dataset.dataset_class not in _DATASET_CLASSES
        or type(dataset.dataset_class_binding_status) is not str
        or dataset.dataset_class_binding_status != "caller_declared"
        or dataset.source_id is not None
        or type(dataset.source_id_status) is not str
        or dataset.source_id_status != "unknown"
        or type(dataset.source_bytes_sha256) is not str
        or len(dataset.source_bytes_sha256) != 64
        or _SHA256_HEX.fullmatch(dataset.source_bytes_sha256) is None
        or type(dataset.date_system) is not str
        or dataset.date_system not in {"1900", "1904"}
        or type(dataset.lifecycle_semantics_status) is not str
        or dataset.lifecycle_semantics_status != "unknown"
        or type(dataset.market_scope_status) is not str
        or dataset.market_scope_status != "unknown"
        or type(dataset.source_completeness_status) is not str
        or dataset.source_completeness_status != "unknown"
        or type(dataset.header_contract_status) is not str
        or dataset.header_contract_status
        not in {"synthetic_contract_only", "selector_specific_observed_headers_only"}
        or type(dataset.headers) is not tuple
        or not dataset.headers
        or len(dataset.headers) > _MAX_SOURCE_COLUMN
        or any(
            type(header) is not str or len(header) > _MAX_CELL_CHARS for header in dataset.headers
        )
        or type(dataset.header_cells) is not tuple
        or len(dataset.header_cells) != len(dataset.headers)
        or type(dataset.rows) is not tuple
    ):
        return False

    if dataset.header_contract_status == "synthetic_contract_only":
        if dataset.headers != SZSE_LIFECYCLE_SYNTHETIC_HEADERS:
            return False
    elif dataset.headers != _SZSE_OBSERVED_HEADERS[dataset.dataset_class]:
        return False

    if not _valid_cells(dataset.header_cells):
        return False
    if tuple(cell.column_index for cell in dataset.header_cells) != tuple(
        range(1, len(dataset.headers) + 1)
    ):
        return False
    if tuple(cell.text_value for cell in dataset.header_cells) != dataset.headers:
        return False
    header_rows: set[int] = set()
    for cell in dataset.header_cells:
        match = _CELL_REFERENCE.fullmatch(cell.reference)
        if match is None:
            return False
        header_rows.add(int(match.group(2)))
    if len(header_rows) != 1:
        return False

    header_row_number = next(iter(header_rows))
    return all(
        type(row) is SzseLifecycleRawRow
        and type(row.source_row_number) is int
        and header_row_number < row.source_row_number <= _MAX_SOURCE_ROW
        and type(row.cells) is tuple
        and _valid_cells(row.cells, expected_row_number=row.source_row_number)
        and all(cell.column_index <= len(dataset.headers) for cell in row.cells)
        for row in dataset.rows
    )


def _valid_cells(
    cells: tuple[SzseLifecycleRawCell, ...], *, expected_row_number: int | None = None
) -> bool:
    """Check immutable cell shape, bounded values, and coordinate consistency."""
    if type(cells) is not tuple:
        return False
    previous_column = 0
    for cell in cells:
        if type(cell) is not SzseLifecycleRawCell:
            return False
        if (
            type(cell.reference) is not str
            or len(cell.reference) > 9
            or type(cell.column_index) is not int
            or not 1 <= cell.column_index <= _MAX_SOURCE_COLUMN
            or type(cell.cell_type) is not str
            or cell.cell_type not in {"n", "s", "inlineStr", "str", "b", "d"}
            or type(cell.raw_value) is not str
            or len(cell.raw_value) > _MAX_CELL_CHARS
            or type(cell.text_value) is not str
            or len(cell.text_value) > _MAX_CELL_CHARS
            or (
                cell.style_index is not None
                and (type(cell.style_index) is not int or not 0 <= cell.style_index <= 10_000)
            )
        ):
            return False
        match = _CELL_REFERENCE.fullmatch(cell.reference)
        if match is None:
            return False
        column_number = _column_number(match.group(1))
        row_number = int(match.group(2))
        if (
            column_number != cell.column_index
            or column_number <= previous_column
            or row_number > _MAX_SOURCE_ROW
            or (expected_row_number is not None and row_number != expected_row_number)
        ):
            return False
        previous_column = column_number
    return True


def _column_number(letters: str) -> int:
    """Convert an uppercase Excel column label to its one-based index."""
    number = 0
    for character in letters:
        number = number * 26 + ord(character) - ord("A") + 1
    return number
