"""Synthetic contract tests for the offline temporal identity resolver."""

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timedelta, timezone
from itertools import repeat

import pytest

from opendata.data.temporal_instrument_identity import (
    Exchange,
    IdentityResolution,
    IdentityResolutionSegment,
    IdentityResolutionStatus,
    MarketSymbolPair,
    ResolutionSubject,
    TemporalIdentityAssignment,
    TemporalIdentityRegistry,
    TemporalInstrumentIdentityError,
)


def d(value: str) -> date:
    """Build literal test dates."""
    return date.fromisoformat(value)


def assignment(
    assignment_id: str,
    instrument_id: str,
    exchange: str,
    symbol: str,
    start: str,
    end: str | None,
    *,
    source_id: str = "fixture_source",
    source_version: str = "fixture-v1",
    evidence_ref: str | None = None,
    source_sha256: str | None = None,
) -> TemporalIdentityAssignment:
    """Build an explicitly synthetic, reviewed-shape assignment row."""
    return TemporalIdentityAssignment(
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        exchange=exchange,
        symbol=symbol,
        valid_from=d(start),
        valid_to=d(end) if end is not None else None,
        source_id=source_id,
        source_version=source_version,
        evidence_ref=evidence_ref or f"TEST-EVIDENCE-{assignment_id}",
        source_sha256=source_sha256,
    )


def registry(*rows: TemporalIdentityAssignment) -> TemporalIdentityRegistry:
    """Build a versioned registry with synthetic evidence pointers."""
    return TemporalIdentityRegistry(
        reference_version="TEST-REFERENCE-v1",
        evidence_ref="TEST-REFERENCE-REVIEW",
        assignments=rows,
    )


def unknown_segment(start: str, end: str) -> IdentityResolutionSegment:
    """Build an explicit unknown interval for constructor contract tests."""
    return IdentityResolutionSegment(
        valid_from=d(start),
        valid_to=d(end),
        status=IdentityResolutionStatus.UNKNOWN,
    )


def known_segment(
    start: str,
    end: str,
    instrument_id: str,
    exchange: str,
    symbol: str,
    *,
    source_sha256: str | None = None,
) -> IdentityResolutionSegment:
    """Build one evidence-bearing segment for resolution constructor tests."""
    return IdentityResolutionSegment(
        valid_from=d(start),
        valid_to=d(end),
        status=IdentityResolutionStatus.KNOWN,
        assignment_id="segment-a",
        instrument_id=instrument_id,
        pair=MarketSymbolPair(exchange, symbol),
        source_id="fixture_source",
        source_version="fixture-v1",
        evidence_ref="TEST-SEGMENT-EVIDENCE",
        source_sha256=source_sha256,
    )


def resolution(
    subject: ResolutionSubject | str,
    pair: MarketSymbolPair | None,
    instrument_id: str | None,
    segments: object,
) -> IdentityResolution:
    """Build a fixed request window while allowing malformed-input tests."""
    return IdentityResolution(
        subject=subject,
        pair=pair,
        instrument_id=instrument_id,
        reference_version="TEST-REFERENCE-v1",
        reference_evidence_ref="TEST-REFERENCE-REVIEW",
        window_start=d("2024-01-01"),
        window_end=d("2024-01-03"),
        segments=segments,  # type: ignore[arg-type]
    )


def test_pair_requires_explicit_supported_exchange_and_ascii_six_digit_code() -> None:
    pair = MarketSymbolPair("XSHG", "600000")

    assert pair.exchange is Exchange.XSHG
    assert pair.symbol == "600000"
    with pytest.raises(TemporalInstrumentIdentityError, match="explicitly"):
        MarketSymbolPair("SH", "600000")
    with pytest.raises(TemporalInstrumentIdentityError, match="explicitly"):
        MarketSymbolPair("600000", "600000")  # type: ignore[arg-type]
    with pytest.raises(TemporalInstrumentIdentityError, match="ASCII digits"):
        MarketSymbolPair("XSHE", "１２３４５６")
    with pytest.raises(TemporalInstrumentIdentityError, match="six ASCII digits"):
        MarketSymbolPair("XBSE", "12345")


