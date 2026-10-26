"""Golden cases for daily full-history coverage evaluation."""

from collections.abc import Iterable
from datetime import date, datetime

import pytest

from opendata.pipeline import history_coverage
from opendata.pipeline.history_coverage import (
    CoverageStatus,
    FrequencySemantics,
    HistoryCoverageRequest,
    UnsupportedFrequencyError,
    evaluate_history_coverage,
)


def d(value: str) -> date:
    """Build a literal golden date."""
    return date.fromisoformat(value)


def request(
    expected: Iterable[date],
    observed: Iterable[date],
    **overrides: object,
) -> HistoryCoverageRequest:
    """Create a known-start daily series request with explicit calendar evidence."""
    values: dict[str, object] = {
        "dataset": "equity_daily",
        "source": "exchange_archive",
        "instrument_key": "XNAS:EXAMPLE",
        "request_start": d("2024-01-01"),
        "request_end": d("2024-01-05"),
        "expected_business_dates": expected,
        "observed_business_dates": observed,
        "calendar_id": "XNYS",
        "calendar_version": "XNYS-2024.1",
        "business_calendar_evidence": "exchange calendar release manifest",
        "calendar_covered_from": d("2024-01-01"),
        "calendar_covered_through": d("2024-12-31"),
        "known_start_date": d("2024-01-01"),
        "start_boundary_unknown": False,
        "independent_start_evidence": "official listing notice dated 2024-01-01",
    }
    values.update(overrides)
    return HistoryCoverageRequest(**values)


def test_same_last_date_and_row_count_still_exposes_interior_gap() -> None:
    expected = tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    observed = (d("2024-01-01"), d("2024-01-02"), d("2024-01-04"), d("2024-01-05"))

    report = evaluate_history_coverage(request(expected, observed))

    assert report.status is CoverageStatus.PARTIAL
    assert report.head_gaps == ()
    assert report.interior_gaps == (d("2024-01-03"),)
    assert report.tail_gaps == ()
    assert report.observed_row_count == 4
    assert max(observed) == d("2024-01-05")


def test_late_start_is_reported_as_head_gap_against_independent_origin() -> None:
    expected = tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    observed = (d("2024-01-03"), d("2024-01-04"), d("2024-01-05"))

    report = evaluate_history_coverage(request(expected, observed))

    assert report.status is CoverageStatus.PARTIAL
    assert report.known_start_date == d("2024-01-01")
    assert report.head_gaps == (d("2024-01-01"), d("2024-01-02"))
    assert report.interior_gaps == report.tail_gaps == ()


def test_source_retention_boundary_is_reported_without_claiming_completion() -> None:
    expected = tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    observed = (d("2024-01-03"), d("2024-01-04"), d("2024-01-05"))
    request_data = request(
        expected,
        observed,
        source_retained_from=d("2024-01-03"),
        source_retention_evidence="provider history policy, checked 2024-02-01",
    )

    report = evaluate_history_coverage(request_data)

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.source_retained_from == d("2024-01-03")
    assert report.head_gaps == (d("2024-01-01"), d("2024-01-02"))
    assert "outside the declared source retention boundary" in report.reasons[0]


def test_archived_observations_before_current_source_retention_can_be_complete() -> None:
    expected = tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            source_retained_from=d("2024-01-03"),
            source_retention_evidence="provider now retains only data from 2024-01-03",
        )
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.source_retained_from == d("2024-01-03")
    assert report.head_gaps == ()


def test_unknown_start_stays_unknown_even_when_requested_rows_are_present() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    observed = expected
    request_data = request(
        expected,
        observed,
        request_end=d("2024-01-03"),
        known_start_date=None,
        start_boundary_unknown=True,
        independent_start_evidence=None,
    )

    report = evaluate_history_coverage(request_data)

    assert report.status is CoverageStatus.UNKNOWN
    assert report.known_start_date is None
    assert report.start_boundary_unknown is True
    assert "cannot establish an instrument inception date" in report.reasons[0]


