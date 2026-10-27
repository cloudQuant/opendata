"""Assess daily data coverage for a bounded, half-open query window.

This module deliberately keeps request-window coverage separate from
``history_coverage``. A caller may receive a complete local window even when
the declared instrument-history boundary begins earlier; the result never
claims that the request is complete since inception.

All expected observation dates are derived from caller-declared calendar and
instrument-specific suspension claims. Observed rows never establish a
listing boundary, a calendar, or the absence of a suspension.

The values called evidence references in this module are opaque caller
assertions. The evaluator checks their syntax and presence, but does not resolve
or authenticate them. A consumer must bind every identity and evidence claim
to an immutable trusted resolver before treating a result as authoritative.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from itertools import islice

MAX_QUERY_DATES = 100_000


class QueryCoverageStatus(str, Enum):
    """Conclusion conditional on the caller-declared claims for this interval."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    SOURCE_LIMITED = "SOURCE_LIMITED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class SourceRetentionStatus(str, Enum):
    """How declared source retention relates to the evaluated query window."""

    UNKNOWN = "UNKNOWN"
    UNBOUNDED = "UNBOUNDED"
    COVERS_WINDOW = "COVERS_WINDOW"
    DOES_NOT_COVER_WINDOW = "DOES_NOT_COVER_WINDOW"


class UnsupportedQueryFrequencyError(ValueError):
    """Raised when an unsupported time-series frequency is requested."""


def _strict_date(value: date | None, name: str) -> date | None:
    """Accept date-only values and reject timestamps and serialized strings."""
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a datetime.date, not a timestamp or string")
    return value