def test_assignment_uses_opaque_ids_and_validates_dates_and_evidence_shape() -> None:
    row = assignment(
        "assignment-A",
        "opaque:instrument-42",
        "XBSE",
        "430001",
        "2024-01-01",
        None,
        source_sha256="a" * 64,
    )

    assert row.instrument_id == "opaque:instrument-42"
    assert row.pair == MarketSymbolPair("XBSE", "430001")
    assert row.valid_to is None
    assert row.source_sha256 == "a" * 64
    with pytest.raises(TemporalInstrumentIdentityError, match="after valid_from"):
        assignment("empty", "id-1", "XSHG", "600000", "2024-01-01", "2024-01-01")
    with pytest.raises(TemporalInstrumentIdentityError, match="datetime.date"):
        TemporalIdentityAssignment(
            assignment_id="bad-date",
            instrument_id="id-1",
            exchange="XSHG",
            symbol="600000",
            valid_from=datetime(2024, 1, 1, tzinfo=timezone.utc),  # type: ignore[arg-type]
            valid_to=None,
            source_id="fixture_source",
            source_version="fixture-v1",
            evidence_ref="TEST-EVIDENCE",
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="64 lowercase"):
        assignment(
            "bad-hash",
            "id-1",
            "XSHG",
            "600000",
            "2024-01-01",
            None,
            source_sha256="A" * 64,
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="evidence reference"):
        assignment(
            "bad-evidence",
            "opaque-A",
            "XSHG",
            "600000",
            "2024-01-01",
            None,
            evidence_ref=" padded ",
        )


def test_segment_validation_rejects_empty_status_and_unbound_evidence() -> None:
    with pytest.raises(TemporalInstrumentIdentityError, match="nonempty"):
        unknown_segment("2024-01-01", "2024-01-01")
    with pytest.raises(TemporalInstrumentIdentityError, match="resolution status"):
        IdentityResolutionSegment(
            d("2024-01-01"),
            d("2024-01-02"),
            "unrecognized",  # type: ignore[arg-type]
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="cannot carry"):
        IdentityResolutionSegment(
            d("2024-01-01"),
            d("2024-01-02"),
            IdentityResolutionStatus.UNKNOWN,
            assignment_id="orphan",
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="require their assignment"):
        IdentityResolutionSegment(
            d("2024-01-01"),
            d("2024-01-02"),
            IdentityResolutionStatus.KNOWN,
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="64 lowercase"):
        known_segment(
            "2024-01-01",
            "2024-01-02",
            "opaque-A",
            "XSHG",
            "600000",
            source_sha256="z" * 64,
        )


def test_resolution_validation_rejects_malformed_subject_and_segments() -> None:
    valid_unknown = (unknown_segment("2024-01-01", "2024-01-03"),)
    pair = MarketSymbolPair("XSHG", "600000")
    with pytest.raises(TemporalInstrumentIdentityError, match="invalid resolution subject"):
        resolution("other", None, None, valid_unknown)
    with pytest.raises(TemporalInstrumentIdentityError, match="pair resolution requires"):
        resolution(ResolutionSubject.PAIR, None, None, valid_unknown)
    with pytest.raises(TemporalInstrumentIdentityError, match="instrument resolution requires"):
        resolution(ResolutionSubject.INSTRUMENT, pair, "opaque-A", valid_unknown)
    with pytest.raises(TemporalInstrumentIdentityError, match="bounded segments"):
        resolution(ResolutionSubject.PAIR, pair, None, ())
    with pytest.raises(TemporalInstrumentIdentityError, match="bounded segments"):
        resolution(ResolutionSubject.PAIR, pair, None, list(valid_unknown))
    with pytest.raises(TemporalInstrumentIdentityError, match="must contain"):
        resolution(ResolutionSubject.PAIR, pair, None, (object(),))


