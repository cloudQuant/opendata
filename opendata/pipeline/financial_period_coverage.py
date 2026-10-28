"""Conservative, evidence-scoped financial reporting-period presence assessment.

This module evaluates only exact period slots explicitly supplied by a caller.
It does not discover securities, derive due periods, read evidence references,
query a provider or database, or certify report completeness or point-in-time
(PIT) history. Evidence references and scan assertions are caller claims; this
module checks their shape and timestamps but does not authenticate them.
Returned SHA-256 fingerprints are stable pseudonyms, not anonymization; low-
entropy or public identifiers may be guessable by dictionary attack.

``observed`` means that at least one non-isolated observation version was
supplied for the exact source, security, statement, cadence, and report period,
under a caller-supplied identity mapping. It means record presence only; it
does not establish issuer or source validity, statement item coverage, mapping
completeness, or revision history. A source disclosure date is not an
observation timestamp; only ``observed_at_utc`` is used as such here.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_SLOTS: Final[int] = 10_000
_MAX_VERSIONS_PER_SLOT: Final[int] = 1_000


class FinancialPeriodCoverageInputError(ValueError):
    """Raised for malformed or unbounded structural input without echoing values."""


class FinancialPeriodCoverageState(str, Enum):
    """Conservative per-slot status; none of these states means report completeness."""

    OBSERVED = "observed"
    MISSING_CONFIRMED = "missing_confirmed"
    UNKNOWN = "unknown"


class StatementType(str, Enum):
    """Supported financial statement families."""

    INCOME = "income"
    BALANCE = "balance"
    CASHFLOW = "cashflow"


class Periodicity(str, Enum):
    """Supported report cadences, kept in separate slots."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"


IsolationStatus = Literal["clear", "isolated", "unknown"]


@dataclass(frozen=True, slots=True)
class FinancialPeriodCrosswalkEvidence:
    """One explicit source-identity to canonical-security mapping assertion.

    The mapping must apply to ``report_period`` and its evidence timestamp must
    be no later than the assessment ``as_of_utc``. A reference is an opaque
    caller identifier, never a file path, URL, secret, or raw provider value.
    """

    source_id: str
    source_instrument_id: str
    security_id: str
    valid_from: date
    valid_through: date | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodLifecycleEvidence:
    """Caller assertion that the issuer reporting lifecycle covers the period.

    This is not a security-listing-date assertion. It is used only as a gate for
    confirming absence; an existing observation can be reported without it.
    """

    covers_report_period: bool | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodDueEvidence:
    """An explicitly sourced deadline, separate from whether a report is due."""

    due_at_utc: datetime | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None
    obligation_evidence_ref: str | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodExpectedObligationEvidence:
    """Caller assertion that this exact source/security period is expected.

    The complete slot key is repeated so a generic deadline or obligation
    cannot be reused to assert another source, instrument, statement, cadence,
    security, or reporting period.
    """

    source_id: str
    source_instrument_id: str
    security_id: str
    statement_type: StatementType | str
    periodicity: Periodicity | str
    report_period: date
    applies: bool | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodScanEvidence:
    """Caller-declared scan facts for one exact source period slot.

    A scan can support a missing claim only when every scope, pagination,
    failure, unresolved-row, isolation, crosswalk, and source-history field is
    evidenced and favorable. ``matched_version_ids`` is the exact inventory of
    returned rows represented by the slot's ``versions``; an empty tuple is an
    explicit zero-row result, while ``None`` means the inventory is unknown.
    These are assertions, not independently verified facts.
    """

    snapshot_at_utc: datetime | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None
    scope_matches_slot: bool | None
    crosswalk_matches_snapshot: bool | None
    cursor_exhausted: bool | None
    failed_page_count: int | None
    unresolved_row_count: int | None
    isolated_row_count: int | None
    ambiguous_crosswalk_count: int | None
    source_history_covers_period: bool | None
    source_history_evidence_ref: str | None
    source_history_evidenced_at_utc: datetime | None
    matched_version_ids: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodObservationVersionEvidence:
    """Metadata for one exact observation version, with no financial values."""

    version_id: str
    source_id: str
    source_instrument_id: str
    security_id: str
    statement_type: StatementType | str
    periodicity: Periodicity | str
    report_period: date
    observed_at_utc: datetime | None
    evidence_ref: str | None
    evidenced_at_utc: datetime | None
    isolation_status: IsolationStatus | str
    isolation_evidence_ref: str | None
    isolation_evidenced_at_utc: datetime | None


