"""Source-native contracts for the Sina FZB financial statement response."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import (  # noqa: TC003 - public dataclass type hints stay introspectable.
    date,
    datetime,
)
from typing import Generic, Literal, TypeAlias, TypeVar

JsonScalar: TypeAlias = str | int | float | bool | None
"""A JSON scalar retained from the upstream response without semantic coercion."""

SinaFzbGroupNo: TypeAlias = Literal[1, 2, 3, 4, 5]
"""Source-local group number for a recognized FZB heading marker."""

SinaFzbGroupTitle: TypeAlias = Literal[
    "流动资产", "非流动资产", "流动负债", "非流动负债", "所有者权益"
]
"""Exact source title for a recognized FZB heading marker."""

SinaFzbValueState: TypeAlias = Literal["present", "empty", "null"]
"""Presence state of a raw financial item value; this does not imply validity."""

SinaFzbAdmissionErrorCode: TypeAlias = Literal["admission_required", "admission_denied"]
"""Stable admission failure codes for the explicitly governed fetch boundary."""

SinaFzbTransportErrorCode: TypeAlias = Literal[
    "transport_failed",
    "http_status",
    "response_too_large",
    "deadline_exceeded",
    "cancelled",
]
"""Stable error codes for bounded HTTP transport failures."""

SinaFzbWireErrorCode: TypeAlias = Literal[
    "invalid_query",
    "invalid_json",
    "invalid_envelope",
    "invalid_identity",
    "invalid_date",
    "invalid_value",
    "duplicate_conflict",
    "limit_exceeded",
]
"""Stable error codes for malformed or unsupported Sina wire data."""

SinaFzbErrorCode: TypeAlias = (
    SinaFzbAdmissionErrorCode | SinaFzbTransportErrorCode | SinaFzbWireErrorCode
)
"""Union of the source-specific stable error codes."""

_ErrorCode = TypeVar("_ErrorCode", bound=str)

SINA_SOURCE = "sina"
"""Stable source identity; this package remains unregistered by default."""

SINA_FZB_CANONICAL_MODEL = "financial_statement"
"""Canonical model name used to scope the future governed request."""

SINA_FZB_URL = (
    "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
)
"""Fixed upstream endpoint for the FZB balance-sheet response."""

SINA_FZB_HOST = "quotes.sina.cn"
"""Exact hostname allowed by a separately reviewed source grant."""

SINA_FZB_SOURCE_CODE = "fzb"
"""Fixed upstream source query parameter and source statement code."""

SINA_FZB_STATEMENT_CODE = SINA_FZB_SOURCE_CODE
"""Alias for the statement-code value emitted in parsed reports."""

SINA_FZB_QUERY_TYPE = "0"
"""Fixed upstream query type; callers cannot select another statement kind."""

SINA_FZB_PAGE = "1"
"""Fixed upstream page for the captured response contract."""

SINA_FZB_PAGE_SIZE = "1000"
"""Fixed upstream page size for the captured response contract."""

MAX_RAW_RESPONSE_BYTES = 4_194_304
"""Maximum accepted raw response size in bytes (4 MiB)."""

MAX_SINA_FZB_REPORTS = 256
"""Maximum report periods accepted in one response."""

MAX_SINA_FZB_ROWS_PER_REPORT = 512
"""Maximum source rows accepted for one report period."""

MAX_SINA_FZB_TOTAL_ROWS = 131_072
"""Maximum source rows accepted across all periods in one response."""

MAX_SINA_FZB_JSON_DEPTH = 32
"""Maximum structural JSON nesting depth accepted by the parser."""

SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS = 20.0
"""Maximum total duration for one bounded upstream request."""

SINA_FZB_HTTP_CONNECT_TIMEOUT_SECONDS = 3.0
"""Maximum connection-establishment duration for one request."""

SINA_FZB_HTTP_READ_TIMEOUT_SECONDS = 5.0
"""Maximum read inactivity duration for one request."""

SINA_FZB_GROUP_MARKERS: tuple[tuple[SinaFzbGroupNo, SinaFzbGroupTitle], ...] = (
    (1, "流动资产"),
    (2, "非流动资产"),
    (3, "流动负债"),
    (4, "非流动负债"),
    (5, "所有者权益"),
)
"""Exact source-native heading pairs evidenced by the captured response."""

SINA_FZB_GROUP_DISPLAY_TYPE = 1
"""Exact `item_display_type` required to recognize a heading marker."""

SINA_FZB_GROUP_DISPLAY = "大类"
"""Exact `item_display` required to recognize a heading marker."""

SINA_FZB_GROUP_ITEM_SOURCE = "fzb"
"""Exact `item_source` required to recognize a heading marker."""

SINA_FZB_GROUP_ITEM_PRECISION = "f2"
"""Exact `item_precision` required to recognize a heading marker."""

SINA_FZB_GROUP_ITEM_TONGBI = ""
"""Exact `item_tongbi` required to recognize a heading marker."""

_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ)")


@dataclass(frozen=True, slots=True)
class SinaFzbQuery:
    """Canonical instrument query for the Sina FZB response."""

    symbol: str

    def __post_init__(self) -> None:
        """Reject symbols outside the exact six-digit SH/SZ form.

        Raises:
            SinaFzbWireError: If the symbol is not a canonical string.
        """
        if type(self.symbol) is not str or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise SinaFzbWireError("invalid_query")


@dataclass(frozen=True, slots=True)
class SinaFzbItem:
    """Unnormalized source financial item with its raw source identity and value."""

    source_item_id: str
    source_title: str
    source_ordinals: tuple[int, ...]
    raw_value: str | None
    value_state: SinaFzbValueState
    source_metadata: tuple[tuple[str, JsonScalar], ...]


@dataclass(frozen=True, slots=True)
class SinaFzbGroupMarker:
    """Source-native structural heading, kept distinct from financial items."""

    source_ordinals: tuple[int, ...]
    source_group_no: SinaFzbGroupNo
    source_title: SinaFzbGroupTitle
    raw_item_field: Literal[""]
    raw_item_value: Literal[""]
    source_metadata: tuple[tuple[str, JsonScalar], ...]


SinaFzbEntry: TypeAlias = SinaFzbItem | SinaFzbGroupMarker
"""A source-order entry, either a financial row or structural heading."""


@dataclass(frozen=True, slots=True)
class SinaFzbReport:
    """Source-native balance-sheet report without inferred units or revisions."""

    symbol: str
    source_paper_code: str
    statement_code: Literal["fzb"]
    report_period: date
    period_metadata: tuple[tuple[str, JsonScalar], ...]
    published_on: date
    publication_precision: Literal["date"]
    source_update_epoch_seconds: int
    source_updated_at: datetime
    currency: str
    report_metadata: tuple[tuple[str, JsonScalar], ...]
    entries: tuple[SinaFzbEntry, ...]


@dataclass(frozen=True, slots=True)
class SinaFzbFetchedSnapshot:
    """Fetched source response with retrieval time kept separate from report time."""

    query: SinaFzbQuery
    retrieved_at_utc: datetime
    reports: tuple[SinaFzbReport, ...]


_ADMISSION_ERROR_MESSAGES: dict[SinaFzbAdmissionErrorCode, str] = {
    "admission_required": "Sina FZB request admission is required.",
    "admission_denied": "Sina FZB request admission was denied.",
}
_TRANSPORT_ERROR_MESSAGES: dict[SinaFzbTransportErrorCode, str] = {
    "transport_failed": "Sina FZB transport failed.",
    "http_status": "Sina FZB returned an unsuccessful HTTP status.",
    "response_too_large": "Sina FZB response exceeded the size limit.",
    "deadline_exceeded": "Sina FZB request deadline was exceeded.",
    "cancelled": "Sina FZB request was cancelled.",
}
_WIRE_ERROR_MESSAGES: dict[SinaFzbWireErrorCode, str] = {
    "invalid_query": "Sina FZB query is invalid.",
    "invalid_json": "Sina FZB response is not valid JSON.",
    "invalid_envelope": "Sina FZB response envelope is invalid.",
    "invalid_identity": "Sina FZB response identity is invalid.",
    "invalid_date": "Sina FZB response contains an invalid date.",
    "invalid_value": "Sina FZB response contains an invalid value.",
    "duplicate_conflict": "Sina FZB response contains conflicting duplicate rows.",
    "limit_exceeded": "Sina FZB response exceeded a parsing limit.",
}


class _SinaFzbError(Exception, Generic[_ErrorCode]):
    """Base for source-specific exceptions with sanitized public text."""

    def __init__(self, code: _ErrorCode, message: str) -> None:
        """Initialize the safe exception representation."""
        self._code = code
        super().__init__(message)

    @property
    def code(self) -> _ErrorCode:
        """Return the stable source-specific error code."""
        return self._code


class SinaFzbAdmissionError(_SinaFzbError[SinaFzbAdmissionErrorCode]):
    """Request could not be admitted under the required source grant."""

    def __init__(self, code: SinaFzbAdmissionErrorCode) -> None:
        """Create a sanitized admission error.

        Args:
            code: Stable source-specific admission failure code.

        Raises:
            ValueError: If an unsupported code is supplied at runtime.
        """
        message = _ADMISSION_ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina FZB admission error code.")
        super().__init__(code, message)


class SinaFzbTransportError(_SinaFzbError[SinaFzbTransportErrorCode]):
    """Bounded Sina HTTP transport failed or returned a non-success status."""

    def __init__(self, code: SinaFzbTransportErrorCode) -> None:
        """Create a sanitized transport error.

        Args:
            code: Stable source-specific transport failure code.

        Raises:
            ValueError: If an unsupported code is supplied at runtime.
        """
        message = _TRANSPORT_ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina FZB transport error code.")
        super().__init__(code, message)


class SinaFzbWireError(_SinaFzbError[SinaFzbWireErrorCode]):
    """Malformed or unsupported source-native Sina response data."""

    def __init__(self, code: SinaFzbWireErrorCode) -> None:
        """Create a sanitized wire validation error.

        Args:
            code: Stable source-specific wire failure code.

        Raises:
            ValueError: If an unsupported code is supplied at runtime.
        """
        message = _WIRE_ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina FZB wire error code.")
        super().__init__(code, message)


__all__ = [
    "JsonScalar",
    "MAX_RAW_RESPONSE_BYTES",
    "MAX_SINA_FZB_JSON_DEPTH",
    "MAX_SINA_FZB_REPORTS",
    "MAX_SINA_FZB_ROWS_PER_REPORT",
    "MAX_SINA_FZB_TOTAL_ROWS",
    "SINA_FZB_CANONICAL_MODEL",
    "SINA_FZB_GROUP_DISPLAY",
    "SINA_FZB_GROUP_DISPLAY_TYPE",
    "SINA_FZB_GROUP_ITEM_PRECISION",
    "SINA_FZB_GROUP_ITEM_SOURCE",
    "SINA_FZB_GROUP_ITEM_TONGBI",
    "SINA_FZB_GROUP_MARKERS",
    "SINA_FZB_HOST",
    "SINA_FZB_HTTP_CONNECT_TIMEOUT_SECONDS",
    "SINA_FZB_HTTP_OVERALL_TIMEOUT_SECONDS",
    "SINA_FZB_HTTP_READ_TIMEOUT_SECONDS",
    "SINA_FZB_PAGE",
    "SINA_FZB_PAGE_SIZE",
    "SINA_FZB_QUERY_TYPE",
    "SINA_FZB_SOURCE_CODE",
    "SINA_FZB_STATEMENT_CODE",
    "SINA_FZB_URL",
    "SINA_SOURCE",
    "SinaFzbAdmissionError",
    "SinaFzbAdmissionErrorCode",
    "SinaFzbEntry",
    "SinaFzbErrorCode",
    "SinaFzbFetchedSnapshot",
    "SinaFzbGroupMarker",
    "SinaFzbGroupNo",
    "SinaFzbGroupTitle",
    "SinaFzbItem",
    "SinaFzbQuery",
    "SinaFzbReport",
    "SinaFzbTransportError",
    "SinaFzbTransportErrorCode",
    "SinaFzbValueState",
    "SinaFzbWireError",
    "SinaFzbWireErrorCode",
]
