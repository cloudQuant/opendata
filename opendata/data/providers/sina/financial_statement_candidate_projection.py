"""Pure projection of recorded Sina FZB rows into versioned mapping candidates."""

from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from typing import NoReturn
from zoneinfo import ZoneInfo

from opendata.data.providers.sina.financial_statement_candidate_contract import (
    MAX_SINA_FZB_REPORTS,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    MAX_SINA_FZB_TOTAL_ROWS,
    SINA_FZB_CANDIDATE_EVIDENCE_REFS,
    SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT,
    SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS,
    SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
    SINA_FZB_CANDIDATE_MAPPINGS,
    SINA_FZB_CANDIDATE_REQUIRED_CURRENCY,
    SINA_FZB_CANDIDATE_STATUS,
    SinaFzbCandidateAccounting,
    SinaFzbCandidateError,
    SinaFzbCandidateErrorCode,
    SinaFzbCandidateItem,
    SinaFzbCandidateMapping,
    SinaFzbCandidateProjection,
    SinaFzbCandidateReport,
    SinaFzbUnmappedReason,
)
from opendata.data.providers.sina.financial_statement_contract import (
    SINA_FZB_GROUP_DISPLAY,
    SINA_FZB_GROUP_DISPLAY_TYPE,
    SINA_FZB_GROUP_ITEM_PRECISION,
    SINA_FZB_GROUP_ITEM_SOURCE,
    SINA_FZB_GROUP_ITEM_TONGBI,
    SINA_FZB_GROUP_MARKERS,
    SinaFzbEntry,
    SinaFzbFetchedSnapshot,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
)

__all__ = ["project_sina_fzb_candidates"]

_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\.(?:SH|SZ)\Z")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHANGHAI_KEY = "Asia/Shanghai"


def project_sina_fzb_candidates(
    snapshot: SinaFzbFetchedSnapshot, *, expected_query: SinaFzbQuery
) -> SinaFzbCandidateProjection:
    """Project a validated W9 snapshot into seven namespace-scoped candidates.

    The projector preserves source-native values, times, metadata, markers, and
    unmapped items. The result remains partial even when every proposed pair is
    present because this finite mapping does not describe a complete statement.

    Args:
        snapshot: Immutable source-native W9 snapshot to validate and project.
        expected_query: Canonical caller query that the snapshot must match.

    Returns:
        An immutable candidate projection with per-report source accounting.

    Raises:
        SinaFzbCandidateError: If the snapshot violates the frozen candidate
            contract, identity, currency, time, row, or ordinal constraints.
    """
    if type(expected_query) is not SinaFzbQuery:
        _raise_candidate("query_mismatch")
    _validate_query(expected_query, error_code="query_mismatch")
    if type(snapshot) is not SinaFzbFetchedSnapshot:
        _raise_candidate("invalid_snapshot")

    _validate_snapshot(snapshot, expected_query=expected_query)
    candidate_reports = tuple(_project_report(report) for report in snapshot.reports)
    return SinaFzbCandidateProjection(
        query=expected_query,
        retrieved_at_utc=snapshot.retrieved_at_utc,
        mapping_namespace=SINA_FZB_CANDIDATE_MAPPING_NAMESPACE,
        status=SINA_FZB_CANDIDATE_STATUS,
        evidence_refs=SINA_FZB_CANDIDATE_EVIDENCE_REFS,
        reports=candidate_reports,
    )


def _validate_snapshot(snapshot: SinaFzbFetchedSnapshot, *, expected_query: SinaFzbQuery) -> None:
    """Validate the complete snapshot before constructing any output DTO."""
    if type(snapshot.query) is not SinaFzbQuery:
        _raise_candidate("invalid_snapshot")
    _validate_query(snapshot.query, error_code="invalid_snapshot")
    if snapshot.query != expected_query:
        _raise_candidate("query_mismatch")
    _validate_utc_datetime(snapshot.retrieved_at_utc)

    reports = snapshot.reports
    if type(reports) is not tuple:
        _raise_candidate("invalid_snapshot")
    if len(reports) > MAX_SINA_FZB_REPORTS:
        _raise_candidate("limit_exceeded")

    expected_paper_code = _paper_code(expected_query.symbol)
    seen_periods: set[date] = set()
    total_occurrences = 0
    for report_value in reports:
        occurrences = _validate_report(
            report_value,
            expected_symbol=expected_query.symbol,
            expected_paper_code=expected_paper_code,
        )
        report = report_value
        if report.report_period in seen_periods:
            _raise_candidate("invalid_snapshot")
        seen_periods.add(report.report_period)
        total_occurrences += occurrences
        if total_occurrences > MAX_SINA_FZB_TOTAL_ROWS:
            _raise_candidate("limit_exceeded")


