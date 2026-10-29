"""Synthetic behavior tests for the identity-bound stock daily v2 core DTOs."""

from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pytest
from pydantic import ValidationError

from opendata.data.models.identity_bound_stock_v2 import (
    IdentityBoundStockDailyV2RawCandidate,
    IdentityBoundStockDailyV2RawRow,
    IdentityBoundStockDailyV2Window,
    IdentityBoundStockV2RawProvenance,
)
from opendata.data.temporal_identity_pins import TemporalIdentityPin
from opendata.data.temporal_instrument_identity import (
    Exchange,
    MarketSymbolPair,
    TemporalIdentityAssignment,
)


def d(value: str) -> date:
    """Return one literal calendar date for a synthetic contract case."""
    return date.fromisoformat(value)


def pin(
    *, dataset_id: str = "stock-daily-fixture", source_id: str = "akshare"
) -> TemporalIdentityPin:
    """Create an explicitly synthetic temporal identity pin."""
    return TemporalIdentityPin(
        dataset_id=dataset_id,
        source_id=source_id,
        authority_id="fixture-authority",
        registry_id="fixture-registry",
        reference_version="fixture-identity-v1",
        snapshot_sha256="a" * 64,
    )


def provenance(
    *, dataset_id: str = "stock-daily-fixture", adapter_id: str = "akshare"
) -> IdentityBoundStockV2RawProvenance:
    """Create bounded synthetic capture metadata with all trust states unknown."""
    return IdentityBoundStockV2RawProvenance(
        dataset_id=dataset_id,
        adapter_id=adapter_id,
        product_id="fixture.daily-bars",
        endpoint_id="daily-bars",
        adapter_version="fixture-v1",
        capture_id="b" * 32,
        request_sha256="c" * 64,
        response_sha256="d" * 64,
        response_size_bytes=128,
        captured_at_utc=datetime(2026, 1, 5, 8, 0, tzinfo=timezone.utc),
    )


def assignment(
    assignment_id: str,
    instrument_id: str,
    exchange: Exchange | str,
    symbol: str,
    valid_from: str,
    valid_to: str | None,
    *,
    source_id: str = "akshare",
) -> TemporalIdentityAssignment:
    """Create one synthetic, explicitly exchange-qualified identity interval."""
    return TemporalIdentityAssignment(
        assignment_id=assignment_id,
        instrument_id=instrument_id,
        exchange=exchange,
        symbol=symbol,
        valid_from=d(valid_from),
        valid_to=d(valid_to) if valid_to is not None else None,
        source_id=source_id,
        source_version="fixture-source-v1",
        evidence_ref=f"TEST-EVIDENCE-{assignment_id}",
    )


def raw_row(
    trade_date: str,
    *,
    source_row_index: int,
    assignment_claim_status: Literal["caller_declared_unverified", "unknown"] = (
        "caller_declared_unverified"
    ),
    assignment_value: TemporalIdentityAssignment | None,
    raw_tokens: tuple[str, ...] = ("2026-01-02", "10.00", "11.00"),
) -> IdentityBoundStockDailyV2RawRow:
    """Build a validated synthetic source row without interpreting its tokens."""
    return IdentityBoundStockDailyV2RawRow(
        trade_date=d(trade_date),
        source_row_index=source_row_index,
        raw_tokens=raw_tokens,
        assignment_claim_status=assignment_claim_status,
        assignment=assignment_value,
    )


def candidate(
    pair: MarketSymbolPair,
    start: str,
    end_exclusive: str,
    rows: tuple[IdentityBoundStockDailyV2RawRow, ...],
    *,
    identity_pin: TemporalIdentityPin | None = None,
    source_provenance: IdentityBoundStockV2RawProvenance | None = None,
) -> IdentityBoundStockDailyV2RawCandidate:
    """Build a raw-only candidate with explicit pair, window, pin, and provenance."""
    return IdentityBoundStockDailyV2RawCandidate(
        request_pair=pair,
        window=IdentityBoundStockDailyV2Window(start=d(start), end_exclusive=d(end_exclusive)),
        identity_pin=identity_pin or pin(),
        provenance=source_provenance or provenance(),
        rows=rows,
    )


