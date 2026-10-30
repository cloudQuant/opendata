"""Pure, evidence-explicit coverage reconciliation for the CSI 500 daily series.

This module adapts caller-supplied date sets to :mod:`history_coverage`. It does
not read OpenData storage, call a provider, infer the index's inception date, or
verify the authenticity of evidence references.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from opendata.data.providers.ths.index_daily_publication import (
    INDEX_DATASET_ID_BY_SYMBOL,
    INDEX_SOURCE_ID,
)
from opendata.pipeline.history_coverage import (
    CoverageStatus,
    HistoryCoverageReport,
    HistoryCoverageRequest,
    evaluate_history_coverage,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

CSI500_SYMBOL = "000905.SH"
CSI500_DATASET_ID = INDEX_DATASET_ID_BY_SYMBOL[CSI500_SYMBOL]

__all__ = [
    "CSI500_DATASET_ID",
    "CSI500_SYMBOL",
    "Csi500CalendarEvidence",
    "Csi500HistoryReconciliationReport",
    "Csi500HistoryReconciliationRequest",
    "Csi500IndependentStartEvidence",
    "Csi500SourceRetentionEvidence",
    "evaluate_csi500_history_reconciliation",
]


@dataclass(frozen=True, slots=True)
class Csi500CalendarEvidence:
    """Caller-supplied identity and coverage bounds for an exchange calendar.

    Fields may be absent so a partial calendar declaration can still produce
    a diagnostic report. The generic evaluator returns ``UNKNOWN`` unless the
    calendar has an identity, version, evidence reference, and bounds covering
    the full requested interval.
    """

    calendar_id: str | None = None
    version: str | None = None
    evidence_reference: str | None = None
    covered_from: date | None = None
    covered_through: date | None = None


@dataclass(frozen=True, slots=True)
class Csi500IndependentStartEvidence:
    """A caller-declared independent reference for the index series start.

    The date must come from evidence independent of the observed rows. This
    value object records the caller's declaration; it does not authenticate
    the reference or decide whether the underlying source is authoritative.
    """

    inception_date: date
    evidence_reference: str


@dataclass(frozen=True, slots=True)
class Csi500SourceRetentionEvidence:
    """A caller-supplied bound on dates the source can currently provide.

    Omit this object when retention is unknown. A source retention bound is
    distinct from the local archive's observed date range.
    """

    evidence_reference: str
    retained_from: date | None = None
    retained_through: date | None = None


@dataclass(frozen=True, slots=True)
class Csi500HistoryReconciliationRequest:
    """Inputs to pure reconciliation for one requested CSI 500 date range.

    ``expected_business_dates`` must be supplied from an instrument-appropriate
    trading calendar. ``observed_business_dates`` must represent the caller's
    already-obtained rows for this exact series; the adapter does not fetch or
    persist them.
    """

    request_start: date
    request_end: date
    expected_business_dates: Iterable[date]
    observed_business_dates: Iterable[date]
    calendar: Csi500CalendarEvidence | None = None
    start_evidence: Csi500IndependentStartEvidence | None = None
    source_retention: Csi500SourceRetentionEvidence | None = None

    def __post_init__(self) -> None:
        """Validate the request through the shared bounded coverage contract."""
        if self.calendar is not None and type(self.calendar) is not Csi500CalendarEvidence:
            raise TypeError("calendar must be Csi500CalendarEvidence or None")
        if self.start_evidence is not None and type(self.start_evidence) is not (
            Csi500IndependentStartEvidence
        ):
            raise TypeError("start_evidence must be Csi500IndependentStartEvidence or None")
        if self.source_retention is not None and type(self.source_retention) is not (
            Csi500SourceRetentionEvidence
        ):
            raise TypeError("source_retention must be Csi500SourceRetentionEvidence or None")

        normalized = _to_coverage_request(self)
        object.__setattr__(self, "expected_business_dates", normalized.expected_business_dates)
        object.__setattr__(self, "observed_business_dates", normalized.observed_business_dates)


@dataclass(frozen=True, slots=True)
class Csi500HistoryReconciliationReport:
    """Coverage result plus explicit observed and evidence boundary summaries."""

    coverage: HistoryCoverageReport
    observed_earliest_date: date | None
    observed_latest_date: date | None
    start_evidence_supplied: bool
    source_retention_evidence_supplied: bool

    @property
    def status(self) -> CoverageStatus:
        """Return the requested-range status from the evidenced start through its end."""
        if (
            self.coverage.start_boundary_unknown
            or not self.coverage.business_calendar_covers_request
        ):
            return CoverageStatus.UNKNOWN
        if not self.coverage.scope_covers_known_start:
            return CoverageStatus.PARTIAL
        return self.coverage.status

    @property
    def requested_range_status(self) -> CoverageStatus:
        """Return the shared evaluator's diagnostic for the supplied date range."""
        return self.coverage.status

    def to_dict(self) -> dict[str, object]:
        """Serialize identity, independent evidence, and observed bounds separately."""
        start_boundary: dict[str, object] = {
            "status": "EVIDENCE_REFERENCE_SUPPLIED" if self.start_evidence_supplied else "UNKNOWN",
            "date": (
                self.coverage.known_start_date.isoformat()
                if self.coverage.known_start_date is not None
                else None
            ),
            "evidence_reference": self.coverage.independent_start_evidence,
            "authenticated_by_evaluator": False,
        }
        retention_boundary: dict[str, object] = {
            "status": (
                "EVIDENCE_REFERENCE_SUPPLIED"
                if self.source_retention_evidence_supplied
                else "UNKNOWN"
            ),
            "retained_from": (
                self.coverage.source_retained_from.isoformat()
                if self.coverage.source_retained_from is not None
                else None
            ),
            "retained_through": (
                self.coverage.source_retained_through.isoformat()
                if self.coverage.source_retained_through is not None
                else None
            ),
            "evidence_reference": self.coverage.source_retention_evidence,
            "authenticated_by_evaluator": False,
        }
        return {
            "schema_version": 1,
            "dataset": CSI500_DATASET_ID,
            "instrument_key": CSI500_SYMBOL,
            "source": INDEX_SOURCE_ID,
            "status": self.status.value,
            "status_scope": "requested_range_from_evidenced_start_through_requested_end",
            "status_through": self.coverage.requested_end.isoformat(),
            "scope_note": (
                "A COMPLETE status covers only the requested interval from the evidenced start "
                "through requested_end; no current or as-of horizon is inferred."
            ),
            "history_start_boundary": start_boundary,
            "observed_date_range": {
                "earliest_observed_date": (
                    self.observed_earliest_date.isoformat()
                    if self.observed_earliest_date is not None
                    else None
                ),
                "latest_observed_date": (
                    self.observed_latest_date.isoformat()
                    if self.observed_latest_date is not None
                    else None
                ),
            },
            "source_retention": retention_boundary,
            "requested_range_diagnostic": self.coverage.to_dict(),
            "evidence_authenticity": "caller_supplied_references_not_authenticated",
        }