def _validate_query(query: SinaFzbQuery, *, error_code: SinaFzbCandidateErrorCode) -> None:
    """Validate a source query using the exact frozen six-digit exchange form."""
    if type(query) is not SinaFzbQuery:
        _raise_candidate(error_code)
    if type(query.symbol) is not str or _SYMBOL_PATTERN.fullmatch(query.symbol) is None:
        _raise_candidate(error_code)


def _paper_code(symbol: str) -> str:
    """Convert a validated exchange-qualified symbol to Sina's paper code."""
    code, separator, exchange = symbol.partition(".")
    if separator != "." or exchange not in {"SH", "SZ"}:
        _raise_candidate("source_identity_mismatch")
    market = exchange.lower()
    return f"{market}{code}"


def _validate_report(value: object, *, expected_symbol: str, expected_paper_code: str) -> int:
    """Validate one source report and return its raw source-row occurrence count."""
    if type(value) is not SinaFzbReport:
        _raise_candidate("invalid_snapshot")
    report = value

    if (
        type(report.symbol) is not str
        or report.symbol != expected_symbol
        or type(report.source_paper_code) is not str
        or report.source_paper_code != expected_paper_code
        or type(report.statement_code) is not str
        or report.statement_code != "fzb"
    ):
        _raise_candidate("source_identity_mismatch")
    if type(report.report_period) is not date or type(report.published_on) is not date:
        _raise_candidate("invalid_snapshot")
    if type(report.publication_precision) is not str or report.publication_precision != "date":
        _raise_candidate("invalid_snapshot")
    if type(report.currency) is not str or report.currency != SINA_FZB_CANDIDATE_REQUIRED_CURRENCY:
        _raise_candidate("currency_not_admitted")

    _validate_report_update_time(report)
    _validate_metadata(report.period_metadata)
    _validate_metadata(report.report_metadata)
    return _validate_entries(report.entries)


def _validate_report_update_time(report: SinaFzbReport) -> None:
    """Require the exact positive epoch and its original Shanghai conversion."""
    epoch = report.source_update_epoch_seconds
    if type(epoch) is not int or epoch <= 0:
        _raise_candidate("invalid_snapshot")
    try:
        expected = datetime.fromtimestamp(epoch, tz=_SHANGHAI)
    except (OverflowError, OSError, ValueError):
        _raise_candidate("invalid_snapshot")

    value = report.source_updated_at
    if type(value) is not datetime:
        _raise_candidate("invalid_snapshot")
    zone = value.tzinfo
    if not isinstance(zone, ZoneInfo) or zone.key != _SHANGHAI_KEY:
        _raise_candidate("invalid_snapshot")
    if value != expected or value.fold != expected.fold:
        _raise_candidate("invalid_snapshot")


def _validate_utc_datetime(value: datetime) -> None:
    """Require an exact aware datetime with a zero UTC offset."""
    if type(value) is not datetime or value.tzinfo is None:
        _raise_candidate("invalid_snapshot")
    try:
        offset = value.utcoffset()
    except Exception:
        _raise_candidate("invalid_snapshot")
    if offset != timedelta(0):
        _raise_candidate("invalid_snapshot")


def _validate_entries(entries: tuple[SinaFzbEntry, ...]) -> int:
    """Validate row identities, metadata, ordinals, ordering, and row bounds."""
    if type(entries) is not tuple:
        _raise_candidate("invalid_snapshot")
    if len(entries) > MAX_SINA_FZB_ROWS_PER_REPORT:
        _raise_candidate("limit_exceeded")

    seen_ordinals: set[int] = set()
    seen_item_ids: set[str] = set()
    seen_group_numbers: set[int] = set()
    previous_first_ordinal = -1
    occurrence_count = 0

    for entry_value in entries:
        entry: object = entry_value
        if type(entry) is SinaFzbItem:
            item = entry
            _validate_item(item)
            source_ordinals = item.source_ordinals
            identity = item.source_item_id
            if identity in seen_item_ids:
                _raise_candidate("conflicting_source_entry")
            seen_item_ids.add(identity)
        elif type(entry) is SinaFzbGroupMarker:
            marker = entry
            _validate_group_marker(marker)
            source_ordinals = marker.source_ordinals
            identity_number = marker.source_group_no
            if identity_number in seen_group_numbers:
                _raise_candidate("conflicting_source_entry")
            seen_group_numbers.add(identity_number)
        else:
            _raise_candidate("invalid_snapshot")

        entry_occurrences, first_ordinal = _validate_ordinals(
            source_ordinals, seen_ordinals=seen_ordinals
        )
        if first_ordinal <= previous_first_ordinal:
            _raise_candidate("invalid_snapshot")
        previous_first_ordinal = first_ordinal
        occurrence_count += entry_occurrences
        if occurrence_count > MAX_SINA_FZB_ROWS_PER_REPORT:
            _raise_candidate("limit_exceeded")

    if len(seen_ordinals) != occurrence_count:
        _raise_candidate("invalid_snapshot")
    if occurrence_count and (min(seen_ordinals) != 0 or max(seen_ordinals) != occurrence_count - 1):
        _raise_candidate("invalid_snapshot")
    return occurrence_count


