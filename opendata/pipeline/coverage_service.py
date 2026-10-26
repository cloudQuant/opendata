"""Evidence-bound history coverage queries over immutable publications.

The service composes :mod:`history_coverage` with explicitly injected reviewed
instrument, calendar, and publication evidence. It accepts half-open API date
ranges and adapts them to the evaluator's inclusive daily interval.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from enum import Enum
from itertools import islice
from typing import TYPE_CHECKING

from opendata.data.instrument_reference import InstrumentReferenceRegistry, LifecycleStatus
from opendata.pipeline.history_coverage import (
    CoverageStatus,
    HistoryCoverageReport,
    HistoryCoverageRequest,
    evaluate_history_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

_IDENTIFIER_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/+-"
)
_MAX_ROWS = 100_000
_PUBLICATION_ID = frozenset("0123456789abcdef")


class PublicationCoverageScope(str, Enum):
    """Temporal meaning of observations attached to one immutable publication."""

    BUSINESS_HISTORY = "business_history"
    ARCHIVED_SNAPSHOT_OBSERVATIONS = "archived_snapshot_observations"
    CURRENT_SNAPSHOT = "current_snapshot"


def _identifier(value: str, name: str) -> str:
    """Validate stable identifiers using the platform contract character set."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or value[0] not in _IDENTIFIER_CHARS
        or any(character not in _IDENTIFIER_CHARS for character in value)
    ):
        raise ValueError(f"{name} must be a 1-128 character stable identifier")
    return value


def _evidence(value: str, name: str) -> str:
    """Validate an opaque, bounded evidence reference."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} must be a bounded nonempty evidence reference")
    return value


def _strict_date(value: date | None, name: str) -> date | None:
    """Accept date values and reject timestamp/string coercion."""
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a datetime.date")
    return value


def _bounded_dates(values: Iterable[date], name: str, *, unique: bool) -> tuple[date, ...]:
    """Materialize a bounded date sequence while preserving observation duplicates."""
    if isinstance(values, (str, bytes, bytearray, dict)):
        raise ValueError(f"{name} must be a bounded iterable of dates")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be a bounded iterable of dates") from exc
    rows = tuple(islice(iterator, _MAX_ROWS + 1))
    if len(rows) > _MAX_ROWS:
        raise ValueError(f"{name} exceeds the {_MAX_ROWS:,}-date limit")
    for index, item in enumerate(rows):
        if isinstance(item, datetime) or not isinstance(item, date):
            raise ValueError(f"{name}[{index}] must be a datetime.date")
    if unique and len(set(rows)) != len(rows):
        raise ValueError(f"{name} must not contain duplicate dates")
    return rows


def _publication_id(value: str) -> str:
    """Require the 32-character immutable publication identifier format."""
    if (
        not isinstance(value, str)
        or len(value) != 32
        or any(character not in _PUBLICATION_ID for character in value)
    ):
        raise ValueError("publication_id must be 32 lowercase hexadecimal characters")
    return value


def _interval(start: date | None, end: date | None, name: str) -> tuple[date | None, date | None]:
    """Validate an optional half-open interval and reject one-sided bounds."""
    left = _strict_date(start, f"{name}_start")
    right = _strict_date(end, f"{name}_end_exclusive")
    if (left is None) != (right is None):
        raise ValueError(f"{name} requires both half-open bounds")
    if left is not None and right is not None and left >= right:
        raise ValueError(f"{name} must be a nonempty half-open interval")
    return left, right


def _inclusive_end(exclusive: date) -> date:
    """Convert a nonempty half-open daily end to the evaluator's inclusive end."""
    return exclusive - timedelta(days=1)


