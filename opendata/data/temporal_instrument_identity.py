"""Resolve reviewed, time-scoped China equity identifiers without I/O.

The registry is an immutable value supplied by its caller. It validates the
shape and internal consistency of that supplied evidence; it does not verify
the source, authenticate the evidence, establish a complete universe, or infer
listing and delisting events. Unmapped dates remain explicitly unknown.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from types import MappingProxyType
from typing import TypeVar

__all__ = [
    "Exchange",
    "IdentityResolution",
    "IdentityResolutionSegment",
    "IdentityResolutionStatus",
    "MarketSymbolPair",
    "ResolutionSubject",
    "TemporalIdentityAssignment",
    "TemporalIdentityRegistry",
    "TemporalInstrumentIdentityError",
]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SIX_ASCII_DIGITS = re.compile(r"^[0-9]{6}$")
_MAX_EVIDENCE_CHARS = 512
_MAX_ASSIGNMENTS = 100_000
_MAX_WINDOW_DAYS = 3_660
_RowT = TypeVar("_RowT")


class TemporalInstrumentIdentityError(ValueError):
    """Raised when an identity map is invalid, ambiguous, or out of bounds."""


class Exchange(str, Enum):
    """Supported exchange namespaces for explicit China equity pairs."""

    XSHG = "XSHG"
    XSHE = "XSHE"
    XBSE = "XBSE"


class IdentityResolutionStatus(str, Enum):
    """Whether reviewed assignment evidence covers a segment of a request."""

    KNOWN = "known"
    UNKNOWN = "unknown"


class ResolutionSubject(str, Enum):
    """Kind of the subject used for a bounded identity lookup."""

    PAIR = "pair"
    INSTRUMENT = "instrument"


def _identifier(value: str, name: str) -> str:
    """Validate an opaque stable identifier without changing its spelling."""
    if type(value) is not str or _IDENTIFIER.fullmatch(value) is None:
        raise TemporalInstrumentIdentityError(f"{name} must be a 1-128 character stable identifier")
    return value


def _evidence_ref(value: str, name: str) -> str:
    """Validate a bounded, printable pointer to separately held evidence."""
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > _MAX_EVIDENCE_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        raise TemporalInstrumentIdentityError(
            f"{name} must be a bounded nonempty evidence reference"
        )
    return value


def _strict_date(value: date, name: str) -> date:
    """Accept calendar dates and reject datetime values or coercion."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TemporalInstrumentIdentityError(f"{name} must be a datetime.date")
    return value


def _bounded_values(
    values: Iterable[object], name: str, expected_type: type[_RowT]
) -> tuple[_RowT, ...]:
    """Validate rows before retaining them and reject oversized iterables."""
    if isinstance(values, (str, bytes, bytearray, dict)) or not isinstance(values, Iterable):
        raise TemporalInstrumentIdentityError(f"{name} must be a bounded iterable")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise TemporalInstrumentIdentityError(f"{name} must be a bounded iterable") from exc
    rows: list[_RowT] = []
    for row in iterator:
        if len(rows) >= _MAX_ASSIGNMENTS:
            raise TemporalInstrumentIdentityError(
                f"{name} exceeds the {_MAX_ASSIGNMENTS:,}-assignment limit"
            )
        if type(row) is not expected_type:
            raise TemporalInstrumentIdentityError(
                f"{name} must contain {expected_type.__name__} values"
            )
        rows.append(row)
    return tuple(rows)


def _validate_window(start: date, end: date) -> tuple[date, date]:
    """Validate a finite half-open calendar-date window and its fixed cap."""
    valid_start = _strict_date(start, "window start")
    valid_end = _strict_date(end, "window end")
    if valid_start >= valid_end:
        raise TemporalInstrumentIdentityError("window must be a nonempty [start, end) interval")
    if (valid_end - valid_start).days > _MAX_WINDOW_DAYS:
        raise TemporalInstrumentIdentityError(
            f"window exceeds the {_MAX_WINDOW_DAYS:,}-calendar-day limit"
        )
    return valid_start, valid_end


@dataclass(frozen=True, slots=True, order=True)
class MarketSymbolPair:
    """A six-digit symbol explicitly qualified by one supported exchange."""

    exchange: Exchange | str
    symbol: str

    def __post_init__(self) -> None:
        """Require an explicit exchange and six ASCII digits."""
        try:
            exchange = Exchange(self.exchange)
        except (TypeError, ValueError):
            raise TemporalInstrumentIdentityError(
                "exchange must be explicitly XSHG, XSHE, or XBSE"
            ) from None
        object.__setattr__(self, "exchange", exchange)
        if type(self.symbol) is not str or _SIX_ASCII_DIGITS.fullmatch(self.symbol) is None:
            raise TemporalInstrumentIdentityError("symbol must be exactly six ASCII digits")


