"""Evaluate full-history coverage for a daily business-date series.

The evaluator compares explicitly supplied expected business dates with the
observed dates for one dataset/source/instrument (or business key). It never
infers an instrument's inception date or a calendar from the observations.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import islice

MAX_DAILY_DATES = 100_000


class FrequencySemantics(str, Enum):
    """Supported frequency semantic for this first evaluator version."""

    DAILY_BUSINESS_DATE = "daily_business_date"


class CoverageStatus(str, Enum):
    """Conclusion for the caller's requested historical scope."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"
    SOURCE_LIMITED = "SOURCE_LIMITED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class UnsupportedFrequencyError(ValueError):
    """Raised when a frequency other than daily business dates is requested."""


def _require_label(value: str, name: str) -> str:
    """Return a stripped nonempty label or reject an invalid identifier."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _require_date(value: date | None, name: str) -> date | None:
    """Reject timestamps and strings where a business date is required."""
    if value is None:
        return None
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a datetime.date, not a timestamp or string")
    return value


def _bounded_dates(values: Iterable[date], name: str) -> tuple[date, ...]:
    """Materialize at most ``MAX_DAILY_DATES`` dates from a one-shot iterable."""
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError(f"{name} must be an iterable of datetime.date values")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be an iterable of datetime.date values") from exc

    items = tuple(islice(iterator, MAX_DAILY_DATES + 1))
    if len(items) > MAX_DAILY_DATES:
        raise ValueError(f"{name} exceeds the {MAX_DAILY_DATES:,}-date evaluation limit")
    for index, item in enumerate(items):
        if item is None:
            raise ValueError(f"{name}[{index}] must be a datetime.date value")
        _require_date(item, f"{name}[{index}]")
    return items


@dataclass(frozen=True, slots=True)
class HistoryCoverageRequest:
    """Inputs for evaluating one daily dataset series over an explicit date range.

    ``expected_business_dates`` must come from a trusted instrument-aware
    business calendar. ``observed_business_dates`` contains one date per row;
    repeating a date therefore identifies duplicate daily keys for this series.
    """

    dataset: str
    source: str
    request_start: date
    request_end: date
    expected_business_dates: Iterable[date]
    observed_business_dates: Iterable[date]
    instrument_key: str | None = None
    business_key: str | None = None
    frequency_semantics: FrequencySemantics | str = FrequencySemantics.DAILY_BUSINESS_DATE
    calendar_id: str | None = None
    calendar_version: str | None = None
    business_calendar_evidence: str | None = None
    calendar_covered_from: date | None = None
    calendar_covered_through: date | None = None
    known_start_date: date | None = None
    start_boundary_unknown: bool = True
    independent_start_evidence: str | None = None
    source_retained_from: date | None = None
    source_retained_through: date | None = None
    source_retention_evidence: str | None = None
    snapshot_only: bool = False
    snapshot_archive_from: date | None = None
    snapshot_archive_through: date | None = None
    snapshot_archive_evidence: str | None = None

    def __post_init__(self) -> None:
        """Validate identifiers, bounds, evidence, and bounded date iterables."""
        object.__setattr__(self, "dataset", _require_label(self.dataset, "dataset"))
        object.__setattr__(self, "source", _require_label(self.source, "source"))
        for field_name in ("calendar_id", "calendar_version", "business_calendar_evidence"):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _require_label(value, field_name))
        if (self.instrument_key is None) == (self.business_key is None):
            raise ValueError("provide exactly one of instrument_key or business_key")
        if self.instrument_key is not None:
            object.__setattr__(
                self, "instrument_key", _require_label(self.instrument_key, "instrument_key")
            )
        if self.business_key is not None:
            object.__setattr__(
                self, "business_key", _require_label(self.business_key, "business_key")
            )

        start = _require_date(self.request_start, "request_start")
        end = _require_date(self.request_end, "request_end")
        if start is None or end is None or start > end:
            raise ValueError("request_start must be on or before request_end")
        object.__setattr__(self, "request_start", start)
        object.__setattr__(self, "request_end", end)

        calendar_from = _require_date(self.calendar_covered_from, "calendar_covered_from")
        calendar_through = _require_date(self.calendar_covered_through, "calendar_covered_through")
        object.__setattr__(self, "calendar_covered_from", calendar_from)
        object.__setattr__(self, "calendar_covered_through", calendar_through)
        if (
            calendar_from is not None
            and calendar_through is not None
            and calendar_from > calendar_through
        ):
            raise ValueError("calendar_covered_from must be on or before calendar_covered_through")

        try:
            frequency = FrequencySemantics(self.frequency_semantics)
        except (TypeError, ValueError) as exc:
            raise UnsupportedFrequencyError(
                "only daily_business_date is supported; intraday coverage is not supported"
            ) from exc
        object.__setattr__(self, "frequency_semantics", frequency)

        known_start = _require_date(self.known_start_date, "known_start_date")
        retained_from = _require_date(self.source_retained_from, "source_retained_from")
        retained_through = _require_date(self.source_retained_through, "source_retained_through")
        object.__setattr__(self, "known_start_date", known_start)
        object.__setattr__(self, "source_retained_from", retained_from)
        object.__setattr__(self, "source_retained_through", retained_through)

        if type(self.start_boundary_unknown) is not bool:
            raise ValueError("start_boundary_unknown must be a bool")
        if self.start_boundary_unknown:
            if known_start is not None or self.independent_start_evidence is not None:
                raise ValueError(
                    "unknown start boundary cannot include a known date or start evidence"
                )
        else:
            if known_start is None:
                raise ValueError("known_start_date is required when the start boundary is known")
            if self.independent_start_evidence is None:
                raise ValueError(
                    "independent_start_evidence is required for a known start boundary"
                )
            object.__setattr__(
                self,
                "independent_start_evidence",
                _require_label(self.independent_start_evidence, "independent_start_evidence"),
            )

        has_retention_bound = retained_from is not None or retained_through is not None
        if has_retention_bound:
            if self.source_retention_evidence is None:
                raise ValueError("source_retention_evidence is required for a retention boundary")
            object.__setattr__(
                self,
                "source_retention_evidence",
                _require_label(self.source_retention_evidence, "source_retention_evidence"),
            )
        elif self.source_retention_evidence is not None:
            raise ValueError("source_retention_evidence requires a retention boundary")
        if (
            retained_from is not None
            and retained_through is not None
            and retained_from > retained_through
        ):
            raise ValueError("source_retained_from must be on or before source_retained_through")
        if (
            known_start is not None
            and retained_through is not None
            and retained_through < known_start
        ):
            raise ValueError("source retention ends before the independently evidenced start")
        if type(self.snapshot_only) is not bool:
            raise ValueError("snapshot_only must be a bool")

        archive_from = _require_date(self.snapshot_archive_from, "snapshot_archive_from")
        archive_through = _require_date(self.snapshot_archive_through, "snapshot_archive_through")
        object.__setattr__(self, "snapshot_archive_from", archive_from)
        object.__setattr__(self, "snapshot_archive_through", archive_through)
        has_archive_bound = archive_from is not None or archive_through is not None
        if has_archive_bound:
            if not self.snapshot_only:
                raise ValueError("snapshot archive boundaries require snapshot_only=True")
            if archive_from is None or archive_through is None:
                raise ValueError("snapshot archive requires both from and through boundaries")
            if self.snapshot_archive_evidence is None:
                raise ValueError("snapshot_archive_evidence is required for archive boundaries")
            object.__setattr__(
                self,
                "snapshot_archive_evidence",
                _require_label(self.snapshot_archive_evidence, "snapshot_archive_evidence"),
            )
            if archive_from > archive_through:
                raise ValueError(
                    "snapshot_archive_from must be on or before snapshot_archive_through"
                )
        elif self.snapshot_archive_evidence is not None:
            raise ValueError("snapshot_archive_evidence requires archive boundaries")

        expected = _bounded_dates(self.expected_business_dates, "expected_business_dates")
        if len(set(expected)) != len(expected):
            raise ValueError("expected_business_dates must not contain duplicates")
        observed = _bounded_dates(self.observed_business_dates, "observed_business_dates")

        object.__setattr__(self, "expected_business_dates", expected)
        object.__setattr__(self, "observed_business_dates", observed)


@dataclass(frozen=True, slots=True)
class HistoryCoverageReport:
    """Immutable coverage result with one canonical dictionary/JSON form."""

    dataset: str
    source: str
    instrument_key: str | None
    business_key: str | None
    frequency_semantics: FrequencySemantics
    calendar_id: str | None
    calendar_version: str | None
    business_calendar_evidence: str | None
    calendar_covered_from: date | None
    calendar_covered_through: date | None
    business_calendar_covers_request: bool
    status: CoverageStatus
    requested_start: date
    requested_end: date
    known_start_date: date | None
    start_boundary_unknown: bool
    independent_start_evidence: str | None
    source_retained_from: date | None
    source_retained_through: date | None
    source_retention_evidence: str | None
    snapshot_only: bool
    snapshot_archive_from: date | None
    snapshot_archive_through: date | None
    snapshot_archive_evidence: str | None
    scope_covers_known_start: bool
    expected_dates: tuple[date, ...]
    observed_row_count: int
    observed_unique_date_count: int
    covered_expected_date_count: int
    head_gaps: tuple[date, ...]
    interior_gaps: tuple[date, ...]
    tail_gaps: tuple[date, ...]
    duplicate_dates: tuple[date, ...]
    unexpected_dates: tuple[date, ...]
    unarchived_dates: tuple[date, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Serialize dates and enums to the stable report schema."""

        def date_values(values: tuple[date, ...]) -> list[str]:
            return [value.isoformat() for value in values]

        return {
            "schema_version": 1,
            "dataset": self.dataset,
            "source": self.source,
            "business_calendar_evidence": self.business_calendar_evidence,
            "series": {
                "instrument_key": self.instrument_key,
                "business_key": self.business_key,
                "frequency_semantics": self.frequency_semantics.value,
            },
            "business_calendar": {
                "id": self.calendar_id,
                "version": self.calendar_version,
                "evidence": self.business_calendar_evidence,
                "covered_from": (
                    self.calendar_covered_from.isoformat()
                    if self.calendar_covered_from is not None
                    else None
                ),
                "covered_through": (
                    self.calendar_covered_through.isoformat()
                    if self.calendar_covered_through is not None
                    else None
                ),
                "covers_request": self.business_calendar_covers_request,
            },
            "status": self.status.value,
            "boundaries": {
                "requested": {
                    "start": self.requested_start.isoformat(),
                    "end": self.requested_end.isoformat(),
                },
                "known_start": {
                    "date": (
                        self.known_start_date.isoformat()
                        if self.known_start_date is not None
                        else None
                    ),
                    "unknown": self.start_boundary_unknown,
                    "independent_evidence": self.independent_start_evidence,
                    "request_covers_start": self.scope_covers_known_start,
                },
                "source_retention": {
                    "from": (
                        self.source_retained_from.isoformat()
                        if self.source_retained_from is not None
                        else None
                    ),
                    "through": (
                        self.source_retained_through.isoformat()
                        if self.source_retained_through is not None
                        else None
                    ),
                    "evidence": self.source_retention_evidence,
                },
                "snapshot_archive": {
                    "from": (
                        self.snapshot_archive_from.isoformat()
                        if self.snapshot_archive_from is not None
                        else None
                    ),
                    "through": (
                        self.snapshot_archive_through.isoformat()
                        if self.snapshot_archive_through is not None
                        else None
                    ),
                    "evidence": self.snapshot_archive_evidence,
                    "outside_interval": date_values(self.unarchived_dates),
                },
            },
            "snapshot_only": self.snapshot_only,
            "counts": {
                "expected_dates": len(self.expected_dates),
                "observed_rows": self.observed_row_count,
                "observed_unique_dates": self.observed_unique_date_count,
                "covered_expected_dates": self.covered_expected_date_count,
            },
            "expected_business_dates": date_values(self.expected_dates),
            "gaps": {
                "head": date_values(self.head_gaps),
                "interior": date_values(self.interior_gaps),
                "tail": date_values(self.tail_gaps),
            },
            "duplicate_dates": date_values(self.duplicate_dates),
            "unexpected_dates": date_values(self.unexpected_dates),
            "reasons": list(self.reasons),
        }

    def to_json(self) -> str:
        """Return deterministic JSON for storage, logs, or API responses."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def evaluate_history_coverage(request: HistoryCoverageRequest) -> HistoryCoverageReport:
    """Compare explicit expected daily dates with actual observations.

    Completeness is based on exact set reconciliation. Counts are descriptive
    only. ``COMPLETE`` means the requested range covers the evidenced start
    and every expected date is observed exactly once without unexpected dates.
    """
    if not isinstance(request, HistoryCoverageRequest):
        raise TypeError("request must be a HistoryCoverageRequest")

    expected = tuple(
        sorted(
            item
            for item in request.expected_business_dates
            if request.request_start <= item <= request.request_end
            and (request.known_start_date is None or item >= request.known_start_date)
        )
    )
    observed_rows = tuple(request.observed_business_dates)
    observed_counts = Counter(observed_rows)
    observed_dates = set(observed_counts)
    expected_set = set(expected)
    covered = expected_set & observed_dates
    missing = expected_set - observed_dates
    duplicates = tuple(sorted(item for item, count in observed_counts.items() if count > 1))
    unexpected = tuple(
        sorted(
            item
            for item in observed_dates
            if not request.request_start <= item <= request.request_end or item not in expected_set
        )
    )

    if covered:
        first_covered = min(covered)
        last_covered = max(covered)
        head_gaps = tuple(item for item in expected if item in missing and item < first_covered)
        interior_gaps = tuple(
            item for item in expected if item in missing and first_covered < item < last_covered
        )
        tail_gaps = tuple(item for item in expected if item in missing and item > last_covered)
    elif missing:
        # With no observed expected date, the empty observed interval has no
        # finite left or right anchor; expose the same missing dates at both ends.
        head_gaps = missing_dates = tuple(sorted(missing))
        interior_gaps = ()
        tail_gaps = missing_dates
    else:
        head_gaps = ()
        interior_gaps = ()
        tail_gaps = ()

    source_limited_dates = tuple(
        item
        for item in expected
        if item in missing
        and (
            (request.source_retained_from is not None and item < request.source_retained_from)
            or (
                request.source_retained_through is not None
                and item > request.source_retained_through
            )
        )
    )
    if (
        request.snapshot_only
        and request.snapshot_archive_from is not None
        and request.snapshot_archive_through is not None
    ):
        unarchived_dates = tuple(
            item
            for item in expected
            if item < request.snapshot_archive_from or item > request.snapshot_archive_through
        )
        snapshot_limited = bool(unarchived_dates)
    elif request.snapshot_only and expected:
        unarchived_dates = expected
        snapshot_limited = True
    else:
        unarchived_dates = ()
        snapshot_limited = False
    scope_covers_known_start = (
        request.known_start_date is not None and request.request_start <= request.known_start_date
    )
    business_calendar_covers_request = (
        request.calendar_id is not None
        and request.calendar_version is not None
        and request.business_calendar_evidence is not None
        and request.calendar_covered_from is not None
        and request.calendar_covered_through is not None
        and request.calendar_covered_from <= request.request_start
        and request.calendar_covered_through >= request.request_end
    )

    reasons: list[str] = []
    if not business_calendar_covers_request:
        reasons.append(
            "Business calendar identity, version, evidence, or declared coverage is missing; "
            "the expected-date set cannot certify the full requested range."
        )
    if request.start_boundary_unknown:
        reasons.append(
            "The independent historical start boundary is unknown; observed rows cannot establish "
            "an instrument inception date."
        )
    elif not scope_covers_known_start:
        reasons.append(
            "The requested range begins after the independently evidenced start date, so it does "
            "not cover the full history."
        )
    if not expected:
        reasons.append(
            "No expected daily business dates fall within the requested scope after applying the "
            "known instrument start boundary."
        )
        if not business_calendar_covers_request or request.start_boundary_unknown:
            status = CoverageStatus.UNKNOWN
        else:
            status = (
                CoverageStatus.NOT_APPLICABLE
                if not duplicates and not unexpected
                else CoverageStatus.PARTIAL
            )
    elif source_limited_dates or snapshot_limited:
        if source_limited_dates:
            reasons.append(
                f"{len(source_limited_dates)} expected business date(s) fall outside the declared "
                "source retention boundary."
            )
        if snapshot_limited:
            if request.snapshot_archive_from is None:
                reasons.append(
                    "A snapshot-only source has no archive-boundary evidence for this multi-date "
                    "history, so its unarchived dates cannot be certified."
                )
            else:
                reasons.append(
                    f"{len(unarchived_dates)} expected business date(s) are outside the evidenced "
                    "snapshot archive interval; a snapshot-only source cannot reconstruct them."
                )
        status = (
            CoverageStatus.SOURCE_LIMITED
            if business_calendar_covers_request
            else CoverageStatus.UNKNOWN
        )
    elif not business_calendar_covers_request or request.start_boundary_unknown:
        status = CoverageStatus.UNKNOWN
    elif not scope_covers_known_start or missing or duplicates or unexpected:
        status = CoverageStatus.PARTIAL
    else:
        status = CoverageStatus.COMPLETE
        reasons.append(
            "Every trusted expected business date from the evidenced start boundary through the "
            "requested end is observed exactly once, with no unexpected dates."
        )

    if missing:
        reasons.append(
            f"{len(missing)} trusted expected business date(s) have no observed row; see the "
            "head, interior, and tail gap lists."
        )
        if not covered:
            reasons.append(
                "No expected business date is observed, so the complete missing range is shown "
                "at both the unanchored head and tail."
            )
    if duplicates:
        reasons.append(f"{len(duplicates)} business date(s) have duplicate observed rows.")
    if unexpected:
        reasons.append(
            f"{len(unexpected)} observed business date(s) are outside the trusted expected "
            "dates or requested range."
        )

    return HistoryCoverageReport(
        dataset=request.dataset,
        source=request.source,
        instrument_key=request.instrument_key,
        business_key=request.business_key,
        frequency_semantics=FrequencySemantics(request.frequency_semantics),
        calendar_id=request.calendar_id,
        calendar_version=request.calendar_version,
        business_calendar_evidence=request.business_calendar_evidence,
        calendar_covered_from=request.calendar_covered_from,
        calendar_covered_through=request.calendar_covered_through,
        business_calendar_covers_request=business_calendar_covers_request,
        status=status,
        requested_start=request.request_start,
        requested_end=request.request_end,
        known_start_date=request.known_start_date,
        start_boundary_unknown=request.start_boundary_unknown,
        independent_start_evidence=request.independent_start_evidence,
        source_retained_from=request.source_retained_from,
        source_retained_through=request.source_retained_through,
        source_retention_evidence=request.source_retention_evidence,
        snapshot_only=request.snapshot_only,
        snapshot_archive_from=request.snapshot_archive_from,
        snapshot_archive_through=request.snapshot_archive_through,
        snapshot_archive_evidence=request.snapshot_archive_evidence,
        scope_covers_known_start=scope_covers_known_start,
        expected_dates=expected,
        observed_row_count=len(observed_rows),
        observed_unique_date_count=len(observed_dates),
        covered_expected_date_count=len(covered),
        head_gaps=head_gaps,
        interior_gaps=interior_gaps,
        tail_gaps=tail_gaps,
        duplicate_dates=duplicates,
        unexpected_dates=unexpected,
        unarchived_dates=unarchived_dates,
        reasons=tuple(reasons),
    )
