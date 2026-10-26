"""Golden behavior for evidence-bound full-history coverage queries."""

from datetime import date, datetime

import pytest

from opendata.data.instrument_reference import (
    InstrumentAlias,
    InstrumentLifecycle,
    InstrumentReferenceRegistry,
)
from opendata.pipeline.coverage_service import (
    CalendarEvidence,
    CoverageQuery,
    InstrumentCoverageService,
    PublicationObservations,
)
from opendata.pipeline.history_coverage import CoverageStatus


def d(value: str) -> date:
    """Build literal test dates."""
    return date.fromisoformat(value)


def lifecycle(
    status: str = "active",
    *,
    start_unknown: bool = False,
    start: date | None = None,
    delisted_on: date | None = None,
    status_as_of: date | None = None,
) -> InstrumentLifecycle:
    """Build one reviewed instrument lifecycle fixture."""
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": "XNYS:ABC",
        "lifecycle_version": "life-2024.1",
        "status": status,
        "status_as_of": status_as_of or d("2024-01-10"),
        "status_evidence_ref": "review:lifecycle-status",
        "known_start_date": None if start_unknown else start or d("2024-01-01"),
        "start_boundary_unknown": start_unknown,
        "start_evidence_ref": None if start_unknown else "exchange:listing-notice",
    }
    if status == "delisted":
        values["delisted_on"] = delisted_on or d("2024-01-03")
        values["delist_evidence_ref"] = "exchange:delisting-notice"
    return InstrumentLifecycle(**values)


def registry(
    row: InstrumentLifecycle | None = None, aliases: tuple[InstrumentAlias, ...] = ()
) -> InstrumentReferenceRegistry:
    """Build an injected reviewed registry fixture."""
    return InstrumentReferenceRegistry(
        registry_version="reference-2024.1",
        evidence_ref="review:reference-manifest",
        lifecycles=(row or lifecycle(),),
        aliases=aliases,
    )


def calendar(
    *,
    complete: bool = True,
    covered_from: date | None = None,
    covered_through: date | None = None,
    open_dates: tuple[date, ...] | None = None,
) -> CalendarEvidence:
    """Build a reviewed daily calendar slice."""
    return CalendarEvidence(
        dataset_id="equity_daily",
        source_id="exchange_archive",
        instrument_id="XNYS:ABC",
        calendar_id="XNYS",
        calendar_version="XNYS-2024.1",
        evidence_ref="exchange-calendar:release-2024.1",
        covered_from=covered_from if covered_from is not None else d("2024-01-01"),
        covered_through=covered_through if covered_through is not None else d("2024-01-10"),
        open_dates=(
            open_dates
            if open_dates is not None
            else (d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))
        ),
        daily_classification_complete=complete,
    )


def publication(
    observed: tuple[date, ...] = (d("2024-01-01"), d("2024-01-02"), d("2024-01-03")),
    *,
    scope: str = "business_history",
    scope_start: date | None = None,
    scope_end: date | None = None,
    scope_unbounded: bool = False,
    archive_start: date | None = None,
    archive_end: date | None = None,
    source_retained_from: date | None = None,
    source_retained_before: date | None = None,
    instrument_id: str = "XNYS:ABC",
) -> PublicationObservations:
    """Build an immutable publication projection with explicit evidence scope."""
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": instrument_id,
        "publication_id": "a" * 32,
        "publication_version": "publication-sequence-7",
        "evidence_ref": "warehouse:publication-manifest-7",
        "scope": scope,
        "observed_dates": observed,
        "scope_start": (
            None if scope_unbounded else scope_start if scope_start is not None else d("2024-01-01")
        ),
        "scope_end_exclusive": (
            None if scope_unbounded else scope_end if scope_end is not None else d("2024-01-04")
        ),
        "scope_evidence_ref": None if scope_unbounded else "warehouse:query-scope-7",
    }
    if source_retained_from is not None or source_retained_before is not None:
        values.update(
            source_retained_from=source_retained_from,
            source_retained_before=source_retained_before,
            source_retention_evidence_ref="provider:retention-policy-v2",
        )
    if archive_start is not None or archive_end is not None:
        values.update(
            archive_start=archive_start,
            archive_end_exclusive=archive_end,
            archive_evidence_ref="archive:inventory-v3",
        )
    return PublicationObservations(**values)


def query(
    start: str = "2024-01-01",
    end: str = "2024-01-04",
    *,
    identifier: str = "XNYS:ABC",
    as_of: str | date | None = None,
) -> CoverageQuery:
    """Build the public half-open query fixture."""
    return CoverageQuery(
        dataset_id="equity_daily",
        source_id="exchange_archive",
        instrument_identifier=identifier,
        start=d(start),
        end_exclusive=d(end),
        identifier_as_of=d(as_of) if isinstance(as_of, str) else as_of,
    )