@dataclass(frozen=True, slots=True)
class CalendarEvidence:
    """Reviewed daily calendar slice bound to one dataset/source/instrument.

    When ``daily_classification_complete`` is true, every date in the declared
    inclusive bounds has an authoritative open/closed classification. Dates
    absent from ``open_dates`` are then holidays or closed days; absent dates
    in an incomplete slice are never silently treated as closed.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    calendar_id: str
    calendar_version: str
    evidence_ref: str
    covered_from: date
    covered_through: date
    open_dates: tuple[date, ...]
    daily_classification_complete: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate scope binding, bounds, evidence, and open-date contents."""
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be the integer 1")
        for name in (
            "dataset_id",
            "source_id",
            "instrument_id",
            "calendar_id",
            "calendar_version",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "evidence_ref", _evidence(self.evidence_ref, "evidence_ref"))
        left = _strict_date(self.covered_from, "covered_from")
        right = _strict_date(self.covered_through, "covered_through")
        if left is None or right is None or left > right:
            raise ValueError("calendar coverage requires covered_from <= covered_through")
        object.__setattr__(self, "covered_from", left)
        object.__setattr__(self, "covered_through", right)
        if type(self.daily_classification_complete) is not bool:
            raise ValueError("daily_classification_complete must be a bool")
        days = _bounded_dates(self.open_dates, "open_dates", unique=True)
        if any(day < left or day > right for day in days):
            raise ValueError("open_dates must fall inside the declared calendar bounds")
        object.__setattr__(self, "open_dates", days)


