"""Golden vectors for the offline CSI 500 history coverage adapter."""

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import cast

import pytest

from opendata.pipeline.csi500_history_reconciliation import (
    CSI500_DATASET_ID,
    CSI500_SYMBOL,
    Csi500CalendarEvidence,
    Csi500HistoryReconciliationRequest,
    Csi500IndependentStartEvidence,
    Csi500SourceRetentionEvidence,
    evaluate_csi500_history_reconciliation,
)
from opendata.pipeline.history_coverage import CoverageStatus

_REQUEST_START = date(2024, 1, 2)
_REQUEST_END = date(2024, 1, 9)
_SESSIONS = (
    date(2024, 1, 2),
    date(2024, 1, 3),
    date(2024, 1, 4),
    date(2024, 1, 5),
    date(2024, 1, 8),
    date(2024, 1, 9),
)
_CALENDAR = Csi500CalendarEvidence(
    calendar_id="SSE-SYNTHETIC-FIXTURE",
    version="fixture-v1",
    evidence_reference="fixture:calendar:2024-01-02/2024-01-09",
    covered_from=_REQUEST_START,
    covered_through=_REQUEST_END,
)
_START = Csi500IndependentStartEvidence(
    inception_date=_REQUEST_START,
    evidence_reference="fixture:independent-index-start:2024-01-02",
)


def make_request(
    observed: tuple[date, ...],
    *,
    expected: tuple[date, ...] = _SESSIONS,
    calendar: Csi500CalendarEvidence | None = _CALENDAR,
    start: Csi500IndependentStartEvidence | None = _START,
    source_retention: Csi500SourceRetentionEvidence | None = None,
    request_start: date = _REQUEST_START,
    request_end: date = _REQUEST_END,
) -> Csi500HistoryReconciliationRequest:
    """Build a request from synthetic calendar and start evidence only."""
    return Csi500HistoryReconciliationRequest(
        request_start=request_start,
        request_end=request_end,
        expected_business_dates=expected,
        observed_business_dates=observed,
        calendar=calendar,
        start_evidence=start,
        source_retention=source_retention,
    )


def mapping_field(value: object) -> Mapping[str, object]:
    """Narrow serialized object fields without weakening test assertions."""
    assert isinstance(value, Mapping)
    return value


def test_complete_synthetic_series_keeps_source_retention_unknown() -> None:
    report = evaluate_csi500_history_reconciliation(make_request(_SESSIONS))
    source_retention = mapping_field(report.to_dict()["source_retention"])

    assert report.status is CoverageStatus.COMPLETE
    assert report.coverage.dataset == CSI500_DATASET_ID
    assert report.coverage.instrument_key == CSI500_SYMBOL
    assert report.observed_earliest_date == _REQUEST_START
    assert report.coverage.known_start_date == _REQUEST_START
    assert report.source_retention_evidence_supplied is False
    assert source_retention["status"] == "UNKNOWN"


def test_first_observed_day_does_not_replace_the_evidenced_history_start() -> None:
    observed = _SESSIONS[2:]

    report = evaluate_csi500_history_reconciliation(make_request(observed))
    serialized = report.to_dict()
    history_start = mapping_field(serialized["history_start_boundary"])
    observed_range = mapping_field(serialized["observed_date_range"])

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.known_start_date == _REQUEST_START
    assert report.observed_earliest_date == date(2024, 1, 4)
    assert report.coverage.head_gaps == _SESSIONS[:2]
    assert history_start["date"] == "2024-01-02"
    assert observed_range["earliest_observed_date"] == "2024-01-04"
    assert history_start["authenticated_by_evaluator"] is False


def test_interior_gap_is_reported_even_when_first_and_last_sessions_are_present() -> None:
    observed = tuple(item for item in _SESSIONS if item != date(2024, 1, 4))

    report = evaluate_csi500_history_reconciliation(make_request(observed))

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.head_gaps == ()
    assert report.coverage.interior_gaps == (date(2024, 1, 4),)
    assert report.coverage.tail_gaps == ()
    assert report.coverage.observed_row_count == len(_SESSIONS) - 1


