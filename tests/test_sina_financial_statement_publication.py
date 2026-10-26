"""Offline source-native Sina publication tests using the pinned W9 response."""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

import opendata.data.providers.sina.financial_statement_publication as publication
from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    MAX_SINA_FZB_ROWS_PER_REPORT,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbReport,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_parser import parse_sina_fzb_response
from opendata.data.providers.sina.financial_statement_publication import (
    SinaFzbPublicationError,
    build_sina_fzb_observation_contract,
    serialize_sina_fzb_response,
)

_CAPTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
_CAPTURE_ARCHIVE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
_QUERY = SinaFzbQuery("600519.SH")
_CAPTURE_ID = "a" * 64
_OTHER_CAPTURE_ID = "b" * 64
_RETRIEVED_AT = datetime(2026, 10, 27, 2, 0, tzinfo=timezone.utc)
_PARSED_SNAPSHOT_SHA256 = "11ff6326d0098fcfe83bfb5c0b40d824958dbc0b6c9fd5fcf820856a12e586ea"


class _CallbackErrorTimezone(tzinfo):
    """Adversarial timezone that raises from a selected conversion callback."""

    def __init__(self, callback: str, error_type: type[Exception]) -> None:
        self.callback = callback
        self.error_type = error_type
        self.canary = f"timezone-callback-canary-{callback}"

    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        if self.callback == "utcoffset":
            self._raise_callback()
        if self.callback == "dst":
            self.dst(dt)
        if self.callback == "fromutc" and dt is not None:
            self.fromutc(dt)
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta | None:
        if self.callback == "dst":
            self._raise_callback()
        return timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        if self.callback == "fromutc":
            self._raise_callback()
        return dt

    def _raise_callback(self) -> None:
        raise self.error_type(self.canary)


class _SingleReadTimezone(tzinfo):
    """Return UTC once, then fail if normalization re-reads source tzinfo."""

    def __init__(self) -> None:
        self.utcoffset_calls = 0
        self.canary = "timezone-repeated-read-canary"

    def utcoffset(self, dt: datetime | None) -> timedelta:
        self.utcoffset_calls += 1
        if self.utcoffset_calls > 1:
            raise RuntimeError(self.canary)
        return timedelta(0)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        raise RuntimeError(self.canary)


class _ShanghaiOffsetFailureTimezone(tzinfo):
    """Look like the parser's Shanghai zone but fail during offset validation."""

    key = "Asia/Shanghai"

    def utcoffset(self, dt: datetime | None) -> timedelta:
        raise ValueError("source-update-offset-canary")

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def fromutc(self, dt: datetime) -> datetime:
        return dt


@lru_cache(maxsize=1)
def _recorded_capture() -> tuple[dict[str, Any], bytes]:
    """Return the original response bytes and their pinned outer recording."""
    archive = _CAPTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == _CAPTURE_ARCHIVE_SHA256
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
    return call, body


def _recorded_body() -> bytes:
    return _recorded_capture()[1]


def _recorded_document() -> dict[str, Any]:
    document = json.loads(_recorded_body())
    assert isinstance(document, dict)
    return document


@lru_cache(maxsize=1)
def _parsed_reports() -> tuple[SinaFzbReport, ...]:
    """Return typed reports parsed from the exact pinned W9 body."""
    return parse_sina_fzb_response(_recorded_body(), query=_QUERY)


def _captured_2024_report() -> SinaFzbReport:
    return next(
        report for report in _parsed_reports() if report.report_period.isoformat() == "2024-12-31"
    )


def _patch_parser_reports(monkeypatch: pytest.MonkeyPatch, reports: tuple[Any, ...]) -> None:
    """Simulate parser seam corruption while testing only the public serializer."""

    def corrupted_parser(body: bytes, *, query: SinaFzbQuery) -> tuple[Any, ...]:
        assert body == b"parser-seam-input"
        assert query == _QUERY
        return reports

    monkeypatch.setattr(publication, "parse_sina_fzb_response", corrupted_parser)


def _encode_document(document: dict[str, Any], *, sort_keys: bool = False) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=sort_keys,
        allow_nan=False,
    ).encode("utf-8", errors="strict")


def _report_record(batch: Any, period: str) -> Any:
    return next(row for row in batch.records if row.payload["report_period"] == period)