@dataclass(frozen=True, slots=True)
class FinancialPeriodCoverageSlotEvidence:
    """Evidence supplied for one caller-selected exact reporting-period slot."""

    source_id: str
    source_instrument_id: str
    security_id: str
    statement_type: StatementType | str
    periodicity: Periodicity | str
    report_period: date
    crosswalk: FinancialPeriodCrosswalkEvidence | None
    lifecycle: FinancialPeriodLifecycleEvidence | None
    obligation: FinancialPeriodExpectedObligationEvidence | None
    due: FinancialPeriodDueEvidence | None
    scan: FinancialPeriodScanEvidence | None
    versions: tuple[FinancialPeriodObservationVersionEvidence, ...] = ()


@dataclass(frozen=True, slots=True)
class FinancialPeriodCoverageItem:
    """Sanitized exact-slot result with stable, guessable SHA-256 pseudonyms."""

    slot_ref: str
    state: FinancialPeriodCoverageState
    basis_codes: tuple[str, ...]
    version_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FinancialPeriodCoverageReport:
    """Per-slot results as of an explicit instant, with no aggregate completeness claim."""

    as_of_utc: datetime
    items: tuple[FinancialPeriodCoverageItem, ...]
    evidence_references_independently_verified: Literal[False] = False
    point_in_time_guarantee: Literal[False] = False