def test_resolution_validation_rejects_gaps_overrun_and_wrong_subject_rows() -> None:
    pair = MarketSymbolPair("XSHG", "600000")
    with pytest.raises(TemporalInstrumentIdentityError, match="without gaps or overlap"):
        resolution(
            ResolutionSubject.PAIR,
            pair,
            None,
            (unknown_segment("2024-01-02", "2024-01-03"),),
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="without gaps or overlap"):
        resolution(
            ResolutionSubject.PAIR,
            pair,
            None,
            (
                unknown_segment("2024-01-01", "2024-01-03"),
                unknown_segment("2024-01-02", "2024-01-03"),
            ),
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="exceeds its request window"):
        resolution(
            ResolutionSubject.PAIR,
            pair,
            None,
            (unknown_segment("2024-01-01", "2024-01-04"),),
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="another pair"):
        resolution(
            ResolutionSubject.PAIR,
            pair,
            None,
            (known_segment("2024-01-01", "2024-01-03", "opaque-A", "XSHE", "000001"),),
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="another instrument_id"):
        resolution(
            ResolutionSubject.INSTRUMENT,
            None,
            "opaque-A",
            (known_segment("2024-01-01", "2024-01-03", "opaque-B", "XSHE", "000001"),),
        )
    with pytest.raises(TemporalInstrumentIdentityError, match="complete request window"):
        resolution(
            ResolutionSubject.PAIR,
            pair,
            None,
            (unknown_segment("2024-01-01", "2024-01-02"),),
        )


def test_pair_resolution_partitions_request_and_preserves_unknown_gaps() -> None:
    snapshot = registry(
        assignment("a1", "opaque-A", "XSHG", "600000", "2024-01-03", "2024-01-05"),
        assignment("a2", "opaque-B", "XSHG", "600000", "2024-01-05", "2024-01-07"),
    )

    result = snapshot.resolve_pair(
        MarketSymbolPair("XSHG", "600000"), d("2024-01-01"), d("2024-01-10")
    )

    assert result.reference_version == "TEST-REFERENCE-v1"
    assert result.reference_evidence_ref == "TEST-REFERENCE-REVIEW"
    assert [
        (segment.valid_from, segment.valid_to, segment.status, segment.instrument_id)
        for segment in result.segments
    ] == [
        (d("2024-01-01"), d("2024-01-03"), IdentityResolutionStatus.UNKNOWN, None),
        (d("2024-01-03"), d("2024-01-05"), IdentityResolutionStatus.KNOWN, "opaque-A"),
        (d("2024-01-05"), d("2024-01-07"), IdentityResolutionStatus.KNOWN, "opaque-B"),
        (d("2024-01-07"), d("2024-01-10"), IdentityResolutionStatus.UNKNOWN, None),
    ]
    assert result.segments[1].evidence_ref == "TEST-EVIDENCE-a1"


def test_adjacent_pair_assignments_handoff_at_exclusive_end() -> None:
    snapshot = registry(
        assignment("before", "opaque-A", "XSHG", "600000", "2024-01-01", "2024-01-05"),
        assignment("after", "opaque-B", "XSHG", "600000", "2024-01-05", "2024-01-09"),
    )

    result = snapshot.resolve_pair(
        MarketSymbolPair("XSHG", "600000"), d("2024-01-04"), d("2024-01-06")
    )

    assert [
        (segment.valid_from, segment.valid_to, segment.instrument_id) for segment in result.segments
    ] == [
        (d("2024-01-04"), d("2024-01-05"), "opaque-A"),
        (d("2024-01-05"), d("2024-01-06"), "opaque-B"),
    ]


def test_overlapping_different_identities_for_one_pair_are_rejected() -> None:
    with pytest.raises(TemporalInstrumentIdentityError, match="must not overlap"):
        registry(
            assignment("a1", "opaque-A", "XSHE", "000001", "2024-01-01", "2024-01-06"),
            assignment("a2", "opaque-B", "XSHE", "000001", "2024-01-05", None),
        )


def test_same_bare_code_on_different_exchanges_is_allowed_and_distinct() -> None:
    snapshot = registry(
        assignment("sh", "opaque-SH", "XSHG", "600000", "2024-01-01", None),
        assignment("sz", "opaque-SZ", "XSHE", "600000", "2024-01-01", None),
    )

    shanghai = snapshot.resolve_pair(
        MarketSymbolPair("XSHG", "600000"), d("2024-02-01"), d("2024-02-02")
    )
    shenzhen = snapshot.resolve_pair(
        MarketSymbolPair("XSHE", "600000"), d("2024-02-01"), d("2024-02-02")
    )

    assert shanghai.segments[0].instrument_id == "opaque-SH"
    assert shenzhen.segments[0].instrument_id == "opaque-SZ"


