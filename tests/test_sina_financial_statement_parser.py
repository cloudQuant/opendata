"""Recorded-response and fail-closed tests for the source-native Sina FZB parser."""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from opendata.data.providers.sina.financial_statement_contract import (
    MAX_RAW_RESPONSE_BYTES,
    SinaFzbGroupMarker,
    SinaFzbItem,
    SinaFzbQuery,
    SinaFzbWireError,
)
from opendata.data.providers.sina.financial_statement_parser import (
    _parse_sina_fzb_response,
    parse_sina_fzb_response,
)

_CAPTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures/upstream/financial_statement/responses.json.gz"
)
_CAPTURE_SHA256 = "fd72f5ec732bb17f2fb7b2c757863c5ded216b57a423a2dcd3488a372af4a0b6"
_QUERY = SinaFzbQuery(symbol="600519.SH")


@lru_cache(maxsize=1)
def _recorded_capture() -> tuple[dict[str, Any], bytes]:
    """Read the pinned outer recording and decode its original response bytes."""
    archive = _CAPTURE_PATH.read_bytes()
    assert hashlib.sha256(archive).hexdigest() == _CAPTURE_SHA256
    calls = json.loads(gzip.decompress(archive))
    assert isinstance(calls, list) and len(calls) == 1
    call = calls[0]
    assert isinstance(call, dict)
    body = base64.b64decode(call["content_b64"], validate=True)
    return call, body


def _recorded_document() -> dict[str, Any]:
    """Return a fresh JSON object decoded from the recorded response."""
    _call, body = _recorded_capture()
    document = json.loads(body)
    assert isinstance(document, dict)
    return document


def _data(document: dict[str, Any]) -> dict[str, Any]:
    return document["result"]["data"]


def _first_report_wire(document: dict[str, Any]) -> dict[str, Any]:
    return _data(document)["report_list"]["20260630"]


def _encode(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _encode_escaped_text(document: dict[str, Any]) -> bytes:
    """Encode synthetic lone surrogates as JSON escapes for parser-boundary tests."""
    return json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _assert_wire_error(body: bytes, code: str) -> None:
    with pytest.raises(SinaFzbWireError) as raised:
        parse_sina_fzb_response(body, query=_QUERY)
    assert raised.value.code == code


def _assert_document_error(document: dict[str, Any], code: str) -> None:
    _assert_wire_error(_encode(document), code)


def _row_by_id(report: Any, source_item_id: str) -> SinaFzbItem:
    return next(
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbItem) and entry.source_item_id == source_item_id
    )


def test_recorded_response_preserves_source_native_facts_and_order() -> None:
    call, body = _recorded_capture()
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

    reports = parse_sina_fzb_response(body, query=_QUERY)
    assert len(reports) == 103
    assert [report.report_period for report in reports[:2]] == [
        date(2026, 6, 30),
        date(2026, 3, 31),
    ]
    assert all(report.symbol == "600519.SH" for report in reports)

    first, second = reports[:2]
    asset = _row_by_id(first, "TOTASSET")
    assert asset.source_title == "资产总计"
    assert asset.raw_value == "309050784569.310000"
    assert asset.value_state == "present"
    assert first.published_on == date(2026, 8, 15)
    assert first.publication_precision == "date"
    assert first.source_update_epoch_seconds == 1786711808
    assert first.source_updated_at == datetime.fromtimestamp(1786711808, ZoneInfo("Asia/Shanghai"))
    assert first.source_updated_at.tzinfo == ZoneInfo("Asia/Shanghai")
    assert first.currency == "CNY"
    assert dict(first.report_metadata)["data_source"] == "定期报告"

    second_asset = _row_by_id(second, "TOTASSET")
    assert second_asset.raw_value == "319918844905.580000"
    assert second.published_on == date(2026, 4, 25)
    assert second.source_update_epoch_seconds == 1777029604

    null_item = _row_by_id(first, "SETTRESEDEPO")
    assert null_item.raw_value is None
    assert null_item.value_state == "null"

    markers = [entry for entry in first.entries if isinstance(entry, SinaFzbGroupMarker)]
    assert len(markers) == 5
    assert [marker.source_group_no for marker in markers] == [1, 2, 3, 4, 5]
    # These are exact source labels from the recording, not canonical domain categories.
    assert [marker.source_title for marker in markers] == [
        "流动资产",
        "非流动资产",
        "流动负债",
        "非流动负债",
        "所有者权益",
    ]
    assert [marker.source_ordinals for marker in markers] == [(0,), (30,), (67,), (106,), (122,)]
    assert all(marker.raw_item_field == "" and marker.raw_item_value == "" for marker in markers)
    financial_items = [entry for entry in first.entries if isinstance(entry, SinaFzbItem)]
    assert len(financial_items) == len(first.entries) - len(markers)
    assert all(not isinstance(marker, SinaFzbItem) for marker in markers)

    duplicate = _row_by_id(first, "DOMETICKSETT")
    assert duplicate.source_ordinals == (96, 97)
    assert duplicate.value_state == "null"


