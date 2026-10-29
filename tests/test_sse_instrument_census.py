"""Synthetic, offline behavior tests for the SSE instrument census parser."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterator

from opendata.pipeline import sse_instrument_census as census
from opendata.pipeline.sse_instrument_census import (
    MAX_SSE_CENSUS_BODY_BYTES,
    SSEInstrumentCensusCapture,
    SSEInstrumentCensusError,
    SSEInstrumentPageEvidence,
    SSEInstrumentPageInput,
    SSEInstrumentSelector,
    SSEInstrumentSourceObservation,
    build_sse_instrument_census,
)

_OBSERVED_AT = datetime(2026, 10, 30, 4, 0, tzinfo=timezone.utc)
_SELECTOR_CASES = (
    (SSEInstrumentSelector.CURRENT_MAIN_A, "1", "2,4,5,7,8"),
    (SSEInstrumentSelector.CURRENT_STAR, "8", "2,4,5,7,8"),
    (SSEInstrumentSelector.STATUS3_MAIN_A_CANDIDATE, "1", "3"),
    (SSEInstrumentSelector.STATUS3_STAR_CANDIDATE, "8", "3"),
)
_AUTO_DIGEST = object()


def _summary(stock_type: str = "1", status: str = "2,4,5,7,8") -> dict[str, object]:
    """Return a synthetic request summary for one fixed selector context."""
    return {
        "method": "GET",
        "endpoint": "https://query.sse.com.cn/sseQuery/commonQuery.do",
        "params": {
            "STOCK_TYPE": stock_type,
            "COMPANY_STATUS": status,
            "pageHelp.pageNo": "1",
        },
    }


def _body(
    rows: list[object] | None = None,
    *,
    page_help: object | None = None,
    omit_page_help: bool = False,
) -> bytes:
    """Build a synthetic SSE-like envelope without contacting a source."""
    payload: dict[str, object] = {"result": rows if rows is not None else []}
    if not omit_page_help:
        payload["pageHelp"] = (
            page_help
            if page_help is not None
            else {"pageCount": 1, "pageNo": 1, "pageSize": 10, "total": 1}
        )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _page(
    body: bytes | None = None,
    *,
    selector: SSEInstrumentSelector | str = SSEInstrumentSelector.CURRENT_MAIN_A,
    sequence: int = 1,
    summary: Mapping[str, object] | None = None,
    omit_summary: bool = False,
    status_code: int = 200,
    declared_hash: str | None = None,
) -> SSEInstrumentPageInput:
    """Create a captured synthetic page and digest its exact body bytes."""
    response = _body() if body is None else body
    digest = declared_hash or hashlib.sha256(response).hexdigest()
    return SSEInstrumentPageInput(
        selector=selector,
        sequence_number=sequence,
        raw_response_body=response,
        declared_response_sha256=digest,
        request_summary=(None if omit_summary else (_summary() if summary is None else summary)),
        http_status_code=status_code,
    )


def _evidence(
    *,
    selector: object = SSEInstrumentSelector.CURRENT_MAIN_A,
    sequence_number: object = 1,
    source_rows: object = (),
    page_help: object = None,
    request_summary: object = None,
    request_summary_sha256: object = _AUTO_DIGEST,
    response_sha256: object = None,
    http_status_code: object = 200,
    raw_response_body: object = b'{"result":[]}',
) -> SSEInstrumentPageEvidence:
    """Build direct DTO evidence for synthetic invariant tests."""
    if request_summary_sha256 is _AUTO_DIGEST:
        if isinstance(request_summary, Mapping):
            summary_bytes = json.dumps(
                dict(request_summary),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_digest: object = hashlib.sha256(summary_bytes).hexdigest()
        else:
            request_digest = None
    else:
        request_digest = request_summary_sha256
    if response_sha256 is None:
        body_digest: object = (
            hashlib.sha256(raw_response_body).hexdigest()
            if type(raw_response_body) is bytes
            else "0" * 64
        )
    else:
        body_digest = response_sha256
    return SSEInstrumentPageEvidence(
        selector=selector,  # type: ignore[arg-type]
        sequence_number=sequence_number,  # type: ignore[arg-type]
        source_rows=source_rows,  # type: ignore[arg-type]
        page_help=page_help,  # type: ignore[arg-type]
        request_summary=request_summary,  # type: ignore[arg-type]
        request_summary_sha256=request_digest,  # type: ignore[arg-type]
        response_sha256=body_digest,  # type: ignore[arg-type]
        http_status_code=http_status_code,  # type: ignore[arg-type]
        raw_response_body=raw_response_body,  # type: ignore[arg-type]
    )


def test_success_retains_raw_rows_page_help_request_and_response_evidence() -> None:
    row = {
        "A_STOCK_CODE": "600001",
        "COMPANY_CODE": "company-token-600001",
        "DELIST_DATE": "2026-10-29",
        "SEC_NAME_CN": "合成证券",
        "extra": {"values": [True, None, "保留"]},
    }
    body = _body([row], page_help={"pageCount": 4, "pageNo": 1, "reported": "原值"})
    request = _summary()
    capture = build_sse_instrument_census([_page(body, summary=request)], observed_at=_OBSERVED_AT)
    page = capture.pages[0]

    assert dict(page.source_rows[0].raw_row) == {
        **row,
        "extra": {"values": (True, None, "保留")},
    }
    assert page.source_rows[0].raw_row["COMPANY_CODE"] == "company-token-600001"
    assert page.source_rows[0].raw_row["DELIST_DATE"] == "2026-10-29"
    assert page.page_help == {"pageCount": 4, "pageNo": 1, "reported": "原值"}
    assert dict(page.request_summary or {}) == request
    assert (
        page.request_summary_sha256
        == hashlib.sha256(
            json.dumps(request, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert page.response_sha256 == hashlib.sha256(body).hexdigest()
    assert capture.response_body_sha256s == (hashlib.sha256(body).hexdigest(),)
    assert capture.raw_response_bodies == (body,)
    assert capture.declared_exchange_scope == ("SSE",)
    assert capture.scope_basis == "caller_declared_fixed_sse_source_context"
    assert capture.pagination_status == "UNKNOWN"
    assert capture.source_authenticity == "UNKNOWN"
    assert capture.market_completeness == "UNKNOWN"


@pytest.mark.parametrize(("selector", "stock_type", "status"), _SELECTOR_CASES)
def test_all_fixed_selectors_are_preserved_as_caller_context(
    selector: SSEInstrumentSelector, stock_type: str, status: str
) -> None:
    capture = build_sse_instrument_census(
        [_page(selector=selector, summary=_summary(stock_type, status))], observed_at=_OBSERVED_AT
    )

    assert capture.pages[0].selector is selector
    assert capture.scope_basis == "caller_declared_fixed_sse_source_context"
    assert capture.market_completeness == "UNKNOWN"


def test_flat_summary_and_missing_summary_are_supported_without_wire_claims() -> None:
    flat = {"STOCK_TYPE": "1", "COMPANY_STATUS": "2,4,5,7,8"}
    with_summary = build_sse_instrument_census([_page(summary=flat)], observed_at=_OBSERVED_AT)
    without_summary = build_sse_instrument_census(
        [_page(omit_summary=True)], observed_at=_OBSERVED_AT
    )

    assert with_summary.pages[0].request_summary == flat
    assert with_summary.request_summary_sha256s[0] is not None
    assert without_summary.pages[0].request_summary is None
    assert without_summary.request_summary_sha256s == (None,)
    assert without_summary.source_authenticity == "UNKNOWN"


def test_rows_and_capture_repr_redact_values() -> None:
    marker = "SYNTHETIC-SENSITIVE-ROW-MARKER"
    capture = build_sse_instrument_census(
        [_page(_body([{"private_source_value": marker}]))], observed_at=_OBSERVED_AT
    )

    assert marker not in repr(capture)
    assert marker not in repr(capture.pages[0])
    assert marker not in repr(capture.pages[0].source_rows[0])
    assert marker not in repr(_page(_body([{"private_source_value": marker}])))


def test_page_help_may_be_absent_and_empty_results_remain_successful() -> None:
    body = _body([], omit_page_help=True)
    capture = build_sse_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert capture.pages[0].source_rows == ()
    assert capture.pages[0].page_help is None
    assert capture.pagination_status == "UNKNOWN"


@pytest.mark.parametrize(
    ("summary", "expected_code"),
    [
        ({"STOCK_TYPE": "8", "COMPANY_STATUS": "2,4,5,7,8"}, "request_summary_stock_type_mismatch"),
        ({"STOCK_TYPE": "1", "COMPANY_STATUS": "3"}, "request_summary_status_mismatch"),
        ({"params": ["bad"]}, "request_summary_params_invalid"),
        ({"params": {"STOCK_TYPE": "1"}}, "request_summary_status_mismatch"),
    ],
)
def test_summary_selector_mismatch_is_rejected_content_free(
    summary: dict[str, object], expected_code: str
) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        _page(summary=summary)

    assert raised.value.code == expected_code
    assert "600" not in str(raised.value)


def test_request_summary_rejects_unpaired_unicode_without_rendering_it() -> None:
    summary = _summary()
    summary["caller_note"] = "\ud800"

    with pytest.raises(SSEInstrumentCensusError) as raised:
        _page(summary=summary)

    assert raised.value.code == "json_text_invalid_unicode"
    assert "\\ud800" not in str(raised.value)


def test_request_summary_rejects_custom_mapping_without_invoking_it() -> None:
    class CustomSummary(Mapping[str, object]):
        def __getitem__(self, key: str) -> object:
            del key
            raise AssertionError("custom mapping code must not run")

        def __iter__(self) -> Iterator[str]:
            raise AssertionError("custom mapping code must not run")

        def __len__(self) -> int:
            raise AssertionError("custom mapping code must not run")

    with pytest.raises(SSEInstrumentCensusError) as raised:
        _page(summary=CustomSummary())

    assert raised.value.code == "json_value_type_invalid"


def test_request_summary_rejects_custom_array_without_invoking_it() -> None:
    class CustomList(list[object]):
        def __iter__(self) -> Iterator[object]:
            raise AssertionError("custom array code must not run")

        def __len__(self) -> int:
            raise AssertionError("custom array code must not run")

    summary = _summary()
    summary["caller_values"] = CustomList()

    with pytest.raises(SSEInstrumentCensusError) as raised:
        _page(summary=summary)

    assert raised.value.code == "json_value_type_invalid"


@pytest.mark.parametrize("selector", ["unsupported", "current_b", "3", None, 1])
def test_unsupported_or_mistyped_selector_is_rejected(selector: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="selector_invalid"):
        _page(selector=selector)  # type: ignore[arg-type]


@pytest.mark.parametrize("sequence", [0, -1, True, 129])
def test_page_sequence_requires_bounded_non_boolean_integer(sequence: int) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="page_sequence_invalid"):
        _page(sequence=sequence)


def test_response_hash_must_match_exact_bytes() -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        build_sse_instrument_census([_page(declared_hash="0" * 64)], observed_at=_OBSERVED_AT)

    assert raised.value.code == "response_digest_mismatch"


@pytest.mark.parametrize("status_code", [True, 199, 300, 700, "200"])
def test_only_declared_success_http_status_is_accepted(status_code: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="successful_http_status_invalid"):
        _page(status_code=status_code)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("body", "expected_code"),
    [
        (b"", "response_body_empty"),
        (b"\xff", "response_utf8_invalid"),
        (b"{", "response_json_invalid"),
        (b"[]", "response_root_invalid"),
        (b'{"items":[]}', "response_result_invalid"),
        (b'{"result":{},"pageHelp":{}}', "response_result_invalid"),
        (b'{"result":[],"pageHelp":[]}', "response_page_help_invalid"),
        (b'{"result":[],"pageHelp":null}', "response_page_help_invalid"),
        (b'{"result":[1]}', "response_row_invalid"),
        (b'{"result":[{}]}', "response_row_invalid"),
        (b'{"result":[],"result":[]}', "response_json_duplicate_key"),
        (b'{"result":[],"pageHelp":{"x":1,"x":2}}', "response_json_duplicate_key"),
        (
            b'{"result":[],"pageHelp":{"text":"\\ud800"}}',
            "json_text_invalid_unicode",
        ),
        (b'{"result":[{"x":NaN}]}', "response_json_nonfinite_number"),
        (b'{"result":[{"x":1e10000}]}', "response_json_nonfinite_number"),
    ],
)
def test_malformed_response_has_stable_content_free_error(body: bytes, expected_code: str) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        build_sse_instrument_census([_page(body)], observed_at=_OBSERVED_AT)

    assert raised.value.code == expected_code


def test_response_numeric_and_nesting_bounds_are_enforced() -> None:
    huge_integer = str(1 << 4096).encode()
    too_deep = b'{"result":[],"extra":' + b"[" * 17 + b"0" + b"]" * 17 + b"}"

    for body, expected in (
        (b'{"result":[{"value":' + huge_integer + b"}]}", "response_json_integer_too_large"),
        (too_deep, "response_json_depth_exceeded"),
    ):
        with pytest.raises(SSEInstrumentCensusError) as raised:
            build_sse_instrument_census([_page(body)], observed_at=_OBSERVED_AT)
        assert raised.value.code == expected


def test_body_size_is_checked_before_response_decode() -> None:
    oversized = b" " * (MAX_SSE_CENSUS_BODY_BYTES + 1)
    with pytest.raises(SSEInstrumentCensusError, match="response_body_too_large"):
        SSEInstrumentPageInput(
            selector=SSEInstrumentSelector.CURRENT_MAIN_A,
            sequence_number=1,
            raw_response_body=oversized,
            declared_response_sha256="0" * 64,
        )


@pytest.mark.parametrize("observed_at", ["2026-10-30T00:00:00Z", datetime(2026, 10, 30)])
def test_observation_time_must_be_timezone_aware(observed_at: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="observed_at_invalid"):
        build_sse_instrument_census([], observed_at=observed_at)  # type: ignore[arg-type]


@pytest.mark.parametrize("pages", [None, "page", b"page", {"page": 1}])
def test_page_collection_must_be_an_iterable_of_page_inputs(pages: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="pages_invalid"):
        build_sse_instrument_census(pages, observed_at=_OBSERVED_AT)  # type: ignore[arg-type]


def test_page_collection_rejects_non_page_members_and_noncontiguous_sequences() -> None:
    with pytest.raises(SSEInstrumentCensusError, match="page_input_invalid"):
        build_sse_instrument_census([object()], observed_at=_OBSERVED_AT)  # type: ignore[list-item]
    with pytest.raises(SSEInstrumentCensusError, match="page_sequence_not_contiguous"):
        build_sse_instrument_census([_page(sequence=2)], observed_at=_OBSERVED_AT)


def test_page_collection_limit_is_checked_before_member_validation() -> None:
    pages = (object() for _ in range(129))

    with pytest.raises(SSEInstrumentCensusError, match="page_count_exceeded"):
        build_sse_instrument_census(pages, observed_at=_OBSERVED_AT)  # type: ignore[arg-type]


def test_failing_page_iterator_is_translated_without_its_message() -> None:
    def pages() -> object:
        yield _page()
        raise RuntimeError("SYNTHETIC PRIVATE ITERATOR MESSAGE")

    with pytest.raises(SSEInstrumentCensusError) as raised:
        build_sse_instrument_census(pages(), observed_at=_OBSERVED_AT)  # type: ignore[arg-type]

    assert raised.value.code == "pages_invalid"
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


def test_capture_accepts_empty_scope_evidence_but_keeps_all_coverage_unknown() -> None:
    capture = build_sse_instrument_census([], observed_at=_OBSERVED_AT)

    assert capture.pages == ()
    assert capture.source_id == "sse.instrument"
    assert capture.pagination_status == "UNKNOWN"
    assert capture.source_authenticity == "UNKNOWN"
    assert capture.market_completeness == "UNKNOWN"


def test_frozen_source_rows_cannot_be_mutated_after_capture() -> None:
    capture = build_sse_instrument_census(
        [_page(_body([{"source": "value"}]))], observed_at=_OBSERVED_AT
    )

    with pytest.raises(TypeError):
        capture.pages[0].source_rows[0].raw_row["source"] = "changed"  # type: ignore[index]


def test_page_evidence_and_capture_representations_do_not_include_raw_summary() -> None:
    marker = "SYNTHETIC-REQUEST-SUMMARY-SECRET"
    summary = _summary()
    summary["caller_note"] = marker
    capture = build_sse_instrument_census([_page(summary=summary)], observed_at=_OBSERVED_AT)

    assert marker not in repr(capture)
    assert marker not in repr(capture.pages[0])


def test_capture_type_rejects_non_page_evidence() -> None:
    with pytest.raises(SSEInstrumentCensusError, match="page_evidence_invalid"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=(object(),))  # type: ignore[arg-type]


def test_capture_rejects_unverified_scope_or_completeness_overrides() -> None:
    capture = build_sse_instrument_census([_page()], observed_at=_OBSERVED_AT)

    with pytest.raises(SSEInstrumentCensusError, match="source_id_invalid"):
        SSEInstrumentCensusCapture(
            observed_at=_OBSERVED_AT, pages=capture.pages, source_id="caller.fake"
        )
    with pytest.raises(SSEInstrumentCensusError, match="declared_scope_invalid"):
        SSEInstrumentCensusCapture(
            observed_at=_OBSERVED_AT, pages=capture.pages, declared_exchange_scope=("SZSE",)
        )
    with pytest.raises(SSEInstrumentCensusError, match="market_completeness_invalid"):
        SSEInstrumentCensusCapture(
            observed_at=_OBSERVED_AT, pages=capture.pages, market_completeness="COMPLETE"
        )


def test_capture_enforces_total_body_bytes_with_bounded_page_evidence() -> None:
    body = b"x" * MAX_SSE_CENSUS_BODY_BYTES
    digest = hashlib.sha256(body).hexdigest()
    pages = tuple(
        SSEInstrumentPageEvidence(
            selector=SSEInstrumentSelector.CURRENT_MAIN_A,
            sequence_number=index,
            source_rows=(),
            page_help=None,
            request_summary=None,
            request_summary_sha256=None,
            response_sha256=digest,
            http_status_code=200,
            raw_response_body=body,
        )
        for index in range(1, 6)
    )

    with pytest.raises(SSEInstrumentCensusError, match="capture_body_total_too_large"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=pages)


def test_capture_enforces_total_row_count_across_pages() -> None:
    row = SSEInstrumentSourceObservation({"synthetic": "row"})
    rows = tuple(row for _ in range(60_000))
    body = b"{}"
    digest = hashlib.sha256(body).hexdigest()
    pages = tuple(
        SSEInstrumentPageEvidence(
            selector=SSEInstrumentSelector.CURRENT_MAIN_A,
            sequence_number=index,
            source_rows=rows,
            page_help=None,
            request_summary=None,
            request_summary_sha256=None,
            response_sha256=digest,
            http_status_code=200,
            raw_response_body=body,
        )
        for index in (1, 2)
    )

    with pytest.raises(SSEInstrumentCensusError, match="capture_row_count_exceeded"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=pages)


def test_page_evidence_rows_are_immutable_sequences() -> None:
    capture = build_sse_instrument_census(
        [_page(_body([{"code": "600001"}]))], observed_at=_OBSERVED_AT
    )

    with pytest.raises(TypeError):
        capture.pages[0].source_rows[0] = capture.pages[0].source_rows[0]  # type: ignore[index]


def test_raw_rows_remain_noncanonical_and_lifecycle_fields_are_unmapped() -> None:
    row = {"A_STOCK_CODE": "600001", "DELIST_DATE": "2026-10-29"}
    capture = build_sse_instrument_census([_page(_body([row]))], observed_at=_OBSERVED_AT)
    observation = capture.pages[0].source_rows[0]

    assert observation.raw_row["A_STOCK_CODE"] == "600001"
    assert observation.raw_row["DELIST_DATE"] == "2026-10-29"
    assert not hasattr(observation, "identity")
    assert not hasattr(observation, "termination_date")
    assert not hasattr(observation, "last_trading_date")


def test_distinct_raw_rows_and_captures_do_not_use_metadata_only_equality() -> None:
    first_row = SSEInstrumentSourceObservation({"source_code": "600001"})
    second_row = SSEInstrumentSourceObservation({"source_code": "600002"})
    first_capture = build_sse_instrument_census(
        [_page(_body([{"source_code": "600001"}]))], observed_at=_OBSERVED_AT
    )
    second_capture = build_sse_instrument_census(
        [_page(_body([{"source_code": "600002"}]))], observed_at=_OBSERVED_AT
    )

    assert first_row != second_row
    assert first_capture != second_capture


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("raw_response_body", bytearray(b"{}"), "response_body_type_invalid"),
        ("declared_response_sha256", "not-a-digest", "response_digest_invalid"),
        ("request_summary", [], "request_summary_invalid"),
    ],
)
def test_page_input_rejects_mistyped_evidence_fields(
    field: str, value: object, expected_code: str
) -> None:
    fields: dict[str, object] = {
        "selector": SSEInstrumentSelector.CURRENT_MAIN_A,
        "sequence_number": 1,
        "raw_response_body": b'{"result":[]}',
        "declared_response_sha256": hashlib.sha256(b'{"result":[]}').hexdigest(),
    }
    fields[field] = value

    with pytest.raises(SSEInstrumentCensusError) as raised:
        SSEInstrumentPageInput(**fields)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


def test_page_input_normalizes_unexpected_summary_freezer_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_freeze(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError("SYNTHETIC PRIVATE FREEZER DETAIL")

    monkeypatch.setattr(census, "_freeze_json", fail_freeze)
    with pytest.raises(SSEInstrumentCensusError) as raised:
        _page()

    assert raised.value.code == "request_summary_invalid"
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


def test_page_input_rejects_non_mapping_freeze_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(census, "_freeze_json", lambda *args, **kwargs: 1)

    with pytest.raises(SSEInstrumentCensusError, match="request_summary_invalid"):
        _page()


def test_summary_canonical_size_is_bounded_after_json_escaping() -> None:
    summary = _summary()
    summary.update({f"note_{index}": '"' * 16_000 for index in range(4)})

    with pytest.raises(SSEInstrumentCensusError, match="request_summary_too_large"):
        _page(summary=summary)


def test_nested_request_summary_arrays_are_frozen_and_canonically_hashed() -> None:
    summary = _summary()
    summary["filters"] = ["A", {"markets": ["main", "star"]}]

    capture = build_sse_instrument_census([_page(summary=summary)], observed_at=_OBSERVED_AT)

    assert capture.pages[0].request_summary is not None
    assert capture.pages[0].request_summary["filters"] == (
        "A",
        {"markets": ("main", "star")},
    )
    assert capture.request_summary_sha256s[0] is not None


@pytest.mark.parametrize(
    ("row", "expected_code"),
    [
        ([], "response_row_invalid"),
        ({}, "response_row_invalid"),
        ({"unsupported": object()}, "response_row_invalid"),
    ],
)
def test_source_observation_rejects_invalid_or_empty_raw_objects(
    row: object, expected_code: str
) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        SSEInstrumentSourceObservation(row)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    ("overrides", "expected_code"),
    [
        ({"selector": "current_main_a"}, "page_selector_invalid"),
        ({"sequence_number": 0}, "page_sequence_invalid"),
        ({"source_rows": []}, "page_rows_invalid"),
        ({"source_rows": (object(),)}, "page_rows_invalid"),
        ({"page_help": []}, "page_help_invalid"),
        ({"request_summary": []}, "request_summary_invalid"),
        (
            {"request_summary": _summary(), "request_summary_sha256": "0" * 64},
            "request_summary_digest_mismatch",
        ),
        (
            {"request_summary": _summary(), "request_summary_sha256": "invalid"},
            "request_summary_digest_invalid",
        ),
        ({"request_summary_sha256": "0" * 64}, "request_summary_digest_mismatch"),
        ({"response_sha256": "invalid"}, "response_digest_invalid"),
        ({"http_status_code": 300}, "successful_http_status_invalid"),
        ({"raw_response_body": bytearray(b"{}")}, "response_body_invalid"),
        ({"response_sha256": "0" * 64}, "response_digest_mismatch"),
    ],
)
def test_page_evidence_revalidates_directly_constructed_values(
    overrides: dict[str, object], expected_code: str
) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        _evidence(**overrides)

    assert raised.value.code == expected_code


def test_page_evidence_enforces_row_and_body_resource_limits() -> None:
    observation = SSEInstrumentSourceObservation({"synthetic": "row"})
    with pytest.raises(SSEInstrumentCensusError, match="page_rows_invalid"):
        _evidence(source_rows=(observation,) * (census._MAX_ROWS + 1))

    oversized = b"x" * (MAX_SSE_CENSUS_BODY_BYTES + 1)
    with pytest.raises(SSEInstrumentCensusError, match="response_body_too_large"):
        _evidence(raw_response_body=oversized, response_sha256="0" * 64)


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        ("scope_basis", "verified", "scope_basis_invalid"),
        ("pagination_status", "COMPLETE", "pagination_status_invalid"),
        ("source_authenticity", "VERIFIED", "source_authenticity_invalid"),
    ],
)
def test_capture_keeps_caller_scope_and_completeness_claims_fixed(
    field: str, value: str, expected_code: str
) -> None:
    extra_fields: Any = {field: value}
    with pytest.raises(SSEInstrumentCensusError) as raised:
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=(), **extra_fields)

    assert raised.value.code == expected_code


@pytest.mark.parametrize("offset", [None, "value", "raise_value", "raise_overflow"])
def test_builder_rejects_invalid_or_raising_timezone_offsets(offset: object) -> None:
    class SyntheticTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta | None:
            del dt
            if offset == "raise_value":
                raise ValueError("SYNTHETIC PRIVATE TZ DETAIL")
            if offset == "raise_overflow":
                raise OverflowError("SYNTHETIC PRIVATE TZ DETAIL")
            if offset == "value":
                return timedelta(hours=25)
            return None

        def dst(self, dt: datetime | None) -> timedelta | None:
            del dt
            return None

        def tzname(self, dt: datetime | None) -> str:
            del dt
            return "synthetic"

    observed_at = datetime(2026, 10, 30, tzinfo=SyntheticTimezone())
    with pytest.raises(SSEInstrumentCensusError) as raised:
        build_sse_instrument_census([], observed_at=observed_at)

    assert raised.value.code == "observed_at_invalid"
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


def test_capture_rejects_bad_page_iterators_and_overlong_page_sequences() -> None:
    def broken_pages() -> object:
        raise RuntimeError("SYNTHETIC PRIVATE ITERATOR DETAIL")
        yield object()

    with pytest.raises(SSEInstrumentCensusError, match="pages_invalid"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=broken_pages())  # type: ignore[arg-type]

    valid_page = _evidence()
    with pytest.raises(SSEInstrumentCensusError, match="page_count_exceeded"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=(valid_page,) * 129)


def test_capture_rejects_noncontiguous_direct_page_evidence() -> None:
    with pytest.raises(SSEInstrumentCensusError, match="page_sequence_not_contiguous"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=(_evidence(sequence_number=2),))


@pytest.mark.parametrize("observed_at", [None, "2026-10-30", datetime(2026, 10, 30)])
def test_capture_rejects_non_datetime_or_naive_observation_times(observed_at: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="observed_at_invalid"):
        SSEInstrumentCensusCapture(
            observed_at=observed_at,  # type: ignore[arg-type]
            pages=(),
        )


def test_capture_normalizes_timezone_offset_exceptions() -> None:
    class BrokenTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            del dt
            raise ValueError("SYNTHETIC PRIVATE CAPTURE TZ DETAIL")

        def dst(self, dt: datetime | None) -> timedelta | None:
            del dt
            return None

        def tzname(self, dt: datetime | None) -> str:
            del dt
            return "synthetic"

    observed_at = datetime(2026, 10, 30, tzinfo=BrokenTimezone())
    with pytest.raises(SSEInstrumentCensusError, match="observed_at_invalid"):
        SSEInstrumentCensusCapture(observed_at=observed_at, pages=())


@pytest.mark.parametrize("pages", ["not-pages", 1, {}])
def test_capture_rejects_non_iterable_or_scalar_page_containers(pages: object) -> None:
    with pytest.raises(SSEInstrumentCensusError, match="pages_invalid"):
        SSEInstrumentCensusCapture(observed_at=_OBSERVED_AT, pages=pages)  # type: ignore[arg-type]


def test_builder_checks_aggregate_response_bytes_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b"x" * MAX_SSE_CENSUS_BODY_BYTES
    digest = hashlib.sha256(body).hexdigest()
    pages = tuple(
        SSEInstrumentPageInput(
            selector=SSEInstrumentSelector.CURRENT_MAIN_A,
            sequence_number=sequence,
            raw_response_body=body,
            declared_response_sha256=digest,
        )
        for sequence in range(1, 6)
    )
    monkeypatch.setattr(census, "_parse_sse_response", lambda raw, *, max_rows: ((), None))

    with pytest.raises(SSEInstrumentCensusError, match="capture_body_total_too_large"):
        build_sse_instrument_census(pages, observed_at=_OBSERVED_AT)


@pytest.mark.parametrize("failure_kind", ["expected", "unexpected"])
def test_builder_normalizes_capture_construction_failures(
    failure_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_capture(**kwargs: object) -> object:
        del kwargs
        if failure_kind == "expected":
            raise SSEInstrumentCensusError("stable_capture_error")
        raise RuntimeError("SYNTHETIC PRIVATE CAPTURE DETAIL")

    monkeypatch.setattr(census, "SSEInstrumentCensusCapture", fail_capture)
    with pytest.raises(SSEInstrumentCensusError) as raised:
        build_sse_instrument_census([], observed_at=_OBSERVED_AT)

    expected_code = "stable_capture_error" if failure_kind == "expected" else "capture_invalid"
    assert raised.value.code == expected_code
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


@pytest.mark.parametrize(
    ("body", "max_rows", "expected_code"),
    [
        (bytearray(b"{}"), 10, "response_body_type_invalid"),
        (b"", 10, "response_body_empty"),
        (b"x" * (MAX_SSE_CENSUS_BODY_BYTES + 1), 10, "response_body_too_large"),
        (_body([{"code": "600001"}]), 0, "response_row_count_exceeded"),
        (b'{"result": }', 10, "response_json_invalid"),
        (b"]", 10, "response_json_invalid"),
    ],
)
def test_response_parser_direct_bounds_and_syntax_failures(
    body: object, max_rows: int, expected_code: str
) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        census._parse_sse_response(body, max_rows=max_rows)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


def test_response_parser_redacts_unexpected_row_constructor_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_row(**kwargs: object) -> object:
        del kwargs
        raise RuntimeError("SYNTHETIC PRIVATE ROW DETAIL")

    monkeypatch.setattr(census, "SSEInstrumentSourceObservation", fail_row)
    with pytest.raises(SSEInstrumentCensusError) as raised:
        census._parse_sse_response(_body([{"code": "600001"}]), max_rows=1)

    assert raised.value.code == "response_row_invalid"
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


def test_response_parser_rejects_non_mapping_frozen_page_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census, "_freeze_json", lambda *args, **kwargs: None)

    with pytest.raises(SSEInstrumentCensusError, match="response_page_help_invalid"):
        census._parse_sse_response(_body([], page_help={"pageNo": 1}), max_rows=1)


def test_response_parser_keeps_finite_float_tokens_as_raw_numbers() -> None:
    capture = build_sse_instrument_census(
        [_page(_body([{"count": 3, "ratio": 1.25}]))], observed_at=_OBSERVED_AT
    )

    row = capture.pages[0].source_rows[0].raw_row
    assert row["count"] == 3
    assert row["ratio"] == 1.25


@pytest.mark.parametrize(
    ("value", "kwargs", "expected_code"),
    [
        (object(), {}, "json_value_type_invalid"),
        (1 << (census._MAX_JSON_INTEGER_BITS + 1), {}, "json_integer_too_large"),
        (float("inf"), {}, "json_nonfinite_number"),
        ("x" * (census._MAX_JSON_TEXT_CHARS + 1), {}, "json_text_too_large"),
        ("x", {"byte_limit": 0}, "json_value_too_large"),
        ({"": "value"}, {}, "json_field_name_invalid"),
        ({1: "value"}, {}, "json_field_name_invalid"),
        ({"x" * 129: "value"}, {}, "json_field_name_invalid"),
        ({"bad\nkey": "value"}, {}, "json_field_name_invalid"),
        ({"long": "x"}, {"byte_limit": 1}, "json_value_too_large"),
        ("\ud800", {}, "json_text_invalid_unicode"),
    ],
)
def test_freeze_json_rejects_unsafe_scalar_and_field_boundaries(
    value: object, kwargs: dict[str, object], expected_code: str
) -> None:
    with pytest.raises(SSEInstrumentCensusError) as raised:
        census._freeze_json(value, **kwargs)  # type: ignore[arg-type]

    assert raised.value.code == expected_code


def test_freeze_json_enforces_container_depth_node_and_field_limits() -> None:
    too_many_fields = {f"field_{index}": None for index in range(census._MAX_JSON_FIELDS + 1)}
    too_many_items = [None] * (census._MAX_JSON_CONTAINER_ITEMS + 1)
    with pytest.raises(SSEInstrumentCensusError, match="json_object_too_large"):
        census._freeze_json(too_many_fields)
    with pytest.raises(SSEInstrumentCensusError, match="json_array_too_large"):
        census._freeze_json(too_many_items)
    with pytest.raises(SSEInstrumentCensusError, match="json_depth_exceeded"):
        census._freeze_json(None, depth=census._MAX_JSON_DEPTH + 1)
    with pytest.raises(SSEInstrumentCensusError, match="json_nodes_exceeded"):
        census._freeze_json(None, node_budget=[census._MAX_JSON_NODES])


def test_freeze_json_accepts_finite_values_and_freezes_mapping_proxy() -> None:
    frozen = census._freeze_json({"values": [1, 1.25, False, None]})

    assert frozen == {"values": (1, 1.25, False, None)}
    assert census._freeze_json(frozen) == frozen


def test_canonical_summary_encoder_normalizes_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_thaw(value: object) -> object:
        del value
        raise RuntimeError("SYNTHETIC PRIVATE ENCODER DETAIL")

    monkeypatch.setattr(census, "_thaw_json", fail_thaw)
    with pytest.raises(SSEInstrumentCensusError) as raised:
        census._encode_canonical_summary({"safe": "summary"})

    assert raised.value.code == "request_summary_invalid"
    assert "SYNTHETIC PRIVATE" not in str(raised.value)


def test_canonical_summary_encoder_preserves_stable_parser_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_thaw(value: object) -> object:
        del value
        raise SSEInstrumentCensusError("stable_encoder_error")

    monkeypatch.setattr(census, "_thaw_json", fail_thaw)
    with pytest.raises(SSEInstrumentCensusError) as raised:
        census._encode_canonical_summary({"safe": "summary"})

    assert raised.value.code == "stable_encoder_error"


def test_preflight_stops_before_decoding_excessive_json_nodes() -> None:
    value_count = census._MAX_JSON_NODES + 1
    text = '{"result":[' + "0," * (value_count - 1) + "0]}"

    with pytest.raises(SSEInstrumentCensusError, match="response_json_nodes_exceeded"):
        census._preflight_json(text)
