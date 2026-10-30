"""Project captured exchange catalogs into reviewable identity candidates.

The projectors retain the exact source row and page provenance while exposing
only caller-declared candidate identity mappings. Candidate identities are not
reviewed registry rows. This module does not assign lifecycle facts, establish
source authenticity, prove pagination, or claim market completeness.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from itertools import islice
from types import MappingProxyType
from typing import TypeAlias

from opendata.data.current_instrument_census import (
    CensusAcquisitionStatus,
    InstrumentIdentity,
)
from opendata.data.instrument_source_census import (
    FieldObservationState,
    PageAttemptStatus,
)
from opendata.pipeline.fuyao_instrument_census import FuyaoInstrumentCensusCapture
from opendata.pipeline.sse_instrument_census import (
    SSEInstrumentCensusCapture,
    SSEInstrumentSelector,
)

__all__ = [
    "CandidateFieldObservation",
    "CandidateMembershipContext",
    "CatalogCandidate",
    "CatalogFieldBindings",
    "CatalogFieldConflictStatus",
    "CatalogIdentitySetObservation",
    "CatalogIdentityDisposition",
    "CatalogProjection",
    "CatalogReconciliation",
    "CatalogSetDifferenceStatus",
    "CatalogSourceConflict",
    "ExchangeCatalogComparison",
    "FuyaoInstrumentFieldBindings",
    "InstrumentCatalogReconciliationError",
    "reconcile_instrument_candidates",
    "project_fuyao_capture",
    "project_sse_capture",
]

_SYMBOL_PATTERN = re.compile(r"^[0-9]{6}$", re.ASCII)
_QUALIFIED_FUYAO_CODE_PATTERN = re.compile(
    r"^(?P<symbol>[0-9]{6})\.(?P<exchange>SH|SZ|BJ)$", re.ASCII
)
_FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$", re.ASCII)
_SOURCE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$", re.ASCII)
_EXCHANGES = frozenset({"SH", "SZ", "BJ"})
_MAX_CANDIDATES = 100_000
_MAX_FIELDS = 16
_MAX_FIELD_ROW_PAIR_COMPARISONS = 20_000
_MAX_FIELD_CONFLICT_OUTPUT = 5_000

FrozenJson: TypeAlias = (
    bool | int | float | str | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"] | None
)


class InstrumentCatalogReconciliationError(ValueError):
    """Raised when a candidate projection or comparison input is malformed."""


def _validate_optional_field_name(value: str | None, label: str, *, required: bool) -> None:
    """Require explicit simple raw field labels; never interpret dotted paths."""
    if value is None and not required:
        return
    if type(value) is not str or _FIELD_NAME_PATTERN.fullmatch(value) is None:
        raise InstrumentCatalogReconciliationError(f"{label}_invalid")


class CandidateMembershipContext(str, Enum):
    """Caller query context; it deliberately does not encode lifecycle status."""

    CURRENT_QUERY = "current_query"
    STATUS3_QUERY_CANDIDATE = "status3_query_candidate"


class CatalogIdentityDisposition(str, Enum):
    """How much identity material could be formed from explicitly mapped data."""

    CANDIDATE = "candidate"
    UNRESOLVED = "unresolved"
    CONFLICT = "conflict"


class CatalogSetDifferenceStatus(str, Enum):
    """Whether captured identity-set differences have sufficient fetch evidence."""

    OBSERVED = "observed"
    UNKNOWN = "unknown"


class CatalogFieldConflictStatus(str, Enum):
    """Whether cross-source row-pair field review completed within its limits."""

    COMPLETE = "complete"
    TRUNCATED = "truncated"


@dataclass(frozen=True, slots=True)
class CatalogFieldBindings:
    """Explicit raw field names used by the SSE candidate projector.

    Only ``code_field`` is required. Optional fields remain ``UNMAPPED`` until
    the caller supplies a source schema reviewed against retained evidence.
    Field labels are local mapping names, not claims about vendor semantics.
    """

    code_field: str
    name_field: str | None = None
    exchange_field: str | None = None
    asset_type_field: str | None = None
    status_field: str | None = None
    listing_date_field: str | None = None
    last_trading_date_field: str | None = None
    termination_date_field: str | None = None

    def __post_init__(self) -> None:
        """Validate explicit field selectors without applying aliases."""
        for label in (
            "code_field",
            "name_field",
            "exchange_field",
            "asset_type_field",
            "status_field",
            "listing_date_field",
            "last_trading_date_field",
            "termination_date_field",
        ):
            _validate_optional_field_name(
                getattr(self, label), label, required=label == "code_field"
            )
        mapped = [
            value
            for value in (
                self.code_field,
                self.name_field,
                self.exchange_field,
                self.asset_type_field,
                self.status_field,
                self.listing_date_field,
                self.last_trading_date_field,
                self.termination_date_field,
            )
            if value is not None
        ]
        if len(mapped) != len(set(mapped)):
            raise InstrumentCatalogReconciliationError("field_bindings_must_be_distinct")


@dataclass(frozen=True, slots=True)
class FuyaoInstrumentFieldBindings:
    """Fields from the existing THS instrument provider's source vocabulary.

    The current A-share capture profile documents ``thscode``, ``name``,
    ``exchange``, ``asset_type``, and ``list_date``. ``list_date`` is kept as
    an observed value/null only; it is not used to set a known start date.
    """

    code_field: str = "thscode"
    name_field: str = "name"
    exchange_field: str = "exchange"
    asset_type_field: str = "asset_type"
    listing_date_field: str = "list_date"
    status_field: str = "status"

    def __post_init__(self) -> None:
        """Require unique, valid source field names."""
        values = (
            self.code_field,
            self.name_field,
            self.exchange_field,
            self.asset_type_field,
            self.listing_date_field,
            self.status_field,
        )
        for value in values:
            _validate_optional_field_name(value, "fuyao_field", required=True)
        if len(values) != len(set(values)):
            raise InstrumentCatalogReconciliationError("field_bindings_must_be_distinct")


@dataclass(frozen=True, slots=True)
class CandidateFieldObservation:
    """One raw field observation with its exact source label and state."""

    source_field: str | None
    state: FieldObservationState | str
    raw_value: FrozenJson = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Validate field-state/value combinations without coercing source data."""
        try:
            state = FieldObservationState(self.state)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("field_state_invalid") from None
        object.__setattr__(self, "state", state)
        _validate_optional_field_name(self.source_field, "source_field", required=False)
        if state is FieldObservationState.UNMAPPED:
            if self.source_field is not None or self.raw_value is not None:
                raise InstrumentCatalogReconciliationError("unmapped_field_has_source_value")
            return
        if self.source_field is None:
            raise InstrumentCatalogReconciliationError("mapped_field_name_required")
        if state in (FieldObservationState.ABSENT, FieldObservationState.NULL):
            if self.raw_value is not None:
                raise InstrumentCatalogReconciliationError("empty_field_has_raw_value")
        elif self.raw_value is None:
            raise InstrumentCatalogReconciliationError("value_field_raw_value_required")
        else:
            object.__setattr__(self, "raw_value", _freeze_json_value(self.raw_value))