def test_empty_item_value_remains_an_empty_source_state() -> None:
    document = _recorded_document()
    report_rows = _first_report_wire(document)["data"]
    row = dict(report_rows[1])
    row["item_field"] = "SYNTHETIC_EMPTY_VALUE"
    row["item_value"] = ""
    report_rows.append(row)

    reports = parse_sina_fzb_response(_encode(document), query=_QUERY)
    item = _row_by_id(reports[0], "SYNTHETIC_EMPTY_VALUE")
    assert item.raw_value == ""
    assert item.value_state == "empty"


def test_non_numeric_item_text_is_preserved_without_conversion() -> None:
    document = _recorded_document()
    report_rows = _first_report_wire(document)["data"]
    report_rows[1]["item_value"] = "source says: not a numeric measurement"

    item = _row_by_id(parse_sina_fzb_response(_encode(document), query=_QUERY)[0], "CURFDS")
    assert item.raw_value == "source says: not a numeric measurement"
    assert item.value_state == "present"


@pytest.mark.parametrize(
    ("symbol", "code"),
    [
        ("600519", "invalid_query"),
        ("600519.sh", "invalid_query"),
        (" 600519.SH", "invalid_query"),
        ("600519.SSE", "invalid_query"),
        ("６００５１９.SH", "invalid_query"),
    ],
)
def test_query_requires_the_canonical_six_digit_exchange_symbol(symbol: str, code: str) -> None:
    with pytest.raises(SinaFzbWireError) as raised:
        SinaFzbQuery(symbol=symbol)
    assert raised.value.code == code
    assert symbol not in str(raised.value)


def test_query_accepts_canonical_shenzhen_symbol_syntax() -> None:
    assert SinaFzbQuery(symbol="000001.SZ").symbol == "000001.SZ"


def test_direct_parser_enforces_raw_size_limit() -> None:
    _assert_wire_error(b" " * (MAX_RAW_RESPONSE_BYTES + 1), "limit_exceeded")


def test_structural_nesting_is_bounded_before_json_decoding() -> None:
    deeply_nested = b"[" * 33 + b"0" + b"]" * 33
    _assert_wire_error(deeply_nested, "limit_exceeded")


def test_bracket_characters_and_escaped_quotes_inside_strings_are_not_depth() -> None:
    document = _recorded_document()
    title = "wire text with bracket tokens " + "[]{}" * 20 + " quote " + chr(34) + chr(92)
    _first_report_wire(document)["data"][1]["item_title"] = title

    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]
    assert _row_by_id(report, "CURFDS").source_title == title


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b'{"result":',
        b'{"result":{"status":{"code":0,"code":0}}}',
        b'{"result":{"extension":NaN}}',
        b'{"result":{"extension":Infinity}}',
        b'{"result":{"extension":-Infinity}}',
        b'{"result":{"extension":1e999}}',
    ],
)
def test_invalid_utf8_json_duplicates_and_non_finite_constants_are_rejected(body: bytes) -> None:
    _assert_wire_error(body, "invalid_json")


@pytest.mark.parametrize(
    ("path", "value", "delete", "code"),
    [
        (("result", "status", "code"), True, False, "invalid_envelope"),
        (("result", "status", "code"), None, True, "invalid_envelope"),
        (("result", "data", "report_count"), "0103", False, "invalid_envelope"),
        (("result", "data", "report_count"), "102", False, "invalid_identity"),
        (("result", "data", "report_count"), 103, False, "invalid_envelope"),
        (("result", "data", "report_date"), None, True, "invalid_envelope"),
        (("result", "data", "report_list"), None, True, "invalid_envelope"),
    ],
)
def test_required_envelope_fields_and_canonical_count_are_strict(
    path: tuple[str, ...], value: Any, delete: bool, code: str
) -> None:
    document = _recorded_document()
    parent = document
    for key in path[:-1]:
        parent = parent[key]
    if delete:
        del parent[path[-1]]
    else:
        parent[path[-1]] = value
    _assert_document_error(document, code)