def _required_label(value: str, name: str) -> str:
    """Return a normalized nonempty evidence or identity label."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _bounded_dates(values: Iterable[date], name: str, *, unique: bool) -> tuple[date, ...]:
    """Materialize a bounded date iterable while preserving observed duplicates."""
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError(f"{name} must be an iterable of datetime.date values")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be an iterable of datetime.date values") from exc
    items = tuple(islice(iterator, MAX_QUERY_DATES + 1))
    if len(items) > MAX_QUERY_DATES:
        raise ValueError(f"{name} exceeds the {MAX_QUERY_DATES:,}-date evaluation limit")
    for index, item in enumerate(items):
        if item is None:
            raise ValueError(f"{name}[{index}] must be a datetime.date value")
        _strict_date(item, f"{name}[{index}]")
    if unique and len(items) != len(set(items)):
        raise ValueError(f"{name} must not contain duplicate dates")
    return items


def _half_open_bounds(
    start: date | None, end_exclusive: date | None, name: str
) -> tuple[date | None, date | None]:
    """Validate an optional nonempty half-open interval."""
    left = _strict_date(start, f"{name}_start")
    right = _strict_date(end_exclusive, f"{name}_end_exclusive")
    if (left is None) != (right is None):
        raise ValueError(f"{name} requires both half-open bounds")
    if left is not None and right is not None and left >= right:
        raise ValueError(f"{name} requires start < end_exclusive")
    return left, right


@dataclass(frozen=True, slots=True)
class QueryWindowCoverageRequest:
    """Caller-declared evidence and observations for one daily query window.

    The requested date interval is ``[start, end_exclusive)``. All identifiers
    and evidence references are unverified strings until a consumer resolves
    them against an immutable trusted registry.

    ``calendar_open_dates`` is a caller-declared extract from a versioned calendar.
    ``suspension_dates`` is a complete instrument-specific classification only
    inside its declared half-open interval. Open dates outside that interval
    remain unclassified and can never be treated as ordinary expected rows.
    A missing suspension record is therefore not interpreted as "not
    suspended".

    ``last_observable_business_date`` means a caller-declared final business
    date on which this instrument can produce observations. It must not be
    inferred from the current query's last returned row or from a delisting
    announcement alone. Source-retention bounds describe the half-open
    interval ``[source_retained_from, source_retained_before)``; an omitted
    edge is unbounded only when a caller-declared reference supports that
    policy. With no retention assertion, retention is unknown.

    The evaluated interval is the intersection of the requested interval and
    the caller-declared instrument lifetime. Any clipping at either lifecycle
    boundary is reported separately and prevents a ``COMPLETE`` result for
    the requested interval.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    start: date
    end_exclusive: date
    observed_dates: Iterable[date]
    calendar_id: str
    calendar_version: str
    calendar_evidence_ref: str
    calendar_covered_from: date
    calendar_covered_before: date
    calendar_open_dates: Iterable[date]
    calendar_daily_classification_complete: bool
    listing_date: date | None
    listing_boundary_unknown: bool
    listing_evidence_ref: str | None
    last_observable_business_date: date | None = None
    last_observable_evidence_ref: str | None = None
    suspension_covered_from: date | None = None
    suspension_covered_before: date | None = None
    suspension_dates: Iterable[date] = ()
    suspension_evidence_ref: str | None = None
    source_retained_from: date | None = None
    source_retained_before: date | None = None
    source_retention_evidence_ref: str | None = None
    source_retention_unbounded: bool = False
    frequency_semantics: str = "daily_business_date"

    def __post_init__(self) -> None:
        """Validate reference presence, half-open intervals, and bounded dates."""
        for name in ("dataset_id", "source_id", "instrument_id", "calendar_id", "calendar_version"):
            object.__setattr__(self, name, _required_label(getattr(self, name), name))
        object.__setattr__(
            self,
            "calendar_evidence_ref",
            _required_label(self.calendar_evidence_ref, "calendar_evidence_ref"),
        )

        start = _strict_date(self.start, "start")
        end = _strict_date(self.end_exclusive, "end_exclusive")
        if start is None or end is None or start >= end:
            raise ValueError("query window requires start < end_exclusive")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end_exclusive", end)

        calendar_from, calendar_before = _half_open_bounds(
            self.calendar_covered_from, self.calendar_covered_before, "calendar_coverage"
        )
        if calendar_from is None or calendar_before is None:
            raise ValueError("calendar coverage bounds are required")
        object.__setattr__(self, "calendar_covered_from", calendar_from)
        object.__setattr__(self, "calendar_covered_before", calendar_before)
        if type(self.calendar_daily_classification_complete) is not bool:
            raise ValueError("calendar_daily_classification_complete must be a bool")

        if type(self.listing_boundary_unknown) is not bool:
            raise ValueError("listing_boundary_unknown must be a bool")
        listing = _strict_date(self.listing_date, "listing_date")
        object.__setattr__(self, "listing_date", listing)
        if self.listing_boundary_unknown:
            if listing is not None or self.listing_evidence_ref is not None:
                raise ValueError("unknown listing boundary cannot include a date or evidence")
        elif listing is None or self.listing_evidence_ref is None:
            raise ValueError(
                "known listing boundary requires date and a caller-declared evidence ref"
            )
        if self.listing_evidence_ref is not None:
            object.__setattr__(
                self,
                "listing_evidence_ref",
                _required_label(self.listing_evidence_ref, "listing_evidence_ref"),
            )

        last_observable = _strict_date(
            self.last_observable_business_date, "last_observable_business_date"
        )
        object.__setattr__(self, "last_observable_business_date", last_observable)
        if (last_observable is None) != (self.last_observable_evidence_ref is None):
            raise ValueError(
                "last_observable_business_date and its evidence must be supplied together"
            )
        if self.last_observable_evidence_ref is not None:
            object.__setattr__(
                self,
                "last_observable_evidence_ref",
                _required_label(self.last_observable_evidence_ref, "last_observable_evidence_ref"),
            )
        if listing is not None and last_observable is not None and listing > last_observable:
            raise ValueError("listing_date cannot follow last_observable_business_date")

        suspension_from, suspension_before = _half_open_bounds(
            self.suspension_covered_from,
            self.suspension_covered_before,
            "suspension_coverage",
        )
        object.__setattr__(self, "suspension_covered_from", suspension_from)
        object.__setattr__(self, "suspension_covered_before", suspension_before)
        suspension_dates = _bounded_dates(self.suspension_dates, "suspension_dates", unique=True)
        if (suspension_from is None) != (self.suspension_evidence_ref is None):
            raise ValueError("suspension coverage bounds and evidence must be supplied together")
        if self.suspension_evidence_ref is not None:
            object.__setattr__(
                self,
                "suspension_evidence_ref",
                _required_label(self.suspension_evidence_ref, "suspension_evidence_ref"),
            )
            if suspension_from is None or suspension_before is None:
                raise ValueError("suspension evidence requires both half-open bounds")
            if any(not suspension_from <= day < suspension_before for day in suspension_dates):
                raise ValueError(
                    "suspension_dates must fall inside the declared suspension interval"
                )
        elif suspension_dates:
            raise ValueError(
                "suspension_dates require caller-declared suspension coverage evidence"
            )
        object.__setattr__(self, "suspension_dates", suspension_dates)

        # A provider may disclose only one edge of its retention policy, such
        # as a rolling lower bound with no known upper bound.
        retained_from = _strict_date(self.source_retained_from, "source_retained_from")
        retained_before = _strict_date(self.source_retained_before, "source_retained_before")
        if type(self.source_retention_unbounded) is not bool:
            raise ValueError("source_retention_unbounded must be a bool")
        object.__setattr__(self, "source_retained_from", retained_from)
        object.__setattr__(self, "source_retained_before", retained_before)
        if (
            retained_from is not None
            and retained_before is not None
            and retained_from >= retained_before
        ):
            raise ValueError("source retention requires from < before")
        has_retention = retained_from is not None or retained_before is not None
        if self.source_retention_unbounded:
            if has_retention:
                raise ValueError("unbounded retention cannot also declare finite boundaries")
            if self.source_retention_evidence_ref is None:
                raise ValueError(
                    "unbounded retention requires a caller-declared evidence reference"
                )
        elif has_retention != (self.source_retention_evidence_ref is not None):
            raise ValueError("source retention bounds and evidence must be supplied together")
        if self.source_retention_evidence_ref is not None:
            object.__setattr__(
                self,
                "source_retention_evidence_ref",
                _required_label(
                    self.source_retention_evidence_ref, "source_retention_evidence_ref"
                ),
            )

        if self.frequency_semantics != "daily_business_date":
            raise UnsupportedQueryFrequencyError(
                "only daily_business_date is supported by this query-window evaluator"
            )

        open_dates = _bounded_dates(self.calendar_open_dates, "calendar_open_dates", unique=True)
        if any(not calendar_from <= day < calendar_before for day in open_dates):
            raise ValueError("calendar_open_dates must fall inside declared calendar coverage")
        if not set(suspension_dates).issubset(open_dates):
            raise ValueError("suspension_dates must be dates classified as open by the calendar")
        if last_observable is not None and calendar_from <= last_observable < calendar_before:
            if last_observable not in open_dates:
                raise ValueError(
                    "last_observable_business_date must be an open date in the declared calendar"
                )
            if last_observable in suspension_dates:
                raise ValueError("last_observable_business_date cannot be declared suspended")
        observed = _bounded_dates(self.observed_dates, "observed_dates", unique=False)
        object.__setattr__(self, "calendar_open_dates", open_dates)
        object.__setattr__(self, "observed_dates", observed)


