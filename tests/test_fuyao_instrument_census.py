"""Synthetic offline behavior tests for the Fuyao instrument census adapter."""

from __future__ import annotations

import hashlib
import itertools
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone, tzinfo
from decimal import Decimal

import pytest

from opendata.data.instrument_source_census import (
    FieldObservationState,
    InstrumentSourceCensusError,
    InstrumentSourceObservation,
    PageAttemptStatus,
    PageReceipt,
    PaginationChainStatus,
    PaginationReason,
    ReportedTotalBasis,
)
from opendata.pipeline import fuyao_instrument_census as census_module
from opendata.pipeline.fuyao_instrument_census import (
    FuyaoInstrumentCensusCapture,
    FuyaoInstrumentCensusError,
    FuyaoInstrumentPageInput,
    build_fuyao_instrument_census,
)

_OBSERVED_AT = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)


def _body(
    rows: list[object],
    *,
    code: int = 0,
    data_overrides: dict[str, object] | None = None,
) -> bytes:
    """Build a synthetic Fuyao-shaped success envelope for offline tests."""
    data: dict[str, object] = {"timestamp": 1_798_320_000_000, "item": rows}
    if data_overrides:
        data.update(data_overrides)
    payload = {"code": code, "message": "synthetic", "request_id": "local", "data": data}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _page(
    body: bytes | None = None,
    *,
    sequence: int = 1,
    status: PageAttemptStatus | str = PageAttemptStatus.SUCCEEDED,
    request_summary: dict[str, object] | None = None,
    source_page_number: int | str | None = None,
    http_status_code: int | None = None,
    request_cursor: str | None = None,
    next_cursor: str | None = None,
    terminal: bool | None = None,
    reported_total: int | None = None,
    failure_code: str | None = None,
    declared_sha256: str | None = None,
) -> FuyaoInstrumentPageInput:
    """Create a page input and hash the exact synthetic response bytes."""
    digest = declared_sha256
    if body is not None and digest is None:
        digest = hashlib.sha256(body).hexdigest()
    status_code = http_status_code
    if status_code is None:
        try:
            normalized_status = PageAttemptStatus(status)
        except (TypeError, ValueError):
            normalized_status = None
        if normalized_status is PageAttemptStatus.SUCCEEDED:
            status_code = 200
    return FuyaoInstrumentPageInput(
        sequence_number=sequence,
        raw_response_body=body,
        declared_response_sha256=digest,
        request_summary=request_summary,
        source_page_number=source_page_number,
        http_status_code=status_code,
        request_cursor=request_cursor,
        next_cursor=next_cursor,
        terminal=terminal,
        reported_total=reported_total,
        attempt_status=status,
        failure_code=failure_code,
    )


def test_success_preserves_raw_rows_and_separates_hash_meanings() -> None:
    row = {
        "thscode": "600519.SH",
        "ticker": "600519",
        "exchange": "source-token-no-inference",
        "asset_type": "source-token-no-inference",
        "status": "source-token-no-inference",
        "list_date": "source-date-token",
        "numeric_text": "00123",
        "native_number": 12.5,
        "extra": {"arbitrary": [True, None, "保留"]},
    }
    body = _body([row], data_overrides={"reported_total": "not-canonicalized"})
    capture = build_fuyao_instrument_census(
        [
            _page(
                body,
                request_summary={
                    "method": "GET",
                    "path": "/api/meta/tickers/list",
                    "params": {"asset_type": "a-share", "limit": 10_000, "offset": 0},
                },
                request_cursor="0",
                terminal=True,
            )
        ],
        observed_at=_OBSERVED_AT,
    )

    page = capture.source_census.pages[0]
    observation = page.rows[0]
    expected_summary = (
        b'{"method":"GET","params":{"asset_type":"a-share","limit":10000,"offset":0},'
        b'"path":"/api/meta/tickers/list"}'
    )

    expected_frozen_row = {
        **row,
        "extra": {"arbitrary": (True, None, "保留")},
    }
    assert dict(observation.raw_row) == expected_frozen_row
    assert dict(observation.raw_row["extra"])["arbitrary"] == (True, None, "保留")
    assert observation.market.state is FieldObservationState.UNMAPPED
    assert observation.security_type.state is FieldObservationState.UNMAPPED
    assert observation.status.state is FieldObservationState.UNMAPPED
    assert observation.listing_date.state is FieldObservationState.UNMAPPED
    assert page.reported_total is None
    assert page.reported_total_basis is None
    assert page.receipt is not None
    assert page.receipt.request_sha256 is None
    assert page.receipt.response_sha256 == hashlib.sha256(body).hexdigest()
    assert capture.request_summary_sha256s == (hashlib.sha256(expected_summary).hexdigest(),)
    assert capture.response_body_sha256s == (hashlib.sha256(body).hexdigest(),)
    assert capture.http_status_codes == (200,)
    assert capture.raw_response_bodies == (body,)
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.REPORTED_TOTAL_MISSING in capture.pagination.reasons