def test_invalid_status_envelope_is_not_treated_as_empty_data() -> None:
    _assert_wire_error(b"{}", "invalid_envelope")
    _assert_wire_error(b'{"result":{"status":{"code":0}}}', "invalid_envelope")


def test_each_envelope_level_requires_its_source_json_container_type() -> None:
    _assert_wire_error(b"[]", "invalid_envelope")
    _assert_wire_error(b'{"result":[]}', "invalid_envelope")

    wrong_status = _recorded_document()
    wrong_status["result"]["status"] = []
    _assert_document_error(wrong_status, "invalid_envelope")

    wrong_data = _recorded_document()
    wrong_data["result"]["data"] = []
    _assert_document_error(wrong_data, "invalid_envelope")

    wrong_dates = _recorded_document()
    _data(wrong_dates)["report_date"] = {}
    _assert_document_error(wrong_dates, "invalid_envelope")

    wrong_report_map = _recorded_document()
    _data(wrong_report_map)["report_list"] = []
    _assert_document_error(wrong_report_map, "invalid_envelope")

    wrong_period = _recorded_document()
    _data(wrong_period)["report_date"][0] = None
    _assert_document_error(wrong_period, "invalid_envelope")

    wrong_report = _recorded_document()
    _data(wrong_report)["report_list"]["20260630"] = None
    _assert_document_error(wrong_report, "invalid_envelope")

    wrong_row = _recorded_document()
    _first_report_wire(wrong_row)["data"][1] = []
    _assert_document_error(wrong_row, "invalid_envelope")


def test_date_list_and_report_map_must_be_a_unique_exact_match() -> None:
    duplicate_date = _recorded_document()
    _data(duplicate_date)["report_date"][1]["date_value"] = "20260630"
    _assert_document_error(duplicate_date, "invalid_identity")

    missing_period = _recorded_document()
    del _data(missing_period)["report_list"]["20260630"]
    _assert_document_error(missing_period, "invalid_identity")

    extra_period = _recorded_document()
    extra_report = dict(_data(extra_period)["report_list"]["20260630"])
    _data(extra_period)["report_list"]["19000101"] = extra_report
    _assert_document_error(extra_period, "invalid_identity")


def test_period_publication_and_update_time_are_validated_independently() -> None:
    invalid_period = _recorded_document()
    _data(invalid_period)["report_date"][0]["date_value"] = "20260230"
    _assert_document_error(invalid_period, "invalid_date")

    invalid_period_format = _recorded_document()
    _data(invalid_period_format)["report_date"][0]["date_value"] = "2026063"
    _assert_document_error(invalid_period_format, "invalid_date")

    invalid_publication = _recorded_document()
    _first_report_wire(invalid_publication)["publish_date"] = "20261301"
    _assert_document_error(invalid_publication, "invalid_date")

    for update_time in (True, 0, -1, 1786711808.0, "1786711808"):
        invalid_update = _recorded_document()
        _first_report_wire(invalid_update)["update_time"] = update_time
        _assert_document_error(invalid_update, "invalid_value")


def test_invalid_and_nested_source_metadata_fails_closed() -> None:
    invalid_currency = _recorded_document()
    _first_report_wire(invalid_currency)["rCurrency"] = ""
    _assert_document_error(invalid_currency, "invalid_value")

    nested_report_metadata = _recorded_document()
    _first_report_wire(nested_report_metadata)["unreviewed_extension"] = {"nested": "value"}
    _assert_document_error(nested_report_metadata, "invalid_envelope")

    nested_period_metadata = _recorded_document()
    _data(nested_period_metadata)["report_date"][0]["future_metadata"] = ["nested"]
    _assert_document_error(nested_period_metadata, "invalid_envelope")

    nested_item_metadata = _recorded_document()
    _first_report_wire(nested_item_metadata)["data"][1]["future_metadata"] = {"nested": 1}
    _assert_document_error(nested_item_metadata, "invalid_envelope")


def test_unknown_scalar_source_metadata_is_preserved() -> None:
    document = _recorded_document()
    _data(document)["report_date"][0]["future_period_flag"] = 7
    report_wire = _first_report_wire(document)
    report_wire["future_report_flag"] = False
    report_wire["data"][1]["future_item_flag"] = "kept verbatim"

    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]
    assert dict(report.period_metadata)["future_period_flag"] == 7
    assert dict(report.report_metadata)["future_report_flag"] is False
    assert dict(_row_by_id(report, "CURFDS").source_metadata)["future_item_flag"] == "kept verbatim"