def _item_payload(report_payload: dict[str, Any], source_item_id: str) -> dict[str, Any]:
    return next(
        entry
        for entry in report_payload["entries"]
        if entry["kind"] == "item" and entry["source_item_id"] == source_item_id
    )


def _assert_publication_error(
    code: str,
    *,
    body: object,
    query: object = _QUERY,
    retrieved_at_utc: object = _RETRIEVED_AT,
    capture_id: object = _CAPTURE_ID,
    secret: str | None = None,
) -> SinaFzbPublicationError:
    result: object | None = None
    with pytest.raises(SinaFzbPublicationError) as raised:
        result = serialize_sina_fzb_response(
            body,
            query=query,
            retrieved_at_utc=retrieved_at_utc,
            capture_id=capture_id,
        )
    assert result is None
    assert raised.value.code == code
    assert "600519" not in str(raised.value)
    assert "600519" not in repr(raised.value)
    if secret is not None:
        assert secret not in str(raised.value)
        assert secret not in repr(raised.value)
    return raised.value


def _assert_parser_seam_error(
    monkeypatch: pytest.MonkeyPatch,
    reports: tuple[Any, ...],
    *,
    code: str = "invalid_response",
    secret: str | None = None,
) -> SinaFzbPublicationError:
    _patch_parser_reports(monkeypatch, reports)
    return _assert_publication_error(
        code,
        body=b"parser-seam-input",
        secret=secret,
    )


def test_factory_declares_the_source_native_atemporal_capture_contract() -> None:
    contract = build_sina_fzb_observation_contract()

    assert contract.dataset_id == "sina_fzb_report_observations"
    assert contract.schema_version == "source-native-report-v1"
    assert contract.semantic_version == "sina-fzb-source-native-v1"
    assert contract.source.source == "sina"
    assert contract.source.source_id == "sina-fzb"
    assert contract.asset_class == "equity"
    assert contract.frequency == "irregular"
    assert contract.record_kind == "table"
    assert contract.temporal_semantics == "none"
    assert contract.time_field is None
    assert contract.source_available_at_field is None
    assert contract.pit_policy == "unsupported"
    assert contract.adjustment_basis == "not_applicable"
    assert contract.calendar_id is None
    assert contract.timezone == "UTC"
    assert contract.currency is None
    assert contract.field_currencies == {}
    assert contract.field_adjustments == {}
    assert contract.field_units == {"source_update_epoch_seconds": "unix_seconds"}
    assert contract.business_key == (
        "instrument_id",
        "capture_id",
        "statement_code",
        "report_period",
    )
    assert contract.ordering == contract.business_key
    assert contract.filter_fields == ("instrument_id", "capture_id")

    expected_types = {
        "instrument_id": "string",
        "capture_id": "string",
        "statement_code": "string",
        "source_paper_code": "string",
        "publication_precision": "string",
        "source_updated_at_timezone": "string",
        "currency": "string",
        "report_period": "date",
        "published_on": "date",
        "source_update_epoch_seconds": "integer",
        "source_updated_at": "datetime",
        "retrieved_at_utc": "datetime",
        "period_metadata": "object",
        "report_metadata": "object",
        "entries": "array",
    }
    actual_fields = {field.name: field for field in contract.fields}
    assert set(actual_fields) == set(expected_types)
    assert {name: field.data_type for name, field in actual_fields.items()} == expected_types
    assert all(field.nullable is False for field in actual_fields.values())


