"""Synthetic contract tests for conservative financial period presence assessment.

No test reads source evidence, contacts a provider, uses a database, or proves
that caller-declared references and scan facts are authentic.
"""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from opendata.pipeline.financial_period_coverage import (
    FinancialPeriodCoverageInputError,
    FinancialPeriodCoverageItem,
    FinancialPeriodCoverageSlotEvidence,
    FinancialPeriodCoverageState,
    FinancialPeriodCrosswalkEvidence,
    FinancialPeriodDueEvidence,
    FinancialPeriodExpectedObligationEvidence,
    FinancialPeriodLifecycleEvidence,
    FinancialPeriodObservationVersionEvidence,
    FinancialPeriodScanEvidence,
    assess_financial_period_coverage,
)

AS_OF = datetime(2026, 10, 28, 4, 0, tzinfo=timezone.utc)
REPORT_PERIOD = date(2025, 12, 31)
_HASH = hashlib.sha256(b"version-a").hexdigest()


def _crosswalk(
    *,
    source_id: str = "source-a",
    source_instrument_id: str = "provider-code-a",
    security_id: str = "security-a",
    valid_from: date = date(1990, 1, 1),
    valid_through: date | None = None,
    evidenced_at: datetime = AS_OF - timedelta(days=1),
) -> FinancialPeriodCrosswalkEvidence:
    return FinancialPeriodCrosswalkEvidence(
        source_id=source_id,
        source_instrument_id=source_instrument_id,
        security_id=security_id,
        valid_from=valid_from,
        valid_through=valid_through,
        evidence_ref="crosswalk-evidence-a",
        evidenced_at_utc=evidenced_at,
    )


def _lifecycle(
    *, covers: bool | None = True, evidenced_at: datetime = AS_OF - timedelta(days=1)
) -> FinancialPeriodLifecycleEvidence:
    return FinancialPeriodLifecycleEvidence(
        covers_report_period=covers,
        evidence_ref="lifecycle-evidence-a",
        evidenced_at_utc=evidenced_at,
    )


def _due(*, due_at: datetime | None = AS_OF - timedelta(days=3)) -> FinancialPeriodDueEvidence:
    return FinancialPeriodDueEvidence(
        due_at_utc=due_at,
        evidence_ref="due-evidence-a" if due_at is not None else None,
        evidenced_at_utc=AS_OF - timedelta(days=2) if due_at is not None else None,
        obligation_evidence_ref=("obligation-evidence-a" if due_at is not None else None),
    )


def _obligation(
    *,
    source_id: str = "source-a",
    source_instrument_id: str = "provider-code-a",
    security_id: str = "security-a",
    statement_type: str = "income",
    periodicity: str = "annual",
    report_period: date = REPORT_PERIOD,
    applies: bool | None = True,
    evidenced_at: datetime = AS_OF - timedelta(days=2),
) -> FinancialPeriodExpectedObligationEvidence:
    return FinancialPeriodExpectedObligationEvidence(
        source_id=source_id,
        source_instrument_id=source_instrument_id,
        security_id=security_id,
        statement_type=statement_type,
        periodicity=periodicity,
        report_period=report_period,
        applies=applies,
        evidence_ref="obligation-evidence-a",
        evidenced_at_utc=evidenced_at,
    )