def test_decimal_token_precision_boundary_keeps_exact_bytes_as_source_record() -> None:
    body = b'{"code":0,"data":{"item":[{"decimal":0.1234567890123456789}]}}'

    capture = build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)
    decoded_value = capture.source_census.pages[0].rows[0].raw_row["decimal"]

    assert type(decoded_value) is float
    assert Decimal(str(decoded_value)) != Decimal("0.1234567890123456789")
    assert capture.raw_response_bodies == (body,)


def test_caller_total_keeps_unknown_basis_and_unknown_pagination() -> None:
    body = _body([{"code": "600519"}])
    capture = build_fuyao_instrument_census(
        [_page(body, reported_total=1, terminal=True)], observed_at=_OBSERVED_AT
    )

    page = capture.source_census.pages[0]
    assert page.reported_total == 1
    assert page.reported_total_basis is ReportedTotalBasis.UNKNOWN
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.REPORTED_TOTAL_BASIS_UNKNOWN in capture.pagination.reasons
    assert PaginationReason.PAGE_RECEIPT_MISSING in capture.pagination.reasons


def test_empty_success_page_is_not_translated_into_a_failed_page() -> None:
    capture = build_fuyao_instrument_census([_page(_body([]))], observed_at=_OBSERVED_AT)

    page = capture.source_census.pages[0]
    assert page.attempt_status is PageAttemptStatus.SUCCEEDED
    assert page.rows == ()
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.EMPTY_PAGE in capture.pagination.reasons


def test_failed_page_has_no_valid_rows_even_if_error_body_is_retained() -> None:
    error_body = b"not a successful JSON response"
    capture = build_fuyao_instrument_census(
        [
            _page(
                error_body,
                status=PageAttemptStatus.FAILED,
                http_status_code=502,
                failure_code="caller_timeout",
                request_cursor="10000",
            )
        ],
        observed_at=_OBSERVED_AT,
    )

    page = capture.source_census.pages[0]
    assert page.attempt_status is PageAttemptStatus.FAILED
    assert page.failure_code == "caller_timeout"
    assert page.rows == ()
    assert page.receipt is None
    assert capture.raw_response_bodies == (error_body,)
    assert capture.response_body_sha256s == (hashlib.sha256(error_body).hexdigest(),)
    assert capture.http_status_codes == (502,)
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.PAGE_ATTEMPT_FAILED in capture.pagination.reasons


def test_unknown_attempt_carries_no_body_or_paging_claims() -> None:
    capture = build_fuyao_instrument_census(
        [
            _page(
                status=PageAttemptStatus.UNKNOWN,
                request_summary={"path": "/api/meta/tickers/list", "offset": 0},
            )
        ],
        observed_at=_OBSERVED_AT,
    )

    page = capture.source_census.pages[0]
    assert page.attempt_status is PageAttemptStatus.UNKNOWN
    assert page.rows == ()
    assert capture.response_body_sha256s == (None,)
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.PAGE_ATTEMPT_UNKNOWN in capture.pagination.reasons


def test_duplicate_raw_source_rows_are_preserved_for_dto_assessment() -> None:
    row = {"code": "600519", "exchange": "unmapped"}
    capture = build_fuyao_instrument_census([_page(_body([row, row]))], observed_at=_OBSERVED_AT)

    assert len(capture.source_census.pages[0].rows) == 2
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.DUPLICATE_SOURCE_ROW in capture.pagination.reasons