def test_json_null_source_metadata_remains_a_json_null() -> None:
    document = _recorded_document()
    _first_report_wire(document)["data"][1]["future_item_flag"] = None

    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]

    assert dict(_row_by_id(report, "CURFDS").source_metadata)["future_item_flag"] is None


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("title", "invalid_value"),
        ("metadata_value", "invalid_value"),
        ("metadata_key", "invalid_json"),
    ],
)
def test_escaped_lone_surrogates_are_rejected_at_text_boundaries(location: str, code: str) -> None:
    document = _recorded_document()
    unpaired_surrogate = chr(0xD800)
    if location == "title":
        _first_report_wire(document)["data"][1]["item_title"] = unpaired_surrogate
    elif location == "metadata_value":
        _first_report_wire(document)["data"][1]["future_text"] = unpaired_surrogate
    else:
        _data(document)["report_date"][0][unpaired_surrogate] = "future metadata key"

    _assert_wire_error(_encode_escaped_text(document), code)


@pytest.mark.parametrize(
    ("field", "value", "delete", "code"),
    [
        ("item_field", None, True, "invalid_envelope"),
        ("item_field", "   ", False, "invalid_value"),
        ("item_title", "", False, "invalid_value"),
        ("item_value", None, True, "invalid_value"),
        ("item_value", 319918844905, False, "invalid_value"),
        ("item_value", [], False, "invalid_value"),
    ],
)
def test_line_identity_title_and_value_contract_is_strict(
    field: str, value: Any, delete: bool, code: str
) -> None:
    document = _recorded_document()
    row = _first_report_wire(document)["data"][1]
    if delete:
        del row[field]
    else:
        row[field] = value
    _assert_document_error(document, code)


def test_wire_errors_do_not_echo_source_values_or_response_content() -> None:
    document = _recorded_document()
    row = _first_report_wire(document)["data"][1]
    secret = "synthetic-private-upstream-value"
    row["item_title"] = secret
    row["item_value"] = []

    with pytest.raises(SinaFzbWireError) as raised:
        parse_sina_fzb_response(_encode(document), query=_QUERY)

    assert raised.value.code == "invalid_value"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_exact_duplicate_item_collapses_but_raw_value_conflicts_reject_all_reports() -> None:
    reports = parse_sina_fzb_response(_recorded_capture()[1], query=_QUERY)
    first = reports[0]
    duplicate = _row_by_id(first, "DOMETICKSETT")
    assert duplicate.source_ordinals == (96, 97)

    conflicting_value = _recorded_document()
    _first_report_wire(conflicting_value)["data"][97]["item_value"] = "changed source text"
    _assert_document_error(conflicting_value, "duplicate_conflict")

    conflicting_metadata = _recorded_document()
    _first_report_wire(conflicting_metadata)["data"][97]["unreviewed_flag"] = "changed"
    _assert_document_error(conflicting_metadata, "duplicate_conflict")


def test_same_display_title_with_distinct_source_ids_stays_distinct() -> None:
    document = _recorded_document()
    rows = _first_report_wire(document)["data"]
    second = dict(rows[1])
    second["item_field"] = "SYNTHETIC_DISTINCT_SOURCE_ID"
    rows.append(second)

    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]
    same_title = [
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbItem) and entry.source_title == "货币资金"
    ]
    assert [entry.source_item_id for entry in same_title] == [
        "CURFDS",
        "SYNTHETIC_DISTINCT_SOURCE_ID",
    ]
    assert [entry.source_ordinals for entry in same_title] == [(1,), (141,)]


@pytest.mark.parametrize(
    ("left", "right"),
    [(True, 1), (1, 1.0), (-0.0, 0.0)],
)
def test_duplicate_comparison_keeps_json_scalar_types_distinct(left: object, right: object) -> None:
    document = _recorded_document()
    rows = _first_report_wire(document)["data"]
    rows[1]["future_scalar"] = left
    duplicate = dict(rows[1])
    duplicate["future_scalar"] = right
    rows.append(duplicate)
    _assert_document_error(document, "duplicate_conflict")


def test_duplicate_group_marker_collapses_with_every_original_position() -> None:
    document = _recorded_document()
    rows = _first_report_wire(document)["data"]
    rows.append(dict(rows[0]))

    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]
    markers = [
        entry
        for entry in report.entries
        if isinstance(entry, SinaFzbGroupMarker) and entry.source_group_no == 1
    ]
    assert len(markers) == 1
    assert markers[0].source_ordinals == (0, 141)
    assert markers[0].raw_item_field == ""
    assert markers[0].raw_item_value == ""