def assess_financial_period_coverage(
    slots: Iterable[FinancialPeriodCoverageSlotEvidence], *, as_of_utc: datetime
) -> FinancialPeriodCoverageReport:
    """Classify explicit financial period slots using only supplied evidence.

    Annual and quarterly periods remain separate. No slots are inferred from a
    listing date, current date, neighboring period, or another cadence. Future
    observations and evidence are never adopted. Conflicting duplicate slot or
    version identifiers make every affected slot ``unknown``. Exact duplicate
    versions with identical metadata are idempotently retained once.

    ``missing_confirmed`` is available only when an exact expected-obligation
    assertion is valid as of the assessment, its separately evidenced due time
    has arrived, and an as-of-valid scan assertion covers this exact slot and
    crosswalk, exhausts pagination, has no failures, unresolved/isolated rows
    or ambiguous mappings, and has an evidenced source-history boundary that
    covers the requested period. The function does not verify these claims.

    Args:
        slots: Bounded iterable of explicit period-slot evidence.
        as_of_utc: Caller-selected timezone-aware instant for this assessment.

    Returns:
        Sanitized per-slot status, stable basis codes, and hashed opaque refs.

    Raises:
        FinancialPeriodCoverageInputError: Structural input is malformed or
            exceeds finite bounds. Error text never includes supplied values.
    """
    as_of = _aware_datetime(as_of_utc, "as_of_utc")
    materialized = _bounded_slots(slots)
    normalized = tuple(_validate_slot(slot) for slot in materialized)

    slot_key_counts: dict[
        tuple[str, str, str, str, date], list[FinancialPeriodCoverageSlotEvidence]
    ] = {}
    version_occurrences: dict[
        str, list[tuple[tuple[str, str, str, str, date], FinancialPeriodObservationVersionEvidence]]
    ] = {}
    crosswalks: dict[
        tuple[str, str],
        list[tuple[tuple[str, str, str, str, date], FinancialPeriodCrosswalkEvidence]],
    ] = {}

    for slot in normalized:
        key = _slot_key(slot)
        slot_key_counts.setdefault(key, []).append(slot)
        if (
            slot.crosswalk is not None
            and _reference_is_valid(slot.crosswalk.evidence_ref)
            and _evidence_is_as_of(slot.crosswalk.evidenced_at_utc, as_of)
        ):
            crosswalks.setdefault(
                (slot.crosswalk.source_id, slot.crosswalk.source_instrument_id), []
            ).append((key, slot.crosswalk))
        for version in slot.versions:
            if _version_observation_is_as_of(version, as_of):
                version_occurrences.setdefault(version.version_id, []).append((key, version))

    conflicting_slot_keys = {
        key
        for key, occurrences in slot_key_counts.items()
        if any(
            _slot_control_signature(occurrence) != _slot_control_signature(occurrences[0])
            for occurrence in occurrences[1:]
        )
    }
    conflicting_version_keys = _conflicting_version_keys(version_occurrences, as_of)
    conflicting_crosswalk_keys = _conflicting_crosswalk_keys(crosswalks, slot_key_counts)

    items: list[FinancialPeriodCoverageItem] = []
    for key, occurrences in slot_key_counts.items():
        # Identical duplicate slots are idempotent. For conflicts, merge only
        # sanitized version fingerprints and preserve the fail-closed status.
        slot = occurrences[0]
        basis: set[str] = set()
        if key in conflicting_slot_keys:
            basis.add("duplicate_slot_conflict")
        if key in conflicting_version_keys:
            basis.add("duplicate_version_conflict")
        if key in conflicting_crosswalk_keys:
            basis.add("crosswalk_conflict")

        identity_ok = _crosswalk_covers_slot(slot, as_of, basis)
        version_refs, has_future_version, has_unusable_version = _version_fingerprints(
            occurrences, as_of, basis
        )
        if has_future_version:
            basis.add("future_observation_or_evidence")

        accepted_version_count = sum(
            _version_is_accepted(version, slot, as_of)
            for occurrence in occurrences
            for version in occurrence.versions
        )
        hard_conflict = (
            key in conflicting_slot_keys
            or key in conflicting_version_keys
            or key in conflicting_crosswalk_keys
        )
        if accepted_version_count and identity_ok and not hard_conflict:
            basis.update(
                {
                    "identity_crosswalk_valid_for_report_period",
                    "accepted_observation_version_present",
                }
            )
            state = FinancialPeriodCoverageState.OBSERVED
        else:
            if has_unusable_version:
                basis.add("observation_version_not_usable")
            lifecycle_ok = _lifecycle_covers_slot(slot, as_of, basis)
            obligation_ok = _expected_obligation_is_valid(slot, as_of, basis)
            due_ok = _due_has_arrived(slot, as_of, basis)
            scan_ok = _scan_covers_absence(slot, occurrences, as_of, basis)
            if (
                not hard_conflict
                and identity_ok
                and lifecycle_ok
                and obligation_ok
                and due_ok
                and scan_ok
                and accepted_version_count == 0
                and not has_unusable_version
                and not has_future_version
            ):
                basis.update(
                    {
                        "explicit_due_time_elapsed",
                        "exact_expected_obligation_asserted",
                        "exact_scan_scope_asserted",
                        "scan_result_inventory_matches_versions",
                        "scan_cursor_exhausted",
                        "source_history_boundary_covers_period",
                        "no_observation_version_in_supplied_scan",
                    }
                )
                state = FinancialPeriodCoverageState.MISSING_CONFIRMED
            else:
                state = FinancialPeriodCoverageState.UNKNOWN

        items.append(
            FinancialPeriodCoverageItem(
                slot_ref=_slot_ref(key),
                state=state,
                basis_codes=tuple(sorted(basis)),
                version_refs=version_refs,
            )
        )

    items.sort(key=lambda item: item.slot_ref)
    return FinancialPeriodCoverageReport(as_of_utc=as_of, items=tuple(items))


def _bounded_slots(slots: Iterable[FinancialPeriodCoverageSlotEvidence]) -> tuple[object, ...]:
    if isinstance(slots, (str, bytes, bytearray, dict)):
        raise FinancialPeriodCoverageInputError("slots must be a bounded iterable")
    try:
        iterator = iter(slots)
    except Exception:
        raise FinancialPeriodCoverageInputError("slots must be a bounded iterable") from None
    materialized: list[object] = []
    while True:
        try:
            slot = next(iterator)
        except StopIteration:
            break
        except Exception:
            raise FinancialPeriodCoverageInputError("slots iteration failed") from None
        if len(materialized) >= _MAX_SLOTS:
            raise FinancialPeriodCoverageInputError("slot limit exceeded")
        materialized.append(slot)
    return tuple(materialized)


