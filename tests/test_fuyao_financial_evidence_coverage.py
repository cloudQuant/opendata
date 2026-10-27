"""Synthetic-only tests for Fuyao financial evidence declaration coverage.

These tests validate the report's fail-closed structure. They do not use or
represent real Fuyao response captures, source authorization, or verified
financial units.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, get_type_hints

import pytest

from opendata.fuyao_financial_evidence_coverage import (
    REQUIRED_FINANCIAL_EVIDENCE_SLOTS,
    FuyaoFinancialEvidenceCoverage,
    assess_fuyao_financial_evidence_coverage,
)

_SYNTHETIC_SHA256 = "a" * 64


def _declaration(statement_type: str, periodicity: str) -> dict[str, str]:
    """Return one synthetic declaration with no source artifact behind it."""
    return {
        "source": "ths",
        "statement_type": statement_type,
        "periodicity": periodicity,
        "artifact_sha256": _SYNTHETIC_SHA256,
        "evidence_kind": "synthetic",
    }


def _all_six() -> list[dict[str, str]]:
    """Return synthetic declarations for each required pair."""
    return [
        _declaration(statement, cadence) for statement, cadence in REQUIRED_FINANCIAL_EVIDENCE_SLOTS
    ]


def _slot_state(
    report: FuyaoFinancialEvidenceCoverage, statement_type: str, periodicity: str
) -> str:
    """Get one slot's state for readable behavioral assertions."""
    slot = next(
        row
        for row in report.slots
        if row.statement_type == statement_type and row.periodicity == periodicity
    )
    return slot.state


def test_report_always_enumerates_six_required_pairs_and_manual_review() -> None:
    report = assess_fuyao_financial_evidence_coverage([])

    assert tuple((row.statement_type, row.periodicity) for row in report.slots) == (
        ("income", "annual"),
        ("income", "quarterly"),
        ("balance", "annual"),
        ("balance", "quarterly"),
        ("cashflow", "annual"),
        ("cashflow", "quarterly"),
    )
    assert len(report.slots) == 6
    assert all(row.state == "missing" for row in report.slots)
    assert report.acceptance_status == "requires_manual_real_source_review"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_six_synthetic_declarations_remain_pending_real_source_review() -> None:
    report = assess_fuyao_financial_evidence_coverage(_all_six())

    assert report.declaration_coverage == "six_slots_declared_pending_review"
    assert report.acceptance_status == "requires_manual_real_source_review"
    assert all(row.state == "declared_pending_review" for row in report.slots)
    assert not report.issues


def test_public_annotations_resolve_for_runtime_documentation() -> None:
    annotations = get_type_hints(assess_fuyao_financial_evidence_coverage)

    assert annotations["declarations"] == Iterable[object]
    assert annotations["return"] is FuyaoFinancialEvidenceCoverage


def test_missing_cadence_cannot_be_hidden_by_five_other_declarations() -> None:
    declarations = [
        row
        for row in _all_six()
        if not (row["statement_type"] == "cashflow" and row["periodicity"] == "quarterly")
    ]

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "cashflow", "quarterly") == "missing"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_duplicate_combination_is_reported_and_blocks_structural_coverage() -> None:
    declarations = _all_six()
    declarations.append(dict(declarations[0]))

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "income", "annual") == "duplicate"
    assert any(issue.code == "duplicate_combination" for issue in report.issues)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