def assess(
    request: CoverageQuery | None = None,
    *,
    lifecycle_row: InstrumentLifecycle | None = None,
    calendar_row: CalendarEvidence | None = None,
    publication_row: PublicationObservations | None = None,
    aliases: tuple[InstrumentAlias, ...] = (),
):
    """Run the public service with explicit immutable evidence inputs."""
    return InstrumentCoverageService(registry(lifecycle_row, aliases)).evaluate(
        request or query(),
        calendar=calendar_row or calendar(),
        publication=publication_row or publication(),
    )


def test_half_open_end_excludes_the_end_date_and_binds_all_evidence_versions() -> None:
    report = assess()

    assert report.status is CoverageStatus.COMPLETE
    assert report.coverage.requested_end == d("2024-01-03")
    assert report.requested_end_exclusive == d("2024-01-04")
    assert report.publication_id == "a" * 32
    assert report.registry_version == "reference-2024.1"
    assert report.calendar_version == "XNYS-2024.1"
    assert report.to_dict()["publication"]["scope_end_exclusive"] == "2024-01-04"


def test_calendar_holiday_is_not_expected_when_complete_daily_slice_omits_it() -> None:
    report = assess(
        calendar_row=calendar(open_dates=(d("2024-01-01"), d("2024-01-03"))),
        publication_row=publication((d("2024-01-01"), d("2024-01-03"))),
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.coverage.expected_dates == (d("2024-01-01"), d("2024-01-03"))


def test_incomplete_calendar_cannot_certify_missing_dates_or_completion() -> None:
    report = assess(calendar_row=calendar(complete=False))

    assert report.status is CoverageStatus.UNKNOWN
    assert report.coverage.business_calendar_covers_request is False
    assert any("complete daily" in reason for reason in report.reasons)


def test_calendar_bounds_must_cover_the_evaluated_half_open_range() -> None:
    report = assess(
        calendar_row=calendar(
            covered_through=d("2024-01-02"),
            open_dates=(d("2024-01-01"), d("2024-01-02")),
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert any("does not cover" in reason for reason in report.reasons)


def test_unknown_listing_boundary_stays_unknown_with_matching_rows() -> None:
    report = assess(lifecycle_row=lifecycle(start_unknown=True))

    assert report.status is CoverageStatus.UNKNOWN
    assert report.coverage.start_boundary_unknown is True
    assert report.coverage.known_start_date is None


def test_range_start_after_known_listing_is_partial_full_history_scope() -> None:
    report = assess(query("2024-01-02", "2024-01-04"))

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.scope_covers_known_start is False


def test_missing_interior_business_date_is_partial() -> None:
    report = assess(publication_row=publication((d("2024-01-01"), d("2024-01-03"))))

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.interior_gaps == (d("2024-01-02"),)


def test_duplicate_daily_observation_is_partial() -> None:
    report = assess(
        publication_row=publication(
            (d("2024-01-01"), d("2024-01-02"), d("2024-01-02"), d("2024-01-03"))
        )
    )

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.duplicate_dates == (d("2024-01-02"),)


def test_weekend_observation_is_reported_as_unexpected() -> None:
    report = assess(
        query("2024-01-05", "2024-01-09"),
        lifecycle_row=lifecycle(start=d("2024-01-05")),
        calendar_row=calendar(
            covered_from=d("2024-01-05"),
            covered_through=d("2024-01-09"),
            open_dates=(d("2024-01-05"), d("2024-01-08"), d("2024-01-09")),
        ),
        publication_row=publication(
            (d("2024-01-05"), d("2024-01-06"), d("2024-01-08"), d("2024-01-09")),
            scope_start=d("2024-01-05"),
            scope_end=d("2024-01-10"),
        ),
    )

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.unexpected_dates == (d("2024-01-06"),)


def test_delisted_life_clips_expected_dates_after_the_reviewed_delist_date() -> None:
    retired = lifecycle(status="delisted", delisted_on=d("2024-01-02"))
    report = assess(
        lifecycle_row=retired,
        publication_row=publication((d("2024-01-01"), d("2024-01-02"))),
    )

    assert report.status is CoverageStatus.COMPLETE
    assert report.evaluated_end_exclusive == d("2024-01-03")
    assert report.coverage.requested_end == d("2024-01-02")
    assert report.delisted_on == d("2024-01-02")


def test_observation_after_delisting_is_not_accepted_as_instrument_history() -> None:
    retired = lifecycle(status="delisted", delisted_on=d("2024-01-02"))
    report = assess(
        lifecycle_row=retired,
        publication_row=publication((d("2024-01-01"), d("2024-01-02"), d("2024-01-03"))),
    )

    assert report.status is CoverageStatus.PARTIAL
    assert report.coverage.unexpected_dates == (d("2024-01-03"),)


def test_interval_before_listing_is_not_applicable_when_no_observation_exists() -> None:
    start = lifecycle(start=d("2024-01-05"))
    report = assess(
        query("2024-01-01", "2024-01-04"),
        lifecycle_row=start,
        publication_row=publication((), scope_end=d("2024-01-04")),
    )

    assert report.status is CoverageStatus.NOT_APPLICABLE
    assert report.evaluated_start == report.requested_end_exclusive
    assert report.evaluated_start == report.evaluated_end_exclusive


def test_interval_after_delisting_is_not_applicable_without_rows_or_calendar_claim() -> None:
    retired = lifecycle(status="delisted", delisted_on=d("2024-01-02"))
    report = assess(
        query("2024-01-04", "2024-01-06"),
        lifecycle_row=retired,
        calendar_row=calendar(complete=False),
        publication_row=publication((), scope_start=d("2024-01-04"), scope_end=d("2024-01-06")),
    )

    assert report.status is CoverageStatus.NOT_APPLICABLE


def test_unknown_lifecycle_status_cannot_be_promoted_to_complete() -> None:
    report = assess(lifecycle_row=lifecycle(status="unknown"))

    assert report.status is CoverageStatus.UNKNOWN
    assert any("lifecycle status is unknown" in reason for reason in report.reasons)


def test_active_status_attestation_that_ends_before_query_is_unknown() -> None:
    report = assess(lifecycle_row=lifecycle(status_as_of=d("2024-01-02")))

    assert report.status is CoverageStatus.UNKNOWN
    assert any("does not reach" in reason for reason in report.reasons)


def test_business_history_requires_publication_scan_scope_evidence() -> None:
    report = assess(
        publication_row=publication(
            (d("2024-01-02"), d("2024-01-03")),
            scope_start=d("2024-01-02"),
            scope_end=d("2024-01-04"),
        )
    )

    assert report.status is CoverageStatus.UNKNOWN
    assert any("lacks evidence" in reason for reason in report.reasons)


def test_business_history_without_any_scan_bounds_is_unknown() -> None:
    report = assess(publication_row=publication(scope_unbounded=True))

    assert report.status is CoverageStatus.UNKNOWN
    assert report.publication_scope_start is None
    assert report.publication_scope_end_exclusive is None


def test_source_retention_limit_is_reported_without_claiming_complete() -> None:
    report = assess(
        publication_row=publication(
            (d("2024-01-02"), d("2024-01-03")),
            source_retained_from=d("2024-01-02"),
        )
    )

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.head_gaps == (d("2024-01-01"),)


def test_current_snapshot_never_certifies_a_business_history_range() -> None:
    report = assess(
        publication_row=publication(
            (d("2024-01-03"),),
            scope="current_snapshot",
            scope_start=d("2024-01-03"),
            scope_end=d("2024-01-04"),
        )
    )

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.snapshot_only is True
    assert report.coverage.unarchived_dates == report.coverage.expected_dates


def test_archived_snapshot_interval_can_support_only_its_evidenced_history() -> None:
    complete_archive = publication(
        scope="archived_snapshot_observations",
        archive_start=d("2024-01-01"),
        archive_end=d("2024-01-04"),
    )
    report = assess(publication_row=complete_archive)

    assert report.status is CoverageStatus.COMPLETE
    assert report.coverage.snapshot_only is True


def test_archived_snapshot_outside_archive_is_source_limited() -> None:
    partial_archive = publication(
        (d("2024-01-02"), d("2024-01-03")),
        scope="archived_snapshot_observations",
        archive_start=d("2024-01-02"),
        archive_end=d("2024-01-04"),
    )
    report = assess(publication_row=partial_archive)

    assert report.status is CoverageStatus.SOURCE_LIMITED
    assert report.coverage.unarchived_dates == (d("2024-01-01"),)


def test_alias_query_resolves_and_echoes_identity_evidence() -> None:
    mapping = InstrumentAlias(
        dataset_id="equity_daily",
        source_id="exchange_archive",
        alias="ABC",
        instrument_id="XNYS:ABC",
        alias_version="ticker-v2",
        evidence_ref="review:ticker-v2",
        valid_from=d("2020-01-01"),
    )
    report = assess(query(identifier="ABC"), aliases=(mapping,))

    assert report.status is CoverageStatus.COMPLETE
    assert report.instrument_id == "XNYS:ABC"
    assert report.alias_version == "ticker-v2"
    assert report.alias_evidence_ref == "review:ticker-v2"


@pytest.mark.parametrize("which", ["calendar", "publication"])
def test_evidence_from_another_instrument_or_dataset_is_rejected(which: str) -> None:
    if which == "calendar":
        evidence = calendar()
        evidence = CalendarEvidence(
            dataset_id="other_dataset",
            source_id=evidence.source_id,
            instrument_id=evidence.instrument_id,
            calendar_id=evidence.calendar_id,
            calendar_version=evidence.calendar_version,
            evidence_ref=evidence.evidence_ref,
            covered_from=evidence.covered_from,
            covered_through=evidence.covered_through,
            open_dates=evidence.open_dates,
            daily_classification_complete=True,
        )
        with pytest.raises(ValueError, match="calendar evidence is bound"):
            assess(calendar_row=evidence)
    else:
        with pytest.raises(ValueError, match="publication evidence is bound"):
            assess(publication_row=publication(instrument_id="XNYS:OTHER"))


@pytest.mark.parametrize(
    "factory, message",
    [
        (lambda: query("2024-01-04", "2024-01-04"), "start < end_exclusive"),
        (
            lambda: query("2024-01-01", "2024-01-04", as_of=datetime(2024, 1, 4, 12, 0)),
            "datetime.date",
        ),
    ],
)
def test_query_rejects_empty_or_coerced_date_ranges(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_service_rejects_untyped_injected_evidence() -> None:
    with pytest.raises(TypeError, match="registry"):
        InstrumentCoverageService(object())  # type: ignore[arg-type]
    service = InstrumentCoverageService(registry())
    with pytest.raises(TypeError, match="query"):
        service.evaluate(object(), calendar=calendar(), publication=publication())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="calendar"):
        service.evaluate(query(), calendar=object(), publication=publication())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="publication"):
        service.evaluate(query(), calendar=calendar(), publication=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"schema_version": True}, "schema_version"),
        ({"covered_from": "2024-01-01"}, "datetime.date"),
        ({"covered_through": d("2023-12-31")}, "covered_from <= covered_through"),
        ({"daily_classification_complete": 1}, "must be a bool"),
        ({"open_dates": (d("2023-12-31"),)}, "inside the declared"),
        ({"open_dates": (d("2024-01-01"), d("2024-01-01"))}, "duplicate dates"),
    ],
)
def test_calendar_evidence_rejects_untrusted_shapes(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": "XNYS:ABC",
        "calendar_id": "XNYS",
        "calendar_version": "calendar-v1",
        "evidence_ref": "review:calendar",
        "covered_from": d("2024-01-01"),
        "covered_through": d("2024-01-10"),
        "open_dates": (d("2024-01-01"),),
        "daily_classification_complete": True,
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        CalendarEvidence(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"schema_version": True}, "schema_version"),
        ({"publication_id": "A" * 32}, "lowercase hexadecimal"),
        ({"scope": "invented"}, "supported publication"),
        ({"scope_start": d("2024-01-01"), "scope_end_exclusive": None}, "both half-open bounds"),
        (
            {
                "scope_start": None,
                "scope_end_exclusive": None,
                "scope_evidence_ref": "orphan",
            },
            "requires a declared publication scope",
        ),
        ({"observed_dates": (d("2023-12-31"),)}, "inside the declared publication scope"),
        (
            {
                "source_retained_from": d("2024-01-04"),
                "source_retained_before": d("2024-01-04"),
                "source_retention_evidence_ref": "e",
            },
            "nonempty half-open interval",
        ),
        ({"source_retained_from": d("2024-01-01")}, "supplied together"),
        (
            {
                "scope": "business_history",
                "archive_start": d("2024-01-01"),
                "archive_end_exclusive": d("2024-01-04"),
                "archive_evidence_ref": "archive",
            },
            "business_history",
        ),
        (
            {"scope": "current_snapshot", "observed_dates": (d("2024-01-01"), d("2024-01-02"))},
            "at most one",
        ),
    ],
)
def test_publication_evidence_rejects_untrusted_shapes(
    changes: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "dataset_id": "equity_daily",
        "source_id": "exchange_archive",
        "instrument_id": "XNYS:ABC",
        "publication_id": "a" * 32,
        "publication_version": "v1",
        "evidence_ref": "review:publication",
        "scope": "business_history",
        "observed_dates": (d("2024-01-01"),),
        "scope_start": d("2024-01-01"),
        "scope_end_exclusive": d("2024-01-04"),
        "scope_evidence_ref": "review:scope",
    }
    values.update(changes)
    with pytest.raises(ValueError, match=message):
        PublicationObservations(**values)  # type: ignore[arg-type]
