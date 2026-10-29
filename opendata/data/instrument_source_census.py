"""Keep raw security-source observations and assess pagination evidence offline.

The values in this module are caller-supplied observations. A market, security
type, lifecycle status, or event date is never inferred from a symbol or from
another event. A ``CHAIN_CONSISTENT`` pagination assessment means only that the
supplied pages form a self-consistent chain whose caller-declared total matches
the rows or pages; it does not authenticate the source or establish market
scope, security-class coverage, historical coverage, or universe completeness.

No database, API, filesystem, environment, or network access is performed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import TypeAlias

__all__ = [
    "FieldObservationState",
    "InstrumentSourceCensus",
    "InstrumentSourceCensusError",
    "InstrumentSourceObservation",
    "InstrumentSourcePage",
    "PageAttemptStatus",
    "PageReceipt",
    "PaginationAssessment",
    "PaginationChainStatus",
    "PaginationReason",
    "ReportedTotalBasis",
    "SourceFieldObservation",
    "assess_source_pagination",
]

_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_SOURCE_ID_CHARS = 64
_MAX_FIELD_NAME_CHARS = 128
_MAX_RAW_FIELDS = 256
_MAX_CONTAINER_ITEMS = 4_096
_MAX_RAW_NODES = 20_000
_MAX_RAW_TEXT_CHARS = 16_384
_MAX_RAW_BYTES = 1_048_576
_MAX_FIELD_VALUE_BYTES = 262_144
_MAX_OBSERVATION_BYTES = 3_145_728
_MAX_VALUE_DEPTH = 12
_MAX_PAGES = 4_096
_MAX_ROWS = 100_000
_MAX_CENSUS_NODES = 2_000_000
_MAX_CENSUS_BYTES = 67_108_864
_MAX_CURSOR_CHARS = 512
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

FrozenJsonValue: TypeAlias = (
    bool
    | int
    | float
    | str
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
    | None
)


class InstrumentSourceCensusError(ValueError):
    """Raised when a source observation or its bounded evidence is malformed."""


class FieldObservationState(str, Enum):
    """Describe exactly what the caller mapped from one source field."""

    UNMAPPED = "unmapped"
    ABSENT = "absent"
    NULL = "null"
    VALUE = "value"


class PaginationChainStatus(str, Enum):
    """State whether supplied caller-declared evidence forms one page chain."""

    CHAIN_CONSISTENT = "chain_consistent"
    UNKNOWN = "unknown"


class PageAttemptStatus(str, Enum):
    """Retain whether a caller recorded a successful, failed, or unknown attempt."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ReportedTotalBasis(str, Enum):
    """Caller-declared meaning of a source-reported total."""

    ROW_COUNT = "caller_declared_row_count"
    PAGE_COUNT = "caller_declared_page_count"
    UNKNOWN = "unknown"


class PaginationReason(str, Enum):
    """Stable reason codes explaining why pagination remains unknown."""

    NO_PAGES = "no_pages"
    PAGE_SEQUENCE_GAP = "page_sequence_gap"
    PAGE_ATTEMPT_FAILED = "page_attempt_failed"
    PAGE_ATTEMPT_UNKNOWN = "page_attempt_unknown"
    PAGE_RECEIPT_MISSING = "page_receipt_missing"
    INITIAL_CURSOR_NOT_EMPTY = "initial_cursor_not_empty"
    CURSOR_CHAIN_BROKEN = "cursor_chain_broken"
    CURSOR_REPEATED = "cursor_repeated"
    CURSOR_EMPTY = "cursor_empty"
    NONFINAL_PAGE_NOT_CONTINUABLE = "nonfinal_page_not_continuable"
    FINAL_PAGE_NOT_TERMINAL = "final_page_not_terminal"
    FINAL_PAGE_HAS_NEXT_CURSOR = "final_page_has_next_cursor"
    REPORTED_TOTAL_MISSING = "reported_total_missing"
    REPORTED_TOTAL_CONFLICT = "reported_total_conflict"
    REPORTED_TOTAL_BASIS_UNKNOWN = "reported_total_basis_unknown"
    REPORTED_TOTAL_BASIS_CONFLICT = "reported_total_basis_conflict"
    ROW_COUNT_MISMATCH = "row_count_mismatch"
    PAGE_COUNT_MISMATCH = "page_count_mismatch"
    EMPTY_PAGE = "empty_page"
    DUPLICATE_SOURCE_ROW = "duplicate_source_row"