def test_same_six_digit_code_is_separated_by_explicit_exchange_and_instrument() -> None:
    """Allow identical codes across exchanges while keeping their IDs distinct."""
    symbol = "600001"
    shanghai = candidate(
        MarketSymbolPair(Exchange.XSHG, symbol),
        "2026-01-01",
        "2026-01-04",
        (
            raw_row(
                "2026-01-02",
                source_row_index=1,
                assignment_value=assignment(
                    "sh-assignment", "instrument-sh", "XSHG", symbol, "2026-01-01", None
                ),
            ),
        ),
    )
    shenzhen = candidate(
        MarketSymbolPair(Exchange.XSHE, symbol),
        "2026-01-01",
        "2026-01-04",
        (
            raw_row(
                "2026-01-02",
                source_row_index=1,
                assignment_value=assignment(
                    "sz-assignment", "instrument-sz", "XSHE", symbol, "2026-01-01", None
                ),
            ),
        ),
    )

    assert shanghai.request_pair != shenzhen.request_pair
    assert shanghai.rows[0].assignment is not None
    assert shenzhen.rows[0].assignment is not None
    assert shanghai.rows[0].assignment.instrument_id == "instrument-sh"
    assert shenzhen.rows[0].assignment.instrument_id == "instrument-sz"
    with pytest.raises(ValidationError, match="assignment pair"):
        candidate(
            MarketSymbolPair(Exchange.XSHG, symbol),
            "2026-01-01",
            "2026-01-04",
            (
                raw_row(
                    "2026-01-02",
                    source_row_index=1,
                    assignment_value=assignment(
                        "wrong-market", "instrument-sz", "XSHE", symbol, "2026-01-01", None
                    ),
                ),
            ),
        )


def test_pair_code_reuse_uses_half_open_assignment_boundary() -> None:
    """Accept two identities for one pair across a shared exclusive boundary."""
    pair = MarketSymbolPair(Exchange.XSHG, "600002")
    first = assignment(
        "old-listing-code", "instrument-old", "XSHG", "600002", "2026-01-01", "2026-01-03"
    )
    second = assignment(
        "reused-listing-code", "instrument-new", "XSHG", "600002", "2026-01-03", None
    )
    result = candidate(
        pair,
        "2026-01-01",
        "2026-01-05",
        (
            raw_row("2026-01-02", source_row_index=1, assignment_value=first),
            raw_row("2026-01-03", source_row_index=2, assignment_value=second),
        ),
    )

    assert tuple(row.assignment.instrument_id for row in result.rows if row.assignment) == (
        "instrument-old",
        "instrument-new",
    )
    with pytest.raises(ValidationError, match="assignment half-open interval"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-05",
            (raw_row("2026-01-03", source_row_index=1, assignment_value=first),),
        )


def test_candidate_rejects_overlapping_or_conflicting_assignment_references() -> None:
    """Apply registry uniqueness rules to all assignment claims present in one batch."""
    pair = MarketSymbolPair(Exchange.XSHG, "600009")
    overlapping_a = assignment(
        "assignment-a", "instrument-a", "XSHG", "600009", "2026-01-01", "2026-01-05"
    )
    overlapping_b = assignment(
        "assignment-b", "instrument-b", "XSHG", "600009", "2026-01-02", "2026-01-04"
    )
    with pytest.raises(ValidationError, match="assignments .* overlap"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-05",
            (
                raw_row("2026-01-02", source_row_index=1, assignment_value=overlapping_a),
                raw_row("2026-01-03", source_row_index=2, assignment_value=overlapping_b),
            ),
        )

    reused_id_a = assignment(
        "reused-id", "instrument-a", "XSHG", "600009", "2026-01-01", "2026-01-03"
    )
    reused_id_b = assignment(
        "reused-id", "instrument-b", "XSHG", "600009", "2026-01-03", "2026-01-05"
    )
    with pytest.raises(ValidationError, match="assignment_id is reused"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-05",
            (
                raw_row("2026-01-02", source_row_index=1, assignment_value=reused_id_a),
                raw_row("2026-01-03", source_row_index=2, assignment_value=reused_id_b),
            ),
        )


