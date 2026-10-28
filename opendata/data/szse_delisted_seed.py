"""Validate an offline, caller-attested seed from the SZSE 2023 delisted table.

The yearbook table is a mixed-security candidate source. This module keeps its
rows as candidates until a caller supplies a separate share-class attestation.
It validates the shape and cross-references of those declarations; it cannot
authenticate source files or caller claims. It performs no file,
database, environment, network, or ``instrument_reference`` operations.

Listing date, last trading date, and listing termination date remain three
separate source facts. Nothing in this module declares a complete SZSE, A-share,
or market-wide universe, or promotes rows into the published instrument
registry. An A-share class attestation may still have unreconciled lifecycle
dates.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import Literal

__all__ = [
    "SZSE_2023_YEARBOOK_PDF_SHA256",
    "SZSE_2023_YEARBOOK_SOURCE_ID",
    "CallerAshareClassAttestation",
    "DateFact",
    "DateReconciliation",
    "SeedClassification",
    "SzseDelistedSeedAudit",
    "SzseDelistedSeedError",
    "SzseDelistedSeedRow",
    "ValidatedSzseDelistedRow",
    "validate_szse_delisted_seed",
]

SZSE_2023_YEARBOOK_SOURCE_ID = "szse.yearbook.2023"
SZSE_2023_YEARBOOK_PDF_SHA256 = "954a719859b3767ffa3ed4d1ca3b50803549bcaca6366925a194d27f12be0bb4"
_SOURCE_PROFILE_SHA256: Mapping[str, str] = MappingProxyType(
    {SZSE_2023_YEARBOOK_SOURCE_ID: SZSE_2023_YEARBOOK_PDF_SHA256}
)

_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$")
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
_MAX_TEXT_LENGTH = 512
_MAX_ROWS = 20_000


class SzseDelistedSeedError(ValueError):
    """Raised when a supplied seed or caller attestation is inconsistent."""


class SeedClassification(str, Enum):
    """Caller-attested per-row class; this says nothing about authenticity or completeness."""

    CANDIDATE = "candidate"
    A_SHARE_CLASS_ATTESTED = "a_share_class_attested"
    NON_A_SHARE_CLASS_ATTESTED = "non_a_share_class_attested"
    CLASS_UNRESOLVED = "class_unresolved"


class DateReconciliation(str, Enum):
    """Comparison of the yearbook's three dates with separately supplied dates."""

    NOT_COMPARED = "not_compared"
    INCOMPLETE = "incomplete"
    AGREES = "agrees"
    DISAGREES = "disagrees"


class _ShareClass(str, Enum):
    A = "A"
    B = "B"
    OTHER = "OTHER"
    UNRESOLVED = "UNRESOLVED"


def _bounded_text(value: object, name: str, *, allow_empty: bool = False) -> str:
    """Validate a bounded text value without silently trimming source text."""
    if (
        not isinstance(value, str)
        or len(value) > _MAX_TEXT_LENGTH
        or (not allow_empty and not value)
        or any(ord(character) < 32 for character in value)
    ):
        qualifier = "text" if allow_empty else "nonempty text"
        raise ValueError(f"{name} must be {qualifier} of at most {_MAX_TEXT_LENGTH} characters")
    return value


def _opaque_identifier(value: object, name: str) -> str:
    """Validate a stable, non-path identifier supplied by the caller."""
    if not isinstance(value, str) or _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a 1-128 character stable identifier")
    return value


def _sha256(value: object, name: str) -> str:
    """Require a lowercase SHA-256 value."""
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _strict_date(value: date | None, name: str) -> date | None:
    """Accept an actual date and reject timestamps or string coercion."""
    if value is not None and (isinstance(value, datetime) or not isinstance(value, date)):
        raise ValueError(f"{name} must be datetime.date or None")
    return value


def _bounded_tuple(values: Iterable[object], name: str) -> tuple[object, ...]:
    """Materialize a finite caller iterable under a fixed row limit."""
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable):
        raise ValueError(f"{name} must be a bounded iterable")
    try:
        result = tuple(islice(iter(values), _MAX_ROWS + 1))
    except TypeError as exc:
        raise ValueError(f"{name} must be a bounded iterable") from exc
    if len(result) > _MAX_ROWS:
        raise ValueError(f"{name} exceeds the {_MAX_ROWS:,}-row limit")
    return result