def test_recorded_capture_preserves_all_reports_rows_markers_and_raw_values() -> None:
    batch = serialize_sina_fzb_response(
        _recorded_body(),
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )

    assert len(batch.records) == 103
    assert isinstance(batch.records, tuple)
    assert [row.payload["report_period"] for row in batch.records[:2]] == [
        "2026-06-30",
        "2026-03-31",
    ]
    assert batch.records[-1].payload["report_period"] == "1998-12-31"
    assert batch.records[0].business_key == (
        '["600519.SH","aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"fzb","2026-06-30"]'
    )

    assert batch.schema_version == "source-native-report-v1"
    assert batch.semantic_version == "sina-fzb-source-native-v1"
    assert batch.unit == "per-field"
    assert batch.adjustment_basis == "not_applicable"
    assert batch.quality == {"status": "unknown", "report_id": None, "issue_count": 0}
    assert batch.watermark is None
    assert batch.provenance["capture_id"] == _CAPTURE_ID
    assert batch.provenance["parsed_snapshot_sha256"] == _PARSED_SNAPSHOT_SHA256
    assert batch.provenance["representation"] == "derived_from_parsed_response"
    assert batch.provenance["parser_contract"] == "sina-fzb-wire-v1"
    assert batch.provenance["retrieved_at_utc"] == "2026-10-27T02:00:00.000000Z"
    assert batch.provenance["coverage_status"] == "unknown"
    assert isinstance(batch.provenance["history_limitation"], str)
    assert batch.provenance["history_limitation"]
    assert "contract_sha256" not in batch.provenance

    payload_fields = {
        "instrument_id",
        "capture_id",
        "statement_code",
        "source_paper_code",
        "report_period",
        "period_metadata",
        "published_on",
        "publication_precision",
        "source_update_epoch_seconds",
        "source_updated_at",
        "source_updated_at_timezone",
        "currency",
        "report_metadata",
        "retrieved_at_utc",
        "entries",
    }
    total_occurrences = 0
    marker_count = 0
    for row in batch.records:
        payload = row.payload
        assert set(payload) == payload_fields
        assert payload["instrument_id"] == "600519.SH"
        assert payload["capture_id"] == _CAPTURE_ID
        assert payload["statement_code"] == "fzb"
        assert payload["source_paper_code"] == "sh600519"
        assert payload["publication_precision"] == "date"
        assert payload["source_updated_at_timezone"] == "Asia/Shanghai"
        assert payload["retrieved_at_utc"] == "2026-10-27T02:00:00.000000Z"
        assert row.observed_at == _RETRIEVED_AT
        assert row.business_date is None
        assert row.event_at is None
        assert row.source_published_at is None
        assert row.available_at is None
        assert isinstance(payload["entries"], list)
        for entry in payload["entries"]:
            ordinals = entry["source_ordinals"]
            assert isinstance(ordinals, list) and ordinals
            total_occurrences += len(ordinals)
            if entry["kind"] == "group_marker":
                marker_count += 1
                assert entry["raw_item_field"] == ""
                assert entry["raw_item_value"] == ""
            else:
                assert entry["kind"] == "item"

    assert total_occurrences == 14_523
    assert marker_count == 515

    report = _report_record(batch, "2024-12-31").payload
    assert report["published_on"] == "2026-04-17"
    assert report["source_update_epoch_seconds"] == 1743599711
    assert report["source_updated_at"] == "2025-04-02T21:15:11+08:00"
    assert report["currency"] == "CNY"
    assert report["period_metadata"] == {"date_description": "2024年报", "date_type": 4}
    assert report["report_metadata"] == {
        "audit_opinion": "标准无保留审计意见",
        "data_source": "定期报告",
        "is_audit": "是",
        "is_exist_yoy": True,
        "rType": "合并期末",
    }
    assert set(report) == payload_fields

    marker = report["entries"][0]
    assert marker == {
        "kind": "group_marker",
        "source_ordinals": [0],
        "source_group_no": 1,
        "source_title": "流动资产",
        "raw_item_field": "",
        "raw_item_value": "",
        "source_metadata": {
            "item_display": "大类",
            "item_display_type": 1,
            "item_group_no": 1,
            "item_precision": "f2",
            "item_source": "fzb",
            "item_tongbi": "",
        },
    }
    cash = _item_payload(report, "CURFDS")
    assert cash == {
        "kind": "item",
        "source_item_id": "CURFDS",
        "source_title": "货币资金",
        "source_ordinals": [1],
        "raw_value": "59295822956.890000",
        "value_state": "present",
        "source_metadata": {
            "item_display": "小类",
            "item_display_type": 2,
            "item_group_no": 1,
            "item_precision": "f2",
            "item_source": "fzb",
            "item_tongbi": -0.14151,
        },
    }
    null_item = _item_payload(report, "SETTRESEDEPO")
    assert null_item["raw_value"] is None
    assert null_item["value_state"] == "null"
    duplicate = _item_payload(report, "DOMETICKSETT")
    assert duplicate["source_ordinals"] == [96, 97]
    assert duplicate["raw_value"] is None
    assert "candidate" not in json.dumps(report, ensure_ascii=False)


