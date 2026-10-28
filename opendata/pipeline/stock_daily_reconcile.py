"""Offline, evidence-bound reconciliation of published stock-daily history.

This module composes the single-series history evaluator with the existing
registry-backed stock-daily request planner. It does not read a warehouse,
contact a provider, write publications, create bars, or dispatch jobs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from itertools import islice
from typing import Final

from opendata.data.instrument_reference import LifecycleStatus
from opendata.pipeline.history_coverage import (
    CoverageStatus,
    HistoryCoverageReport,
    HistoryCoverageRequest,
    evaluate_history_coverage,
)
from opendata.pipeline.stock_daily_backfill_plan import (
    BackfillBlocker,
    BackfillBlockReason,
    InstrumentBackfillResult,
    StockDailyBackfillChunk,
    StockDailyBackfillPlan,
)

MAX_SERIES_PER_RECONCILIATION: Final[int] = 10_000
MAX_EVIDENCE_DATES: Final[int] = 100_000
MAX_TOTAL_EVIDENCE_DATE_ENTRIES: Final[int] = 5_000_000
MAX_TOTAL_PLAN_INSTRUMENTS: Final[int] = 50_000
MAX_TOTAL_PLAN_CHUNKS: Final[int] = 250_000
MAX_TOTAL_PLAN_MATCHING_WORK: Final[int] = 10_000_000
MAX_PROVIDER_SYMBOLS: Final[int] = 10_000
MAX_CANDIDATE_WINDOWS: Final[int] = 10_000
MAX_WINDOW_DAYS: Final[int] = 3_660
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


class EndBoundaryKind(str, Enum):
    """Independent terminal lifecycle boundary used for a completeness claim."""

    UNKNOWN = "unknown"
    ACTIVE_AS_OF = "active_as_of"
    DELISTED_LAST_EXPECTED_OBSERVATION = "delisted_last_expected_observation"


class ReconciliationIssueCode(str, Enum):
    """Stable reason codes for unknown scope and dates without safe candidates."""

    PUBLICATION_MISSING = "publication_missing"
    INSTRUMENT_EXPECTATION_EVIDENCE_MISSING = "instrument_expectation_evidence_missing"
    UNCERTAIN_INSTRUMENT_OBSERVATION_STATUS = "uncertain_instrument_observation_status"
    CALENDAR_COVERAGE_INCOMPLETE = "calendar_coverage_incomplete"
    START_BOUNDARY_UNKNOWN = "start_boundary_unknown"
    END_BOUNDARY_UNKNOWN = "end_boundary_unknown"
    SOURCE_RETENTION_UNKNOWN = "source_retention_unknown"
    SOURCE_RETENTION_EXCLUDES_DATE = "source_retention_excludes_date"
    SOURCE_CAPABILITY_UNKNOWN = "source_capability_unknown"
    SOURCE_CAPABILITY_EXCLUDES_DATE = "source_capability_excludes_date"
    BACKFILL_PLAN_MISSING = "backfill_plan_missing"
    BACKFILL_INSTRUMENT_MISSING = "backfill_instrument_missing"
    BACKFILL_BOUNDARY_MISMATCH = "backfill_boundary_mismatch"
    BACKFILL_ROUTE_BLOCKED = "backfill_route_blocked"
    BACKFILL_ALIAS_MISSING = "backfill_alias_missing"
    BACKFILL_ALIAS_AMBIGUOUS = "backfill_alias_ambiguous"
    BACKFILL_SYMBOL_UNVERIFIED = "backfill_symbol_unverified"
    CANDIDATE_WINDOW_LIMIT = "candidate_window_limit"


def _require_label(value: str, name: str) -> str:
    """Return a normalized nonempty label."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _require_date(value: date, name: str) -> date:
    """Reject timestamps and non-date values at the API boundary."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError(f"{name} must be a datetime.date, not a timestamp or string")
    return value


def _date_tuple(values: Iterable[date], name: str) -> tuple[date, ...]:
    """Materialize a bounded sequence of dates, preserving duplicates."""
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError(f"{name} must be an iterable of datetime.date values")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError(f"{name} must be an iterable of datetime.date values") from exc
    result = tuple(islice(iterator, MAX_EVIDENCE_DATES + 1))
    if len(result) > MAX_EVIDENCE_DATES:
        raise ValueError(f"{name} exceeds the {MAX_EVIDENCE_DATES:,}-date limit")
    for index, item in enumerate(result):
        _require_date(item, f"{name}[{index}]")
    return result


def _sha256(value: str, name: str) -> str:
    """Validate a lowercase hexadecimal SHA-256 pin."""
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase 64-character SHA-256 value")
    return value


@dataclass(frozen=True, slots=True)
class DailyPublicationSnapshot:
    """Pinned identity and observed business dates from one immutable publication.

    ``content_sha256`` must be calculated over the original publication bytes
    by the caller. The reconciler binds that pin to the dataset, source and
    instrument but does not open or authenticate the referenced artifact.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    publication_id: str
    content_sha256: str
    observed_business_dates: Iterable[date]

    def __post_init__(self) -> None:
        """Validate publication identity, digest shape and bounded dates."""
        for name in ("dataset_id", "source_id", "instrument_id", "publication_id"):
            object.__setattr__(self, name, _require_label(getattr(self, name), name))
        object.__setattr__(self, "content_sha256", _sha256(self.content_sha256, "content_sha256"))
        object.__setattr__(
            self,
            "observed_business_dates",
            _date_tuple(self.observed_business_dates, "observed_business_dates"),
        )