@dataclass(frozen=True, slots=True)
class DateFact:
    """One date exactly as supplied, with its separately parsed calendar date.

    ``raw_text`` is retained verbatim, including an empty source cell. A caller
    may leave ``parsed_date`` unknown if the source representation is ambiguous.
    The module does not parse or infer dates from raw text.
    """

    raw_text: str
    parsed_date: date | None
    source_locator: str

    def __post_init__(self) -> None:
        """Validate source text, date type, and field-level locator."""
        _bounded_text(self.raw_text, "raw_text", allow_empty=True)
        parsed = _strict_date(self.parsed_date, "parsed_date")
        if parsed is not None and not self.raw_text:
            raise ValueError("parsed_date requires nonempty raw_text")
        _bounded_text(self.source_locator, "source_locator")


@dataclass(frozen=True, slots=True)
class SzseDelistedSeedRow:
    """A row extracted from the mixed-security table in the 2023 SZSE yearbook.

    The six-digit code is only a candidate key; it does not establish A-share
    class. Both the printed page label and one-based PDF page are retained to
    avoid conflating the two page numbering systems.
    """

    symbol: str
    issuer_name: str
    source_id: str
    pdf_sha256: str
    pdf_page_number: int
    printed_page_label: str
    row_locator: str
    listing_date: DateFact
    last_trade_date: DateFact
    termination_date: DateFact
    security_class_as_printed: str | None = None

    def __post_init__(self) -> None:
        """Validate row identity, source pin, and chronology without inferring facts."""
        if not isinstance(self.symbol, str) or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must be exactly six decimal digits")
        _bounded_text(self.issuer_name, "issuer_name")
        object.__setattr__(self, "source_id", _opaque_identifier(self.source_id, "source_id"))
        _sha256(self.pdf_sha256, "pdf_sha256")
        if type(self.pdf_page_number) is not int or not 1 <= self.pdf_page_number <= 10_000:
            raise ValueError("pdf_page_number must be an integer from 1 through 10000")
        _bounded_text(self.printed_page_label, "printed_page_label")
        _bounded_text(self.row_locator, "row_locator")
        for name in ("listing_date", "last_trade_date", "termination_date"):
            if not isinstance(getattr(self, name), DateFact):
                raise ValueError(f"{name} must be a DateFact")
        if self.security_class_as_printed is not None:
            _bounded_text(self.security_class_as_printed, "security_class_as_printed")
        _validate_date_order(
            self.listing_date,
            self.last_trade_date,
            self.termination_date,
            context="yearbook",
        )


@dataclass(frozen=True, slots=True)
class CallerAshareClassAttestation:
    """Caller-attested independent share-class evidence for one candidate.

    The exact six-digit symbol and issuer name bind the attestation to a seed
    row. No canonical ``instrument_id`` is assigned because the existing
    registry accepts caller-defined identifiers and has no approved symbol-to-ID
    convention. Independence, attester identity, and source contents remain
    caller-attested; this value is not an authenticated evidence chain.
    """

    symbol: str
    issuer_name: str
    share_class: str
    market: str
    exchange: str
    source_id: str
    pdf_sha256: str
    source_locator: str
    attestation_ref: str
    attester_id: str
    attested_on: date
    attests_independent_of_yearbook: bool
    listing_date: DateFact | None = None
    last_trade_date: DateFact | None = None
    termination_date: DateFact | None = None

    def __post_init__(self) -> None:
        """Validate exact candidate identity and caller-attested source declarations."""
        if not isinstance(self.symbol, str) or _SYMBOL_PATTERN.fullmatch(self.symbol) is None:
            raise ValueError("symbol must be exactly six decimal digits")
        _bounded_text(self.issuer_name, "issuer_name")
        try:
            share_class = _ShareClass(self.share_class)
        except (TypeError, ValueError) as exc:
            raise ValueError("share_class must be A, B, OTHER, or UNRESOLVED") from exc
        object.__setattr__(self, "share_class", share_class.value)
        if self.market != "CN" or self.exchange != "XSHE":
            raise ValueError("attestation must explicitly identify market CN and exchange XSHE")
        object.__setattr__(self, "source_id", _opaque_identifier(self.source_id, "source_id"))
        _sha256(self.pdf_sha256, "pdf_sha256")
        _bounded_text(self.source_locator, "source_locator")
        _bounded_text(self.attestation_ref, "attestation_ref")
        _opaque_identifier(self.attester_id, "attester_id")
        attested_on = _strict_date(self.attested_on, "attested_on")
        if attested_on is None:
            raise ValueError("attested_on is required")
        object.__setattr__(self, "attested_on", attested_on)
        if type(self.attests_independent_of_yearbook) is not bool:
            raise ValueError("attests_independent_of_yearbook must be a bool")
        for name in ("listing_date", "last_trade_date", "termination_date"):
            fact = getattr(self, name)
            if fact is not None and not isinstance(fact, DateFact):
                raise ValueError(f"{name} must be a DateFact or None")
        _validate_date_order(
            self.listing_date,
            self.last_trade_date,
            self.termination_date,
            context="attestation",
        )


