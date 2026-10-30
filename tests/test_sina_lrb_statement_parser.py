"""Synthetic offline coverage for the raw Sina LRB structure parser."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from opendata.data.providers.sina import lrb_statement_parser
from opendata.data.providers.sina.lrb_statement_parser import (
    MAX_SINA_LRB_JSON_DEPTH,
    MAX_SINA_LRB_RAW_RESPONSE_BYTES,
    MAX_SINA_LRB_REPORTS,
    MAX_SINA_LRB_ROWS_PER_REPORT,
    MAX_SINA_LRB_TOTAL_ROWS,
    SinaLrbRawArray,
    SinaLrbRawObject,
    SinaLrbWireError,
    parse_sina_lrb_response,
)


def _fixture_document() -> dict[str, Any]:
    """Return synthetic wire data with intentionally independent date and map keys."""
    return {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": "2",
                "report_date": [
                    {"date_value": "20301231", "date_description": "synthetic annual"},
                    {"date_value": "20300930", "date_description": "synthetic quarter"},
                ],
                "report_list": {
                    "wire-key-b": {
                        "source_note": "first synthetic report",
                        "data": [
                            {
                                "item_field": "TEST_REVENUE",
                                "item_title": 'synthetic title []{} "quote" \\ path',
                                "item_value": "00123.4500",
                                "extra": {"source_flag": True, "source_order": [2, 1]},
                            },
                            {
                                "item_value": None,
                                "item_title": "",
                                "item_field": "TEST_NULL",
                            },
                        ],
                    },
                    "wire-key-a": {
                        "data": [
                            {
                                "item_field": "TEST_DECIMAL",
                                "item_value": 12.3400,
                            }
                        ],
                        "source_note": "second synthetic report",
                    },
                },
                "provider_extension": {"keep": "uninterpreted"},
            },
            "status_extension": ["keep", "order"],
        },
        "document_extension": "preserved",
    }


def _encode(document: dict[str, Any]) -> bytes:
    """Encode synthetic JSON while retaining declared insertion order."""
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return body.replace(b'"item_value":12.34', b'"item_value":12.3400')


def _field(value: SinaLrbRawObject, name: str) -> Any:
    """Return one named field from the frozen test result."""
    return next(item for key, item in value.fields if key == name)


def _assert_error(body: bytes, code: str) -> None:
    """Assert the parser raises its fixed, value-free error code."""
    with pytest.raises(SinaLrbWireError) as raised:
        parse_sina_lrb_response(body)
    error = raised.value
    assert error.code == code
    assert error.args == (code,)
    assert error.__cause__ is None
    assert error.__context__ is None
    _assert_parser_frames_do_not_retain_body(error.__traceback__, body)


def _assert_parser_frames_do_not_retain_body(traceback: Any, body: bytes) -> None:
    """Inspect parser frames only; caller frames necessarily retain their input."""
    parser_frames = []
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_globals.get("__name__") == lrb_statement_parser.__name__:
            parser_frames.append(frame)
            frame_locals = frame.f_locals
            assert "body" not in frame_locals
            assert all(value is not body for value in frame_locals.values())
        traceback = traceback.tb_next
    assert parser_frames
    assert all(frame.f_code.co_name != "_parse_sina_lrb_response" for frame in parser_frames)


def test_parser_preserves_raw_fields_values_and_order_without_linking_collections() -> None:
    response = parse_sina_lrb_response(_encode(_fixture_document()))

    assert [_field(period, "date_value") for period in response.report_dates] == [
        "20301231",
        "20300930",
    ]
    assert [report.report_list_key for report in response.reports] == [
        "wire-key-b",
        "wire-key-a",
    ]
    assert [report.source_ordinal for report in response.reports] == [0, 1]
    assert {report.report_list_key for report in response.reports}.isdisjoint(
        {_field(period, "date_value") for period in response.report_dates}
    )

    first = response.reports[0]
    assert [key for key, _value in first.source_fields.fields] == ["source_note", "data"]
    assert len(first.rows) == 2
    first_row = first.rows[0]
    assert [key for key, _value in first_row.fields] == [
        "item_field",
        "item_title",
        "item_value",
        "extra",
    ]
    assert _field(first_row, "item_title") == 'synthetic title []{} "quote" \\ path'
    assert _field(first_row, "item_value") == "00123.4500"
    nested = _field(first_row, "extra")
    assert isinstance(nested, SinaLrbRawObject)
    assert [key for key, _value in nested.fields] == ["source_flag", "source_order"]
    assert _field(nested, "source_order") == SinaLrbRawArray(items=(2, 1))
    assert [key for key, _value in first.rows[1].fields] == [
        "item_value",
        "item_title",
        "item_field",
    ]
    assert _field(first.rows[1], "item_value") is None
    assert _field(first.rows[1], "item_title") == ""
    assert _field(response.source_document, "document_extension") == "preserved"

    second = response.reports[1]
    assert _field(second.rows[0], "item_value") == Decimal("12.3400")
    assert _field(response.source_document, "result") == SinaLrbRawObject(
        fields=(
            ("status", SinaLrbRawObject(fields=(("code", 0),))),
            (
                "data",
                SinaLrbRawObject(
                    fields=(
                        ("report_count", "2"),
                        (
                            "report_date",
                            SinaLrbRawArray(
                                items=(
                                    SinaLrbRawObject(
                                        fields=(
                                            ("date_value", "20301231"),
                                            ("date_description", "synthetic annual"),
                                        )
                                    ),
                                    SinaLrbRawObject(
                                        fields=(
                                            ("date_value", "20300930"),
                                            ("date_description", "synthetic quarter"),
                                        )
                                    ),
                                )
                            ),
                        ),
                        (
                            "report_list",
                            SinaLrbRawObject(
                                fields=(
                                    ("wire-key-b", first.source_fields),
                                    ("wire-key-a", second.source_fields),
                                )
                            ),
                        ),
                        (
                            "provider_extension",
                            SinaLrbRawObject(fields=(("keep", "uninterpreted"),)),
                        ),
                    )
                ),
            ),
            ("status_extension", SinaLrbRawArray(items=("keep", "order"))),
        )
    )


def test_zero_report_response_keeps_empty_arrays_and_objects() -> None:
    document = {
        "result": {
            "status": {"code": 0},
            "data": {"report_count": "0", "report_date": [], "report_list": {}},
        }
    }

    response = parse_sina_lrb_response(_encode(document))

    assert response.report_dates == ()
    assert response.reports == ()
    assert _field(_field(response.source_document, "result"), "data") == SinaLrbRawObject(
        fields=(
            ("report_count", "0"),
            ("report_date", SinaLrbRawArray(items=())),
            ("report_list", SinaLrbRawObject(fields=())),
        )
    )


@pytest.mark.parametrize(
    ("path", "replacement", "code"),
    [
        (("result", "status", "code"), 1, "invalid_envelope"),
        (("result", "data", "report_count"), "02", "invalid_envelope"),
        (("result", "data", "report_count"), "9" * 5_000, "limit_exceeded"),
        (("result", "data", "report_count"), "1", "invalid_identity"),
        (("result", "data", "report_date"), {}, "invalid_envelope"),
        (("result", "data", "report_list"), [], "invalid_envelope"),
    ],
)
def test_invalid_status_and_independent_collection_shapes_are_rejected(
    path: tuple[str, ...], replacement: Any, code: str
) -> None:
    document = _fixture_document()
    parent: dict[str, Any] = document
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = replacement

    _assert_error(_encode(document), code)


@pytest.mark.parametrize(
    "body",
    [
        b"\xff",
        b'{"result":',
        b'{"result":{"status":{"code":0,"code":0}}}',
        b'{"result":{"status":{"code":0},"data":{"x":NaN}}}',
    ],
)
def test_invalid_utf8_json_duplicate_keys_and_nonstandard_constants_fail_closed(
    body: bytes,
) -> None:
    _assert_error(body, "invalid_json")


def test_response_size_and_structural_depth_are_bounded() -> None:
    _assert_error(b" " * (MAX_SINA_LRB_RAW_RESPONSE_BYTES + 1), "limit_exceeded")
    nested = b"[" * (MAX_SINA_LRB_JSON_DEPTH + 1) + b"0" + b"]" * (MAX_SINA_LRB_JSON_DEPTH + 1)
    _assert_error(nested, "limit_exceeded")


def test_non_object_report_row_is_rejected() -> None:
    document = _fixture_document()
    first_report = next(iter(document["result"]["data"]["report_list"].values()))
    first_report["data"][0] = "row-shape-must-remain-an-object"

    _assert_error(_encode(document), "invalid_envelope")


def test_report_and_per_report_row_limits_are_enforced() -> None:
    too_many_reports = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": str(MAX_SINA_LRB_REPORTS + 1),
                "report_date": [],
                "report_list": {},
            },
        }
    }
    _assert_error(_encode(too_many_reports), "limit_exceeded")

    document = _fixture_document()
    first_report = next(iter(document["result"]["data"]["report_list"].values()))
    first_report["data"] = [{}] * (MAX_SINA_LRB_ROWS_PER_REPORT + 1)
    _assert_error(_encode(document), "limit_exceeded")


def test_total_row_limit_is_enforced_across_report_list_entries() -> None:
    report_count = MAX_SINA_LRB_REPORTS // 2 + 1
    report_dates = [{"source_position": ordinal} for ordinal in range(report_count)]
    report_list = {
        f"synthetic-key-{ordinal}": {
            "data": [{}]
            * (
                MAX_SINA_LRB_ROWS_PER_REPORT
                if ordinal < report_count - 1
                else MAX_SINA_LRB_TOTAL_ROWS - MAX_SINA_LRB_ROWS_PER_REPORT * (report_count - 1) + 1
            )
        }
        for ordinal in range(report_count)
    }
    document = {
        "result": {
            "status": {"code": 0},
            "data": {
                "report_count": str(report_count),
                "report_date": report_dates,
                "report_list": report_list,
            },
        }
    }

    _assert_error(_encode(document), "limit_exceeded")


def test_required_envelope_fields_are_not_silently_defaulted() -> None:
    _assert_error(b'{"result":{"status":{"code":0},"data":{}}}', "invalid_envelope")


def test_unexpected_parser_failure_is_mapped_without_retaining_source_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_body: bytes) -> Any:
        raise RuntimeError("synthetic internal detail")

    monkeypatch.setattr(lrb_statement_parser, "_parse_sina_lrb_response", fail)

    _assert_error(b"synthetic private body", "parser_failed")


def test_process_signal_is_reraised_without_parser_body_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(_body: bytes) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setattr(lrb_statement_parser, "_parse_sina_lrb_response", interrupt)
    body = b"synthetic private body"

    with pytest.raises(KeyboardInterrupt) as raised:
        parse_sina_lrb_response(body)

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    _assert_parser_frames_do_not_retain_body(raised.value.__traceback__, body)