def test_unknown_identity_is_explicit_and_cannot_carry_an_assignment() -> None:
    """Preserve unknown source rows without inventing or attaching an identity."""
    result = candidate(
        MarketSymbolPair(Exchange.XBSE, "430001"),
        "2026-01-01",
        "2026-01-04",
        (
            raw_row(
                "2026-01-02",
                source_row_index=1,
                assignment_claim_status="unknown",
                assignment_value=None,
                raw_tokens=("2026-01-02", "", "N/A"),
            ),
        ),
    )

    assert result.rows[0].assignment_claim_status == "unknown"
    assert result.rows[0].assignment is None
    assert result.provenance.history_coverage_status == "unknown"
    assert result.source_market_capability_status == "unknown"
    with pytest.raises(ValidationError, match="unknown identity rows"):
        raw_row(
            "2026-01-02",
            source_row_index=1,
            assignment_claim_status="unknown",
            assignment_value=assignment(
                "unknown-but-assigned", "instrument-x", "XBSE", "430001", "2026-01-01", None
            ),
        )
    with pytest.raises(
        ValidationError, match="Input should be 'caller_declared_unverified' or 'unknown'"
    ):
        IdentityBoundStockDailyV2RawRow.model_validate(
            {
                "trade_date": d("2026-01-02"),
                "source_row_index": 1,
                "raw_tokens": ("2026-01-02",),
                "assignment_claim_status": "known",
                "assignment": None,
            }
        )


def test_akshare_bse_assignment_claim_never_asserts_market_support_or_known_identity() -> None:
    """Keep BSE support and assignment provenance unknown for the AkShare raw route."""
    pair = MarketSymbolPair(Exchange.XBSE, "430002")
    result = candidate(
        pair,
        "2026-01-01",
        "2026-01-04",
        (
            raw_row(
                "2026-01-02",
                source_row_index=1,
                assignment_value=assignment(
                    "bse-claim", "instrument-bse", "XBSE", "430002", "2026-01-01", None
                ),
            ),
        ),
    )

    assert result.provenance.adapter_id == "akshare"
    assert result.source_market_capability_status == "unknown"
    assert result.rows[0].assignment_claim_status == "caller_declared_unverified"


def test_pin_dataset_source_and_assignment_source_must_match_declarations() -> None:
    """Reject cross-dataset or cross-adapter combinations without claiming trust."""
    pair = MarketSymbolPair(Exchange.XSHG, "600003")
    row = raw_row(
        "2026-01-02",
        source_row_index=1,
        assignment_value=assignment(
            "assignment", "instrument-3", "XSHG", "600003", "2026-01-01", None
        ),
    )
    with pytest.raises(ValidationError, match="pin dataset"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-04",
            (row,),
            identity_pin=pin(dataset_id="other-dataset"),
        )
    with pytest.raises(ValidationError, match="pin source"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-04",
            (row,),
            identity_pin=pin(source_id="ths"),
        )
    with pytest.raises(ValidationError, match="assignment source"):
        candidate(
            pair,
            "2026-01-01",
            "2026-01-04",
            (
                raw_row(
                    "2026-01-02",
                    source_row_index=1,
                    assignment_value=assignment(
                        "foreign-assignment",
                        "instrument-3",
                        "XSHG",
                        "600003",
                        "2026-01-01",
                        None,
                        source_id="ths",
                    ),
                ),
            ),
        )