def test_multiple_pages_are_preserved_in_capture_order_and_sequence_gaps_stay_unknown() -> None:
    first = _body([{"code": "first"}])
    last = _body([{"code": "last"}])
    capture = build_fuyao_instrument_census(
        [
            _page(first, sequence=1, request_cursor="0", next_cursor="10000", terminal=False),
            _page(last, sequence=3, request_cursor="10000", terminal=True),
        ],
        observed_at=_OBSERVED_AT,
    )

    assert [page.sequence_number for page in capture.source_census.pages] == [1, 3]
    assert [
        dict(row.raw_row)["code"] for page in capture.source_census.pages for row in page.rows
    ] == [
        "first",
        "last",
    ]
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.PAGE_SEQUENCE_GAP in capture.pagination.reasons


def test_empty_page_list_is_recorded_as_unknown_not_complete() -> None:
    capture = build_fuyao_instrument_census([], observed_at=_OBSERVED_AT)

    assert capture.source_census.pages == ()
    assert capture.pagination.chain_status is PaginationChainStatus.UNKNOWN
    assert capture.pagination.reasons == (PaginationReason.NO_PAGES,)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"\xff", "response_utf8_invalid"),
        (b"", "response_body_empty"),
        (b"[]", "response_root_invalid"),
        (b"{", "response_json_invalid"),
        (b"]", "response_json_invalid"),
        (b'{"code":0 "data":{}}', "response_json_invalid"),
        (b'{"code":0,"data":{"item":[{"unterminated":"value}]}}', "response_json_invalid"),
        (b'{"code":0,"code":0,"data":{"item":[]}}', "response_json_duplicate_key"),
        (
            b'{"code":0,"data":{"item":[{"value":1,"value":2}]}}',
            "response_json_duplicate_key",
        ),
        (b'{"code":0,"data":{"item":[{"value":NaN}]}}', "response_json_nonfinite_number"),
        (
            b'{"code":0,"data":{"item":[{"value":Infinity}]}}',
            "response_json_nonfinite_number",
        ),
        (
            b'{"code":0,"data":{"item":[{"value":1e999}]}}',
            "response_json_nonfinite_number",
        ),
        (
            (b'{"code":0,"data":{"item":[{"value":') + b"9" * 1300 + b"}]}}",
            "response_json_integer_too_large",
        ),
        (b'{"code":false,"data":{"item":[]}}', "response_code_invalid"),
        (b'{"code":5,"data":{"item":[]}}', "response_not_successful"),
        (b'{"code":0,"data":null}', "response_data_invalid"),
        (b'{"code":0,"data":{}}', "response_items_invalid"),
        (b'{"code":0,"data":{"item":null}}', "response_items_invalid"),
        (b'{"code":0,"data":{"item":[null]}}', "response_row_invalid"),
        (b'{"code":0,"data":{"item":[{}]}}', "response_row_invalid"),
    ],
)
def test_invalid_response_bytes_fail_with_stable_codes(body: bytes, code: str) -> None:
    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == code
    assert str(raised.value) == code


def test_nested_json_depth_is_rejected_before_json_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "_MAX_JSON_DEPTH", 3)
    body = b'{"code":0,"data":{"item":[{"x":[[[[]]]]}]}}'

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_json_depth_exceeded"


def test_json_node_limit_is_checked_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "_MAX_JSON_NODES", 2)

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(_body([{"code": "x"}]))], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_json_nodes_exceeded"


def test_response_body_limit_is_checked_before_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "MAX_FUYAO_CENSUS_BODY_BYTES", 3)
    body = b"1234"

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_body_too_large"


def test_row_parser_rechecks_its_response_body_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "MAX_FUYAO_CENSUS_BODY_BYTES", 3)

    with pytest.raises(FuyaoInstrumentCensusError, match="response_body_too_large"):
        census_module._parse_rows(b"1234", max_rows=1)


def test_success_requires_a_caller_declared_http_success_status() -> None:
    body = _body([{"code": "600519"}])

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        _page(body, http_status_code=503)

    assert raised.value.code == "successful_http_status_invalid"


def test_page_input_repr_hides_raw_body_and_request_summary() -> None:
    body = b"private response body"
    page = _page(
        body,
        request_summary={"authorization": "private request token"},
        request_cursor="private cursor",
    )

    rendered = repr(page)

    assert "private response body" not in rendered
    assert "private request token" not in rendered
    assert "private cursor" not in rendered

    failed = _page(
        body,
        status=PageAttemptStatus.FAILED,
        failure_code="private failure code",
        request_cursor="private failure cursor",
    )
    failed_rendered = repr(failed)
    assert "private failure code" not in failed_rendered
    assert "private failure cursor" not in failed_rendered