def evaluate_csi500_history_reconciliation(
    request: Csi500HistoryReconciliationRequest,
) -> Csi500HistoryReconciliationReport:
    """Compare explicit CSI 500 trading sessions with supplied observation dates.

    The result's earliest observed date is descriptive only. It is never used as
    the independently evidenced history start. Missing start or calendar
    evidence therefore remains ``UNKNOWN`` even when every supplied expected
    date has an observation.

    Args:
        request: Bounded request with explicit calendar, start, and optional
            source-retention evidence.

    Returns:
        A report with generic head/interior/tail gaps and distinct evidence
        and observation boundaries.

    Raises:
        TypeError: If ``request`` is not a CSI 500 reconciliation request.
    """
    if type(request) is not Csi500HistoryReconciliationRequest:
        raise TypeError("request must be a Csi500HistoryReconciliationRequest")

    coverage_request = _to_coverage_request(request)
    coverage = evaluate_history_coverage(coverage_request)
    observed = coverage_request.observed_business_dates
    return Csi500HistoryReconciliationReport(
        coverage=coverage,
        observed_earliest_date=min(observed) if observed else None,
        observed_latest_date=max(observed) if observed else None,
        start_evidence_supplied=request.start_evidence is not None,
        source_retention_evidence_supplied=request.source_retention is not None,
    )


def _to_coverage_request(
    request: Csi500HistoryReconciliationRequest,
) -> HistoryCoverageRequest:
    """Adapt the fixed CSI 500 identity and caller evidence to the shared evaluator."""
    calendar = request.calendar
    start = request.start_evidence
    retention = request.source_retention
    return HistoryCoverageRequest(
        dataset=CSI500_DATASET_ID,
        source=INDEX_SOURCE_ID,
        instrument_key=CSI500_SYMBOL,
        request_start=request.request_start,
        request_end=request.request_end,
        expected_business_dates=request.expected_business_dates,
        observed_business_dates=request.observed_business_dates,
        calendar_id=calendar.calendar_id if calendar is not None else None,
        calendar_version=calendar.version if calendar is not None else None,
        business_calendar_evidence=(calendar.evidence_reference if calendar is not None else None),
        calendar_covered_from=calendar.covered_from if calendar is not None else None,
        calendar_covered_through=calendar.covered_through if calendar is not None else None,
        known_start_date=start.inception_date if start is not None else None,
        start_boundary_unknown=start is None,
        independent_start_evidence=start.evidence_reference if start is not None else None,
        source_retained_from=retention.retained_from if retention is not None else None,
        source_retained_through=retention.retained_through if retention is not None else None,
        source_retention_evidence=retention.evidence_reference if retention is not None else None,
    )