def _validate_slot(slot: object) -> FinancialPeriodCoverageSlotEvidence:
    if not isinstance(slot, FinancialPeriodCoverageSlotEvidence):
        raise FinancialPeriodCoverageInputError("slot has an unsupported structure")
    _identifier(slot.source_id, "source_id")
    _identifier(slot.source_instrument_id, "source_instrument_id")
    _identifier(slot.security_id, "security_id")
    _statement_type(slot.statement_type)
    _periodicity(slot.periodicity)
    _strict_date(slot.report_period, "report_period")
    if not isinstance(slot.versions, tuple) or len(slot.versions) > _MAX_VERSIONS_PER_SLOT:
        raise FinancialPeriodCoverageInputError("versions must be a bounded tuple")
    for version in slot.versions:
        _validate_version(version)
    if slot.crosswalk is not None:
        _validate_crosswalk(slot.crosswalk)
    if slot.lifecycle is not None:
        _validate_lifecycle(slot.lifecycle)
    if slot.obligation is not None:
        _validate_expected_obligation(slot.obligation)
    if slot.due is not None:
        _validate_due(slot.due)
    if slot.scan is not None:
        _validate_scan(slot.scan)
    return slot


def _validate_version(version: object) -> None:
    if not isinstance(version, FinancialPeriodObservationVersionEvidence):
        raise FinancialPeriodCoverageInputError("version has an unsupported structure")
    for name in ("version_id", "source_id", "source_instrument_id", "security_id"):
        _identifier(getattr(version, name), name)
    _statement_type(version.statement_type)
    _periodicity(version.periodicity)
    _strict_date(version.report_period, "report_period")
    if version.isolation_status not in {"clear", "isolated", "unknown"}:
        raise FinancialPeriodCoverageInputError("isolation status is unsupported")
    _optional_reference(version.evidence_ref, "evidence_ref")
    _optional_reference(version.isolation_evidence_ref, "isolation_evidence_ref")
    _optional_datetime(version.observed_at_utc, "observed_at_utc")
    _optional_datetime(version.evidenced_at_utc, "evidenced_at_utc")
    _optional_datetime(version.isolation_evidenced_at_utc, "isolation_evidenced_at_utc")


def _validate_crosswalk(crosswalk: FinancialPeriodCrosswalkEvidence) -> None:
    if not isinstance(crosswalk, FinancialPeriodCrosswalkEvidence):
        raise FinancialPeriodCoverageInputError("crosswalk has an unsupported structure")
    for name in ("source_id", "source_instrument_id", "security_id"):
        _identifier(getattr(crosswalk, name), name)
    _strict_date(crosswalk.valid_from, "valid_from")
    _strict_date(crosswalk.valid_through, "valid_through", optional=True)
    if crosswalk.valid_through is not None and crosswalk.valid_through < crosswalk.valid_from:
        raise FinancialPeriodCoverageInputError("crosswalk range is invalid")
    _optional_reference(crosswalk.evidence_ref, "crosswalk evidence_ref")
    _optional_datetime(crosswalk.evidenced_at_utc, "crosswalk evidenced_at_utc")


def _validate_lifecycle(lifecycle: FinancialPeriodLifecycleEvidence) -> None:
    if not isinstance(lifecycle, FinancialPeriodLifecycleEvidence):
        raise FinancialPeriodCoverageInputError("lifecycle has an unsupported structure")
    if lifecycle.covers_report_period not in {True, False, None}:
        raise FinancialPeriodCoverageInputError("lifecycle assertion is invalid")
    _optional_reference(lifecycle.evidence_ref, "lifecycle evidence_ref")
    _optional_datetime(lifecycle.evidenced_at_utc, "lifecycle evidenced_at_utc")


def _validate_due(due: FinancialPeriodDueEvidence) -> None:
    if not isinstance(due, FinancialPeriodDueEvidence):
        raise FinancialPeriodCoverageInputError("due evidence has an unsupported structure")
    _optional_datetime(due.due_at_utc, "due_at_utc")
    _optional_reference(due.evidence_ref, "due evidence_ref")
    _optional_datetime(due.evidenced_at_utc, "due evidenced_at_utc")
    _optional_reference(due.obligation_evidence_ref, "due obligation_evidence_ref")


