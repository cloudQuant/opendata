"""Golden behavior for query-scoped daily coverage and its evidence boundaries."""

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from opendata.pipeline.query_window_coverage import (
    QueryCoverageStatus,
    QueryWindowCoverageRequest,
    UnsupportedQueryFrequencyError,
    evaluate_query_window_coverage,
)


def d(value: str) -> date:
    """Return a literal business date for an auditable test case."""
    return date.fromisoformat(value)


def weekdays_between(start: date, end_exclusive: date) -> tuple[date, ...]:
    """Build a synthetic weekday calendar for lifecycle clipping cases."""
    dates: list[date] = []
    current = start
    while current < end_exclusive:
        if current.weekday() < 5:
            dates.append(current)
        current += timedelta(days=1)
    return tuple(dates)


def request(**overrides: Any) -> QueryWindowCoverageRequest:
    """Build a complete five-day schedule from caller-declared evidence claims."""
    values: dict[str, Any] = {
        "dataset_id": "a_share_daily",
        "source_id": "authorized_archive",
        "instrument_id": "XSHG:600000",
        "start": d("2024-01-01"),
        "end_exclusive": d("2024-01-06"),
        "observed_dates": tuple(d(f"2024-01-0{day}") for day in range(1, 6)),
        "calendar_id": "XSHG",
        "calendar_version": "exchange-2024.1",
        "calendar_evidence_ref": "calendar-release:2024.1",
        "calendar_covered_from": d("2024-01-01"),
        "calendar_covered_before": d("2024-02-01"),
        "calendar_open_dates": tuple(d(f"2024-01-0{day}") for day in range(1, 6)),
        "calendar_daily_classification_complete": True,
        "listing_date": d("2023-12-20"),
        "listing_boundary_unknown": False,
        "listing_evidence_ref": "listing-notice:600000",
        "suspension_covered_from": d("2024-01-01"),
        "suspension_covered_before": d("2024-02-01"),
        "suspension_dates": (),
        "suspension_evidence_ref": "suspension-register:2024-01",
        "source_retention_unbounded": True,
        "source_retention_evidence_ref": "retention-policy:unbounded",
    }
    values.update(overrides)
    return QueryWindowCoverageRequest(**values)