@dataclass(frozen=True, slots=True)
class QueryWindowCoverageReport:
    """Immutable query-window assessment with gaps and per-side lifecycle clipping."""

    schema_version: int
    dataset_id: str
    source_id: str
    instrument_id: str
    status: QueryCoverageStatus
    requested_start: date
    requested_end_exclusive: date
    evaluated_start: date
    evaluated_end_exclusive: date
    listing_date: date | None
    listing_boundary_unknown: bool
    listing_evidence_ref: str | None
    last_observable_business_date: date | None
    last_observable_evidence_ref: str | None
    lifecycle_start_truncated: bool
    lifecycle_start_truncation_reason: str | None
    lifecycle_end_truncated: bool
    lifecycle_end_truncation_reason: str | None
    lifecycle_truncated: bool
    lifecycle_truncation_reason: str | None
    calendar_id: str
    calendar_version: str
    calendar_evidence_ref: str
    calendar_covers_evaluated_window: bool
    suspension_evidence_ref: str | None
    expected_dates: tuple[date, ...]
    unclassified_open_dates: tuple[date, ...]
    unclassified_observed_dates: tuple[date, ...]
    observed_row_count: int
    observed_unique_date_count: int
    covered_expected_date_count: int
    head_gaps: tuple[date, ...]
    interior_gaps: tuple[date, ...]
    tail_gaps: tuple[date, ...]
    duplicate_dates: tuple[date, ...]
    unexpected_dates: tuple[date, ...]
    source_retained_from: date | None
    source_retained_before: date | None
    source_retention_evidence_ref: str | None
    source_retention_unbounded: bool
    source_retention_status: SourceRetentionStatus
    source_retention_covers_evaluated_window: bool | None
    source_limited_missing_dates: tuple[date, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Serialize the assessment with exact half-open boundaries."""

        def date_value(value: date | None) -> str | None:
            return value.isoformat() if value is not None else None

        def date_values(values: tuple[date, ...]) -> list[str]:
            return [value.isoformat() for value in values]

        return {
            "schema_version": self.schema_version,
            "scope": "query_window",
            "evidence_binding": {
                "status": "unverified_caller_assertion",
                "trusted_resolver_required": True,
                "assessment_is_authoritative": False,
            },
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "status": self.status.value,
            "requested_window": {
                "start": self.requested_start.isoformat(),
                "end_exclusive": self.requested_end_exclusive.isoformat(),
            },
            "evaluated_window": {
                "start": self.evaluated_start.isoformat(),
                "end_exclusive": self.evaluated_end_exclusive.isoformat(),
            },
            "listing": {
                "date": date_value(self.listing_date),
                "boundary_unknown": self.listing_boundary_unknown,
                "evidence_ref": self.listing_evidence_ref,
            },
            "lifecycle_boundary": {
                "last_observable_business_date": date_value(self.last_observable_business_date),
                "evidence_ref": self.last_observable_evidence_ref,
                "start_truncated": self.lifecycle_start_truncated,
                "start_truncation_reason": self.lifecycle_start_truncation_reason,
                "start_boundary": {
                    "date": date_value(self.listing_date),
                    "evidence_ref": self.listing_evidence_ref,
                },
                "end_truncated": self.lifecycle_end_truncated,
                "end_truncation_reason": self.lifecycle_end_truncation_reason,
                "end_boundary": {
                    "date": date_value(self.last_observable_business_date),
                    "evidence_ref": self.last_observable_evidence_ref,
                },
                "truncated": self.lifecycle_truncated,
                "truncation_reason": self.lifecycle_truncation_reason,
            },
            "calendar": {
                "id": self.calendar_id,
                "version": self.calendar_version,
                "evidence_ref": self.calendar_evidence_ref,
                "covers_evaluated_window": self.calendar_covers_evaluated_window,
            },
            "suspension_evidence_ref": self.suspension_evidence_ref,
            "counts": {
                "expected_dates": len(self.expected_dates),
                "observed_rows": self.observed_row_count,
                "observed_unique_dates": self.observed_unique_date_count,
                "covered_expected_dates": self.covered_expected_date_count,
                "unclassified_open_dates": len(self.unclassified_open_dates),
            },
            "expected_dates": date_values(self.expected_dates),
            "unclassified_open_dates": date_values(self.unclassified_open_dates),
            "unclassified_observed_dates": date_values(self.unclassified_observed_dates),
            "gaps": {
                "head": date_values(self.head_gaps),
                "interior": date_values(self.interior_gaps),
                "tail": date_values(self.tail_gaps),
            },
            "duplicate_dates": date_values(self.duplicate_dates),
            "unexpected_dates": date_values(self.unexpected_dates),
            "source_retention": {
                "from": date_value(self.source_retained_from),
                "before": date_value(self.source_retained_before),
                "evidence_ref": self.source_retention_evidence_ref,
                "unbounded": self.source_retention_unbounded,
                "assessment": self.source_retention_status.value,
                "covers_evaluated_window": self.source_retention_covers_evaluated_window,
                "missing_dates_outside_retention": date_values(self.source_limited_missing_dates),
            },
            "reasons": list(self.reasons),
        }


def evaluate_query_window_coverage(
    request: QueryWindowCoverageRequest,
) -> QueryWindowCoverageReport:
    """Reconcile observations against caller-declared expected dates in ``[start, end)``.

    The evaluated interval is the intersection of the request and the known
    caller-declared instrument lifetime. If there is no intersection, the
    evaluated interval is an empty interval at the nearest request boundary.
    Any difference between the requested and evaluated intervals prevents a
    ``COMPLETE`` result, even when all rows in the clipped interval are present.
    A complete, unclipped result is query-scoped and makes no since-listing
    history claim. Unknown listing, calendar, suspension, or source-retention
    scope also prevents ``COMPLETE``. An empty result set is never
    ``COMPLETE``. Evidence references remain unverified strings.
    """
    if not isinstance(request, QueryWindowCoverageRequest):
        raise TypeError("request must be a QueryWindowCoverageRequest")

    evaluated_start = request.start
    evaluated_end = request.end_exclusive
    lifecycle_start_truncated = False
    lifecycle_start_truncation_reason: str | None = None
    lifecycle_end_truncated = False
    lifecycle_end_truncation_reason: str | None = None

    if request.listing_date is not None and request.listing_date > request.start:
        lifecycle_start_truncated = True
        if request.listing_date < request.end_exclusive:
            evaluated_start = request.listing_date
            lifecycle_start_truncation_reason = (
                "Requested window starts before the caller-declared listing date; "
                "pre-listing dates are excluded from expected observations."
            )
        else:
            # The whole request precedes listing. Represent the empty
            # intersection at the request's exclusive right edge.
            evaluated_start = request.end_exclusive
            evaluated_end = request.end_exclusive
            lifecycle_start_truncation_reason = (
                "The entire requested window ends on or before the caller-declared listing "
                "date; no requested date is within the declared instrument life."
            )

    if (
        request.last_observable_business_date is not None
        and request.last_observable_business_date < date.max
    ):
        day_after_last_observable = request.last_observable_business_date + timedelta(days=1)
        if day_after_last_observable < request.end_exclusive:
            lifecycle_end_truncated = True
            if day_after_last_observable > request.start:
                evaluated_end = min(evaluated_end, day_after_last_observable)
                lifecycle_end_truncation_reason = (
                    "Requested window extends beyond the caller-declared last observable "
                    "business date; later dates are excluded from expected observations."
                )
            else:
                # The whole request follows the last observable date.
                evaluated_start = request.start
                evaluated_end = request.start
                lifecycle_end_truncation_reason = (
                    "The entire requested window starts after the caller-declared last "
                    "observable business date; no requested date is within the declared "
                    "instrument life."
                )

    lifecycle_truncated = evaluated_start != request.start or evaluated_end != request.end_exclusive
    truncation_reasons = tuple(
        reason
        for reason in (
            lifecycle_start_truncation_reason,
            lifecycle_end_truncation_reason,
        )
        if reason is not None
    )
    lifecycle_truncation_reason = " ".join(truncation_reasons) or None

    overlaps_lifecycle = evaluated_start < evaluated_end
    calendar_covers = (
        request.calendar_daily_classification_complete
        and request.calendar_covered_from <= evaluated_start
        and request.calendar_covered_before >= evaluated_end
    )
    open_dates = tuple(
        sorted(
            day
            for day in request.calendar_open_dates
            if overlaps_lifecycle and evaluated_start <= day < evaluated_end
        )
    )
    suspension_scope_start = request.suspension_covered_from
    suspension_scope_end = request.suspension_covered_before
    known_suspension_scope_dates = tuple(
        day
        for day in open_dates
        if suspension_scope_start is not None
        and suspension_scope_end is not None
        and suspension_scope_start <= day < suspension_scope_end
    )
    suspended = set(request.suspension_dates)
    expected = tuple(day for day in known_suspension_scope_dates if day not in suspended)
    unclassified = tuple(day for day in open_dates if day not in known_suspension_scope_dates)

    observed_rows = tuple(request.observed_dates)
    observed_counts = Counter(observed_rows)
    observed_set = set(observed_counts)
    expected_set = set(expected)
    unclassified_set = set(unclassified)
    missing_set = expected_set - observed_set
    covered = expected_set & observed_set
    duplicates = tuple(sorted(day for day, count in observed_counts.items() if count > 1))
    unexpected = tuple(
        sorted(
            day
            for day in observed_set
            if not request.start <= day < request.end_exclusive
            or (day not in expected_set and day not in unclassified_set)
        )
    )
    unclassified_observed = tuple(sorted(observed_set & unclassified_set))
    missing = tuple(sorted(missing_set))
    if covered:
        first_covered = min(covered)
        last_covered = max(covered)
        head_gaps = tuple(day for day in missing if day < first_covered)
        interior_gaps = tuple(day for day in missing if first_covered < day < last_covered)
        tail_gaps = tuple(day for day in missing if day > last_covered)
    else:
        head_gaps = missing
        interior_gaps = ()
        tail_gaps = missing

    source_limited_missing = tuple(
        day
        for day in missing
        if (request.source_retained_from is not None and day < request.source_retained_from)
        or (request.source_retained_before is not None and day >= request.source_retained_before)
    )
    if request.source_retention_unbounded:
        source_retention_status = SourceRetentionStatus.UNBOUNDED
        source_retention_covers_window: bool | None = True
    elif request.source_retention_evidence_ref is None:
        source_retention_status = SourceRetentionStatus.UNKNOWN
        source_retention_covers_window = None
    else:
        source_retention_covers_window = (
            request.source_retained_from is None or request.source_retained_from <= evaluated_start
        ) and (
            request.source_retained_before is None
            or request.source_retained_before >= evaluated_end
        )
        source_retention_status = (
            SourceRetentionStatus.COVERS_WINDOW
            if source_retention_covers_window
            else SourceRetentionStatus.DOES_NOT_COVER_WINDOW
        )

    reasons: list[str] = []
    listing_known = not request.listing_boundary_unknown
    if not listing_known:
        reasons.append("Independent instrument listing date is unknown.")
    if not calendar_covers:
        reasons.append(
            "The caller-declared versioned calendar does not completely classify "
            "the evaluated window."
        )
    if unclassified:
        reasons.append(
            f"{len(unclassified)} open calendar date(s) lack complete instrument-specific "
            "suspension classification; they are neither assumed expected nor treated as gaps."
        )
    if unclassified_observed:
        reasons.append(
            f"{len(unclassified_observed)} observed date(s) also lack complete suspension "
            "classification and cannot certify the expected-date schedule."
        )
    if not overlaps_lifecycle:
        reasons.append("The request does not overlap the caller-declared instrument life.")
    if lifecycle_truncated:
        reasons.append(
            "The evaluated window was clipped at a caller-declared instrument lifecycle "
            "boundary; the boundary and reference are assertions until resolved."
        )
    if missing:
        reasons.append(f"{len(missing)} expected observation date(s) have no row.")
    if duplicates:
        reasons.append(f"{len(duplicates)} observed date(s) have duplicate rows.")
    if unexpected:
        reasons.append(
            f"{len(unexpected)} observed date(s) are outside the caller-declared expected dates "
            "or requested half-open interval."
        )
    if source_limited_missing:
        reasons.append(
            f"{len(source_limited_missing)} missing expected date(s) fall outside the "
            "caller-declared source-retention interval."
        )
    if source_retention_status is SourceRetentionStatus.UNKNOWN:
        reasons.append(
            "Source retention is unknown; caller-provided rows alone cannot establish retention "
            "for the full evaluated window."
        )
    elif source_retention_status is SourceRetentionStatus.DOES_NOT_COVER_WINDOW:
        reasons.append(
            "The caller-declared source-retention interval does not cover the evaluated window."
        )

    if not overlaps_lifecycle:
        status = (
            QueryCoverageStatus.PARTIAL if observed_rows else QueryCoverageStatus.NOT_APPLICABLE
        )
    elif (
        not listing_known
        or not calendar_covers
        or source_retention_status is SourceRetentionStatus.UNKNOWN
    ):
        status = QueryCoverageStatus.UNKNOWN
    elif (
        source_retention_status is SourceRetentionStatus.DOES_NOT_COVER_WINDOW
        or source_limited_missing
    ):
        status = QueryCoverageStatus.SOURCE_LIMITED
    elif missing or duplicates or unexpected:
        status = QueryCoverageStatus.PARTIAL
    elif unclassified:
        status = QueryCoverageStatus.UNKNOWN
    elif lifecycle_truncated:
        status = QueryCoverageStatus.PARTIAL
        reasons.append(
            "The evaluated interval is not the full requested interval; lifecycle clipping "
            "cannot certify the requested window as complete."
        )
    elif not expected:
        status = QueryCoverageStatus.NOT_APPLICABLE
        reasons.append("No observation is expected in this window; empty results are not complete.")
    else:
        status = QueryCoverageStatus.COMPLETE
        reasons.append(
            "Every caller-declared expected observation date in the evaluated window appears "
            "exactly once. The result is query-scoped and its evidence references remain "
            "unverified caller assertions."
        )

    return QueryWindowCoverageReport(
        schema_version=1,
        dataset_id=request.dataset_id,
        source_id=request.source_id,
        instrument_id=request.instrument_id,
        status=status,
        requested_start=request.start,
        requested_end_exclusive=request.end_exclusive,
        evaluated_start=evaluated_start,
        evaluated_end_exclusive=evaluated_end,
        listing_date=request.listing_date,
        listing_boundary_unknown=request.listing_boundary_unknown,
        listing_evidence_ref=request.listing_evidence_ref,
        last_observable_business_date=request.last_observable_business_date,
        last_observable_evidence_ref=request.last_observable_evidence_ref,
        lifecycle_start_truncated=lifecycle_start_truncated,
        lifecycle_start_truncation_reason=lifecycle_start_truncation_reason,
        lifecycle_end_truncated=lifecycle_end_truncated,
        lifecycle_end_truncation_reason=lifecycle_end_truncation_reason,
        lifecycle_truncated=lifecycle_truncated,
        lifecycle_truncation_reason=lifecycle_truncation_reason,
        calendar_id=request.calendar_id,
        calendar_version=request.calendar_version,
        calendar_evidence_ref=request.calendar_evidence_ref,
        calendar_covers_evaluated_window=calendar_covers,
        suspension_evidence_ref=request.suspension_evidence_ref,
        expected_dates=expected,
        unclassified_open_dates=unclassified,
        unclassified_observed_dates=unclassified_observed,
        observed_row_count=len(observed_rows),
        observed_unique_date_count=len(observed_set),
        covered_expected_date_count=len(covered),
        head_gaps=head_gaps,
        interior_gaps=interior_gaps,
        tail_gaps=tail_gaps,
        duplicate_dates=duplicates,
        unexpected_dates=unexpected,
        source_retained_from=request.source_retained_from,
        source_retained_before=request.source_retained_before,
        source_retention_evidence_ref=request.source_retention_evidence_ref,
        source_retention_unbounded=request.source_retention_unbounded,
        source_retention_status=source_retention_status,
        source_retention_covers_evaluated_window=source_retention_covers_window,
        source_limited_missing_dates=source_limited_missing,
        reasons=tuple(reasons),
    )


__all__ = [
    "QueryCoverageStatus",
    "QueryWindowCoverageReport",
    "QueryWindowCoverageRequest",
    "SourceRetentionStatus",
    "UnsupportedQueryFrequencyError",
    "evaluate_query_window_coverage",
]