def test_capture_id_and_wire_format_do_not_change_the_parsed_snapshot_digest() -> None:
    body = _recorded_body()
    reformatted = _encode_document(_recorded_document(), sort_keys=True)
    assert reformatted != body
    equivalent_utc = datetime(
        2026, 10, 27, 2, 0, tzinfo=timezone(timedelta(0), name="fixed-zero-offset")
    )

    first = serialize_sina_fzb_response(
        body, query=_QUERY, retrieved_at_utc=_RETRIEVED_AT, capture_id=_CAPTURE_ID
    )
    second_capture = serialize_sina_fzb_response(
        body, query=_QUERY, retrieved_at_utc=_RETRIEVED_AT, capture_id=_OTHER_CAPTURE_ID
    )
    reformatted_input = serialize_sina_fzb_response(
        reformatted,
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )
    normalized_utc_input = serialize_sina_fzb_response(
        body,
        query=_QUERY,
        retrieved_at_utc=equivalent_utc,
        capture_id=_CAPTURE_ID,
    )

    digest = first.provenance["parsed_snapshot_sha256"]
    assert digest == _PARSED_SNAPSHOT_SHA256
    assert second_capture.provenance["parsed_snapshot_sha256"] == digest
    assert reformatted_input.provenance["parsed_snapshot_sha256"] == digest
    assert normalized_utc_input.provenance["parsed_snapshot_sha256"] == digest
    assert normalized_utc_input.records[0].observed_at == _RETRIEVED_AT
    assert normalized_utc_input.records[0].observed_at.tzinfo is timezone.utc
    assert first.records[0].payload["capture_id"] == _CAPTURE_ID
    assert second_capture.records[0].payload["capture_id"] == _OTHER_CAPTURE_ID
    assert first.records[0].business_key != second_capture.records[0].business_key


def test_parsed_snapshot_digest_changes_with_retrieval_time_and_source_data() -> None:
    body = _recorded_body()
    changed_document = copy.deepcopy(_recorded_document())
    first_report = changed_document["result"]["data"]["report_list"]["20260630"]
    first_report["audit_opinion"] = "fixture metadata mutation"
    changed_body = _encode_document(changed_document)

    baseline = serialize_sina_fzb_response(
        body, query=_QUERY, retrieved_at_utc=_RETRIEVED_AT, capture_id=_CAPTURE_ID
    )
    later_retrieval = serialize_sina_fzb_response(
        body,
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT + timedelta(seconds=1),
        capture_id=_CAPTURE_ID,
    )
    changed_source_data = serialize_sina_fzb_response(
        changed_body,
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )

    baseline_digest = baseline.provenance["parsed_snapshot_sha256"]
    assert later_retrieval.provenance["parsed_snapshot_sha256"] != baseline_digest
    assert changed_source_data.provenance["parsed_snapshot_sha256"] != baseline_digest
    assert later_retrieval.records[0].observed_at == _RETRIEVED_AT + timedelta(seconds=1)
    changed_report = _report_record(changed_source_data, "2026-06-30").payload
    assert changed_report["report_metadata"]["audit_opinion"] == "fixture metadata mutation"


@pytest.mark.parametrize(
    ("query", "retrieved_at_utc", "capture_id"),
    [
        (object(), _RETRIEVED_AT, _CAPTURE_ID),
        (_QUERY, None, _CAPTURE_ID),
        (_QUERY, datetime(2026, 10, 27, 2, 0), _CAPTURE_ID),
        (_QUERY, datetime(2026, 10, 27, 10, 0, tzinfo=timezone(timedelta(hours=8))), _CAPTURE_ID),
        (_QUERY, _RETRIEVED_AT, None),
        (_QUERY, _RETRIEVED_AT, "A" * 64),
        (_QUERY, _RETRIEVED_AT, "a" * 63),
    ],
)
def test_invalid_query_time_or_capture_identity_is_rejected(
    query: object, retrieved_at_utc: object, capture_id: object
) -> None:
    error = _assert_publication_error(
        "invalid_input",
        body=_recorded_body(),
        query=query,
        retrieved_at_utc=retrieved_at_utc,
        capture_id=capture_id,
    )
    assert error.__cause__ is None


def test_mutated_query_symbol_is_revalidated_before_parser_invocation() -> None:
    query = SinaFzbQuery("600519.SH")
    object.__setattr__(query, "symbol", "invalid-query-canary")

    error = _assert_publication_error(
        "invalid_input",
        body=_recorded_body(),
        query=query,
        secret="invalid-query-canary",
    )
    assert error.__cause__ is None