def _validate_expected_obligation(
    obligation: FinancialPeriodExpectedObligationEvidence,
) -> None:
    if not isinstance(obligation, FinancialPeriodExpectedObligationEvidence):
        raise FinancialPeriodCoverageInputError("expected obligation has an unsupported structure")
    for name in ("source_id", "source_instrument_id", "security_id"):
        _identifier(getattr(obligation, name), name)
    _statement_type(obligation.statement_type)
    _periodicity(obligation.periodicity)
    _strict_date(obligation.report_period, "report_period")
    if obligation.applies not in {True, False, None}:
        raise FinancialPeriodCoverageInputError("expected obligation assertion is invalid")
    _optional_reference(obligation.evidence_ref, "expected obligation evidence_ref")
    _optional_datetime(obligation.evidenced_at_utc, "expected obligation evidenced_at_utc")


def _validate_scan(scan: FinancialPeriodScanEvidence) -> None:
    if not isinstance(scan, FinancialPeriodScanEvidence):
        raise FinancialPeriodCoverageInputError("scan evidence has an unsupported structure")
    for name in ("snapshot_at_utc", "evidenced_at_utc", "source_history_evidenced_at_utc"):
        _optional_datetime(getattr(scan, name), name)
    _optional_reference(scan.evidence_ref, "scan evidence_ref")
    _optional_reference(scan.source_history_evidence_ref, "source_history_evidence_ref")
    for name in (
        "scope_matches_slot",
        "crosswalk_matches_snapshot",
        "cursor_exhausted",
        "source_history_covers_period",
    ):
        value = getattr(scan, name)
        if value not in {True, False, None}:
            raise FinancialPeriodCoverageInputError("scan assertion is invalid")
    for name in (
        "failed_page_count",
        "unresolved_row_count",
        "isolated_row_count",
        "ambiguous_crosswalk_count",
    ):
        value = getattr(scan, name)
        if value is not None and (type(value) is not int or value < 0 or value > 1_000_000):
            raise FinancialPeriodCoverageInputError("scan count is invalid")
    if scan.matched_version_ids is not None:
        if (
            not isinstance(scan.matched_version_ids, tuple)
            or len(scan.matched_version_ids) > _MAX_VERSIONS_PER_SLOT
        ):
            raise FinancialPeriodCoverageInputError("scan version inventory must be bounded")
        for version_id in scan.matched_version_ids:
            _identifier(version_id, "scan version_id")
        if len(set(scan.matched_version_ids)) != len(scan.matched_version_ids):
            raise FinancialPeriodCoverageInputError("scan version inventory must be unique")


def _crosswalk_covers_slot(
    slot: FinancialPeriodCoverageSlotEvidence, as_of: datetime, basis: set[str]
) -> bool:
    crosswalk = slot.crosswalk
    if crosswalk is None or not _reference_is_valid(crosswalk.evidence_ref):
        basis.add("identity_crosswalk_evidence_missing")
        return False
    if not _evidence_is_as_of(crosswalk.evidenced_at_utc, as_of):
        basis.add("future_or_undated_crosswalk_evidence")
        return False
    if (
        crosswalk.source_id != slot.source_id
        or crosswalk.source_instrument_id != slot.source_instrument_id
        or crosswalk.security_id != slot.security_id
    ):
        basis.add("identity_crosswalk_mismatch")
        return False
    if crosswalk.valid_from > slot.report_period or (
        crosswalk.valid_through is not None and slot.report_period > crosswalk.valid_through
    ):
        basis.add("identity_crosswalk_outside_report_period")
        return False
    basis.add("identity_crosswalk_valid_for_report_period")
    return True


def _lifecycle_covers_slot(
    slot: FinancialPeriodCoverageSlotEvidence, as_of: datetime, basis: set[str]
) -> bool:
    lifecycle = slot.lifecycle
    if lifecycle is None or not _reference_is_valid(lifecycle.evidence_ref):
        basis.add("lifecycle_evidence_missing")
        return False
    if not _evidence_is_as_of(lifecycle.evidenced_at_utc, as_of):
        basis.add("future_or_undated_lifecycle_evidence")
        return False
    if lifecycle.covers_report_period is not True:
        basis.add("lifecycle_does_not_confirm_report_period")
        return False
    basis.add("issuer_reporting_lifecycle_covers_report_period")
    return True