def test_unknown_pair_is_unknown_for_entire_window_without_lifecycle_inference() -> None:
    result = registry().resolve_pair(
        MarketSymbolPair("XBSE", "430001"), d("2000-01-01"), d("2010-01-01")
    )

    assert len(result.segments) == 1
    assert result.segments[0].status is IdentityResolutionStatus.UNKNOWN
    assert result.segments[0].valid_from == d("2000-01-01")
    assert result.segments[0].valid_to == d("2010-01-01")
    assert result.segments[0].instrument_id is None


def test_pair_rows_outside_request_do_not_change_unknown_result() -> None:
    snapshot = registry(assignment("future", "opaque-A", "XBSE", "430001", "2024-01-01", None))

    result = snapshot.resolve_pair(
        MarketSymbolPair("XBSE", "430001"), d("2020-01-01"), d("2021-01-01")
    )

    assert len(result.segments) == 1
    assert result.segments[0].status is IdentityResolutionStatus.UNKNOWN
    assert result.segments[0].valid_from == d("2020-01-01")
    assert result.segments[0].valid_to == d("2021-01-01")


def test_instrument_resolution_returns_sequential_pair_chain_and_unknown_gap() -> None:
    snapshot = registry(
        assignment("first", "opaque-A", "XSHG", "600000", "2020-01-01", "2021-01-01"),
        assignment("second", "opaque-A", "XSHE", "000001", "2022-01-01", "2024-01-01"),
    )

    result = snapshot.resolve_instrument("opaque-A", d("2020-01-01"), d("2024-01-01"))

    assert [
        (segment.valid_from, segment.valid_to, segment.status, segment.pair)
        for segment in result.segments
    ] == [
        (
            d("2020-01-01"),
            d("2021-01-01"),
            IdentityResolutionStatus.KNOWN,
            MarketSymbolPair("XSHG", "600000"),
        ),
        (d("2021-01-01"), d("2022-01-01"), IdentityResolutionStatus.UNKNOWN, None),
        (
            d("2022-01-01"),
            d("2024-01-01"),
            IdentityResolutionStatus.KNOWN,
            MarketSymbolPair("XSHE", "000001"),
        ),
    ]


def test_instrument_resolution_fails_closed_only_when_requested_window_is_ambiguous() -> None:
    snapshot = registry(
        assignment("sh", "opaque-A", "XSHG", "600000", "2024-01-01", "2024-01-10"),
        assignment("sz", "opaque-A", "XSHE", "000001", "2024-01-05", "2024-01-15"),
    )

    before_overlap = snapshot.resolve_instrument("opaque-A", d("2024-01-01"), d("2024-01-05"))
    assert before_overlap.segments[0].pair == MarketSymbolPair("XSHG", "600000")
    with pytest.raises(TemporalInstrumentIdentityError, match="multiple pairs"):
        snapshot.resolve_instrument("opaque-A", d("2024-01-04"), d("2024-01-06"))


def test_open_ended_assignment_is_clipped_to_finite_query_end() -> None:
    snapshot = registry(assignment("current", "opaque-A", "XBSE", "430001", "2024-01-01", None))

    result = snapshot.resolve_instrument("opaque-A", d("2024-01-03"), d("2024-01-08"))

    assert len(result.segments) == 1
    assert result.segments[0].valid_from == d("2024-01-03")
    assert result.segments[0].valid_to == d("2024-01-08")


def test_registry_sorting_and_input_copy_are_deterministic() -> None:
    rows = [
        assignment("b", "opaque-B", "XSHE", "000001", "2024-01-01", None),
        assignment("a", "opaque-A", "XSHG", "600000", "2024-01-01", None),
    ]
    first = TemporalIdentityRegistry("TEST-v1", "TEST-REF", rows)
    second = TemporalIdentityRegistry("TEST-v1", "TEST-REF", reversed(rows))
    rows.clear()

    assert first.assignments == second.assignments
    assert [row.pair for row in first.assignments] == [
        MarketSymbolPair("XSHE", "000001"),
        MarketSymbolPair("XSHG", "600000"),
    ]