def _validate_item(item: SinaFzbItem) -> None:
    """Validate one source-native financial item without interpreting its value."""
    if (
        type(item.source_item_id) is not str
        or not item.source_item_id.strip()
        or type(item.source_title) is not str
        or item.source_title == ""
    ):
        _raise_candidate("invalid_snapshot")
    _validate_utf8_text(item.source_item_id)
    _validate_utf8_text(item.source_title)

    value = item.raw_value
    if value is not None and type(value) is not str:
        _raise_candidate("invalid_snapshot")
    if type(value) is str:
        _validate_utf8_text(value)
    expected_state = "null" if value is None else "empty" if value == "" else "present"
    if type(item.value_state) is not str or item.value_state != expected_state:
        _raise_candidate("invalid_snapshot")
    _validate_metadata(item.source_metadata)


def _validate_group_marker(marker: SinaFzbGroupMarker) -> None:
    """Validate the exact source-local heading identity and its source flags."""
    if type(marker.source_group_no) is not int:
        _raise_candidate("invalid_snapshot")
    expected_title: str | None = None
    for group_number, title in SINA_FZB_GROUP_MARKERS:
        if marker.source_group_no == group_number:
            expected_title = title
            break
    if (
        expected_title is None
        or type(marker.source_title) is not str
        or marker.source_title != expected_title
        or type(marker.raw_item_field) is not str
        or marker.raw_item_field != ""
        or type(marker.raw_item_value) is not str
        or marker.raw_item_value != ""
    ):
        _raise_candidate("invalid_snapshot")

    _validate_metadata(marker.source_metadata)
    metadata = dict(marker.source_metadata)
    expected_flags: tuple[tuple[str, object], ...] = (
        ("item_group_no", marker.source_group_no),
        ("item_display_type", SINA_FZB_GROUP_DISPLAY_TYPE),
        ("item_display", SINA_FZB_GROUP_DISPLAY),
        ("item_source", SINA_FZB_GROUP_ITEM_SOURCE),
        ("item_precision", SINA_FZB_GROUP_ITEM_PRECISION),
        ("item_tongbi", SINA_FZB_GROUP_ITEM_TONGBI),
    )
    for key, expected in expected_flags:
        actual = metadata.get(key)
        if type(actual) is not type(expected) or actual != expected:
            _raise_candidate("invalid_snapshot")


def _validate_ordinals(value: object, *, seen_ordinals: set[int]) -> tuple[int, int]:
    """Require a nonempty ordered ordinal tuple with no overlap."""
    if type(value) is not tuple or len(value) == 0:
        _raise_candidate("invalid_snapshot")
    if len(value) > MAX_SINA_FZB_ROWS_PER_REPORT:
        _raise_candidate("limit_exceeded")
    previous = -1
    first: int | None = None
    for ordinal_value in value:
        ordinal: object = ordinal_value
        if type(ordinal) is not int or ordinal < 0 or ordinal <= previous:
            _raise_candidate("invalid_snapshot")
        if ordinal in seen_ordinals:
            _raise_candidate("invalid_snapshot")
        seen_ordinals.add(ordinal)
        if first is None:
            first = ordinal
        previous = ordinal
    if first is None:
        _raise_candidate("invalid_snapshot")
    return len(value), first


def _validate_metadata(metadata: object) -> None:
    """Require sorted unique string-keyed JSON scalar metadata tuples."""
    if type(metadata) is not tuple:
        _raise_candidate("invalid_snapshot")
    previous_key: str | None = None
    for pair_value in metadata:
        key, value = _metadata_pair(pair_value)
        if previous_key is not None and key <= previous_key:
            _raise_candidate("invalid_snapshot")
        previous_key = key
        _validate_json_scalar(value)