def test_window_is_nonempty_bounded_and_end_exclusive() -> None:
    """Validate exact window boundaries, strict date types, and the finite cap."""
    accepted = candidate(
        MarketSymbolPair(Exchange.XSHG, "600004"),
        "2026-01-01",
        "2026-01-03",
        (
            raw_row(
                "2026-01-01",
                source_row_index=1,
                assignment_value=assignment(
                    "assignment", "instrument-4", "XSHG", "600004", "2026-01-01", None
                ),
            ),
        ),
    )
    assert accepted.rows[0].trade_date == accepted.window.start
    with pytest.raises(ValidationError, match="outside the half-open"):
        candidate(
            MarketSymbolPair(Exchange.XSHG, "600004"),
            "2026-01-01",
            "2026-01-03",
            (
                raw_row(
                    "2026-01-03",
                    source_row_index=1,
                    assignment_value=assignment(
                        "assignment", "instrument-4", "XSHG", "600004", "2026-01-01", None
                    ),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="nonempty"):
        IdentityBoundStockDailyV2Window(start=d("2026-01-03"), end_exclusive=d("2026-01-03"))
    boundary_start = d("2000-01-01")
    accepted_limit = IdentityBoundStockDailyV2Window(
        start=boundary_start,
        end_exclusive=date.fromordinal(boundary_start.toordinal() + 3_660),
    )
    assert accepted_limit.end_exclusive - accepted_limit.start == timedelta(days=3_660)
    with pytest.raises(ValidationError, match="3660-calendar-day"):
        IdentityBoundStockDailyV2Window(
            start=boundary_start,
            end_exclusive=date.fromordinal(boundary_start.toordinal() + 3_661),
        )
    with pytest.raises(ValidationError):
        IdentityBoundStockDailyV2Window(
            start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            end_exclusive=d("2026-01-02"),
        )


def test_candidate_rejects_implicit_identity_types_and_mixed_raw_values() -> None:
    """Reject bare pair mappings, coercive numeric tokens, booleans, and extra fields."""
    with pytest.raises(ValidationError, match="explicit MarketSymbolPair"):
        IdentityBoundStockDailyV2RawCandidate.model_validate(
            {
                "request_pair": {"symbol": "600005"},
                "window": {"start": "2026-01-01", "end_exclusive": "2026-01-03"},
                "identity_pin": pin(),
                "provenance": provenance(),
                "rows": (),
            }
        )
    with pytest.raises(ValidationError, match="valid integer"):
        raw_row(
            "2026-01-02",
            source_row_index=True,
            assignment_value=None,
            assignment_claim_status="unknown",
        )
    with pytest.raises(ValidationError, match="string"):
        IdentityBoundStockDailyV2RawRow(
            trade_date=d("2026-01-02"),
            source_row_index=1,
            raw_tokens=("2026-01-02", 10, "11.00"),  # type: ignore[arg-type]
            assignment_claim_status="unknown",
        )
    with pytest.raises(ValidationError, match="extra_forbidden"):
        IdentityBoundStockDailyV2Window(
            start=d("2026-01-01"),
            end_exclusive=d("2026-01-03"),
            inclusive_end=True,  # type: ignore[call-arg]
        )


def test_unverified_statuses_and_request_only_adjustment_are_not_overridable() -> None:
    """Keep source facts unknown and reject any qfq/hfq-capable candidate mode."""
    pair = MarketSymbolPair(Exchange.XSHG, "600006")
    row = raw_row(
        "2026-01-02",
        source_row_index=1,
        assignment_value=assignment(
            "assignment", "instrument-6", "XSHG", "600006", "2026-01-01", None
        ),
    )
    result = candidate(pair, "2026-01-01", "2026-01-04", (row,))

    assert result.candidate_status == "raw_candidate_only"
    assert result.pin_provenance_status == "caller_declared_unverified"
    assert result.source_market_capability_status == "unknown"
    assert result.provenance.source_authorization_status == "unknown"
    assert result.provenance.adjustment_semantics == "unknown"
    assert result.provenance.history_coverage_status == "unknown"
    with pytest.raises(ValidationError, match="Input should be 'none'"):
        IdentityBoundStockDailyV2RawCandidate(
            request_pair=pair,
            window=IdentityBoundStockDailyV2Window(
                start=d("2026-01-01"), end_exclusive=d("2026-01-04")
            ),
            identity_pin=pin(),
            requested_adjustment_mode="qfq",  # type: ignore[arg-type]
            provenance=provenance(),
            rows=(row,),
        )
    with pytest.raises(ValidationError, match="Input should be 'unknown'"):
        invalid_values = {
            **provenance().model_dump(),
            "source_authorization_status": "authorized",
        }
        IdentityBoundStockV2RawProvenance.model_validate(invalid_values)


def test_provenance_hashes_timestamp_and_resource_bounds_are_strict() -> None:
    """Reject malformed digests, naive timestamps, oversized bodies, and bad row indexes."""
    values = provenance().model_dump()
    with pytest.raises(ValidationError, match="String should match pattern"):
        IdentityBoundStockV2RawProvenance(**{**values, "response_sha256": "not-a-hash"})
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        IdentityBoundStockV2RawProvenance(
            **{**values, "captured_at_utc": datetime(2026, 1, 5, 8, 0)}
        )
    with pytest.raises(ValidationError, match="less than or equal"):
        IdentityBoundStockV2RawProvenance(**{**values, "response_size_bytes": 64 * 1024 * 1024 + 1})
    with pytest.raises(ValidationError, match="less than or equal"):
        IdentityBoundStockDailyV2RawRow(
            trade_date=d("2026-01-02"),
            source_row_index=10_001,
            raw_tokens=("2026-01-02",),
            assignment_claim_status="unknown",
        )


def test_candidate_rejects_duplicate_business_dates_and_source_indexes() -> None:
    """Prevent ambiguous duplicate rows from representing one daily business key."""
    pair = MarketSymbolPair(Exchange.XSHG, "600007")
    identity = assignment("assignment", "instrument-7", "XSHG", "600007", "2026-01-01", None)
    repeated_date = (
        raw_row("2026-01-02", source_row_index=1, assignment_value=identity),
        raw_row("2026-01-02", source_row_index=2, assignment_value=identity),
    )
    with pytest.raises(ValidationError, match="duplicate dates or row indexes"):
        candidate(pair, "2026-01-01", "2026-01-04", repeated_date)
    repeated_index = (
        raw_row("2026-01-02", source_row_index=1, assignment_value=identity),
        raw_row("2026-01-03", source_row_index=1, assignment_value=identity),
    )
    with pytest.raises(ValidationError, match="duplicate dates or row indexes"):
        candidate(pair, "2026-01-01", "2026-01-04", repeated_index)


def test_assignment_and_pin_mapping_objects_must_be_prevalidated_values() -> None:
    """Require domain constructors before DTO binding instead of coercing identity maps."""
    with pytest.raises(ValidationError, match="validated TemporalIdentityAssignment"):
        IdentityBoundStockDailyV2RawRow(
            trade_date=d("2026-01-02"),
            source_row_index=1,
            raw_tokens=("2026-01-02",),
            assignment_claim_status="caller_declared_unverified",
            assignment={
                "assignment_id": "assignment",
                "instrument_id": "instrument-8",
                "exchange": "XSHG",
                "symbol": "600008",
                "valid_from": d("2026-01-01"),
                "valid_to": None,
                "source_id": "akshare",
                "source_version": "fixture-v1",
                "evidence_ref": "TEST-EVIDENCE",
            },  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError, match="validated TemporalIdentityPin"):
        IdentityBoundStockDailyV2RawCandidate(
            request_pair=MarketSymbolPair(Exchange.XSHG, "600008"),
            window=IdentityBoundStockDailyV2Window(
                start=d("2026-01-01"), end_exclusive=d("2026-01-04")
            ),
            identity_pin={"dataset_id": "stock-daily-fixture"},  # type: ignore[arg-type]
            provenance=provenance(),
            rows=(
                raw_row(
                    "2026-01-02",
                    source_row_index=1,
                    assignment_value=assignment(
                        "assignment", "instrument-8", "XSHG", "600008", "2026-01-01", None
                    ),
                ),
            ),
        )