def _scan(
    *,
    snapshot_at: datetime = AS_OF - timedelta(hours=2),
    evidenced_at: datetime | None = AS_OF - timedelta(hours=1),
    scope_matches_slot: bool | None = True,
    crosswalk_matches_snapshot: bool | None = True,
    cursor_exhausted: bool | None = True,
    failed_page_count: int | None = 0,
    unresolved_row_count: int | None = 0,
    isolated_row_count: int | None = 0,
    ambiguous_crosswalk_count: int | None = 0,
    source_history_covers_period: bool | None = True,
    source_history_evidence_ref: str | None = "history-evidence-a",
    source_history_evidenced_at_utc: datetime | None = AS_OF - timedelta(days=2),
    matched_version_ids: tuple[str, ...] | None = (),
) -> FinancialPeriodScanEvidence:
    return FinancialPeriodScanEvidence(
        snapshot_at_utc=snapshot_at,
        evidence_ref="scan-evidence-a",
        evidenced_at_utc=evidenced_at,
        scope_matches_slot=scope_matches_slot,
        crosswalk_matches_snapshot=crosswalk_matches_snapshot,
        cursor_exhausted=cursor_exhausted,
        failed_page_count=failed_page_count,
        unresolved_row_count=unresolved_row_count,
        isolated_row_count=isolated_row_count,
        ambiguous_crosswalk_count=ambiguous_crosswalk_count,
        source_history_covers_period=source_history_covers_period,
        source_history_evidence_ref=source_history_evidence_ref,
        source_history_evidenced_at_utc=source_history_evidenced_at_utc,
        matched_version_ids=matched_version_ids,
    )


def _version(
    *,
    version_id: str = "version-a",
    source_id: str = "source-a",
    source_instrument_id: str = "provider-code-a",
    security_id: str = "security-a",
    statement_type: str = "income",
    periodicity: str = "annual",
    report_period: date = REPORT_PERIOD,
    observed_at: datetime = AS_OF - timedelta(days=3),
    evidenced_at: datetime = AS_OF - timedelta(days=3),
    isolation_status: str = "clear",
    isolation_evidenced_at: datetime | None = AS_OF - timedelta(days=2),
) -> FinancialPeriodObservationVersionEvidence:
    return FinancialPeriodObservationVersionEvidence(
        version_id=version_id,
        source_id=source_id,
        source_instrument_id=source_instrument_id,
        security_id=security_id,
        statement_type=statement_type,
        periodicity=periodicity,
        report_period=report_period,
        observed_at_utc=observed_at,
        evidence_ref="observation-evidence-a",
        evidenced_at_utc=evidenced_at,
        isolation_status=isolation_status,
        isolation_evidence_ref=(
            "isolation-evidence-a" if isolation_evidenced_at is not None else None
        ),
        isolation_evidenced_at_utc=isolation_evidenced_at,
    )


def _slot(
    *,
    source_id: str = "source-a",
    source_instrument_id: str = "provider-code-a",
    security_id: str = "security-a",
    statement_type: str = "income",
    periodicity: str = "annual",
    report_period: date = REPORT_PERIOD,
    crosswalk: FinancialPeriodCrosswalkEvidence | None = None,
    lifecycle: FinancialPeriodLifecycleEvidence | None = None,
    obligation: FinancialPeriodExpectedObligationEvidence | None = None,
    due: FinancialPeriodDueEvidence | None = None,
    scan: FinancialPeriodScanEvidence | None = None,
    versions: tuple[FinancialPeriodObservationVersionEvidence, ...] = (),
) -> FinancialPeriodCoverageSlotEvidence:
    return FinancialPeriodCoverageSlotEvidence(
        source_id=source_id,
        source_instrument_id=source_instrument_id,
        security_id=security_id,
        statement_type=statement_type,
        periodicity=periodicity,
        report_period=report_period,
        crosswalk=crosswalk
        or _crosswalk(
            source_id=source_id,
            source_instrument_id=source_instrument_id,
            security_id=security_id,
        ),
        lifecycle=lifecycle or _lifecycle(),
        obligation=obligation
        or _obligation(
            source_id=source_id,
            source_instrument_id=source_instrument_id,
            security_id=security_id,
            statement_type=statement_type,
            periodicity=periodicity,
            report_period=report_period,
        ),
        due=due,
        scan=scan,
        versions=versions,
    )


def _item(slot: FinancialPeriodCoverageSlotEvidence) -> FinancialPeriodCoverageItem:
    report = assess_financial_period_coverage([slot], as_of_utc=AS_OF)
    assert len(report.items) == 1
    return report.items[0]


