"""Synthetic, offline tests for source-catalog candidate projection and comparison."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, cast

import pytest

import opendata.pipeline.instrument_catalog_reconciliation as catalog_reconciliation
from opendata.data.current_instrument_census import InstrumentIdentity
from opendata.data.instrument_source_census import FieldObservationState
from opendata.pipeline.fuyao_instrument_census import (
    FuyaoInstrumentPageInput,
    build_fuyao_instrument_census,
)
from opendata.pipeline.instrument_catalog_reconciliation import (
    CandidateFieldObservation,
    CandidateMembershipContext,
    CatalogFieldBindings,
    CatalogFieldConflictStatus,
    CatalogIdentityDisposition,
    CatalogIdentitySetObservation,
    CatalogReconciliation,
    CatalogSetDifferenceStatus,
    FuyaoInstrumentFieldBindings,
    InstrumentCatalogReconciliationError,
    project_fuyao_capture,
    project_sse_capture,
    reconcile_instrument_candidates,
)
from opendata.pipeline.sse_instrument_census import (
    SSEInstrumentPageInput,
    SSEInstrumentSelector,
    build_sse_instrument_census,
)

_OBSERVED_AT = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
_SSE_FIELDS = CatalogFieldBindings(
    code_field="A_STOCK_CODE",
    name_field="SEC_NAME_CN",
    exchange_field="EXCHANGE",
    asset_type_field="ASSET_TYPE",
    status_field="COMPANY_STATUS",
    listing_date_field="LIST_DATE",
    last_trading_date_field="LAST_TRADE_DATE",
    termination_date_field="DELIST_DATE",
)


class _NoOffsetTimezone(tzinfo):
    """Represent a malformed timezone whose offset cannot be resolved."""

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        del dt
        return None


def _replace_with_unvalidated_values(instance: Any, changes: dict[str, object]) -> Any:
    """Call dataclass replacement with intentionally malformed DTO field values."""
    dynamic_replace: Any = replace
    return dynamic_replace(instance, **changes)


def _invoke_with_unvalidated_input(function: Any, *args: Any, **kwargs: Any) -> Any:
    """Exercise public validation boundaries with deliberately wrong runtime types."""
    return function(*args, **kwargs)


def _sse_capture(
    rows: list[dict[str, object]],
    *,
    selector: SSEInstrumentSelector = SSEInstrumentSelector.CURRENT_MAIN_A,
    sequence: int = 1,
    page_number: str = "1",
) -> Any:
    """Build one synthetic SSE capture with explicit request context."""
    body = json.dumps(
        {"result": rows, "pageHelp": {"pageCount": 2, "pageNo": page_number}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    params = {
        "STOCK_TYPE": "8"
        if selector
        in (SSEInstrumentSelector.CURRENT_STAR, SSEInstrumentSelector.STATUS3_STAR_CANDIDATE)
        else "1",
        "COMPANY_STATUS": "3"
        if selector
        in (
            SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
            SSEInstrumentSelector.STATUS3_STAR_CANDIDATE,
        )
        else "2,4,5,7,8",
        "pageHelp.pageNo": page_number,
    }
    return build_sse_instrument_census(
        [
            SSEInstrumentPageInput(
                selector=selector,
                sequence_number=sequence,
                raw_response_body=body,
                declared_response_sha256=hashlib.sha256(body).hexdigest(),
                request_summary={"method": "GET", "params": params},
            )
        ],
        observed_at=_OBSERVED_AT,
    )


def _fuyao_capture(rows: list[dict[str, object]], *, sequence: int = 1) -> Any:
    """Build one synthetic Fuyao A-share page with a caller-declared scope."""
    body = json.dumps(
        {"code": 0, "data": {"item": rows}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return build_fuyao_instrument_census(
        [
            FuyaoInstrumentPageInput(
                sequence_number=sequence,
                raw_response_body=body,
                declared_response_sha256=hashlib.sha256(body).hexdigest(),
                request_summary={
                    "method": "GET",
                    "path": "/api/meta/tickers/list",
                    "params": {"asset_type": "a-share", "limit": 10000, "offset": 0},
                },
                source_page_number=sequence - 1,
                http_status_code=200,
            )
        ],
        observed_at=_OBSERVED_AT,
    )


def _fuyao_row(
    code: str,
    *,
    exchange: str | None = None,
    name: object = "合成证券",
    asset_type: str = "a-share",
    list_date: object = None,
    status: str | None = None,
) -> dict[str, object]:
    """Return a source-shaped synthetic Fuyao row."""
    suffix = code.rpartition(".")[2]
    row: dict[str, object] = {
        "thscode": code,
        "ticker": code.partition(".")[0],
        "exchange": exchange if exchange is not None else suffix,
        "asset_type": asset_type,
        "name": name,
        "list_date": list_date,
    }
    if status is not None:
        row["status"] = status
    return row


def test_sse_projection_preserves_row_and_page_provenance_without_lifecycle_promotion() -> None:
    capture = _sse_capture(
        [
            {
                "A_STOCK_CODE": "600001",
                "SEC_NAME_CN": "甲公司",
                "EXCHANGE": "SH",
                "ASSET_TYPE": "equity",
                "COMPANY_STATUS": "2",
                "LIST_DATE": "2001-01-01",
                "LAST_TRADE_DATE": None,
                "DELIST_DATE": None,
            }
        ],
        page_number="2",
    )

    projection = project_sse_capture(capture, field_bindings=_SSE_FIELDS)

    assert projection.current_query_scope == ("SH",)
    assert projection.market_completeness == "unknown"
    candidate = projection.candidates[0]
    assert candidate.identity_candidate is not None
    assert candidate.identity_candidate.exchange == "SH"
    assert candidate.identity_candidate.symbol == "600001"
    assert candidate.source_page_number == "2"
    assert candidate.page_sequence == 1
    assert candidate.source_row_number == 1
    assert candidate.response_sha256 == capture.pages[0].response_sha256
    assert candidate.source_code.source_field == "A_STOCK_CODE"
    assert candidate.fields["display_name"].raw_value == "甲公司"
    assert candidate.fields["termination_date"].state is FieldObservationState.NULL
    assert candidate.membership_context is CandidateMembershipContext.CURRENT_QUERY


def test_sse_status3_selector_is_candidate_context_not_current_membership() -> None:
    capture = _sse_capture(
        [{"A_STOCK_CODE": "600002", "SEC_NAME_CN": "乙公司"}],
        selector=SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
    )

    projection = project_sse_capture(capture, field_bindings=_SSE_FIELDS)

    assert projection.current_query_scope == ()
    assert (
        projection.candidates[0].membership_context
        is CandidateMembershipContext.STATUS3_QUERY_CANDIDATE
    )
    assert projection.candidates[0].identity_candidate is not None
    assert projection.candidates[0].disposition is CatalogIdentityDisposition.CANDIDATE


@pytest.mark.parametrize(
    ("row", "disposition", "reason"),
    [
        (
            {"SEC_NAME_CN": "无代码"},
            CatalogIdentityDisposition.UNRESOLVED,
            "mapped_code_missing_or_not_six_ascii_digits",
        ),
        (
            {"A_STOCK_CODE": 600001},
            CatalogIdentityDisposition.UNRESOLVED,
            "mapped_code_missing_or_not_six_ascii_digits",
        ),
        (
            {"A_STOCK_CODE": "600001", "EXCHANGE": "SZ"},
            CatalogIdentityDisposition.CONFLICT,
            "exchange_field_disagrees_with_selector_context",
        ),
    ],
)
def test_sse_projection_keeps_invalid_or_conflicting_rows_unresolved(
    row: dict[str, object],
    disposition: CatalogIdentityDisposition,
    reason: str,
) -> None:
    capture = _sse_capture([row])

    candidate = project_sse_capture(capture, field_bindings=_SSE_FIELDS).candidates[0]

    assert candidate.disposition is disposition
    assert candidate.identity_candidate is None
    assert candidate.unresolved_reason == reason
    assert dict(candidate.raw_row) == row


def test_candidate_dto_rejects_identity_and_observation_mismatches() -> None:
    candidate = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司", "EXCHANGE": "SH"}]),
        field_bindings=_SSE_FIELDS,
    ).candidates[0]

    with pytest.raises(InstrumentCatalogReconciliationError, match="identity_source_code_mismatch"):
        replace(candidate, identity_candidate=InstrumentIdentity("SH", "600002"))
    with pytest.raises(InstrumentCatalogReconciliationError, match="source_code_raw_row_mismatch"):
        replace(candidate, raw_row={**candidate.raw_row, "A_STOCK_CODE": "600002"})
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="field_observation_raw_row_mismatch"
    ):
        replace(
            candidate,
            fields={
                **candidate.fields,
                "display_name": CandidateFieldObservation(
                    "SEC_NAME_CN", FieldObservationState.VALUE, "错误名称"
                ),
            },
        )


@pytest.mark.parametrize(
    ("source_field", "state", "raw_value", "error"),
    [
        ("CODE", "unsupported", None, "field_state_invalid"),
        ("CODE", FieldObservationState.UNMAPPED, None, "unmapped_field_has_source_value"),
        (None, FieldObservationState.VALUE, "600001", "mapped_field_name_required"),
        ("CODE", FieldObservationState.ABSENT, "600001", "empty_field_has_raw_value"),
        ("CODE", FieldObservationState.NULL, "600001", "empty_field_has_raw_value"),
        ("CODE", FieldObservationState.VALUE, None, "value_field_raw_value_required"),
        ("bad field", FieldObservationState.ABSENT, None, "source_field_invalid"),
    ],
)
def test_field_observation_dto_rejects_invalid_states_and_payloads(
    source_field: str | None,
    state: FieldObservationState | str,
    raw_value: Any,
    error: str,
) -> None:
    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        CandidateFieldObservation(source_field, state, raw_value)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"source_id": None}, "source_id_invalid"),
        ({"observed_at": "not-a-datetime"}, "observed_at_invalid"),
        ({"observed_at": datetime(2026, 10, 30, 4, 0)}, "observed_at_invalid"),
        (
            {"observed_at": datetime(2026, 10, 30, 4, 0, tzinfo=_NoOffsetTimezone())},
            "observed_at_invalid",
        ),
        ({"page_sequence": True}, "page_sequence_invalid"),
        ({"source_row_number": 0}, "source_row_number_invalid"),
        ({"source_page_number": True}, "source_page_number_invalid"),
        ({"response_sha256": "g" * 64}, "response_sha256_invalid"),
        ({"request_summary_sha256": "g" * 64}, "request_sha256_invalid"),
        ({"membership_context": "historical"}, "candidate_context_invalid"),
        ({"disposition": "active"}, "candidate_context_invalid"),
        ({"exchange_candidate": "XX"}, "exchange_candidate_invalid"),
        ({"identity_candidate": object()}, "identity_candidate_invalid"),
        (
            {"identity_candidate": InstrumentIdentity("SZ", "600001")},
            "identity_exchange_mismatch",
        ),
        (
            {
                "disposition": CatalogIdentityDisposition.UNRESOLVED,
                "unresolved_reason": "caller rejected identity",
            },
            "noncandidate_has_identity",
        ),
        ({"identity_candidate": None}, "candidate_identity_required"),
        (
            {
                "disposition": CatalogIdentityDisposition.UNRESOLVED,
                "identity_candidate": None,
                "unresolved_reason": None,
            },
            "unresolved_reason_required",
        ),
        (
            {
                "disposition": CatalogIdentityDisposition.CONFLICT,
                "identity_candidate": None,
                "unresolved_reason": None,
            },
            "conflict_reason_required",
        ),
        ({"source_code": object()}, "source_code_observation_invalid"),
        ({"fields": []}, "candidate_fields_invalid"),
        (
            {"fields": {"bad field": CandidateFieldObservation(None, "unmapped")}},
            "candidate_field_key_invalid",
        ),
        ({"fields": {"display_name": "raw text"}}, "candidate_field_value_invalid"),
        (
            {
                "fields": {
                    f"field_{index}": CandidateFieldObservation(None, "unmapped")
                    for index in range(17)
                }
            },
            "candidate_fields_invalid",
        ),
        ({"raw_row": {}}, "raw_row_invalid"),
    ],
)
def test_candidate_dto_rejects_invalid_provenance_and_identity_contracts(
    changes: dict[str, object], error: str
) -> None:
    candidate = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    ).candidates[0]

    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _replace_with_unvalidated_values(candidate, changes)


def test_fuyao_candidate_dto_requires_a_share_scope_and_current_membership() -> None:
    candidate = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]),
        declared_asset_type="a-share",
    ).candidates[0]

    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="fuyao_a_share_identity_contract_mismatch",
    ):
        replace(
            candidate,
            raw_row={**candidate.raw_row, "asset_type": "a-share-index"},
            fields={
                **candidate.fields,
                "asset_type": CandidateFieldObservation(
                    "asset_type", FieldObservationState.VALUE, "a-share-index"
                ),
            },
        )

    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="fuyao_a_share_identity_contract_mismatch",
    ):
        replace(candidate, membership_context=CandidateMembershipContext.STATUS3_QUERY_CANDIDATE)


def test_fuyao_candidate_dto_requires_the_provider_selector_and_asset_observation() -> None:
    candidate = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    ).candidates[0]

    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="fuyao_a_share_identity_contract_mismatch",
    ):
        replace(candidate, selector_context="another-query")

    raw_row_without_asset = dict(candidate.raw_row)
    raw_row_without_asset.pop("asset_type")
    fields_without_asset = dict(candidate.fields)
    fields_without_asset.pop("asset_type")
    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="fuyao_a_share_identity_contract_mismatch",
    ):
        replace(candidate, raw_row=raw_row_without_asset, fields=fields_without_asset)

    absent_asset = CandidateFieldObservation("asset_type", FieldObservationState.ABSENT)
    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="fuyao_a_share_identity_contract_mismatch",
    ):
        replace(
            candidate,
            raw_row=raw_row_without_asset,
            fields={**fields_without_asset, "asset_type": absent_asset},
        )


def test_sse_bare_code_cannot_be_rebound_away_from_selector_exchange() -> None:
    candidate = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    ).candidates[0]

    with pytest.raises(
        InstrumentCatalogReconciliationError, match="sse_selector_identity_mismatch"
    ):
        replace(
            candidate,
            exchange_candidate="SZ",
            identity_candidate=InstrumentIdentity("SZ", "600001"),
        )


def test_projection_rejects_candidate_identity_outside_its_scope() -> None:
    projection = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )

    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="candidate_identity_outside_declared_scope",
    ):
        replace(
            projection,
            declared_exchange_scope=("SZ",),
            current_query_scope=("SZ",),
        )


def test_empty_sse_projection_cannot_claim_another_exchange_or_status3_current_scope() -> None:
    status3_projection = project_sse_capture(
        _sse_capture(
            [{"A_STOCK_CODE": "600002"}],
            selector=SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
        ),
        field_bindings=_SSE_FIELDS,
    )
    empty_status3_projection = replace(status3_projection, candidates=())

    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_projection_scope_invalid"):
        replace(
            empty_status3_projection,
            declared_exchange_scope=("SZ",),
            current_query_scope=("SZ",),
            contexts_seen=(),
        )

    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_projection_scope_invalid"):
        replace(status3_projection, current_query_scope=("SH",))
    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_projection_scope_invalid"):
        replace(status3_projection, contexts_seen=("unrecognized-selector",))
    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_projection_scope_invalid"):
        replace(status3_projection, contexts_seen=())


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"source_id": "invalid source"}, "source_id_invalid"),
        ({"observed_at": datetime(2026, 10, 30, 4, 0)}, "observed_at_invalid"),
        ({"declared_exchange_scope": ()}, "declared_exchange_scope_invalid"),
        ({"current_query_scope": ("SZ",)}, "current_scope_outside_declared_scope"),
        ({"acquisition_status": "unknown"}, "acquisition_status_invalid"),
        ({"candidates": (object(),)}, "candidates_invalid"),
        ({"contexts_seen": (None,)}, "contexts_seen_invalid"),
        ({"pagination_status": ""}, "pagination_status_invalid"),
        ({"pagination_status": 1}, "pagination_status_invalid"),
        ({"source_authenticity": ""}, "source_authenticity_invalid"),
        ({"source_authenticity": "x" * 65}, "source_authenticity_invalid"),
        ({"market_completeness": ""}, "market_completeness_invalid"),
        ({"market_completeness": "complete"}, "market_completeness_must_remain_unknown"),
    ],
)
def test_projection_dto_rejects_invalid_scopes_and_capture_metadata(
    changes: dict[str, object], error: str
) -> None:
    projection = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )

    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _replace_with_unvalidated_values(projection, changes)


def test_projection_rejects_candidate_rows_from_another_source() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    )

    with pytest.raises(InstrumentCatalogReconciliationError, match="candidate_projection_mismatch"):
        replace(left, candidates=right.candidates)


def test_projection_candidate_count_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog_reconciliation, "_MAX_CANDIDATES", 1)
    capture = _sse_capture(
        [
            {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"},
            {"A_STOCK_CODE": "600002", "SEC_NAME_CN": "乙公司"},
        ]
    )

    with pytest.raises(InstrumentCatalogReconciliationError, match="candidates_too_large"):
        project_sse_capture(capture, field_bindings=_SSE_FIELDS)

    first = InstrumentIdentity("SH", "600001")
    second = InstrumentIdentity("SH", "600002")
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="left_observed_identities_too_large"
    ):
        CatalogIdentitySetObservation(
            difference_status=CatalogSetDifferenceStatus.OBSERVED,
            left_observed_identities=(first, second),
            right_observed_identities=(),
            left_only_observed=(first, second),
            right_only_observed=(),
            shared_observed=(),
            unknown_reasons=(),
        )


def test_current_identity_candidate_must_belong_to_current_query_scope() -> None:
    candidate = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    ).candidates[0]
    generic_candidate = replace(candidate, source_id="custom.instrument")
    projection = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    )

    with pytest.raises(
        InstrumentCatalogReconciliationError, match="candidate_identity_outside_current_scope"
    ):
        replace(
            projection,
            source_id="custom.instrument",
            current_query_scope=(),
            candidates=(generic_candidate,),
        )


def _reconciliation_with_name_conflict() -> CatalogReconciliation:
    left = project_sse_capture(
        _sse_capture(
            [
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称一"},
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称二"},
            ]
        ),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name="右侧名称")]),
        declared_asset_type="a-share",
    )
    return reconcile_instrument_candidates(left, right)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"identity": object()}, "conflict_identity_invalid"),
        ({"field_name": "status"}, "conflict_field_invalid"),
        ({"left_source_id": "invalid source"}, "conflict_source_id_invalid"),
        ({"left_page_sequence": 0}, "conflict_page_sequence_invalid"),
        ({"right_source_row_number": 0}, "conflict_row_number_invalid"),
        ({"left_identity_row_count": 0}, "conflict_identity_row_count_invalid"),
        ({"right_identity_row_count": 0}, "conflict_identity_row_count_invalid"),
        ({"ambiguous_identity": False}, "conflict_ambiguity_flag_invalid"),
        ({"left_value": object()}, "raw_row_value_not_json"),
    ],
)
def test_source_conflict_dto_rejects_invalid_provenance_and_conflicting_values(
    changes: dict[str, object], error: str
) -> None:
    conflict = _reconciliation_with_name_conflict().exchange_comparisons[0].field_conflicts[0]

    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _replace_with_unvalidated_values(conflict, changes)


def test_source_conflict_dto_rejects_equal_field_values() -> None:
    conflict = _reconciliation_with_name_conflict().exchange_comparisons[0].field_conflicts[0]

    with pytest.raises(InstrumentCatalogReconciliationError, match="conflict_values_must_differ"):
        replace(conflict, left_value=conflict.right_value)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"difference_status": "complete"}, "difference_status_invalid"),
        ({"left_observed_identities": (object(),)}, "left_observed_identities_invalid"),
        (
            {
                "left_observed_identities": (
                    InstrumentIdentity("SH", "600002"),
                    InstrumentIdentity("SH", "600001"),
                )
            },
            "left_observed_identities_not_unique_sorted",
        ),
        ({"unknown_reasons": (1,)}, "unknown_reasons_invalid"),
        (
            {"difference_status": CatalogSetDifferenceStatus.UNKNOWN},
            "unknown_difference_reason_required",
        ),
        ({"unknown_reasons": ("capture incomplete",)}, "observed_difference_has_unknown_reason"),
        ({"left_only_observed": ()}, "left_only_observed_inconsistent"),
        ({"right_only_observed": ()}, "right_only_observed_inconsistent"),
        ({"shared_observed": ()}, "shared_observed_inconsistent"),
    ],
)
def test_identity_set_dto_rejects_invalid_status_and_partitions(
    changes: dict[str, object], error: str
) -> None:
    first = InstrumentIdentity("SH", "600001")
    second = InstrumentIdentity("SH", "600002")
    third = InstrumentIdentity("SH", "600003")
    observation = CatalogIdentitySetObservation(
        difference_status=CatalogSetDifferenceStatus.OBSERVED,
        left_observed_identities=(first, second),
        right_observed_identities=(first, third),
        left_only_observed=(second,),
        right_only_observed=(third,),
        shared_observed=(first,),
        unknown_reasons=(),
    )

    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _replace_with_unvalidated_values(observation, changes)


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"exchange": "XX"}, "comparison_exchange_invalid"),
        ({"field_conflict_status": "not-reviewed"}, "field_conflict_status_invalid"),
        ({"field_conflict_total_pairs": -1}, "field_conflict_pair_counts_invalid"),
        ({"field_conflict_pairs_compared": 3}, "field_conflict_pair_counts_invalid"),
        (
            {"field_conflict_status": CatalogFieldConflictStatus.TRUNCATED},
            "field_conflict_status_count_mismatch",
        ),
        ({"field_conflicts": (object(),)}, "field_conflicts_invalid"),
        (
            {"field_conflicts": (object(), object())},
            "field_conflicts_invalid",
        ),
        ({"identity_sets": object()}, "identity_sets_invalid"),
    ],
)
def test_exchange_comparison_dto_rejects_invalid_counts_and_payloads(
    changes: dict[str, object], error: str
) -> None:
    comparison = _reconciliation_with_name_conflict().exchange_comparisons[0]

    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _replace_with_unvalidated_values(comparison, changes)


def test_reconciliation_repr_and_completeness_claim_are_fail_closed() -> None:
    result = _reconciliation_with_name_conflict()

    assert "values_redacted=True" in repr(result)
    assert "左侧名称" not in repr(result)
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="market_completeness_must_remain_unknown"
    ):
        replace(result, market_completeness="complete")


def test_candidate_and_projection_reprs_redact_source_values() -> None:
    projection = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "PRIVATE_SOURCE_NAME"}]),
        field_bindings=_SSE_FIELDS,
    )

    assert "PRIVATE_SOURCE_NAME" not in repr(projection.candidates[0])
    assert "PRIVATE_SOURCE_NAME" not in repr(projection)
    assert "values_redacted=True" in repr(projection)


def test_name_comparison_does_not_invent_conflicts_for_unobserved_values() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001"}]), field_bindings=_SSE_FIELDS
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name=None)]),
        declared_asset_type="a-share",
    )

    comparison = reconcile_instrument_candidates(left, right).exchange_comparisons[0]

    assert comparison.identity_sets.shared_observed == (InstrumentIdentity("SH", "600001"),)
    assert comparison.field_conflicts == ()
    assert comparison.field_conflict_status is CatalogFieldConflictStatus.COMPLETE
    assert comparison.field_conflict_total_pairs == 1


def test_name_conflicts_compare_nested_json_values_without_coercion() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": {"segments": ["A", 1]}}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name={"segments": ["A", True]})]),
        declared_asset_type="a-share",
    )

    comparison = reconcile_instrument_candidates(left, right).exchange_comparisons[0]

    assert len(comparison.field_conflicts) == 1
    conflict = comparison.field_conflicts[0]
    assert conflict.field_name == "display_name"
    assert isinstance(conflict.left_value, Mapping)
    assert isinstance(conflict.right_value, Mapping)
    left_value = cast("Mapping[str, Any]", conflict.left_value)
    right_value = cast("Mapping[str, Any]", conflict.right_value)
    left_segments = left_value["segments"]
    right_segments = right_value["segments"]
    assert isinstance(left_segments, tuple)
    assert isinstance(right_segments, tuple)
    assert left_segments[0] == right_segments[0] == "A"
    assert type(left_segments[1]) is int
    assert left_segments[1] == 1
    assert type(right_segments[1]) is bool
    assert right_segments[1] is True


def test_reconciliation_revalidates_candidate_evidence_at_boundary() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name="甲公司")]),
        declared_asset_type="a-share",
    )
    object.__setattr__(left.candidates[0], "identity_candidate", InstrumentIdentity("SH", "600002"))

    with pytest.raises(InstrumentCatalogReconciliationError, match="identity_source_code_mismatch"):
        reconcile_instrument_candidates(left, right)


def test_reconciliation_boundary_rejects_tampered_candidate_evidence() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司", "EXCHANGE": "SH"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name="甲公司")]),
        declared_asset_type="a-share",
    )
    base = left.candidates[0]

    def assert_rejected(changes: dict[str, object], error: str) -> None:
        candidate = replace(base)
        projection = replace(left, candidates=(candidate,))
        for name, value in changes.items():
            object.__setattr__(candidate, name, value)
        with pytest.raises(InstrumentCatalogReconciliationError, match=error):
            reconcile_instrument_candidates(projection, right)

    assert_rejected({"source_code": object()}, "source_code_observation_invalid")
    assert_rejected({"fields": object()}, "candidate_fields_invalid")
    assert_rejected({"fields": {"bad field": base.source_code}}, "candidate_field_key_invalid")
    assert_rejected({"raw_row": object()}, "raw_row_invalid")
    assert_rejected({"disposition": "unknown"}, "candidate_context_invalid")
    assert_rejected({"membership_context": "unknown"}, "candidate_context_invalid")
    assert_rejected(
        {"fields": {**base.fields, "display_name": "not an observation"}},
        "candidate_field_value_invalid",
    )
    assert_rejected(
        {"raw_row": {**base.raw_row, "A_STOCK_CODE": "600002"}},
        "source_code_raw_row_mismatch",
    )
    assert_rejected(
        {
            "fields": {
                **base.fields,
                "display_name": CandidateFieldObservation(
                    "SEC_NAME_CN", FieldObservationState.VALUE, "另一家公司"
                ),
            }
        },
        "field_observation_raw_row_mismatch",
    )
    malformed_absent = CandidateFieldObservation("MISSING_FIELD", FieldObservationState.ABSENT)
    object.__setattr__(malformed_absent, "source_field", None)
    assert_rejected(
        {"fields": {**base.fields, "display_name": malformed_absent}},
        "field_observation_raw_row_mismatch",
    )
    assert_rejected(
        {
            "raw_row": {**base.raw_row, "ALT_CODE": "600001"},
            "fields": {
                **base.fields,
                "code": CandidateFieldObservation(
                    "ALT_CODE", FieldObservationState.VALUE, "600001"
                ),
            },
        },
        "source_code_field_observation_mismatch",
    )
    assert_rejected({"identity_candidate": None}, "candidate_identity_required")
    assert_rejected(
        {
            "disposition": CatalogIdentityDisposition.UNRESOLVED,
            "identity_candidate": None,
            "unresolved_reason": None,
        },
        "unresolved_reason_required",
    )
    assert_rejected({"identity_candidate": object()}, "identity_candidate_invalid")
    assert_rejected(
        {"disposition": CatalogIdentityDisposition.UNRESOLVED}, "noncandidate_has_identity"
    )
    assert_rejected({"membership_context": "current_query"}, "candidate_context_invalid")
    null_code = CandidateFieldObservation("A_STOCK_CODE", FieldObservationState.NULL)
    assert_rejected(
        {
            "source_code": null_code,
            "raw_row": {**base.raw_row, "A_STOCK_CODE": None},
            "fields": {**base.fields, "code": null_code},
        },
        "identity_source_code_not_value",
    )
    changed_code = CandidateFieldObservation("A_STOCK_CODE", FieldObservationState.VALUE, "600002")
    assert_rejected(
        {
            "source_code": changed_code,
            "raw_row": {**base.raw_row, "A_STOCK_CODE": "600002"},
            "fields": {**base.fields, "code": changed_code},
        },
        "identity_source_code_mismatch",
    )
    assert_rejected({"exchange_candidate": "SZ"}, "identity_exchange_mismatch")
    assert_rejected(
        {
            "raw_row": {**base.raw_row, "EXCHANGE": "SZ"},
            "fields": {
                **base.fields,
                "source_exchange": CandidateFieldObservation(
                    "EXCHANGE", FieldObservationState.VALUE, "SZ"
                ),
            },
        },
        "identity_source_exchange_mismatch",
    )
    assert_rejected({"selector_context": None}, "sse_selector_context_invalid")
    assert_rejected({"selector_context": "unrecognized-selector"}, "sse_selector_context_invalid")


def test_reconciliation_boundary_rechecks_projection_rows_and_source_binding() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    )
    object.__setattr__(left, "candidates", ("corrupt row",))

    with pytest.raises(InstrumentCatalogReconciliationError, match="candidates_invalid"):
        reconcile_instrument_candidates(left, right)

    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    object.__setattr__(left.candidates[0], "source_id", "vendor.instrument")
    with pytest.raises(InstrumentCatalogReconciliationError, match="candidate_projection_mismatch"):
        reconcile_instrument_candidates(left, right)


def test_fuyao_projection_preserves_null_date_and_raw_status_as_observations() -> None:
    capture = _fuyao_capture([_fuyao_row("600519.SH", list_date=None, status="suspended")])

    projection = project_fuyao_capture(
        capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH", "SZ", "BJ"),
    )

    candidate = projection.candidates[0]
    assert candidate.identity_candidate is not None
    assert candidate.identity_candidate.exchange == "SH"
    assert candidate.identity_candidate.symbol == "600519"
    assert candidate.fields["listing_date"].state is FieldObservationState.NULL
    assert candidate.fields["source_status"].raw_value == "suspended"
    assert candidate.membership_context is CandidateMembershipContext.CURRENT_QUERY
    assert projection.pagination_status == "unknown"
    assert projection.source_authenticity == "unknown"
    assert projection.market_completeness == "unknown"


def test_fuyao_projection_requires_row_asset_type_and_qualified_code() -> None:
    capture = _fuyao_capture(
        [
            _fuyao_row("600001.SH", asset_type="a-share-index"),
            _fuyao_row("600002", asset_type="a-share"),
        ]
    )

    candidates = project_fuyao_capture(
        capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH", "SZ", "BJ"),
    ).candidates

    assert [row.disposition for row in candidates] == [
        CatalogIdentityDisposition.UNRESOLVED,
        CatalogIdentityDisposition.UNRESOLVED,
    ]
    assert [row.identity_candidate for row in candidates] == [None, None]
    assert candidates[0].unresolved_reason == "row_asset_type_not_verified"
    assert candidates[1].unresolved_reason == "qualified_code_unavailable_or_invalid"


def test_fuyao_exchange_conflict_does_not_emit_candidate_identity() -> None:
    capture = _fuyao_capture([_fuyao_row("600001.SH", exchange="SZ")])

    candidate = project_fuyao_capture(
        capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH", "SZ", "BJ"),
    ).candidates[0]

    assert candidate.disposition is CatalogIdentityDisposition.CONFLICT
    assert candidate.identity_candidate is None
    assert candidate.unresolved_reason == "exchange_field_disagrees_with_qualified_code"
    assert candidate.fields["source_exchange"].raw_value == "SZ"


def test_fuyao_projection_rejects_non_a_share_scope_and_bad_bindings() -> None:
    capture = _fuyao_capture([_fuyao_row("600001.SH")])

    with pytest.raises(
        InstrumentCatalogReconciliationError, match="fuyao_asset_type_scope_invalid"
    ):
        project_fuyao_capture(capture, declared_asset_type="a-share-index")
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="field_bindings_must_be_distinct"
    ):
        FuyaoInstrumentFieldBindings(name_field="thscode")
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="field_bindings_must_be_distinct"
    ):
        CatalogFieldBindings(code_field="A_STOCK_CODE", name_field="A_STOCK_CODE")


def test_fuyao_projector_uses_default_bindings_and_rejects_invalid_field_labels() -> None:
    projection = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]),
        declared_asset_type="a-share",
    )

    assert projection.candidates[0].identity_candidate == InstrumentIdentity("SH", "600001")
    with pytest.raises(InstrumentCatalogReconciliationError, match="fuyao_field_invalid"):
        FuyaoInstrumentFieldBindings(code_field="thscode.nested")


def test_public_entry_points_reject_invalid_capture_binding_and_projection_types() -> None:
    sse_capture = _sse_capture([{"A_STOCK_CODE": "600001"}])
    fuyao_capture = _fuyao_capture([_fuyao_row("600001.SH")])

    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_capture_invalid"):
        _invoke_with_unvalidated_input(project_sse_capture, None, field_bindings=_SSE_FIELDS)
    with pytest.raises(InstrumentCatalogReconciliationError, match="sse_field_bindings_required"):
        _invoke_with_unvalidated_input(project_sse_capture, sse_capture, field_bindings=object())
    with pytest.raises(InstrumentCatalogReconciliationError, match="fuyao_capture_invalid"):
        _invoke_with_unvalidated_input(project_fuyao_capture, None, declared_asset_type="a-share")
    with pytest.raises(InstrumentCatalogReconciliationError, match="fuyao_field_bindings_invalid"):
        _invoke_with_unvalidated_input(
            project_fuyao_capture,
            fuyao_capture,
            declared_asset_type="a-share",
            field_bindings=object(),
        )
    with pytest.raises(InstrumentCatalogReconciliationError, match="projections_required"):
        _invoke_with_unvalidated_input(
            reconcile_instrument_candidates,
            None,
            project_fuyao_capture(fuyao_capture, declared_asset_type="a-share"),
        )


def test_fuyao_partial_and_failed_pages_never_claim_successful_acquisition() -> None:
    success_body = json.dumps(
        {"code": 0, "data": {"item": [_fuyao_row("600001.SH")]}},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    request_summary = {
        "method": "GET",
        "path": "/api/meta/tickers/list",
        "params": {"asset_type": "a-share", "limit": 10000, "offset": 0},
    }
    success_page = FuyaoInstrumentPageInput(
        sequence_number=1,
        raw_response_body=success_body,
        declared_response_sha256=hashlib.sha256(success_body).hexdigest(),
        request_summary=request_summary,
        source_page_number=0,
        http_status_code=200,
    )
    failed_page = FuyaoInstrumentPageInput(
        sequence_number=2,
        raw_response_body=None,
        declared_response_sha256=None,
        request_summary={
            **request_summary,
            "params": {"asset_type": "a-share", "limit": 10000, "offset": 10000},
        },
        source_page_number=1,
        http_status_code=503,
        attempt_status="failed",
        failure_code="http_503",
    )
    failed_page_first = replace(failed_page, sequence_number=1, source_page_number=0)
    mixed_capture = build_fuyao_instrument_census(
        [success_page, failed_page], observed_at=_OBSERVED_AT
    )
    failed_only_capture = build_fuyao_instrument_census(
        [failed_page_first], observed_at=_OBSERVED_AT
    )

    partial = project_fuyao_capture(mixed_capture, declared_asset_type="a-share")
    failed = project_fuyao_capture(failed_only_capture, declared_asset_type="a-share")

    assert partial.acquisition_status == "partial"
    assert len(partial.candidates) == 1
    assert partial.market_completeness == "unknown"
    assert failed.acquisition_status == "failed"
    assert failed.candidates == ()
    assert failed.market_completeness == "unknown"
    sse = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    difference = reconcile_instrument_candidates(partial, sse).exchange_comparisons[0].identity_sets
    assert difference.difference_status is CatalogSetDifferenceStatus.UNKNOWN
    assert "left_acquisition_not_succeeded" in difference.unknown_reasons


def test_fuyao_projector_rechecks_successful_page_digest_at_projection_boundary() -> None:
    capture = _fuyao_capture([_fuyao_row("600001.SH")])
    object.__setattr__(capture.source_census.pages[0], "receipt", None)
    object.__setattr__(capture, "response_body_sha256s", (None,))

    with pytest.raises(
        InstrumentCatalogReconciliationError,
        match="successful_page_response_hash_missing",
    ):
        project_fuyao_capture(capture, declared_asset_type="a-share")


def test_generic_bare_source_code_requires_a_row_exchange_observation() -> None:
    candidate = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    ).candidates[0]
    raw_row = {**candidate.raw_row, "thscode": "600001"}
    fields = dict(candidate.fields)
    fields.pop("source_exchange")

    with pytest.raises(
        InstrumentCatalogReconciliationError, match="bare_code_exchange_evidence_required"
    ):
        replace(
            candidate,
            source_id="vendor.instrument",
            source_code=CandidateFieldObservation("thscode", FieldObservationState.VALUE, "600001"),
            raw_row=raw_row,
            fields=fields,
        )


def test_source_page_number_reader_accepts_only_explicit_scalar_page_numbers() -> None:
    assert catalog_reconciliation._source_page_number(None) is None
    assert catalog_reconciliation._source_page_number({"params": "not-an-object"}) is None
    assert catalog_reconciliation._source_page_number({"params": {"pageHelp.pageNo": True}}) is None
    assert catalog_reconciliation._source_page_number({"pageHelp.pageNo": 3}) == 3
    assert catalog_reconciliation._source_page_number({"params": {"pageHelp.pageNo": "03"}}) == "03"


@pytest.mark.parametrize(
    ("scope", "allow_empty", "error"),
    [
        ("SH", False, "declared_exchange_scope_invalid"),
        (None, False, "declared_exchange_scope_invalid"),
        ((), False, "declared_exchange_scope_invalid"),
        (("NYSE",), False, "declared_exchange_scope_invalid"),
        (("SH", "SH"), False, "declared_exchange_scope_duplicate"),
        (("SH", "SZ", "BJ", "OTC"), False, "declared_exchange_scope_invalid"),
    ],
)
def test_exchange_scope_parser_rejects_scalar_empty_duplicate_and_invalid_markets(
    scope: object, allow_empty: bool, error: str
) -> None:
    with pytest.raises(InstrumentCatalogReconciliationError, match=error):
        _invoke_with_unvalidated_input(
            catalog_reconciliation._exchange_tuple,
            scope,
            "declared_exchange_scope",
            allow_empty=allow_empty,
        )


def test_bounded_tuple_helpers_fail_closed_for_broken_and_oversized_iterables() -> None:
    def broken_iterable() -> Any:
        yield "one"
        raise RuntimeError("iterator failure")

    assert catalog_reconciliation._exchange_tuple(("BJ", "SH"), "declared_exchange_scope") == (
        "BJ",
        "SH",
    )
    assert catalog_reconciliation._exchange_tuple((), "current_query_scope", allow_empty=True) == ()
    with pytest.raises(
        InstrumentCatalogReconciliationError, match="declared_exchange_scope_invalid"
    ):
        catalog_reconciliation._exchange_tuple(broken_iterable(), "declared_exchange_scope")
    with pytest.raises(InstrumentCatalogReconciliationError, match="contexts_seen_invalid"):
        catalog_reconciliation._bounded_tuple("scalar", "contexts_seen", 1)
    with pytest.raises(InstrumentCatalogReconciliationError, match="contexts_seen_invalid"):
        catalog_reconciliation._bounded_tuple(broken_iterable(), "contexts_seen", 1)
    with pytest.raises(InstrumentCatalogReconciliationError, match="contexts_seen_too_large"):
        catalog_reconciliation._bounded_tuple(("one", "two"), "contexts_seen", 1)


def test_source_row_freezer_rejects_malformed_and_oversized_inputs() -> None:
    large_byte_budget = ["x" * 16_384] * 65
    large_key_budget = [
        {f"{group:02d}_{index:0125d}": None for index in range(256)} for group in range(33)
    ]
    too_many_nodes = [[None] * 4_096 for _ in range(5)]
    too_deep: object = None
    for _ in range(13):
        too_deep = [too_deep]
    oversized_object = {f"field_{index}": None for index in range(257)}
    cases = [
        (float("inf"), "raw_row_number_not_finite"),
        (1 << 4_097, "raw_row_integer_too_large"),
        ("x" * 16_385, "raw_row_text_too_large"),
        (large_byte_budget, "raw_row_value_too_large"),
        (large_key_budget, "raw_row_value_too_large"),
        (oversized_object, "raw_row_object_too_large"),
        ({1: "invalid key"}, "raw_row_field_name_invalid"),
        ({"k" * 129: None}, "raw_row_field_name_invalid"),
        (too_many_nodes, "raw_row_value_too_large"),
        (too_deep, "raw_row_value_too_large"),
        ([None] * 4_097, "raw_row_array_too_large"),
        (object(), "raw_row_value_not_json"),
    ]

    for value, error in cases:
        with pytest.raises(InstrumentCatalogReconciliationError, match=error):
            catalog_reconciliation._freeze_json_value(value)

    assert catalog_reconciliation._freeze_json_value(True) is True
    assert catalog_reconciliation._freeze_json_value(1) == 1
    assert catalog_reconciliation._freeze_json_value(1.5) == 1.5
    with pytest.raises(InstrumentCatalogReconciliationError, match="raw_row_invalid"):
        _invoke_with_unvalidated_input(catalog_reconciliation._freeze_raw_row, None)
    with pytest.raises(InstrumentCatalogReconciliationError, match="raw_row_invalid"):
        catalog_reconciliation._freeze_raw_row({})
    with pytest.raises(InstrumentCatalogReconciliationError, match="raw_row_invalid"):
        catalog_reconciliation._freeze_raw_row(oversized_object)


def test_json_equality_preserves_json_types_and_nested_structure() -> None:
    freeze = catalog_reconciliation._freeze_json_value
    assert catalog_reconciliation._same_json(
        freeze({"items": [1, True]}), freeze({"items": [1, True]})
    )
    assert not catalog_reconciliation._same_json(freeze({"items": [1]}), freeze({"items": [2]}))
    assert not catalog_reconciliation._same_json(freeze({"a": 1, "b": 2}), freeze({"a": 2, "b": 1}))
    assert not catalog_reconciliation._same_json(True, 1)


def test_unresolved_rows_are_excluded_from_identity_grouping_and_duplicate_counts() -> None:
    unresolved = project_sse_capture(
        _sse_capture([{"SEC_NAME_CN": "没有证券代码"}]),
        field_bindings=_SSE_FIELDS,
    ).candidates[0]

    assert unresolved.identity_candidate is None
    assert catalog_reconciliation._candidates_by_identity((unresolved,)) == {}
    assert catalog_reconciliation._duplicates((unresolved,)) == ()


def test_fuyao_identity_outside_requested_exchange_scope_stays_a_conflict() -> None:
    capture = _fuyao_capture([_fuyao_row("000001.SZ")])

    candidate = project_fuyao_capture(
        capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH",),
    ).candidates[0]

    assert candidate.disposition is CatalogIdentityDisposition.CONFLICT
    assert candidate.identity_candidate is None
    assert candidate.unresolved_reason == "code_exchange_outside_declared_scope"


def test_sse_empty_and_unmapped_source_fields_preserve_context_without_guessing() -> None:
    empty_capture = build_sse_instrument_census([], observed_at=_OBSERVED_AT)
    empty_projection = project_sse_capture(empty_capture, field_bindings=_SSE_FIELDS)
    unmapped_projection = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001"}]),
        field_bindings=CatalogFieldBindings(code_field="A_STOCK_CODE", name_field=None),
    )

    assert empty_projection.acquisition_status == "failed"
    assert empty_projection.current_query_scope == ()
    assert empty_projection.candidates == ()
    assert (
        unmapped_projection.candidates[0].fields["display_name"].state
        is FieldObservationState.UNMAPPED
    )


def test_status3_sse_candidates_are_not_compared_as_current_exchange_rows() -> None:
    status3 = project_sse_capture(
        _sse_capture(
            [{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "退市候选"}],
            selector=SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE,
        ),
        field_bindings=_SSE_FIELDS,
    )
    fuyao = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH")]), declared_asset_type="a-share"
    )

    result = reconcile_instrument_candidates(status3, fuyao)

    assert result.exchange_comparisons == ()
    assert result.not_compared_exchanges == ("BJ", "SH", "SZ")
    assert result.market_completeness == "unknown"


def test_candidate_builder_enforces_asset_type_when_the_binding_is_selected() -> None:
    base = {
        "source_id": "vendor.instrument",
        "observed_at": _OBSERVED_AT,
        "page_sequence": 1,
        "source_page_number": 1,
        "source_row_number": 1,
        "selector_context": "vendor_catalog",
        "membership_context": CandidateMembershipContext.CURRENT_QUERY,
        "request_summary_sha256": None,
        "response_sha256": "0" * 64,
        "exchange_candidate": "SH",
        "identity_code_field": "code",
        "exchange_field": "source_exchange",
        "required_asset_type": "equity",
        "source_exchange_expected": "SH",
    }
    bindings = {"code": "ticker", "source_exchange": "exchange", "asset_type": "asset_type"}
    rows = [
        ({"ticker": "600001", "exchange": "SH", "asset_type": "equity"}, True),
        ({"ticker": "600001", "exchange": "SH", "asset_type": "bond"}, False),
        ({"ticker": "600001", "exchange": "SH", "asset_type": None}, False),
        ({"ticker": "600001", "exchange": "SH"}, False),
    ]
    for row, is_candidate in rows:
        candidate = _invoke_with_unvalidated_input(
            catalog_reconciliation._build_candidate,
            **base,
            raw_row=row,
            bindings=bindings,
        )
        assert (candidate.identity_candidate is not None) is is_candidate
        if not is_candidate:
            assert candidate.unresolved_reason == "row_asset_type_not_verified"

    unbound = _invoke_with_unvalidated_input(
        catalog_reconciliation._build_candidate,
        **base,
        raw_row={"ticker": "600001", "exchange": "SH"},
        bindings={"code": "ticker", "source_exchange": "exchange", "asset_type": None},
    )
    assert unbound.identity_candidate is None
    assert unbound.fields["asset_type"].state is FieldObservationState.UNMAPPED


def test_reconciliation_compares_only_shared_scope_and_reports_set_and_name_differences() -> None:
    sse_capture = _sse_capture(
        [
            {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "SSE名字"},
            {"A_STOCK_CODE": "600002", "SEC_NAME_CN": "SSE独有"},
        ]
    )
    fuyao_capture = _fuyao_capture(
        [
            _fuyao_row("600001.SH", name="Fuyao名字"),
            _fuyao_row("600003.SH", name="Fuyao独有"),
            _fuyao_row("000001.SZ", name="深圳"),
            _fuyao_row("430001.BJ", name="北交所"),
        ]
    )
    left = project_sse_capture(sse_capture, field_bindings=_SSE_FIELDS)
    right = project_fuyao_capture(
        fuyao_capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH", "SZ", "BJ"),
    )

    result = reconcile_instrument_candidates(left, right)

    assert len(result.exchange_comparisons) == 1
    assert result.exchange_comparisons[0].exchange == "SH"
    comparison = result.exchange_comparisons[0].identity_sets
    assert comparison.difference_status is CatalogSetDifferenceStatus.UNKNOWN
    assert [row.symbol for row in comparison.left_only_observed] == ["600002"]
    assert [row.symbol for row in comparison.right_only_observed] == ["600003"]
    assert [row.symbol for row in comparison.shared_observed] == ["600001"]
    assert "left_pagination_not_chain_consistent" in comparison.unknown_reasons
    assert "right_pagination_not_chain_consistent" in comparison.unknown_reasons
    assert result.not_compared_exchanges == ("BJ", "SZ")
    conflict = result.exchange_comparisons[0].field_conflicts[0]
    assert conflict.identity.symbol == "600001"
    assert conflict.field_name == "display_name"
    assert conflict.left_value == "SSE名字"
    assert conflict.right_value == "Fuyao名字"
    assert result.market_completeness == "unknown"


def test_failed_or_incomplete_capture_keeps_set_differences_unknown() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600002.SH", name="乙公司")]),
        declared_asset_type="a-share",
    )
    failed_left = replace(left, acquisition_status="failed", pagination_status="chain_consistent")
    complete_chain_right = replace(right, pagination_status="chain_consistent")

    observation = (
        reconcile_instrument_candidates(failed_left, complete_chain_right)
        .exchange_comparisons[0]
        .identity_sets
    )

    assert observation.difference_status is CatalogSetDifferenceStatus.UNKNOWN
    assert [row.symbol for row in observation.left_only_observed] == ["600001"]
    assert [row.symbol for row in observation.right_only_observed] == ["600002"]
    assert "left_acquisition_not_succeeded" in observation.unknown_reasons


def test_successful_consistent_capture_marks_set_difference_as_observed_only() -> None:
    left = project_sse_capture(
        _sse_capture([{"A_STOCK_CODE": "600001", "SEC_NAME_CN": "甲公司"}]),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600002.SH", name="乙公司")]),
        declared_asset_type="a-share",
    )
    left = replace(left, pagination_status="chain_consistent")
    right = replace(right, pagination_status="chain_consistent")

    observation = reconcile_instrument_candidates(left, right).exchange_comparisons[0].identity_sets

    assert observation.difference_status is CatalogSetDifferenceStatus.OBSERVED
    assert observation.left_only_observed == (InstrumentIdentity("SH", "600001"),)
    assert observation.right_only_observed == (InstrumentIdentity("SH", "600002"),)
    assert observation.unknown_reasons == ()


def test_reconciliation_reports_duplicate_source_identities_without_deduplicating_rows() -> None:
    sse_capture = _sse_capture(
        [
            {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "同名"},
            {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "同名"},
        ]
    )
    fuyao_capture = _fuyao_capture([_fuyao_row("600001.SH", name="同名")])
    left = project_sse_capture(sse_capture, field_bindings=_SSE_FIELDS)
    right = project_fuyao_capture(
        fuyao_capture,
        declared_asset_type="a-share",
        declared_exchange_scope=("SH", "SZ", "BJ"),
    )

    result = reconcile_instrument_candidates(left, right)

    assert len(left.candidates) == 2
    assert result.exchange_comparisons[0].left_duplicate_identities[0].symbol == "600001"
    assert (
        result.exchange_comparisons[0].field_conflict_status is CatalogFieldConflictStatus.COMPLETE
    )
    assert result.exchange_comparisons[0].field_conflict_total_pairs == 2
    assert result.exchange_comparisons[0].field_conflict_pairs_compared == 2


def test_duplicate_identity_field_conflicts_compare_every_row_pair_and_mark_ambiguity() -> None:
    left = project_sse_capture(
        _sse_capture(
            [
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称一"},
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称二"},
            ]
        ),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture([_fuyao_row("600001.SH", name="右侧名称")]),
        declared_asset_type="a-share",
    )

    conflicts = reconcile_instrument_candidates(left, right).exchange_comparisons[0].field_conflicts

    assert len(conflicts) == 2
    assert {row.left_source_row_number for row in conflicts} == {1, 2}
    assert all(row.ambiguous_identity for row in conflicts)
    assert all(row.left_identity_row_count == 2 for row in conflicts)
    assert all(row.right_identity_row_count == 1 for row in conflicts)


@pytest.mark.parametrize(
    ("pair_budget", "conflict_budget", "expected_pairs", "expected_conflicts"),
    [(2, 100, 2, 2), (100, 1, 1, 1)],
)
def test_field_conflict_review_truncates_with_global_work_and_output_limits(
    monkeypatch: pytest.MonkeyPatch,
    pair_budget: int,
    conflict_budget: int,
    expected_pairs: int,
    expected_conflicts: int,
) -> None:
    monkeypatch.setattr(catalog_reconciliation, "_MAX_FIELD_ROW_PAIR_COMPARISONS", pair_budget)
    monkeypatch.setattr(catalog_reconciliation, "_MAX_FIELD_CONFLICT_OUTPUT", conflict_budget)
    left = project_sse_capture(
        _sse_capture(
            [
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称一"},
                {"A_STOCK_CODE": "600001", "SEC_NAME_CN": "左侧名称二"},
            ]
        ),
        field_bindings=_SSE_FIELDS,
    )
    right = project_fuyao_capture(
        _fuyao_capture(
            [
                _fuyao_row("600001.SH", name="右侧名称一"),
                _fuyao_row("600001.SH", name="右侧名称二"),
            ]
        ),
        declared_asset_type="a-share",
    )

    comparison = reconcile_instrument_candidates(left, right).exchange_comparisons[0]

    assert comparison.field_conflict_status is CatalogFieldConflictStatus.TRUNCATED
    assert comparison.field_conflict_total_pairs == 4
    assert comparison.field_conflict_pairs_compared == expected_pairs
    assert len(comparison.field_conflicts) == expected_conflicts


def test_field_pair_budget_is_shared_across_exchange_comparisons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog_reconciliation, "_MAX_FIELD_ROW_PAIR_COMPARISONS", 1)
    monkeypatch.setattr(catalog_reconciliation, "_MAX_FIELD_CONFLICT_OUTPUT", 10)
    left_source = project_fuyao_capture(
        _fuyao_capture(
            [
                _fuyao_row("600001.SH", name="左侧沪市名称"),
                _fuyao_row("000001.SZ", name="左侧深市名称"),
            ]
        ),
        declared_asset_type="a-share",
    )
    left = replace(
        left_source,
        source_id="vendor.instrument",
        candidates=tuple(
            replace(candidate, source_id="vendor.instrument")
            for candidate in left_source.candidates
        ),
    )
    right = project_fuyao_capture(
        _fuyao_capture(
            [
                _fuyao_row("600001.SH", name="右侧沪市名称"),
                _fuyao_row("000001.SZ", name="右侧深市名称"),
            ]
        ),
        declared_asset_type="a-share",
    )

    comparisons = reconcile_instrument_candidates(left, right).exchange_comparisons
    by_exchange = {comparison.exchange: comparison for comparison in comparisons}

    assert by_exchange["SH"].field_conflict_pairs_compared == 1
    assert by_exchange["SH"].field_conflict_status is CatalogFieldConflictStatus.COMPLETE
    assert by_exchange["SZ"].field_conflict_pairs_compared == 0
    assert by_exchange["SZ"].field_conflict_status is CatalogFieldConflictStatus.TRUNCATED


def test_reconciliation_requires_distinct_sources_and_never_marks_scope_complete() -> None:
    capture = _sse_capture([{"A_STOCK_CODE": "600001"}])
    projection = project_sse_capture(capture, field_bindings=_SSE_FIELDS)

    with pytest.raises(InstrumentCatalogReconciliationError, match="distinct_sources_required"):
        reconcile_instrument_candidates(projection, projection)
