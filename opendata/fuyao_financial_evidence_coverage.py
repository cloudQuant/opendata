"""Fail-closed structural review of Fuyao financial evidence declarations.

The report always requires manual review against real source captures. This
module checks only whether declarations name the six expected statement and
cadence pairs without structural errors. A syntactically valid SHA-256 value
does not prove that an artifact exists, came from Fuyao, contains the claimed
statement, or supports the claimed units or history. This module does not
read files, issue requests, inspect credentials, or connect to a database.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal, TypeAlias

DeclarationRows: TypeAlias = Iterable[object]

StatementType: TypeAlias = Literal["income", "balance", "cashflow"]
Periodicity: TypeAlias = Literal["annual", "quarterly"]
SlotState: TypeAlias = Literal["missing", "duplicate", "invalid", "declared_pending_review"]
DeclarationCoverage: TypeAlias = Literal[
    "declarations_incomplete_or_invalid", "six_slots_declared_pending_review"
]
AcceptanceStatus: TypeAlias = Literal["requires_manual_real_source_review"]
IssueCode: TypeAlias = Literal[
    "declaration_not_mapping",
    "declaration_fields_invalid",
    "combination_invalid",
    "source_mismatch",
    "artifact_digest_invalid",
    "evidence_kind_invalid",
    "duplicate_combination",
    "too_many_declarations",
    "declaration_iteration_failed",
]

EvidenceSlot: TypeAlias = tuple[StatementType, Periodicity]

REQUIRED_FINANCIAL_EVIDENCE_SLOTS: Final[tuple[EvidenceSlot, ...]] = (
    ("income", "annual"),
    ("income", "quarterly"),
    ("balance", "annual"),
    ("balance", "quarterly"),
    ("cashflow", "annual"),
    ("cashflow", "quarterly"),
)
_STATEMENT_BY_TOKEN: Final[dict[str, StatementType]] = {
    "income": "income",
    "balance": "balance",
    "cashflow": "cashflow",
}
_PERIODICITY_BY_TOKEN: Final[dict[str, Periodicity]] = {
    "annual": "annual",
    "quarterly": "quarterly",
}
_ALLOWED_FIELDS: Final[frozenset[str]] = frozenset(
    {"source", "statement_type", "periodicity", "artifact_sha256", "evidence_kind"}
)
_EVIDENCE_KINDS: Final[frozenset[str]] = frozenset({"synthetic", "captured_unreviewed"})
_SHA256_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_MAX_DECLARATIONS: Final[int] = 64
_MAX_ROW_FIELDS: Final[int] = len(_ALLOWED_FIELDS)
_MAX_FIELD_NAME_CHARS: Final[int] = 64
_MAX_DIGEST_CHARS: Final[int] = 64
_MAX_SOURCE_CHARS: Final[int] = len("ths")
_MAX_STATEMENT_CHARS: Final[int] = len("cashflow")
_MAX_PERIODICITY_CHARS: Final[int] = len("quarterly")
_MAX_EVIDENCE_KIND_CHARS: Final[int] = len("captured_unreviewed")


@dataclass(frozen=True, slots=True)
class EvidenceSlotAssessment:
    """Structural state for one required statement and cadence pair."""

    statement_type: StatementType
    periodicity: Periodicity
    declaration_count: int
    state: SlotState


@dataclass(frozen=True, slots=True)
class EvidenceCoverageIssue:
    """Sanitized issue referring to an input row without copying its contents."""

    code: IssueCode
    row_index: int | None
    slot: EvidenceSlot | None


@dataclass(frozen=True, slots=True)
class FuyaoFinancialEvidenceCoverage:
    """Six-slot declaration report that never asserts source acceptance."""

    slots: tuple[EvidenceSlotAssessment, ...]
    issues: tuple[EvidenceCoverageIssue, ...]
    declaration_coverage: DeclarationCoverage

    @property
    def acceptance_status(self) -> AcceptanceStatus:
        """Return the fixed status; callers cannot promote declarations to accepted."""
        return "requires_manual_real_source_review"


def assess_fuyao_financial_evidence_coverage(
    declarations: DeclarationRows,
) -> FuyaoFinancialEvidenceCoverage:
    """Assess declarations for all six Fuyao financial statement combinations.

    Every input row must be an exact built-in ``dict`` (as produced by a JSON
    decoder) with exactly these fields: ``source``, ``statement_type``,
    ``periodicity``, ``artifact_sha256``, and ``evidence_kind``. The source
    token must be ``ths``. Evidence kinds are
    restricted to ``synthetic`` or ``captured_unreviewed``; neither is
    accepted as proof of live-source provenance. A hash is checked for
    lowercase SHA-256 syntax only. Extra fields, including unit declarations
    or review claims, invalidate the row.

    The input is bounded to 64 rows. Invalid rows are reported by index and a
    stable code only; supplied values are never copied into the result.

    Args:
        declarations: In-memory declaration rows.

    Returns:
        The six slot states, sanitized structural issues, a declaration-only
        coverage label, and the permanent manual real-source review status.
    """
    counts: Counter[EvidenceSlot] = Counter()
    invalid_slots: set[EvidenceSlot] = set()
    issues: list[EvidenceCoverageIssue] = []

    try:
        iterator = iter(declarations)
    except Exception:
        iterator = iter(())
        issues.append(EvidenceCoverageIssue("declaration_iteration_failed", None, None))

    row_index = 0
    while True:
        try:
            raw_row = next(iterator)
        except StopIteration:
            break
        except Exception:
            issues.append(EvidenceCoverageIssue("declaration_iteration_failed", row_index, None))
            break

        if row_index >= _MAX_DECLARATIONS:
            issues.append(EvidenceCoverageIssue("too_many_declarations", row_index, None))
            break

        if type(raw_row) is not dict:
            issues.append(EvidenceCoverageIssue("declaration_not_mapping", row_index, None))
            row_index += 1
            continue

        # Reject oversized rows before visiting their keys or values.
        if len(raw_row) > _MAX_ROW_FIELDS:
            issues.append(EvidenceCoverageIssue("declaration_fields_invalid", row_index, None))
            row_index += 1
            continue
        keys = tuple(raw_row.keys())
        if any(type(key) is not str or len(key) > _MAX_FIELD_NAME_CHARS for key in keys):
            issues.append(EvidenceCoverageIssue("declaration_not_mapping", row_index, None))
            row_index += 1
            continue
        fields = frozenset(keys)

        statement = raw_row.get("statement_type")
        periodicity = raw_row.get("periodicity")
        slot = _canonical_slot(statement, periodicity)
        if slot is None:
            issues.append(EvidenceCoverageIssue("combination_invalid", row_index, None))
            row_index += 1
            continue

        counts[slot] += 1
        row_is_invalid = False
        if fields != _ALLOWED_FIELDS:
            issues.append(EvidenceCoverageIssue("declaration_fields_invalid", row_index, slot))
            row_is_invalid = True
        source = raw_row.get("source")
        if type(source) is not str or len(source) > _MAX_SOURCE_CHARS or source != "ths":
            issues.append(EvidenceCoverageIssue("source_mismatch", row_index, slot))
            row_is_invalid = True

        digest = raw_row.get("artifact_sha256")
        if (
            type(digest) is not str
            or len(digest) != _MAX_DIGEST_CHARS
            or _SHA256_PATTERN.fullmatch(digest) is None
        ):
            issues.append(EvidenceCoverageIssue("artifact_digest_invalid", row_index, slot))
            row_is_invalid = True

        evidence_kind = raw_row.get("evidence_kind")
        if (
            type(evidence_kind) is not str
            or len(evidence_kind) > _MAX_EVIDENCE_KIND_CHARS
            or evidence_kind not in _EVIDENCE_KINDS
        ):
            issues.append(EvidenceCoverageIssue("evidence_kind_invalid", row_index, slot))
            row_is_invalid = True

        if row_is_invalid:
            invalid_slots.add(slot)

        row_index += 1

    for slot, count in counts.items():
        if count > 1:
            issues.append(EvidenceCoverageIssue("duplicate_combination", None, slot))

    slot_assessments: list[EvidenceSlotAssessment] = []
    for slot in REQUIRED_FINANCIAL_EVIDENCE_SLOTS:
        count = counts[slot]
        if count == 0:
            state: SlotState = "missing"
        elif slot in invalid_slots:
            state = "invalid"
        elif count > 1:
            state = "duplicate"
        else:
            state = "declared_pending_review"
        slot_assessments.append(
            EvidenceSlotAssessment(
                statement_type=slot[0],
                periodicity=slot[1],
                declaration_count=count,
                state=state,
            )
        )

    issues_tuple = tuple(issues)
    all_slots_declared_once = all(
        assessment.state == "declared_pending_review" for assessment in slot_assessments
    )
    declaration_coverage: DeclarationCoverage = (
        "six_slots_declared_pending_review"
        if all_slots_declared_once and not issues_tuple
        else "declarations_incomplete_or_invalid"
    )
    return FuyaoFinancialEvidenceCoverage(
        slots=tuple(slot_assessments),
        issues=issues_tuple,
        declaration_coverage=declaration_coverage,
    )


def _canonical_slot(statement: object, periodicity: object) -> EvidenceSlot | None:
    """Return a supported exact pair without coercing or normalizing input."""
    if (
        type(statement) is not str
        or len(statement) > _MAX_STATEMENT_CHARS
        or statement not in _STATEMENT_BY_TOKEN
        or type(periodicity) is not str
        or len(periodicity) > _MAX_PERIODICITY_CHARS
        or periodicity not in _PERIODICITY_BY_TOKEN
    ):
        return None
    return _STATEMENT_BY_TOKEN[statement], _PERIODICITY_BY_TOKEN[periodicity]


__all__ = [
    "DeclarationRows",
    "REQUIRED_FINANCIAL_EVIDENCE_SLOTS",
    "EvidenceCoverageIssue",
    "EvidenceSlotAssessment",
    "FuyaoFinancialEvidenceCoverage",
    "assess_fuyao_financial_evidence_coverage",
]