def _metadata_pair(value: object) -> tuple[str, object]:
    """Narrow one exact two-element metadata pair without trusting annotations."""
    if type(value) is not tuple or len(value) != 2:
        _raise_candidate("invalid_snapshot")
    key_value: object = value[0]
    scalar_value: object = value[1]
    if type(key_value) is not str:
        _raise_candidate("invalid_snapshot")
    _validate_utf8_text(key_value)
    return key_value, scalar_value


def _validate_json_scalar(value: object) -> None:
    """Accept exact finite JSON scalar types only."""
    if value is None or type(value) is bool or type(value) is int:
        return
    if type(value) is float:
        if not math.isfinite(value):
            _raise_candidate("invalid_snapshot")
        return
    if type(value) is str:
        _validate_utf8_text(value)
        return
    _raise_candidate("invalid_snapshot")


def _validate_utf8_text(value: str) -> None:
    """Reject manually forged lone-surrogate strings that W9 cannot emit."""
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _raise_candidate("invalid_snapshot")


def _project_report(report: SinaFzbReport) -> SinaFzbCandidateReport:
    """Build one deterministic mapping/accounting report from validated W9 data."""
    mappings_by_pair: dict[tuple[str, str], SinaFzbCandidateMapping] = {
        (mapping.source_item_id, mapping.source_title): mapping
        for mapping in SINA_FZB_CANDIDATE_MAPPINGS
    }
    known_source_ids = frozenset(mapping.source_item_id for mapping in SINA_FZB_CANDIDATE_MAPPINGS)
    matched_pairs: set[tuple[str, str]] = set()
    matched_source_item_ids: list[str] = []
    unmapped_ordinals: list[int] = []
    projected_entries: list[SinaFzbCandidateItem | SinaFzbGroupMarker] = []
    financial_item_count = 0
    group_marker_count = 0
    raw_row_occurrence_count = 0

    for entry in report.entries:
        if type(entry) is SinaFzbGroupMarker:
            projected_entries.append(entry)
            group_marker_count += 1
            raw_row_occurrence_count += len(entry.source_ordinals)
            continue
        if type(entry) is not SinaFzbItem:
            _raise_candidate("invalid_snapshot")

        item = entry
        financial_item_count += 1
        raw_row_occurrence_count += len(item.source_ordinals)
        pair = (item.source_item_id, item.source_title)
        mapping = mappings_by_pair.get(pair)
        if mapping is not None:
            matched_pairs.add(pair)
            matched_source_item_ids.append(item.source_item_id)
            projected_entries.append(
                SinaFzbCandidateItem(
                    source_item=item,
                    mapping_namespace=mapping.mapping_namespace,
                    disposition="candidate",
                    proposed_item_key=mapping.proposed_item_key,
                    unmapped_reason=None,
                    mapping_evidence_refs=SINA_FZB_CANDIDATE_MAPPING_EVIDENCE_REFS,
                )
            )
            continue

        reason: SinaFzbUnmappedReason = (
            "title_mismatch" if item.source_item_id in known_source_ids else "unknown_source_item"
        )
        unmapped_ordinals.extend(item.source_ordinals)
        projected_entries.append(
            SinaFzbCandidateItem(
                source_item=item,
                mapping_namespace=None,
                disposition="unmapped",
                proposed_item_key=None,
                unmapped_reason=reason,
                mapping_evidence_refs=(),
            )
        )

    missing_pairs = tuple(
        (mapping.source_item_id, mapping.source_title)
        for mapping in SINA_FZB_CANDIDATE_MAPPINGS
        if (mapping.source_item_id, mapping.source_title) not in matched_pairs
    )
    accounting = SinaFzbCandidateAccounting(
        expected_mapping_count=SINA_FZB_CANDIDATE_EXPECTED_MAPPING_COUNT,
        matched_source_item_ids=tuple(matched_source_item_ids),
        missing_expected_source_pairs=missing_pairs,
        unmapped_source_ordinals=tuple(sorted(unmapped_ordinals)),
        raw_source_row_occurrence_count=raw_row_occurrence_count,
        unique_source_financial_item_count=financial_item_count,
        unique_source_group_marker_count=group_marker_count,
    )
    return SinaFzbCandidateReport(
        source_report=report,
        entries=tuple(projected_entries),
        accounting=accounting,
    )


def _raise_candidate(code: SinaFzbCandidateErrorCode) -> NoReturn:
    """Raise one fixed-message candidate-contract error."""
    raise SinaFzbCandidateError(code)