def test_observed_means_one_exact_version_and_needs_no_full_scan() -> None:
    slot = _slot(versions=(_version(),))

    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.OBSERVED
    assert "accepted_observation_version_present" in item.basis_codes
    assert len(item.version_refs) == 1
    assert item.version_refs[0] == _HASH


def test_observed_does_not_depend_on_security_listing_lifecycle_evidence() -> None:
    slot = replace(_slot(versions=(_version(),)), lifecycle=None)

    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.OBSERVED
    assert "lifecycle_evidence_missing" not in item.basis_codes


def test_observed_result_does_not_claim_item_coverage_or_point_in_time_history() -> None:
    report = assess_financial_period_coverage([_slot(versions=(_version(),))], as_of_utc=AS_OF)

    assert report.evidence_references_independently_verified is False
    assert report.point_in_time_guarantee is False
    assert not hasattr(report, "complete")
    assert not hasattr(report, "statement_complete")


def test_explicit_complete_scan_can_confirm_only_the_requested_absence() -> None:
    slot = _slot(due=_due(), scan=_scan())

    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.MISSING_CONFIRMED
    assert "explicit_due_time_elapsed" in item.basis_codes
    assert "exact_scan_scope_asserted" in item.basis_codes
    assert "source_history_boundary_covers_period" in item.basis_codes
    assert item.version_refs == ()


@pytest.mark.parametrize(  # type: ignore[misc]
    ("due", "scan", "lifecycle", "crosswalk", "expected_basis"),
    [
        (_due(due_at=None), _scan(), _lifecycle(), None, "explicit_due_evidence_missing"),
        (
            _due(due_at=AS_OF + timedelta(hours=1)),
            _scan(),
            _lifecycle(),
            None,
            "explicit_due_time_not_arrived",
        ),
        (
            _due(),
            _scan(scope_matches_slot=False),
            _lifecycle(),
            None,
            "scan_scope_not_confirmed_for_exact_slot",
        ),
        (_due(), _scan(cursor_exhausted=False), _lifecycle(), None, "scan_cursor_not_exhausted"),
        (
            _due(),
            _scan(failed_page_count=1),
            _lifecycle(),
            None,
            "scan_has_failures_or_unknown_failure_count",
        ),
        (
            _due(),
            _scan(unresolved_row_count=1),
            _lifecycle(),
            None,
            "scan_has_unresolved_rows_or_unknown_count",
        ),
        (
            _due(),
            _scan(isolated_row_count=1),
            _lifecycle(),
            None,
            "scan_has_isolated_rows_or_unknown_count",
        ),
        (
            _due(),
            _scan(source_history_covers_period=None, source_history_evidence_ref=None),
            _lifecycle(),
            None,
            "source_history_boundary_unknown_or_unverified",
        ),
        (
            _due(),
            _scan(),
            _lifecycle(covers=None),
            None,
            "lifecycle_does_not_confirm_report_period",
        ),
        (
            _due(),
            _scan(),
            _lifecycle(),
            _crosswalk(valid_from=date(2026, 1, 1)),
            "identity_crosswalk_outside_report_period",
        ),
    ],
)
def test_missing_requires_due_identity_lifecycle_and_clean_exact_scan(
    due: FinancialPeriodDueEvidence,
    scan: FinancialPeriodScanEvidence,
    lifecycle: FinancialPeriodLifecycleEvidence,
    crosswalk: FinancialPeriodCrosswalkEvidence | None,
    expected_basis: str,
) -> None:
    slot = _slot(due=due, scan=scan, lifecycle=lifecycle, crosswalk=crosswalk)

    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert expected_basis in item.basis_codes