def test_capture_repr_does_not_render_raw_census_rows() -> None:
    capture = build_fuyao_instrument_census(
        [_page(_body([{"code": "private source row value"}]))],
        observed_at=_OBSERVED_AT,
    )

    rendered = repr(capture)

    assert "private source row value" not in rendered
    assert "page_count=1" in rendered
    assert "row_count=1" in rendered


def test_page_http_status_must_be_a_real_http_code() -> None:
    body = _body([{"code": "600519"}])

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        _page(body, http_status_code=True)

    assert raised.value.code == "http_status_invalid"


def test_page_input_requires_body_and_digest_as_a_pair() -> None:
    with pytest.raises(FuyaoInstrumentCensusError, match="response_body_digest_pair_invalid"):
        FuyaoInstrumentPageInput(
            sequence_number=1,
            raw_response_body=b"{}",
            declared_response_sha256=None,
            http_status_code=200,
        )


def test_page_input_requires_successful_body() -> None:
    with pytest.raises(FuyaoInstrumentCensusError, match="successful_page_body_required"):
        _page(None)


def test_page_and_total_row_bounds_stop_observation_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_module, "_MAX_CENSUS_ROWS", 1)
    body = _body([{"code": "first"}, {"code": "second"}])

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_row_count_exceeded"


def test_census_row_bound_applies_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "_MAX_CENSUS_ROWS", 1)

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census(
            [_page(_body([{"code": "first"}])), _page(_body([{"code": "second"}]), sequence=2)],
            observed_at=_OBSERVED_AT,
        )

    assert raised.value.code == "response_row_count_exceeded"


def test_total_capture_body_limit_applies_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census_module, "MAX_FUYAO_CENSUS_BODY_BYTES", 10)
    monkeypatch.setattr(census_module, "_MAX_TOTAL_CAPTURE_BYTES", 3)
    body = b"123"

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census(
            [
                _page(body, sequence=1, status=PageAttemptStatus.FAILED, failure_code="failed"),
                _page(body, sequence=2, status=PageAttemptStatus.FAILED, failure_code="failed"),
            ],
            observed_at=_OBSERVED_AT,
        )

    assert raised.value.code == "capture_body_total_too_large"


def test_response_digest_must_match_exact_original_bytes() -> None:
    body = _body([{"code": "600519"}])
    wrong_digest = "0" * 64

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census(
            [_page(body, declared_sha256=wrong_digest)], observed_at=_OBSERVED_AT
        )

    assert raised.value.code == "response_digest_mismatch"


def test_escaped_lone_surrogate_is_reported_as_a_safe_row_error() -> None:
    body = b'{"code":0,"data":{"item":[{"name":"\\ud800"}]}}'

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_row_invalid"


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"sequence": 0}, "page_sequence_invalid"),
        ({"sequence": True}, "page_sequence_invalid"),
        ({"source_page_number": 1.5}, "source_page_number_invalid"),
        ({"source_page_number": 1 << 64}, "source_page_number_invalid"),
        ({"source_page_number": "x" * 129}, "source_page_number_invalid"),
        ({"request_cursor": "x" * 513}, "page_cursor_invalid"),
        ({"terminal": 1}, "page_terminal_invalid"),
        ({"reported_total": True}, "reported_total_invalid"),
        ({"status": "bad"}, "page_attempt_status_invalid"),
        ({"failure_code": "unexpected"}, "nonfailed_page_has_failure_code"),
        (
            {"status": PageAttemptStatus.FAILED, "failure_code": "bad\n"},
            "page_failure_code_invalid",
        ),
        ({"status": PageAttemptStatus.FAILED}, "failed_page_code_required"),
        ({"failure_code": ""}, "page_failure_code_invalid"),
        ({"failure_code": "x" * 129}, "page_failure_code_invalid"),
        ({"failure_code": 123}, "page_failure_code_invalid"),
        (
            {"status": PageAttemptStatus.UNKNOWN, "terminal": False},
            "unknown_page_has_paging_fields",
        ),
        ({"status": PageAttemptStatus.UNKNOWN, "body": b"{}"}, "unknown_page_has_response_body"),
        ({"body": None}, "successful_page_body_required"),
        ({"body": bytearray(b"{}")}, "response_body_type_invalid"),
        ({"body": b"{}", "declared_sha256": "z" * 64}, "response_digest_invalid"),
        (
            {"status": PageAttemptStatus.FAILED, "failure_code": "failed", "terminal": True},
            "failed_page_has_paging_fields",
        ),
        ({"body": _body([]), "request_summary": 12}, "request_summary_invalid"),
    ],
)
def test_invalid_page_declarations_are_rejected(kwargs: dict[str, object], code: str) -> None:
    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        _page(**kwargs)  # type: ignore[arg-type]

    assert raised.value.code == code