@dataclass(frozen=True, slots=True)
class ValidatedSzseDelistedRow:
    """One candidate and its optional, separately retained caller attestation."""

    candidate: SzseDelistedSeedRow
    classification: SeedClassification
    independent_attestation: CallerAshareClassAttestation | None
    date_reconciliation: DateReconciliation
    date_conflicts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SzseDelistedSeedAudit:
    """Deterministic audit output; completeness is deliberately never assessed.

    ``market_completeness`` is excluded from the constructor and fixed to
    ``not_assessed``. Frozen dataclasses prevent ordinary assignment, but Python
    callers can bypass this guard with ``object.__setattr__``; it is not a
    security boundary against hostile in-process code.
    """

    source_id: str
    source_pdf_sha256: str
    rows: tuple[ValidatedSzseDelistedRow, ...]
    market_completeness: Literal["not_assessed"] = field(default="not_assessed", init=False)

    @property
    def candidate_count(self) -> int:
        """Return rows with no independent share-class decision."""
        return sum(row.classification is SeedClassification.CANDIDATE for row in self.rows)

    @property
    def a_share_attested_count(self) -> int:
        """Return rows whose caller attestation declares A-share class."""
        return sum(
            row.classification is SeedClassification.A_SHARE_CLASS_ATTESTED for row in self.rows
        )

    @property
    def non_a_share_attested_count(self) -> int:
        """Return rows whose caller attestation declares a non-A share class."""
        return sum(
            row.classification is SeedClassification.NON_A_SHARE_CLASS_ATTESTED for row in self.rows
        )

    @property
    def unresolved_class_count(self) -> int:
        """Return rows whose caller attestation did not determine share class."""
        return sum(row.classification is SeedClassification.CLASS_UNRESOLVED for row in self.rows)


def validate_szse_delisted_seed(
    candidates: Iterable[SzseDelistedSeedRow],
    attestations: Iterable[CallerAshareClassAttestation] = (),
) -> SzseDelistedSeedAudit:
    """Validate mixed-table candidates and caller-attested share-class evidence.

    The fixed profile binds ``szse.yearbook.2023`` to its captured PDF SHA-256;
    callers cannot substitute a digest or label an arbitrary source as that
    yearbook. This shape check still cannot authenticate row extraction or
    artifacts. A caller-attested class never overwrites yearbook facts, and date
    differences remain visible.

    Args:
        candidates: Bounded candidate rows extracted from the mixed-security table.
        attestations: Optional caller declarations keyed by exact symbol and
            issuer name, with an asserted independent source and attestation reference.

    Returns:
        An immutable, symbol-sorted audit with per-row classification and date
        comparison. The source profile and ``market_completeness`` are pinned;
        completeness is always ``not_assessed``.

    Raises:
        SzseDelistedSeedError: If row types, profile bindings, identities, or attestation
            declarations are duplicated, dangling, or internally inconsistent.
    """
    try:
        candidate_rows = _bounded_tuple(candidates, "candidates")
        attestation_rows = _bounded_tuple(attestations, "attestations")
        if any(not isinstance(row, SzseDelistedSeedRow) for row in candidate_rows):
            raise ValueError("candidates must contain SzseDelistedSeedRow rows")
        if any(not isinstance(row, CallerAshareClassAttestation) for row in attestation_rows):
            raise ValueError("attestations must contain CallerAshareClassAttestation rows")
        typed_candidates = tuple(
            row for row in candidate_rows if isinstance(row, SzseDelistedSeedRow)
        )
        typed_attestations = tuple(
            row for row in attestation_rows if isinstance(row, CallerAshareClassAttestation)
        )

        by_symbol: dict[str, SzseDelistedSeedRow] = {}
        row_locations: dict[tuple[str, str, int, str], SzseDelistedSeedRow] = {}
        for row in typed_candidates:
            profile_digest = _SOURCE_PROFILE_SHA256.get(row.source_id)
            if profile_digest is None:
                raise ValueError(f"candidate source_id has no controlled profile: {row.source_id}")
            if row.pdf_sha256 != profile_digest:
                raise ValueError(
                    f"candidate source_id/digest does not match controlled profile: {row.source_id}"
                )
            row_key = (row.source_id, row.pdf_sha256, row.pdf_page_number, row.row_locator)
            prior_row = row_locations.get(row_key)
            if prior_row is not None:
                raise ValueError(
                    "duplicate source row locator "
                    f"{row.source_id} page {row.pdf_page_number} {row.row_locator!r}; "
                    f"symbols {prior_row.symbol} and {row.symbol} conflict"
                )
            row_locations[row_key] = row
            if row.symbol in by_symbol:
                prior_row = by_symbol[row.symbol]
                raise ValueError(
                    f"duplicate candidate symbol {row.symbol}: "
                    f"pages {prior_row.pdf_page_number} and {row.pdf_page_number}"
                )
            by_symbol[row.symbol] = row

        attestations_by_symbol: dict[str, CallerAshareClassAttestation] = {}
        for attestation in typed_attestations:
            if attestation.symbol not in by_symbol:
                raise ValueError("attestation has no matching yearbook candidate")
            if attestation.symbol in attestations_by_symbol:
                raise ValueError("attestation symbols must be unique within one seed")
            candidate = by_symbol[attestation.symbol]
            if attestation.issuer_name != candidate.issuer_name:
                raise ValueError("attestation issuer_name does not exactly match candidate")
            if not attestation.attests_independent_of_yearbook:
                raise ValueError("attestation must declare independence from the yearbook")
            if (
                attestation.source_id == candidate.source_id
                or attestation.pdf_sha256 == candidate.pdf_sha256
            ):
                raise ValueError(
                    "attestation source must differ from yearbook source and document digest"
                )
            attestations_by_symbol[attestation.symbol] = attestation

        results = tuple(
            _validated_row(candidate, attestations_by_symbol.get(candidate.symbol))
            for candidate in sorted(typed_candidates, key=lambda row: row.symbol)
        )
    except SzseDelistedSeedError:
        raise
    except (TypeError, ValueError) as exc:
        raise SzseDelistedSeedError(str(exc)) from exc

    return SzseDelistedSeedAudit(
        source_id=SZSE_2023_YEARBOOK_SOURCE_ID,
        source_pdf_sha256=SZSE_2023_YEARBOOK_PDF_SHA256,
        rows=results,
    )