@pytest.mark.parametrize(
    ("callback", "error_type"),
    [
        ("utcoffset", TypeError),
        ("utcoffset", RuntimeError),
        ("dst", TypeError),
        ("dst", RuntimeError),
        ("fromutc", TypeError),
        ("fromutc", RuntimeError),
    ],
)
def test_timezone_callback_failures_are_sanitized_as_invalid_input(
    callback: str, error_type: type[Exception]
) -> None:
    source_timezone = _CallbackErrorTimezone(callback, error_type)
    retrieval_time = datetime(2026, 10, 27, 2, 0, tzinfo=source_timezone)

    error = _assert_publication_error(
        "invalid_input",
        body=_recorded_body(),
        retrieved_at_utc=retrieval_time,
        secret=source_timezone.canary,
    )
    assert error.__cause__ is None


def test_stateful_timezone_normalizes_once_or_fails_with_a_safe_error() -> None:
    source_timezone = _SingleReadTimezone()
    retrieval_time = datetime(2026, 10, 27, 2, 0, tzinfo=source_timezone)

    try:
        batch = serialize_sina_fzb_response(
            _recorded_body(),
            query=_QUERY,
            retrieved_at_utc=retrieval_time,
            capture_id=_CAPTURE_ID,
        )
    except SinaFzbPublicationError as error:
        assert error.code == "invalid_input"
        assert source_timezone.canary not in str(error)
        assert source_timezone.canary not in repr(error)
        assert source_timezone.utcoffset_calls == 2
        return

    assert source_timezone.utcoffset_calls == 1
    assert batch.records[0].observed_at == _RETRIEVED_AT
    assert batch.records[0].observed_at.tzinfo is timezone.utc
    assert batch.records[0].payload["retrieved_at_utc"] == "2026-10-27T02:00:00.000000Z"


def test_recorded_response_without_identity_echo_uses_its_explicit_request() -> None:
    """The response alone cannot prove symbol ownership when it omits identity echoes."""
    call, body = _recorded_capture()
    document = json.loads(body)
    data = document["result"]["data"]
    assert not {"paperCode", "source", "type"}.intersection(data)
    # With no body echo, the pinned outer request is the only recorded symbol binding.
    assert call["params"]["paperCode"] == "sh600519"

    batch = serialize_sina_fzb_response(
        body,
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )
    assert all(row.payload["instrument_id"] == "600519.SH" for row in batch.records)


def test_matching_optional_identity_echoes_preserve_explicit_request_binding() -> None:
    document = _recorded_document()
    data = document["result"]["data"]
    data.update({"paperCode": "sh600519", "source": "fzb", "type": "0"})

    batch = serialize_sina_fzb_response(
        _encode_document(document),
        query=_QUERY,
        retrieved_at_utc=_RETRIEVED_AT,
        capture_id=_CAPTURE_ID,
    )

    assert all(row.payload["source_paper_code"] == "sh600519" for row in batch.records)


@pytest.mark.parametrize(
    ("echo_field", "echo_value"),
    [("paperCode", "sh000001"), ("source", "other-source"), ("type", "1")],
)
def test_mismatched_source_identity_echo_is_rejected(echo_field: str, echo_value: str) -> None:
    document = _recorded_document()
    document["result"]["data"][echo_field] = echo_value

    error = _assert_publication_error(
        "invalid_response",
        body=_encode_document(document),
        secret=echo_value,
    )
    assert isinstance(error.__cause__, SinaFzbWireError)
    assert error.__cause__.code == "invalid_identity"


def test_parser_output_report_cap_fails_closed_before_any_batch_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _parsed_reports()[0]
    reports = (captured,) * 257

    _assert_parser_seam_error(monkeypatch, reports, code="limit_exceeded")