def test_missing_sessions_after_the_last_observation_are_tail_gaps() -> None:
    report = evaluate_csi500_history_reconciliation(make_request(_SESSIONS[:4]))

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.head_gaps == ()
    assert report.coverage.interior_gaps == ()
    assert report.coverage.tail_gaps == _SESSIONS[4:]
    assert report.observed_latest_date == date(2024, 1, 5)


def test_unknown_start_stays_unknown_when_all_requested_sessions_are_observed() -> None:
    report = evaluate_csi500_history_reconciliation(make_request(_SESSIONS, start=None))
    serialized = report.to_dict()
    history_start = mapping_field(serialized["history_start_boundary"])

    assert report.status is CoverageStatus.UNKNOWN
    assert report.coverage.known_start_date is None
    assert report.observed_earliest_date == _REQUEST_START
    assert history_start["status"] == "UNKNOWN"
    assert history_start["date"] is None


@pytest.mark.parametrize(
    "calendar",
    [
        None,
        Csi500CalendarEvidence(
            calendar_id="SSE-SYNTHETIC-FIXTURE",
            version=None,
            evidence_reference="fixture:missing-calendar-version",
            covered_from=_REQUEST_START,
            covered_through=_REQUEST_END,
        ),
        Csi500CalendarEvidence(
            calendar_id="SSE-SYNTHETIC-FIXTURE",
            version="fixture-v1",
            evidence_reference="fixture:truncated-calendar",
            covered_from=date(2024, 1, 3),
            covered_through=_REQUEST_END,
        ),
    ],
)
def test_missing_or_incomplete_calendar_evidence_prevents_complete_status(
    calendar: Csi500CalendarEvidence | None,
) -> None:
    report = evaluate_csi500_history_reconciliation(make_request(_SESSIONS, calendar=calendar))

    assert report.status is CoverageStatus.UNKNOWN
    assert report.coverage.business_calendar_covers_request is False
    assert report.coverage.covered_expected_date_count == len(_SESSIONS)


def test_explicit_trading_sessions_do_not_create_weekend_gaps() -> None:
    report = evaluate_csi500_history_reconciliation(make_request(_SESSIONS))

    assert report.status is CoverageStatus.COMPLETE
    assert date(2024, 1, 6) not in report.coverage.expected_dates
    assert date(2024, 1, 7) not in report.coverage.expected_dates
    assert report.coverage.head_gaps == ()
    assert report.coverage.interior_gaps == ()
    assert report.coverage.tail_gaps == ()


def test_unknown_source_retention_does_not_turn_a_gap_into_source_limited() -> None:
    report = evaluate_csi500_history_reconciliation(
        make_request(_SESSIONS[2:], source_retention=None)
    )
    source_retention = mapping_field(report.to_dict()["source_retention"])

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.head_gaps == _SESSIONS[:2]
    assert source_retention["status"] == "UNKNOWN"
    assert report.coverage.source_retained_from is None
    assert report.coverage.source_retained_through is None


def test_declared_retention_boundary_can_explain_unavailable_head_dates() -> None:
    retention = Csi500SourceRetentionEvidence(
        evidence_reference="fixture:source-retention-policy",
        retained_from=date(2024, 1, 4),
    )
    report = evaluate_csi500_history_reconciliation(
        make_request(_SESSIONS[2:], source_retention=retention)
    )
    source_retention_report = mapping_field(report.to_dict()["source_retention"])

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.head_gaps == _SESSIONS[:2]
    assert source_retention_report["status"] == "EVIDENCE_REFERENCE_SUPPLIED"
    assert source_retention_report["authenticated_by_evaluator"] is False