@pytest.mark.parametrize(
    ("field", "value", "expected_issue"),
    [
        ("source", "other", "source_mismatch"),
        ("artifact_sha256", "not-a-digest", "artifact_digest_invalid"),
        ("evidence_kind", "verified", "evidence_kind_invalid"),
    ],
)
def test_wrong_declarations_invalidate_their_slot(
    field: str, value: str, expected_issue: str
) -> None:
    declarations: list[dict[str, Any]] = _all_six()
    declarations[0][field] = value

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "income", "annual") == "invalid"
    assert any(issue.code == expected_issue for issue in report.issues)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_unknown_statement_or_cadence_does_not_fill_any_required_slot() -> None:
    declarations: list[dict[str, Any]] = _all_six()
    declarations[0]["statement_type"] = "profit_and_loss"

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "income", "annual") == "missing"
    assert any(issue.code == "combination_invalid" for issue in report.issues)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_unit_digest_or_review_claim_as_extra_field_is_not_evidence() -> None:
    declarations: list[dict[str, Any]] = _all_six()
    declarations[0]["unit_mapping_sha256_declared"] = _SYNTHETIC_SHA256

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert any(issue.code == "declaration_fields_invalid" for issue in report.issues)
    assert report.acceptance_status == "requires_manual_real_source_review"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_malformed_input_row_is_reported_without_exposing_its_contents() -> None:
    secret_like_value = "synthetic-private-marker"
    report = assess_fuyao_financial_evidence_coverage([secret_like_value])

    assert len(report.issues) == 1
    assert report.issues[0].code == "declaration_not_mapping"
    assert secret_like_value not in repr(report)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_input_bound_blocks_six_slots_followed_by_unbounded_extra_rows() -> None:
    declarations = _all_six() + [_declaration("income", "annual")] * 59

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert any(issue.code == "too_many_declarations" for issue in report.issues)
    assert report.acceptance_status == "requires_manual_real_source_review"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_exactly_sixty_four_rows_does_not_trigger_the_row_limit() -> None:
    declarations = _all_six() + [_declaration("income", "annual")] * 58

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert not any(issue.code == "too_many_declarations" for issue in report.issues)
    assert _slot_state(report, "income", "annual") == "duplicate"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_oversized_row_is_rejected_without_echoing_secret_like_fields() -> None:
    secret_marker = "synthetic-secret-marker"
    oversized = _all_six()[0]
    oversized.update({f"extra_{index}": secret_marker for index in range(4096)})

    report = assess_fuyao_financial_evidence_coverage(_all_six() + [oversized])

    assert any(issue.code == "declaration_fields_invalid" for issue in report.issues)
    assert secret_marker not in repr(report)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_oversized_field_name_is_rejected_without_echoing_it() -> None:
    secret_marker = "synthetic-secret-key-marker"
    declaration = _all_six()[0]
    declaration[secret_marker * 100] = "synthetic-value"
    del declaration["evidence_kind"]

    report = assess_fuyao_financial_evidence_coverage([declaration])

    assert report.issues[0].code == "declaration_not_mapping"
    assert secret_marker not in repr(report)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_oversized_digest_is_rejected_before_pattern_matching_and_not_echoed() -> None:
    secret_marker = "synthetic-secret-marker"
    declarations: list[dict[str, Any]] = _all_six()
    declarations[0]["artifact_sha256"] = secret_marker + ("a" * 100_000)

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "income", "annual") == "invalid"
    assert any(issue.code == "artifact_digest_invalid" for issue in report.issues)
    assert secret_marker not in repr(report)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_non_builtin_mapping_is_rejected_without_calling_its_methods() -> None:
    class ExplodingMapping(Mapping[str, object]):
        """A custom mapping that must never be inspected by the validator."""

        def __getitem__(self, key: str) -> object:
            raise AssertionError(key)

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("iteration must not be attempted")

        def __len__(self) -> int:
            raise AssertionError("length must not be requested")

    report = assess_fuyao_financial_evidence_coverage([ExplodingMapping()])

    assert report.issues[0].code == "declaration_not_mapping"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_declaration_iterator_failure_is_sanitized_and_fail_closed() -> None:
    secret_marker = "synthetic-iterator-secret"

    def declarations() -> Iterator[object]:
        yield _all_six()[0]
        raise RuntimeError(secret_marker)

    report = assess_fuyao_financial_evidence_coverage(declarations())

    assert any(issue.code == "declaration_iteration_failed" for issue in report.issues)
    assert secret_marker not in repr(report)
    assert report.acceptance_status == "requires_manual_real_source_review"
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"


def test_string_subclasses_are_rejected_as_non_json_string_values() -> None:
    class SyntheticString(str):
        """A string subclass standing in for a non-JSON Python value."""

    declarations: list[dict[str, Any]] = _all_six()
    declarations[0]["source"] = SyntheticString("ths")

    report = assess_fuyao_financial_evidence_coverage(declarations)

    assert _slot_state(report, "income", "annual") == "invalid"
    assert any(issue.code == "source_mismatch" for issue in report.issues)
    assert report.declaration_coverage == "declarations_incomplete_or_invalid"