def _expected_obligation_is_valid(
    slot: FinancialPeriodCoverageSlotEvidence, as_of: datetime, basis: set[str]
) -> bool:
    obligation = slot.obligation
    if obligation is None or not _reference_is_valid(obligation.evidence_ref):
        basis.add("expected_obligation_evidence_missing")
        return False
    if not _evidence_is_as_of(obligation.evidenced_at_utc, as_of):
        basis.add("future_or_undated_expected_obligation_evidence")
        return False
    if (
        obligation.source_id != slot.source_id
        or obligation.source_instrument_id != slot.source_instrument_id
        or obligation.security_id != slot.security_id
        or _statement_type(obligation.statement_type) != _statement_type(slot.statement_type)
        or _periodicity(obligation.periodicity) != _periodicity(slot.periodicity)
        or obligation.report_period != slot.report_period
    ):
        basis.add("expected_obligation_scope_mismatch")
        return False
    if obligation.applies is not True:
        basis.add("expected_obligation_not_confirmed_for_exact_slot")
        return False
    basis.add("exact_expected_obligation_asserted")
    return True


def _due_has_arrived(
    slot: FinancialPeriodCoverageSlotEvidence, as_of: datetime, basis: set[str]
) -> bool:
    due = slot.due
    obligation = slot.obligation
    if (
        due is None
        or due.due_at_utc is None
        or not _reference_is_valid(due.evidence_ref)
        or obligation is None
        or not _reference_is_valid(obligation.evidence_ref)
        or due.obligation_evidence_ref != obligation.evidence_ref
    ):
        basis.add("explicit_due_evidence_missing")
        return False
    if not _evidence_is_as_of(due.evidenced_at_utc, as_of):
        basis.add("future_or_undated_due_evidence")
        return False
    if _aware_datetime(due.due_at_utc, "due_at_utc") > as_of:
        basis.add("explicit_due_time_not_arrived")
        return False
    basis.add("explicit_due_time_elapsed")
    return True


def _scan_covers_absence(
    slot: FinancialPeriodCoverageSlotEvidence,
    occurrences: list[FinancialPeriodCoverageSlotEvidence],
    as_of: datetime,
    basis: set[str],
) -> bool:
    scan = slot.scan
    if scan is None or not _reference_is_valid(scan.evidence_ref):
        basis.add("complete_scan_evidence_missing")
        return False
    if not _evidence_is_as_of(scan.snapshot_at_utc, as_of) or not _evidence_is_as_of(
        scan.evidenced_at_utc, as_of
    ):
        basis.add("future_or_undated_scan_evidence")
        return False
    if _aware_datetime(scan.evidenced_at_utc, "scan evidenced_at_utc") < _aware_datetime(
        scan.snapshot_at_utc, "scan snapshot_at_utc"
    ):
        basis.add("scan_evidence_predates_snapshot")
        return False
    if scan.matched_version_ids is None:
        basis.add("scan_result_inventory_unknown")
        inventory_ok = False
    else:
        as_of_versions = tuple(
            version
            for occurrence in occurrences
            for version in occurrence.versions
            if _version_observation_is_as_of(version, as_of)
        )
        supplied_version_ids = {version.version_id for version in as_of_versions}
        scanned_version_ids = set(scan.matched_version_ids)
        snapshot_at = _aware_datetime(scan.snapshot_at_utc, "scan snapshot_at_utc")
        all_versions_predate_snapshot = all(
            _aware_datetime(version.observed_at_utc, "version observed_at_utc") <= snapshot_at
            for version in as_of_versions
        )
        inventory_ok = scanned_version_ids == supplied_version_ids and all_versions_predate_snapshot
        if not inventory_ok:
            basis.add("scan_result_inventory_mismatch_or_unknown")
    if inventory_ok:
        basis.add("scan_result_inventory_matches_versions")
    due_at = slot.due.due_at_utc if slot.due is not None else None
    if due_at is not None and _aware_datetime(scan.snapshot_at_utc, "scan snapshot_at_utc") < (
        _aware_datetime(due_at, "due_at_utc")
    ):
        basis.add("scan_predates_explicit_due_time")
        return False
    if scan.scope_matches_slot is not True:
        basis.add("scan_scope_not_confirmed_for_exact_slot")
    if scan.crosswalk_matches_snapshot is not True:
        basis.add("scan_crosswalk_snapshot_mismatch_or_unknown")
    if scan.cursor_exhausted is not True:
        basis.add("scan_cursor_not_exhausted")
    if scan.failed_page_count != 0:
        basis.add("scan_has_failures_or_unknown_failure_count")
    if scan.unresolved_row_count != 0:
        basis.add("scan_has_unresolved_rows_or_unknown_count")
    if scan.isolated_row_count != 0:
        basis.add("scan_has_isolated_rows_or_unknown_count")
    if scan.ambiguous_crosswalk_count != 0:
        basis.add("scan_has_ambiguous_crosswalks_or_unknown_count")
    history_ok = (
        scan.source_history_covers_period is True
        and _reference_is_valid(scan.source_history_evidence_ref)
        and _evidence_is_as_of(scan.source_history_evidenced_at_utc, as_of)
    )
    if not history_ok:
        basis.add("source_history_boundary_unknown_or_unverified")
    else:
        basis.add("source_history_boundary_covers_period")
    scan_ok = (
        scan.scope_matches_slot is True
        and scan.crosswalk_matches_snapshot is True
        and scan.cursor_exhausted is True
        and scan.failed_page_count == 0
        and scan.unresolved_row_count == 0
        and scan.isolated_row_count == 0
        and scan.ambiguous_crosswalk_count == 0
        and history_ok
        and inventory_ok
    )
    if scan_ok:
        basis.update({"exact_scan_scope_asserted", "scan_cursor_exhausted"})
    return scan_ok