def _bounded_text(value: object, name: str, limit: int) -> str:
    """Validate nonempty printable text without trimming source input."""
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or any(not character.isprintable() for character in value)
    ):
        raise InstrumentSourceCensusError(
            f"{name} must be nonempty printable text up to {limit} chars"
        )
    return value


def _validate_optional_sha256(value: object, name: str) -> None:
    """Validate a caller-declared lowercase SHA-256 digest, if provided."""
    if value is not None and (type(value) is not str or _SHA256_PATTERN.fullmatch(value) is None):
        raise InstrumentSourceCensusError(f"{name} must be a lowercase SHA-256 hex digest or None")


def _freeze_json(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
    byte_budget: list[int] | None = None,
    byte_limit: int = _MAX_RAW_BYTES,
) -> FrozenJsonValue:
    """Copy a finite JSON-shaped value into recursively immutable containers."""
    if budget is None:
        budget = [0]
    if byte_budget is None:
        byte_budget = [0]
    budget[0] += 1
    if budget[0] > _MAX_RAW_NODES:
        raise InstrumentSourceCensusError("raw source value exceeds the node limit")
    if depth > _MAX_VALUE_DEPTH:
        raise InstrumentSourceCensusError("raw source value exceeds the nesting limit")
    if value is None:
        return value
    if type(value) is bool:
        return bool(value)
    if type(value) is int:
        if value.bit_length() > 4_096:
            raise InstrumentSourceCensusError("raw source integer exceeds the numeric limit")
        return int(value)
    if type(value) is str:
        if len(value) > _MAX_RAW_TEXT_CHARS:
            raise InstrumentSourceCensusError("raw source text exceeds the character limit")
        byte_budget[0] += len(value.encode("utf-8"))
        if byte_budget[0] > byte_limit:
            raise InstrumentSourceCensusError("raw source value exceeds the byte limit")
        return str(value)
    if type(value) is float:
        if not math.isfinite(value):
            raise InstrumentSourceCensusError("raw source numbers must be finite")
        return float(value)
    if isinstance(value, Mapping):
        if len(value) > _MAX_RAW_FIELDS:
            raise InstrumentSourceCensusError("raw source object has too many fields")
        frozen: dict[str, FrozenJsonValue] = {}
        for key, item in value.items():
            field_name = _bounded_text(key, "raw source field name", _MAX_FIELD_NAME_CHARS)
            byte_budget[0] += len(field_name.encode("utf-8"))
            if byte_budget[0] > byte_limit:
                raise InstrumentSourceCensusError("raw source value exceeds the byte limit")
            frozen[field_name] = _freeze_json(
                item,
                depth=depth + 1,
                budget=budget,
                byte_budget=byte_budget,
                byte_limit=byte_limit,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_CONTAINER_ITEMS:
            raise InstrumentSourceCensusError("raw source array has too many items")
        return tuple(
            _freeze_json(
                item,
                depth=depth + 1,
                budget=budget,
                byte_budget=byte_budget,
                byte_limit=byte_limit,
            )
            for item in value
        )
    raise InstrumentSourceCensusError("raw source values must be finite JSON-compatible values")


def _same_json_value(left: object, right: object) -> bool:
    """Compare frozen source values with strict JSON type semantics."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        if left.keys() != right.keys():
            return False
        return all(_same_json_value(left[key], right[key]) for key in left)
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _same_json_value(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _count_json_nodes(value: object) -> int:
    """Count a frozen source value's tree nodes for the census-wide budget."""
    if isinstance(value, Mapping):
        return 1 + sum(_count_json_nodes(item) for item in value.values())
    if isinstance(value, tuple):
        return 1 + sum(_count_json_nodes(item) for item in value)
    return 1


def _count_json_bytes(value: object) -> int:
    """Count UTF-8 bytes in frozen string values and object keys."""
    if isinstance(value, Mapping):
        return sum(
            len(key.encode("utf-8")) + _count_json_bytes(item) for key, item in value.items()
        )
    if isinstance(value, tuple):
        return sum(_count_json_bytes(item) for item in value)
    if type(value) is str:
        return len(value.encode("utf-8"))
    return 0


def _observation_resource_usage(row: InstrumentSourceObservation) -> tuple[int, int]:
    """Count raw-row and copied field-fact nodes/bytes for a bounded census."""
    fields = (
        row.market,
        row.security_type,
        row.status,
        row.listing_date,
        row.last_trading_date,
        row.termination_date,
    )
    node_count = _count_json_nodes(row.raw_row)
    byte_count = _count_json_bytes(row.raw_row)
    for observation in fields:
        if observation.source_name is not None:
            byte_count += len(observation.source_name.encode("utf-8"))
        state = FieldObservationState(observation.state)
        if state in (FieldObservationState.NULL, FieldObservationState.VALUE):
            node_count += _count_json_nodes(observation.raw_value)
        if state is FieldObservationState.VALUE:
            byte_count += _count_json_bytes(observation.raw_value)
    return node_count, byte_count


def _canonical_json_key(value: object) -> tuple[object, ...]:
    """Create a strict, hashable key for one frozen JSON-compatible value."""
    if value is None:
        return ("null",)
    if type(value) is bool:
        return ("bool", value)
    if type(value) is int:
        return ("int", value)
    if type(value) is float:
        return ("float", value.hex())
    if type(value) is str:
        return ("string", value)
    if isinstance(value, tuple):
        return ("array", tuple(_canonical_json_key(item) for item in value))
    if isinstance(value, Mapping):
        return (
            "object",
            tuple((key, _canonical_json_key(value[key])) for key in sorted(value)),
        )
    raise InstrumentSourceCensusError("raw source values must be frozen JSON-compatible values")


@dataclass(frozen=True, slots=True)
class SourceFieldObservation:
    """Retain one named source field and whether it was absent, null, or valued.

    ``UNMAPPED`` is deliberately distinct from ``ABSENT``: the former means no
    source-field mapping was supplied, while the latter means a named field
    was checked and did not occur in the raw row. Values are frozen copies.
    """

    state: FieldObservationState | str = FieldObservationState.UNMAPPED
    source_name: str | None = None
    raw_value: object = None

    def __post_init__(self) -> None:
        """Normalize state and reject contradictory field-level declarations."""
        try:
            state = FieldObservationState(self.state)
        except (TypeError, ValueError):
            raise InstrumentSourceCensusError("invalid source field observation state") from None
        object.__setattr__(self, "state", state)
        if state is FieldObservationState.UNMAPPED:
            if self.source_name is not None or self.raw_value is not None:
                raise InstrumentSourceCensusError("unmapped fields cannot carry a name or value")
            return
        _bounded_text(self.source_name, "source_name", _MAX_FIELD_NAME_CHARS)
        if state in (FieldObservationState.ABSENT, FieldObservationState.NULL):
            if self.raw_value is not None:
                raise InstrumentSourceCensusError("absent or null fields cannot carry a raw value")
            return
        if self.raw_value is None:
            raise InstrumentSourceCensusError("value fields must carry a non-null raw value")
        object.__setattr__(
            self,
            "raw_value",
            _freeze_json(self.raw_value, byte_limit=_MAX_FIELD_VALUE_BYTES),
        )


@dataclass(frozen=True, slots=True)
class InstrumentSourceObservation:
    """One source row with unmapped identity and separate lifecycle facts.

    The original row is recursively copied and frozen. The six field
    observations retain caller-declared source labels and raw values, without
    translating a market, stock class, status, or date into a canonical fact.
    """

    raw_row: Mapping[str, object]
    market: SourceFieldObservation = field(default_factory=SourceFieldObservation)
    security_type: SourceFieldObservation = field(default_factory=SourceFieldObservation)
    status: SourceFieldObservation = field(default_factory=SourceFieldObservation)
    listing_date: SourceFieldObservation = field(default_factory=SourceFieldObservation)
    last_trading_date: SourceFieldObservation = field(default_factory=SourceFieldObservation)
    termination_date: SourceFieldObservation = field(default_factory=SourceFieldObservation)

    def __post_init__(self) -> None:
        """Freeze the source row and verify that each mapped fact matches it."""
        if not isinstance(self.raw_row, Mapping):
            raise InstrumentSourceCensusError("raw_row must be a mapping")
        frozen = _freeze_json(self.raw_row)
        if not isinstance(frozen, Mapping):
            raise InstrumentSourceCensusError("raw_row must be a mapping")
        if not frozen:
            raise InstrumentSourceCensusError("raw_row must retain at least one source field")
        object.__setattr__(self, "raw_row", frozen)
        for name in (
            "market",
            "security_type",
            "status",
            "listing_date",
            "last_trading_date",
            "termination_date",
        ):
            observation = getattr(self, name)
            if not isinstance(observation, SourceFieldObservation):
                raise InstrumentSourceCensusError(f"{name} must be a SourceFieldObservation")
            _validate_field_against_row(observation, frozen, name)
        event_fields = (
            self.listing_date,
            self.last_trading_date,
            self.termination_date,
        )
        event_source_names = [
            observation.source_name
            for observation in event_fields
            if observation.source_name is not None
            and observation.state in (FieldObservationState.NULL, FieldObservationState.VALUE)
        ]
        if len(event_source_names) != len(set(event_source_names)):
            raise InstrumentSourceCensusError(
                "listing, last trading, and termination dates require separate source fields"
            )
        _, row_bytes = _observation_resource_usage(self)
        if row_bytes > _MAX_OBSERVATION_BYTES:
            raise InstrumentSourceCensusError(
                f"source observation exceeds the {_MAX_OBSERVATION_BYTES:,}-byte limit"
            )


def _validate_field_against_row(
    observation: SourceFieldObservation,
    raw_row: Mapping[str, FrozenJsonValue],
    label: str,
) -> None:
    """Ensure a mapped fact is directly present in the retained raw row."""
    state = FieldObservationState(observation.state)
    if state is FieldObservationState.UNMAPPED:
        return
    source_name = observation.source_name
    if source_name is None:
        raise InstrumentSourceCensusError(f"{label} mapping requires a source field name")
    present = source_name in raw_row
    if state is FieldObservationState.ABSENT:
        if present:
            raise InstrumentSourceCensusError(f"{label} is marked absent but exists in raw_row")
        return
    if not present:
        raise InstrumentSourceCensusError(f"{label} field does not exist in raw_row")
    source_value = raw_row[source_name]
    if state is FieldObservationState.NULL:
        if source_value is not None:
            raise InstrumentSourceCensusError(f"{label} is marked null but has a raw value")
        return
    if source_value is None or not _same_json_value(source_value, observation.raw_value):
        raise InstrumentSourceCensusError(f"{label} raw value does not match raw_row")


@dataclass(frozen=True, slots=True)
class PageReceipt:
    """Caller-declared hashes for the exact request and captured response.

    The hashes are retained as audit labels only. This offline DTO cannot read
    the corresponding bytes, verify the hashes, or authenticate who supplied
    them.
    """

    request_sha256: str | None
    response_sha256: str | None

    def __post_init__(self) -> None:
        """Validate digest syntax without claiming that the digest is genuine."""
        _validate_optional_sha256(self.request_sha256, "request_sha256")
        _validate_optional_sha256(self.response_sha256, "response_sha256")


@dataclass(frozen=True, slots=True)
class InstrumentSourcePage:
    """One page attempt plus caller-recorded request, receipt, and total evidence.

    Cursors are opaque source tokens. They are compared for exact continuity;
    their meaning, ordering, source authenticity, and server-side stability are
    not verified by this DTO. ``sequence_number`` is the local capture order;
    ``source_page_number`` preserves a source-native page label without
    converting or interpreting it (including zero-based labels). The source
    page label is not used to check source-side page-number continuity.
    """

    sequence_number: int
    source_page_number: int | str | None
    request_cursor: str | None
    next_cursor: str | None
    terminal: bool | None
    reported_total: int | None
    rows: tuple[InstrumentSourceObservation, ...]
    reported_total_basis: ReportedTotalBasis | str | None = None
    attempt_status: PageAttemptStatus | str = PageAttemptStatus.UNKNOWN
    failure_code: str | None = None
    receipt: PageReceipt | None = None

    def __post_init__(self) -> None:
        """Validate page bounds and freeze rows without repairing evidence."""
        if type(self.sequence_number) is not int or not 1 <= self.sequence_number <= _MAX_PAGES:
            raise InstrumentSourceCensusError(
                f"sequence_number must be from 1 through {_MAX_PAGES}"
            )
        source_page_number = self.source_page_number
        if source_page_number is not None and type(source_page_number) not in (int, str):
            raise InstrumentSourceCensusError(
                "source_page_number must preserve an integer or string"
            )
        if type(source_page_number) is int and source_page_number.bit_length() > 63:
            raise InstrumentSourceCensusError("source_page_number exceeds the integer limit")
        if type(source_page_number) is str and len(source_page_number) > _MAX_FIELD_NAME_CHARS:
            raise InstrumentSourceCensusError("source_page_number exceeds the text limit")
        for name in ("request_cursor", "next_cursor"):
            cursor = getattr(self, name)
            if cursor is not None and (type(cursor) is not str or len(cursor) > _MAX_CURSOR_CHARS):
                raise InstrumentSourceCensusError(f"{name} must be a bounded string or None")
        if self.terminal is not None and type(self.terminal) is not bool:
            raise InstrumentSourceCensusError("terminal must be bool or None")
        if self.reported_total is not None and (
            type(self.reported_total) is not int or self.reported_total < 0
        ):
            raise InstrumentSourceCensusError(
                "reported_total must be a nonnegative integer or None"
            )
        try:
            basis = (
                None
                if self.reported_total_basis is None
                else ReportedTotalBasis(self.reported_total_basis)
            )
        except (TypeError, ValueError):
            raise InstrumentSourceCensusError("invalid reported_total_basis") from None
        if (self.reported_total is None) != (basis is None):
            raise InstrumentSourceCensusError(
                "reported_total and reported_total_basis must be supplied together"
            )
        object.__setattr__(self, "reported_total_basis", basis)
        try:
            attempt_status = PageAttemptStatus(self.attempt_status)
        except (TypeError, ValueError):
            raise InstrumentSourceCensusError("invalid page attempt status") from None
        object.__setattr__(self, "attempt_status", attempt_status)
        if self.failure_code is not None:
            _bounded_text(self.failure_code, "failure_code", _MAX_FIELD_NAME_CHARS)
        if self.receipt is not None and not isinstance(self.receipt, PageReceipt):
            raise InstrumentSourceCensusError("receipt must be a PageReceipt or None")
        rows = _bounded_values(self.rows, "rows", _MAX_ROWS)
        if any(not isinstance(row, InstrumentSourceObservation) for row in rows):
            raise InstrumentSourceCensusError(
                "rows must contain InstrumentSourceObservation values"
            )
        object.__setattr__(self, "rows", tuple(rows))
        if attempt_status is PageAttemptStatus.SUCCEEDED and self.failure_code is not None:
            raise InstrumentSourceCensusError("successful page attempts cannot carry failure_code")
        if attempt_status is PageAttemptStatus.FAILED and self.failure_code is None:
            raise InstrumentSourceCensusError("failed page attempts require failure_code")
        if attempt_status is PageAttemptStatus.UNKNOWN and self.failure_code is not None:
            raise InstrumentSourceCensusError("unknown page attempts cannot carry failure_code")
        if attempt_status is not PageAttemptStatus.SUCCEEDED and (
            self.next_cursor is not None
            or self.terminal is not None
            or self.reported_total is not None
            or rows
        ):
            raise InstrumentSourceCensusError(
                "unsuccessful page attempts cannot declare response paging fields or rows"
            )


@dataclass(frozen=True, slots=True)
class InstrumentSourceCensus:
    """A bounded caller-captured sequence of source pages.

    This wrapper deliberately has no market-scope assertion. Even a complete
    ``CHAIN_CONSISTENT`` page evidence proves only agreement among the supplied
    records and does not prove all exchanges, security types, or historical
    instruments were requested or returned.
    """

    source_id: str
    observed_at: datetime
    pages: tuple[InstrumentSourcePage, ...]

    def __post_init__(self) -> None:
        """Validate source metadata and freeze the supplied page sequence."""
        if (
            type(self.source_id) is not str
            or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None
            or len(self.source_id) > _MAX_SOURCE_ID_CHARS
        ):
            raise InstrumentSourceCensusError(
                "source_id must be a lowercase stable identifier up to 64 characters"
            )
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InstrumentSourceCensusError("observed_at must be a timezone-aware datetime")
        if self.observed_at.utcoffset() is None:
            raise InstrumentSourceCensusError("observed_at must be a timezone-aware datetime")
        pages = _bounded_values(self.pages, "pages", _MAX_PAGES)
        row_count = 0
        node_count = 0
        byte_count = 0
        for page in pages:
            if not isinstance(page, InstrumentSourcePage):
                raise InstrumentSourceCensusError("pages must contain InstrumentSourcePage values")
            for row in page.rows:
                row_count += 1
                if row_count > _MAX_ROWS:
                    raise InstrumentSourceCensusError(f"census exceeds the {_MAX_ROWS:,}-row limit")
                row_nodes, row_bytes = _observation_resource_usage(row)
                node_count += row_nodes
                if node_count > _MAX_CENSUS_NODES:
                    raise InstrumentSourceCensusError(
                        f"census exceeds the {_MAX_CENSUS_NODES:,}-source-node limit"
                    )
                byte_count += row_bytes
                if byte_count > _MAX_CENSUS_BYTES:
                    raise InstrumentSourceCensusError(
                        f"census exceeds the {_MAX_CENSUS_BYTES:,}-UTF-8-byte limit"
                    )
        object.__setattr__(self, "pages", tuple(pages))


@dataclass(frozen=True, slots=True)
class PaginationAssessment:
    """Deterministic result for supplied paging evidence, never market scope."""

    chain_status: PaginationChainStatus
    page_count: int
    row_count: int
    reasons: tuple[PaginationReason, ...]


def assess_source_pagination(census: InstrumentSourceCensus) -> PaginationAssessment:
    """Assess the supplied caller-declared page chain and its receipts.

    ``CHAIN_CONSISTENT`` requires local sequence numbers 1..N, successful page
    attempts with request and response hashes, an empty initial cursor, every
    nonfinal page to provide the exact next-page cursor and a nonterminal
    signal, a terminal final page without a next cursor, a repeated total with
    one caller-declared and known basis matching the observed rows or pages,
    nonempty successful responses, and no exact raw-row duplicates. This is
    self-consistency in caller capture order: source-native page labels are
    retained but their continuity is not checked. Hashes, totals, cursor
    meaning, and source identity are not independently verified; receipt
    hashes do not authenticate the source or prove which bytes were hashed.
    Missing or contradictory evidence returns UNKNOWN with stable reasons.

    This status does not claim that the external source is authentic, that its
    advertised total is truthful, or that a market, security class, current
    universe, or historical lifecycle range is complete.

    Args:
        census: Immutable, caller-captured source pages.

    Returns:
        Page and row counts plus CHAIN_CONSISTENT or UNKNOWN and reason codes.

    Raises:
        InstrumentSourceCensusError: If ``census`` is not a validated census.
    """
    if not isinstance(census, InstrumentSourceCensus):
        raise InstrumentSourceCensusError("census must be an InstrumentSourceCensus")
    pages = census.pages
    rows = tuple(row for page in pages for row in page.rows)
    reasons: list[PaginationReason] = []

    def add(reason: PaginationReason) -> None:
        """Append each deterministic diagnostic once."""
        if reason not in reasons:
            reasons.append(reason)

    if not pages:
        add(PaginationReason.NO_PAGES)
        return PaginationAssessment(PaginationChainStatus.UNKNOWN, 0, 0, tuple(reasons))

    if tuple(page.sequence_number for page in pages) != tuple(range(1, len(pages) + 1)):
        add(PaginationReason.PAGE_SEQUENCE_GAP)
    if pages[0].request_cursor is not None:
        add(PaginationReason.INITIAL_CURSOR_NOT_EMPTY)

    seen_request_cursors: set[str] = set()
    for index, page in enumerate(pages):
        attempt_status = PageAttemptStatus(page.attempt_status)
        if attempt_status is PageAttemptStatus.FAILED:
            add(PaginationReason.PAGE_ATTEMPT_FAILED)
        elif attempt_status is PageAttemptStatus.UNKNOWN:
            add(PaginationReason.PAGE_ATTEMPT_UNKNOWN)
        elif (
            page.receipt is None
            or page.receipt.request_sha256 is None
            or page.receipt.response_sha256 is None
        ):
            add(PaginationReason.PAGE_RECEIPT_MISSING)
        if page.request_cursor is not None:
            if not page.request_cursor:
                add(PaginationReason.CURSOR_EMPTY)
            if page.request_cursor in seen_request_cursors:
                add(PaginationReason.CURSOR_REPEATED)
            seen_request_cursors.add(page.request_cursor)
        if index < len(pages) - 1:
            following = pages[index + 1]
            if page.terminal is not False or page.next_cursor is None:
                add(PaginationReason.NONFINAL_PAGE_NOT_CONTINUABLE)
            if page.next_cursor == "":
                add(PaginationReason.CURSOR_EMPTY)
            if page.next_cursor is not None and page.next_cursor in seen_request_cursors:
                add(PaginationReason.CURSOR_REPEATED)
            if following.request_cursor != page.next_cursor or page.next_cursor is None:
                add(PaginationReason.CURSOR_CHAIN_BROKEN)
        else:
            if page.terminal is not True:
                add(PaginationReason.FINAL_PAGE_NOT_TERMINAL)
            if page.next_cursor is not None:
                add(PaginationReason.FINAL_PAGE_HAS_NEXT_CURSOR)

    declared_totals = [page.reported_total for page in pages]
    if any(total is None for total in declared_totals):
        add(PaginationReason.REPORTED_TOTAL_MISSING)
    present_totals = {total for total in declared_totals if total is not None}
    if len(present_totals) > 1:
        add(PaginationReason.REPORTED_TOTAL_CONFLICT)
    declared_bases = {
        ReportedTotalBasis(page.reported_total_basis)
        for page in pages
        if page.reported_total is not None
    }
    if len(declared_bases) > 1:
        add(PaginationReason.REPORTED_TOTAL_BASIS_CONFLICT)
    if ReportedTotalBasis.UNKNOWN in declared_bases:
        add(PaginationReason.REPORTED_TOTAL_BASIS_UNKNOWN)
    if len(present_totals) == 1 and len(declared_bases) == 1:
        total = next(iter(present_totals))
        basis = next(iter(declared_bases))
        if basis is ReportedTotalBasis.ROW_COUNT and total != len(rows):
            add(PaginationReason.ROW_COUNT_MISMATCH)
        elif basis is ReportedTotalBasis.PAGE_COUNT and total != len(pages):
            add(PaginationReason.PAGE_COUNT_MISMATCH)
    if any(
        not page.rows and PageAttemptStatus(page.attempt_status) is PageAttemptStatus.SUCCEEDED
        for page in pages
    ):
        add(PaginationReason.EMPTY_PAGE)
    if not rows and all(
        PageAttemptStatus(page.attempt_status) is PageAttemptStatus.SUCCEEDED for page in pages
    ):
        add(PaginationReason.EMPTY_PAGE)
    if _has_duplicate_source_rows(rows):
        add(PaginationReason.DUPLICATE_SOURCE_ROW)

    chain_status = (
        PaginationChainStatus.CHAIN_CONSISTENT if not reasons else PaginationChainStatus.UNKNOWN
    )
    return PaginationAssessment(chain_status, len(pages), len(rows), tuple(reasons))


def _has_duplicate_source_rows(rows: tuple[InstrumentSourceObservation, ...]) -> bool:
    """Detect byte-semantics-equivalent JSON rows without deriving identity."""
    unique_rows: set[tuple[object, ...]] = set()
    for row in rows:
        key = _canonical_json_key(row.raw_row)
        if key in unique_rows:
            return True
        unique_rows.add(key)
    return False


def _bounded_values(values: Iterable[object], name: str, limit: int) -> tuple[object, ...]:
    """Materialize a finite iterable once and reject scalars or oversize input."""
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable):
        raise InstrumentSourceCensusError(f"{name} must be a bounded iterable")
    try:
        items = tuple(islice(iter(values), limit + 1))
    except TypeError:
        raise InstrumentSourceCensusError(f"{name} must be a bounded iterable") from None
    if len(items) > limit:
        raise InstrumentSourceCensusError(f"{name} exceeds the {limit:,}-item limit")
    return items
