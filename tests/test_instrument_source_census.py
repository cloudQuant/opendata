"""Behavioral tests for raw instrument observations and pagination evidence."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

import pytest

from opendata.data import instrument_source_census as census_module
from opendata.data.instrument_source_census import (
    FieldObservationState,
    InstrumentSourceCensus,
    InstrumentSourceCensusError,
    InstrumentSourceObservation,
    InstrumentSourcePage,
    PageAttemptStatus,
    PageReceipt,
    PaginationChainStatus,
    PaginationReason,
    ReportedTotalBasis,
    SourceFieldObservation,
    assess_source_pagination,
)

_OBSERVED_AT = datetime(2026, 10, 30, 2, 0, tzinfo=timezone.utc)


def _field(
    state: FieldObservationState,
    source_name: str | None = None,
    raw_value: object = None,
) -> SourceFieldObservation:
    """Build a caller-mapped source field without interpreting its meaning."""
    return SourceFieldObservation(state=state, source_name=source_name, raw_value=raw_value)


def _row(
    symbol: str,
    *,
    issuer: str = "Issuer",
    market: SourceFieldObservation | None = None,
    security_type: SourceFieldObservation | None = None,
    status: SourceFieldObservation | None = None,
    listing_date: SourceFieldObservation | None = None,
    last_trading_date: SourceFieldObservation | None = None,
    termination_date: SourceFieldObservation | None = None,
) -> InstrumentSourceObservation:
    """Create one raw source row with only explicitly supplied field mappings."""
    raw: dict[str, object] = {"code": symbol, "issuer": issuer}
    for observation in (
        market,
        security_type,
        status,
        listing_date,
        last_trading_date,
        termination_date,
    ):
        if observation is not None and observation.source_name is not None:
            if observation.state is FieldObservationState.VALUE:
                raw[observation.source_name] = observation.raw_value
            elif observation.state is FieldObservationState.NULL:
                raw[observation.source_name] = None
    return InstrumentSourceObservation(
        raw_row=raw,
        market=market or SourceFieldObservation(),
        security_type=security_type or SourceFieldObservation(),
        status=status or SourceFieldObservation(),
        listing_date=listing_date or SourceFieldObservation(),
        last_trading_date=last_trading_date or SourceFieldObservation(),
        termination_date=termination_date or SourceFieldObservation(),
    )


def _page(
    page_number: int,
    *,
    source_page_number: int | str | None = None,
    request_cursor: str | None,
    next_cursor: str | None,
    terminal: bool | None,
    reported_total: int | None,
    rows: Iterable[InstrumentSourceObservation],
    reported_total_basis: ReportedTotalBasis | str | None = None,
    attempt_status: PageAttemptStatus | str = PageAttemptStatus.SUCCEEDED,
    failure_code: str | None = None,
    receipt: PageReceipt | None = None,
    with_receipt: bool = True,
) -> InstrumentSourcePage:
    """Create one immutable page evidence value."""
    if reported_total is not None and reported_total_basis is None:
        reported_total_basis = ReportedTotalBasis.ROW_COUNT
    if (
        receipt is None
        and with_receipt
        and PageAttemptStatus(attempt_status) is PageAttemptStatus.SUCCEEDED
    ):
        receipt = PageReceipt(
            request_sha256=f"{page_number:064x}",
            response_sha256=f"{page_number + 10_000:064x}",
        )
    return InstrumentSourcePage(
        sequence_number=page_number,
        source_page_number=source_page_number,
        request_cursor=request_cursor,
        next_cursor=next_cursor,
        terminal=terminal,
        reported_total=reported_total,
        rows=cast("tuple[InstrumentSourceObservation, ...]", rows),
        reported_total_basis=reported_total_basis,
        attempt_status=attempt_status,
        failure_code=failure_code,
        receipt=receipt,
    )


def _census(*pages: InstrumentSourcePage) -> InstrumentSourceCensus:
    """Wrap explicit pages in fixed source and observation metadata."""
    return InstrumentSourceCensus(
        source_id="akshare.stock_list",
        observed_at=_OBSERVED_AT,
        pages=pages,
    )


def test_source_row_freezes_raw_data_and_keeps_unknown_market_and_events_separate() -> None:
    nested: dict[str, object] = {"feed": ["raw", 7]}
    raw: dict[str, object] = {
        "code": "600519",
        "market": None,
        "type": "",
        "status": "paused",
        "list_date": "2001-08-27",
        "last_trade_date": "2026-10-29",
        "termination_date": None,
        "nested": nested,
    }
    row = InstrumentSourceObservation(
        raw_row=raw,
        market=_field(FieldObservationState.NULL, "market"),
        security_type=_field(FieldObservationState.VALUE, "type", ""),
        status=_field(FieldObservationState.VALUE, "status", "paused"),
        listing_date=_field(FieldObservationState.VALUE, "list_date", "2001-08-27"),
        last_trading_date=_field(FieldObservationState.VALUE, "last_trade_date", "2026-10-29"),
        termination_date=_field(FieldObservationState.NULL, "termination_date"),
    )

    cast("list[object]", nested["feed"]).append("mutated")
    raw["new-field"] = "later caller mutation"

    assert row.raw_row["nested"] == {"feed": ("raw", 7)}
    assert "new-field" not in row.raw_row
    assert row.market.state is FieldObservationState.NULL
    assert row.security_type.raw_value == ""
    assert row.status.raw_value == "paused"
    assert row.listing_date.raw_value == "2001-08-27"
    assert row.last_trading_date.raw_value == "2026-10-29"
    assert row.termination_date.state is FieldObservationState.NULL
    assert row.market.raw_value is None


def test_unmapped_and_absent_fields_do_not_claim_unknown_values_from_symbol() -> None:
    row = _row(
        "600000",
        market=_field(FieldObservationState.ABSENT, "market"),
    )

    assert row.market.state is FieldObservationState.ABSENT
    assert row.market.source_name == "market"
    assert row.security_type.state is FieldObservationState.UNMAPPED
    assert row.security_type.source_name is None
    assert row.raw_row["code"] == "600000"


def test_unrecognized_market_and_security_type_values_remain_raw_source_facts() -> None:
    row = InstrumentSourceObservation(
        raw_row={"market": "exchange-like label", "class": "ordinary share"},
        market=_field(FieldObservationState.VALUE, "market", "exchange-like label"),
        security_type=_field(FieldObservationState.VALUE, "class", "ordinary share"),
    )

    assert row.market.raw_value == "exchange-like label"
    assert row.security_type.raw_value == "ordinary share"


@pytest.mark.parametrize(
    ("observation", "raw_row", "message"),
    [
        (
            _field(FieldObservationState.ABSENT, "market"),
            {"market": "SH"},
            "marked absent",
        ),
        (
            _field(FieldObservationState.NULL, "market"),
            {"market": "SH"},
            "marked null",
        ),
        (
            _field(FieldObservationState.VALUE, "market", "SH"),
            {"market": None},
            "does not match",
        ),
        (
            _field(FieldObservationState.VALUE, "market", True),
            {"market": 1},
            "does not match",
        ),
        (
            _field(FieldObservationState.VALUE, "market", "SH"),
            {"unrelated": "retained"},
            "does not exist",
        ),
    ],
)
def test_field_claim_must_match_exact_raw_row_value(
    observation: SourceFieldObservation,
    raw_row: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(InstrumentSourceCensusError, match=message):
        InstrumentSourceObservation(raw_row=raw_row, market=observation)


def test_unmapped_field_cannot_carry_a_name_or_value() -> None:
    with pytest.raises(InstrumentSourceCensusError, match="unmapped"):
        SourceFieldObservation(source_name="market")
    with pytest.raises(InstrumentSourceCensusError, match="unmapped"):
        SourceFieldObservation(raw_value="SH")


def test_lifecycle_dates_cannot_alias_one_source_field() -> None:
    source_date = _field(FieldObservationState.VALUE, "date", "2026-01-02")
    with pytest.raises(InstrumentSourceCensusError, match="separate source fields"):
        InstrumentSourceObservation(
            raw_row={"date": "2026-01-02"},
            listing_date=source_date,
            termination_date=source_date,
        )

    missing_date = _field(FieldObservationState.ABSENT, "missing-date")
    observation = InstrumentSourceObservation(
        raw_row={"code": "600000"},
        listing_date=missing_date,
        last_trading_date=missing_date,
        termination_date=missing_date,
    )
    assert observation.listing_date.state is FieldObservationState.ABSENT


def test_chain_consistency_requires_receipted_pages_and_matching_total() -> None:
    rows_one = (_row("600000"), _row("000001", issuer="Another"))
    rows_two = (_row("430001", issuer="BSE"),)
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="page-2",
                terminal=False,
                reported_total=3,
                rows=rows_one,
            ),
            _page(
                2,
                request_cursor="page-2",
                next_cursor=None,
                terminal=True,
                reported_total=3,
                rows=rows_two,
            ),
        )
    )

    assert result.chain_status is PaginationChainStatus.CHAIN_CONSISTENT
    assert result.page_count == 2
    assert result.row_count == 3
    assert result.reasons == ()


def test_consistent_page_chain_does_not_claim_full_market_coverage() -> None:
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=1,
                rows=(_row("600000"),),
            )
        )
    )

    assert result.chain_status is PaginationChainStatus.CHAIN_CONSISTENT
    assert result.page_count == 1
    assert result.row_count == 1


def test_empty_page_collection_is_unknown() -> None:
    result = assess_source_pagination(_census())

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert result.page_count == 0
    assert result.row_count == 0
    assert result.reasons == (PaginationReason.NO_PAGES,)


def test_empty_terminal_source_result_is_unknown_even_when_total_is_zero() -> None:
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=0,
                rows=(),
            )
        )
    )

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert result.row_count == 0
    assert result.reasons == (PaginationReason.EMPTY_PAGE,)


def test_source_page_number_zero_is_retained_separately_from_sequence_number() -> None:
    page = _page(
        1,
        source_page_number=0,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        rows=(_row("600000"),),
    )

    result = assess_source_pagination(_census(page))

    assert page.sequence_number == 1
    assert page.source_page_number == 0
    assert result.chain_status is PaginationChainStatus.CHAIN_CONSISTENT


def test_page_without_request_and_response_hashes_remains_unknown() -> None:
    page = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        rows=(_row("600000"),),
        with_receipt=False,
    )

    result = assess_source_pagination(_census(page))

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert result.reasons == (PaginationReason.PAGE_RECEIPT_MISSING,)


def test_failed_page_attempt_is_retained_and_never_proves_an_empty_source() -> None:
    page = _page(
        1,
        source_page_number=0,
        request_cursor=None,
        next_cursor=None,
        terminal=None,
        reported_total=None,
        rows=(),
        attempt_status=PageAttemptStatus.FAILED,
        failure_code="timeout",
        receipt=PageReceipt(request_sha256="a" * 64, response_sha256=None),
    )

    result = assess_source_pagination(_census(page))

    assert page.source_page_number == 0
    assert page.attempt_status is PageAttemptStatus.FAILED
    assert page.failure_code == "timeout"
    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.PAGE_ATTEMPT_FAILED in result.reasons
    assert PaginationReason.EMPTY_PAGE not in result.reasons


def test_unknown_page_attempt_is_retained_as_unknown() -> None:
    page = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=None,
        reported_total=None,
        rows=(),
        attempt_status=PageAttemptStatus.UNKNOWN,
    )

    result = assess_source_pagination(_census(page))

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.PAGE_ATTEMPT_UNKNOWN in result.reasons


@pytest.mark.parametrize("invalid_digest", ["", "g" * 64, "A" * 64, "f" * 63])
def test_page_receipt_requires_lowercase_sha256_syntax(invalid_digest: str) -> None:
    with pytest.raises(InstrumentSourceCensusError, match="SHA-256"):
        PageReceipt(request_sha256=invalid_digest, response_sha256=None)


def test_total_basis_is_caller_declared_and_unknown_basis_fails_closed() -> None:
    page_count_total = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        reported_total_basis=ReportedTotalBasis.PAGE_COUNT,
        rows=(_row("600000"),),
    )
    assert (
        assess_source_pagination(_census(page_count_total)).chain_status
        is PaginationChainStatus.CHAIN_CONSISTENT
    )

    unknown_basis = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        reported_total_basis=ReportedTotalBasis.UNKNOWN,
        rows=(_row("600000"),),
    )
    result = assess_source_pagination(_census(unknown_basis))

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert result.reasons == (PaginationReason.REPORTED_TOTAL_BASIS_UNKNOWN,)


def test_page_count_mismatch_and_mixed_total_basis_are_distinct() -> None:
    mismatch = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=2,
        reported_total_basis=ReportedTotalBasis.PAGE_COUNT,
        rows=(_row("600000"),),
    )
    result = assess_source_pagination(_census(mismatch))
    assert result.reasons == (PaginationReason.PAGE_COUNT_MISMATCH,)

    mixed = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="next",
                terminal=False,
                reported_total=2,
                reported_total_basis=ReportedTotalBasis.ROW_COUNT,
                rows=(_row("600000"),),
            ),
            _page(
                2,
                request_cursor="next",
                next_cursor=None,
                terminal=True,
                reported_total=2,
                reported_total_basis=ReportedTotalBasis.PAGE_COUNT,
                rows=(_row("000001"),),
            ),
        )
    )
    assert PaginationReason.REPORTED_TOTAL_BASIS_CONFLICT in mixed.reasons


def test_missing_or_contradictory_paging_evidence_remains_unknown() -> None:
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor="unproved-start",
                next_cursor=None,
                terminal=None,
                reported_total=None,
                rows=(_row("600000"),),
            )
        )
    )

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert result.reasons == (
        PaginationReason.INITIAL_CURSOR_NOT_EMPTY,
        PaginationReason.FINAL_PAGE_NOT_TERMINAL,
        PaginationReason.REPORTED_TOTAL_MISSING,
    )


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        (
            (
                _page(
                    1,
                    request_cursor=None,
                    next_cursor="cursor-2",
                    terminal=False,
                    reported_total=2,
                    rows=(_row("600000"),),
                ),
                _page(
                    3,
                    request_cursor="other-cursor",
                    next_cursor=None,
                    terminal=True,
                    reported_total=2,
                    rows=(_row("000001"),),
                ),
            ),
            {
                PaginationReason.PAGE_SEQUENCE_GAP,
                PaginationReason.CURSOR_CHAIN_BROKEN,
            },
        ),
        (
            (
                _page(
                    1,
                    request_cursor=None,
                    next_cursor="cursor-2",
                    terminal=True,
                    reported_total=2,
                    rows=(_row("600000"),),
                ),
                _page(
                    2,
                    request_cursor="cursor-2",
                    next_cursor=None,
                    terminal=True,
                    reported_total=2,
                    rows=(_row("000001"),),
                ),
            ),
            {PaginationReason.NONFINAL_PAGE_NOT_CONTINUABLE},
        ),
        (
            (
                _page(
                    1,
                    request_cursor=None,
                    next_cursor="cursor-2",
                    terminal=False,
                    reported_total=2,
                    rows=(_row("600000"),),
                ),
                _page(
                    2,
                    request_cursor="cursor-2",
                    next_cursor="extra",
                    terminal=True,
                    reported_total=2,
                    rows=(_row("000001"),),
                ),
            ),
            {PaginationReason.FINAL_PAGE_HAS_NEXT_CURSOR},
        ),
    ],
)
def test_page_gaps_and_terminal_contradictions_are_unknown(
    pages: tuple[InstrumentSourcePage, ...], expected: set[PaginationReason]
) -> None:
    result = assess_source_pagination(_census(*pages))

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert expected.issubset(set(result.reasons))


def test_repeated_cursor_and_empty_cursor_are_unknown() -> None:
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="repeat",
                terminal=False,
                reported_total=2,
                rows=(_row("600000"),),
            ),
            _page(
                2,
                request_cursor="repeat",
                next_cursor="repeat",
                terminal=False,
                reported_total=2,
                rows=(_row("000001"),),
            ),
            _page(
                3,
                request_cursor="repeat",
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(_row("430001"),),
            ),
        )
    )

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.CURSOR_REPEATED in result.reasons
    assert PaginationReason.ROW_COUNT_MISMATCH in result.reasons

    empty_cursor = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="",
                terminal=False,
                reported_total=2,
                rows=(_row("600000"),),
            ),
            _page(
                2,
                request_cursor="",
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(_row("000001"),),
            ),
        )
    )
    assert empty_cursor.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.CURSOR_EMPTY in empty_cursor.reasons


def test_reported_total_conflict_and_row_count_mismatch_are_distinct() -> None:
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="two",
                terminal=False,
                reported_total=3,
                rows=(_row("600000"),),
            ),
            _page(
                2,
                request_cursor="two",
                next_cursor=None,
                terminal=True,
                reported_total=4,
                rows=(_row("000001"),),
            ),
        )
    )

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.REPORTED_TOTAL_CONFLICT in result.reasons
    assert PaginationReason.ROW_COUNT_MISMATCH not in result.reasons

    count_mismatch = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=3,
                rows=(_row("600000"), _row("000001")),
            )
        )
    )
    assert count_mismatch.chain_status is PaginationChainStatus.UNKNOWN
    assert count_mismatch.reasons == (PaginationReason.ROW_COUNT_MISMATCH,)


def test_exact_duplicate_rows_make_page_proof_unknown_without_symbol_deduplication() -> None:
    duplicate = _row("600000")
    different_order_same_record = InstrumentSourceObservation(
        raw_row={"issuer": "Issuer", "code": "600000"}
    )
    result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(duplicate, different_order_same_record),
            )
        )
    )

    assert result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.DUPLICATE_SOURCE_ROW in result.reasons

    rich_first = InstrumentSourceObservation(
        raw_row={
            "null": None,
            "boolean": True,
            "integer": 2,
            "float": 1.25,
            "text": "source",
            "array": [None, False, 3, 2.5, {"nested": "value"}],
        }
    )
    rich_duplicate = InstrumentSourceObservation(
        raw_row={
            "array": [None, False, 3, 2.5, {"nested": "value"}],
            "text": "source",
            "float": 1.25,
            "integer": 2,
            "boolean": True,
            "null": None,
        }
    )
    rich_result = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(rich_first, rich_duplicate),
            )
        )
    )
    assert rich_result.chain_status is PaginationChainStatus.UNKNOWN
    assert PaginationReason.DUPLICATE_SOURCE_ROW in rich_result.reasons

    same_symbol_different_source_rows = assess_source_pagination(
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(_row("600000", issuer="One"), _row("600000", issuer="Two")),
            )
        )
    )
    assert same_symbol_different_source_rows.chain_status is PaginationChainStatus.CHAIN_CONSISTENT


def test_bad_raw_values_and_invalid_source_metadata_fail_closed() -> None:
    with pytest.raises(InstrumentSourceCensusError, match="finite"):
        InstrumentSourceObservation(raw_row={"bad": float("nan")})
    with pytest.raises(InstrumentSourceCensusError, match="source_id"):
        InstrumentSourceCensus("AkShare", _OBSERVED_AT, ())
    with pytest.raises(InstrumentSourceCensusError, match="timezone-aware"):
        InstrumentSourceCensus("akshare.list", datetime(2026, 10, 30), ())
    with pytest.raises(InstrumentSourceCensusError, match="sequence_number"):
        InstrumentSourcePage(
            sequence_number=0,
            source_page_number=0,
            request_cursor=None,
            next_cursor=None,
            terminal=True,
            reported_total=0,
            rows=(),
            reported_total_basis=ReportedTotalBasis.ROW_COUNT,
            attempt_status=PageAttemptStatus.SUCCEEDED,
        )
    with pytest.raises(InstrumentSourceCensusError, match="source_page_number"):
        _page(
            1,
            source_page_number=True,
            request_cursor=None,
            next_cursor=None,
            terminal=True,
            reported_total=1,
            rows=(_row("600000"),),
        )
    with pytest.raises(InstrumentSourceCensusError, match="integer limit"):
        _page(
            1,
            source_page_number=1 << 63,
            request_cursor=None,
            next_cursor=None,
            terminal=True,
            reported_total=1,
            rows=(_row("600000"),),
        )
    with pytest.raises(InstrumentSourceCensusError, match="reported_total"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=0,
            request_cursor=None,
            next_cursor=None,
            terminal=True,
            reported_total=-1,
            reported_total_basis=ReportedTotalBasis.ROW_COUNT,
            rows=(),
            attempt_status=PageAttemptStatus.SUCCEEDED,
        )
    with pytest.raises(InstrumentSourceCensusError, match="supplied together"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=None,
            request_cursor=None,
            next_cursor=None,
            terminal=True,
            reported_total=1,
            rows=(_row("600000"),),
            attempt_status=PageAttemptStatus.SUCCEEDED,
        )
    with pytest.raises(InstrumentSourceCensusError, match="require failure_code"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=0,
            request_cursor=None,
            next_cursor=None,
            terminal=None,
            reported_total=None,
            rows=(),
            attempt_status=PageAttemptStatus.FAILED,
        )
    with pytest.raises(InstrumentSourceCensusError, match="unknown page attempts"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=None,
            request_cursor=None,
            next_cursor=None,
            terminal=None,
            reported_total=None,
            rows=(),
            attempt_status=PageAttemptStatus.UNKNOWN,
            failure_code="unconfirmed",
        )
    with pytest.raises(InstrumentSourceCensusError, match="cannot declare response"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=0,
            request_cursor=None,
            next_cursor=None,
            terminal=None,
            reported_total=None,
            rows=(_row("600000"),),
            attempt_status=PageAttemptStatus.FAILED,
            failure_code="parse-error",
        )
    with pytest.raises(InstrumentSourceCensusError, match="bounded string"):
        InstrumentSourcePage(
            sequence_number=1,
            source_page_number=0,
            request_cursor=None,
            next_cursor="x" * 513,
            terminal=False,
            reported_total=0,
            reported_total_basis=ReportedTotalBasis.ROW_COUNT,
            rows=(),
            attempt_status=PageAttemptStatus.SUCCEEDED,
        )
    with pytest.raises(InstrumentSourceCensusError, match="raw source values"):
        InstrumentSourceObservation(raw_row={"bytes": b"not json"})
    with pytest.raises(InstrumentSourceCensusError, match="at least one source field"):
        InstrumentSourceObservation(raw_row={})
    with pytest.raises(InstrumentSourceCensusError, match="must be a mapping"):
        InstrumentSourceObservation(raw_row=None)  # type: ignore[arg-type]


def test_source_field_json_containers_are_immutable_and_type_sensitive() -> None:
    field_value: dict[str, object] = {"raw": [1, True, "x", 1.5]}
    observation = _field(FieldObservationState.VALUE, "metadata", field_value)
    row = InstrumentSourceObservation(raw_row={"metadata": field_value}, market=observation)
    cast("list[object]", field_value["raw"]).append("later")

    assert row.market.raw_value == {"raw": (1, True, "x", 1.5)}
    assert row.raw_row["metadata"] == {"raw": (1, True, "x", 1.5)}


def test_nested_field_value_mismatch_is_not_coerced() -> None:
    with pytest.raises(InstrumentSourceCensusError, match="does not match"):
        InstrumentSourceObservation(
            raw_row={"metadata": {"other": [1, 2]}},
            market=_field(FieldObservationState.VALUE, "metadata", {"expected": [1]}),
        )


def test_source_raw_value_depth_size_and_numeric_limits_fail_closed() -> None:
    too_many_fields = {f"field-{index}": index for index in range(257)}
    too_many_array_values = {"items": [0] * 4_097}
    too_deep: object = "leaf"
    for _ in range(14):
        too_deep = {"nested": too_deep}

    with pytest.raises(InstrumentSourceCensusError, match="too many fields"):
        InstrumentSourceObservation(raw_row=too_many_fields)
    with pytest.raises(InstrumentSourceCensusError, match="too many items"):
        InstrumentSourceObservation(raw_row=too_many_array_values)
    with pytest.raises(InstrumentSourceCensusError, match="nesting limit"):
        InstrumentSourceObservation(raw_row=too_deep)  # type: ignore[arg-type]
    with pytest.raises(InstrumentSourceCensusError, match="character limit"):
        InstrumentSourceObservation(raw_row={"name": "x" * 16_385})
    with pytest.raises(InstrumentSourceCensusError, match="numeric limit"):
        InstrumentSourceObservation(raw_row={"amount": 1 << 4_096})


def test_source_row_and_census_node_budgets_bound_recursive_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_module, "_MAX_RAW_NODES", 3)
    with pytest.raises(InstrumentSourceCensusError, match="node limit"):
        InstrumentSourceObservation(raw_row={"values": [1, 2]})

    monkeypatch.setattr(census_module, "_MAX_RAW_NODES", 20_000)
    monkeypatch.setattr(census_module, "_MAX_CENSUS_NODES", 5)
    first = InstrumentSourceObservation(raw_row={"values": [1]})
    second = InstrumentSourceObservation(raw_row={"values": [2]})
    with pytest.raises(InstrumentSourceCensusError, match="source-node limit"):
        _census(
            _page(
                1,
                request_cursor=None,
                next_cursor="next",
                terminal=False,
                reported_total=2,
                rows=(first,),
            ),
            _page(
                2,
                request_cursor="next",
                next_cursor=None,
                terminal=True,
                reported_total=2,
                rows=(second,),
            ),
        )


def test_raw_and_census_byte_budgets_reject_large_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wide_raw_row = {f"field-{index}": "x" * 12_000 for index in range(90)}
    with pytest.raises(InstrumentSourceCensusError, match="byte limit"):
        InstrumentSourceObservation(raw_row=wide_raw_row)

    wide_field_value = ["x" * 10_000 for _ in range(27)]
    with pytest.raises(InstrumentSourceCensusError, match="byte limit"):
        _field(FieldObservationState.VALUE, "field", wide_field_value)

    monkeypatch.setattr(census_module, "_MAX_CENSUS_BYTES", 5)
    row = InstrumentSourceObservation(raw_row={"field": "value"})
    page = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        rows=(row,),
    )
    with pytest.raises(InstrumentSourceCensusError, match="UTF-8-byte limit"):
        _census(page)


def test_census_stops_at_row_limit_before_scanning_later_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(census_module, "_MAX_ROWS", 1)
    observed: list[InstrumentSourceObservation] = []

    def record_usage(row: InstrumentSourceObservation) -> tuple[int, int]:
        observed.append(row)
        return 1, 1

    first = InstrumentSourceObservation(raw_row={"code": "600000"})
    second = InstrumentSourceObservation(raw_row={"code": "000001"})
    pages = (
        _page(
            1,
            request_cursor=None,
            next_cursor="next",
            terminal=False,
            reported_total=2,
            rows=(first,),
        ),
        _page(
            2,
            request_cursor="next",
            next_cursor=None,
            terminal=True,
            reported_total=2,
            rows=(second,),
        ),
    )
    monkeypatch.setattr(census_module, "_observation_resource_usage", record_usage)

    with pytest.raises(InstrumentSourceCensusError, match="row limit"):
        _census(*pages)
    assert observed == [first]


def test_bounded_row_generator_is_consumed_once() -> None:
    consumed: list[int] = []

    def row_generator() -> Iterator[InstrumentSourceObservation]:
        consumed.append(1)
        yield _row("600000")

    page = _page(
        1,
        request_cursor=None,
        next_cursor=None,
        terminal=True,
        reported_total=1,
        rows=row_generator(),
    )

    assert len(page.rows) == 1
    assert consumed == [1]


def test_assessor_rejects_non_census_input() -> None:
    with pytest.raises(InstrumentSourceCensusError, match="InstrumentSourceCensus"):
        assess_source_pagination(object())  # type: ignore[arg-type]