@dataclass(frozen=True, slots=True, order=True)
class TemporalIdentityAssignment:
    """One versioned evidence row assigning a pair to an opaque identity.

    The effective interval is half-open: ``valid_from`` is included and
    ``valid_to`` is excluded. ``None`` means no known upper boundary, not proof
    that the identity is currently active. IDs are opaque and never derived
    from an exchange or symbol. ``source_sha256`` is only format-checked and
    echoed; this module does not recalculate it or bind it to the evidence.
    """

    assignment_id: str
    instrument_id: str
    exchange: Exchange | str
    symbol: str
    valid_from: date
    valid_to: date | None
    source_id: str
    source_version: str
    evidence_ref: str
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        """Validate identity fields, effective dates, and evidence metadata."""
        for name in ("assignment_id", "instrument_id", "source_id", "source_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        pair = MarketSymbolPair(self.exchange, self.symbol)
        object.__setattr__(self, "exchange", pair.exchange)
        valid_from = _strict_date(self.valid_from, "valid_from")
        valid_to = self.valid_to
        if valid_to is not None:
            valid_to = _strict_date(valid_to, "valid_to")
            if valid_to <= valid_from:
                raise TemporalInstrumentIdentityError(
                    "valid_to must be after valid_from for a nonempty half-open interval"
                )
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_to", valid_to)
        object.__setattr__(self, "evidence_ref", _evidence_ref(self.evidence_ref, "evidence_ref"))
        if self.source_sha256 is not None and (
            type(self.source_sha256) is not str or _SHA256.fullmatch(self.source_sha256) is None
        ):
            raise TemporalInstrumentIdentityError(
                "source_sha256 must be 64 lowercase hexadecimal characters"
            )

    @property
    def pair(self) -> MarketSymbolPair:
        """Return the explicit exchange-qualified pair for this assignment."""
        return MarketSymbolPair(self.exchange, self.symbol)


@dataclass(frozen=True, slots=True)
class IdentityResolutionSegment:
    """One known assignment or explicit unknown gap within a finite request."""

    valid_from: date
    valid_to: date
    status: IdentityResolutionStatus
    assignment_id: str | None = None
    instrument_id: str | None = None
    pair: MarketSymbolPair | None = None
    source_id: str | None = None
    source_version: str | None = None
    evidence_ref: str | None = None
    source_sha256: str | None = None

    def __post_init__(self) -> None:
        """Keep result segments finite and distinguish unknown from evidence."""
        start = _strict_date(self.valid_from, "segment valid_from")
        end = _strict_date(self.valid_to, "segment valid_to")
        if start >= end:
            raise TemporalInstrumentIdentityError("resolution segments must be nonempty")
        try:
            status = IdentityResolutionStatus(self.status)
        except (TypeError, ValueError):
            raise TemporalInstrumentIdentityError("invalid resolution status") from None
        object.__setattr__(self, "status", status)
        if status is IdentityResolutionStatus.UNKNOWN:
            if any(
                value is not None
                for value in (
                    self.assignment_id,
                    self.instrument_id,
                    self.pair,
                    self.source_id,
                    self.source_version,
                    self.evidence_ref,
                    self.source_sha256,
                )
            ):
                raise TemporalInstrumentIdentityError(
                    "unknown segments cannot carry assignment or identity evidence"
                )
            return
        if (
            self.assignment_id is None
            or self.instrument_id is None
            or not isinstance(self.pair, MarketSymbolPair)
            or self.source_id is None
            or self.source_version is None
            or self.evidence_ref is None
        ):
            raise TemporalInstrumentIdentityError(
                "known segments require their assignment, pair, source, and evidence"
            )
        for name in ("assignment_id", "instrument_id", "source_id", "source_version"):
            _identifier(getattr(self, name), name)
        _evidence_ref(self.evidence_ref, "evidence_ref")
        if self.source_sha256 is not None and (
            type(self.source_sha256) is not str or _SHA256.fullmatch(self.source_sha256) is None
        ):
            raise TemporalInstrumentIdentityError(
                "source_sha256 must be 64 lowercase hexadecimal characters"
            )


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    """A complete partition of a bounded query with unknown gaps preserved."""

    subject: ResolutionSubject
    pair: MarketSymbolPair | None
    instrument_id: str | None
    reference_version: str
    reference_evidence_ref: str
    window_start: date
    window_end: date
    segments: tuple[IdentityResolutionSegment, ...]

    def __post_init__(self) -> None:
        """Validate query identity and the gap-free partition of its window."""
        try:
            subject = ResolutionSubject(self.subject)
        except (TypeError, ValueError):
            raise TemporalInstrumentIdentityError("invalid resolution subject") from None
        object.__setattr__(self, "subject", subject)
        start, end = _validate_window(self.window_start, self.window_end)
        object.__setattr__(self, "window_start", start)
        object.__setattr__(self, "window_end", end)
        object.__setattr__(
            self,
            "reference_version",
            _identifier(self.reference_version, "reference_version"),
        )
        object.__setattr__(
            self,
            "reference_evidence_ref",
            _evidence_ref(self.reference_evidence_ref, "reference_evidence_ref"),
        )
        if subject is ResolutionSubject.PAIR:
            if not isinstance(self.pair, MarketSymbolPair) or self.instrument_id is not None:
                raise TemporalInstrumentIdentityError(
                    "pair resolution requires exactly one explicit market-symbol pair"
                )
        elif self.pair is not None or self.instrument_id is None:
            raise TemporalInstrumentIdentityError(
                "instrument resolution requires exactly one opaque instrument_id"
            )
        else:
            _identifier(self.instrument_id, "instrument_id")
        if type(self.segments) is not tuple or not self.segments:
            raise TemporalInstrumentIdentityError("resolution must contain bounded segments")
        cursor = start
        for segment in self.segments:
            if not isinstance(segment, IdentityResolutionSegment):
                raise TemporalInstrumentIdentityError(
                    "segments must contain IdentityResolutionSegment values"
                )
            if segment.valid_from != cursor:
                raise TemporalInstrumentIdentityError(
                    "resolution segments must cover the window without gaps or overlap"
                )
            if segment.valid_to > end:
                raise TemporalInstrumentIdentityError(
                    "resolution segment exceeds its request window"
                )
            if segment.status is IdentityResolutionStatus.KNOWN:
                if subject is ResolutionSubject.PAIR and segment.pair != self.pair:
                    raise TemporalInstrumentIdentityError(
                        "pair resolution contains an assignment for another pair"
                    )
                if (
                    subject is ResolutionSubject.INSTRUMENT
                    and segment.instrument_id != self.instrument_id
                ):
                    raise TemporalInstrumentIdentityError(
                        "instrument resolution contains another instrument_id"
                    )
            cursor = segment.valid_to
        if cursor != end:
            raise TemporalInstrumentIdentityError(
                "resolution segments must cover the complete request window"
            )


@dataclass(frozen=True, slots=True)
class TemporalIdentityRegistry:
    """Immutable, bounded, caller-supplied temporal identity evidence.

    Different identities cannot overlap for the same exchange-symbol pair.
    A single instrument may have simultaneous assignments for multiple pairs;
    ``resolve_instrument`` rejects a requested interval containing that
    ambiguity instead of guessing which pair represents the instrument. The
    registry defines no canonical digest or cryptographic evidence protocol.
    """

    reference_version: str
    evidence_ref: str
    assignments: tuple[TemporalIdentityAssignment, ...]
    _by_pair: Mapping[MarketSymbolPair, tuple[TemporalIdentityAssignment, ...]] = field(
        init=False, repr=False, compare=False
    )
    _by_instrument: Mapping[str, tuple[TemporalIdentityAssignment, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Freeze sorted assignments and reject pair overlaps or duplicate IDs."""
        object.__setattr__(
            self,
            "reference_version",
            _identifier(self.reference_version, "reference_version"),
        )
        object.__setattr__(self, "evidence_ref", _evidence_ref(self.evidence_ref, "evidence_ref"))
        assignments = _bounded_values(self.assignments, "assignments", TemporalIdentityAssignment)
        if len({row.assignment_id for row in assignments}) != len(assignments):
            raise TemporalInstrumentIdentityError("assignment_id values must be unique")
        ordered = tuple(
            sorted(
                assignments,
                key=lambda row: (
                    Exchange(row.exchange).value,
                    row.symbol,
                    row.valid_from,
                    row.valid_to or date.max,
                    row.instrument_id,
                    row.assignment_id,
                ),
            )
        )
        by_pair_rows: dict[MarketSymbolPair, list[TemporalIdentityAssignment]] = {}
        by_instrument_rows: dict[str, list[TemporalIdentityAssignment]] = {}
        for row in ordered:
            by_pair_rows.setdefault(row.pair, []).append(row)
            by_instrument_rows.setdefault(row.instrument_id, []).append(row)
        for pair_rows in by_pair_rows.values():
            for previous, current in zip(pair_rows, pair_rows[1:], strict=False):
                if previous.valid_to is None or current.valid_from < previous.valid_to:
                    raise TemporalInstrumentIdentityError(
                        "assignments for the same exchange-symbol pair must not overlap"
                    )
        frozen_by_pair = {pair: tuple(pair_rows) for pair, pair_rows in by_pair_rows.items()}
        frozen_by_instrument = {
            instrument_id: tuple(
                sorted(
                    instrument_rows,
                    key=lambda row: (
                        row.valid_from,
                        row.valid_to or date.max,
                        Exchange(row.exchange).value,
                        row.symbol,
                        row.assignment_id,
                    ),
                )
            )
            for instrument_id, instrument_rows in by_instrument_rows.items()
        }
        object.__setattr__(self, "assignments", ordered)
        object.__setattr__(self, "_by_pair", MappingProxyType(frozen_by_pair))
        object.__setattr__(self, "_by_instrument", MappingProxyType(frozen_by_instrument))

    def resolve_pair(self, pair: MarketSymbolPair, start: date, end: date) -> IdentityResolution:
        """Resolve a pair across a finite half-open window, exposing unknown gaps.

        The result proves only that the supplied registry contains the returned
        assignments. It does not interpret gaps as pre-listing, delisting,
        inactivity, or a complete market history.
        """
        if not isinstance(pair, MarketSymbolPair):
            raise TemporalInstrumentIdentityError("pair must be an explicit MarketSymbolPair")
        valid_start, valid_end = _validate_window(start, end)
        matching = self._by_pair.get(pair, ())
        segments = _partition_window(matching, valid_start, valid_end)
        return IdentityResolution(
            subject=ResolutionSubject.PAIR,
            pair=pair,
            instrument_id=None,
            reference_version=self.reference_version,
            reference_evidence_ref=self.evidence_ref,
            window_start=valid_start,
            window_end=valid_end,
            segments=segments,
        )

    def resolve_instrument(self, instrument_id: str, start: date, end: date) -> IdentityResolution:
        """Resolve an opaque instrument's pair chain or fail on concurrent pairs.

        Adjacent pair assignments form a valid handoff. Concurrent assignments
        for different pairs within the requested interval are ambiguous and
        raise ``TemporalInstrumentIdentityError``. Unknown gaps remain explicit.
        """
        identity = _identifier(instrument_id, "instrument_id")
        valid_start, valid_end = _validate_window(start, end)
        matching = self._by_instrument.get(identity, ())
        _reject_instrument_overlap(matching, valid_start, valid_end)
        segments = _partition_window(matching, valid_start, valid_end)
        return IdentityResolution(
            subject=ResolutionSubject.INSTRUMENT,
            pair=None,
            instrument_id=identity,
            reference_version=self.reference_version,
            reference_evidence_ref=self.evidence_ref,
            window_start=valid_start,
            window_end=valid_end,
            segments=segments,
        )


def _reject_instrument_overlap(
    assignments: tuple[TemporalIdentityAssignment, ...], start: date, end: date
) -> None:
    """Reject simultaneous pair mappings for one instrument in the query window."""
    clipped = sorted(
        (
            max(start, row.valid_from),
            min(end, row.valid_to or end),
            Exchange(row.exchange).value,
            row.symbol,
            row.assignment_id,
        )
        for row in assignments
        if max(start, row.valid_from) < min(end, row.valid_to or end)
    )
    furthest_end = start
    for segment_start, segment_end, _, _, _ in clipped:
        if segment_start < furthest_end:
            raise TemporalInstrumentIdentityError(
                "instrument_id maps to multiple pairs during the requested window"
            )
        furthest_end = segment_end


def _partition_window(
    assignments: tuple[TemporalIdentityAssignment, ...], start: date, end: date
) -> tuple[IdentityResolutionSegment, ...]:
    """Clip non-overlapping assignments and fill every evidence gap as unknown."""
    segments: list[IdentityResolutionSegment] = []
    cursor = start
    for row in assignments:
        segment_start = max(start, row.valid_from)
        segment_end = min(end, row.valid_to or end)
        if segment_start >= segment_end:
            continue
        if cursor < segment_start:
            segments.append(
                IdentityResolutionSegment(
                    valid_from=cursor,
                    valid_to=segment_start,
                    status=IdentityResolutionStatus.UNKNOWN,
                )
            )
        segments.append(
            IdentityResolutionSegment(
                valid_from=segment_start,
                valid_to=segment_end,
                status=IdentityResolutionStatus.KNOWN,
                assignment_id=row.assignment_id,
                instrument_id=row.instrument_id,
                pair=row.pair,
                source_id=row.source_id,
                source_version=row.source_version,
                evidence_ref=row.evidence_ref,
                source_sha256=row.source_sha256,
            )
        )
        cursor = segment_end
    if cursor < end:
        segments.append(
            IdentityResolutionSegment(
                valid_from=cursor,
                valid_to=end,
                status=IdentityResolutionStatus.UNKNOWN,
            )
        )
    return tuple(segments)