def _version_fingerprints(
    slots: Iterable[FinancialPeriodCoverageSlotEvidence], as_of: datetime, basis: set[str]
) -> tuple[tuple[str, ...], bool, bool]:
    refs: set[str] = set()
    has_future = False
    has_unusable = False
    for slot in slots:
        for version in slot.versions:
            if not _version_observation_is_as_of(version, as_of):
                if _has_future_version_timestamp(version, as_of):
                    has_future = True
                    basis.add("future_observation_or_evidence")
                else:
                    has_unusable = True
                    basis.add("observation_version_time_or_evidence_missing")
                continue
            refs.add(_fingerprint(version.version_id))
            if not _version_matches_slot(version, slot):
                has_unusable = True
                basis.add("observation_version_scope_mismatch")
            if (
                version.isolation_status != "clear"
                or not _reference_is_valid(version.isolation_evidence_ref)
                or not _evidence_is_as_of(version.isolation_evidenced_at_utc, as_of)
            ):
                has_unusable = True
                basis.add("observation_version_isolated_or_isolation_unknown")
                if _has_future_timestamp(version.isolation_evidenced_at_utc, as_of):
                    has_future = True
    return tuple(sorted(refs)), has_future, has_unusable


def _version_is_accepted(
    version: FinancialPeriodObservationVersionEvidence,
    slot: FinancialPeriodCoverageSlotEvidence,
    as_of: datetime,
) -> bool:
    return (
        _version_observation_is_as_of(version, as_of)
        and _version_matches_slot(version, slot)
        and version.isolation_status == "clear"
        and _reference_is_valid(version.isolation_evidence_ref)
        and _evidence_is_as_of(version.isolation_evidenced_at_utc, as_of)
    )


def _version_matches_slot(
    version: FinancialPeriodObservationVersionEvidence, slot: FinancialPeriodCoverageSlotEvidence
) -> bool:
    return (
        version.source_id == slot.source_id
        and version.source_instrument_id == slot.source_instrument_id
        and version.security_id == slot.security_id
        and _statement_type(version.statement_type) == _statement_type(slot.statement_type)
        and _periodicity(version.periodicity) == _periodicity(slot.periodicity)
        and version.report_period == slot.report_period
        and _reference_is_valid(version.evidence_ref)
    )


def _version_observation_is_as_of(
    version: FinancialPeriodObservationVersionEvidence, as_of: datetime
) -> bool:
    return _evidence_is_as_of(version.observed_at_utc, as_of) and _evidence_is_as_of(
        version.evidenced_at_utc, as_of
    )


def _has_future_version_timestamp(
    version: FinancialPeriodObservationVersionEvidence, as_of: datetime
) -> bool:
    return any(
        _has_future_timestamp(value, as_of)
        for value in (
            version.observed_at_utc,
            version.evidenced_at_utc,
            version.isolation_evidenced_at_utc,
        )
    )


def _has_future_timestamp(value: datetime | None, as_of: datetime) -> bool:
    return value is not None and _aware_datetime(value, "evidence timestamp") > as_of