def test_failed_page_digest_is_still_checked_without_parsing_its_body() -> None:
    body = b"not-json-but-observed-failure"
    page = _page(
        body,
        status=PageAttemptStatus.FAILED,
        failure_code="transport_failure",
        declared_sha256="f" * 64,
    )

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([page], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_digest_mismatch"


@pytest.mark.parametrize(
    ("summary", "code"),
    [
        ({"price": float("nan")}, "request_summary_nonfinite_number"),
        ({"path": "\ud800"}, "request_summary_unicode_invalid"),
        ({"key": object()}, "request_summary_value_invalid"),
        ({1: "non-string-key"}, "request_summary_key_invalid"),
        ({"deep": [[[[[[[[[[[[[[0]]]]]]]]]]]]]]}, "request_summary_depth_exceeded"),
    ],
)
def test_request_summary_is_bounded_finite_json(summary: object, code: str) -> None:
    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census(
            [_page(_body([]), request_summary=summary)],  # type: ignore[arg-type]
            observed_at=_OBSERVED_AT,
        )

    assert raised.value.code == code


def test_request_summary_hash_uses_canonical_json_order() -> None:
    body = _body([])
    first = build_fuyao_instrument_census(
        [_page(body, request_summary={"offset": 0, "limit": 10})], observed_at=_OBSERVED_AT
    )
    second = build_fuyao_instrument_census(
        [_page(body, request_summary={"limit": 10, "offset": 0})], observed_at=_OBSERVED_AT
    )

    assert first.request_summary_sha256s == second.request_summary_sha256s
    assert first.request_summary_sha256s[0] is not None
    assert first.source_census.pages[0].receipt is not None
    assert first.source_census.pages[0].receipt.request_sha256 is None


def test_request_summary_is_snapshotted_when_page_input_is_created() -> None:
    body = _body([])
    summary: dict[str, object] = {"path": "/original"}
    page = _page(body, request_summary=summary)
    summary["path"] = "/mutated-after-capture"

    capture = build_fuyao_instrument_census([page], observed_at=_OBSERVED_AT)
    expected = hashlib.sha256(b'{"path":"/original"}').hexdigest()

    assert capture.request_summary_sha256s == (expected,)


def test_request_summary_byte_bound_is_checked_while_copying() -> None:
    summary = {f"field{index}": "x" * 15_000 for index in range(5)}

    with pytest.raises(FuyaoInstrumentCensusError, match="request_summary_too_large"):
        build_fuyao_instrument_census(
            [_page(_body([]), request_summary=summary)], observed_at=_OBSERVED_AT
        )


@pytest.mark.parametrize(
    ("summary", "code"),
    [
        ({"large": 1 << 5_000}, "request_summary_integer_too_large"),
        ({"large": "x" * 16_385}, "request_summary_text_too_large"),
        ({"": "empty-key"}, "request_summary_key_invalid"),
        ({"x" * 129: "long-key"}, "request_summary_key_invalid"),
        ({f"field{index}": index for index in range(257)}, "request_summary_fields_exceeded"),
        ({"items": [None] * 4_097}, "request_summary_array_exceeded"),
        (
            {f"items{index}": [None] * 4_096 for index in range(5)},
            "request_summary_nodes_exceeded",
        ),
        (
            {f"field{index}": '"' * 10_000 for index in range(5)},
            "request_summary_too_large",
        ),
    ],
    ids=[
        "large-integer",
        "long-text",
        "empty-key",
        "long-key",
        "too-many-fields",
        "long-array",
        "too-many-nodes",
        "escaped-json-too-large",
    ],
)
def test_request_summary_resource_limits_fail_closed(summary: object, code: str) -> None:
    with pytest.raises(FuyaoInstrumentCensusError, match=code):
        _page(_body([]), request_summary=summary)  # type: ignore[arg-type]


def test_request_summary_accepts_finite_float_and_tuple_values() -> None:
    page = _page(_body([]), request_summary={"ratio": 0.125, "flags": (True, None)})

    capture = build_fuyao_instrument_census([page], observed_at=_OBSERVED_AT)

    assert (
        capture.request_summary_sha256s[0]
        == hashlib.sha256(b'{"flags":[true,null],"ratio":0.125}').hexdigest()
    )


def test_request_summary_unexpected_mapping_error_is_sanitized() -> None:
    class BrokenMapping(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            raise KeyError(key)

        def __iter__(self):
            return iter(("field",))

        def __len__(self) -> int:
            return 1

        def items(self):
            raise RuntimeError("private mapping error")

    with pytest.raises(FuyaoInstrumentCensusError, match="request_summary_invalid"):
        _page(_body([]), request_summary=BrokenMapping())


def test_capture_constructor_bounds_untrusted_iterables() -> None:
    empty = build_fuyao_instrument_census([], observed_at=_OBSERVED_AT).source_census

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_alignment_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=empty,
            request_summary_sha256s=itertools.repeat(None),
            response_body_sha256s=(),
            http_status_codes=(),
            raw_response_bodies=(),
        )


def test_capture_constructor_rejects_non_census_values() -> None:
    with pytest.raises(FuyaoInstrumentCensusError, match="capture_census_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=object(),  # type: ignore[arg-type]
            request_summary_sha256s=(),
            response_body_sha256s=(),
            http_status_codes=(),
            raw_response_bodies=(),
        )


def test_capture_constructor_rejects_scalar_alignment_values() -> None:
    capture = build_fuyao_instrument_census(
        [_page(status=PageAttemptStatus.UNKNOWN)], observed_at=_OBSERVED_AT
    )

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_alignment_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s="not-a-sequence",  # type: ignore[arg-type]
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_rejects_page_count_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = build_fuyao_instrument_census(
        [
            _page(status=PageAttemptStatus.UNKNOWN, sequence=1),
            _page(status=PageAttemptStatus.UNKNOWN, sequence=2),
        ],
        observed_at=_OBSERVED_AT,
    )
    monkeypatch.setattr(census_module, "_MAX_CAPTURE_PAGES", 1)

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_page_count_exceeded"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=capture.request_summary_sha256s,
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_converts_iterator_errors_to_stable_codes() -> None:
    capture = build_fuyao_instrument_census(
        [_page(status=PageAttemptStatus.UNKNOWN)], observed_at=_OBSERVED_AT
    )

    def broken_values():
        raise RuntimeError("private iterator failure")
        yield None

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_alignment_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=broken_values(),
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_validates_digest_syntax() -> None:
    capture = build_fuyao_instrument_census(
        [_page(_body([{"code": "captured"}]))], observed_at=_OBSERVED_AT
    )

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_digest_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=("bad",),
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_checks_body_type_digest_pair_and_digest_value() -> None:
    failed = build_fuyao_instrument_census(
        [
            _page(
                b"error",
                status=PageAttemptStatus.FAILED,
                failure_code="failed",
            )
        ],
        observed_at=_OBSERVED_AT,
    )

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_body_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=failed.source_census,
            request_summary_sha256s=failed.request_summary_sha256s,
            response_body_sha256s=failed.response_body_sha256s,
            http_status_codes=failed.http_status_codes,
            raw_response_bodies=(bytearray(b"error"),),  # type: ignore[arg-type]
        )
    with pytest.raises(FuyaoInstrumentCensusError, match="capture_body_digest_pair_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=failed.source_census,
            request_summary_sha256s=failed.request_summary_sha256s,
            response_body_sha256s=(None,),
            http_status_codes=failed.http_status_codes,
            raw_response_bodies=(b"error",),
        )
    with pytest.raises(FuyaoInstrumentCensusError, match="capture_body_digest_mismatch"):
        FuyaoInstrumentCensusCapture(
            source_census=failed.source_census,
            request_summary_sha256s=failed.request_summary_sha256s,
            response_body_sha256s=failed.response_body_sha256s,
            http_status_codes=failed.http_status_codes,
            raw_response_bodies=(b"different error",),
        )