def test_parser_output_must_remain_a_report_dto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _assert_parser_seam_error(monkeypatch, (object(),))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("symbol", "000001.SZ"),
        ("source_paper_code", "sz000001"),
        ("statement_code", "bzk"),
    ],
)
def test_parser_output_identity_must_match_the_explicit_query(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    report = replace(_captured_2024_report(), **{field: value})

    _assert_parser_seam_error(monkeypatch, (report,), secret=value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("report_period", "not-a-date"),
        ("published_on", "not-a-date"),
        ("publication_precision", "timestamp"),
        ("currency", ""),
    ],
)
def test_parser_output_report_fields_reject_invalid_values(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    report = replace(_captured_2024_report(), **{field: value})

    _assert_parser_seam_error(monkeypatch, (report,))


@pytest.mark.parametrize(
    "case",
    [
        "boolean_epoch",
        "zero_epoch",
        "wrong_timezone",
        "wrong_datetime_type",
        "offset_callback_error",
        "unrepresentable_epoch",
        "clock_skew",
        "fold_mismatch",
    ],
)
def test_parser_output_source_update_timezone_and_epoch_are_consistent(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    report = _captured_2024_report()
    fields: dict[str, object] = {}
    if case == "boolean_epoch":
        fields["source_update_epoch_seconds"] = True
    elif case == "zero_epoch":
        fields["source_update_epoch_seconds"] = 0
    elif case == "wrong_timezone":
        fields["source_updated_at"] = datetime(2025, 4, 2, 13, 15, 11, tzinfo=timezone.utc)
    elif case == "wrong_datetime_type":
        fields["source_updated_at"] = None
    elif case == "offset_callback_error":
        fields["source_updated_at"] = datetime(
            2025, 4, 2, 21, 15, 11, tzinfo=_ShanghaiOffsetFailureTimezone()
        )
    elif case == "unrepresentable_epoch":
        fields["source_update_epoch_seconds"] = 10**100
    elif case == "clock_skew":
        fields["source_updated_at"] = report.source_updated_at + timedelta(seconds=1)
    else:
        fields["source_updated_at"] = report.source_updated_at.replace(fold=1)
    invalid = replace(report, **fields)

    _assert_parser_seam_error(monkeypatch, (invalid,))


@pytest.mark.parametrize(
    "case",
    ["not_tuple", "bad_pair", "empty_key", "unsorted_keys", "nonfinite", "unsupported"],
)
def test_parser_output_metadata_must_be_sorted_unique_json_scalars(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    report = _captured_2024_report()
    if case == "not_tuple":
        metadata: Any = []
    elif case == "bad_pair":
        metadata = (("bad-pair", 1, 2),)
    elif case == "empty_key":
        metadata = (("", "empty-key"),)
    elif case == "unsorted_keys":
        metadata = (("z-last", 1), ("a-first", 2))
    elif case == "nonfinite":
        metadata = (("nonfinite", float("nan")),)
    else:
        metadata = (("a-null", None), ("z-unsupported", object()))
    invalid = replace(report, report_metadata=metadata)

    _assert_parser_seam_error(monkeypatch, (invalid,))


@pytest.mark.parametrize("case", ["empty_identity", "nonstring_raw_value", "wrong_value_state"])
def test_parser_output_item_values_and_identity_are_rechecked(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    report = _captured_2024_report()
    item = next(
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbItem) and entry.source_item_id == "CURFDS"
    )
    if case == "empty_identity":
        invalid_item = replace(item, source_item_id="")
    elif case == "nonstring_raw_value":
        invalid_item = replace(item, raw_value=123)
    else:
        invalid_item = replace(item, value_state="empty")
    invalid = replace(report, entries=(invalid_item,))

    _assert_parser_seam_error(monkeypatch, (invalid,))


@pytest.mark.parametrize(
    "case",
    [
        "not_tuple",
        "too_many_entries",
        "unknown_entry",
        "empty_ordinals",
        "boolean_ordinal",
        "gapped_ordinals",
        "too_many_occurrences",
        "overlapping_entries",
        "descending_order",
        "negative_ordinal",
        "repeated_ordinal",
        "empty_entry_sequence",
    ],
)
def test_parser_output_entry_order_and_occurrence_bounds_are_rechecked(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    report = _captured_2024_report()
    item = next(
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbItem) and entry.source_item_id == "CURFDS"
    )
    if case == "not_tuple":
        entries: Any = []
        expected_code = "invalid_response"
    elif case == "too_many_entries":
        entries = (item,) * (MAX_SINA_FZB_ROWS_PER_REPORT + 1)
        expected_code = "limit_exceeded"
    elif case == "unknown_entry":
        entries = (object(),)
        expected_code = "invalid_response"
    elif case == "empty_ordinals":
        entries = (replace(item, source_ordinals=()),)
        expected_code = "invalid_response"
    elif case == "boolean_ordinal":
        entries = (replace(item, source_ordinals=(True,)),)
        expected_code = "invalid_response"
    elif case == "gapped_ordinals":
        entries = (replace(item, source_ordinals=(1,)),)
        expected_code = "invalid_response"
    elif case == "too_many_occurrences":
        entries = (replace(item, source_ordinals=tuple(range(MAX_SINA_FZB_ROWS_PER_REPORT + 1))),)
        expected_code = "limit_exceeded"
    elif case == "overlapping_entries":
        entries = (
            replace(item, source_ordinals=(0, 2)),
            replace(item, source_item_id="ANOTHER_ITEM", source_ordinals=(1, 2)),
        )
        expected_code = "invalid_response"
    elif case == "descending_order":
        entries = (item, replace(item, source_item_id="ANOTHER_ITEM", source_ordinals=(0,)))
        expected_code = "invalid_response"
    elif case == "negative_ordinal":
        entries = (replace(item, source_ordinals=(-1,)),)
        expected_code = "invalid_response"
    elif case == "repeated_ordinal":
        entries = (replace(item, source_ordinals=(0, 0)),)
        expected_code = "invalid_response"
    else:
        entries = ()
        expected_code = "invalid_response"
    invalid = replace(report, entries=entries)

    if case == "empty_entry_sequence":
        _patch_parser_reports(monkeypatch, (invalid,))
        batch = serialize_sina_fzb_response(
            b"parser-seam-input",
            query=_QUERY,
            retrieved_at_utc=_RETRIEVED_AT,
            capture_id=_CAPTURE_ID,
        )
        assert len(batch.records) == 1
        assert batch.records[0].payload["entries"] == []
    else:
        _assert_parser_seam_error(monkeypatch, (invalid,), code=expected_code)


def test_parser_output_group_marker_cannot_become_a_financial_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _captured_2024_report()
    marker = next(entry for entry in report.entries if isinstance(entry, SinaFzbGroupMarker))
    invalid_marker = replace(marker, raw_item_field="synthetic-value")
    invalid = replace(report, entries=(invalid_marker,))

    _assert_parser_seam_error(monkeypatch, (invalid,))


def test_runservice_node_cap_is_checked_across_the_complete_parser_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    padding = tuple((f"node_{index:04d}", index) for index in range(9_000))
    reports = tuple(
        replace(
            report,
            report_metadata=tuple(sorted((*report.report_metadata, *padding))),
        )
        for report in _parsed_reports()
    )

    _assert_parser_seam_error(monkeypatch, reports, code="limit_exceeded")


def test_runservice_text_bound_applies_after_the_larger_store_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _captured_2024_report()
    invalid = replace(report, report_metadata=(("long_but_storable", "x" * 4_097),))

    _assert_parser_seam_error(monkeypatch, (invalid,), code="limit_exceeded")


def test_store_rejects_secret_metadata_after_parser_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _captured_2024_report()
    invalid = replace(report, report_metadata=(("api_key", "synthetic-secret"),))

    _assert_parser_seam_error(
        monkeypatch,
        (invalid,),
        code="invalid_response",
        secret="synthetic-secret",
    )


def test_store_rejects_an_unencodable_source_scalar_at_the_public_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _captured_2024_report()
    invalid = replace(report, report_metadata=(("surrogate", "\ud800"),))

    _assert_parser_seam_error(monkeypatch, (invalid,), secret="\ud800")


def test_runservice_encoded_result_cap_rejects_a_whole_oversized_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    padding = tuple((f"bounded_text_{index:02d}", "x" * 4_000) for index in range(31))
    reports = tuple(
        replace(
            report,
            report_metadata=tuple(sorted((*report.report_metadata, *padding))),
        )
        for report in _parsed_reports()
    )

    _assert_parser_seam_error(monkeypatch, reports, code="limit_exceeded")


def test_malformed_body_is_sanitized_and_keeps_wire_cause() -> None:
    malformed = _assert_publication_error(
        "invalid_response",
        body=b"malformed-private-capture-content",
        secret="private-capture-content",
    )
    assert isinstance(malformed.__cause__, SinaFzbWireError)

    _assert_publication_error("invalid_input", body="not bytes")
    _assert_publication_error("invalid_input", body=bytearray(b"not bytes"))


def test_raw_body_and_wire_report_limits_reject_the_whole_capture() -> None:
    oversized_body = b" " * (MAX_RAW_RESPONSE_BYTES + 1)
    _assert_publication_error("limit_exceeded", body=oversized_body)

    document = _recorded_document()
    last_report = document["result"]["data"]["report_list"]["19981231"]
    last_report["publish_date"] = "19991301"
    result: object | None = None
    with pytest.raises(SinaFzbPublicationError) as raised:
        result = serialize_sina_fzb_response(
            _encode_document(document),
            query=_QUERY,
            retrieved_at_utc=_RETRIEVED_AT,
            capture_id=_CAPTURE_ID,
        )
    assert result is None
    assert raised.value.code == "invalid_response"
    assert isinstance(raised.value.__cause__, SinaFzbWireError)


def test_downstream_shape_or_credential_bounds_reject_without_a_prefix() -> None:
    oversized_text_document = _recorded_document()
    last_report = oversized_text_document["result"]["data"]["report_list"]["19981231"]
    last_report["oversized_metadata"] = "x" * 4097
    _assert_publication_error(
        "limit_exceeded",
        body=_encode_document(oversized_text_document),
    )

    sensitive_document = _recorded_document()
    last_report = sensitive_document["result"]["data"]["report_list"]["19981231"]
    last_report["api_key"] = "synthetic-private-token"
    _assert_publication_error(
        "invalid_response",
        body=_encode_document(sensitive_document),
        secret="synthetic-private-token",
    )


@pytest.mark.parametrize(
    ("metadata_key", "metadata_value", "code"),
    [
        ("", "empty-key", "invalid_response"),
        ("control\x00key", "value", "limit_exceeded"),
        ("k" * 257, "long-key", "limit_exceeded"),
        ("control-value", "value\x00with-control", "invalid_response"),
        ("remarks", "Bearer synthetic-test-token", "invalid_response"),
    ],
)
def test_source_metadata_key_and_value_policies_are_enforced(
    metadata_key: str, metadata_value: str, code: str
) -> None:
    document = _recorded_document()
    last_report = document["result"]["data"]["report_list"]["19981231"]
    last_report[metadata_key] = metadata_value

    _assert_publication_error(
        code,
        body=_encode_document(document),
        secret=metadata_value if "Bearer" in metadata_value else metadata_key or None,
    )


def test_nested_store_object_item_limit_rejects_the_complete_capture() -> None:
    document = _recorded_document()
    last_report = document["result"]["data"]["report_list"]["19981231"]
    last_report.update({f"source_extension_{index:05d}": index for index in range(10_001)})
    body = _encode_document(document)
    assert len(body) < MAX_RAW_RESPONSE_BYTES

    _assert_publication_error("limit_exceeded", body=body)


def test_store_text_limit_rejects_a_body_still_within_the_wire_cap() -> None:
    document = _recorded_document()
    last_report = document["result"]["data"]["report_list"]["19981231"]
    last_report["oversized_store_text"] = "x" * (256 * 1024 + 1)
    body = _encode_document(document)
    assert len(body) < MAX_RAW_RESPONSE_BYTES

    _assert_publication_error("limit_exceeded", body=body)


def test_single_record_encoded_size_limit_rejects_the_capture() -> None:
    document = _recorded_document()
    rows = document["result"]["data"]["report_list"]["20260630"]["data"]
    cash_row = next(row for row in rows if row.get("item_field") == "CURFDS")
    cash_row.update({f"synthetic_metadata_{index:02d}": "x" * 4_000 for index in range(66)})
    body = _encode_document(document)
    assert len(body) < MAX_RAW_RESPONSE_BYTES

    _assert_publication_error("limit_exceeded", body=body)


def test_source_row_limit_is_enforced_before_any_batch_is_returned() -> None:
    document = _recorded_document()
    rows = document["result"]["data"]["report_list"]["19981231"]["data"]
    rows.extend(
        copy.deepcopy(rows[-1]) for _ in range(MAX_SINA_FZB_ROWS_PER_REPORT + 1 - len(rows))
    )
    assert len(rows) == MAX_SINA_FZB_ROWS_PER_REPORT + 1

    _assert_publication_error("limit_exceeded", body=_encode_document(document))