def test_complete_local_window_does_not_claim_since_listing_history() -> None:
    report = evaluate_query_window_coverage(
        request(
            start=d("2024-01-02"),
            end_exclusive=d("2024-01-05"),
            observed_dates=(d("2024-01-02"), d("2024-01-03"), d("2024-01-04")),
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.requested_start == d("2024-01-02")
    assert report.requested_end_exclusive == d("2024-01-05")
    assert report.expected_dates == (d("2024-01-02"), d("2024-01-03"), d("2024-01-04"))
    assert report.listing_date == d("2023-12-20")
    assert "query-scoped" in report.reasons[-1]


def test_end_exclusive_is_not_expected_and_observation_at_edge_is_unexpected() -> None:
    report = evaluate_query_window_coverage(
        request(
            start=d("2024-01-01"),
            end_exclusive=d("2024-01-03"),
            observed_dates=(d("2024-01-01"), d("2024-01-02"), d("2024-01-03")),
        )
    )

    assert report.expected_dates == (d("2024-01-01"), d("2024-01-02"))
    assert report.unexpected_dates == (d("2024-01-03"),)
    assert report.status is QueryCoverageStatus.PARTIAL


def test_missing_rows_are_split_into_head_interior_and_tail_gaps() -> None:
    report = evaluate_query_window_coverage(
        request(
            observed_dates=(d("2024-01-02"), d("2024-01-04")),
        )
    )

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.head_gaps == (d("2024-01-01"),)
    assert report.interior_gaps == (d("2024-01-03"),)
    assert report.tail_gaps == (d("2024-01-05"),)


def test_missing_expected_rows_with_no_observations_are_never_complete() -> None:
    report = evaluate_query_window_coverage(request(observed_dates=()))

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.observed_row_count == 0
    assert report.head_gaps == report.expected_dates
    assert report.tail_gaps == report.expected_dates


def test_unknown_listing_boundary_blocks_complete_even_when_rows_match_calendar() -> None:
    report = evaluate_query_window_coverage(
        request(
            listing_date=None,
            listing_boundary_unknown=True,
            listing_evidence_ref=None,
        )
    )

    assert report.status is QueryCoverageStatus.UNKNOWN
    assert report.listing_boundary_unknown is True
    assert any("listing date is unknown" in reason for reason in report.reasons)


def test_incomplete_calendar_scope_blocks_complete() -> None:
    report = evaluate_query_window_coverage(request(calendar_daily_classification_complete=False))

    assert report.status is QueryCoverageStatus.UNKNOWN
    assert report.calendar_covers_evaluated_window is False


def test_unknown_suspension_classification_is_not_assumed_to_mean_not_suspended() -> None:
    report = evaluate_query_window_coverage(
        request(
            suspension_covered_from=None,
            suspension_covered_before=None,
            suspension_dates=(),
            suspension_evidence_ref=None,
        )
    )

    assert report.status is QueryCoverageStatus.UNKNOWN
    assert report.expected_dates == ()
    assert report.unclassified_open_dates == tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    assert report.unclassified_observed_dates == report.unclassified_open_dates
    assert any("suspension classification" in reason for reason in report.reasons)


def test_incomplete_suspension_scope_leaves_outside_open_dates_unclassified() -> None:
    report = evaluate_query_window_coverage(
        request(
            suspension_covered_from=d("2024-01-02"),
            suspension_covered_before=d("2024-01-05"),
            suspension_dates=(),
            suspension_evidence_ref="suspension-register:partial-window",
        )
    )

    assert report.expected_dates == (d("2024-01-02"), d("2024-01-03"), d("2024-01-04"))
    assert report.unclassified_open_dates == (d("2024-01-01"), d("2024-01-05"))
    assert report.status is QueryCoverageStatus.UNKNOWN


def test_declared_suspension_removes_that_date_from_expected_observations() -> None:
    report = evaluate_query_window_coverage(
        request(
            suspension_dates=(d("2024-01-03"),),
            observed_dates=(d("2024-01-01"), d("2024-01-02"), d("2024-01-04"), d("2024-01-05")),
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.expected_dates == (
        d("2024-01-01"),
        d("2024-01-02"),
        d("2024-01-04"),
        d("2024-01-05"),
    )
    assert report.unexpected_dates == ()


def test_observation_on_declared_suspension_date_is_unexpected() -> None:
    report = evaluate_query_window_coverage(request(suspension_dates=(d("2024-01-03"),)))

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.unexpected_dates == (d("2024-01-03"),)


def test_empty_holiday_only_window_is_not_misreported_as_complete() -> None:
    report = evaluate_query_window_coverage(
        request(
            start=d("2024-01-06"),
            end_exclusive=d("2024-01-08"),
            observed_dates=(),
            calendar_open_dates=(),
            suspension_covered_from=d("2024-01-06"),
            suspension_covered_before=d("2024-01-08"),
        )
    )

    assert report.status is QueryCoverageStatus.NOT_APPLICABLE
    assert report.expected_dates == ()
    assert report.observed_row_count == 0


def test_window_before_declared_listing_date_is_not_applicable() -> None:
    report = evaluate_query_window_coverage(
        request(
            start=d("2023-12-01"),
            end_exclusive=d("2023-12-10"),
            observed_dates=(),
            calendar_covered_from=d("2023-11-01"),
            calendar_covered_before=d("2024-02-01"),
            calendar_open_dates=(),
            suspension_covered_from=d("2023-11-01"),
            suspension_covered_before=d("2024-02-01"),
        )
    )

    assert report.status is QueryCoverageStatus.NOT_APPLICABLE
    assert report.evaluated_start == report.evaluated_end_exclusive == d("2023-12-10")
    assert report.lifecycle_start_truncated is True
    assert report.lifecycle_end_truncated is False
    assert report.lifecycle_truncation_reason is not None


def test_last_observable_business_date_clips_window_with_reason_and_reference() -> None:
    outside = evaluate_query_window_coverage(
        request(
            start=d("2024-01-01"),
            end_exclusive=d("2024-01-10"),
            observed_dates=tuple(d(f"2024-01-0{day}") for day in range(1, 6)),
            last_observable_business_date=d("2024-01-05"),
            last_observable_evidence_ref="observed-boundary:600000",
        )
    )

    assert outside.status is QueryCoverageStatus.PARTIAL
    assert outside.evaluated_end_exclusive == d("2024-01-06")
    assert outside.lifecycle_truncated is True
    assert outside.lifecycle_start_truncated is False
    assert outside.lifecycle_end_truncated is True
    assert outside.last_observable_business_date == d("2024-01-05")
    assert outside.last_observable_evidence_ref == "observed-boundary:600000"
    assert outside.lifecycle_truncation_reason is not None
    assert outside.to_dict()["lifecycle_boundary"] == {
        "last_observable_business_date": "2024-01-05",
        "evidence_ref": "observed-boundary:600000",
        "start_truncated": False,
        "start_truncation_reason": None,
        "start_boundary": {
            "date": "2023-12-20",
            "evidence_ref": "listing-notice:600000",
        },
        "end_truncated": True,
        "end_truncation_reason": outside.lifecycle_end_truncation_reason,
        "end_boundary": {
            "date": "2024-01-05",
            "evidence_ref": "observed-boundary:600000",
        },
        "truncated": True,
        "truncation_reason": outside.lifecycle_truncation_reason,
    }


def test_listing_date_clips_left_side_and_blocks_complete() -> None:
    listing = d("2023-12-20")
    end_exclusive = d("2024-01-06")
    expected = weekdays_between(listing, end_exclusive)
    report = evaluate_query_window_coverage(
        request(
            start=d("2023-12-01"),
            end_exclusive=end_exclusive,
            observed_dates=expected,
            calendar_covered_from=d("2023-12-01"),
            calendar_open_dates=expected,
            suspension_covered_from=d("2023-12-01"),
            suspension_covered_before=d("2024-02-01"),
        )
    )

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.requested_start == d("2023-12-01")
    assert report.evaluated_start == listing
    assert report.evaluated_end_exclusive == end_exclusive
    assert report.lifecycle_start_truncated is True
    assert report.lifecycle_end_truncated is False
    assert report.listing_evidence_ref == "listing-notice:600000"
    assert report.lifecycle_start_truncation_reason is not None
    assert report.expected_dates == expected


def test_listing_and_last_observable_dates_clip_both_sides() -> None:
    listing = d("2023-12-20")
    last_observable = d("2024-01-05")
    expected = weekdays_between(listing, d("2024-01-06"))
    report = evaluate_query_window_coverage(
        request(
            start=d("2023-12-01"),
            end_exclusive=d("2024-01-10"),
            observed_dates=expected,
            calendar_covered_from=d("2023-12-01"),
            calendar_open_dates=expected,
            suspension_covered_from=d("2023-12-01"),
            suspension_covered_before=d("2024-02-01"),
            last_observable_business_date=last_observable,
            last_observable_evidence_ref="observed-boundary:600000",
        )
    )

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.evaluated_start == listing
    assert report.evaluated_end_exclusive == d("2024-01-06")
    assert report.lifecycle_start_truncated is True
    assert report.lifecycle_end_truncated is True
    assert report.lifecycle_truncated is True
    assert report.lifecycle_start_truncation_reason is not None
    assert report.lifecycle_end_truncation_reason is not None
    payload = report.to_dict()["lifecycle_boundary"]
    assert isinstance(payload, dict)
    assert payload["start_boundary"] == {
        "date": "2023-12-20",
        "evidence_ref": "listing-notice:600000",
    }
    assert payload["end_boundary"] == {
        "date": "2024-01-05",
        "evidence_ref": "observed-boundary:600000",
    }


def test_last_observable_business_date_at_exclusive_end_does_not_clip() -> None:
    report = evaluate_query_window_coverage(
        request(
            last_observable_business_date=d("2024-01-05"),
            last_observable_evidence_ref="observed-boundary:600000",
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.lifecycle_truncated is False
    assert report.lifecycle_start_truncated is False
    assert report.lifecycle_end_truncated is False
    assert report.evaluated_end_exclusive == d("2024-01-06")
    assert report.lifecycle_truncation_reason is None


def test_last_observable_business_date_requires_its_own_reference() -> None:
    with pytest.raises(ValueError, match="last_observable_business_date and its evidence"):
        request(last_observable_business_date=d("2024-01-05"))


def test_last_observable_business_date_must_be_open_and_not_suspended() -> None:
    with pytest.raises(ValueError, match="must be an open date"):
        request(
            last_observable_business_date=d("2024-01-06"),
            last_observable_evidence_ref="last-row:600000",
        )
    with pytest.raises(ValueError, match="cannot be declared suspended"):
        request(
            last_observable_business_date=d("2024-01-03"),
            last_observable_evidence_ref="last-row:600000",
            suspension_dates=(d("2024-01-03"),),
        )


def test_source_retention_reports_missing_dates_outside_declared_half_open_range() -> None:
    report = evaluate_query_window_coverage(
        request(
            observed_dates=(d("2024-01-03"), d("2024-01-04"), d("2024-01-05")),
            source_retained_from=d("2024-01-03"),
            source_retained_before=d("2024-01-06"),
            source_retention_evidence_ref="provider-policy:2024-01",
            source_retention_unbounded=False,
        )
    )

    assert report.status is QueryCoverageStatus.SOURCE_LIMITED
    assert report.source_limited_missing_dates == (d("2024-01-01"), d("2024-01-02"))
    assert report.head_gaps == (d("2024-01-01"), d("2024-01-02"))


def test_source_retained_before_is_exclusive_at_the_right_boundary() -> None:
    report = evaluate_query_window_coverage(
        request(
            end_exclusive=d("2024-01-07"),
            observed_dates=tuple(d(f"2024-01-0{day}") for day in range(1, 6)),
            calendar_open_dates=tuple(d(f"2024-01-0{day}") for day in range(1, 7)),
            source_retention_unbounded=False,
            source_retained_from=d("2024-01-01"),
            source_retained_before=d("2024-01-06"),
            source_retention_evidence_ref="retention-manifest:exclusive-right-edge",
        )
    )

    assert report.status is QueryCoverageStatus.SOURCE_LIMITED
    assert report.expected_dates[-1] == d("2024-01-06")
    assert report.source_limited_missing_dates == (d("2024-01-06"),)


def test_source_retention_with_only_one_known_edge_is_validated_and_reported() -> None:
    report = evaluate_query_window_coverage(
        request(
            observed_dates=(d("2024-01-03"), d("2024-01-04"), d("2024-01-05")),
            source_retained_from=d("2024-01-03"),
            source_retention_evidence_ref="rolling-history-policy",
            source_retention_unbounded=False,
        )
    )

    assert report.status is QueryCoverageStatus.SOURCE_LIMITED
    assert report.source_retained_from == d("2024-01-03")
    assert report.source_retained_before is None


def test_unknown_source_retention_blocks_complete_even_when_all_rows_match() -> None:
    report = evaluate_query_window_coverage(
        request(
            source_retention_unbounded=False,
            source_retention_evidence_ref=None,
        )
    )

    assert report.status is QueryCoverageStatus.UNKNOWN
    assert report.source_retention_status.value == "UNKNOWN"
    assert report.source_retention_covers_evaluated_window is None


def test_caller_declared_retention_boundary_covering_window_allows_complete() -> None:
    report = evaluate_query_window_coverage(
        request(
            source_retention_unbounded=False,
            source_retained_from=d("2024-01-01"),
            source_retained_before=d("2024-01-06"),
            source_retention_evidence_ref="retention-manifest:2024-01",
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.source_retention_status.value == "COVERS_WINDOW"
    assert report.source_retention_covers_evaluated_window is True


def test_one_sided_retention_end_can_cover_window_when_its_lower_edge_is_unbounded() -> None:
    report = evaluate_query_window_coverage(
        request(
            source_retention_unbounded=False,
            source_retained_before=d("2024-01-06"),
            source_retention_evidence_ref="retention-manifest:upper-bound-only",
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.source_retention_status.value == "COVERS_WINDOW"
    assert report.source_retention_covers_evaluated_window is True


def test_caller_declared_retention_boundary_outside_window_is_source_limited() -> None:
    report = evaluate_query_window_coverage(
        request(
            source_retention_unbounded=False,
            source_retained_from=d("2024-01-02"),
            source_retention_evidence_ref="retention-manifest:rolling",
        )
    )

    assert report.status is QueryCoverageStatus.SOURCE_LIMITED
    assert report.source_retention_status.value == "DOES_NOT_COVER_WINDOW"
    assert report.source_retention_covers_evaluated_window is False


def test_duplicates_and_observations_outside_query_are_reported() -> None:
    report = evaluate_query_window_coverage(
        request(observed_dates=(d("2024-01-01"), d("2024-01-02"), d("2024-01-02"), d("2024-01-06")))
    )

    assert report.status is QueryCoverageStatus.PARTIAL
    assert report.duplicate_dates == (d("2024-01-02"),)
    assert report.unexpected_dates == (d("2024-01-06"),)


def test_calendar_closure_is_not_a_missing_observation() -> None:
    report = evaluate_query_window_coverage(
        request(
            calendar_open_dates=(d("2024-01-01"), d("2024-01-03"), d("2024-01-05")),
            suspension_dates=(),
            observed_dates=(d("2024-01-01"), d("2024-01-03"), d("2024-01-05")),
        )
    )

    assert report.status is QueryCoverageStatus.COMPLETE
    assert report.expected_dates == (d("2024-01-01"), d("2024-01-03"), d("2024-01-05"))


def test_serialized_contract_keeps_query_scope_and_exclusive_endpoint_explicit() -> None:
    payload = evaluate_query_window_coverage(
        request(
            start=d("2024-01-02"),
            end_exclusive=d("2024-01-05"),
            observed_dates=(d("2024-01-02"), d("2024-01-03"), d("2024-01-04")),
        )
    ).to_dict()

    assert payload["scope"] == "query_window"
    assert payload["requested_window"] == {
        "start": "2024-01-02",
        "end_exclusive": "2024-01-05",
    }
    assert payload["status"] == "COMPLETE"
    assert payload["evidence_binding"] == {
        "status": "unverified_caller_assertion",
        "trusted_resolver_required": True,
        "assessment_is_authoritative": False,
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("start", datetime(2024, 1, 1), "datetime.date"),
        ("end_exclusive", d("2024-01-01"), "start < end_exclusive"),
        ("calendar_covered_before", d("2024-01-01"), "start < end_exclusive"),
        ("calendar_open_dates", (d("2024-02-01"),), "inside declared calendar coverage"),
        ("suspension_dates", (d("2024-02-01"),), "inside the declared suspension interval"),
        ("suspension_evidence_ref", None, "bounds and evidence"),
        ("frequency_semantics", "intraday", "only daily_business_date"),
    ],
)
def test_invalid_bounds_evidence_and_frequency_fail_closed(
    field: str, value: object, message: str
) -> None:
    with pytest.raises((ValueError, UnsupportedQueryFrequencyError), match=message):
        request(**{field: value})


def test_listing_evidence_reference_must_be_present_or_boundary_unknown() -> None:
    with pytest.raises(ValueError, match="known listing boundary requires"):
        request(
            listing_date=d("2023-12-20"),
            listing_boundary_unknown=False,
            listing_evidence_ref=None,
        )
    with pytest.raises(ValueError, match="unknown listing boundary cannot"):
        request(
            listing_date=None,
            listing_boundary_unknown=True,
            listing_evidence_ref="row-min-date",
        )


def test_source_retention_bounds_must_form_a_valid_half_open_interval() -> None:
    with pytest.raises(ValueError, match="source retention requires from < before"):
        request(
            source_retention_unbounded=False,
            source_retained_from=d("2024-01-05"),
            source_retained_before=d("2024-01-01"),
            source_retention_evidence_ref="bad-retention-manifest",
        )


def test_unbounded_source_retention_requires_evidence_and_excludes_finite_bounds() -> None:
    with pytest.raises(ValueError, match="unbounded retention requires"):
        request(source_retention_evidence_ref=None)
    with pytest.raises(ValueError, match="cannot also declare finite boundaries"):
        request(source_retained_from=d("2024-01-01"))


def test_suspension_dates_without_scope_evidence_are_rejected() -> None:
    with pytest.raises(ValueError, match="suspension coverage bounds and evidence"):
        request(
            suspension_covered_from=None,
            suspension_covered_before=None,
            suspension_dates=(d("2024-01-03"),),
            suspension_evidence_ref="partial-feed",
        )


def test_request_type_is_checked_and_report_is_immutable() -> None:
    with pytest.raises(TypeError, match="QueryWindowCoverageRequest"):
        evaluate_query_window_coverage(object())  # type: ignore[arg-type]
    report = evaluate_query_window_coverage(request())
    with pytest.raises(FrozenInstanceError):
        report.status = QueryCoverageStatus.PARTIAL  # type: ignore[misc]


def test_bounded_date_materialization_stops_after_limit() -> None:
    too_many = (d("2024-01-01") for _ in range(100_001))
    with pytest.raises(ValueError, match="evaluation limit"):
        request(calendar_open_dates=too_many)