def test_capture_constructor_requires_success_receipts_and_failed_pages_without_rows() -> None:
    success = build_fuyao_instrument_census(
        [_page(_body([{"code": "success"}]))], observed_at=_OBSERVED_AT
    )
    page_without_receipt = replace(success.source_census.pages[0], receipt=None)
    census_without_receipt = replace(success.source_census, pages=(page_without_receipt,))

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_success_receipt_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=census_without_receipt,
            request_summary_sha256s=success.request_summary_sha256s,
            response_body_sha256s=success.response_body_sha256s,
            http_status_codes=success.http_status_codes,
            raw_response_bodies=success.raw_response_bodies,
        )

    original_receipt = success.source_census.pages[0].receipt
    assert original_receipt is not None
    for bad_receipt, digest, body in (
        (
            PageReceipt(
                request_sha256="0" * 64,
                response_sha256=original_receipt.response_sha256,
            ),
            success.response_body_sha256s,
            success.raw_response_bodies,
        ),
        (
            PageReceipt(request_sha256=None, response_sha256="0" * 64),
            success.response_body_sha256s,
            success.raw_response_bodies,
        ),
        (original_receipt, (None,), (None,)),
    ):
        altered_page = replace(success.source_census.pages[0], receipt=bad_receipt)
        altered_census = replace(success.source_census, pages=(altered_page,))
        with pytest.raises(FuyaoInstrumentCensusError, match="capture_success_receipt_invalid"):
            FuyaoInstrumentCensusCapture(
                source_census=altered_census,
                request_summary_sha256s=success.request_summary_sha256s,
                response_body_sha256s=digest,
                http_status_codes=success.http_status_codes,
                raw_response_bodies=body,
            )

    failed = build_fuyao_instrument_census(
        [_page(b"error", status=PageAttemptStatus.FAILED, failure_code="failed")],
        observed_at=_OBSERVED_AT,
    )
    forged_page = replace(failed.source_census.pages[0])
    object.__setattr__(
        forged_page,
        "rows",
        (InstrumentSourceObservation(raw_row={"code": "forged"}),),
    )
    forged_census = replace(failed.source_census, pages=(forged_page,))

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_failed_page_has_evidence"):
        FuyaoInstrumentCensusCapture(
            source_census=forged_census,
            request_summary_sha256s=failed.request_summary_sha256s,
            response_body_sha256s=failed.response_body_sha256s,
            http_status_codes=failed.http_status_codes,
            raw_response_bodies=failed.raw_response_bodies,
        )

    failed_with_receipt = replace(
        failed.source_census.pages[0],
        receipt=PageReceipt(request_sha256=None, response_sha256=failed.response_body_sha256s[0]),
    )
    failed_census_with_receipt = replace(failed.source_census, pages=(failed_with_receipt,))
    with pytest.raises(FuyaoInstrumentCensusError, match="capture_failed_page_has_evidence"):
        FuyaoInstrumentCensusCapture(
            source_census=failed_census_with_receipt,
            request_summary_sha256s=failed.request_summary_sha256s,
            response_body_sha256s=failed.response_body_sha256s,
            http_status_codes=failed.http_status_codes,
            raw_response_bodies=failed.raw_response_bodies,
        )