@dataclass(frozen=True, slots=True)
class InstrumentExpectedObservationEvidence:
    """Evidence for dates on which this instrument/source should publish a bar.

    ``expected_business_dates`` is instrument-specific: it must account for
    listing status, suspensions, delisting and the provider's publication
    semantics. Exchange sessions alone are not sufficient. Dates whose
    expected-observation status is unresolved are listed separately and block
    a completeness claim.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    calendar_id: str
    calendar_version: str
    calendar_evidence_ref: str
    instrument_evidence_ref: str
    covered_from: date
    covered_through: date
    expected_business_dates: Iterable[date]
    unresolved_observation_dates: Iterable[date] = ()

    def __post_init__(self) -> None:
        """Validate exact series identity, coverage bounds and evidence dates."""
        for name in (
            "dataset_id",
            "source_id",
            "instrument_id",
            "calendar_id",
            "calendar_version",
            "calendar_evidence_ref",
            "instrument_evidence_ref",
        ):
            object.__setattr__(self, name, _require_label(getattr(self, name), name))
        start = _require_date(self.covered_from, "covered_from")
        end = _require_date(self.covered_through, "covered_through")
        if start > end:
            raise ValueError("covered_from must be on or before covered_through")
        object.__setattr__(self, "covered_from", start)
        object.__setattr__(self, "covered_through", end)
        expected = _date_tuple(self.expected_business_dates, "expected_business_dates")
        unresolved = _date_tuple(self.unresolved_observation_dates, "unresolved_observation_dates")
        if len(set(expected)) != len(expected):
            raise ValueError("expected_business_dates must not contain duplicates")
        if len(set(unresolved)) != len(unresolved):
            raise ValueError("unresolved_observation_dates must not contain duplicates")
        if set(expected) & set(unresolved):
            raise ValueError("unresolved dates must not be classified as expected observations")
        for name, values in (
            ("expected_business_dates", expected),
            ("unresolved_observation_dates", unresolved),
        ):
            if any(item < start or item > end for item in values):
                raise ValueError(f"{name} must fall inside the evidenced calendar coverage")
        object.__setattr__(self, "expected_business_dates", expected)
        object.__setattr__(self, "unresolved_observation_dates", unresolved)


@dataclass(frozen=True, slots=True)
class EndBoundaryEvidence:
    """Independent end-of-scope evidence for one security lifecycle.

    An active boundary is a status observation that covers the requested end.
    A delisted boundary is the last business date on which this exact
    instrument/source was independently expected to publish a row.
    """

    dataset_id: str
    instrument_id: str
    kind: EndBoundaryKind | str
    boundary_date: date | None = None
    evidence_ref: str | None = None

    def __post_init__(self) -> None:
        """Validate boundary kind and require evidence for known endpoints."""
        object.__setattr__(self, "dataset_id", _require_label(self.dataset_id, "dataset_id"))
        object.__setattr__(
            self, "instrument_id", _require_label(self.instrument_id, "instrument_id")
        )
        try:
            kind = EndBoundaryKind(self.kind)
        except (TypeError, ValueError) as exc:
            raise ValueError("kind must be a supported EndBoundaryKind") from exc
        object.__setattr__(self, "kind", kind)
        if kind is EndBoundaryKind.UNKNOWN:
            if self.boundary_date is not None or self.evidence_ref is not None:
                raise ValueError("unknown end boundary cannot include a date or evidence")
            return
        if self.boundary_date is None or self.evidence_ref is None:
            raise ValueError("a known end boundary requires both a date and evidence_ref")
        object.__setattr__(
            self, "boundary_date", _require_date(self.boundary_date, "boundary_date")
        )
        object.__setattr__(self, "evidence_ref", _require_label(self.evidence_ref, "evidence_ref"))


@dataclass(frozen=True, slots=True)
class SourceRetrievalEvidence:
    """Reviewed source capability for one instrument and provider-symbol set.

    The reference is caller-supplied evidence, not a cryptographic signature.
    It must identify the exact source and symbol mapping in the backfill plan.
    """

    dataset_id: str
    source_id: str
    instrument_id: str
    evidence_ref: str
    queryable_from: date
    queryable_through: date
    provider_symbols: Iterable[str]
    max_window_days: int

    def __post_init__(self) -> None:
        """Validate source-specific query range, symbols and request limit."""
        for name in ("dataset_id", "source_id", "instrument_id", "evidence_ref"):
            object.__setattr__(self, name, _require_label(getattr(self, name), name))
        start = _require_date(self.queryable_from, "queryable_from")
        end = _require_date(self.queryable_through, "queryable_through")
        if start > end:
            raise ValueError("queryable_from must be on or before queryable_through")
        object.__setattr__(self, "queryable_from", start)
        object.__setattr__(self, "queryable_through", end)
        if (
            type(self.max_window_days) is not int
            or not 1 <= self.max_window_days <= MAX_WINDOW_DAYS
        ):
            raise ValueError(f"max_window_days must be from 1 to {MAX_WINDOW_DAYS}")
        if isinstance(self.provider_symbols, (str, bytes, Mapping)):
            raise ValueError("provider_symbols must be a nonempty iterable of strings")
        try:
            iterator = iter(self.provider_symbols)
        except TypeError as exc:
            raise ValueError("provider_symbols must be a nonempty iterable of strings") from exc
        raw_symbols = tuple(islice(iterator, MAX_PROVIDER_SYMBOLS + 1))
        if len(raw_symbols) > MAX_PROVIDER_SYMBOLS:
            raise ValueError(f"provider_symbols exceeds {MAX_PROVIDER_SYMBOLS:,} entries")
        symbols = tuple(
            _require_label(value, f"provider_symbols[{index}]")
            for index, value in enumerate(raw_symbols)
        )
        if not symbols or len(symbols) != len(set(symbols)):
            raise ValueError("provider_symbols must be nonempty and unique")
        object.__setattr__(self, "provider_symbols", tuple(sorted(symbols)))


@dataclass(frozen=True, slots=True)
class StockDailySeriesEvidence:
    """All exact inputs needed to reconcile one source/security publication."""

    coverage_request: HistoryCoverageRequest
    publication: DailyPublicationSnapshot | None
    expected_observations: InstrumentExpectedObservationEvidence | None
    end_boundary: EndBoundaryEvidence
    source_retrieval: SourceRetrievalEvidence | None = None

    def __post_init__(self) -> None:
        """Reject cross-publication, cross-source and cross-instrument evidence."""
        if not isinstance(self.coverage_request, HistoryCoverageRequest):
            raise TypeError("coverage_request must be a HistoryCoverageRequest")
        if self.publication is not None and not isinstance(
            self.publication, DailyPublicationSnapshot
        ):
            raise TypeError("publication must be a DailyPublicationSnapshot or None")
        if self.expected_observations is not None and not isinstance(
            self.expected_observations, InstrumentExpectedObservationEvidence
        ):
            raise TypeError("expected_observations has an unsupported type")
        if not isinstance(self.end_boundary, EndBoundaryEvidence):
            raise TypeError("end_boundary must be an EndBoundaryEvidence")
        if self.source_retrieval is not None and not isinstance(
            self.source_retrieval, SourceRetrievalEvidence
        ):
            raise TypeError("source_retrieval has an unsupported type")
        request = self.coverage_request
        if request.instrument_key is None or request.business_key is not None:
            raise ValueError("stock-daily reconciliation requires exactly one instrument_key")
        identity = (request.dataset, request.source, request.instrument_key)
        if (self.end_boundary.dataset_id, self.end_boundary.instrument_id) != (
            request.dataset,
            request.instrument_key,
        ):
            raise ValueError("end-boundary evidence identity differs from the coverage request")
        if self.publication is not None:
            publication_identity = (
                self.publication.dataset_id,
                self.publication.source_id,
                self.publication.instrument_id,
            )
            if publication_identity != identity:
                raise ValueError(
                    "publication identity must exactly match dataset/source/instrument"
                )
            if tuple(sorted(self.publication.observed_business_dates)) != tuple(
                sorted(request.observed_business_dates)
            ):
                raise ValueError("coverage observations do not match the pinned publication rows")
        elif request.observed_business_dates:
            raise ValueError("observed dates require a pinned publication identity")
        if self.expected_observations is not None:
            evidence = self.expected_observations
            if (evidence.dataset_id, evidence.source_id, evidence.instrument_id) != identity:
                raise ValueError("expected-observation evidence identity differs from the series")
            if tuple(sorted(evidence.expected_business_dates)) != tuple(
                sorted(request.expected_business_dates)
            ):
                raise ValueError(
                    "coverage expected dates do not match instrument-specific evidence"
                )
            if (
                evidence.calendar_id != request.calendar_id
                or evidence.calendar_version != request.calendar_version
                or evidence.calendar_evidence_ref != request.business_calendar_evidence
            ):
                raise ValueError("expected-observation evidence uses a different calendar pin")
            if (
                evidence.covered_from > request.request_start
                or evidence.covered_through < request.request_end
            ):
                raise ValueError(
                    "instrument expected-observation evidence does not cover the scope"
                )
            if request.known_start_date is not None and any(
                item < request.known_start_date for item in evidence.expected_business_dates
            ):
                raise ValueError(
                    "expected-observation dates precede the independently evidenced "
                    "known_start_date"
                )
            if any(
                item < request.request_start or item > request.request_end
                for item in evidence.unresolved_observation_dates
            ):
                raise ValueError("unresolved observation dates must fall inside the request scope")
        if self.source_retrieval is not None:
            capability = self.source_retrieval
            if (capability.dataset_id, capability.source_id, capability.instrument_id) != identity:
                raise ValueError("source-retrieval evidence identity differs from the series")


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    """One reason dates remain unknown, limited, or without a safe candidate."""

    code: ReconciliationIssueCode
    detail: str
    affected_dates: tuple[date, ...] = ()
    evidence_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Serialize an issue using stable field names and ISO dates."""
        return {
            "code": self.code.value,
            "detail": self.detail,
            "affected_dates": [item.isoformat() for item in self.affected_dates],
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class StockDailyGapCandidate:
    """A non-dispatching, alias-pinned date window for human review."""

    candidate_id: str
    dataset_id: str
    source_id: str
    instrument_id: str
    provider_symbol: str
    alias_version: str
    alias_evidence_ref: str
    publication_id: str
    publication_sha256: str
    source_capability_evidence_ref: str
    expected_missing_dates: tuple[date, ...]
    window_start: date
    window_end: date
    plan_sha256: str

    @property
    def window_days(self) -> int:
        """Return the inclusive calendar-day window length."""
        return (self.window_end - self.window_start).days + 1

    def to_dict(self) -> dict[str, object]:
        """Serialize candidate identity and exact missing business dates."""
        return {
            "candidate_id": self.candidate_id,
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "provider_symbol": self.provider_symbol,
            "alias_version": self.alias_version,
            "alias_evidence_ref": self.alias_evidence_ref,
            "publication_id": self.publication_id,
            "publication_sha256": self.publication_sha256,
            "source_capability_evidence_ref": self.source_capability_evidence_ref,
            "expected_missing_dates": [item.isoformat() for item in self.expected_missing_dates],
            "window": {
                "start": self.window_start.isoformat(),
                "end": self.window_end.isoformat(),
                "inclusive_calendar_days": self.window_days,
            },
            "plan_sha256": self.plan_sha256,
            "dispatch_authorized": False,
        }


@dataclass(frozen=True, slots=True)
class StockDailySeriesReconciliation:
    """Coverage, evidence gates, and bounded candidates for one exact series."""

    dataset_id: str
    source_id: str
    instrument_id: str
    publication_id: str | None
    publication_sha256: str | None
    status: CoverageStatus
    coverage: HistoryCoverageReport
    diagnostic_gap_dates: tuple[date, ...]
    confirmed_expected_gap_dates: tuple[date, ...]
    unresolved_observation_dates: tuple[date, ...]
    candidates: tuple[StockDailyGapCandidate, ...]
    unplanned_gap_dates: tuple[date, ...]
    issues: tuple[ReconciliationIssue, ...]

    @property
    def series_complete(self) -> bool:
        """Return whether evidence proves this source/security scope complete."""
        return self.status is CoverageStatus.COMPLETE

    def to_dict(self) -> dict[str, object]:
        """Serialize the per-series audit record deterministically."""
        return {
            "dataset_id": self.dataset_id,
            "source_id": self.source_id,
            "instrument_id": self.instrument_id,
            "publication": {
                "id": self.publication_id,
                "sha256": self.publication_sha256,
            },
            "status": self.status.value,
            "coverage": self.coverage.to_dict(),
            "diagnostic_gap_dates": [item.isoformat() for item in self.diagnostic_gap_dates],
            "confirmed_expected_gap_dates": [
                item.isoformat() for item in self.confirmed_expected_gap_dates
            ],
            "unresolved_observation_dates": [
                item.isoformat() for item in self.unresolved_observation_dates
            ],
            "candidates": [item.to_dict() for item in self.candidates],
            "unplanned_gap_dates": [item.isoformat() for item in self.unplanned_gap_dates],
            "issues": [item.to_dict() for item in self.issues],
            "series_complete": self.series_complete,
        }


@dataclass(frozen=True, slots=True)
class _InstrumentPlanIndex:
    """Constant-time instrument record plus validated date-sorted chunk starts."""

    instrument: InstrumentBackfillResult
    chunk_starts: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class _BackfillPlanIndex:
    """One shared source plan index reused by every matching series."""

    plan: StockDailyBackfillPlan
    instruments: Mapping[str, _InstrumentPlanIndex | None]


@dataclass(slots=True)
class _PlanMatchingWorkBudget:
    """Count instrument lookups and binary-search comparisons across one report."""

    used: int = 0

    def consume(self, units: int = 1) -> None:
        """Account for matching units or reject the whole reconciliation."""
        if type(units) is not int or units < 0:
            raise ValueError("matching work units must be a nonnegative integer")
        if self.used + units > MAX_TOTAL_PLAN_MATCHING_WORK:
            raise ValueError(
                f"total plan matching work exceeds {MAX_TOTAL_PLAN_MATCHING_WORK:,} units"
            )
        self.used += units


@dataclass(frozen=True, slots=True)
class StockDailyGapReconciliation:
    """Deterministic report for explicitly supplied stock-daily series."""

    series: tuple[StockDailySeriesReconciliation, ...]
    max_candidate_windows: int
    max_window_days: int
    max_total_evidence_date_entries: int
    total_evidence_date_entries: int
    max_total_plan_instruments: int
    total_plan_instruments: int
    max_total_plan_chunks: int
    total_plan_chunks: int
    max_total_plan_matching_work: int
    total_plan_matching_work: int
    candidate_limit_reached: bool
    plan_sha256s: tuple[str, ...]
    report_sha256: str

    @property
    def all_requested_series_complete(self) -> bool:
        """Return whether every supplied series is complete; an empty set is false."""
        return bool(self.series) and all(item.series_complete for item in self.series)

    @property
    def all_a_universe_complete(self) -> bool:
        """Never infer an all-A universe census from a caller-supplied series list."""
        return False

    def to_dict(self) -> dict[str, object]:
        """Return a canonical report mapping with its content pin."""
        value = self._content_dict()
        value["report_sha256"] = self.report_sha256
        return value

    def to_json(self) -> str:
        """Return compact canonical JSON for a durable review record."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _content_dict(self) -> dict[str, object]:
        """Build report content excluding the self-referential digest."""
        return {
            "schema_version": 4,
            "series": [item.to_dict() for item in self.series],
            "scope": {
                "all_requested_series_complete": self.all_requested_series_complete,
                "all_a_universe_complete": False,
                "universe_census_attested": False,
            },
            "limits": {
                "max_candidate_windows": self.max_candidate_windows,
                "max_window_days": self.max_window_days,
                "max_total_evidence_date_entries": self.max_total_evidence_date_entries,
                "total_evidence_date_entries": self.total_evidence_date_entries,
                "max_total_plan_instruments": self.max_total_plan_instruments,
                "total_plan_instruments": self.total_plan_instruments,
                "max_total_plan_chunks": self.max_total_plan_chunks,
                "total_plan_chunks": self.total_plan_chunks,
                "max_total_plan_matching_work": self.max_total_plan_matching_work,
                "total_plan_matching_work": self.total_plan_matching_work,
                "candidate_limit_reached": self.candidate_limit_reached,
            },
            "backfill_plan_sha256s": list(self.plan_sha256s),
            "dispatch_authorized": False,
            "bars_created": False,
        }


def reconcile_stock_daily_gaps(
    series: Iterable[StockDailySeriesEvidence],
    *,
    backfill_plans: Mapping[tuple[str, str], StockDailyBackfillPlan] | None = None,
    max_candidate_windows: int = 500,
    max_window_days: int = MAX_WINDOW_DAYS,
) -> StockDailyGapReconciliation:
    """Reconcile pinned publications and create bounded offline review candidates.

    A date is a confirmed gap only when a pinned publication and an
    instrument-specific expected-observation calendar share the exact
    dataset/source/instrument identity. A candidate additionally requires
    known lifecycle boundaries, source retention and query-capability evidence,
    plus one unambiguous alias-pinned chunk from the existing backfill plan.
    This function never dispatches a candidate.

    Args:
        series: Caller-supplied evidence for each dataset/source/instrument.
        backfill_plans: Existing plans keyed by their exact dataset/source IDs.
        max_candidate_windows: Maximum total review candidates in this report.
        max_window_days: Inclusive calendar-day maximum for a candidate.

    Returns:
        A stable report with per-series status, all diagnostic gaps, candidates,
        and explicit blockers. The report never attests an all-A universe.

    Raises:
        TypeError: If an input has an unsupported type.
        ValueError: If a total work budget is exceeded, identities or evidence pins conflict,
            limits are invalid, or plan keys/chunk ordering are invalid.
    """
    if (
        type(max_candidate_windows) is not int
        or not 1 <= max_candidate_windows <= MAX_CANDIDATE_WINDOWS
    ):
        raise ValueError(f"max_candidate_windows must be from 1 to {MAX_CANDIDATE_WINDOWS}")
    if type(max_window_days) is not int or not 1 <= max_window_days <= MAX_WINDOW_DAYS:
        raise ValueError(f"max_window_days must be from 1 to {MAX_WINDOW_DAYS}")

    plans, total_plan_instruments, total_plan_chunks = _validate_plans(backfill_plans or {})
    raw_series, total_evidence_date_entries = _bounded_series(series)
    ordered_series = tuple(
        sorted(
            raw_series,
            key=lambda item: (
                item.coverage_request.dataset,
                item.coverage_request.source,
                item.coverage_request.instrument_key or "",
            ),
        )
    )
    identities = tuple(
        (
            item.coverage_request.dataset,
            item.coverage_request.source,
            item.coverage_request.instrument_key,
        )
        for item in ordered_series
    )
    if len(set(identities)) != len(identities):
        raise ValueError("each dataset/source/instrument series may appear only once")

    matching_work = _PlanMatchingWorkBudget()
    remaining = max_candidate_windows
    results: list[StockDailySeriesReconciliation] = []
    limit_reached = False
    for item in ordered_series:
        result = _reconcile_one(
            item,
            plan_index=plans.get((item.coverage_request.dataset, item.coverage_request.source)),
            matching_work=matching_work,
            max_candidate_windows=remaining,
            max_window_days=max_window_days,
        )
        results.append(result)
        remaining -= len(result.candidates)
        if result.unplanned_gap_dates and any(
            issue.code is ReconciliationIssueCode.CANDIDATE_WINDOW_LIMIT for issue in result.issues
        ):
            limit_reached = True

    plan_hashes = tuple(sorted({item.plan.plan_sha256 for item in plans.values()}))
    report = StockDailyGapReconciliation(
        series=tuple(results),
        max_candidate_windows=max_candidate_windows,
        max_window_days=max_window_days,
        max_total_evidence_date_entries=MAX_TOTAL_EVIDENCE_DATE_ENTRIES,
        total_evidence_date_entries=total_evidence_date_entries,
        max_total_plan_instruments=MAX_TOTAL_PLAN_INSTRUMENTS,
        total_plan_instruments=total_plan_instruments,
        max_total_plan_chunks=MAX_TOTAL_PLAN_CHUNKS,
        total_plan_chunks=total_plan_chunks,
        max_total_plan_matching_work=MAX_TOTAL_PLAN_MATCHING_WORK,
        total_plan_matching_work=matching_work.used,
        candidate_limit_reached=limit_reached,
        plan_sha256s=plan_hashes,
        report_sha256="0" * 64,
    )
    canonical = json.dumps(
        report._content_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return StockDailyGapReconciliation(
        series=report.series,
        max_candidate_windows=report.max_candidate_windows,
        max_window_days=report.max_window_days,
        max_total_evidence_date_entries=report.max_total_evidence_date_entries,
        total_evidence_date_entries=report.total_evidence_date_entries,
        max_total_plan_instruments=report.max_total_plan_instruments,
        total_plan_instruments=report.total_plan_instruments,
        max_total_plan_chunks=report.max_total_plan_chunks,
        total_plan_chunks=report.total_plan_chunks,
        max_total_plan_matching_work=report.max_total_plan_matching_work,
        total_plan_matching_work=report.total_plan_matching_work,
        candidate_limit_reached=report.candidate_limit_reached,
        plan_sha256s=report.plan_sha256s,
        report_sha256=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _bounded_series(
    values: Iterable[StockDailySeriesEvidence],
) -> tuple[tuple[StockDailySeriesEvidence, ...], int]:
    """Bound series and date work before materializing the complete input."""
    if isinstance(values, (str, bytes, Mapping)):
        raise ValueError("series must be an iterable of StockDailySeriesEvidence")
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ValueError("series must be an iterable of StockDailySeriesEvidence") from exc
    items: list[StockDailySeriesEvidence] = []
    total_date_entries = 0
    for item in iterator:
        if len(items) >= MAX_SERIES_PER_RECONCILIATION:
            raise ValueError(f"series exceeds {MAX_SERIES_PER_RECONCILIATION:,} items")
        if not isinstance(item, StockDailySeriesEvidence):
            raise TypeError("series items must be StockDailySeriesEvidence")
        item_date_entries = _evidence_date_entry_count(item)
        if total_date_entries + item_date_entries > MAX_TOTAL_EVIDENCE_DATE_ENTRIES:
            raise ValueError(
                f"total evidence date entries exceed {MAX_TOTAL_EVIDENCE_DATE_ENTRIES:,}"
            )
        total_date_entries += item_date_entries
        items.append(item)
    return tuple(items), total_date_entries


def _evidence_date_entry_count(evidence: StockDailySeriesEvidence) -> int:
    """Count every date entry across the evidence sequences retained for one series."""
    request = evidence.coverage_request
    count = _validated_tuple_length(request.expected_business_dates, "expected dates")
    count += _validated_tuple_length(request.observed_business_dates, "observed dates")
    if evidence.publication is not None:
        count += _validated_tuple_length(
            evidence.publication.observed_business_dates,
            "publication observed dates",
        )
    if evidence.expected_observations is not None:
        count += _validated_tuple_length(
            evidence.expected_observations.expected_business_dates,
            "instrument expected dates",
        )
        count += _validated_tuple_length(
            evidence.expected_observations.unresolved_observation_dates,
            "unresolved expected dates",
        )
    return count


def _validated_tuple_length(values: Iterable[object], name: str) -> int:
    """Count a sequence already materialized and validated by its evidence model."""
    if not isinstance(values, tuple):
        raise TypeError(f"validated evidence {name} must be a tuple")
    return len(values)


def _validate_plans(
    values: Mapping[tuple[str, str], StockDailyBackfillPlan],
) -> tuple[dict[tuple[str, str], _BackfillPlanIndex], int, int]:
    """Validate and index plans within shared instrument and chunk budgets."""
    if not isinstance(values, Mapping):
        raise TypeError("backfill_plans must be a mapping")
    if len(values) > MAX_SERIES_PER_RECONCILIATION:
        raise ValueError("backfill_plans exceeds the supported scope limit")
    result: dict[tuple[str, str], _BackfillPlanIndex] = {}
    total_instruments = 0
    total_chunks = 0
    for key, plan in values.items():
        if not isinstance(key, tuple) or len(key) != 2:
            raise ValueError("backfill plan keys must be (dataset_id, source_id) pairs")
        normalized = (
            _require_label(key[0], "plan dataset_id"),
            _require_label(key[1], "plan source_id"),
        )
        if not isinstance(plan, StockDailyBackfillPlan):
            raise TypeError("backfill_plans values must be StockDailyBackfillPlan")
        if (plan.dataset_id, plan.source_id) != normalized:
            raise ValueError("backfill plan mapping key differs from its declared identity")
        _sha256(plan.plan_sha256, "plan_sha256")
        instruments: dict[str, _InstrumentPlanIndex | None] = {}
        for instrument in plan.instruments:
            if total_instruments >= MAX_TOTAL_PLAN_INSTRUMENTS:
                raise ValueError(f"total plan instruments exceed {MAX_TOTAL_PLAN_INSTRUMENTS:,}")
            if not isinstance(instrument, InstrumentBackfillResult):
                raise TypeError("plan instruments must be InstrumentBackfillResult values")
            total_instruments += 1
            chunks = instrument.candidate_chunks
            if total_chunks + len(chunks) > MAX_TOTAL_PLAN_CHUNKS:
                raise ValueError(f"total plan chunks exceed {MAX_TOTAL_PLAN_CHUNKS:,}")
            total_chunks += len(chunks)
            chunk_starts: list[date] = []
            previous_end: date | None = None
            for chunk in chunks:
                if not isinstance(chunk, StockDailyBackfillChunk):
                    raise TypeError("plan chunks must be StockDailyBackfillChunk values")
                if chunk.window_start > chunk.window_end or (
                    previous_end is not None and chunk.window_start <= previous_end
                ):
                    raise ValueError(
                        "plan chunks for an instrument must be ordered and non-overlapping"
                    )
                chunk_starts.append(chunk.window_start)
                previous_end = chunk.window_end
            indexed = _InstrumentPlanIndex(instrument, tuple(chunk_starts))
            if instrument.instrument_id in instruments:
                instruments[instrument.instrument_id] = None
            else:
                instruments[instrument.instrument_id] = indexed
        result[normalized] = _BackfillPlanIndex(plan=plan, instruments=instruments)
    return result, total_instruments, total_chunks


def _reconcile_one(
    evidence: StockDailySeriesEvidence,
    *,
    plan_index: _BackfillPlanIndex | None,
    matching_work: _PlanMatchingWorkBudget,
    max_candidate_windows: int,
    max_window_days: int,
) -> StockDailySeriesReconciliation:
    """Evaluate one pinned series and produce only evidence-supported windows."""
    request = evidence.coverage_request
    plan = plan_index.plan if plan_index is not None else None
    instrument_index: _InstrumentPlanIndex | None = None
    if plan_index is not None:
        matching_work.consume()
        instrument_index = plan_index.instruments.get(request.instrument_key or "")
    coverage = evaluate_history_coverage(request)
    diagnostic = tuple(
        sorted(set(coverage.head_gaps + coverage.interior_gaps + coverage.tail_gaps))
    )
    expectation = evidence.expected_observations
    publication = evidence.publication
    unresolved = (
        tuple(sorted(expectation.unresolved_observation_dates)) if expectation is not None else ()
    )
    confirmed = diagnostic if expectation is not None and publication is not None else ()
    issues: list[ReconciliationIssue] = []
    if publication is None:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.PUBLICATION_MISSING,
                (
                    "No pinned publication is available; absence of rows cannot be distinguished "
                    "from absence of a publication."
                ),
                diagnostic,
            )
        )
    if expectation is None:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.INSTRUMENT_EXPECTATION_EVIDENCE_MISSING,
                (
                    "Exchange sessions alone do not prove that this security/source should "
                    "publish a bar."
                ),
                diagnostic,
            )
        )
    if unresolved:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.UNCERTAIN_INSTRUMENT_OBSERVATION_STATUS,
                (
                    "Suspension, lifecycle or source-specific bar expectation is unresolved on "
                    "these dates."
                ),
                unresolved,
                (expectation.instrument_evidence_ref,) if expectation is not None else (),
            )
        )
    if not coverage.business_calendar_covers_request:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.CALENDAR_COVERAGE_INCOMPLETE,
                (
                    "Calendar identity, version, evidence or date coverage does not span the "
                    "requested interval."
                ),
                diagnostic,
                (coverage.business_calendar_evidence,)
                if coverage.business_calendar_evidence is not None
                else (),
            )
        )
    if request.start_boundary_unknown or request.known_start_date is None:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.START_BOUNDARY_UNKNOWN,
                "No independently evidenced listing/start boundary is available.",
                diagnostic,
                (),
            )
        )
    if not _end_boundary_covers(evidence):
        refs = (evidence.end_boundary.evidence_ref,) if evidence.end_boundary.evidence_ref else ()
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.END_BOUNDARY_UNKNOWN,
                (
                    "Independent active-through or last-expected-observation evidence does not "
                    "cover the requested end."
                ),
                diagnostic,
                refs,
            )
        )
    retention_known = (
        request.source_retained_from is not None
        and request.source_retained_through is not None
        and request.source_retention_evidence is not None
    )
    if not retention_known:
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.SOURCE_RETENTION_UNKNOWN,
                (
                    "Source retention is not bounded and evidenced at both ends; completeness "
                    "and repairability remain unknown."
                ),
                diagnostic,
                (request.source_retention_evidence,)
                if request.source_retention_evidence is not None
                else (),
            )
        )

    status = _evidence_gated_status(
        coverage.status,
        publication_present=publication is not None,
        expectation_present=expectation is not None,
        calendar_complete=coverage.business_calendar_covers_request,
        unresolved_dates=unresolved,
        start_known=not request.start_boundary_unknown and request.known_start_date is not None,
        end_known=_end_boundary_covers(evidence),
        retention_known=retention_known,
    )
    plan_instrument = instrument_index.instrument if instrument_index is not None else None
    if plan_instrument is not None and not _plan_boundaries_match(evidence, plan_instrument):
        status = CoverageStatus.UNKNOWN
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH,
                (
                    "The source plan lifecycle does not confirm the supplied start/end "
                    "boundary evidence."
                ),
                diagnostic,
                tuple(
                    sorted(
                        ref
                        for ref in (
                            evidence.end_boundary.evidence_ref,
                            plan_instrument.status_evidence_ref,
                            plan_instrument.delist_evidence_ref,
                            plan_instrument.start_evidence_ref,
                        )
                        if ref is not None
                    )
                ),
            )
        )
    candidate_windows: list[StockDailyGapCandidate] = []
    unplanned: set[date] = set()
    if confirmed:
        eligible, candidate_issues = _eligible_gap_dates(
            evidence,
            plan_index,
            instrument_index,
            confirmed,
            matching_work,
        )
        issues.extend(candidate_issues)
        candidate_windows, unplanned, limit_used = _make_candidates(
            evidence,
            plan,
            eligible,
            max_candidate_windows=max_candidate_windows,
            max_window_days=max_window_days,
        )
        if limit_used:
            issues.append(
                ReconciliationIssue(
                    ReconciliationIssueCode.CANDIDATE_WINDOW_LIMIT,
                    (
                        "The report-level candidate window limit was reached; remaining "
                        "confirmed gaps are listed as unplanned."
                    ),
                    tuple(sorted(unplanned)),
                )
            )
        else:
            planned_dates = {
                day for candidate in candidate_windows for day in candidate.expected_missing_dates
            }
            unplanned.update(set(confirmed) - planned_dates - set(eligible))
    _merge_unplanned_reasons(issues, unplanned, candidate_windows, confirmed)
    return StockDailySeriesReconciliation(
        dataset_id=request.dataset,
        source_id=request.source,
        instrument_id=request.instrument_key or "",
        publication_id=publication.publication_id if publication is not None else None,
        publication_sha256=publication.content_sha256 if publication is not None else None,
        status=status,
        coverage=coverage,
        diagnostic_gap_dates=diagnostic,
        confirmed_expected_gap_dates=confirmed,
        unresolved_observation_dates=unresolved,
        candidates=tuple(candidate_windows),
        unplanned_gap_dates=tuple(sorted(unplanned)),
        issues=_sort_issues(issues),
    )


def _end_boundary_covers(evidence: StockDailySeriesEvidence) -> bool:
    """Return whether independent terminal evidence reaches the requested scope."""
    boundary = evidence.end_boundary
    request = evidence.coverage_request
    if boundary.kind is EndBoundaryKind.ACTIVE_AS_OF:
        return bool(boundary.boundary_date and boundary.boundary_date >= request.request_end)
    if boundary.kind is EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION:
        expected = tuple(request.expected_business_dates)
        return bool(
            boundary.boundary_date
            and boundary.boundary_date <= request.request_end
            and expected
            and max(expected) == boundary.boundary_date
        )
    return False


def _evidence_gated_status(
    coverage_status: CoverageStatus,
    *,
    publication_present: bool,
    expectation_present: bool,
    calendar_complete: bool,
    unresolved_dates: tuple[date, ...],
    start_known: bool,
    end_known: bool,
    retention_known: bool,
) -> CoverageStatus:
    """Apply instrument, publication and boundary evidence gates to coverage."""
    if not all(
        (
            publication_present,
            expectation_present,
            calendar_complete,
            not unresolved_dates,
            start_known,
            end_known,
            retention_known,
        )
    ):
        return CoverageStatus.UNKNOWN
    return coverage_status


def _eligible_gap_dates(
    evidence: StockDailySeriesEvidence,
    plan_index: _BackfillPlanIndex | None,
    instrument_index: _InstrumentPlanIndex | None,
    gap_dates: tuple[date, ...],
    matching_work: _PlanMatchingWorkBudget,
) -> tuple[dict[date, StockDailyBackfillChunk], list[ReconciliationIssue]]:
    """Match gaps to retention, source capability and one safe alias route."""
    request = evidence.coverage_request
    plan = plan_index.plan if plan_index is not None else None
    issues: list[ReconciliationIssue] = []
    eligible: dict[date, StockDailyBackfillChunk] = {}
    blocked: dict[ReconciliationIssueCode, set[date]] = {}

    def block(code: ReconciliationIssueCode, day: date) -> None:
        blocked.setdefault(code, set()).add(day)

    if request.source_retained_from is None or request.source_retained_through is None:
        for day in gap_dates:
            block(ReconciliationIssueCode.SOURCE_RETENTION_UNKNOWN, day)
    if evidence.source_retrieval is None:
        for day in gap_dates:
            block(ReconciliationIssueCode.SOURCE_CAPABILITY_UNKNOWN, day)
    if plan is None:
        for day in gap_dates:
            block(ReconciliationIssueCode.BACKFILL_PLAN_MISSING, day)
    if (
        request.start_boundary_unknown
        or request.known_start_date is None
        or not _end_boundary_covers(evidence)
        or not request.business_calendar_evidence
        or not evidence.publication
        or evidence.expected_observations is None
    ):
        for day in gap_dates:
            block(ReconciliationIssueCode.BACKFILL_ROUTE_BLOCKED, day)

    if plan is not None and instrument_index is None:
        for day in gap_dates:
            block(ReconciliationIssueCode.BACKFILL_INSTRUMENT_MISSING, day)
    if plan is None or instrument_index is None:
        issues.extend(_issues_from_dates(blocked))
        return eligible, issues
    plan_instrument = instrument_index.instrument

    if (
        plan.dataset_id != request.dataset
        or plan.source_id != request.source
        or plan_instrument.instrument_id != request.instrument_key
    ):
        for day in gap_dates:
            block(ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH, day)
        issues.extend(_issues_from_dates(blocked))
        return eligible, issues
    if not _plan_boundaries_match(evidence, plan_instrument):
        for day in gap_dates:
            block(ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH, day)
        issues.extend(_issues_from_dates(blocked))
        return eligible, issues

    capability = evidence.source_retrieval
    if capability is None:
        issues.extend(_issues_from_dates(blocked))
        return eligible, issues
    if not _end_boundary_covers(evidence) or request.start_boundary_unknown:
        issues.extend(_issues_from_dates(blocked))
        return eligible, issues
    matching_work.consume(_validated_tuple_length(capability.provider_symbols, "provider symbols"))
    provider_symbols = frozenset(capability.provider_symbols)

    for day in gap_dates:
        if request.source_retained_from is None or request.source_retained_through is None:
            block(ReconciliationIssueCode.SOURCE_RETENTION_UNKNOWN, day)
            continue
        if day < request.source_retained_from or day > request.source_retained_through:
            block(ReconciliationIssueCode.SOURCE_RETENTION_EXCLUDES_DATE, day)
            continue
        if day < capability.queryable_from or day > capability.queryable_through:
            block(ReconciliationIssueCode.SOURCE_CAPABILITY_EXCLUDES_DATE, day)
            continue
        blocker = _instrument_blocker_for_date(
            evidence,
            plan_instrument,
            day,
            matching_work,
        )
        if blocker is not None:
            code = _blocker_issue_code(blocker)
            block(code, day)
            continue
        chunk = _find_chunk_for_date(instrument_index, day, matching_work)
        if chunk is None:
            block(ReconciliationIssueCode.BACKFILL_ALIAS_MISSING, day)
            continue
        matching_work.consume()
        if (
            chunk.instrument_id != request.instrument_key
            or chunk.lifecycle_version != plan_instrument.lifecycle_version
            or not _chunk_window_within_alias(chunk, chunk.window_start, chunk.window_end)
        ):
            block(ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH, day)
            continue
        if chunk.provider_symbol not in provider_symbols:
            block(ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED, day)
            continue
        eligible[day] = chunk

    issues.extend(_issues_from_dates(blocked))
    return eligible, issues


def _chunk_window_within_alias(
    chunk: StockDailyBackfillChunk,
    window_start: date,
    window_end: date,
) -> bool:
    """Require the complete query interval to remain inside one pinned alias chunk."""
    return bool(
        chunk.window_start <= window_start <= window_end <= chunk.window_end
        and (chunk.alias_valid_from is None or window_start >= chunk.alias_valid_from)
        and (chunk.alias_valid_through is None or window_end <= chunk.alias_valid_through)
    )


def _find_chunk_for_date(
    instrument_index: _InstrumentPlanIndex,
    day: date,
    matching_work: _PlanMatchingWorkBudget,
) -> StockDailyBackfillChunk | None:
    """Find the unique covering chunk by binary search over validated chunk starts."""
    starts = instrument_index.chunk_starts
    chunks = instrument_index.instrument.candidate_chunks
    lower = 0
    upper = len(starts)
    while lower < upper:
        middle = (lower + upper) // 2
        matching_work.consume()
        if starts[middle] <= day:
            lower = middle + 1
        else:
            upper = middle
    if lower == 0:
        return None
    candidate = chunks[lower - 1]
    matching_work.consume()
    if candidate.window_start <= day <= candidate.window_end:
        return candidate
    return None


def _plan_boundaries_match(
    evidence: StockDailySeriesEvidence,
    instrument: InstrumentBackfillResult,
) -> bool:
    """Ensure the query plan uses the same independently pinned lifecycle."""
    request = evidence.coverage_request
    if (
        instrument.known_start_date != request.known_start_date
        or instrument.start_boundary_unknown != request.start_boundary_unknown
        or instrument.start_evidence_ref != request.independent_start_evidence
    ):
        return False
    boundary = evidence.end_boundary
    if boundary.kind is EndBoundaryKind.ACTIVE_AS_OF:
        return bool(
            instrument.lifecycle_status is LifecycleStatus.ACTIVE
            and instrument.status_as_of >= request.request_end
            and boundary.evidence_ref == instrument.status_evidence_ref
        )
    if boundary.kind is EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION:
        return bool(
            instrument.lifecycle_status is LifecycleStatus.DELISTED
            and instrument.delisted_on is not None
            and boundary.boundary_date is not None
            and boundary.boundary_date <= instrument.delisted_on
            and boundary.evidence_ref != instrument.delist_evidence_ref
        )
    return False


def _instrument_blocker_for_date(
    evidence: StockDailySeriesEvidence,
    instrument: InstrumentBackfillResult,
    day: date,
    matching_work: _PlanMatchingWorkBudget,
) -> BackfillBlocker | None:
    """Return the first plan blocker that applies to one candidate date."""
    resolved_delisting = bool(
        evidence.end_boundary.kind is EndBoundaryKind.DELISTED_LAST_EXPECTED_OBSERVATION
        and evidence.end_boundary.boundary_date is not None
        and instrument.delisted_on is not None
        and evidence.end_boundary.boundary_date <= instrument.delisted_on
        and evidence.end_boundary.evidence_ref != instrument.delist_evidence_ref
    )
    blocking_reasons = {
        BackfillBlockReason.UNKNOWN_START_BOUNDARY,
        BackfillBlockReason.UNKNOWN_LIFECYCLE_STATUS,
        BackfillBlockReason.STALE_ACTIVE_STATUS,
        BackfillBlockReason.SOURCE_ALIAS_GAP,
        BackfillBlockReason.AMBIGUOUS_SOURCE_ALIAS,
        BackfillBlockReason.INVALID_PROVIDER_SYMBOL,
        BackfillBlockReason.BJ_PROVIDER_MAPPING_UNVERIFIED,
        BackfillBlockReason.DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED,
        BackfillBlockReason.NOT_YET_IN_SCOPE,
    }
    for blocker in instrument.blockers:
        matching_work.consume()
        if blocker.reason not in blocking_reasons:
            continue
        if (
            blocker.reason is BackfillBlockReason.DELISTED_BOUNDARY_SEMANTICS_UNVERIFIED
            and resolved_delisting
        ):
            continue
        if blocker.range_start is not None and day < blocker.range_start:
            continue
        if blocker.range_end is not None and day > blocker.range_end:
            continue
        return blocker
    return None


def _blocker_issue_code(blocker: BackfillBlocker) -> ReconciliationIssueCode:
    """Map a backfill-plan blocker to a stable reconciliation issue."""
    mapping = {
        BackfillBlockReason.SOURCE_ALIAS_GAP: ReconciliationIssueCode.BACKFILL_ALIAS_MISSING,
        BackfillBlockReason.AMBIGUOUS_SOURCE_ALIAS: (
            ReconciliationIssueCode.BACKFILL_ALIAS_AMBIGUOUS
        ),
        BackfillBlockReason.INVALID_PROVIDER_SYMBOL: (
            ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED
        ),
        BackfillBlockReason.BJ_PROVIDER_MAPPING_UNVERIFIED: (
            ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED
        ),
    }
    return mapping.get(blocker.reason, ReconciliationIssueCode.BACKFILL_ROUTE_BLOCKED)


def _issues_from_dates(
    grouped: Mapping[ReconciliationIssueCode, set[date]],
) -> list[ReconciliationIssue]:
    """Create stable issue records from code-to-date groups."""
    details = {
        ReconciliationIssueCode.SOURCE_RETENTION_UNKNOWN: (
            "Source retention boundaries are incomplete."
        ),
        ReconciliationIssueCode.SOURCE_RETENTION_EXCLUDES_DATE: (
            "The missing date falls outside evidenced source retention."
        ),
        ReconciliationIssueCode.SOURCE_CAPABILITY_UNKNOWN: (
            "No exact source/symbol retrieval capability evidence is available."
        ),
        ReconciliationIssueCode.SOURCE_CAPABILITY_EXCLUDES_DATE: (
            "The date is outside the source's evidenced query range."
        ),
        ReconciliationIssueCode.BACKFILL_PLAN_MISSING: (
            "No backfill plan is bound to this exact dataset/source."
        ),
        ReconciliationIssueCode.BACKFILL_INSTRUMENT_MISSING: (
            "The exact canonical instrument is absent or ambiguous in the source plan."
        ),
        ReconciliationIssueCode.BACKFILL_BOUNDARY_MISMATCH: (
            "The plan lifecycle or alias chunk bounds differ from the reconciliation evidence."
        ),
        ReconciliationIssueCode.BACKFILL_ROUTE_BLOCKED: (
            "The existing plan contains a blocker for this instrument/date."
        ),
        ReconciliationIssueCode.BACKFILL_ALIAS_MISSING: (
            "No reviewed provider alias route covers this date."
        ),
        ReconciliationIssueCode.BACKFILL_ALIAS_AMBIGUOUS: (
            "More than one provider alias route covers this date."
        ),
        ReconciliationIssueCode.BACKFILL_SYMBOL_UNVERIFIED: (
            "The planned provider symbol is not in the exact capability evidence."
        ),
    }
    return [
        ReconciliationIssue(code, details[code], tuple(sorted(days)))
        for code, days in sorted(grouped.items(), key=lambda item: item[0].value)
        if days
    ]


def _make_candidates(
    evidence: StockDailySeriesEvidence,
    plan: StockDailyBackfillPlan | None,
    eligible: Mapping[date, StockDailyBackfillChunk],
    *,
    max_candidate_windows: int,
    max_window_days: int,
) -> tuple[list[StockDailyGapCandidate], set[date], bool]:
    """Group eligible adjacent expected dates into deterministic bounded windows."""
    publication = evidence.publication
    source_retrieval = evidence.source_retrieval
    if not eligible or plan is None or publication is None or source_retrieval is None:
        return [], set(), False
    request = evidence.coverage_request
    expectation = evidence.expected_observations
    if expectation is None:
        return [], set(eligible), False
    allowed_days = min(max_window_days, source_retrieval.max_window_days, plan.max_window_days)
    expected_order = {
        day: index for index, day in enumerate(sorted(request.expected_business_dates))
    }
    candidates: list[StockDailyGapCandidate] = []
    unplanned: set[date] = set()
    limit_reached = False
    current_window: list[date] = []
    current_chunk: StockDailyBackfillChunk | None = None

    def emit_window(missing_dates: tuple[date, ...]) -> None:
        """Create at most one candidate, retaining overflow dates as unplanned."""
        nonlocal limit_reached
        if not missing_dates:
            return
        if len(candidates) >= max_candidate_windows:
            unplanned.update(missing_dates)
            limit_reached = True
            return
        first = missing_dates[0]
        last = missing_dates[-1]
        chunk = eligible[first]
        if not _chunk_window_within_alias(chunk, first, last):
            unplanned.update(missing_dates)
            return
        payload = {
            "dataset_id": request.dataset,
            "source_id": request.source,
            "instrument_id": request.instrument_key,
            "provider_symbol": chunk.provider_symbol,
            "alias_version": chunk.alias_version,
            "alias_evidence_ref": chunk.alias_evidence_ref,
            "publication_id": publication.publication_id,
            "publication_sha256": publication.content_sha256,
            "capability_evidence_ref": source_retrieval.evidence_ref,
            "missing_dates": [item.isoformat() for item in missing_dates],
            "window_start": first.isoformat(),
            "window_end": last.isoformat(),
            "plan_sha256": plan.plan_sha256,
        }
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        candidates.append(
            StockDailyGapCandidate(
                candidate_id=f"gap-{hashlib.sha256(key.encode('utf-8')).hexdigest()}",
                dataset_id=request.dataset,
                source_id=request.source,
                instrument_id=request.instrument_key or "",
                provider_symbol=chunk.provider_symbol,
                alias_version=chunk.alias_version,
                alias_evidence_ref=chunk.alias_evidence_ref,
                publication_id=publication.publication_id,
                publication_sha256=publication.content_sha256,
                source_capability_evidence_ref=source_retrieval.evidence_ref,
                expected_missing_dates=missing_dates,
                window_start=first,
                window_end=last,
                plan_sha256=plan.plan_sha256,
            )
        )

    for day in sorted(eligible):
        if limit_reached:
            unplanned.add(day)
            continue
        chunk = eligible[day]
        if current_window:
            previous = current_window[-1]
            same_chunk = current_chunk == chunk
            adjacent_expected = expected_order[day] == expected_order[previous] + 1
            exceeds_window = (day - current_window[0]).days + 1 > allowed_days
            if not same_chunk or not adjacent_expected or exceeds_window:
                emit_window(tuple(current_window))
                current_window = []
                current_chunk = None
                if limit_reached:
                    unplanned.add(day)
                    continue
        current_window.append(day)
        current_chunk = chunk
    if current_window:
        emit_window(tuple(current_window))
    return candidates, unplanned, limit_reached


def _merge_unplanned_reasons(
    issues: list[ReconciliationIssue],
    unplanned: set[date],
    candidates: list[StockDailyGapCandidate],
    confirmed: tuple[date, ...],
) -> None:
    """Keep every confirmed gap either in a candidate or in an issue record."""
    planned_dates = {day for candidate in candidates for day in candidate.expected_missing_dates}
    unresolved = set(confirmed) - planned_dates
    unresolved.update(unplanned)
    if unresolved and not any(
        issue.affected_dates and unresolved.intersection(issue.affected_dates) for issue in issues
    ):
        issues.append(
            ReconciliationIssue(
                ReconciliationIssueCode.BACKFILL_ROUTE_BLOCKED,
                "Confirmed missing dates remain unplanned; inspect the per-date evidence blockers.",
                tuple(sorted(unresolved)),
            )
        )


def _sort_issues(issues: Iterable[ReconciliationIssue]) -> tuple[ReconciliationIssue, ...]:
    """Deduplicate and sort issues deterministically."""
    unique = {
        (
            issue.code,
            issue.detail,
            issue.affected_dates,
            issue.evidence_refs,
        ): issue
        for issue in issues
    }
    return tuple(
        unique[key]
        for key in sorted(
            unique,
            key=lambda item: (
                item[0].value,
                item[1],
                item[2],
                item[3],
            ),
        )
    )