def _conflicting_version_keys(
    occurrences: dict[
        str,
        list[tuple[tuple[str, str, str, str, date], FinancialPeriodObservationVersionEvidence]],
    ],
    as_of: datetime,
) -> set[tuple[str, str, str, str, date]]:
    conflicts: set[tuple[str, str, str, str, date]] = set()
    for versions in occurrences.values():
        if len(versions) <= 1:
            continue
        base_signatures = {_version_base_signature(version) for _, version in versions}
        current_isolation = {
            (
                version.isolation_status,
                version.isolation_evidence_ref,
                version.isolation_evidenced_at_utc,
            )
            for _, version in versions
            if _evidence_is_as_of(version.isolation_evidenced_at_utc, as_of)
        }
        if len(base_signatures) > 1 or len(current_isolation) > 1:
            conflicts.update(key for key, _ in versions)
    return conflicts


def _version_base_signature(
    version: FinancialPeriodObservationVersionEvidence,
) -> tuple[object, ...]:
    return (
        version.source_id,
        version.source_instrument_id,
        version.security_id,
        _statement_type(version.statement_type),
        _periodicity(version.periodicity),
        version.report_period,
        version.observed_at_utc,
        version.evidence_ref,
        version.evidenced_at_utc,
    )


def _conflicting_crosswalk_keys(
    crosswalks: dict[
        tuple[str, str],
        list[tuple[tuple[str, str, str, str, date], FinancialPeriodCrosswalkEvidence]],
    ],
    slots_by_key: dict[tuple[str, str, str, str, date], list[FinancialPeriodCoverageSlotEvidence]],
) -> set[tuple[str, str, str, str, date]]:
    conflicts: set[tuple[str, str, str, str, date]] = set()
    for key, occurrences in slots_by_key.items():
        slot = occurrences[0]
        source_key = (slot.source_id, slot.source_instrument_id)
        period = slot.report_period
        effective_targets = {
            mapping.security_id
            for _, mapping in crosswalks.get(source_key, ())
            if mapping.valid_from <= period
            and (mapping.valid_through is None or period <= mapping.valid_through)
        }
        if len(effective_targets) > 1:
            conflicts.add(key)
    return conflicts


def _slot_key(
    slot: FinancialPeriodCoverageSlotEvidence,
) -> tuple[str, str, str, str, date]:
    return (
        slot.source_id,
        slot.source_instrument_id,
        slot.security_id,
        _statement_type(slot.statement_type).value + ":" + _periodicity(slot.periodicity).value,
        slot.report_period,
    )


def _slot_control_signature(slot: FinancialPeriodCoverageSlotEvidence) -> tuple[object, ...]:
    """Compare duplicate slot claims while allowing versions to accumulate."""
    return (
        slot.source_id,
        slot.source_instrument_id,
        slot.security_id,
        _statement_type(slot.statement_type),
        _periodicity(slot.periodicity),
        slot.report_period,
        slot.crosswalk,
        slot.lifecycle,
        slot.due,
        slot.obligation,
        slot.scan,
    )


def _slot_ref(key: tuple[str, str, str, str, date]) -> str:
    return _fingerprint("\x1f".join((*key[:4], key[4].isoformat())))


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _statement_type(value: object) -> StatementType:
    try:
        return StatementType(value)
    except (TypeError, ValueError):
        raise FinancialPeriodCoverageInputError("statement type is unsupported") from None


def _periodicity(value: object) -> Periodicity:
    try:
        return Periodicity(value)
    except (TypeError, ValueError):
        raise FinancialPeriodCoverageInputError("periodicity is unsupported") from None


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise FinancialPeriodCoverageInputError(f"{name} must be a bounded opaque identifier")
    return value


def _optional_reference(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _reference_is_valid(value: str | None) -> bool:
    return isinstance(value, str) and _IDENTIFIER_PATTERN.fullmatch(value) is not None


def _strict_date(value: object, name: str, *, optional: bool = False) -> date | None:
    if optional and value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise FinancialPeriodCoverageInputError(f"{name} must be a calendar date")
    return value


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    return _aware_datetime(value, name)


def _aware_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise FinancialPeriodCoverageInputError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _evidence_is_as_of(value: datetime | None, as_of: datetime) -> bool:
    return value is not None and _aware_datetime(value, "evidence timestamp") <= as_of