def test_registry_and_pair_index_are_immutable_after_construction() -> None:
    snapshot = registry(assignment("a", "opaque-A", "XSHG", "600000", "2024-01-01", None))

    with pytest.raises(FrozenInstanceError):
        snapshot.assignments = ()  # type: ignore[misc]
    with pytest.raises(TypeError):
        snapshot._by_pair[MarketSymbolPair("XSHE", "000001")] = ()  # type: ignore[index]


def test_assignment_ids_must_be_unique_and_iterable_is_bounded() -> None:
    row = assignment("same", "opaque-A", "XSHG", "600000", "2024-01-01", None)
    with pytest.raises(TemporalInstrumentIdentityError, match="must be unique"):
        registry(row, row)
    with pytest.raises(TemporalInstrumentIdentityError, match="100,000-assignment limit"):
        TemporalIdentityRegistry("TEST-v1", "TEST-REF", repeat(row, 100_001))  # type: ignore[arg-type]
    with pytest.raises(TemporalInstrumentIdentityError, match="bounded iterable"):
        TemporalIdentityRegistry("TEST-v1", "TEST-REF", {"not": "rows"})  # type: ignore[arg-type]
    with pytest.raises(TemporalInstrumentIdentityError, match="TemporalIdentityAssignment"):
        TemporalIdentityRegistry("TEST-v1", "TEST-REF", (object(),))  # type: ignore[arg-type]

    class BrokenIterable:
        """Expose the iterable protocol but fail when an iterator is requested."""

        def __iter__(self) -> Iterator[object]:
            raise TypeError("fixture iterator failure")

    with pytest.raises(TemporalInstrumentIdentityError, match="bounded iterable"):
        TemporalIdentityRegistry("TEST-v1", "TEST-REF", BrokenIterable())  # type: ignore[arg-type]


def test_lazy_iterable_is_type_checked_before_retaining_or_consuming_more_rows() -> None:
    consumed: list[str] = []
    valid_row = assignment("later", "opaque-A", "XSHG", "600000", "2024-01-01", None)

    def lazy_rows() -> Iterator[object]:
        """Yield one oversized invalid item before a sentinel row."""
        consumed.append("first")
        yield "x" * 1_000_000
        consumed.append("second")
        yield valid_row

    with pytest.raises(TemporalInstrumentIdentityError, match="TemporalIdentityAssignment"):
        TemporalIdentityRegistry("TEST-v1", "TEST-REF", lazy_rows())  # type: ignore[arg-type]

    assert consumed == ["first"]


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (date(2024, 1, 1), date(2024, 1, 1), "nonempty"),
        (datetime(2024, 1, 1, tzinfo=timezone.utc), date(2024, 1, 2), "datetime.date"),
        (date(2024, 1, 1), date(2035, 1, 1), "3,660-calendar-day limit"),
    ],
)
def test_query_window_must_be_finite_strict_and_bounded(
    start: date, end: date, message: str
) -> None:
    with pytest.raises(TemporalInstrumentIdentityError, match=message):
        registry().resolve_pair(MarketSymbolPair("XSHG", "600000"), start, end)


def test_exact_window_cap_is_permitted() -> None:
    start = d("2020-01-01")
    end = start + timedelta(days=3_660)

    result = registry().resolve_pair(MarketSymbolPair("XSHG", "600000"), start, end)

    assert result.window_start == start
    assert result.window_end == end
    assert len(result.segments) == 1


def test_lookup_requires_typed_pair_and_valid_opaque_instrument_id() -> None:
    snapshot = registry()
    with pytest.raises(TemporalInstrumentIdentityError, match="MarketSymbolPair"):
        snapshot.resolve_pair(("XSHG", "600000"), d("2024-01-01"), d("2024-01-02"))  # type: ignore[arg-type]
    with pytest.raises(TemporalInstrumentIdentityError, match="stable identifier"):
        snapshot.resolve_instrument("", d("2024-01-01"), d("2024-01-02"))