@dataclass(frozen=True, slots=True, repr=False)
class CatalogCandidate:
    """One row-level candidate and the evidence location that produced it."""

    source_id: str
    observed_at: datetime
    page_sequence: int
    source_page_number: int | str | None
    source_row_number: int
    selector_context: str | None
    membership_context: CandidateMembershipContext | str
    request_summary_sha256: str | None
    response_sha256: str
    disposition: CatalogIdentityDisposition | str
    exchange_candidate: str | None
    source_code: CandidateFieldObservation
    identity_candidate: InstrumentIdentity | None
    fields: Mapping[str, CandidateFieldObservation] = field(repr=False)
    raw_row: Mapping[str, FrozenJson] = field(repr=False)
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        """Validate provenance, candidate key, and immutable source row."""
        if type(self.source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None:
            raise InstrumentCatalogReconciliationError("source_id_invalid")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InstrumentCatalogReconciliationError("observed_at_invalid")
        if self.observed_at.utcoffset() is None:
            raise InstrumentCatalogReconciliationError("observed_at_invalid")
        if type(self.page_sequence) is not int or self.page_sequence < 1:
            raise InstrumentCatalogReconciliationError("page_sequence_invalid")
        if type(self.source_row_number) is not int or self.source_row_number < 1:
            raise InstrumentCatalogReconciliationError("source_row_number_invalid")
        if self.source_page_number is not None and type(self.source_page_number) not in (int, str):
            raise InstrumentCatalogReconciliationError("source_page_number_invalid")
        if type(self.response_sha256) is not str or not _is_sha256(self.response_sha256):
            raise InstrumentCatalogReconciliationError("response_sha256_invalid")
        if self.request_summary_sha256 is not None and not _is_sha256(self.request_summary_sha256):
            raise InstrumentCatalogReconciliationError("request_sha256_invalid")
        try:
            membership = CandidateMembershipContext(self.membership_context)
            disposition = CatalogIdentityDisposition(self.disposition)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("candidate_context_invalid") from None
        object.__setattr__(self, "membership_context", membership)
        object.__setattr__(self, "disposition", disposition)
        if self.exchange_candidate is not None and self.exchange_candidate not in _EXCHANGES:
            raise InstrumentCatalogReconciliationError("exchange_candidate_invalid")
        if self.identity_candidate is not None:
            if not isinstance(self.identity_candidate, InstrumentIdentity):
                raise InstrumentCatalogReconciliationError("identity_candidate_invalid")
            if self.identity_candidate.exchange != self.exchange_candidate:
                raise InstrumentCatalogReconciliationError("identity_exchange_mismatch")
            if disposition is not CatalogIdentityDisposition.CANDIDATE:
                raise InstrumentCatalogReconciliationError("noncandidate_has_identity")
        elif disposition is CatalogIdentityDisposition.CANDIDATE:
            raise InstrumentCatalogReconciliationError("candidate_identity_required")
        if disposition is CatalogIdentityDisposition.UNRESOLVED and not self.unresolved_reason:
            raise InstrumentCatalogReconciliationError("unresolved_reason_required")
        if disposition is CatalogIdentityDisposition.CONFLICT and not self.unresolved_reason:
            raise InstrumentCatalogReconciliationError("conflict_reason_required")
        if not isinstance(self.source_code, CandidateFieldObservation):
            raise InstrumentCatalogReconciliationError("source_code_observation_invalid")
        if not isinstance(self.fields, Mapping) or len(self.fields) > _MAX_FIELDS:
            raise InstrumentCatalogReconciliationError("candidate_fields_invalid")
        frozen_fields: dict[str, CandidateFieldObservation] = {}
        for key, value in self.fields.items():
            if type(key) is not str or not _FIELD_NAME_PATTERN.fullmatch(key):
                raise InstrumentCatalogReconciliationError("candidate_field_key_invalid")
            if not isinstance(value, CandidateFieldObservation):
                raise InstrumentCatalogReconciliationError("candidate_field_value_invalid")
            frozen_fields[key] = value
        object.__setattr__(self, "fields", MappingProxyType(frozen_fields))
        raw_row = _freeze_raw_row(self.raw_row)
        if not raw_row:
            raise InstrumentCatalogReconciliationError("raw_row_invalid")
        object.__setattr__(self, "raw_row", raw_row)
        _validate_candidate_evidence(self)

    def __repr__(self) -> str:
        """Render bounded provenance without exposing source row values."""
        return (
            "CatalogCandidate("
            f"source_id={self.source_id!r}, page_sequence={self.page_sequence}, "
            f"source_row_number={self.source_row_number}, "
            f"disposition={CatalogIdentityDisposition(self.disposition).value!r}, "
            "source_values_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CatalogProjection:
    """A source capture projected into candidates without a completeness claim."""

    source_id: str
    observed_at: datetime
    declared_exchange_scope: tuple[str, ...]
    current_query_scope: tuple[str, ...]
    acquisition_status: CensusAcquisitionStatus | str
    pagination_status: str
    source_authenticity: str
    market_completeness: str
    candidates: tuple[CatalogCandidate, ...] = field(repr=False)
    contexts_seen: tuple[str, ...]

    def __post_init__(self) -> None:
        """Freeze bounded projection fields and keep UNKNOWN explicit."""
        if type(self.source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(self.source_id) is None:
            raise InstrumentCatalogReconciliationError("source_id_invalid")
        if not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None:
            raise InstrumentCatalogReconciliationError("observed_at_invalid")
        declared = _exchange_tuple(self.declared_exchange_scope, "declared_exchange_scope")
        current = _exchange_tuple(self.current_query_scope, "current_query_scope", allow_empty=True)
        if any(exchange not in declared for exchange in current):
            raise InstrumentCatalogReconciliationError("current_scope_outside_declared_scope")
        object.__setattr__(self, "declared_exchange_scope", declared)
        object.__setattr__(self, "current_query_scope", current)
        try:
            status = CensusAcquisitionStatus(self.acquisition_status)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("acquisition_status_invalid") from None
        object.__setattr__(self, "acquisition_status", status)
        candidate_items = _bounded_tuple(self.candidates, "candidates", _MAX_CANDIDATES)
        candidates = tuple(row for row in candidate_items if isinstance(row, CatalogCandidate))
        if len(candidates) != len(candidate_items):
            raise InstrumentCatalogReconciliationError("candidates_invalid")
        if any(
            row.source_id != self.source_id or row.observed_at != self.observed_at
            for row in candidates
        ):
            raise InstrumentCatalogReconciliationError("candidate_projection_mismatch")
        for row in candidates:
            _validate_candidate_scope(row, declared, current)
        object.__setattr__(self, "candidates", candidates)
        contexts = _bounded_tuple(self.contexts_seen, "contexts_seen", 32)
        if any(type(context) is not str or not context for context in contexts):
            raise InstrumentCatalogReconciliationError("contexts_seen_invalid")
        contexts = tuple(dict.fromkeys(contexts))
        object.__setattr__(self, "contexts_seen", contexts)
        if self.source_id == "sse.instrument":
            current_selectors = {
                SSEInstrumentSelector.CURRENT_MAIN_A.value,
                SSEInstrumentSelector.CURRENT_STAR.value,
            }
            status3_selectors = {
                SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE.value,
                SSEInstrumentSelector.STATUS3_STAR_CANDIDATE.value,
            }
            valid_selectors = current_selectors | status3_selectors
            expected_current_scope = ("SH",) if current_selectors.intersection(contexts) else ()
            if (
                declared != ("SH",)
                or current != expected_current_scope
                or any(context not in valid_selectors for context in contexts)
                or any(candidate.selector_context not in contexts for candidate in candidates)
            ):
                raise InstrumentCatalogReconciliationError("sse_projection_scope_invalid")
        for label in ("pagination_status", "source_authenticity", "market_completeness"):
            value = getattr(self, label)
            if type(value) is not str or not value or len(value) > 64:
                raise InstrumentCatalogReconciliationError(f"{label}_invalid")
        if self.market_completeness.lower() != "unknown":
            raise InstrumentCatalogReconciliationError("market_completeness_must_remain_unknown")

    def __repr__(self) -> str:
        """Render counts and status while withholding candidate contents."""
        return (
            f"CatalogProjection(source_id={self.source_id!r}, "
            f"candidate_count={len(self.candidates)}, "
            f"pagination_status={self.pagination_status!r}, "
            "market_completeness='unknown', values_redacted=True)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class CatalogSourceConflict:
    """A row-level disagreement retained as a review item, not auto-resolved."""

    identity: InstrumentIdentity = field(repr=False)
    field_name: str
    left_source_id: str
    left_source_field: str | None
    left_page_sequence: int
    left_source_row_number: int
    right_source_id: str
    right_source_field: str | None
    right_page_sequence: int
    right_source_row_number: int
    ambiguous_identity: bool
    left_identity_row_count: int
    right_identity_row_count: int
    left_value: FrozenJson = field(repr=False)
    right_value: FrozenJson = field(repr=False)

    def __post_init__(self) -> None:
        """Validate conflict provenance and keep ambiguity counts self-consistent."""
        if not isinstance(self.identity, InstrumentIdentity):
            raise InstrumentCatalogReconciliationError("conflict_identity_invalid")
        if self.field_name != "display_name":
            raise InstrumentCatalogReconciliationError("conflict_field_invalid")
        for source_id in (self.left_source_id, self.right_source_id):
            if type(source_id) is not str or _SOURCE_ID_PATTERN.fullmatch(source_id) is None:
                raise InstrumentCatalogReconciliationError("conflict_source_id_invalid")
        for page_sequence, row_number in (
            (self.left_page_sequence, self.left_source_row_number),
            (self.right_page_sequence, self.right_source_row_number),
        ):
            if type(page_sequence) is not int or page_sequence < 1:
                raise InstrumentCatalogReconciliationError("conflict_page_sequence_invalid")
            if type(row_number) is not int or row_number < 1:
                raise InstrumentCatalogReconciliationError("conflict_row_number_invalid")
        if (
            type(self.left_identity_row_count) is not int
            or self.left_identity_row_count < 1
            or type(self.right_identity_row_count) is not int
            or self.right_identity_row_count < 1
        ):
            raise InstrumentCatalogReconciliationError("conflict_identity_row_count_invalid")
        expected_ambiguity = self.left_identity_row_count > 1 or self.right_identity_row_count > 1
        if (
            type(self.ambiguous_identity) is not bool
            or self.ambiguous_identity != expected_ambiguity
        ):
            raise InstrumentCatalogReconciliationError("conflict_ambiguity_flag_invalid")
        left_value = _freeze_json_value(self.left_value)
        right_value = _freeze_json_value(self.right_value)
        if _same_json(left_value, right_value):
            raise InstrumentCatalogReconciliationError("conflict_values_must_differ")
        object.__setattr__(self, "left_value", left_value)
        object.__setattr__(self, "right_value", right_value)


@dataclass(frozen=True, slots=True, repr=False)
class CatalogIdentitySetObservation:
    """Observed identity-set partition; left/right-only are not market gaps."""

    difference_status: CatalogSetDifferenceStatus | str
    left_observed_identities: tuple[InstrumentIdentity, ...]
    right_observed_identities: tuple[InstrumentIdentity, ...]
    left_only_observed: tuple[InstrumentIdentity, ...]
    right_only_observed: tuple[InstrumentIdentity, ...]
    shared_observed: tuple[InstrumentIdentity, ...]
    unknown_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        """Validate status and make clear that unknown differences stay unknown."""
        try:
            status = CatalogSetDifferenceStatus(self.difference_status)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("difference_status_invalid") from None
        object.__setattr__(self, "difference_status", status)
        names = (
            "left_observed_identities",
            "right_observed_identities",
            "left_only_observed",
            "right_only_observed",
            "shared_observed",
        )
        for name in names:
            identities = _bounded_tuple(getattr(self, name), name, _MAX_CANDIDATES)
            if any(not isinstance(identity, InstrumentIdentity) for identity in identities):
                raise InstrumentCatalogReconciliationError(f"{name}_invalid")
            normalized = tuple(
                identity for identity in identities if isinstance(identity, InstrumentIdentity)
            )
            if tuple(sorted(set(normalized))) != normalized:
                raise InstrumentCatalogReconciliationError(f"{name}_not_unique_sorted")
            object.__setattr__(self, name, normalized)
        reasons = _bounded_tuple(self.unknown_reasons, "unknown_reasons", 16)
        if any(type(reason) is not str or not reason for reason in reasons):
            raise InstrumentCatalogReconciliationError("unknown_reasons_invalid")
        object.__setattr__(self, "unknown_reasons", tuple(dict.fromkeys(reasons)))
        if status is CatalogSetDifferenceStatus.UNKNOWN and not reasons:
            raise InstrumentCatalogReconciliationError("unknown_difference_reason_required")
        if status is CatalogSetDifferenceStatus.OBSERVED and reasons:
            raise InstrumentCatalogReconciliationError("observed_difference_has_unknown_reason")
        left_set = set(self.left_observed_identities)
        right_set = set(self.right_observed_identities)
        if self.left_only_observed != tuple(sorted(left_set - right_set)):
            raise InstrumentCatalogReconciliationError("left_only_observed_inconsistent")
        if self.right_only_observed != tuple(sorted(right_set - left_set)):
            raise InstrumentCatalogReconciliationError("right_only_observed_inconsistent")
        if self.shared_observed != tuple(sorted(left_set & right_set)):
            raise InstrumentCatalogReconciliationError("shared_observed_inconsistent")


@dataclass(frozen=True, slots=True, repr=False)
class ExchangeCatalogComparison:
    """Set difference for one exchange scope, with duplicate and field conflicts."""

    exchange: str
    identity_sets: CatalogIdentitySetObservation = field(repr=False)
    left_duplicate_identities: tuple[InstrumentIdentity, ...]
    right_duplicate_identities: tuple[InstrumentIdentity, ...]
    field_conflicts: tuple[CatalogSourceConflict, ...] = field(repr=False)
    field_conflict_status: CatalogFieldConflictStatus | str
    field_conflict_total_pairs: int
    field_conflict_pairs_compared: int

    def __post_init__(self) -> None:
        """Reject misleading conflict-review status or unbounded result rows."""
        if type(self.exchange) is not str or self.exchange not in _EXCHANGES:
            raise InstrumentCatalogReconciliationError("comparison_exchange_invalid")
        try:
            status = CatalogFieldConflictStatus(self.field_conflict_status)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("field_conflict_status_invalid") from None
        object.__setattr__(self, "field_conflict_status", status)
        if (
            type(self.field_conflict_total_pairs) is not int
            or self.field_conflict_total_pairs < 0
            or type(self.field_conflict_pairs_compared) is not int
            or not 0 <= self.field_conflict_pairs_compared <= self.field_conflict_total_pairs
        ):
            raise InstrumentCatalogReconciliationError("field_conflict_pair_counts_invalid")
        if (status is CatalogFieldConflictStatus.COMPLETE) != (
            self.field_conflict_pairs_compared == self.field_conflict_total_pairs
        ):
            raise InstrumentCatalogReconciliationError("field_conflict_status_count_mismatch")
        conflicts = _bounded_tuple(
            self.field_conflicts, "field_conflicts", _MAX_FIELD_CONFLICT_OUTPUT
        )
        if len(conflicts) > self.field_conflict_pairs_compared or any(
            not isinstance(conflict, CatalogSourceConflict) for conflict in conflicts
        ):
            raise InstrumentCatalogReconciliationError("field_conflicts_invalid")
        object.__setattr__(self, "field_conflicts", conflicts)
        if not isinstance(self.identity_sets, CatalogIdentitySetObservation):
            raise InstrumentCatalogReconciliationError("identity_sets_invalid")


@dataclass(frozen=True, slots=True, repr=False)
class CatalogReconciliation:
    """Pairwise candidate comparison; source and market completeness stay unknown."""

    left_source_id: str
    right_source_id: str
    exchange_comparisons: tuple[ExchangeCatalogComparison, ...] = field(repr=False)
    not_compared_exchanges: tuple[str, ...]
    left_unresolved_rows: tuple[CatalogCandidate, ...] = field(repr=False)
    right_unresolved_rows: tuple[CatalogCandidate, ...] = field(repr=False)
    left_identity_conflicts: tuple[CatalogCandidate, ...] = field(repr=False)
    right_identity_conflicts: tuple[CatalogCandidate, ...] = field(repr=False)
    market_completeness: str = "unknown"

    def __post_init__(self) -> None:
        """Keep the reconciliation's broadest claim fail-closed."""
        if self.market_completeness != "unknown":
            raise InstrumentCatalogReconciliationError("market_completeness_must_remain_unknown")

    def __repr__(self) -> str:
        """Summarize counts without exposing row-level source values."""
        return (
            "CatalogReconciliation("
            f"left_source_id={self.left_source_id!r}, "
            f"right_source_id={self.right_source_id!r}, "
            f"exchange_count={len(self.exchange_comparisons)}, "
            f"market_completeness={self.market_completeness!r}, "
            "values_redacted=True)"
        )


@dataclass(slots=True)
class _FieldConflictBudget:
    """Global per-reconciliation work and output bounds for duplicate rows."""

    remaining_pairs: int = _MAX_FIELD_ROW_PAIR_COMPARISONS
    remaining_conflicts: int = _MAX_FIELD_CONFLICT_OUTPUT


def project_sse_capture(
    capture: SSEInstrumentCensusCapture,
    *,
    field_bindings: CatalogFieldBindings,
) -> CatalogProjection:
    """Project explicit SSE raw-field bindings into exchange-qualified candidates.

    The SSE selector supplies a caller-declared Shanghai query context. The
    actual code/name fields are never guessed: callers must pass a mapping
    reviewed against an observed source schema. Status3 selectors remain
    separate candidate context and are not labeled active or delisted.
    """
    if not isinstance(capture, SSEInstrumentCensusCapture):
        raise InstrumentCatalogReconciliationError("sse_capture_invalid")
    if not isinstance(field_bindings, CatalogFieldBindings):
        raise InstrumentCatalogReconciliationError("sse_field_bindings_required")
    candidates: list[CatalogCandidate] = []
    contexts: list[str] = []
    current_context_seen = False
    for page in capture.pages:
        selector = SSEInstrumentSelector(page.selector)
        current_context = selector in {
            SSEInstrumentSelector.CURRENT_MAIN_A,
            SSEInstrumentSelector.CURRENT_STAR,
        }
        current_context_seen = current_context_seen or current_context
        context = (
            CandidateMembershipContext.CURRENT_QUERY
            if current_context
            else CandidateMembershipContext.STATUS3_QUERY_CANDIDATE
        )
        contexts.append(selector.value)
        for row_index, source_row in enumerate(page.source_rows, start=1):
            candidates.append(
                _build_candidate(
                    source_id=capture.source_id,
                    observed_at=capture.observed_at,
                    page_sequence=page.sequence_number,
                    source_page_number=_source_page_number(page.request_summary),
                    source_row_number=row_index,
                    selector_context=selector.value,
                    membership_context=context,
                    request_summary_sha256=page.request_summary_sha256,
                    response_sha256=page.response_sha256,
                    raw_row=source_row.raw_row,
                    exchange_candidate="SH",
                    bindings={
                        "code": field_bindings.code_field,
                        "display_name": field_bindings.name_field,
                        "source_exchange": field_bindings.exchange_field,
                        "asset_type": field_bindings.asset_type_field,
                        "source_status": field_bindings.status_field,
                        "listing_date": field_bindings.listing_date_field,
                        "last_trading_date": field_bindings.last_trading_date_field,
                        "termination_date": field_bindings.termination_date_field,
                    },
                    identity_code_field="code",
                    exchange_field="source_exchange",
                    required_asset_type=None,
                    source_exchange_expected="SH",
                )
            )
    return CatalogProjection(
        source_id=capture.source_id,
        observed_at=capture.observed_at,
        declared_exchange_scope=("SH",),
        current_query_scope=("SH",) if current_context_seen else (),
        acquisition_status=CensusAcquisitionStatus.SUCCEEDED
        if capture.pages
        else CensusAcquisitionStatus.FAILED,
        pagination_status=capture.pagination_status,
        source_authenticity=capture.source_authenticity,
        market_completeness="unknown",
        candidates=tuple(candidates),
        contexts_seen=tuple(contexts),
    )


def project_fuyao_capture(
    capture: FuyaoInstrumentCensusCapture,
    *,
    declared_asset_type: str,
    declared_exchange_scope: Iterable[str] = ("SH", "SZ", "BJ"),
    field_bindings: FuyaoInstrumentFieldBindings = FuyaoInstrumentFieldBindings(),
) -> CatalogProjection:
    """Project captured Fuyao A-share rows into qualified candidates.

    ``declared_asset_type`` and exchange scope are caller context, not proof of
    what the remote service received. Each row still must carry exact
    ``asset_type=a-share`` and a strict qualified ``thscode`` before a candidate
    key is emitted. Provider status and dates remain raw field observations.
    """
    if not isinstance(capture, FuyaoInstrumentCensusCapture):
        raise InstrumentCatalogReconciliationError("fuyao_capture_invalid")
    if declared_asset_type != "a-share":
        raise InstrumentCatalogReconciliationError("fuyao_asset_type_scope_invalid")
    if not isinstance(field_bindings, FuyaoInstrumentFieldBindings):
        raise InstrumentCatalogReconciliationError("fuyao_field_bindings_invalid")
    exchange_scope = _exchange_tuple(declared_exchange_scope, "declared_exchange_scope")
    candidates: list[CatalogCandidate] = []
    failed_pages = False
    succeeded_pages = False
    contexts: list[str] = []
    for page in capture.source_census.pages:
        status = PageAttemptStatus(page.attempt_status)
        contexts.append(f"page:{page.sequence_number}:{status.value}")
        if status is not PageAttemptStatus.SUCCEEDED:
            failed_pages = True
            continue
        succeeded_pages = True
        response_sha256 = (
            page.receipt.response_sha256
            if page.receipt is not None and page.receipt.response_sha256 is not None
            else capture.response_body_sha256s[page.sequence_number - 1]
        )
        if response_sha256 is None or not _is_sha256(response_sha256):
            raise InstrumentCatalogReconciliationError("successful_page_response_hash_missing")
        request_sha256 = capture.request_summary_sha256s[page.sequence_number - 1]
        for row_index, source_observation in enumerate(page.rows, start=1):
            raw_row = _freeze_raw_row(source_observation.raw_row)
            qualified = _observed_field(raw_row, field_bindings.code_field)
            parsed = (
                _QUALIFIED_FUYAO_CODE_PATTERN.fullmatch(qualified.raw_value)
                if qualified.state is FieldObservationState.VALUE
                and type(qualified.raw_value) is str
                else None
            )
            exchange = parsed.group("exchange") if parsed is not None else None
            row_asset_type = _observed_field(raw_row, field_bindings.asset_type_field)
            declared_exchange = _observed_field(raw_row, field_bindings.exchange_field)
            reason: str | None = None
            disposition = CatalogIdentityDisposition.CANDIDATE
            if (
                row_asset_type.state is not FieldObservationState.VALUE
                or row_asset_type.raw_value != "a-share"
            ):
                disposition = CatalogIdentityDisposition.UNRESOLVED
                reason = "row_asset_type_not_verified"
            elif parsed is None:
                disposition = CatalogIdentityDisposition.UNRESOLVED
                reason = "qualified_code_unavailable_or_invalid"
            elif exchange not in exchange_scope:
                disposition = CatalogIdentityDisposition.CONFLICT
                reason = "code_exchange_outside_declared_scope"
            elif (
                declared_exchange.state is FieldObservationState.VALUE
                and declared_exchange.raw_value != exchange
            ):
                disposition = CatalogIdentityDisposition.CONFLICT
                reason = "exchange_field_disagrees_with_qualified_code"
            identity = (
                InstrumentIdentity(exchange=exchange, symbol=parsed.group("symbol"))
                if disposition is CatalogIdentityDisposition.CANDIDATE
                and exchange is not None
                and parsed is not None
                else None
            )
            candidates.append(
                CatalogCandidate(
                    source_id=capture.source_census.source_id,
                    observed_at=capture.source_census.observed_at,
                    page_sequence=page.sequence_number,
                    source_page_number=page.source_page_number,
                    source_row_number=row_index,
                    selector_context="fuyao_a_share_catalog",
                    membership_context=CandidateMembershipContext.CURRENT_QUERY,
                    request_summary_sha256=request_sha256,
                    response_sha256=response_sha256,
                    disposition=disposition,
                    exchange_candidate=exchange,
                    source_code=qualified,
                    identity_candidate=identity,
                    fields={
                        "display_name": _observed_field(raw_row, field_bindings.name_field),
                        "source_exchange": declared_exchange,
                        "asset_type": row_asset_type,
                        "source_status": _observed_field(raw_row, field_bindings.status_field),
                        "listing_date": _observed_field(raw_row, field_bindings.listing_date_field),
                    },
                    raw_row=raw_row,
                    unresolved_reason=reason,
                )
            )
    acquisition = (
        CensusAcquisitionStatus.FAILED
        if not succeeded_pages
        else CensusAcquisitionStatus.PARTIAL
        if failed_pages
        else CensusAcquisitionStatus.SUCCEEDED
    )
    return CatalogProjection(
        source_id=capture.source_census.source_id,
        observed_at=capture.source_census.observed_at,
        declared_exchange_scope=exchange_scope,
        current_query_scope=exchange_scope,
        acquisition_status=acquisition,
        pagination_status=capture.pagination.chain_status.value,
        source_authenticity="unknown",
        market_completeness="unknown",
        candidates=tuple(candidates),
        contexts_seen=tuple(contexts),
    )


def reconcile_instrument_candidates(
    left: CatalogProjection,
    right: CatalogProjection,
) -> CatalogReconciliation:
    """Compare candidate identity sets and report, rather than repair, conflicts.

    Only exchanges in the intersection of each projection's current query
    scope are compared. Set differences are observations; they do not establish
    which provider is authoritative or whether either set is complete.
    """
    if not isinstance(left, CatalogProjection) or not isinstance(right, CatalogProjection):
        raise InstrumentCatalogReconciliationError("projections_required")
    if left.source_id == right.source_id:
        raise InstrumentCatalogReconciliationError("distinct_sources_required")
    _validate_projection_candidates(left)
    _validate_projection_candidates(right)
    compared = tuple(sorted(set(left.current_query_scope) & set(right.current_query_scope)))
    all_scopes = set(left.declared_exchange_scope) | set(right.declared_exchange_scope)
    comparisons: list[ExchangeCatalogComparison] = []
    conflict_budget = _FieldConflictBudget(
        remaining_pairs=_MAX_FIELD_ROW_PAIR_COMPARISONS,
        remaining_conflicts=_MAX_FIELD_CONFLICT_OUTPUT,
    )
    for exchange in compared:
        left_rows = _current_identity_rows(left, exchange)
        right_rows = _current_identity_rows(right, exchange)
        left_ids = {
            row.identity_candidate for row in left_rows if row.identity_candidate is not None
        }
        right_ids = {
            row.identity_candidate for row in right_rows if row.identity_candidate is not None
        }
        conflicts, conflict_status, total_pairs, compared_pairs = _field_conflicts(
            left_rows, right_rows, conflict_budget
        )
        comparisons.append(
            ExchangeCatalogComparison(
                exchange=exchange,
                identity_sets=_identity_set_observation(
                    left_ids,
                    right_ids,
                    _difference_unknown_reasons(left, right),
                ),
                left_duplicate_identities=_duplicates(left_rows),
                right_duplicate_identities=_duplicates(right_rows),
                field_conflicts=conflicts,
                field_conflict_status=conflict_status,
                field_conflict_total_pairs=total_pairs,
                field_conflict_pairs_compared=compared_pairs,
            )
        )
    return CatalogReconciliation(
        left_source_id=left.source_id,
        right_source_id=right.source_id,
        exchange_comparisons=tuple(comparisons),
        not_compared_exchanges=tuple(sorted(all_scopes - set(compared))),
        left_unresolved_rows=tuple(
            row
            for row in left.candidates
            if row.disposition is CatalogIdentityDisposition.UNRESOLVED
        ),
        right_unresolved_rows=tuple(
            row
            for row in right.candidates
            if row.disposition is CatalogIdentityDisposition.UNRESOLVED
        ),
        left_identity_conflicts=tuple(
            row for row in left.candidates if row.disposition is CatalogIdentityDisposition.CONFLICT
        ),
        right_identity_conflicts=tuple(
            row
            for row in right.candidates
            if row.disposition is CatalogIdentityDisposition.CONFLICT
        ),
    )


def _build_candidate(
    *,
    source_id: str,
    observed_at: datetime,
    page_sequence: int,
    source_page_number: int | str | None,
    source_row_number: int,
    selector_context: str,
    membership_context: CandidateMembershipContext,
    request_summary_sha256: str | None,
    response_sha256: str,
    raw_row: Mapping[str, FrozenJson],
    exchange_candidate: str,
    bindings: Mapping[str, str | None],
    identity_code_field: str,
    exchange_field: str,
    required_asset_type: str | None,
    source_exchange_expected: str,
) -> CatalogCandidate:
    """Build one SSE row candidate from explicit raw field bindings."""
    fields = {label: _observed_field(raw_row, name) for label, name in bindings.items()}
    code_observation = fields[identity_code_field]
    exchange_observation = fields[exchange_field]
    disposition = CatalogIdentityDisposition.CANDIDATE
    reason: str | None = None
    code_value = code_observation.raw_value
    if (
        code_observation.state is not FieldObservationState.VALUE
        or type(code_value) is not str
        or _SYMBOL_PATTERN.fullmatch(code_value) is None
    ):
        disposition = CatalogIdentityDisposition.UNRESOLVED
        reason = "mapped_code_missing_or_not_six_ascii_digits"
    elif (
        exchange_observation.state is FieldObservationState.VALUE
        and exchange_observation.raw_value != source_exchange_expected
    ):
        disposition = CatalogIdentityDisposition.CONFLICT
        reason = "exchange_field_disagrees_with_selector_context"
    elif required_asset_type is not None:
        asset_type = fields.get("asset_type", _unmapped_field())
        if (
            asset_type.state is not FieldObservationState.VALUE
            or asset_type.raw_value != required_asset_type
        ):
            disposition = CatalogIdentityDisposition.UNRESOLVED
            reason = "row_asset_type_not_verified"
    identity = (
        InstrumentIdentity(exchange=exchange_candidate, symbol=code_value)
        if disposition is CatalogIdentityDisposition.CANDIDATE and type(code_value) is str
        else None
    )
    return CatalogCandidate(
        source_id=source_id,
        observed_at=observed_at,
        page_sequence=page_sequence,
        source_page_number=source_page_number,
        source_row_number=source_row_number,
        selector_context=selector_context,
        membership_context=membership_context,
        request_summary_sha256=request_summary_sha256,
        response_sha256=response_sha256,
        disposition=disposition,
        exchange_candidate=exchange_candidate,
        source_code=code_observation,
        identity_candidate=identity,
        fields=fields,
        raw_row=raw_row,
        unresolved_reason=reason,
    )


def _observed_field(
    raw_row: Mapping[str, FrozenJson], source_field: str | None
) -> CandidateFieldObservation:
    """Represent a selected raw field as UNMAPPED, ABSENT, NULL, or VALUE."""
    if source_field is None:
        return _unmapped_field()
    if source_field not in raw_row:
        return CandidateFieldObservation(source_field, FieldObservationState.ABSENT)
    value = raw_row[source_field]
    if value is None:
        return CandidateFieldObservation(source_field, FieldObservationState.NULL)
    return CandidateFieldObservation(source_field, FieldObservationState.VALUE, value)


def _validate_projection_candidates(projection: CatalogProjection) -> None:
    """Recheck public candidate DTO invariants at the reconciliation boundary."""
    for candidate in projection.candidates:
        if not isinstance(candidate, CatalogCandidate):
            raise InstrumentCatalogReconciliationError("candidates_invalid")
        if (
            candidate.source_id != projection.source_id
            or candidate.observed_at != projection.observed_at
        ):
            raise InstrumentCatalogReconciliationError("candidate_projection_mismatch")
        _validate_candidate_evidence(candidate)
        _validate_candidate_scope(
            candidate, projection.declared_exchange_scope, projection.current_query_scope
        )


def _validate_candidate_evidence(candidate: CatalogCandidate) -> None:
    """Reject row observations or identity keys that disagree with retained raw evidence."""
    if not isinstance(candidate.source_code, CandidateFieldObservation):
        raise InstrumentCatalogReconciliationError("source_code_observation_invalid")
    if not isinstance(candidate.fields, Mapping):
        raise InstrumentCatalogReconciliationError("candidate_fields_invalid")
    if len(candidate.fields) > _MAX_FIELDS or any(
        type(label) is not str or _FIELD_NAME_PATTERN.fullmatch(label) is None
        for label in candidate.fields
    ):
        raise InstrumentCatalogReconciliationError("candidate_field_key_invalid")
    if not isinstance(candidate.raw_row, Mapping) or not candidate.raw_row:
        raise InstrumentCatalogReconciliationError("raw_row_invalid")
    try:
        disposition = CatalogIdentityDisposition(candidate.disposition)
        membership = CandidateMembershipContext(candidate.membership_context)
    except (TypeError, ValueError):
        raise InstrumentCatalogReconciliationError("candidate_context_invalid") from None
    raw_row = _freeze_raw_row(candidate.raw_row)
    if not _observation_matches_row(candidate.source_code, raw_row):
        raise InstrumentCatalogReconciliationError("source_code_raw_row_mismatch")
    for observation in candidate.fields.values():
        if not isinstance(observation, CandidateFieldObservation):
            raise InstrumentCatalogReconciliationError("candidate_field_value_invalid")
        if not _observation_matches_row(observation, raw_row):
            raise InstrumentCatalogReconciliationError("field_observation_raw_row_mismatch")
    code_field = candidate.fields.get("code")
    if code_field is not None and not _observations_equal(candidate.source_code, code_field):
        raise InstrumentCatalogReconciliationError("source_code_field_observation_mismatch")
    identity = candidate.identity_candidate
    if identity is None:
        if disposition is CatalogIdentityDisposition.CANDIDATE:
            raise InstrumentCatalogReconciliationError("candidate_identity_required")
        if (
            disposition
            in {
                CatalogIdentityDisposition.UNRESOLVED,
                CatalogIdentityDisposition.CONFLICT,
            }
            and not candidate.unresolved_reason
        ):
            raise InstrumentCatalogReconciliationError("unresolved_reason_required")
        return
    if not isinstance(identity, InstrumentIdentity):
        raise InstrumentCatalogReconciliationError("identity_candidate_invalid")
    if disposition is not CatalogIdentityDisposition.CANDIDATE:
        raise InstrumentCatalogReconciliationError("noncandidate_has_identity")
    if membership is not candidate.membership_context:
        raise InstrumentCatalogReconciliationError("candidate_context_invalid")
    source_code = candidate.source_code.raw_value
    if (
        candidate.source_code.state is not FieldObservationState.VALUE
        or type(source_code) is not str
    ):
        raise InstrumentCatalogReconciliationError("identity_source_code_not_value")
    if source_code not in (identity.symbol, f"{identity.symbol}.{identity.exchange}"):
        raise InstrumentCatalogReconciliationError("identity_source_code_mismatch")
    if candidate.exchange_candidate != identity.exchange:
        raise InstrumentCatalogReconciliationError("identity_exchange_mismatch")
    exchange_field = candidate.fields.get("source_exchange")
    if (
        exchange_field is not None
        and exchange_field.state is FieldObservationState.VALUE
        and exchange_field.raw_value != identity.exchange
    ):
        raise InstrumentCatalogReconciliationError("identity_source_exchange_mismatch")
    _validate_identity_source_binding(candidate, identity, source_code)


def _validate_identity_source_binding(
    candidate: CatalogCandidate, identity: InstrumentIdentity, source_code: str
) -> None:
    """Require an exchange field or a recognized source-specific identity contract."""
    if candidate.source_id == "sse.instrument":
        if type(candidate.selector_context) is not str:
            raise InstrumentCatalogReconciliationError("sse_selector_context_invalid")
        try:
            selector = SSEInstrumentSelector(candidate.selector_context)
        except (TypeError, ValueError):
            raise InstrumentCatalogReconciliationError("sse_selector_context_invalid") from None
        current_selectors = {
            SSEInstrumentSelector.CURRENT_MAIN_A,
            SSEInstrumentSelector.CURRENT_STAR,
        }
        status3_selectors = {
            SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
            SSEInstrumentSelector.STATUS3_STAR_CANDIDATE,
        }
        valid_selectors = current_selectors | status3_selectors
        expected_membership = (
            CandidateMembershipContext.CURRENT_QUERY
            if selector in current_selectors
            else CandidateMembershipContext.STATUS3_QUERY_CANDIDATE
        )
        if (
            selector not in valid_selectors
            or identity.exchange != "SH"
            or candidate.exchange_candidate != "SH"
            or source_code != identity.symbol
            or candidate.membership_context is not expected_membership
        ):
            raise InstrumentCatalogReconciliationError("sse_selector_identity_mismatch")
        return
    if candidate.source_id == "fuyao.instrument":
        asset_type = candidate.fields.get("asset_type")
        if (
            candidate.selector_context != "fuyao_a_share_catalog"
            or source_code != f"{identity.symbol}.{identity.exchange}"
            or candidate.membership_context is not CandidateMembershipContext.CURRENT_QUERY
            or asset_type is None
            or asset_type.state is not FieldObservationState.VALUE
            or asset_type.raw_value != "a-share"
        ):
            raise InstrumentCatalogReconciliationError("fuyao_a_share_identity_contract_mismatch")
        return
    if source_code == identity.symbol:
        source_exchange = candidate.fields.get("source_exchange")
        if (
            source_exchange is None
            or source_exchange.state is not FieldObservationState.VALUE
            or source_exchange.raw_value != identity.exchange
        ):
            raise InstrumentCatalogReconciliationError("bare_code_exchange_evidence_required")


def _validate_candidate_scope(
    candidate: CatalogCandidate,
    declared_exchange_scope: tuple[str, ...],
    current_query_scope: tuple[str, ...],
) -> None:
    """Ensure every emitted identity belongs to the projection's query scope."""
    identity = candidate.identity_candidate
    if identity is None:
        return
    if identity.exchange not in declared_exchange_scope:
        raise InstrumentCatalogReconciliationError("candidate_identity_outside_declared_scope")
    if (
        candidate.membership_context is CandidateMembershipContext.CURRENT_QUERY
        and identity.exchange not in current_query_scope
    ):
        raise InstrumentCatalogReconciliationError("candidate_identity_outside_current_scope")


def _observation_matches_row(
    observation: CandidateFieldObservation, raw_row: Mapping[str, FrozenJson]
) -> bool:
    """Check whether an observation exactly represents its field in one raw row."""
    if observation.state is FieldObservationState.UNMAPPED:
        return observation.source_field is None and observation.raw_value is None
    field_name = observation.source_field
    if field_name is None:
        return False
    if observation.state is FieldObservationState.ABSENT:
        return field_name not in raw_row
    if observation.state is FieldObservationState.NULL:
        return field_name in raw_row and raw_row[field_name] is None
    return (
        observation.state is FieldObservationState.VALUE
        and field_name in raw_row
        and _same_json(observation.raw_value, raw_row[field_name])
    )


def _observations_equal(left: CandidateFieldObservation, right: CandidateFieldObservation) -> bool:
    """Compare field observations including source labels and absent/null state."""
    return (
        left.source_field == right.source_field
        and left.state is right.state
        and _same_json(left.raw_value, right.raw_value)
    )


def _unmapped_field() -> CandidateFieldObservation:
    """Construct an explicit unbound field observation."""
    return CandidateFieldObservation(None, FieldObservationState.UNMAPPED)


def _current_identity_rows(
    projection: CatalogProjection, exchange: str
) -> tuple[CatalogCandidate, ...]:
    """Select usable current-query rows for one exchange without deduplicating."""
    return tuple(
        row
        for row in projection.candidates
        if row.membership_context is CandidateMembershipContext.CURRENT_QUERY
        and row.disposition is CatalogIdentityDisposition.CANDIDATE
        and row.identity_candidate is not None
        and row.identity_candidate.exchange == exchange
    )


def _duplicates(rows: tuple[CatalogCandidate, ...]) -> tuple[InstrumentIdentity, ...]:
    """Return candidate identities repeated within a single projected source."""
    counts: dict[InstrumentIdentity, int] = defaultdict(int)
    for row in rows:
        if row.identity_candidate is not None:
            counts[row.identity_candidate] += 1
    return tuple(sorted(identity for identity, count in counts.items() if count > 1))


def _difference_unknown_reasons(
    left: CatalogProjection, right: CatalogProjection
) -> tuple[str, ...]:
    """Require successful acquisition and an evidenced page chain for a diff."""
    reasons: list[str] = []
    for side, projection in (("left", left), ("right", right)):
        if projection.acquisition_status is not CensusAcquisitionStatus.SUCCEEDED:
            reasons.append(f"{side}_acquisition_not_succeeded")
        if projection.pagination_status.lower() != "chain_consistent":
            reasons.append(f"{side}_pagination_not_chain_consistent")
    return tuple(reasons)


def _identity_set_observation(
    left_identities: set[InstrumentIdentity],
    right_identities: set[InstrumentIdentity],
    unknown_reasons: tuple[str, ...],
) -> CatalogIdentitySetObservation:
    """Partition only identities actually seen and label incomplete evidence."""
    status = (
        CatalogSetDifferenceStatus.UNKNOWN
        if unknown_reasons
        else CatalogSetDifferenceStatus.OBSERVED
    )
    return CatalogIdentitySetObservation(
        difference_status=status,
        left_observed_identities=tuple(sorted(left_identities)),
        right_observed_identities=tuple(sorted(right_identities)),
        left_only_observed=tuple(sorted(left_identities - right_identities)),
        right_only_observed=tuple(sorted(right_identities - left_identities)),
        shared_observed=tuple(sorted(left_identities & right_identities)),
        unknown_reasons=unknown_reasons,
    )


def _field_conflicts(
    left_rows: tuple[CatalogCandidate, ...],
    right_rows: tuple[CatalogCandidate, ...],
    budget: _FieldConflictBudget,
) -> tuple[
    tuple[CatalogSourceConflict, ...],
    CatalogFieldConflictStatus,
    int,
    int,
]:
    """Compare bounded row pairs and explicitly report any truncated review."""
    left_by_identity = _candidates_by_identity(left_rows)
    right_by_identity = _candidates_by_identity(right_rows)
    shared_identities = left_by_identity.keys() & right_by_identity.keys()
    total_pairs = sum(
        len(left_by_identity[identity]) * len(right_by_identity[identity])
        for identity in shared_identities
    )
    conflicts: list[CatalogSourceConflict] = []
    compared_pairs = 0
    stopped = False
    for identity in sorted(shared_identities):
        left_matches = left_by_identity[identity]
        right_matches = right_by_identity[identity]
        ambiguous = len(left_matches) > 1 or len(right_matches) > 1
        for left in left_matches:
            for right in right_matches:
                if budget.remaining_pairs <= 0 or budget.remaining_conflicts <= 0:
                    stopped = True
                    break
                budget.remaining_pairs -= 1
                compared_pairs += 1
                left_name = left.fields.get("display_name", _unmapped_field())
                right_name = right.fields.get("display_name", _unmapped_field())
                if (
                    left_name.state is FieldObservationState.VALUE
                    and right_name.state is FieldObservationState.VALUE
                    and not _same_json(left_name.raw_value, right_name.raw_value)
                ):
                    conflicts.append(
                        CatalogSourceConflict(
                            identity=identity,
                            field_name="display_name",
                            left_source_id=left.source_id,
                            left_source_field=left_name.source_field,
                            left_page_sequence=left.page_sequence,
                            left_source_row_number=left.source_row_number,
                            right_source_id=right.source_id,
                            right_source_field=right_name.source_field,
                            right_page_sequence=right.page_sequence,
                            right_source_row_number=right.source_row_number,
                            ambiguous_identity=ambiguous,
                            left_identity_row_count=len(left_matches),
                            right_identity_row_count=len(right_matches),
                            left_value=left_name.raw_value,
                            right_value=right_name.raw_value,
                        )
                    )
                    budget.remaining_conflicts -= 1
            if stopped:
                break
        if stopped:
            break
    status = (
        CatalogFieldConflictStatus.COMPLETE
        if compared_pairs == total_pairs
        else CatalogFieldConflictStatus.TRUNCATED
    )
    return tuple(conflicts), status, total_pairs, compared_pairs


def _candidates_by_identity(
    rows: tuple[CatalogCandidate, ...],
) -> dict[InstrumentIdentity, tuple[CatalogCandidate, ...]]:
    """Group all candidate rows while preserving their original order."""
    found: dict[InstrumentIdentity, list[CatalogCandidate]] = defaultdict(list)
    for row in rows:
        if row.identity_candidate is not None:
            found[row.identity_candidate].append(row)
    return {identity: tuple(matches) for identity, matches in found.items()}


def _same_json(left: object, right: object) -> bool:
    """Compare JSON values without treating booleans and integers as equal."""
    if type(left) is not type(right):
        return False
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        return left.keys() == right.keys() and all(
            _same_json(left[key], right[key]) for key in left
        )
    if isinstance(left, tuple) and isinstance(right, tuple):
        return len(left) == len(right) and all(
            _same_json(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _source_page_number(summary: Mapping[str, FrozenJson] | None) -> int | str | None:
    """Read only an explicitly retained page-number parameter from summary."""
    if summary is None:
        return None
    params = summary.get("params", summary)
    if not isinstance(params, Mapping):
        return None
    value = params.get("pageHelp.pageNo")
    return value if type(value) is int or isinstance(value, str) else None


def _exchange_tuple(
    values: Iterable[str], label: str, *, allow_empty: bool = False
) -> tuple[str, ...]:
    """Validate and sort an explicit SH/SZ/BJ scope."""
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable):
        raise InstrumentCatalogReconciliationError(f"{label}_invalid")
    try:
        result = tuple(islice(iter(values), 4))
    except Exception:
        raise InstrumentCatalogReconciliationError(f"{label}_invalid") from None
    if (
        (not result and not allow_empty)
        or len(result) > 3
        or any(type(value) is not str or value not in _EXCHANGES for value in result)
    ):
        raise InstrumentCatalogReconciliationError(f"{label}_invalid")
    if len(result) != len(set(result)):
        raise InstrumentCatalogReconciliationError(f"{label}_duplicate")
    return tuple(sorted(result))


def _bounded_tuple(values: Iterable[object], label: str, limit: int) -> tuple[object, ...]:
    """Materialize one bounded iterable without treating scalar values as rows."""
    if isinstance(values, (str, bytes, bytearray, Mapping)) or not isinstance(values, Iterable):
        raise InstrumentCatalogReconciliationError(f"{label}_invalid")
    try:
        result = tuple(islice(iter(values), limit + 1))
    except Exception:
        raise InstrumentCatalogReconciliationError(f"{label}_invalid") from None
    if len(result) > limit:
        raise InstrumentCatalogReconciliationError(f"{label}_too_large")
    return result


def _freeze_raw_row(row: Mapping[str, object]) -> Mapping[str, FrozenJson]:
    """Copy one bounded source row into recursively immutable JSON values."""
    if not isinstance(row, Mapping) or not row or len(row) > 256:
        raise InstrumentCatalogReconciliationError("raw_row_invalid")
    frozen = _freeze_json_value(row)
    if not isinstance(frozen, Mapping):
        raise InstrumentCatalogReconciliationError("raw_row_invalid")
    return frozen


def _freeze_json_value(
    value: object,
    *,
    depth: int = 0,
    node_budget: list[int] | None = None,
    byte_budget: list[int] | None = None,
) -> FrozenJson:
    """Validate and freeze finite JSON-shaped values within source DTO limits."""
    if node_budget is None:
        node_budget = [0]
    if byte_budget is None:
        byte_budget = [0]
    node_budget[0] += 1
    if node_budget[0] > 20_000 or depth > 12:
        raise InstrumentCatalogReconciliationError("raw_row_value_too_large")
    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        if value.bit_length() > 4_096:
            raise InstrumentCatalogReconciliationError("raw_row_integer_too_large")
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise InstrumentCatalogReconciliationError("raw_row_number_not_finite")
        return value
    if type(value) is str:
        if len(value) > 16_384:
            raise InstrumentCatalogReconciliationError("raw_row_text_too_large")
        byte_budget[0] += len(value.encode("utf-8"))
        if byte_budget[0] > 1_048_576:
            raise InstrumentCatalogReconciliationError("raw_row_value_too_large")
        return value
    if isinstance(value, Mapping):
        if len(value) > 256:
            raise InstrumentCatalogReconciliationError("raw_row_object_too_large")
        frozen: dict[str, FrozenJson] = {}
        for key, item in value.items():
            if type(key) is not str or len(key) > 128:
                raise InstrumentCatalogReconciliationError("raw_row_field_name_invalid")
            byte_budget[0] += len(key.encode("utf-8"))
            if byte_budget[0] > 1_048_576:
                raise InstrumentCatalogReconciliationError("raw_row_value_too_large")
            frozen[key] = _freeze_json_value(
                item,
                depth=depth + 1,
                node_budget=node_budget,
                byte_budget=byte_budget,
            )
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        if len(value) > 4_096:
            raise InstrumentCatalogReconciliationError("raw_row_array_too_large")
        return tuple(
            _freeze_json_value(
                item,
                depth=depth + 1,
                node_budget=node_budget,
                byte_budget=byte_budget,
            )
            for item in value
        )
    raise InstrumentCatalogReconciliationError("raw_row_value_not_json")


def _is_sha256(value: str) -> bool:
    """Validate lowercase SHA-256 syntax without authenticating its source."""
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