@dataclass(frozen=True, slots=True)
class PublicationObservations:
    """Actual daily observations and explicit scan scope from one publication.

    ``scope_start``/``scope_end_exclusive`` state what the publisher actually
    checked. They are not inferred from the first or last row. Duplicate dates
    are preserved so the evaluator can report duplicate business keys.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    publication_id: str
    publication_version: str
    evidence_ref: str
    scope: PublicationCoverageScope | str
    observed_dates: tuple[date, ...]
    scope_start: date | None = None
    scope_end_exclusive: date | None = None
    scope_evidence_ref: str | None = None
    source_retained_from: date | None = None
    source_retained_before: date | None = None
    source_retention_evidence_ref: str | None = None
    archive_start: date | None = None
    archive_end_exclusive: date | None = None
    archive_evidence_ref: str | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate immutable publication identity, temporal scope, and limits."""
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be the integer 1")
        for name in ("dataset_id", "source_id", "instrument_id", "publication_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        object.__setattr__(self, "publication_id", _publication_id(self.publication_id))
        object.__setattr__(self, "evidence_ref", _evidence(self.evidence_ref, "evidence_ref"))
        try:
            scope = PublicationCoverageScope(self.scope)
        except (TypeError, ValueError) as exc:
            raise ValueError("scope must be a supported publication coverage scope") from exc
        object.__setattr__(self, "scope", scope)
        days = _bounded_dates(self.observed_dates, "observed_dates", unique=False)
        object.__setattr__(self, "observed_dates", days)

        scope_start, scope_end = _interval(self.scope_start, self.scope_end_exclusive, "scope")
        object.__setattr__(self, "scope_start", scope_start)
        object.__setattr__(self, "scope_end_exclusive", scope_end)
        if scope_start is None:
            if self.scope_evidence_ref is not None:
                raise ValueError("scope_evidence_ref requires a declared publication scope")
        else:
            if scope_end is None:
                raise ValueError("scope requires both half-open bounds")
            if self.scope_evidence_ref is None:
                raise ValueError("declared publication scope requires scope_evidence_ref")
            object.__setattr__(
                self, "scope_evidence_ref", _evidence(self.scope_evidence_ref, "scope_evidence_ref")
            )
            if any(not scope_start <= day < scope_end for day in days):
                raise ValueError("observed_dates must fall inside the declared publication scope")

        retained_from = _strict_date(self.source_retained_from, "source_retained_from")
        retained_before = _strict_date(self.source_retained_before, "source_retained_before")
        object.__setattr__(self, "source_retained_from", retained_from)
        object.__setattr__(self, "source_retained_before", retained_before)
        if (
            retained_from is not None
            and retained_before is not None
            and retained_from >= retained_before
        ):
            raise ValueError("source retention bounds must form a nonempty half-open interval")
        has_retention = retained_from is not None or retained_before is not None
        if has_retention != (self.source_retention_evidence_ref is not None):
            raise ValueError("source retention bounds and their evidence must be supplied together")
        if self.source_retention_evidence_ref is not None:
            object.__setattr__(
                self,
                "source_retention_evidence_ref",
                _evidence(self.source_retention_evidence_ref, "source_retention_evidence_ref"),
            )

        archive_start, archive_end = _interval(
            self.archive_start, self.archive_end_exclusive, "archive"
        )
        object.__setattr__(self, "archive_start", archive_start)
        object.__setattr__(self, "archive_end_exclusive", archive_end)
        has_archive = archive_start is not None
        if has_archive != (self.archive_evidence_ref is not None):
            raise ValueError("archive bounds and their evidence must be supplied together")
        if self.archive_evidence_ref is not None:
            object.__setattr__(
                self,
                "archive_evidence_ref",
                _evidence(self.archive_evidence_ref, "archive_evidence_ref"),
            )
        if scope is PublicationCoverageScope.BUSINESS_HISTORY and has_archive:
            raise ValueError("business_history publication cannot declare snapshot archive bounds")
        if scope is PublicationCoverageScope.CURRENT_SNAPSHOT and has_archive:
            raise ValueError("current_snapshot publication cannot declare archive bounds")
        if scope is PublicationCoverageScope.CURRENT_SNAPSHOT and len(set(days)) > 1:
            raise ValueError("current_snapshot must contain at most one business date")


@dataclass(frozen=True, slots=True)
class CoverageQuery:
    """A half-open coverage request using a canonical ID or reviewed alias.

    ``identifier_as_of`` pins resolution of a time-scoped/reused alias. Once
    resolved, coverage applies to that canonical instrument across the complete
    requested range, even when the alias itself was valid for only one date.
    """

    dataset_id: str
    source_id: str
    instrument_identifier: str
    start: date
    end_exclusive: date
    identifier_as_of: date | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Validate query identities and require a nonempty half-open date range."""
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must be the integer 1")
        for name in ("dataset_id", "source_id", "instrument_identifier"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        start = _strict_date(self.start, "start")
        end = _strict_date(self.end_exclusive, "end_exclusive")
        if start is None or end is None or start >= end:
            raise ValueError("coverage query requires start < end_exclusive")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end_exclusive", end)
        object.__setattr__(
            self,
            "identifier_as_of",
            _strict_date(self.identifier_as_of, "identifier_as_of"),
        )


@dataclass(frozen=True, slots=True)
class InstrumentHistoryCoverageReport:
    """Final assessment with all identity, evidence versions, and half-open scopes."""

    schema_version: int
    dataset_id: str
    source_id: str
    input_identifier: str
    instrument_id: str
    requested_start: date
    requested_end_exclusive: date
    evaluated_start: date
    evaluated_end_exclusive: date
    registry_version: str
    registry_evidence_ref: str
    lifecycle_version: str
    lifecycle_status: LifecycleStatus
    lifecycle_status_as_of: date
    lifecycle_status_evidence_ref: str
    known_start_date: date | None
    start_boundary_unknown: bool
    start_evidence_ref: str | None
    delisted_on: date | None
    delist_evidence_ref: str | None
    alias_version: str | None
    alias_evidence_ref: str | None
    alias_valid_from: date | None
    alias_valid_through: date | None
    alias_resolved_as_of: date | None
    calendar_id: str
    calendar_version: str
    calendar_evidence_ref: str
    calendar_covered_from: date
    calendar_covered_through: date
    calendar_daily_classification_complete: bool
    publication_id: str
    publication_version: str
    publication_evidence_ref: str
    publication_scope: PublicationCoverageScope
    publication_scope_start: date | None
    publication_scope_end_exclusive: date | None
    publication_scope_evidence_ref: str | None
    coverage: HistoryCoverageReport
    reasons: tuple[str, ...]

    @property
    def status(self) -> CoverageStatus:
        """Return the final status after all evidence-scope gates."""
        return self.coverage.status

    def to_dict(self) -> dict[str, object]:
        """Serialize the versioned assessment with identity and evidence scope."""
        return {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "instrument": {
                "input_identifier": self.input_identifier,
                "instrument_id": self.instrument_id,
                "registry_version": self.registry_version,
                "registry_evidence_ref": self.registry_evidence_ref,
                "lifecycle_version": self.lifecycle_version,
                "lifecycle_status": self.lifecycle_status.value,
                "lifecycle_status_as_of": self.lifecycle_status_as_of.isoformat(),
                "lifecycle_status_evidence_ref": self.lifecycle_status_evidence_ref,
                "known_start_date": (
                    self.known_start_date.isoformat() if self.known_start_date is not None else None
                ),
                "start_boundary_unknown": self.start_boundary_unknown,
                "start_evidence_ref": self.start_evidence_ref,
                "delisted_on": self.delisted_on.isoformat() if self.delisted_on else None,
                "delist_evidence_ref": self.delist_evidence_ref,
                "alias_version": self.alias_version,
                "alias_evidence_ref": self.alias_evidence_ref,
                "alias_valid_from": (
                    self.alias_valid_from.isoformat() if self.alias_valid_from is not None else None
                ),
                "alias_valid_through": (
                    self.alias_valid_through.isoformat()
                    if self.alias_valid_through is not None
                    else None
                ),
                "alias_resolved_as_of": (
                    self.alias_resolved_as_of.isoformat()
                    if self.alias_resolved_as_of is not None
                    else None
                ),
            },
            "requested_scope": {
                "start_inclusive": self.requested_start.isoformat(),
                "end_exclusive": self.requested_end_exclusive.isoformat(),
                "evaluated_start_inclusive": self.evaluated_start.isoformat(),
                "evaluated_end_exclusive": self.evaluated_end_exclusive.isoformat(),
            },
            "calendar": {
                "id": self.calendar_id,
                "version": self.calendar_version,
                "evidence_ref": self.calendar_evidence_ref,
                "covered_from": self.calendar_covered_from.isoformat(),
                "covered_through": self.calendar_covered_through.isoformat(),
                "daily_classification_complete": self.calendar_daily_classification_complete,
            },
            "publication": {
                "id": self.publication_id,
                "version": self.publication_version,
                "evidence_ref": self.publication_evidence_ref,
                "scope": self.publication_scope.value,
                "scope_start": (
                    self.publication_scope_start.isoformat()
                    if self.publication_scope_start is not None
                    else None
                ),
                "scope_end_exclusive": (
                    self.publication_scope_end_exclusive.isoformat()
                    if self.publication_scope_end_exclusive is not None
                    else None
                ),
                "scope_evidence_ref": self.publication_scope_evidence_ref,
            },
            "status": self.status.value,
            "coverage": self.coverage.to_dict(),
            "reasons": list(self.reasons),
        }


class InstrumentCoverageService:
    """Compose reviewed reference, calendar, and publication evidence."""

    def __init__(self, registry: InstrumentReferenceRegistry) -> None:
        """Create a service using one explicitly injected reviewed registry."""
        if not isinstance(registry, InstrumentReferenceRegistry):
            raise TypeError("registry must be an InstrumentReferenceRegistry")
        self._registry = registry

    def evaluate(
        self,
        query: CoverageQuery,
        *,
        calendar: CalendarEvidence,
        publication: PublicationObservations,
    ) -> InstrumentHistoryCoverageReport:
        """Evaluate one immutable publication over a half-open daily date range.

        Args:
            query: Dataset/source/instrument and requested ``[start, end)``.
            calendar: Reviewed, instrument-bound calendar evidence.
            publication: Actual observations and explicit scan scope from a
                fixed publication ID.

        Returns:
            A coverage report bound to every supplied evidence version.

        Raises:
            TypeError: If a caller injects values of the wrong model type.
            ValueError: If evidence belongs to a different requested series.
            InstrumentReferenceError: If the identifier is not uniquely reviewed.
        """
        if not isinstance(query, CoverageQuery):
            raise TypeError("query must be a CoverageQuery")
        if not isinstance(calendar, CalendarEvidence):
            raise TypeError("calendar must be a CalendarEvidence")
        if not isinstance(publication, PublicationObservations):
            raise TypeError("publication must be PublicationObservations")
        resolved = self._registry.resolve(
            query.dataset_id,
            query.source_id,
            query.instrument_identifier,
            as_of=query.identifier_as_of,
        )
        lifecycle = resolved.lifecycle
        lifecycle_status = LifecycleStatus(lifecycle.status)
        publication_scope = PublicationCoverageScope(publication.scope)
        identity = (query.dataset_id, query.source_id, resolved.instrument_id)
        if (calendar.dataset_id, calendar.source_id, calendar.instrument_id) != identity:
            raise ValueError("calendar evidence is bound to a different dataset/source/instrument")
        if (publication.dataset_id, publication.source_id, publication.instrument_id) != identity:
            raise ValueError(
                "publication evidence is bound to a different dataset/source/instrument"
            )

        evaluated_start = max(
            query.start,
            lifecycle.known_start_date if lifecycle.known_start_date is not None else query.start,
        )
        evaluated_end_exclusive = query.end_exclusive
        if (
            lifecycle_status is LifecycleStatus.DELISTED
            and lifecycle.delisted_on is not None
            and lifecycle.delisted_on < date.max
        ):
            day_after_delisting = lifecycle.delisted_on + timedelta(days=1)
            evaluated_end_exclusive = min(evaluated_end_exclusive, day_after_delisting)
        has_lifecycle_overlap = evaluated_start < evaluated_end_exclusive
        if has_lifecycle_overlap:
            reported_evaluated_start = evaluated_start
            reported_evaluated_end_exclusive = evaluated_end_exclusive
        else:
            empty_at = (
                query.end_exclusive
                if lifecycle.known_start_date is not None
                and query.end_exclusive <= lifecycle.known_start_date
                else query.start
            )
            reported_evaluated_start = empty_at
            reported_evaluated_end_exclusive = empty_at

        if has_lifecycle_overlap:
            evaluator_start = evaluated_start
            evaluator_end = _inclusive_end(evaluated_end_exclusive)
            expected = tuple(
                day for day in calendar.open_dates if evaluator_start <= day <= evaluator_end
            )
        else:
            # An empty lifecycle intersection is explicitly not applicable. A
            # one-day evaluator interval keeps its public result schema stable.
            evaluator_start = query.start
            evaluator_end = _inclusive_end(query.end_exclusive)
            expected = ()

        requested_observations = tuple(
            day for day in publication.observed_dates if query.start <= day < query.end_exclusive
        )
        source_retained_through = (
            _inclusive_end(publication.source_retained_before)
            if publication.source_retained_before is not None
            else None
        )
        archive_through = (
            _inclusive_end(publication.archive_end_exclusive)
            if publication.archive_end_exclusive is not None
            else None
        )
        base_request = HistoryCoverageRequest(
            dataset=query.dataset_id,
            source=query.source_id,
            instrument_key=resolved.instrument_id,
            request_start=evaluator_start,
            request_end=evaluator_end,
            expected_business_dates=expected,
            observed_business_dates=requested_observations,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.calendar_version,
            business_calendar_evidence=calendar.evidence_ref,
            calendar_covered_from=calendar.covered_from,
            calendar_covered_through=calendar.covered_through,
            known_start_date=lifecycle.known_start_date,
            start_boundary_unknown=lifecycle.start_boundary_unknown,
            independent_start_evidence=lifecycle.start_evidence_ref,
            source_retained_from=publication.source_retained_from,
            source_retained_through=source_retained_through,
            source_retention_evidence=publication.source_retention_evidence_ref,
            snapshot_only=publication_scope is not PublicationCoverageScope.BUSINESS_HISTORY,
            snapshot_archive_from=publication.archive_start,
            snapshot_archive_through=archive_through,
            snapshot_archive_evidence=publication.archive_evidence_ref,
        )
        coverage = evaluate_history_coverage(base_request)
        reasons = list(coverage.reasons)
        calendar_spans_scope = (
            calendar.daily_classification_complete
            and calendar.covered_from <= evaluator_start
            and calendar.covered_through >= evaluator_end
        )
        publication_spans_scope = (
            publication.scope_start is not None
            and publication.scope_end_exclusive is not None
            and publication.scope_start <= evaluator_start
            and publication.scope_end_exclusive >= evaluated_end_exclusive
        )

        if not has_lifecycle_overlap and not requested_observations:
            coverage = replace(
                coverage,
                status=CoverageStatus.NOT_APPLICABLE,
                reasons=coverage.reasons
                + (
                    "The requested interval does not overlap the independently reviewed "
                    "instrument lifecycle.",
                ),
            )
            reasons = list(coverage.reasons)
        elif not calendar.daily_classification_complete:
            reasons.append(
                "Calendar evidence does not certify a complete daily open/closed classification."
            )
            coverage = replace(
                coverage,
                status=CoverageStatus.UNKNOWN,
                business_calendar_covers_request=False,
                reasons=tuple(reasons),
            )
        elif not calendar_spans_scope and has_lifecycle_overlap:
            reasons.append(
                "Calendar evidence does not cover the evaluated instrument-life interval."
            )
            coverage = replace(
                coverage,
                status=CoverageStatus.UNKNOWN,
                business_calendar_covers_request=False,
                reasons=tuple(reasons),
            )
        elif lifecycle_status is LifecycleStatus.UNKNOWN:
            reasons.append("The reviewed instrument lifecycle status is unknown.")
            coverage = replace(coverage, status=CoverageStatus.UNKNOWN, reasons=tuple(reasons))
        elif lifecycle_status is LifecycleStatus.ACTIVE and evaluator_end > lifecycle.status_as_of:
            reasons.append(
                "The active lifecycle attestation does not reach the requested end date."
            )
            coverage = replace(coverage, status=CoverageStatus.UNKNOWN, reasons=tuple(reasons))
        elif (
            publication_scope is PublicationCoverageScope.BUSINESS_HISTORY
            and not publication_spans_scope
        ):
            reasons.append(
                "The selected publication lacks evidence that its scan scope covers the "
                "evaluated interval."
            )
            coverage = replace(coverage, status=CoverageStatus.UNKNOWN, reasons=tuple(reasons))
        elif (
            publication_scope is PublicationCoverageScope.ARCHIVED_SNAPSHOT_OBSERVATIONS
            and not publication_spans_scope
            and coverage.status is not CoverageStatus.SOURCE_LIMITED
        ):
            reasons.append(
                "The archived-snapshot publication scope does not cover the evaluated interval."
            )
            coverage = replace(coverage, status=CoverageStatus.UNKNOWN, reasons=tuple(reasons))

        return InstrumentHistoryCoverageReport(
            schema_version=1,
            dataset_id=query.dataset_id,
            source_id=query.source_id,
            input_identifier=query.instrument_identifier,
            instrument_id=resolved.instrument_id,
            requested_start=query.start,
            requested_end_exclusive=query.end_exclusive,
            evaluated_start=reported_evaluated_start,
            evaluated_end_exclusive=reported_evaluated_end_exclusive,
            registry_version=resolved.registry_version,
            registry_evidence_ref=resolved.registry_evidence_ref,
            lifecycle_version=lifecycle.lifecycle_version,
            lifecycle_status=lifecycle_status,
            lifecycle_status_as_of=lifecycle.status_as_of,
            lifecycle_status_evidence_ref=lifecycle.status_evidence_ref,
            known_start_date=lifecycle.known_start_date,
            start_boundary_unknown=lifecycle.start_boundary_unknown,
            start_evidence_ref=lifecycle.start_evidence_ref,
            delisted_on=lifecycle.delisted_on,
            delist_evidence_ref=lifecycle.delist_evidence_ref,
            alias_version=resolved.alias_version,
            alias_evidence_ref=resolved.alias_evidence_ref,
            alias_valid_from=resolved.alias_valid_from,
            alias_valid_through=resolved.alias_valid_through,
            alias_resolved_as_of=resolved.alias_resolved_as_of,
            calendar_id=calendar.calendar_id,
            calendar_version=calendar.calendar_version,
            calendar_evidence_ref=calendar.evidence_ref,
            calendar_covered_from=calendar.covered_from,
            calendar_covered_through=calendar.covered_through,
            calendar_daily_classification_complete=calendar.daily_classification_complete,
            publication_id=publication.publication_id,
            publication_version=publication.publication_version,
            publication_evidence_ref=publication.evidence_ref,
            publication_scope=publication_scope,
            publication_scope_start=publication.scope_start,
            publication_scope_end_exclusive=publication.scope_end_exclusive,
            publication_scope_evidence_ref=publication.scope_evidence_ref,
            coverage=coverage,
            reasons=tuple(reasons),
        )


__all__ = [
    "CalendarEvidence",
    "CoverageQuery",
    "InstrumentCoverageService",
    "InstrumentHistoryCoverageReport",
    "PublicationCoverageScope",
    "PublicationObservations",
]