def test_trusted_exchange_calendar_does_not_invent_weekend_business_dates() -> None:
    expected = (d("2024-01-05"), d("2024-01-08"), d("2024-01-09"))
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            request_start=d("2024-01-05"),
            request_end=d("2024-01-09"),
            known_start_date=d("2024-01-05"),
        )
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.expected_dates == expected
    assert report.unexpected_dates == ()


def test_delisted_instrument_scope_ends_at_caller_supplied_range() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            request_end=d("2024-01-03"),
            source_retained_through=d("2024-01-03"),
            source_retention_evidence="delisting archive manifest",
        )
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.requested_end == d("2024-01-03")
    assert report.tail_gaps == ()


def test_duplicate_and_unexpected_observed_dates_are_both_visible() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    observed = (
        d("2024-01-01"),
        d("2024-01-01"),
        d("2024-01-03"),
        d("2024-01-06"),
    )

    report = evaluate_history_coverage(request(expected, observed))

    assert report.status is CoverageStatus.PARTIAL
    assert report.duplicate_dates == (d("2024-01-01"),)
    assert report.unexpected_dates == (d("2024-01-06"),)
    assert report.interior_gaps == (d("2024-01-02"),)


def test_empty_expected_calendar_is_not_applicable_and_empty_observations_are_partial() -> None:
    no_sessions = evaluate_history_coverage(request((), ()))
    no_rows = evaluate_history_coverage(request((d("2024-01-01"), d("2024-01-02")), ()))

    assert no_sessions.status is CoverageStatus.NOT_APPLICABLE
    assert no_sessions.business_calendar_covers_request is True
    assert no_sessions.expected_dates == ()
    assert no_rows.status is CoverageStatus.PARTIAL
    assert no_rows.head_gaps == (d("2024-01-01"), d("2024-01-02"))
    assert no_rows.tail_gaps == no_rows.head_gaps
    assert no_rows.interior_gaps == ()


