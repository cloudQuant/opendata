"""Contracts for offline, source-qualified Sina FZB mapping candidates."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime  # noqa: TC003 - public DTO hints stay introspectable.
from typing import Literal, TypeAlias

from opendata.data.providers.sina.financial_statement_contract import (
    MAX_SINA_FZB_REPORTS,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    MAX_SINA_FZB_TOTAL_ROWS,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
)

ProposedSinaFzbItemKey: TypeAlias = Literal[
    "assets_total",
    "total_current_assets",
    "non_current_nets_total",
    "cash",
    "accounts_receivable",
    "total_debt",
    "holder_equity_total",
]
"""C10 crosswalk tokens proposed for the bounded FZB candidate pilot."""

SinaFzbCandidateStatus: TypeAlias = Literal["partial"]
"""Candidate projection status, always partial regardless of matched count."""

SinaFzbCandidateDisposition: TypeAlias = Literal["candidate", "unmapped"]
"""Disposition for a financial source item in the candidate projection."""

SinaFzbUnmappedReason: TypeAlias = Literal["unknown_source_item", "title_mismatch"]
"""Stable reason a source item was not mapped by the exact pair table."""

SinaFzbCandidateMappingNamespace: TypeAlias = Literal["c10-seven-balance-v1"]
"""Versioned namespace for the seven C10-derived proposed mapping tokens."""

SinaFzbCandidateErrorCode: TypeAlias = Literal[
    "invalid_snapshot",
    "query_mismatch",
    "source_identity_mismatch",
    "currency_not_admitted",
    "conflicting_source_entry",
    "limit_exceeded",
]
"""Safe, stable errors raised by candidate-contract validation."""

SINA_FZB_CANDIDATE_MAPPING_NAMESPACE: SinaFzbCandidateMappingNamespace = "c10-seven-balance-v1"
"""Exact namespace attached to every proposed candidate mapping."""

SINA_FZB_CANDIDATE_STATUS: SinaFzbCandidateStatus = "partial"
"""Fixed status for this candidate-only projection."""

SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT = 7
"""Number of exact source ID/title pairs in the frozen candidate table."""

SINA_FZB_CANDIDATE_REQUIRED_CURRENCY = "CNY"
"""Currency admitted for this finite pilot; this does not establish units."""

SINA_FZB_CANDIDATE_EVIDENCE_REFS: tuple[str, ...] = (
    "scripts/ops/ths_financial_statement_cross_check.py"
    "@sha256:eda12562d0d48757c1389e86224e54c5ed62a86cb16bb5468ab17935b24f5bb8"
    "#ITEM_MAP.balance",
    "docs/evidence/C10/README.md"
    "@sha256:95e0de3eec01d57e7fedfcb5a96428e1db215ae990aaf86f6cab5affbb4cb1dc"
    "#C10-tested-cells",
    "tests/fixtures/upstream/financial_statement/responses.json.gz"
    "@sha256:fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
    "#recorded-sh600519-fzb",
)
"""Pinned crosswalk, finite C10 scope, and recorded source-identity evidence."""

SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS = SINA_FZB_CANDIDATE_EVIDENCE_REFS
"""Evidence references attached to each matched candidate item."""


@dataclass(frozen=True, slots=True)
class SinaFzbCandidateMapping:
    """One exact source identity pair and its namespace-scoped proposal."""

    mapping_namespace: SinaFzbCandidateMappingNamespace
    source_item_id: str
    source_title: str
    proposed_item_key: ProposedSinaFzbItemKey


@dataclass(frozen=True, slots=True)
class SinaFzbCandidateItem:
    """Source-native item with an explicit mapping proposal or unmapped reason."""

    source_item: SinaFzbItem
    mapping_namespace: SinaFzbCandidateMappingNamespace | None
    disposition: SinaFzbCandidateDisposition
    proposed_item_key: ProposedSinaFzbItemKey | None
    unmapped_reason: SinaFzbUnmappedReason | None
    mapping_evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SinaFzbCandidateAccounting:
    """Deterministic per-report counts and missing/unmapped source positions."""

    expected_mapping_count: int
    matched_source_item_ids: tuple[str, ...]
    missing_expected_source_pairs: tuple[tuple[str, str], ...]
    unmapped_source_ordinals: tuple[int, ...]
    raw_source_row_occurrence_count: int
    unique_source_financial_item_count: int
    unique_source_group_marker_count: int


@dataclass(frozen=True, slots=True)
class SinaFzbCandidateReport:
    """Candidate entries and accounting alongside their untouched W9 report."""

    source_report: SinaFzbReport
    entries: tuple[SinaFzbCandidateItem | SinaFzbGroupMarker, ...]
    accounting: SinaFzbCandidateAccounting


@dataclass(frozen=True, slots=True)
class SinaFzbCandidateProjection:
    """Immutable partial mapping candidate projection of a W9 snapshot."""

    query: SinaFzbQuery
    retrieved_at_utc: datetime
    mapping_namespace: SinaFzbCandidateMappingNamespace
    status: SinaFzbCandidateStatus
    evidence_refs: tuple[str, ...]
    reports: tuple[SinaFzbCandidateReport, ...]


SINA_FZB_CANDIDATE_MAPPINGS: tuple[SinaFzbCandidateMapping, ...] = (
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="TOTASSET",
        source_title="资产总计",
        proposed_item_key="assets_total",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="TOTCURRASSET",
        source_title="流动资产合计",
        proposed_item_key="total_current_assets",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="TOTALNONCASSETS",
        source_title="非流动资产合计",
        proposed_item_key="non_current_nets_total",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="CURFDS",
        source_title="货币资金",
        proposed_item_key="cash",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="ACCORECE",
        source_title="应收账款",
        proposed_item_key="accounts_receivable",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="TOTLIAB",
        source_title="负债合计",
        proposed_item_key="total_debt",
    ),
    SinaFzbCandidateMapping(
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        source_item_id="RIGHAGGR",
        source_title="所有者权益(或股东权益)合计",
        proposed_item_key="holder_equity_total",
    ),
)
"""Frozen exact-pair mapping table, in the required deterministic order."""

_ERROR_MESSAGES: dict[SinaFzbCandidateErrorCode, str] = {
    "invalid_snapshot": "Sina FZB candidate snapshot is invalid.",
    "query_mismatch": "Sina FZB candidate query does not match.",
    "source_identity_mismatch": "Sina FZB candidate source identity is invalid.",
    "currency_not_admitted": "Sina FZB candidate currency is not admitted.",
    "conflicting_source_entry": "Sina FZB candidate source entries conflict.",
    "limit_exceeded": "Sina FZB candidate input exceeded a limit.",
}


class SinaFzbCandidateError(Exception):
    """Sanitized source-specific error for candidate contract validation."""

    def __init__(self, code: SinaFzbCandidateErrorCode) -> None:
        """Initialize with a stable code and a fixed, non-sensitive message.

        Args:
            code: One of the candidate error codes declared by this module.

        Raises:
            ValueError: If an unsupported error code is supplied at runtime.
        """
        message = _ERROR_MESSAGES.get(code)
        if message is None:
            raise ValueError("Unsupported Sina FZB candidate error code.")
        self._code = code
        super().__init__(message)

    @property
    def code(self) -> SinaFzbCandidateErrorCode:
        """Return the stable, sanitized error code."""
        return self._code


__all__ = [
    "MAX_SINA_FZB_REPORTS",
    "MAX_SINA_FZB_ROWS_PER_REPORT",
    "MAX_SINA_FZB_TOTAL_ROWS",
    "SINA_FZB_CANDIDATE_EVIDENCE_REFS",
    "SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT",
    "SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS",
    "SINA_FZB_CANDIDATE_MAPPING_NAMESPACE",
    "SINA_FZB_CANDIDATE_MAPPINGS",
    "SINA_FZB_CANDIDATE_REQUIRED_CURRENCY",
    "SINA_FZB_CANDIDATE_STATUS",
    "ProposedSinaFzbItemKey",
    "SinaFzbCandidateAccounting",
    "SinaFzbCandidateDisposition",
    "SinaFzbCandidateError",
    "SinaFzbCandidateErrorCode",
    "SinaFzbCandidateItem",
    "SinaFzbCandidateMapping",
    "SinaFzbCandidateMappingNamespace",
    "SinaFzbCandidateProjection",
    "SinaFzbCandidateReport",
    "SinaFzbCandidateStatus",
    "SinaFzbUnmappedReason",
]
