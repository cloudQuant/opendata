"""Synthetic behavior tests for conservative Sina LRB financial adaptation."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal

import pytest

from opendata.data.providers.sina.sina_lrb_financial_mapping import (
    SinaLrbFinancialFieldDeclaration,
    SinaLrbFinancialMappingError,
    SinaLrbPeriodObservation,
    SinaLrbReportPeriodMapping,
    assess_sina_lrb_financial_fields,
    assess_sina_lrb_period_coverage,
)
from opendata.pipeline.financial_period_coverage import (
    FinancialPeriodCoverageState,
    FinancialPeriodCrosswalkEvidence,
    Periodicity,
    StatementType,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_AS_OF = datetime(2025, 5, 1, tzinfo=timezone.utc)
_CAPTURED_AT = datetime(2025, 4, 30, 8, 0, tzinfo=timezone.utc)


def _field_declaration(
    *,
    source_item_key: str = "ITEM_A",
    source_item_title: str = "Revenue",
    canonical_item_code: str = "revenue",
    native_unit: str = "source-native-unit",
    currency: str | None = "CNY",
    evidenced_at_utc: datetime = _CAPTURED_AT,
) -> SinaLrbFinancialFieldDeclaration:
    """Return one synthetic operator assertion, not a Sina source fact."""
    return SinaLrbFinancialFieldDeclaration(
        source_item_key=source_item_key,
        source_item_title=source_item_title,
        canonical_item_code=canonical_item_code,
        native_unit=native_unit,
        currency=currency,
        evidence_ref="mapping:synthetic:1",
        evidenced_at_utc=evidenced_at_utc,
    )


def _crosswalk() -> FinancialPeriodCrosswalkEvidence:
    """Return a synthetic, exact as-of source identity assertion."""
    return FinancialPeriodCrosswalkEvidence(
        source_id="sina-lrb",
        source_instrument_id="600519.SH",
        security_id="security:600519",
        valid_from=date(1990, 1, 1),
        valid_through=None,
        evidence_ref="crosswalk:synthetic:1",
        evidenced_at_utc=_CAPTURED_AT,
    )


def _period_mapping() -> SinaLrbReportPeriodMapping:
    """Return one synthetic explicit report-period assertion."""
    return SinaLrbReportPeriodMapping(
        source_key="20241231",
        report_period=date(2024, 12, 31),
        statement_type=StatementType.INCOME,
        periodicity=Periodicity.ANNUAL,
        evidence_ref="period:synthetic:1",
        evidenced_at_utc=_CAPTURED_AT,
    )


def _observation(
    *,
    mapping: SinaLrbReportPeriodMapping | None = None,
    crosswalk: FinancialPeriodCrosswalkEvidence | None = None,
    isolation_status: Literal["clear", "isolated", "unknown"] = "clear",
    isolation_evidence_ref: str | None = "isolation:synthetic:1",
    isolation_evidenced_at_utc: datetime | None = _CAPTURED_AT,
    source_report_key: str = "20241231",
) -> SinaLrbPeriodObservation:
    """Return one synthetic capture with caller supplied evidence assertions."""
    return SinaLrbPeriodObservation(
        capture_id="a" * 64,
        source_instrument_id="600519.SH",
        source_report_key=source_report_key,
        retrieved_at_utc=_CAPTURED_AT,
        capture_evidence_ref="capture:synthetic:1",
        report_period_mapping=_period_mapping() if mapping is None else mapping,
        crosswalk=_crosswalk() if crosswalk is None else crosswalk,
        isolation_status=isolation_status,
        isolation_evidence_ref=isolation_evidence_ref,
        isolation_evidenced_at_utc=isolation_evidenced_at_utc,
    )


def test_unmapped_sina_fields_remain_unknown_without_a_builtin_crosswalk() -> None:
    result = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"), ("ITEM_B", "营业收入")),
        (),
        as_of_utc=_AS_OF,
    )

    assert [item.state for item in result] == ["unknown", "unknown"]
    assert [item.reason for item in result] == [
        "exact_pair_assertion_missing",
        "exact_pair_assertion_missing",
    ]
    assert all(item.canonical_item_code is None for item in result)
    assert all(item.native_unit is None and item.currency is None for item in result)
    assert all(item.mapping_evidence_independently_verified is False for item in result)


def test_exact_field_assertion_is_as_of_and_keeps_native_unit_unconverted() -> None:
    declaration = _field_declaration()
    result = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"), ("ITEM_A", "营业收入")),
        (declaration,),
        as_of_utc=_AS_OF,
    )

    assert result[0].state == "operator_assertion_unverified"
    assert result[0].canonical_item_code == "revenue"
    assert result[0].native_unit == "source-native-unit"
    assert result[0].currency == "CNY"
    assert result[0].value_conversion_performed is False
    assert result[1].state == "unknown"
    assert result[1].canonical_item_code is None


def test_future_mapping_evidence_and_conflicting_pairs_fail_closed() -> None:
    future = _field_declaration(evidenced_at_utc=_AS_OF.replace(year=2026))
    future_conflict = replace(future, native_unit="another-unit")
    conflict = _field_declaration(native_unit="another-unit")

    future_result = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"),), (future, future_conflict), as_of_utc=_AS_OF
    )
    conflict_result = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"),), (_field_declaration(), conflict), as_of_utc=_AS_OF
    )

    assert future_result[0].state == "unknown"
    assert future_result[0].reason == "mapping_evidence_future_or_undated"
    assert conflict_result[0].state == "conflict"
    assert conflict_result[0].canonical_item_code is None
    assert conflict_result[0].native_unit is None


def test_future_conflicting_mapping_does_not_change_historical_as_of_result() -> None:
    historical = _field_declaration()
    future_conflict = replace(
        _field_declaration(native_unit="revised-unit"),
        evidenced_at_utc=_AS_OF.replace(year=2026),
    )

    at_historical_as_of = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"),),
        (historical, future_conflict),
        as_of_utc=_AS_OF,
    )[0]
    after_both_assertions = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"),),
        (historical, future_conflict),
        as_of_utc=_AS_OF.replace(year=2027),
    )[0]

    assert at_historical_as_of.state == "operator_assertion_unverified"
    assert at_historical_as_of.canonical_item_code == "revenue"
    assert at_historical_as_of.native_unit == historical.native_unit
    assert at_historical_as_of.mapping_evidence_independently_verified is False
    assert after_both_assertions.state == "conflict"
    assert after_both_assertions.canonical_item_code is None


def test_field_mapping_without_evidence_reference_remains_unknown() -> None:
    declaration = replace(_field_declaration(), evidence_ref=None, evidenced_at_utc=None)

    result = assess_sina_lrb_financial_fields(
        (("ITEM_A", "Revenue"),), (declaration,), as_of_utc=_AS_OF
    )

    assert result[0].state == "unknown"
    assert result[0].reason == "mapping_evidence_unavailable"
    assert result[0].canonical_item_code is None
    assert result[0].native_unit is None


def test_malformed_field_assertions_fail_with_value_free_errors() -> None:
    invalid_declaration = replace(_field_declaration(), native_unit="")

    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_field_mapping_input"):
        assess_sina_lrb_financial_fields(
            (("ITEM_A", "Revenue"),), (invalid_declaration,), as_of_utc=_AS_OF
        )
    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_field_mapping_input"):
        assess_sina_lrb_financial_fields((("ITEM_A", ""),), (), as_of_utc=_AS_OF)

    invalid_currency = replace(_field_declaration(), currency="CN")
    invalid_item_code = replace(_field_declaration(), canonical_item_code="Revenue")
    for declaration in (invalid_currency, invalid_item_code):
        with pytest.raises(SinaLrbFinancialMappingError, match="invalid_field_mapping_input"):
            assess_sina_lrb_financial_fields(
                (("ITEM_A", "Revenue"),), (declaration,), as_of_utc=_AS_OF
            )


def test_unavailable_or_path_shaped_field_evidence_never_maps() -> None:
    for evidence_ref, evidenced_at in ((None, _CAPTURED_AT), ("mapping:missing-time", None)):
        declaration = replace(
            _field_declaration(),
            evidence_ref=evidence_ref,
            evidenced_at_utc=evidenced_at,
        )
        assessment = assess_sina_lrb_financial_fields(
            (("ITEM_A", "Revenue"),), (declaration,), as_of_utc=_AS_OF
        )[0]
        assert assessment.state == "unknown"
        assert assessment.canonical_item_code is None

    path_shaped_reference = replace(_field_declaration(), evidence_ref="docs/private/source.html")
    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_field_mapping_input"):
        assess_sina_lrb_financial_fields(
            (("ITEM_A", "Revenue"),), (path_shaped_reference,), as_of_utc=_AS_OF
        )


def test_field_and_declaration_iterables_are_bounded() -> None:
    too_many_fields = ((f"ITEM_{index}", "Title") for index in range(4_097))

    with pytest.raises(SinaLrbFinancialMappingError, match="limit_exceeded"):
        assess_sina_lrb_financial_fields(too_many_fields, (), as_of_utc=_AS_OF)

    def failed_source() -> Iterator[tuple[str, str]]:
        yield ("ITEM_A", "Revenue")
        raise RuntimeError("private source failure")

    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_field_mapping_input"):
        assess_sina_lrb_financial_fields(failed_source(), (), as_of_utc=_AS_OF)


def test_missing_period_semantics_stay_unknown_without_inventing_a_date() -> None:
    observation = replace(_observation(), report_period_mapping=None)

    result = assess_sina_lrb_period_coverage((observation,), as_of_utc=_AS_OF)

    assert result.coverage.items == ()
    assert result.unassessable_observation_count == 1
    assert result.unassessable_reason_counts == (("period_semantics_missing", 1),)
    assert result.report_completeness_established is False
    assert result.announcement_time_independently_verified is False
    assert result.revision_history_established is False
    assert result.point_in_time_guarantee is False


def test_exact_mapped_capture_is_observed_without_completeness_or_pit_claims() -> None:
    result = assess_sina_lrb_period_coverage((_observation(),), as_of_utc=_AS_OF)

    assert result.unassessable_observation_count == 0
    assert len(result.coverage.items) == 1
    assert result.coverage.items[0].state is FinancialPeriodCoverageState.OBSERVED
    assert "accepted_observation_version_present" in result.coverage.items[0].basis_codes
    assert result.coverage.evidence_references_independently_verified is False
    assert result.coverage.point_in_time_guarantee is False
    assert not hasattr(result.coverage, "complete")
    assert result.report_completeness_established is False
    assert result.announcement_time_independently_verified is False
    assert result.revision_history_established is False


def test_key_date_disagreement_and_missing_crosswalk_remain_unknown() -> None:
    disagreement = _observation(source_report_key="20240930")
    missing_crosswalk = replace(_observation(), crosswalk=None)

    result = assess_sina_lrb_period_coverage((disagreement, missing_crosswalk), as_of_utc=_AS_OF)

    assert result.coverage.items == ()
    assert result.unassessable_observation_count == 2
    assert result.unassessable_reason_counts == (
        ("identity_crosswalk_missing", 1),
        ("period_key_does_not_match_asserted_period", 1),
    )


def test_period_mapping_requires_supported_family_and_as_of_evidence() -> None:
    future = replace(
        _period_mapping(),
        evidenced_at_utc=_AS_OF.replace(year=2026),
    )
    without_reference = replace(
        _period_mapping(),
        evidence_ref=None,
        evidenced_at_utc=None,
    )
    unsupported = replace(
        _period_mapping(),
        statement_type="profit_and_loss",
    )

    result = assess_sina_lrb_period_coverage(
        (
            _observation(mapping=future),
            _observation(mapping=without_reference),
            _observation(mapping=unsupported),
        ),
        as_of_utc=_AS_OF,
    )

    assert result.coverage.items == ()
    assert result.unassessable_reason_counts == (
        ("period_semantics_evidence_unavailable", 2),
        ("period_semantics_unsupported", 1),
    )


def test_future_conflicting_period_mapping_is_unknown_for_historical_as_of() -> None:
    future_conflict = replace(
        _period_mapping(),
        statement_type=StatementType.BALANCE,
        periodicity=Periodicity.QUARTERLY,
        evidenced_at_utc=_AS_OF.replace(year=2026),
    )

    result = assess_sina_lrb_period_coverage(
        (_observation(), _observation(mapping=future_conflict)), as_of_utc=_AS_OF
    )

    assert len(result.coverage.items) == 1
    assert result.coverage.items[0].state is FinancialPeriodCoverageState.OBSERVED
    assert result.unassessable_observation_count == 1
    assert result.unassessable_reason_counts == (("period_semantics_evidence_unavailable", 1),)
    assert result.report_completeness_established is False
    assert result.announcement_time_independently_verified is False
    assert result.revision_history_established is False
    assert result.point_in_time_guarantee is False


def test_report_period_as_of_boundary_uses_shanghai_calendar_date() -> None:
    may_first_mapping = replace(
        _period_mapping(),
        source_key="20250501",
        report_period=date(2025, 5, 1),
    )
    may_first_observation = _observation(
        mapping=may_first_mapping,
        source_report_key="20250501",
    )

    before_shanghai_midnight = assess_sina_lrb_period_coverage(
        (may_first_observation,),
        as_of_utc=datetime(2025, 4, 30, 15, 59, 59, tzinfo=timezone.utc),
    )
    at_shanghai_midnight = assess_sina_lrb_period_coverage(
        (may_first_observation,),
        as_of_utc=datetime(2025, 4, 30, 16, 0, 0, tzinfo=timezone.utc),
    )

    assert before_shanghai_midnight.coverage.items == ()
    assert before_shanghai_midnight.unassessable_reason_counts == (
        ("report_period_after_as_of", 1),
    )
    assert len(at_shanghai_midnight.coverage.items) == 1
    assert at_shanghai_midnight.coverage.items[0].state is FinancialPeriodCoverageState.OBSERVED
    assert at_shanghai_midnight.report_completeness_established is False
    assert at_shanghai_midnight.announcement_time_independently_verified is False
    assert at_shanghai_midnight.revision_history_established is False
    assert at_shanghai_midnight.point_in_time_guarantee is False


def test_capture_evidence_must_be_present_and_as_of() -> None:
    missing = replace(_observation(), capture_evidence_ref=None)
    future = replace(_observation(), retrieved_at_utc=_AS_OF.replace(year=2026))

    result = assess_sina_lrb_period_coverage((missing, future), as_of_utc=_AS_OF)

    assert result.coverage.items == ()
    assert result.unassessable_reason_counts == (("capture_observation_evidence_missing", 2),)


def test_crosswalk_scope_and_isolation_evidence_are_required_as_of() -> None:
    wrong_scope = replace(_crosswalk(), source_instrument_id="000001.SZ")
    isolation_mapping = replace(
        _period_mapping(),
        source_key="20240930",
        report_period=date(2024, 9, 30),
        periodicity=Periodicity.QUARTERLY,
    )
    future_isolation = _observation(
        mapping=isolation_mapping,
        source_report_key="20240930",
        isolation_evidenced_at_utc=_AS_OF.replace(year=2026),
    )
    result = assess_sina_lrb_period_coverage(
        (_observation(crosswalk=wrong_scope), future_isolation), as_of_utc=_AS_OF
    )

    assert len(result.coverage.items) == 2
    assert all(item.state is FinancialPeriodCoverageState.UNKNOWN for item in result.coverage.items)
    basis_by_item = tuple(set(item.basis_codes) for item in result.coverage.items)
    assert any("identity_crosswalk_mismatch" in basis for basis in basis_by_item)
    assert any(
        "observation_version_isolated_or_isolation_unknown" in basis for basis in basis_by_item
    )


def test_plain_string_period_types_are_accepted_without_coercion() -> None:
    mapping = replace(
        _period_mapping(),
        statement_type="income",
        periodicity="annual",
    )

    result = assess_sina_lrb_period_coverage((_observation(mapping=mapping),), as_of_utc=_AS_OF)

    assert result.coverage.items[0].state is FinancialPeriodCoverageState.OBSERVED


def test_malformed_period_and_crosswalk_inputs_fail_closed() -> None:
    invalid_period = replace(_observation(), source_instrument_id="600519")
    reversed_crosswalk = replace(
        _crosswalk(), valid_from=date(2025, 1, 1), valid_through=date(2024, 12, 31)
    )

    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_period_input"):
        assess_sina_lrb_period_coverage((invalid_period,), as_of_utc=_AS_OF)
    with pytest.raises(SinaLrbFinancialMappingError, match="invalid_period_input"):
        assess_sina_lrb_period_coverage(
            (_observation(crosswalk=reversed_crosswalk),), as_of_utc=_AS_OF
        )


def test_period_observation_iterables_are_bounded() -> None:
    too_many = (_observation() for _ in range(257))

    with pytest.raises(SinaLrbFinancialMappingError, match="limit_exceeded"):
        assess_sina_lrb_period_coverage(too_many, as_of_utc=_AS_OF)


def test_unknown_isolation_does_not_become_an_observed_financial_period() -> None:
    observation = _observation(
        isolation_status="unknown",
        isolation_evidence_ref=None,
        isolation_evidenced_at_utc=None,
    )

    result = assess_sina_lrb_period_coverage((observation,), as_of_utc=_AS_OF)

    assert result.coverage.items[0].state is FinancialPeriodCoverageState.UNKNOWN
    assert "observation_version_isolated_or_isolation_unknown" in (
        result.coverage.items[0].basis_codes
    )
    assert all(
        item.state is not FinancialPeriodCoverageState.MISSING_CONFIRMED
        for item in result.coverage.items
    )


def test_single_capture_cannot_confirm_missing_periods_or_revision_history() -> None:
    result = assess_sina_lrb_period_coverage((_observation(),), as_of_utc=_AS_OF)

    assert all(
        item.state is not FinancialPeriodCoverageState.MISSING_CONFIRMED
        for item in result.coverage.items
    )
    assert result.report_completeness_established is False
    assert result.revision_history_established is False
    assert result.point_in_time_guarantee is False