def test_unknown_start_remains_unknown_while_range_diagnostic_keeps_source_limit() -> None:
    retention = Csi500SourceRetentionEvidence(
        evidence_reference="fixture:source-retention-policy",
        retained_from=date(2024, 1, 4),
    )
    report = evaluate_csi500_history_reconciliation(
        make_request(_SESSIONS[2:], start=None, source_retention=retention)
    )
    serialized = report.to_dict()
    range_diagnostic = mapping_field(serialized["requested_range_diagnostic"])

    assert report.status is CoverageStatus.UNKNOWN
    assert report.requested_range_status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.head_gaps == _SESSIONS[:2]
    assert any(
        "outside the declared source retention boundary" in reason
        for reason in report.coverage.reasons
    )
    assert serialized["status"] == "UNKNOWN"
    assert range_diagnostic["status"] == "SOURCE_LIMITED"
    assert (
        serialized["status_scope"] == "requested_range_from_evidenced_start_through_requested_end"
    )


def test_local_archive_before_current_source_retention_can_be_complete() -> None:
    retention = Csi500SourceRetentionEvidence(
        evidence_reference="fixture:source-retention-policy",
        retained_from=date(2024, 1, 4),
    )
    report = evaluate_csi500_history_reconciliation(
        make_request(_SESSIONS, source_retention=retention)
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.observed_earliest_date == _REQUEST_START
    assert report.coverage.source_retained_from == date(2024, 1, 4)


def test_complete_status_stops_at_the_explicit_request_end() -> None:
    requested_end = date(2024, 1, 5)
    report = evaluate_csi500_history_reconciliation(
        make_request(_SESSIONS[:4], request_end=requested_end)
    )
    serialized = report.to_dict()

    assert report.status is CoverageStatus.COMPLETE
    assert report.coverage.requested_end == requested_end
    assert serialized["status_through"] == "2024-01-05"
    assert serialized["scope_note"] == (
        "A COMPLETE status covers only the requested interval from the evidenced start through "
        "requested_end; no current or as-of horizon is inferred."
    )


def test_start_evidence_after_requested_start_does_not_claim_full_history() -> None:
    request = make_request(
        _SESSIONS[2:],
        start=Csi500IndependentStartEvidence(
            inception_date=_REQUEST_START,
            evidence_reference="fixture:independent-index-start:2024-01-02",
        ),
        request_start=date(2024, 1, 4),
    )

    report = evaluate_csi500_history_reconciliation(request)

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.scope_covers_known_start is False
    assert report.coverage.known_start_date == _REQUEST_START
    assert report.observed_earliest_date == date(2024, 1, 4)


def test_range_source_limit_does_not_hide_a_range_that_omits_the_evidenced_start() -> None:
    retention = Csi500SourceRetentionEvidence(
        evidence_reference="fixture:source-retention-policy",
        retained_through=date(2024, 1, 5),
    )
    report = evaluate_csi500_history_reconciliation(
        make_request(
            _SESSIONS[2:4],
            request_start=date(2024, 1, 4),
            source_retention=retention,
        )
    )

    assert report.status is CoverageStatus.PARTIAL
    assert report.requested_range_status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.scope_covers_known_start is False
    assert report.coverage.tail_gaps == _SESSIONS[4:]


def test_empty_observations_expose_the_unanchored_missing_range() -> None:
    report = evaluate_csi500_history_reconciliation(make_request(()))

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.head_gaps == _SESSIONS
    assert report.coverage.tail_gaps == _SESSIONS
    assert report.observed_earliest_date is None
    assert report.observed_latest_date is None


def test_request_rejects_wrong_evidence_value_object_types() -> None:
    request = make_request(_SESSIONS)

    with pytest.raises(TypeError, match="calendar must be Csi500CalendarEvidence"):
        replace(request, calendar=cast("Csi500CalendarEvidence", object()))
    with pytest.raises(TypeError, match="start_evidence must be Csi500IndependentStartEvidence"):
        replace(request, start_evidence=cast("Csi500IndependentStartEvidence", object()))
    with pytest.raises(TypeError, match="source_retention must be Csi500SourceRetentionEvidence"):
        replace(request, source_retention=cast("Csi500SourceRetentionEvidence", object()))


def test_evaluator_rejects_non_csi500_reconciliation_request() -> None:
    with pytest.raises(TypeError, match="request must be a Csi500HistoryReconciliationRequest"):
        evaluate_csi500_history_reconciliation(cast("Csi500HistoryReconciliationRequest", object()))
