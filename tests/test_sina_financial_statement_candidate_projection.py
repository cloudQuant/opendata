"""Recorded-response tests for the partial Sina FZB mapping candidate seam."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from opendata.data.providers.sina.financial_statement_candidate_contract import (
    MAX_SINA_FZB_REPORTS,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    SINA_FZB_CANDIDATE_EVIDENCE_REFS,
    SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT,
    SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS,
    SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
    ProposedSinaFzbItemKey,
    SinaFzbCandidateError,
    SinaFzbCandidateItem,
    SinaFzbCandidateProjection,
    SinaFzbCandidateReport,
)
from opendata.data.providers.sina.financial_statement_candidate_projection import (
    project_sina_fzb_candidates,
)
from opendata.data.providers.sina.financial_statement_contract import (
    SinaFzbEntry,
    SinaFzbFetchedSnapshot,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
)
from opendata.data.providers.sina.financial_statement_parser import parse_sina_fzb_response

_CAPTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
_CAPTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
_QUERY = SinaFzbQuery(symbol="600519.SH")
_RETRIEVED_AT = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
_PILOT_PERIOD = date(2024, 12, 31)
_PILOT_GOLDENS = (
    (
        "cash",
        "CURFDS",
        "货币资金",
        "59295822956.890000",
        (1,),
        (
            ("item_display", "小类"),
            ("item_display_type", 2),
            ("item_group_no", 1),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", -0.14151),
        ),
    ),
    (
        "accounts_receivable",
        "ACCORECE",
        "应收账款",
        "18974192.750000",
        (9,),
        (
            ("item_display", "缩进"),
            ("item_display_type", 3),
            ("item_group_no", 1),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", -0.68572),
        ),
    ),
    (
        "total_current_assets",
        "TOTCURRASSET",
        "流动资产合计",
        "251726674636.660000",
        (29,),
        (
            ("item_display", "中类"),
            ("item_display_type", 6),
            ("item_group_no", 1),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", 0.11793),
        ),
    ),
    (
        "non_current_nets_total",
        "TOTALNONCASSETS",
        "非流动资产合计",
        "47217905282.040000",
        (65,),
        (
            ("item_display", "中类"),
            ("item_display_type", 6),
            ("item_group_no", 2),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", -0.00651),
        ),
    ),
    (
        "assets_total",
        "TOTASSET",
        "资产总计",
        "298944579918.700000",
        (66,),
        (
            ("item_display", "中类"),
            ("item_display_type", 6),
            ("item_group_no", 2),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", 0.09624),
        ),
    ),
    (
        "total_debt",
        "TOTLIAB",
        "负债合计",
        "56933264798.100000",
        (121,),
        (
            ("item_display", "中类"),
            ("item_display_type", 6),
            ("item_group_no", 4),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", 0.16088),
        ),
    ),
    (
        "holder_equity_total",
        "RIGHAGGR",
        "所有者权益(或股东权益)合计",
        "242011315120.600000",
        (139,),
        (
            ("item_display", "中类"),
            ("item_display_type", 6),
            ("item_group_no", 5),
            ("item_precision", "f2"),
            ("item_source", "fzb"),
            ("item_tongbi", 0.08207),
        ),
    ),
)
_EXPECTED_PAIRS = tuple((source_id, title) for _, source_id, title, *_ in _PILOT_GOLDENS)


@lru_cache(maxsize=1)
def _recorded_snapshot() -> SinaFzbFetchedSnapshot:
    """Parse the exact hash-pinned response and attach only a test retrieval time."""
    archive = _CAPTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == _CAPTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    assert call["method"] == "GET"
    assert call["url"] == (
        "https://quotes.sina.cn/cn/api/openapi.php/CompanyFinanceService.getFinanceReport2022"
    )
    assert call["params"] == {
        "paperCode": "sh600519",
        "source": "fzb",
        "type": "0",
        "page": "1",
        "num": "1000",
    }
    assert call["status"] == 200
    body = base64.b64decode(call["content_b64"], validate=True)
    reports = parse_sina_fzb_response(body, query=_QUERY)
    return SinaFzbFetchedSnapshot(
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        reports=reports,
    )


def _report_index(snapshot: SinaFzbFetchedSnapshot, period: date) -> int:
    return next(
        index for index, report in enumerate(snapshot.reports) if report.report_period == period
    )


def _report_for_period(snapshot: SinaFzbFetchedSnapshot, period: date) -> SinaFzbReport:
    return snapshot.reports[_report_index(snapshot, period)]


def _item(report: SinaFzbReport, source_item_id: str) -> SinaFzbItem:
    return next(
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbItem) and entry.source_item_id == source_item_id
    )


def _entry_index(report: SinaFzbReport, entry: SinaFzbEntry) -> int:
    return report.entries.index(entry)


def _replace_report(
    snapshot: SinaFzbFetchedSnapshot,
    report_index: int,
    report: SinaFzbReport,
) -> SinaFzbFetchedSnapshot:
    reports = list(snapshot.reports)
    reports[report_index] = report
    return replace(snapshot, reports=tuple(reports))


def _replace_entry(
    snapshot: SinaFzbFetchedSnapshot,
    period: date,
    entry_index: int,
    replacement: SinaFzbEntry,
) -> SinaFzbFetchedSnapshot:
    report_index = _report_index(snapshot, period)
    report = snapshot.reports[report_index]
    entries = list(report.entries)
    entries[entry_index] = replacement
    return _replace_report(snapshot, report_index, replace(report, entries=tuple(entries)))


def _assert_candidate_error(
    snapshot: SinaFzbFetchedSnapshot,
    code: str,
    *,
    expected_query: SinaFzbQuery = _QUERY,
    secret: str | None = None,
) -> None:
    with pytest.raises(SinaFzbCandidateError) as raised:
        project_sina_fzb_candidates(snapshot, expected_query=expected_query)
    assert raised.value.code == code
    assert "600519" not in str(raised.value)
    assert "600519" not in repr(raised.value)
    if secret is not None:
        assert secret not in str(raised.value)
        assert secret not in repr(raised.value)


def _candidate_report(
    projection: SinaFzbCandidateProjection, period: date
) -> SinaFzbCandidateReport:
    return next(
        report for report in projection.reports if report.source_report.report_period == period
    )


def _candidate_items_by_key(
    report: SinaFzbCandidateReport,
) -> dict[ProposedSinaFzbItemKey, SinaFzbCandidateItem]:
    return {
        entry.proposed_item_key: entry
        for entry in report.entries
        if isinstance(entry, SinaFzbCandidateItem) and entry.proposed_item_key is not None
    }


def test_recorded_2024_report_projects_seven_exact_pairs_as_partial_candidates() -> None:
    snapshot = _recorded_snapshot()
    projection = project_sina_fzb_candidates(snapshot, expected_query=_QUERY)
    source_report = _report_for_period(snapshot, _PILOT_PERIOD)
    candidate_report = _candidate_report(projection, _PILOT_PERIOD)
    candidates = _candidate_items_by_key(candidate_report)

    assert projection.query == _QUERY
    assert projection.retrieved_at_utc == _RETRIEVED_AT
    assert projection.retrieved_at_utc.tzinfo is timezone.utc
    assert projection.mapping_namespace == SINA_FZB_CANDIDATE_MAPPING_NAMESPACE
    assert projection.status == "partial"
    assert projection.evidence_refs == SINA_FZB_CANDIDATE_EVIDENCE_REFS
    assert source_report.report_period == date(2024, 12, 31)
    assert source_report.published_on == date(2026, 4, 17)
    assert source_report.publication_precision == "date"
    assert source_report.source_update_epoch_seconds == 1743599711
    assert source_report.source_updated_at == datetime(
        2025, 4, 2, 21, 15, 11, tzinfo=ZoneInfo("Asia/Shanghai")
    )
    assert source_report.source_updated_at.tzinfo.key == "Asia/Shanghai"
    assert source_report.currency == "CNY"
    assert dict(source_report.period_metadata) == {"date_description": "2024年报", "date_type": 4}
    assert dict(source_report.report_metadata) == {
        "audit_opinion": "标准无保留审计意见",
        "data_source": "定期报告",
        "is_audit": "是",
        "is_exist_yoy": True,
        "rType": "合并期末",
    }
    assert candidate_report.source_report == source_report

    assert set(candidates) == {
        "assets_total",
        "total_current_assets",
        "non_current_nets_total",
        "cash",
        "accounts_receivable",
        "total_debt",
        "holder_equity_total",
    }
    assert len(candidates) == SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT == 7
    for proposed_key, source_id, title, raw_value, ordinals, metadata in _PILOT_GOLDENS:
        candidate = candidates[proposed_key]
        assert candidate.mapping_namespace == SINA_FZB_CANDIDATE_MAPPING_NAMESPACE
        assert candidate.disposition == "candidate"
        assert candidate.proposed_item_key == proposed_key
        assert candidate.unmapped_reason is None
        assert candidate.mapping_evidence_refs == SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS
        assert candidate.source_item.source_item_id == source_id
        assert candidate.source_item.source_title == title
        assert candidate.source_item.raw_value == raw_value
        assert candidate.source_item.value_state == "present"
        assert candidate.source_item.source_ordinals == ordinals
        assert candidate.source_item.source_metadata == metadata

    assert candidate_report.accounting.expected_mapping_count == 7
    assert candidate_report.accounting.matched_source_item_ids == (
        "CURFDS",
        "ACCORECE",
        "TOTCURRASSET",
        "TOTALNONCASSETS",
        "TOTASSET",
        "TOTLIAB",
        "RIGHAGGR",
    )
    assert candidate_report.accounting.missing_expected_source_pairs == ()
    assert candidate_report.accounting.raw_source_row_occurrence_count == 141
    assert candidate_report.accounting.unique_source_financial_item_count == 135
    assert candidate_report.accounting.unique_source_group_marker_count == 5

    source_markers = [
        entry for entry in source_report.entries if isinstance(entry, SinaFzbGroupMarker)
    ]
    projected_markers = [
        entry for entry in candidate_report.entries if isinstance(entry, SinaFzbGroupMarker)
    ]
    assert projected_markers == source_markers
    assert [entry.source_ordinals for entry in projected_markers] == [
        (0,),
        (30,),
        (67,),
        (106,),
        (122,),
    ]
    assert len(candidate_report.entries) == len(source_report.entries) == 140

    source_unmapped = [
        entry
        for entry in source_report.entries
        if isinstance(entry, SinaFzbItem)
        and (entry.source_item_id, entry.source_title) not in _EXPECTED_PAIRS
    ]
    projected_unmapped = [
        entry
        for entry in candidate_report.entries
        if isinstance(entry, SinaFzbCandidateItem) and entry.disposition == "unmapped"
    ]
    assert [entry.source_item for entry in projected_unmapped] == source_unmapped
    assert all(entry.unmapped_reason == "unknown_source_item" for entry in projected_unmapped)
    expected_unmapped_ordinals = tuple(
        sorted(ordinal for entry in source_unmapped for ordinal in entry.source_ordinals)
    )
    assert candidate_report.accounting.unmapped_source_ordinals == expected_unmapped_ordinals
    assert len(expected_unmapped_ordinals) == 129
    assert expected_unmapped_ordinals == tuple(sorted(expected_unmapped_ordinals))
    duplicate_item = next(
        entry for entry in projected_unmapped if entry.source_item.source_item_id == "DOMETICKSETT"
    )
    assert duplicate_item.source_item.source_ordinals == (96, 97)
    null_item = next(
        entry for entry in projected_unmapped if entry.source_item.source_item_id == "SETTRESEDEPO"
    )
    assert null_item.source_item.raw_value is None
    assert null_item.source_item.value_state == "null"
    assert [
        entry.source_ordinals
        if isinstance(entry, SinaFzbGroupMarker)
        else entry.source_item.source_ordinals
        for entry in candidate_report.entries
    ] == [entry.source_ordinals for entry in source_report.entries]


@pytest.mark.parametrize(
    ("raw_value", "value_state"),
    [(None, "null"), ("", "empty"), ("not a numeric value", "present")],
)
def test_exact_pair_mapping_is_independent_of_raw_value_state(
    raw_value: str | None, value_state: str
) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    original = _item(report, "TOTASSET")
    changed = replace(original, raw_value=raw_value, value_state=value_state)
    modified = _replace_entry(snapshot, _PILOT_PERIOD, _entry_index(report, original), changed)

    candidate_report = _candidate_report(
        project_sina_fzb_candidates(modified, expected_query=_QUERY), _PILOT_PERIOD
    )
    candidate = _candidate_items_by_key(candidate_report)["assets_total"]

    assert candidate.disposition == "candidate"
    assert candidate.proposed_item_key == "assets_total"
    assert candidate.source_item.raw_value == raw_value
    assert candidate.source_item.value_state == value_state


@pytest.mark.parametrize(
    ("source_item_id", "source_title", "reason", "missing_pair"),
    [
        (
            "SYNTHETIC_UNKNOWN_SOURCE_ID",
            "无人验证的来源项目",
            "unknown_source_item",
            ("TOTASSET", "资产总计"),
        ),
        ("TOTASSET", "synthetic title mismatch", "title_mismatch", ("TOTASSET", "资产总计")),
    ],
)
def test_unknown_and_title_mismatched_rows_are_preserved_as_unmapped(
    source_item_id: str,
    source_title: str,
    reason: str,
    missing_pair: tuple[str, str],
) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    original = _item(report, "TOTASSET")
    changed = replace(original, source_item_id=source_item_id, source_title=source_title)
    modified = _replace_entry(snapshot, _PILOT_PERIOD, _entry_index(report, original), changed)

    candidate_report = _candidate_report(
        project_sina_fzb_candidates(modified, expected_query=_QUERY), _PILOT_PERIOD
    )
    candidate = next(
        entry
        for entry in candidate_report.entries
        if isinstance(entry, SinaFzbCandidateItem) and entry.source_item == changed
    )

    assert candidate.disposition == "unmapped"
    assert candidate.mapping_namespace is None
    assert candidate.proposed_item_key is None
    assert candidate.unmapped_reason == reason
    assert candidate.source_item == changed
    assert candidate.mapping_evidence_refs == ()
    assert candidate_report.accounting.missing_expected_source_pairs == (missing_pair,)


def test_snapshot_query_must_match_the_callers_expected_query() -> None:
    _assert_candidate_error(
        _recorded_snapshot(),
        "query_mismatch",
        expected_query=SinaFzbQuery("000001.SZ"),
    )


def test_public_boundary_rejects_objects_outside_the_w9_dto_contract() -> None:
    with pytest.raises(SinaFzbCandidateError) as wrong_query:
        project_sina_fzb_candidates(_recorded_snapshot(), expected_query=object())
    assert wrong_query.value.code == "query_mismatch"

    with pytest.raises(SinaFzbCandidateError) as wrong_snapshot:
        project_sina_fzb_candidates(object(), expected_query=_QUERY)
    assert wrong_snapshot.value.code == "invalid_snapshot"


def test_snapshot_and_report_containers_must_remain_typed_tuples() -> None:
    snapshot = _recorded_snapshot()
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]

    _assert_candidate_error(replace(snapshot, query=object()), "invalid_snapshot")
    _assert_candidate_error(replace(snapshot, reports=list(snapshot.reports)), "invalid_snapshot")

    invalid_report_type = _replace_report(snapshot, report_index, object())
    _assert_candidate_error(invalid_report_type, "invalid_snapshot")

    invalid_entries_type = _replace_report(snapshot, report_index, replace(report, entries=[]))
    _assert_candidate_error(invalid_entries_type, "invalid_snapshot")


def test_manually_forged_exact_query_symbols_are_revalidated() -> None:
    forged_query = object.__new__(SinaFzbQuery)
    object.__setattr__(forged_query, "symbol", "600519.")
    snapshot = _recorded_snapshot()

    with pytest.raises(SinaFzbCandidateError) as invalid_expected_query:
        project_sina_fzb_candidates(snapshot, expected_query=forged_query)
    assert invalid_expected_query.value.code == "query_mismatch"

    _assert_candidate_error(replace(snapshot, query=forged_query), "invalid_snapshot")


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("symbol", "000001.SZ", "source_identity_mismatch"),
        ("symbol", None, "source_identity_mismatch"),
        ("source_paper_code", "sz000001", "source_identity_mismatch"),
        ("source_paper_code", 100001, "source_identity_mismatch"),
        ("statement_code", "lrb", "source_identity_mismatch"),
        ("statement_code", None, "source_identity_mismatch"),
        ("publication_precision", "datetime", "invalid_snapshot"),
        ("published_on", datetime(2026, 4, 17, tzinfo=timezone.utc), "invalid_snapshot"),
    ],
)
def test_report_identity_and_publication_precision_are_bound_to_w9(
    field: str, value: object, code: str
) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    modified = _replace_report(snapshot, report_index, replace(report, **{field: value}))

    _assert_candidate_error(modified, code)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ("duplicate_period", "invalid_snapshot"),
        ("wrong_currency", "currency_not_admitted"),
        ("invalid_publication_date", "invalid_snapshot"),
        ("datetime_report_period", "invalid_snapshot"),
        ("naive_retrieval_time", "invalid_snapshot"),
        ("non_utc_retrieval_time", "invalid_snapshot"),
        ("wrong_source_timezone", "invalid_snapshot"),
        ("boolean_update_epoch", "invalid_snapshot"),
        ("zero_update_epoch", "invalid_snapshot"),
        ("mismatched_source_update_time", "invalid_snapshot"),
        ("unrepresentable_epoch", "invalid_snapshot"),
    ],
)
def test_snapshot_period_currency_and_time_guards_fail_closed(mutation: str, code: str) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    if mutation == "duplicate_period":
        changed = replace(report, report_period=snapshot.reports[0].report_period)
        modified = _replace_report(snapshot, report_index, changed)
    elif mutation == "wrong_currency":
        modified = _replace_report(snapshot, report_index, replace(report, currency="USD"))
    elif mutation == "invalid_publication_date":
        modified = _replace_report(snapshot, report_index, replace(report, published_on="20260417"))
    elif mutation == "datetime_report_period":
        modified = _replace_report(
            snapshot,
            report_index,
            replace(report, report_period=datetime(2024, 12, 31, tzinfo=timezone.utc)),
        )
    elif mutation == "naive_retrieval_time":
        modified = replace(snapshot, retrieved_at_utc=datetime(2026, 10, 27, 2, 0))
    elif mutation == "non_utc_retrieval_time":
        modified = replace(
            snapshot,
            retrieved_at_utc=datetime(2026, 10, 27, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
    elif mutation == "wrong_source_timezone":
        wrong_zone_time = datetime(2025, 4, 2, 21, 15, 11, tzinfo=timezone(timedelta(hours=8)))
        modified = _replace_report(
            snapshot, report_index, replace(report, source_updated_at=wrong_zone_time)
        )
    elif mutation == "boolean_update_epoch":
        modified = _replace_report(
            snapshot, report_index, replace(report, source_update_epoch_seconds=True)
        )
    elif mutation == "zero_update_epoch":
        modified = _replace_report(
            snapshot, report_index, replace(report, source_update_epoch_seconds=0)
        )
    elif mutation == "mismatched_source_update_time":
        modified = _replace_report(
            snapshot,
            report_index,
            replace(report, source_updated_at=report.source_updated_at.replace(second=12)),
        )
    else:
        changed = replace(report, source_update_epoch_seconds=10**1000)
        modified = _replace_report(snapshot, report_index, changed)

    _assert_candidate_error(modified, code)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("raw_value", 100),
        ("value_state", "null"),
        ("source_item_id", "   "),
        ("source_title", ""),
        ("source_ordinals", ()),
        ("source_ordinals", (True,)),
        ("source_ordinals", [1]),
        ("source_ordinals", (-1,)),
        ("source_ordinals", (2, 1)),
        ("source_item_id", "\ud800"),
        ("source_title", "\ud800"),
        ("raw_value", "\ud800"),
        ("source_metadata", [("valid", "shape but not a tuple")]),
        ("source_metadata", (("key",),)),
        ("source_metadata", ((1, "non-string key"),)),
        ("source_metadata", (("\ud800", "bad key encoding"),)),
        ("source_metadata", (("value", "\ud800"),)),
        ("source_metadata", (("z", "late"), ("a", "early"))),
        ("source_metadata", (("same", "one"), ("same", "two"))),
        ("source_metadata", (("nested", {"not": "scalar"}),)),
        ("source_metadata", (("nonfinite", float("inf")),)),
    ],
)
def test_malformed_manual_item_dtos_are_rejected(field: str, value: object) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    source_item = _item(report, "TOTASSET")
    changed = replace(source_item, **{field: value})
    modified = _replace_entry(snapshot, _PILOT_PERIOD, _entry_index(report, source_item), changed)

    _assert_candidate_error(modified, "invalid_snapshot")


def test_overlapping_or_gapped_source_ordinals_are_rejected() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    item = _item(report, "TOTASSET")
    index = _entry_index(report, item)
    overlapping = _replace_entry(
        snapshot,
        _PILOT_PERIOD,
        index,
        replace(item, source_ordinals=(1,)),
    )
    _assert_candidate_error(overlapping, "invalid_snapshot")

    last_entry = report.entries[-1]
    assert isinstance(last_entry, SinaFzbItem)
    gap = _replace_entry(
        snapshot,
        _PILOT_PERIOD,
        len(report.entries) - 1,
        replace(last_entry, source_ordinals=(141,)),
    )
    _assert_candidate_error(gap, "invalid_snapshot")


def test_source_entries_must_stay_in_first_ordinal_order() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    reordered = replace(report, entries=tuple(reversed(report.entries)))

    _assert_candidate_error(_replace_report(snapshot, report_index, reordered), "invalid_snapshot")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_title", "unknown marker title"),
        ("raw_item_field", "synthetic-id"),
        ("raw_item_value", "0.00"),
        ("source_ordinals", ()),
        ("source_metadata", (("item_display", "小类"),)),
    ],
)
def test_non_whitelisted_group_marker_dtos_are_rejected(field: str, value: object) -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    marker = next(entry for entry in report.entries if isinstance(entry, SinaFzbGroupMarker))
    changed = replace(marker, **{field: value})
    modified = _replace_entry(snapshot, _PILOT_PERIOD, _entry_index(report, marker), changed)

    _assert_candidate_error(modified, "invalid_snapshot")


@pytest.mark.parametrize("group_value", [True, 99])
def test_marker_identity_and_required_flag_values_are_exact(group_value: object) -> None:
    snapshot = _recorded_snapshot()
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    marker = next(entry for entry in report.entries if isinstance(entry, SinaFzbGroupMarker))
    marker_index = _entry_index(report, marker)

    invalid_group_number = _replace_entry(
        snapshot,
        _PILOT_PERIOD,
        marker_index,
        replace(marker, source_group_no=group_value),
    )
    _assert_candidate_error(invalid_group_number, "invalid_snapshot")

    invalid_flags = tuple(
        (key, group_value if key == "item_group_no" else value)
        for key, value in marker.source_metadata
    )
    invalid_marker = replace(marker, source_metadata=invalid_flags)
    _assert_candidate_error(
        _replace_entry(snapshot, _PILOT_PERIOD, marker_index, invalid_marker),
        "invalid_snapshot",
    )


def test_repeated_source_item_id_and_group_number_are_rejected_not_remerged() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report = _report_for_period(snapshot, _PILOT_PERIOD)
    cash = _item(report, "CURFDS")
    receivable = _item(report, "ACCORECE")
    duplicate_id = _replace_entry(
        snapshot,
        _PILOT_PERIOD,
        _entry_index(report, receivable),
        replace(receivable, source_item_id=cash.source_item_id),
    )
    _assert_candidate_error(duplicate_id, "conflicting_source_entry")

    markers = [entry for entry in report.entries if isinstance(entry, SinaFzbGroupMarker)]
    first, second = markers[:2]
    metadata = tuple(
        (key, 1 if key == "item_group_no" else value) for key, value in second.source_metadata
    )
    duplicate_group = _replace_entry(
        snapshot,
        _PILOT_PERIOD,
        _entry_index(report, second),
        replace(
            second,
            source_group_no=first.source_group_no,
            source_title=first.source_title,
            source_metadata=metadata,
        ),
    )
    _assert_candidate_error(duplicate_group, "conflicting_source_entry")


def test_late_invalid_report_rejects_the_entire_candidate_projection() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report_index = len(snapshot.reports) - 1
    report = snapshot.reports[report_index]
    modified = _replace_report(snapshot, report_index, replace(report, currency="USD"))

    _assert_candidate_error(modified, "currency_not_admitted", secret="USD")


def test_report_limit_is_checked_before_any_candidate_output() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    source_report = _report_for_period(snapshot, _PILOT_PERIOD)
    reports = tuple(
        replace(source_report, report_period=date(1900, 1, 1) + timedelta(days=index))
        for index in range(MAX_SINA_FZB_REPORTS + 1)
    )
    oversized = replace(snapshot, reports=reports)

    _assert_candidate_error(oversized, "limit_exceeded")


# The frozen aggregate ceiling is 256 * 512, and each factor is checked first;
# exceeding the aggregate without violating an earlier bound is unreachable.
def test_per_report_raw_occurrence_limit_counts_collapsed_source_ordinals() -> None:
    snapshot = deepcopy(_recorded_snapshot())
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    item = _item(report, "TOTASSET")
    excess_item = replace(item, source_ordinals=tuple(range(MAX_SINA_FZB_ROWS_PER_REPORT + 1)))
    oversized = _replace_report(
        snapshot,
        report_index,
        replace(report, entries=(excess_item,)),
    )

    _assert_candidate_error(oversized, "limit_exceeded")


def test_per_report_limit_counts_occurrences_across_distinct_source_items() -> None:
    snapshot = _recorded_snapshot()
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    first = _item(report, "TOTASSET")
    second = _item(report, "TOTLIAB")
    oversized_entries = (
        replace(first, source_ordinals=tuple(range(MAX_SINA_FZB_ROWS_PER_REPORT))),
        replace(
            second,
            source_item_id="SYNTHETIC_OVERFLOW_ROW",
            source_ordinals=(MAX_SINA_FZB_ROWS_PER_REPORT,),
        ),
    )
    oversized = _replace_report(snapshot, report_index, replace(report, entries=oversized_entries))

    _assert_candidate_error(oversized, "limit_exceeded")


def test_per_report_entry_count_is_bounded_before_duplicate_validation() -> None:
    snapshot = _recorded_snapshot()
    report_index = _report_index(snapshot, _PILOT_PERIOD)
    report = snapshot.reports[report_index]
    repeated_entry = report.entries[-1]
    extra_entry_count = MAX_SINA_FZB_ROWS_PER_REPORT + 1 - len(report.entries)
    oversized_entries = report.entries + (repeated_entry,) * extra_entry_count

    oversized = _replace_report(snapshot, report_index, replace(report, entries=oversized_entries))
    _assert_candidate_error(oversized, "limit_exceeded")