def test_conflicting_duplicate_group_marker_rejects_the_response() -> None:
    document = _recorded_document()
    rows = _first_report_wire(document)["data"]
    conflict = dict(rows[0])
    conflict["future_group_flag"] = "changed row"
    rows.append(conflict)
    _assert_document_error(document, "duplicate_conflict")


@pytest.mark.parametrize(
    ("field", "value", "delete"),
    [
        ("item_title", "unknown heading", False),
        ("item_group_no", True, False),
        ("item_display_type", 2, False),
        ("item_display", "小类", False),
        ("item_value", None, False),
        ("item_source", "other", False),
        ("item_precision", "f3", False),
        ("item_tongbi", "not-empty", False),
        ("item_precision", None, True),
    ],
)
def test_empty_identity_requires_an_exact_recorded_group_marker(
    field: str, value: Any, delete: bool
) -> None:
    document = _recorded_document()
    row = _first_report_wire(document)["data"][0]
    if delete:
        del row[field]
    else:
        row[field] = value
    _assert_document_error(document, "invalid_value")


def test_report_and_per_report_row_limits_are_enforced() -> None:
    too_many_reports = _recorded_document()
    data = _data(too_many_reports)
    source_report = _first_report_wire(too_many_reports)
    source_item = dict(source_report["data"][1])
    periods: list[dict[str, Any]] = []
    report_list: dict[str, Any] = {}
    first_period = date(2000, 1, 1)
    for offset in range(257):
        period = (first_period + timedelta(days=offset)).strftime("%Y%m%d")
        periods.append({"date_value": period, "future_period_flag": offset})
        report = dict(source_report)
        report["publish_date"] = period
        report["data"] = [dict(source_item)]
        report_list[period] = report
    data["report_count"] = "257"
    data["report_date"] = periods
    data["report_list"] = report_list
    _assert_document_error(too_many_reports, "limit_exceeded")

    too_many_rows = _recorded_document()
    report_rows = _first_report_wire(too_many_rows)["data"]
    prototype = dict(report_rows[1])
    report_rows[:] = [{**prototype, "item_field": f"SYNTHETIC_ROW_{index}"} for index in range(513)]
    _assert_document_error(too_many_rows, "limit_exceeded")


def test_late_invalid_row_prevents_a_partial_report_set_from_returning() -> None:
    document = _recorded_document()
    last_period = _data(document)["report_date"][-1]["date_value"]
    last_report = _data(document)["report_list"][last_period]
    last_report["data"][0]["item_display"] = "not a recorded marker"
    _assert_document_error(document, "invalid_value")


@pytest.mark.parametrize(
    ("pointer", "expected", "mismatch", "wrong_type"),
    [
        ("paperCode", "sh600519", "sz600519", 600519),
        ("source", "fzb", "other", 1),
        ("type", "0", "1", 0),
    ],
)
def test_optional_identity_echo_is_checked_at_its_exact_data_pointer(
    pointer: str, expected: object, mismatch: object, wrong_type: object
) -> None:
    matching = _recorded_document()
    _data(matching)[pointer] = expected
    parse_sina_fzb_response(_encode(matching), query=_QUERY)

    conflicting = _recorded_document()
    _data(conflicting)[pointer] = mismatch
    _assert_document_error(conflicting, "invalid_identity")

    wrong_type_document = _recorded_document()
    _data(wrong_type_document)[pointer] = wrong_type
    _assert_document_error(wrong_type_document, "invalid_identity")


def test_identity_like_report_metadata_is_not_recursively_promoted() -> None:
    document = _recorded_document()
    _first_report_wire(document)["source"] = "descriptive report metadata"
    report = parse_sina_fzb_response(_encode(document), query=_QUERY)[0]
    assert dict(report.report_metadata)["source"] == "descriptive report metadata"


def test_object_hook_propagates_lease_loss_instead_of_invalid_json() -> None:
    """A lease error raised while constructing a decoded object keeps its identity."""
    body = b'{"result":{"status":{"code":0}}}'
    expected = ValueError("lease lost in object_pairs_hook")
    checkpoint_calls = 0

    def checkpoint() -> None:
        nonlocal checkpoint_calls
        checkpoint_calls += 1
        if checkpoint_calls == 2:
            raise expected

    with pytest.raises(ValueError) as raised:
        _parse_sina_fzb_response(body, query=_QUERY, checkpoint=checkpoint)

    assert raised.value is expected
    assert checkpoint_calls == 2
    assert not isinstance(raised.value, SinaFzbWireError)