def test_capture_constructor_reapplies_response_body_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = build_fuyao_instrument_census(
        [_page(_body([{"code": "captured"}]))], observed_at=_OBSERVED_AT
    )
    monkeypatch.setattr(census_module, "MAX_FUYAO_CENSUS_BODY_BYTES", 1)

    with pytest.raises(FuyaoInstrumentCensusError, match="response_body_too_large"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=capture.request_summary_sha256s,
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_reapplies_aggregate_body_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = build_fuyao_instrument_census(
        [
            _page(b"a", status=PageAttemptStatus.FAILED, failure_code="failed"),
            _page(b"b", sequence=2, status=PageAttemptStatus.FAILED, failure_code="failed"),
        ],
        observed_at=_OBSERVED_AT,
    )
    monkeypatch.setattr(census_module, "_MAX_TOTAL_CAPTURE_BYTES", 1)

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_body_total_too_large"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=capture.request_summary_sha256s,
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=capture.http_status_codes,
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_validates_retained_http_status_codes() -> None:
    capture = build_fuyao_instrument_census(
        [_page(_body([{"code": "captured"}]))], observed_at=_OBSERVED_AT
    )

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_success_http_status_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=capture.request_summary_sha256s,
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=(503,),
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_capture_constructor_rejects_non_http_status_codes() -> None:
    capture = build_fuyao_instrument_census(
        [_page(_body([{"code": "captured"}]))], observed_at=_OBSERVED_AT
    )

    with pytest.raises(FuyaoInstrumentCensusError, match="capture_http_status_invalid"):
        FuyaoInstrumentCensusCapture(
            source_census=capture.source_census,
            request_summary_sha256s=capture.request_summary_sha256s,
            response_body_sha256s=capture.response_body_sha256s,
            http_status_codes=(True,),  # type: ignore[arg-type]
            raw_response_bodies=capture.raw_response_bodies,
        )


def test_page_collection_and_capture_metadata_limits_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page_one = _page(status=PageAttemptStatus.UNKNOWN, sequence=1)
    page_two = _page(status=PageAttemptStatus.UNKNOWN, sequence=1)
    monkeypatch.setattr(census_module, "_MAX_CAPTURE_PAGES", 1)

    with pytest.raises(FuyaoInstrumentCensusError, match="page_count_exceeded"):
        build_fuyao_instrument_census([page_one, page_two], observed_at=_OBSERVED_AT)


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("not-pages", "pages_invalid"),
        ({}, "pages_invalid"),
        (object(), "pages_invalid"),
        ([object()], "page_input_invalid"),
    ],
)
def test_invalid_page_collections_fail_closed(value: object, code: str) -> None:
    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census(value, observed_at=_OBSERVED_AT)  # type: ignore[arg-type]

    assert raised.value.code == code