def test_empty_expected_set_without_calendar_coverage_is_unknown() -> None:
    report = evaluate_history_coverage(
        request(
            (),
            (),
            calendar_id=None,
            calendar_version=None,
            business_calendar_evidence=None,
            calendar_covered_from=None,
            calendar_covered_through=None,
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert report.business_calendar_covers_request is False
    assert "cannot certify the full requested range" in report.reasons[0]


def test_empty_calendar_sessions_do_not_hide_unknown_instrument_start() -> None:
    report = evaluate_history_coverage(
        request(
            (),
            (),
            known_start_date=None,
            start_boundary_unknown=True,
            independent_start_evidence=None,
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert any("historical start boundary is unknown" in reason for reason in report.reasons)


def test_truncated_calendar_coverage_cannot_certify_a_present_subset_as_complete() -> None:
    expected = (d("2024-01-03"),)
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            calendar_covered_from=d("2024-01-02"),
            calendar_covered_through=d("2024-01-05"),
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert report.business_calendar_covers_request is False
    assert report.covered_expected_date_count == 1


def test_missing_calendar_coverage_keeps_source_limit_as_a_reason_but_status_unknown() -> None:
    expected = tuple(d(f"2024-01-0{day}") for day in range(1, 6))
    report = evaluate_history_coverage(
        request(
            expected,
            expected[2:],
            calendar_covered_from=d("2024-01-03"),
            source_retained_from=d("2024-01-03"),
            source_retention_evidence="provider retention policy",
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert any(
        "outside the declared source retention boundary" in reason for reason in report.reasons
    )
    assert report.head_gaps == expected[:2]


def test_business_key_identity_and_report_json_are_explicit_and_stable() -> None:
    daily = (d("2024-01-01"), d("2024-01-02"))
    report = evaluate_history_coverage(
        request(
            daily,
            daily,
            instrument_key=None,
            business_key="series_id=GDP;geo=US",
            request_end=d("2024-01-02"),
        )
    )

    serialized = report.to_dict()
    assert report.status is CoverageStatus.COMPLETE
    assert serialized["series"] == {
        "instrument_key": None,
        "business_key": "series_id=GDP;geo=US",
        "frequency_semantics": "daily_business_date",
    }
    assert serialized["boundaries"]["known_start"]["independent_evidence"] == (
        "official listing notice dated 2024-01-01"
    )
    assert serialized["business_calendar"] == {
        "id": "XNYS",
        "version": "XNYS-2024.1",
        "evidence": "exchange calendar release manifest",
        "covered_from": "2024-01-01",
        "covered_through": "2024-12-31",
        "covers_request": True,
    }
    assert report.to_json() == report.to_json()
    assert '"status":"COMPLETE"' in report.to_json()


def test_snapshot_only_source_cannot_certify_multi_date_history() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    report = evaluate_history_coverage(
        request(expected, expected, request_end=d("2024-01-03"), snapshot_only=True)
    )

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert any("snapshot-only" in reason for reason in report.reasons)


def test_snapshot_only_single_observation_without_archive_evidence_is_limited() -> None:
    one_day = (d("2024-01-01"),)
    report = evaluate_history_coverage(
        request(one_day, one_day, request_end=d("2024-01-01"), snapshot_only=True)
    )

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.unarchived_dates == one_day


def test_snapshot_archive_evidence_allows_full_coverage_inside_archived_interval() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            request_end=d("2024-01-03"),
            snapshot_only=True,
            snapshot_archive_from=d("2024-01-01"),
            snapshot_archive_through=d("2024-01-03"),
            snapshot_archive_evidence="daily archive manifest, verified 2024-02-01",
        )
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.unarchived_dates == ()
    assert report.to_dict()["boundaries"]["snapshot_archive"]["evidence"] == (
        "daily archive manifest, verified 2024-02-01"
    )


def test_snapshot_archive_gap_outside_evidenced_interval_stays_source_limited() -> None:
    expected = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
    report = evaluate_history_coverage(
        request(
            expected,
            (d("2024-01-02"), d("2024-01-03")),
            request_end=d("2024-01-03"),
            snapshot_only=True,
            snapshot_archive_from=d("2024-01-02"),
            snapshot_archive_through=d("2024-01-03"),
            snapshot_archive_evidence="daily archive manifest",
        )
    )

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.unarchived_dates == (d("2024-01-01"),)
    assert report.head_gaps == (d("2024-01-01"),)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"request_start": d("2024-01-05"), "request_end": d("2024-01-01")}, "request_start"),
        (
            {
                "source_retained_from": d("2024-01-04"),
                "source_retained_through": d("2024-01-02"),
                "source_retention_evidence": "provider policy",
            },
            "source_retained_from",
        ),
        (
            {"known_start_date": d("2024-01-01"), "start_boundary_unknown": True},
            "unknown start boundary",
        ),
        (
            {
                "snapshot_only": True,
                "snapshot_archive_from": d("2024-01-04"),
                "snapshot_archive_through": d("2024-01-02"),
                "snapshot_archive_evidence": "contradictory archive manifest",
            },
            "snapshot_archive_from",
        ),
        (
            {
                "calendar_covered_from": d("2024-01-04"),
                "calendar_covered_through": d("2024-01-02"),
            },
            "calendar_covered_from",
        ),
    ],
)
def test_conflicting_boundaries_are_rejected(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        request((d("2024-01-01"),), (d("2024-01-01"),), **overrides)


def test_intraday_frequency_is_rejected_with_explicit_scope_message() -> None:
    with pytest.raises(UnsupportedFrequencyError, match="intraday coverage is not supported"):
        request(
            (d("2024-01-01"),),
            (d("2024-01-01"),),
            frequency_semantics="intraday",
        )


def test_iterable_size_is_bounded_before_full_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(history_coverage, "MAX_DAILY_DATES", 2)
    values = (d(f"2024-01-0{day}") for day in range(1, 4))

    with pytest.raises(ValueError, match="evaluation limit"):
        request(values, ())


def test_expected_dates_reject_duplicates_and_datetime_values() -> None:
    with pytest.raises(ValueError, match="must not contain duplicates"):
        request((d("2024-01-01"), d("2024-01-01")), ())
    with pytest.raises(ValueError, match="datetime.date"):
        request((date(2024, 1, 1),), (), request_start=datetime(2024, 1, 1))
    with pytest.raises(ValueError, match="must be a datetime.date value"):
        request((None,), ())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dataset": "   "}, "dataset must be a nonempty string"),
        ({"source": ""}, "source must be a nonempty string"),
        (
            {"instrument_key": None, "business_key": None},
            "provide exactly one of instrument_key or business_key",
        ),
        (
            {"business_key": "a composite key"},
            "provide exactly one of instrument_key or business_key",
        ),
        ({"instrument_key": " "}, "instrument_key must be a nonempty string"),
        ({"frequency_semantics": "weekly"}, "only daily_business_date is supported"),
        ({"start_boundary_unknown": 1}, "start_boundary_unknown must be a bool"),
        (
            {"known_start_date": None, "start_boundary_unknown": False},
            "known_start_date is required",
        ),
        (
            {
                "known_start_date": None,
                "start_boundary_unknown": True,
                "independent_start_evidence": "stale record",
            },
            "unknown start boundary cannot include",
        ),
        ({"independent_start_evidence": None}, "independent_start_evidence is required"),
        (
            {"independent_start_evidence": "  "},
            "independent_start_evidence must be a nonempty string",
        ),
        (
            {"source_retained_from": d("2024-01-02")},
            "source_retention_evidence is required",
        ),
        (
            {"source_retention_evidence": "unsupported boundary"},
            "source_retention_evidence requires a retention boundary",
        ),
        (
            {
                "source_retained_through": d("2023-12-31"),
                "source_retention_evidence": "source policy",
            },
            "source retention ends before",
        ),
        ({"snapshot_only": 1}, "snapshot_only must be a bool"),
        (
            {
                "snapshot_archive_from": d("2024-01-01"),
                "snapshot_archive_through": d("2024-01-02"),
                "snapshot_archive_evidence": "manifest",
            },
            "require snapshot_only=True",
        ),
        (
            {
                "snapshot_only": True,
                "snapshot_archive_from": d("2024-01-01"),
                "snapshot_archive_evidence": "manifest",
            },
            "requires both from and through",
        ),
        (
            {
                "snapshot_only": True,
                "snapshot_archive_from": d("2024-01-01"),
                "snapshot_archive_through": d("2024-01-02"),
            },
            "snapshot_archive_evidence is required",
        ),
        ({"snapshot_archive_evidence": "manifest"}, "requires archive boundaries"),
    ],
)
def test_invalid_metadata_and_boundary_combinations_are_rejected(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        request((d("2024-01-01"),), (d("2024-01-01"),), **overrides)


def test_non_iterable_and_mapping_calendar_inputs_are_rejected() -> None:
    with pytest.raises(ValueError, match="iterable of datetime.date"):
        request(None, ())
    with pytest.raises(ValueError, match="iterable of datetime.date"):
        request({d("2024-01-01"): "ignored"}, ())
    with pytest.raises(ValueError, match="iterable of datetime.date"):
        request("2024-01-01", ())


def test_subrange_after_known_inception_is_not_reported_as_full_history() -> None:
    expected = (d("2024-01-02"), d("2024-01-03"))
    report = evaluate_history_coverage(
        request(
            expected,
            expected,
            request_start=d("2024-01-02"),
            request_end=d("2024-01-03"),
        )
    )

    assert report.status is CoverageStatus.PARTIAL
    assert report.scope_covers_known_start is False
    assert report.head_gaps == report.interior_gaps == report.tail_gaps == ()
    assert "does not cover the full history" in report.reasons[0]


def test_evaluator_rejects_wrong_request_object() -> None:
    with pytest.raises(TypeError, match="HistoryCoverageRequest"):
        evaluate_history_coverage(object())


def test_frequency_semantics_serializes_as_daily_business_date() -> None:
    report = evaluate_history_coverage(
        request(
            (d("2024-01-01"),),
            (d("2024-01-01"),),
            request_end=d("2024-01-01"),
            frequency_semantics=FrequencySemantics.DAILY_BUSINESS_DATE,
        )
    )

    assert report.frequency_semantics is FrequencySemantics.DAILY_BUSINESS_DATE
    assert report.to_dict()["series"]["frequency_semantics"] == "daily_business_date"