def _validated_row(
    candidate: SzseDelistedSeedRow,
    attestation: CallerAshareClassAttestation | None,
) -> ValidatedSzseDelistedRow:
    """Derive caller-attested disposition and field-by-field date comparison."""
    if attestation is None:
        return ValidatedSzseDelistedRow(
            candidate,
            SeedClassification.CANDIDATE,
            None,
            DateReconciliation.NOT_COMPARED,
            (),
        )
    if attestation.share_class == _ShareClass.A.value:
        classification = SeedClassification.A_SHARE_CLASS_ATTESTED
    elif attestation.share_class in {_ShareClass.B.value, _ShareClass.OTHER.value}:
        classification = SeedClassification.NON_A_SHARE_CLASS_ATTESTED
    else:
        classification = SeedClassification.CLASS_UNRESOLVED

    pairs = (
        ("listing_date", candidate.listing_date, attestation.listing_date),
        ("last_trade_date", candidate.last_trade_date, attestation.last_trade_date),
        ("termination_date", candidate.termination_date, attestation.termination_date),
    )
    comparable = 0
    conflicts: list[str] = []
    complete = True
    for name, yearbook_fact, attested_fact in pairs:
        if (
            attested_fact is None
            or yearbook_fact.parsed_date is None
            or attested_fact.parsed_date is None
        ):
            complete = False
        else:
            comparable += 1
            if yearbook_fact.parsed_date != attested_fact.parsed_date:
                conflicts.append(name)

    if conflicts:
        reconciliation = DateReconciliation.DISAGREES
    elif complete and comparable == len(pairs):
        reconciliation = DateReconciliation.AGREES
    else:
        reconciliation = DateReconciliation.INCOMPLETE
    return ValidatedSzseDelistedRow(
        candidate,
        classification,
        attestation,
        reconciliation,
        tuple(conflicts),
    )


def _validate_date_order(
    listing: DateFact | None,
    last_trade: DateFact | None,
    termination: DateFact | None,
    *,
    context: str,
) -> None:
    """Reject impossible ordering among explicitly parsed date facts only."""
    facts = (
        ("listing_date", listing),
        ("last_trade_date", last_trade),
        ("termination_date", termination),
    )
    known = [(name, fact.parsed_date) for name, fact in facts if fact is not None]
    for left_index, (left_name, left_date) in enumerate(known):
        if left_date is None:
            continue
        for right_name, right_date in known[left_index + 1 :]:
            if right_date is not None and left_date > right_date:
                raise ValueError(f"{context} {left_name} cannot follow {right_name}")