def test_page_iterable_error_is_sanitized() -> None:
    def broken_pages():
        raise RuntimeError("private iterator error")
        yield None

    with pytest.raises(FuyaoInstrumentCensusError, match="pages_invalid"):
        build_fuyao_instrument_census(broken_pages(), observed_at=_OBSERVED_AT)


def test_source_page_dto_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_page(**kwargs: object) -> None:
        del kwargs
        raise InstrumentSourceCensusError("private DTO validation detail")

    monkeypatch.setattr(census_module, "InstrumentSourcePage", fail_page)

    with pytest.raises(FuyaoInstrumentCensusError, match="source_page_invalid"):
        build_fuyao_instrument_census(
            [_page(status=PageAttemptStatus.UNKNOWN)], observed_at=_OBSERVED_AT
        )


def test_source_census_dto_errors_are_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_census(**kwargs: object) -> None:
        del kwargs
        raise InstrumentSourceCensusError("private census validation detail")

    monkeypatch.setattr(census_module, "InstrumentSourceCensus", fail_census)

    with pytest.raises(FuyaoInstrumentCensusError, match="source_census_invalid"):
        build_fuyao_instrument_census([], observed_at=_OBSERVED_AT)


@pytest.mark.parametrize(
    "observed_at",
    [None, "2026-10-30T00:00:00Z", datetime(2026, 10, 30)],
)
def test_observation_time_must_be_timezone_aware(observed_at: object) -> None:
    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([], observed_at=observed_at)  # type: ignore[arg-type]

    assert raised.value.code == "observed_at_invalid"


def test_observation_time_with_unknown_utc_offset_is_rejected() -> None:
    class MissingOffset(tzinfo):
        def utcoffset(self, dt: datetime | None):
            return None

    observed_at = datetime(2026, 10, 30, tzinfo=MissingOffset())

    with pytest.raises(FuyaoInstrumentCensusError, match="observed_at_invalid"):
        build_fuyao_instrument_census([], observed_at=observed_at)


def test_response_payload_failure_codes_do_not_expose_source_contents() -> None:
    sensitive = b'{"code":1,"message":"private upstream detail","data":{"item":[]}}'

    with pytest.raises(FuyaoInstrumentCensusError) as raised:
        build_fuyao_instrument_census([_page(sensitive)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_not_successful"
    assert "private upstream detail" not in str(raised.value)