def test_unknown_source_history_boundary_cannot_confirm_retained_history_absence() -> None:
    item = _item(
        _slot(
            due=_due(),
            scan=_scan(
                source_history_covers_period=False,
                source_history_evidence_ref="history-limited",
            ),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "source_history_boundary_unknown_or_unverified" in item.basis_codes


def test_due_scan_must_be_taken_at_or_after_explicit_deadline() -> None:
    deadline = AS_OF - timedelta(days=1)
    item = _item(
        _slot(
            due=_due(due_at=deadline),
            scan=_scan(snapshot_at=deadline - timedelta(seconds=1)),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "scan_predates_explicit_due_time" in item.basis_codes


def test_scan_match_cannot_be_omitted_from_slot_versions() -> None:
    item = _item(
        _slot(
            due=_due(),
            scan=_scan(matched_version_ids=("version-found-by-scan",)),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "scan_result_inventory_mismatch_or_unknown" in item.basis_codes


def test_missing_requires_an_explicit_empty_scan_inventory() -> None:
    item = _item(_slot(due=_due(), scan=_scan(matched_version_ids=None)))

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "scan_result_inventory_unknown" in item.basis_codes


def test_scan_evidence_must_not_predate_its_snapshot() -> None:
    item = _item(
        _slot(
            due=_due(),
            scan=_scan(
                snapshot_at=AS_OF - timedelta(hours=2),
                evidenced_at=AS_OF - timedelta(hours=3),
            ),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "scan_evidence_predates_snapshot" in item.basis_codes


@pytest.mark.parametrize(  # type: ignore[misc]
    ("obligation", "expected_basis"),
    [
        (None, "expected_obligation_evidence_missing"),
        (_obligation(applies=False), "expected_obligation_not_confirmed_for_exact_slot"),
        (
            _obligation(evidenced_at=AS_OF + timedelta(seconds=1)),
            "future_or_undated_expected_obligation_evidence",
        ),
        (
            _obligation(security_id="another-security"),
            "expected_obligation_scope_mismatch",
        ),
    ],
)
def test_missing_requires_as_of_valid_exact_expected_obligation(
    obligation: FinancialPeriodExpectedObligationEvidence | None, expected_basis: str
) -> None:
    slot = replace(_slot(due=_due(), scan=_scan()), obligation=obligation)
    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert expected_basis in item.basis_codes


def test_due_evidence_must_reference_the_exact_expected_obligation() -> None:
    item = _item(
        _slot(
            due=replace(_due(), obligation_evidence_ref="different-obligation"),
            scan=_scan(),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "explicit_due_evidence_missing" in item.basis_codes


def test_isolated_observation_is_retained_as_reference_but_never_counts_as_observed() -> None:
    item = _item(
        _slot(
            due=_due(),
            scan=_scan(isolated_row_count=1),
            versions=(_version(isolation_status="isolated"),),
        )
    )

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert item.version_refs == (_HASH,)
    assert "observation_version_isolated_or_isolation_unknown" in item.basis_codes


def test_future_observation_or_future_isolation_evidence_cannot_enter_as_of_result() -> None:
    future_observation = _version(
        observed_at=AS_OF + timedelta(seconds=1),
        evidenced_at=AS_OF + timedelta(seconds=1),
    )
    future_isolation_review = _version(isolation_evidenced_at=AS_OF + timedelta(seconds=1))

    for version in (future_observation, future_isolation_review):
        item = _item(_slot(due=_due(), scan=_scan(), versions=(version,)))
        assert item.state is FinancialPeriodCoverageState.UNKNOWN
        assert "future_observation_or_evidence" in item.basis_codes
        if version is future_observation:
            assert item.version_refs == ()
        else:
            assert item.version_refs == (_HASH,)


def test_future_crosswalk_blocks_observation_but_future_lifecycle_does_not() -> None:
    future = AS_OF + timedelta(seconds=1)
    future_crosswalk = _item(
        _slot(crosswalk=_crosswalk(evidenced_at=future), versions=(_version(),))
    )
    future_lifecycle = _item(
        _slot(lifecycle=_lifecycle(evidenced_at=future), versions=(_version(),))
    )

    assert future_crosswalk.state is FinancialPeriodCoverageState.UNKNOWN
    assert "future_or_undated_crosswalk_evidence" in future_crosswalk.basis_codes
    assert future_lifecycle.state is FinancialPeriodCoverageState.OBSERVED
    assert "future_or_undated_lifecycle_evidence" not in future_lifecycle.basis_codes


def test_future_evidence_cannot_confirm_missing() -> None:
    future = AS_OF + timedelta(seconds=1)
    cases = (
        (
            _slot(crosswalk=_crosswalk(evidenced_at=future), due=_due(), scan=_scan()),
            "future_or_undated_crosswalk_evidence",
        ),
        (
            _slot(lifecycle=_lifecycle(evidenced_at=future), due=_due(), scan=_scan()),
            "future_or_undated_lifecycle_evidence",
        ),
        (
            _slot(obligation=_obligation(evidenced_at=future), due=_due(), scan=_scan()),
            "future_or_undated_expected_obligation_evidence",
        ),
        (
            _slot(due=replace(_due(), evidenced_at_utc=future), scan=_scan()),
            "future_or_undated_due_evidence",
        ),
        (
            _slot(due=_due(), scan=_scan(evidenced_at=future)),
            "future_or_undated_scan_evidence",
        ),
        (
            _slot(due=_due(), scan=_scan(source_history_evidenced_at_utc=future)),
            "source_history_boundary_unknown_or_unverified",
        ),
    )

    for slot, expected_basis in cases:
        item = _item(slot)

        assert item.state is FinancialPeriodCoverageState.UNKNOWN
        assert expected_basis in item.basis_codes


def test_annual_and_quarterly_slots_are_not_substituted_for_each_other() -> None:
    annual = _slot(
        periodicity="annual",
        versions=(_version(version_id="annual-version", periodicity="annual"),),
    )
    quarterly = _slot(
        periodicity="quarterly",
        versions=(_version(version_id="quarterly-version", periodicity="quarterly"),),
    )

    report = assess_financial_period_coverage([annual, quarterly], as_of_utc=AS_OF)

    assert len(report.items) == 2
    assert {item.state for item in report.items} == {FinancialPeriodCoverageState.OBSERVED}
    assert len({item.slot_ref for item in report.items}) == 2


def test_multiple_versions_are_retained_and_exact_duplicate_is_idempotent() -> None:
    first = _version(version_id="version-first")
    second = _version(version_id="version-second", observed_at=AS_OF - timedelta(days=1))
    slot = _slot(versions=(first, second, first))

    item = _item(slot)

    assert item.state is FinancialPeriodCoverageState.OBSERVED
    assert len(item.version_refs) == 2
    assert all(len(reference) == 64 for reference in item.version_refs)


def test_conflicting_duplicate_version_id_blocks_all_affected_period_slots() -> None:
    annual = _slot(versions=(_version(version_id="reused-id", periodicity="annual"),))
    quarterly = _slot(
        periodicity="quarterly",
        versions=(_version(version_id="reused-id", periodicity="quarterly"),),
    )

    report = assess_financial_period_coverage([annual, quarterly], as_of_utc=AS_OF)

    assert len(report.items) == 2
    assert all(item.state is FinancialPeriodCoverageState.UNKNOWN for item in report.items)
    assert all("duplicate_version_conflict" in item.basis_codes for item in report.items)


def test_conflicting_duplicate_slot_claim_blocks_observed_and_missing() -> None:
    observed = _slot(versions=(_version(),))
    changed_scan = replace(observed, scan=_scan())

    item = assess_financial_period_coverage([observed, changed_scan], as_of_utc=AS_OF).items[0]

    assert item.state is FinancialPeriodCoverageState.UNKNOWN
    assert "duplicate_slot_conflict" in item.basis_codes


def test_overlapping_source_identity_crosswalks_to_two_securities_fail_closed() -> None:
    left = _slot(
        security_id="security-left",
        versions=(_version(version_id="left-version", security_id="security-left"),),
    )
    right = _slot(
        security_id="security-right",
        versions=(_version(version_id="right-version", security_id="security-right"),),
    )

    report = assess_financial_period_coverage([left, right], as_of_utc=AS_OF)

    assert len(report.items) == 2
    assert all(item.state is FinancialPeriodCoverageState.UNKNOWN for item in report.items)
    assert all("crosswalk_conflict" in item.basis_codes for item in report.items)


def test_disjoint_crosswalk_validity_ranges_do_not_conflict() -> None:
    earlier_period = date(2023, 12, 31)
    later_period = date(2025, 12, 31)
    earlier = _slot(
        security_id="security-left",
        report_period=earlier_period,
        crosswalk=_crosswalk(
            security_id="security-left",
            valid_from=date(2020, 1, 1),
            valid_through=date(2024, 12, 31),
        ),
        versions=(
            _version(
                version_id="earlier-version",
                security_id="security-left",
                report_period=earlier_period,
            ),
        ),
    )
    later = _slot(
        security_id="security-right",
        report_period=later_period,
        crosswalk=_crosswalk(
            security_id="security-right",
            valid_from=date(2025, 1, 1),
        ),
        versions=(
            _version(
                version_id="later-version",
                security_id="security-right",
                report_period=later_period,
            ),
        ),
    )

    report = assess_financial_period_coverage([earlier, later], as_of_utc=AS_OF)

    assert {item.state for item in report.items} == {FinancialPeriodCoverageState.OBSERVED}
    assert all("crosswalk_conflict" not in item.basis_codes for item in report.items)


def test_result_never_echoes_source_ids_security_ids_version_ids_or_evidence_refs() -> None:
    slot = _slot(
        source_id="source-private-marker",
        source_instrument_id="alias-private-marker",
        security_id="security-private-marker",
        crosswalk=_crosswalk(
            source_id="source-private-marker",
            source_instrument_id="alias-private-marker",
            security_id="security-private-marker",
        ),
        lifecycle=replace(_lifecycle(), evidence_ref="life-private-marker"),
        versions=(
            _version(
                version_id="version-private-marker",
                source_id="source-private-marker",
                source_instrument_id="alias-private-marker",
                security_id="security-private-marker",
            ),
        ),
    )

    rendered = repr(assess_financial_period_coverage([slot], as_of_utc=AS_OF))

    for marker in (
        "source-private-marker",
        "alias-private-marker",
        "security-private-marker",
        "version-private-marker",
        "life-private-marker",
    ):
        assert marker not in rendered


def test_invalid_cadence_and_naive_as_of_fail_without_echoing_values() -> None:
    with pytest.raises(FinancialPeriodCoverageInputError, match="periodicity is unsupported"):
        assess_financial_period_coverage([_slot(periodicity="monthly")], as_of_utc=AS_OF)

    with pytest.raises(FinancialPeriodCoverageInputError, match="as_of_utc must be timezone-aware"):
        assess_financial_period_coverage([_slot()], as_of_utc=datetime(2026, 10, 28))


def test_sensitive_input_is_not_exposed_in_exception_tracebacks() -> None:
    marker = "sensitive-enum-value"
    with pytest.raises(FinancialPeriodCoverageInputError) as error:
        assess_financial_period_coverage([_slot(statement_type=marker)], as_of_utc=AS_OF)

    assert marker not in str(error.value)
    assert marker not in "".join(traceback.format_exception(error.value))


def test_slot_generator_exception_is_sanitized() -> None:
    def broken_slots() -> Iterator[FinancialPeriodCoverageSlotEvidence]:
        yield _slot()
        raise RuntimeError("sensitive-provider-response")

    with pytest.raises(FinancialPeriodCoverageInputError, match="slots iteration failed") as error:
        assess_financial_period_coverage(broken_slots(), as_of_utc=AS_OF)

    assert "sensitive-provider-response" not in str(error.value)
    assert "sensitive-provider-response" not in "".join(traceback.format_exception(error.value))
