"""Conservative financial-field and period adapter for Sina LRB captures.

The current LRB publication is source-native: it preserves report-date and
report-list nodes, source order, and capture time. It does not validate
financial item semantics, native units, currency, announcement timestamps, or
revision identity. This module therefore has no built-in field crosswalk and
does not infer a report period from a source key. A field or period can be
mapped only by an explicit, time-bounded caller assertion. The existing
financial-period assessor is used only for exact slots; this adapter never
supplies lifecycle, expected-obligation, due-time, or complete-scan evidence,
so one capture cannot confirm an absent report period.

Evidence references and assertions are shape-checked caller claims, not
independently authenticated evidence. An ``observed`` result means only that
an exact captured period version was supplied under an as-of crosswalk and
clear-isolation assertion. It does not establish item coverage, report
completeness, source-history boundaries, announcement time, revision history,
or point-in-time availability. Unit declarations are never converted.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Final, Literal, TypeVar, cast
from zoneinfo import ZoneInfo

from opendata.pipeline.financial_period_coverage import (
    FinancialPeriodCoverageReport,
    FinancialPeriodCoverageSlotEvidence,
    FinancialPeriodCrosswalkEvidence,
    FinancialPeriodObservationVersionEvidence,
    Periodicity,
    StatementType,
    assess_financial_period_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "SINA_LRB_FINANCIAL_MAPPING_VERSION",
    "SinaLrbFinancialFieldAssessment",
    "SinaLrbFinancialFieldDeclaration",
    "SinaLrbFinancialMappingError",
    "SinaLrbPeriodCoverageAssessment",
    "SinaLrbPeriodObservation",
    "SinaLrbReportPeriodMapping",
    "assess_sina_lrb_financial_fields",
    "assess_sina_lrb_period_coverage",
]

SINA_LRB_FINANCIAL_MAPPING_VERSION: Final[str] = "sina-lrb-financial-mapping-v1"
"""Version of this conservative adapter contract."""

_SOURCE_ID: Final[str] = "sina-lrb"
_BUSINESS_TIMEZONE: Final[ZoneInfo] = ZoneInfo("Asia/Shanghai")
_MAX_FIELD_ROWS: Final[int] = 4_096
_MAX_PERIOD_OBSERVATIONS: Final[int] = 256
_SOURCE_SYMBOL = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z", re.ASCII)
_CAPTURE_ID = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_ITEM_CODE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)
_PERIOD_KEY = re.compile(r"[0-9]{8}\Z", re.ASCII)
_OPAQUE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:#-]{0,127}\Z", re.ASCII)
_SOURCE_TEXT_MAX: Final[int] = 256
_UTC = timezone.utc
_T = TypeVar("_T")

FieldAssessmentState = Literal[
    "operator_assertion_unverified",
    "unknown",
    "conflict",
]
"""Field mapping status; no state represents independently verified semantics."""

FieldAssessmentReason = Literal[
    "exact_pair_assertion_present",
    "exact_pair_assertion_missing",
    "mapping_evidence_unavailable",
    "mapping_evidence_future_or_undated",
    "conflicting_exact_pair_assertions",
]
"""Sanitized reason for one field assessment."""

PeriodAssessmentReason = Literal[
    "period_semantics_missing",
    "period_key_does_not_match_asserted_period",
    "period_semantics_evidence_unavailable",
    "period_semantics_unsupported",
    "report_period_after_as_of",
    "identity_crosswalk_missing",
    "capture_observation_evidence_missing",
]
"""Sanitized reason for a period observation not passed to coverage assessment."""


class SinaLrbFinancialMappingError(ValueError):
    """Raised for malformed, unsupported, or unbounded adapter input."""

    def __init__(self, code: str) -> None:
        """Initialize a fixed message without echoing source or evidence values."""
        allowed = {"invalid_field_mapping_input", "invalid_period_input", "limit_exceeded"}
        self.code = code if code in allowed else "invalid_field_mapping_input"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class SinaLrbFinancialFieldDeclaration:
    """One exact source-key/title mapping and caller-declared native unit.

    ``evidence_ref`` and ``evidenced_at_utc`` document the caller's assertion;
    this module does not fetch, inspect, or authenticate the referenced item.
    ``native_unit`` must be explicit. A currency, when supplied, is retained as
    a three-letter token without claiming that the source supports it.
    """

    source_item_key: str
    source_item_title: str
    canonical_item_code: str
    native_unit: str
    currency: str | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class SinaLrbFinancialFieldAssessment:
    """Exact-pair assessment with no coerced value or inferred unit."""

    source_item_key: str
    source_item_title: str
    state: FieldAssessmentState
    reason: FieldAssessmentReason
    canonical_item_code: str | None
    native_unit: str | None
    currency: str | None
    mapping_evidence_independently_verified: Literal[False] = False
    value_conversion_performed: Literal[False] = False


@dataclass(frozen=True, slots=True)
class SinaLrbReportPeriodMapping:
    """Caller assertion binding one exact source report key to a period slot.

    The statement family and cadence are explicit because the LRB request code
    alone is not treated as sufficient semantic evidence here. ``source_key``
    must equal the asserted period in ``YYYYMMDD`` form, but that shape check
    does not authenticate the source's meaning.
    """

    source_key: str
    report_period: date
    statement_type: StatementType | str
    periodicity: Periodicity | str
    evidence_ref: str | None
    evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class SinaLrbPeriodObservation:
    """One captured source report with explicit provenance and mapping claims.

    ``capture_id`` is the opaque capture/run identifier. Retrieval time is not
    treated as an announcement time. Crosswalk, capture evidence, and isolation
    assertions remain caller supplied and are not independently verified.
    """

    capture_id: str
    source_instrument_id: str
    source_report_key: str
    retrieved_at_utc: datetime
    capture_evidence_ref: str | None
    report_period_mapping: SinaLrbReportPeriodMapping | None
    crosswalk: FinancialPeriodCrosswalkEvidence | None
    isolation_status: Literal["clear", "isolated", "unknown"]
    isolation_evidence_ref: str | None
    isolation_evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class SinaLrbPeriodCoverageAssessment:
    """Exact-slot presence report plus counts of candidates kept unknown."""

    coverage: FinancialPeriodCoverageReport
    unassessable_observation_count: int
    unassessable_reason_counts: tuple[tuple[PeriodAssessmentReason, int], ...]
    report_completeness_established: Literal[False] = False
    announcement_time_independently_verified: Literal[False] = False
    revision_history_established: Literal[False] = False
    point_in_time_guarantee: Literal[False] = False


def assess_sina_lrb_financial_fields(
    source_fields: Iterable[tuple[str, str]],
    declarations: Iterable[SinaLrbFinancialFieldDeclaration],
    *,
    as_of_utc: datetime,
) -> tuple[SinaLrbFinancialFieldAssessment, ...]:
    """Assess exact source-key/title pairs against explicit unit declarations.

    No title normalization, fuzzy matching, source-family inference, numeric
    parsing, currency conversion, or unit conversion is performed. An exact
    declaration yields only an unverified caller-assertion state. Duplicate
    conflicting declarations fail closed for that pair.

    Args:
        source_fields: Bounded source key/title pairs observed by a caller.
        declarations: Bounded exact-pair mapping assertions.
        as_of_utc: Time at which mapping evidence must already exist.

    Returns:
        One sanitized assessment per supplied source pair, in source order.

    Raises:
        SinaLrbFinancialMappingError: If input is malformed or exceeds bounds.
    """
    as_of = _aware_datetime(as_of_utc)
    source_pairs = _bounded_values(source_fields, _MAX_FIELD_ROWS, "invalid_field_mapping_input")
    supplied_declarations = _bounded_values(
        declarations, _MAX_FIELD_ROWS, "invalid_field_mapping_input"
    )
    _validate_source_pairs(source_pairs)
    for declaration in supplied_declarations:
        _validate_field_declaration(declaration)

    by_pair: dict[tuple[str, str], list[SinaLrbFinancialFieldDeclaration]] = {}
    for declaration in supplied_declarations:
        key = (declaration.source_item_key, declaration.source_item_title)
        by_pair.setdefault(key, []).append(declaration)

    assessments: list[SinaLrbFinancialFieldAssessment] = []
    for source_item_key, source_item_title in source_pairs:
        matches = by_pair.get((source_item_key, source_item_title), [])
        if not matches:
            assessments.append(
                SinaLrbFinancialFieldAssessment(
                    source_item_key=source_item_key,
                    source_item_title=source_item_title,
                    state="unknown",
                    reason="exact_pair_assertion_missing",
                    canonical_item_code=None,
                    native_unit=None,
                    currency=None,
                )
            )
            continue

        # Future and undated declarations cannot establish the mapping at this
        # assessment instant, and must not create a historical conflict.
        available = [
            item
            for item in matches
            if _valid_reference(item.evidence_ref)
            and item.evidenced_at_utc is not None
            and _aware_datetime(item.evidenced_at_utc) <= as_of
        ]
        unique = {_field_declaration_signature(item) for item in available}
        if len(unique) > 1:
            assessments.append(
                SinaLrbFinancialFieldAssessment(
                    source_item_key=source_item_key,
                    source_item_title=source_item_title,
                    state="conflict",
                    reason="conflicting_exact_pair_assertions",
                    canonical_item_code=None,
                    native_unit=None,
                    currency=None,
                )
            )
            continue
        if not available:
            has_future_evidence = any(
                _valid_reference(item.evidence_ref)
                and item.evidenced_at_utc is not None
                and _aware_datetime(item.evidenced_at_utc) > as_of
                for item in matches
            )
            assessments.append(
                SinaLrbFinancialFieldAssessment(
                    source_item_key=source_item_key,
                    source_item_title=source_item_title,
                    state="unknown",
                    reason=(
                        "mapping_evidence_future_or_undated"
                        if has_future_evidence
                        else "mapping_evidence_unavailable"
                    ),
                    canonical_item_code=None,
                    native_unit=None,
                    currency=None,
                )
            )
            continue
        declaration = available[0]
        assessments.append(
            SinaLrbFinancialFieldAssessment(
                source_item_key=source_item_key,
                source_item_title=source_item_title,
                state="operator_assertion_unverified",
                reason="exact_pair_assertion_present",
                canonical_item_code=declaration.canonical_item_code,
                native_unit=declaration.native_unit,
                currency=declaration.currency,
            )
        )
    return tuple(assessments)


def assess_sina_lrb_period_coverage(
    observations: Iterable[SinaLrbPeriodObservation], *, as_of_utc: datetime
) -> SinaLrbPeriodCoverageAssessment:
    """Assess only exact, explicitly mapped LRB period observations.

    Candidates without period semantics, identity crosswalk, or capture
    evidence remain in an unknown count. A report period after the as-of
    calendar date in ``Asia/Shanghai`` is also kept unassessable. The wrapped
    generic assessor receives no lifecycle, obligation, due, or scan evidence.
    Consequently this adapter can report an accepted observation, but cannot
    conclude a period is absent.

    Args:
        observations: Bounded LRB capture/report observations.
        as_of_utc: Assessment instant; future observations/evidence are excluded.

    Returns:
        Sanitized per-slot coverage plus unknown candidate counts. The generic
        report itself also states that evidence references are not verified and
        PIT guarantees are unavailable.

    Raises:
        SinaLrbFinancialMappingError: If input is malformed or exceeds bounds.
    """
    as_of = _aware_datetime(as_of_utc)
    candidates = _bounded_values(observations, _MAX_PERIOD_OBSERVATIONS, "invalid_period_input")
    slots: list[FinancialPeriodCoverageSlotEvidence] = []
    unassessable: Counter[PeriodAssessmentReason] = Counter()
    for candidate in candidates:
        _validate_period_observation(candidate)
        mapping = candidate.report_period_mapping
        if mapping is None:
            unassessable["period_semantics_missing"] += 1
            continue
        if (
            not _valid_reference(mapping.evidence_ref)
            or mapping.evidenced_at_utc is None
            or _aware_datetime(mapping.evidenced_at_utc) > as_of
        ):
            unassessable["period_semantics_evidence_unavailable"] += 1
            continue
        if mapping.report_period > as_of.astimezone(_BUSINESS_TIMEZONE).date():
            unassessable["report_period_after_as_of"] += 1
            continue
        if candidate.source_report_key != mapping.source_key or (
            _PERIOD_KEY.fullmatch(candidate.source_report_key) is None
            or candidate.source_report_key != mapping.report_period.strftime("%Y%m%d")
        ):
            unassessable["period_key_does_not_match_asserted_period"] += 1
            continue
        statement_type = _enum_value(mapping.statement_type)
        periodicity = _enum_value(mapping.periodicity)
        if statement_type not in {item.value for item in StatementType} or periodicity not in {
            item.value for item in Periodicity
        }:
            unassessable["period_semantics_unsupported"] += 1
            continue
        if candidate.crosswalk is None:
            unassessable["identity_crosswalk_missing"] += 1
            continue
        if (
            not _valid_reference(candidate.capture_evidence_ref)
            or _aware_datetime(candidate.retrieved_at_utc) > as_of
        ):
            unassessable["capture_observation_evidence_missing"] += 1
            continue

        version_id = _version_id(candidate.capture_id, candidate.source_report_key)
        version = FinancialPeriodObservationVersionEvidence(
            version_id=version_id,
            source_id=_SOURCE_ID,
            source_instrument_id=candidate.source_instrument_id,
            security_id=candidate.crosswalk.security_id,
            statement_type=mapping.statement_type,
            periodicity=mapping.periodicity,
            report_period=mapping.report_period,
            observed_at_utc=candidate.retrieved_at_utc,
            evidence_ref=candidate.capture_evidence_ref,
            evidenced_at_utc=candidate.retrieved_at_utc,
            isolation_status=candidate.isolation_status,
            isolation_evidence_ref=candidate.isolation_evidence_ref,
            isolation_evidenced_at_utc=candidate.isolation_evidenced_at_utc,
        )
        slots.append(
            FinancialPeriodCoverageSlotEvidence(
                source_id=_SOURCE_ID,
                source_instrument_id=candidate.source_instrument_id,
                security_id=candidate.crosswalk.security_id,
                statement_type=mapping.statement_type,
                periodicity=mapping.periodicity,
                report_period=mapping.report_period,
                crosswalk=candidate.crosswalk,
                lifecycle=None,
                obligation=None,
                due=None,
                scan=None,
                versions=(version,),
            )
        )
    coverage = assess_financial_period_coverage(slots, as_of_utc=as_of)
    return SinaLrbPeriodCoverageAssessment(
        coverage=coverage,
        unassessable_observation_count=sum(unassessable.values()),
        unassessable_reason_counts=tuple(sorted(unassessable.items())),
    )


def _validate_source_pairs(source_pairs: tuple[object, ...]) -> None:
    """Validate bounded exact source-key/title pairs without normalizing them."""
    for pair in source_pairs:
        if (
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not _bounded_text(pair[0])
            or not _bounded_text(pair[1])
        ):
            raise SinaLrbFinancialMappingError("invalid_field_mapping_input")


def _validate_field_declaration(declaration: object) -> None:
    """Validate the shape of a caller assertion, not its factual accuracy."""
    if (
        not isinstance(declaration, SinaLrbFinancialFieldDeclaration)
        or not _bounded_text(declaration.source_item_key)
        or not _bounded_text(declaration.source_item_title)
        or type(declaration.canonical_item_code) is not str
        or _ITEM_CODE.fullmatch(declaration.canonical_item_code) is None
        or not _bounded_text(declaration.native_unit)
        or (
            declaration.currency is not None
            and (
                type(declaration.currency) is not str
                or _CURRENCY.fullmatch(declaration.currency) is None
            )
        )
        or (declaration.evidence_ref is not None and not _valid_reference(declaration.evidence_ref))
        or (
            declaration.evidenced_at_utc is not None
            and not _is_aware_datetime(declaration.evidenced_at_utc)
        )
    ):
        raise SinaLrbFinancialMappingError("invalid_field_mapping_input")


def _validate_period_observation(observation: object) -> None:
    """Validate source identity and timestamps without asserting semantics."""
    if (
        not isinstance(observation, SinaLrbPeriodObservation)
        or type(observation.capture_id) is not str
        or _CAPTURE_ID.fullmatch(observation.capture_id) is None
        or type(observation.source_instrument_id) is not str
        or _SOURCE_SYMBOL.fullmatch(observation.source_instrument_id) is None
        or type(observation.source_report_key) is not str
        or len(observation.source_report_key) > 32
        or not _is_aware_datetime(observation.retrieved_at_utc)
        or observation.isolation_status not in {"clear", "isolated", "unknown"}
        or (
            observation.isolation_evidence_ref is not None
            and not _valid_reference(observation.isolation_evidence_ref)
        )
        or (
            observation.isolation_evidenced_at_utc is not None
            and not _is_aware_datetime(observation.isolation_evidenced_at_utc)
        )
    ):
        raise SinaLrbFinancialMappingError("invalid_period_input")
    mapping = observation.report_period_mapping
    if mapping is not None and (
        not isinstance(mapping, SinaLrbReportPeriodMapping)
        or isinstance(mapping.report_period, datetime)
        or not isinstance(mapping.report_period, date)
        or (
            mapping.evidenced_at_utc is not None
            and not _is_aware_datetime(mapping.evidenced_at_utc)
        )
        or type(mapping.source_key) is not str
        or len(mapping.source_key) > 32
    ):
        raise SinaLrbFinancialMappingError("invalid_period_input")
    crosswalk = observation.crosswalk
    if crosswalk is not None and (
        not isinstance(crosswalk, FinancialPeriodCrosswalkEvidence)
        or not _bounded_text(crosswalk.source_id)
        or not _bounded_text(crosswalk.source_instrument_id)
        or not _bounded_text(crosswalk.security_id)
        or isinstance(crosswalk.valid_from, datetime)
        or not isinstance(crosswalk.valid_from, date)
        or (
            crosswalk.valid_through is not None
            and (
                isinstance(crosswalk.valid_through, datetime)
                or not isinstance(crosswalk.valid_through, date)
            )
        )
        or (crosswalk.evidence_ref is not None and not _valid_reference(crosswalk.evidence_ref))
        or (
            crosswalk.evidenced_at_utc is not None
            and not _is_aware_datetime(crosswalk.evidenced_at_utc)
        )
        or (crosswalk.valid_through is not None and crosswalk.valid_through < crosswalk.valid_from)
    ):
        raise SinaLrbFinancialMappingError("invalid_period_input")


def _field_declaration_signature(
    declaration: SinaLrbFinancialFieldDeclaration,
) -> tuple[str, str, str, str, str | None, str | None, datetime | None]:
    """Return the exact semantic signature used only for conflict detection."""
    return (
        declaration.source_item_key,
        declaration.source_item_title,
        declaration.canonical_item_code,
        declaration.native_unit,
        declaration.currency,
        declaration.evidence_ref,
        (
            _aware_datetime(declaration.evidenced_at_utc)
            if declaration.evidenced_at_utc is not None
            else None
        ),
    )


def _bounded_values(values: Iterable[_T], limit: int, error_code: str) -> tuple[_T, ...]:
    """Materialize at most ``limit`` objects and reject scalar impostors."""
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise SinaLrbFinancialMappingError(error_code)
    try:
        iterator = iter(values)
    except Exception:
        raise SinaLrbFinancialMappingError(error_code) from None
    materialized: list[_T] = []
    while True:
        try:
            value = next(iterator)
        except StopIteration:
            break
        except Exception:
            raise SinaLrbFinancialMappingError(error_code) from None
        if len(materialized) >= limit:
            raise SinaLrbFinancialMappingError("limit_exceeded")
        materialized.append(value)
    return tuple(materialized)


def _version_id(capture_id: str, source_report_key: str) -> str:
    """Derive a bounded per-capture/per-report pseudonymous version key."""
    material = f"{capture_id}\n{source_report_key}".encode("ascii", errors="strict")
    return hashlib.sha256(material).hexdigest()


def _bounded_text(value: object) -> bool:
    """Check non-empty bounded text without changing source bytes or spelling."""
    return (
        type(value) is str
        and 0 < len(value) <= _SOURCE_TEXT_MAX
        and not any(ord(character) < 32 for character in value)
    )


def _valid_reference(value: object) -> bool:
    """Accept only an opaque, bounded identifier, never a URL or file path."""
    return type(value) is str and _OPAQUE_REFERENCE.fullmatch(value) is not None


def _is_aware_datetime(value: object) -> bool:
    """Return whether ``value`` has a usable timezone offset."""
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def _aware_datetime(value: datetime) -> datetime:
    """Normalize a prevalidated aware timestamp to UTC."""
    if not _is_aware_datetime(value):
        raise SinaLrbFinancialMappingError("invalid_period_input")
    return value.astimezone(_UTC)


def _enum_value(value: StatementType | Periodicity | str) -> str:
    """Read an enum value or exact string without accepting arbitrary coercion."""
    if isinstance(value, (StatementType, Periodicity)):
        return cast("str", value.value)
    return value if type(value) is str else ""
